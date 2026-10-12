import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import agent_status  # noqa: E402
import qwen_night as qn  # noqa: E402

KICK_M = {"dur_s": 0.4, "peak_db": -0.5, "rms_db": -12.0, "crest_db": 9.0, "centroid_hz": 400, "low_ratio": 0.7,
          "high_ratio": 0.01, "attack_ms": 5.0, "bpm": None, "key": None}
HAT_M = {**KICK_M, "centroid_hz": 8000, "low_ratio": 0.01, "high_ratio": 0.5, "dur_s": 0.15, "crest_db": 14.0}


def test_path_hint_beats_the_model_and_unsupported_tags_drop():
    v, dropped = qn.check("Hardtek Pack/Kicks/K 07.wav", KICK_M,
                          {"role": "bass", "tags": ["short", "bright", "distorted", "made-up"], "note": "Big 150 bpm kick"})
    assert v["role"] == "kick" and v["tags"] == ["short", "distorted"]
    assert "bpm" in v["note"] and not any(c.isdigit() for c in v["note"])
    assert {"role bass->kick (path)", "tag bright", "tag made-up"} <= set(dropped)


def test_role_must_fit_the_measurements_without_a_path_hint():
    v, dropped = qn.check("misc/one.wav", HAT_M, {"role": "kick", "tags": ["bright", "airy"], "note": "tick"})
    assert v["role"] == "other" and "role kick (measurements)" in dropped and v["tags"] == ["bright", "airy"]
    v, _ = qn.check("misc/one.wav", HAT_M, {"role": "nonsense"})
    assert v["role"] == "other" and v["tags"] == []


def write_wavs(lib):
    np = pytest.importorskip("numpy")
    sf = pytest.importorskip("soundfile")
    sr = 44100
    t = np.arange(int(sr * 0.4)) / sr
    (lib / "Pack" / "Kicks").mkdir(parents=True)
    sf.write(lib / "Pack" / "Kicks" / "hard.wav", (0.9 * np.sin(2 * np.pi * 55 * t) * np.exp(-t * 8)).astype("float32"), sr)
    rng = np.random.default_rng(1)
    noise = np.diff(rng.standard_normal(int(sr * 0.1)), prepend=0) * 0.3
    sf.write(lib / "odd.wav", noise.astype("float32"), sr)
    (lib / "broken.wav").write_bytes(b"not audio")


def test_tag_night_measures_checks_and_resumes(tmp_path, monkeypatch):
    lib, night = tmp_path / "lib", tmp_path / "night"
    write_wavs(lib)
    monkeypatch.setattr(qn, "LIB", lib)
    monkeypatch.setattr(qn, "NIGHT", night)
    monkeypatch.setattr(agent_status, "DIR", tmp_path / "agents")
    seen = []

    def chat(system, user, max_tokens=2048):
        rows = [json.loads(x) for x in user.splitlines()]
        seen.extend(rows)
        return "here you go: " + json.dumps({"items": [
            {"id": r["id"], "role": "kick" if "Kicks" in r["path"] else "hat", "tags": ["short", "boomy", "bright"],
             "note": "a 909-ish thing"} for r in rows]})
    monkeypatch.setattr(qn, "chat", chat)
    assert qn.tag(100, False) == 0
    db = json.loads((night / "tags.json").read_text())
    assert db["Pack/Kicks/hard.wav"]["role"] == "kick" and "boomy" in db["Pack/Kicks/hard.wav"]["tags"]
    assert "bright" not in db["Pack/Kicks/hard.wav"]["tags"]
    assert db["odd.wav"]["role"] == "hat" and "boomy" not in db["odd.wav"]["tags"]
    assert "error" in db["broken.wav"]
    assert not any(c.isdigit() for c in db["odd.wav"]["note"])
    assert all("path" in r and "low_ratio" in r for r in seen)
    last = json.loads((night / "tag_last.json").read_text())
    assert last["tagged"] == 2 and last["unreadable"] == 1
    assert agent_status.read("qwen-night")["state"] == "done"
    seen.clear()
    qn.tag(100, False)  # resumable: nothing left to do
    assert seen == []


def test_model_down_still_records_measurements(tmp_path, monkeypatch):
    lib, night = tmp_path / "lib", tmp_path / "night"
    write_wavs(lib)
    monkeypatch.setattr(qn, "LIB", lib)
    monkeypatch.setattr(qn, "NIGHT", night)
    monkeypatch.setattr(agent_status, "DIR", tmp_path / "agents")

    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(qn, "chat", boom)
    qn.tag(100, False)
    db = json.loads((night / "tags.json").read_text())
    assert db["Pack/Kicks/hard.wav"]["role"] == "kick" and db["Pack/Kicks/hard.wav"]["by"] == "runner"
    assert json.loads((night / "tag_last.json").read_text())["model_errors"] == 1


def digest_env(tmp_path, monkeypatch):
    for k, d in (("FB", "fb"), ("HANDOFF", "handoff"), ("CC_JOBS", "cc"), ("NIGHT", "night")):
        (tmp_path / d).mkdir()
        monkeypatch.setattr(qn, k, tmp_path / d)
    (tmp_path / "fb" / "handoff").mkdir()
    (tmp_path / "fb" / "runs").mkdir()
    monkeypatch.setattr(agent_status, "DIR", tmp_path / "agents")
    monkeypatch.setattr(qn.llm, "LOCAL", ("http://127.0.0.1:9", "x"))  # nothing listens: local model not answering
    monkeypatch.setattr(qn.llm, "GATEWAY", ("", "auto"))
    db = tmp_path / "kanban.db"
    con = sqlite3.connect(db)
    con.execute("create table tasks (title text, status text)")
    con.executemany("insert into tasks values (?, ?)", [("rm-vite", "review"), ("palette", "ready"), ("old", "archived")])
    con.commit()
    con.close()
    monkeypatch.setattr(qn, "KANBAN_DB", db)
    (tmp_path / "cc" / "tm001-loop.log").write_text("working\nDelivered A and B to handoff/tm-001\n")
    (tmp_path / "fb" / "handoff" / "rm-x.md").write_text("STATUS: blocked\n")
    agent_status.start("cc-tm001", "opus", "tm-001", "loop")


def test_digest_facts(tmp_path, monkeypatch):
    digest_env(tmp_path, monkeypatch)
    f = "\n".join(qn.facts())
    assert "agent cc-tm001 (opus): working" in f
    assert "Claude Code job tm001-loop" in f and "Delivered A and B" in f
    assert "fallback hand-off note rm-x: blocked" in f
    assert "kanban card 'rm-vite' is review" in f and "1 ready" in f and "archived" not in f
    assert "local model: NOT answering" in f and "data disk" in f


def test_digest_posts_summary_and_facts_and_survives_model_down(tmp_path, monkeypatch):
    digest_env(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(qn, "mail", lambda body, thread: sent.append((thread, body)) or True)
    monkeypatch.setattr(qn, "chat", lambda *a, **k: "\n".join(f"line {i}" for i in range(9)))
    assert qn.digest(False) == 0
    thread, body = sent[-1]
    assert thread == "morning-report" and "line 4" in body and "line 5" not in body and "Facts:" in body
    assert agent_status.read("qwen-night")["state"] == "done"

    def boom(*a, **k):
        raise OSError("down")
    monkeypatch.setattr(qn, "chat", boom)
    qn.digest(False)
    assert "facts only" in sent[-1][1] and "kanban card 'rm-vite'" in sent[-1][1]


def test_digest_dry_run_posts_nothing(tmp_path, monkeypatch, capsys):
    digest_env(tmp_path, monkeypatch)
    monkeypatch.setattr(qn, "mail", lambda *a: pytest.fail("posted in dry run"))
    monkeypatch.setattr(qn, "chat", lambda *a, **k: "ok")
    agent_status.done("cc-tm001")
    before = agent_status.read("qwen-night")
    qn.digest(True)
    assert "Facts:" in capsys.readouterr().out and agent_status.read("qwen-night") == before


class FakeClap:
    """Hears a kick in anything under Kicks/ and a metallic hat in the rest."""

    def listen(self, clips):
        import numpy as np
        out, emb = [], []
        for p in clips:
            kick = "Kicks" in str(p)
            out.append({"clap_role": "kick" if kick else "hat", "clap_role_p": 0.9 if kick else 0.4,
                        "clap_tags": ["gabber"] if kick else ["metallic"]})
            emb.append([1.0, 0.0] if kick else [0.0, 1.0])
        return out, np.array(emb, dtype="float32")


def test_clap_role_and_tags_are_evidence():
    m = {**HAT_M, "clap_role": "perc", "clap_role_p": 0.8, "clap_tags": ["metallic", "industrial"]}
    v, dropped = qn.check("misc/one.wav", m, {"role": "hat", "tags": ["industrial", "gabber", "bright"]})
    assert v["role"] == "perc" and "role hat->perc (clap)" in dropped
    assert v["tags"] == ["industrial", "bright"] and "tag gabber" in dropped
    v, _ = qn.check("Pack/Kicks/x.wav", {**KICK_M, "clap_role": "bass", "clap_role_p": 0.99}, {"role": "kick"})
    assert v["role"] == "kick"  # the path still wins


def test_tag_with_clap_builds_the_search_index(tmp_path, monkeypatch, capsys):
    lib, night = tmp_path / "lib", tmp_path / "night"
    write_wavs(lib)
    for k, v in (("LIB", lib), ("NIGHT", night), ("clip48", lambda p: p), ("clap_or_none", lambda log=None: FakeClap())):
        monkeypatch.setattr(qn, k, v)
    monkeypatch.setattr(agent_status, "DIR", tmp_path / "agents")
    monkeypatch.setattr(qn, "chat", lambda *a, **k: json.dumps({"items": [{"id": 0, "role": "bass", "tags": ["gabber"]},
                                                                        {"id": 1, "role": "kick", "tags": ["metallic"]}]}))
    qn.tag(100, False)
    db = json.loads((night / "tags.json").read_text())
    assert db["Pack/Kicks/hard.wav"]["tags"] == ["gabber"] and db["Pack/Kicks/hard.wav"]["clap_role"] == "kick"
    assert db["odd.wav"]["role"] == "other" and db["odd.wav"]["tags"] == ["metallic"]  # unsure CLAP, kick fails the check
    assert json.loads((night / "clap" / "paths.json").read_text()) == ["Pack/Kicks/hard.wav", "odd.wav"]
    qn.save_emb(["odd.wav"], __import__("numpy").array([[0.0, 1.0]], "float32"))  # re-embedding never duplicates
    assert json.loads((night / "clap" / "paths.json").read_text()) == ["Pack/Kicks/hard.wav", "odd.wav"]
    assert qn.find(None, "Pack/Kicks/hard.wav", 5) == 0
    out = capsys.readouterr().out
    assert "odd.wav" in out and "[other: metallic]" in out


def test_pack_folder_name_never_decides_the_role():
    assert qn.role_hint("@pluggsupply samples loops/Breaks/Bubble Break 01.wav") is None
    assert qn.role_hint("Ghosthack - Carbon - Acid Techno/Kicks/K1.wav") == "kick"
    assert qn.role_hint("Ghosthack - Carbon - Acid Techno/Synths/S1.wav") == "synth"
    assert qn.role_hint("Pack/Loops/G-Loop-LP-Filtered.wav") == "loop"


def test_digest_says_when_it_cannot_read_services_and_a_job_is_running(tmp_path, monkeypatch):
    digest_env(tmp_path, monkeypatch)

    class R:
        returncode, stdout, stderr = 1, "", "Failed to connect to bus: No medium found"
    monkeypatch.setattr(qn.subprocess, "run", lambda *a, **k: R())
    monkeypatch.setattr(qn, "job_running", lambda name: name == "tm001-loop")
    f = "\n".join(qn.facts())
    assert "failed services: unknown (systemctl: Failed to connect to bus" in f
    assert "Claude Code job tm001-loop: running" in f


def test_samples_tagged_without_clap_are_heard_again_once_clap_works(tmp_path, monkeypatch):
    lib, night = tmp_path / "lib", tmp_path / "night"
    write_wavs(lib)
    for k, v in (("LIB", lib), ("NIGHT", night), ("clip48", lambda p: p), ("clap_or_none", lambda log=None: None)):
        monkeypatch.setattr(qn, k, v)
    monkeypatch.setattr(agent_status, "DIR", tmp_path / "agents")
    monkeypatch.setattr(qn, "chat", lambda *a, **k: "{}")
    qn.tag(100, False)
    assert "clap_role" not in json.loads((night / "tags.json").read_text())["odd.wav"]
    monkeypatch.setattr(qn, "clap_or_none", lambda log=None: FakeClap())
    qn.tag(100, False)
    db = json.loads((night / "tags.json").read_text())
    assert db["odd.wav"]["clap_role"] == "hat" and "error" in db["broken.wav"]  # broken files are not retried
