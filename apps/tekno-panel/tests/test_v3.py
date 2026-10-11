"""Panel v3: studio (REAPER view), palette, local model / fallback, styles, references, agents, brief, fallback.mode."""
import json
import os
import sys
import time
from pathlib import Path

import pytest

from tekno_panel import agents, files, local, palette, studio
from tekno_panel.control import ActionError, Controller
from tekno_panel.helper import handle
from tests.test_server import client, login, runner  # noqa: F401  (fixtures)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import reaper_snap  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 300 + b"\xff\xd9"


# ---------------------------------------------------------------- brief / styles on track cards
def test_brief_summary(cards):
    c = cards / "e2e-001"
    (c / "study" / "brief.json").write_text(json.dumps(
        {"style": "acid_tekno", "styles": ["tekno", "mental"], "model": "Opus", "agent": "sk-0930  builder"}))
    b = files.brief_summary(c)
    assert b == {"style": "acid_tekno", "styles": ["acid_tekno", "tekno", "mental"], "model": "opus", "agent": "sk-0930 builder"}
    (c / "study" / "brief.json").write_text(json.dumps({"bpm": 167, "model": "gpt-9"}))
    assert files.brief_summary(c) is None  # unknown model ignored, nothing else to show
    (c / "study" / "brief.json").write_text("not json")
    assert files.brief_summary(c) is None


def test_brief_reaches_the_snapshot(client, settings):
    (settings.cards_path / "e2e-001" / "study" / "brief.json").write_text('{"style": "gabber", "model": "sonnet"}')
    s = login(client).get("/api/state").json()
    t = {x["slug"]: x for x in s["tracks"]}
    assert t["e2e-001"]["brief"]["style"] == "gabber" and t["e2e-001"]["brief"]["model"] == "sonnet"
    assert t["dry-001"]["brief"] is None
    for k in ("local", "fallback", "styles", "references", "agents"):
        assert k in s


def test_ace_disabled_does_not_probe(settings):
    from tekno_panel.state import Collector
    settings.ace_enabled = False
    snap = Collector(settings, runner=lambda *a, **k: None).refresh()
    assert snap["health"]["ace"]["enabled"] is False and snap["health"]["journal"] == []


# ---------------------------------------------------------------- local model, fallback, styles, references
def test_local_model(monkeypatch):
    monkeypatch.setattr(local.machine, "http_json", lambda url, t=2.0: {"data": [{"id": "qwen3-coder", "max_model_len": 65536}]})
    gpu = {"ok": True, "mem_used": 20000, "mem_total": 32000, "apps": [{"name": "vLLM", "mem": 18000}, {"name": "x", "mem": 5}]}
    m = local.local_model("http://127.0.0.1:8001", gpu)
    assert m["ok"] and m["model"] == "qwen3-coder" and m["gpu"]["model_mem"] == 18000
    monkeypatch.setattr(local.machine, "http_json", lambda url, t=2.0: None)
    assert local.local_model("http://x", None) == {"ok": False, "url": "http://x", "model": None, "max_len": None}


NOTE = "STATUS: blocked\nCARD: t_225ce01d\nDIR: /x/cards/rm\nNEXT:\n1. 01-a.sh\nNOT ALLOWED: all else\nLOG:\n- 19:00 [qwen] a\n- 19:01 [runner] b\n"


def test_parse_note():
    n = local.parse_note(NOTE)
    assert n["state"] == "blocked" and n["card"] == "t_225ce01d" and n["next"] == ["1. 01-a.sh"]
    assert n["log"] == ["- 19:00 [qwen] a", "- 19:01 [runner] b"]
    assert local.parse_note("STATUS: claimed-by-qwen 2026\n")["state"] == "claimed"
    assert local.parse_note("")["state"] == "unknown"


def test_fallback_box(tmp_path):
    fb = tmp_path / "fb"
    (fb / "handoff").mkdir(parents=True)
    (fb / "runs").mkdir()
    (fb / "MODE").write_text("auto\n")
    (fb / "claude_heartbeat").touch()
    (fb / "handoff" / "rm.md").write_text(NOTE)
    (fb / "handoff" / "ok.md").write_text("STATUS: done\n")
    for i in range(7):
        r = fb / "runs" / f"r{i}.log"
        r.write_text("tool x -> 'y'\ntool finish {\"status\": \"done\", \"reason\": \"ok\"} -> 'finished'\n")
        os.utime(r, (1000 + i, 1000 + i))
    out = local.fallback(fb, True, time.time() + 90)
    assert out["mode"] == "auto" and out["writable"] and 80 < out["heartbeat_age"] < 100
    assert [n["slug"] for n in out["notes"]] == ["rm"] and out["done_notes"] == 1
    assert out["notes"][0]["log"][-1].endswith("b")
    assert len(out["runs"]) == 5 and out["runs"][0]["name"] == "r6.log" and out["runs"][0]["result"] == "done"
    assert local.fallback(tmp_path / "missing", None)["present"] is False


def test_styles_defensive(tmp_path):
    p = tmp_path / "house_profile.json"
    assert local.styles(p) == []
    p.write_text(json.dumps({"name": "x"}))
    assert local.styles(p) == []
    p.write_text(json.dumps({"presets": {"tekno": {"bpm": [160, 175], "status": "Provisional"},
                                         "gabber": {"bpm_min": 180, "bpm_max": 200, "calibrated": True},
                                         "odd": 5}}))
    s = {x["name"]: x for x in local.styles(p)}
    assert s["tekno"] == {"name": "tekno", "bpm": [160.0, 175.0], "calibration": "provisional"}
    assert s["gabber"]["bpm"] == [180.0, 200.0] and s["gabber"]["calibration"] == "calibrated" and "odd" not in s
    p.write_text(json.dumps({"styles": [{"name": "mental", "bpm": {"min": 150, "max": 165}, "calibration": {"status": "provisional"}}]}))
    assert local.styles(p) == [{"name": "mental", "bpm": [150.0, 165.0], "calibration": "provisional"}]


def test_references_bom_and_titles(tmp_path):
    (tmp_path / "ref_A.mp3").write_bytes(b"ID3")
    (tmp_path / "ref_B.mp3").write_bytes(b"ID3")
    (tmp_path / "ref_names.json").write_bytes(b"\xef\xbb\xbf" + json.dumps([{"name": "ref_A.mp3", "original": "Älpha - Ünit.mp3"}]).encode())
    (tmp_path / "README.md").write_text("MEASURE ONLY")
    r = local.references(tmp_path)
    assert [i["title"] for i in r["items"]] == ["ref_B", "Älpha - Ünit.mp3"] and r["readme"] == "MEASURE ONLY"
    assert local.references(tmp_path / "nope") is None


# ---------------------------------------------------------------- agents roster
def test_roster(tmp_path):
    now = 5_000_000
    (tmp_path / "a-1.json").write_text(json.dumps({"name": "a-1", "model": "opus", "card": "c", "step": "mixing", "state": "working",
                                                   "updated": now - 1500, "history": [{"at": now - 1500, "step": "x"}]}))
    (tmp_path / "b.json").write_text(json.dumps({"model": "gpt", "state": "done", "updated": now - 90_000}))
    (tmp_path / "c.json").write_text(json.dumps({"model": "qwen", "state": "failed", "updated": now - 100}))
    (tmp_path / "bad.json").write_text("{")
    r = {x["name"]: x for x in agents.roster(tmp_path, now)}
    assert set(r) == {"a-1", "c"} and r["a-1"]["stale"] and r["a-1"]["model"] == "opus"
    assert not r["c"]["stale"] and r["c"]["state"] == "failed"


# ---------------------------------------------------------------- palette
def make_palette(tmp_path):
    root = tmp_path / "pal"
    (root / "kicks").mkdir(parents=True)
    (root / "kicks" / "a.wav").write_bytes(b"RIFF" + b"\0" * 40)
    (root / "secret.json").write_text("{}")
    (root / "README.md").write_text("# Ryan palette")
    idx = {"meta": {"sources": {"fsweep": "Filter Sweep"}}, "sources": {"fsweep": {"bpm": 167}},
           "files": [{"file": "kicks/a.wav", "source": "fsweep", "type": "kick", "bpm": 167.5, "hard_kick": True,
                      "fits_styles": ["gabber", "tekno"]},
                     {"file": "../outside.wav", "type": "kick"}, {"file": "kicks/b.txt"}, {"type": "kick"}]}
    (root / "index.json").write_text(json.dumps(idx))
    return root


def test_palette_load_list_and_dict(tmp_path):
    root = make_palette(tmp_path)
    p = palette.load(root)
    assert [c["file"] for c in p["clips"]] == ["kicks/a.wav"] and p["clips"][0]["hard_kick"] is True
    assert p["types"] == ["kick"] and p["styles"] == ["gabber", "tekno"] and p["sources"] == {"fsweep": "Filter Sweep"}
    assert p["clips"][0]["url"] == "/palette/media/kicks/a.wav" and p["readme"].startswith("# Ryan")
    (root / "index.json").write_text(json.dumps([{"path": "kicks/a.wav", "type": "kick"}]))
    assert palette.load(root)["clips"][0]["name"] == "a.wav"
    (root / "index.json").write_text(json.dumps({"kicks/a.wav": {"type": "kick", "bpm": 150}}))
    assert palette.load(root)["clips"][0]["bpm"] == 150
    (root / "index.json").write_text("junk")
    assert palette.load(root)["clips"] == [] and palette.load(tmp_path / "none")["ok"] is False


def test_palette_routes(client, settings, tmp_path):
    settings.palette_dir = make_palette(tmp_path)
    assert client.get("/api/palette").status_code == 401
    assert client.get("/palette/media/kicks/a.wav").status_code == 401
    login(client)
    assert client.get("/api/palette").json()["clips"][0]["file"] == "kicks/a.wav"
    r = client.get("/palette/media/kicks/a.wav")
    assert r.status_code == 200 and r.content.startswith(b"RIFF")
    for bad in ("secret.json", "../secret.json", "..%2Fsecret.json", "kicks/../../cards/secret.txt", "kicks", "nope.wav"):
        assert client.get("/palette/media/" + bad).status_code == 404, bad


# ---------------------------------------------------------------- REAPER project parser and studio
def test_parse_rpp_fixture():
    r = studio.parse_rpp(FIX / "sample.rpp")
    assert r["tempo"] == 167 and r["sample_rate"] == 48000 and r["render_file"].endswith("mix//premaster.wav")
    assert [(t["name"], t["items"]) for t in r["tracks"]] == [("KICK", 2), ("Acid 303 lead", 1), ("Track 3", 0)]
    assert r["length_s"] == 15.75
    assert studio.parse_rpp(FIX / "missing.rpp") is None


def test_parse_rpp_garbage(tmp_path):
    p = tmp_path / "x.rpp"
    p.write_text("hello\n>\n>\n<TRACK\n")
    assert studio.parse_rpp(p) is None or studio.parse_rpp(p)["tracks"] is not None


def test_display_validation():
    assert [studio.display_number(x) for x in (":104", "104", ":100", "199", ":199")] == [104, 104, 100, 199, 199]
    for bad in (":99", "200", ":1040", ":10", "1x4", "../104", ":104.jpg", "", ":0"):
        assert studio.display_number(bad) is None, bad


def studio_setup(settings, now=None):
    now = now or time.time()
    d = settings.data_dir / "reaper"
    d.mkdir(parents=True)
    card = settings.cards_path / "sk-0930"
    (card / "reaper").mkdir(parents=True)
    (card / "mix").mkdir()
    (card / "reaper" / "sk-0930.rpp").write_text((FIX / "sample.rpp").read_text())
    (card / "mix" / "pre.wav").write_bytes(b"RIFF")
    (d / "104.jpg").write_bytes(JPEG)
    (d / "instances.json").write_text(json.dumps({"ts": now, "instances": [
        {"display": ":104", "n": 104, "pid": 42, "slug": "sk-0930", "project": str(card / "reaper" / "sk-0930.rpp"), "uptime_s": 60},
        {"display": ":103", "n": 103, "pid": 43, "slug": "../etc", "project": "/etc/passwd"},
        {"display": ":7", "pid": 1}]}))
    return d


def test_studio_routes(client, settings):
    assert client.get("/api/reaper").status_code == 401
    assert client.get("/reaper/frame/104.jpg").status_code == 401
    studio_setup(settings)
    login(client)
    r = client.get("/api/reaper")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    ins = {i["n"]: i for i in r.json()["instances"]}
    assert set(ins) == {103, 104}  # ":7" is not a valid display
    i = ins[104]
    assert i["slug"] == "sk-0930" and i["frame_url"] == "/reaper/frame/104.jpg" and i["frame_age"] < 5
    assert i["rpp"]["tempo"] == 167 and i["rpp"]["tracks"][0]["name"] == "KICK" and i["render"]["file"] == "mix/pre.wav"
    assert ins[103]["slug"] is None and ins[103]["rpp"] is None and ins[103]["frame_url"] is None  # bad slug / project ignored
    assert (settings.data_dir / "reaper" / "viewing").exists()  # wakes the grabber
    f = client.get("/reaper/frame/104.jpg")
    assert f.status_code == 200 and f.content == JPEG and f.headers["content-type"] == "image/jpeg"
    assert f.headers["cache-control"] == "no-store"
    assert client.get("/reaper/frame/:104.jpg").status_code == 200
    for bad in ("99", "200", "1040", "abc", "..%2F104", "103"):
        assert client.get(f"/reaper/frame/{bad}.jpg").status_code == 404, bad


def test_studio_without_snapper(client):
    r = login(client).get("/api/reaper").json()
    assert r["instances"] == [] and r["snapper_age"] is None


# ---------------------------------------------------------------- reaper_snap (the capture loop)
def fake_proc(root: Path, pid: int, argv: list[str], env: dict, cwd: str = "/"):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")
    (d / "stat").write_text(f"{pid} (x) S " + " ".join(["0"] * 18) + " 100 0")
    os.symlink(cwd, d / "cwd")


@pytest.fixture
def snap_env(tmp_path, monkeypatch):
    proc, data, cards = tmp_path / "proc", tmp_path / "data" / "reaper", tmp_path / "cards"
    proc.mkdir()
    (proc / "uptime").write_text("5000.0 1.0")
    (cards / "sk-0930" / "reaper").mkdir(parents=True)
    (cards / "sk-0930" / "reaper" / "sk-0930.rpp").write_text("x")
    data.mkdir(parents=True)
    for k, v in (("PROC", proc), ("DATA", data), ("CARDS", cards)):
        monkeypatch.setattr(reaper_snap, k, v)
    return proc, data, cards


def test_snap_scan_pairs_xvfb_and_reaper(snap_env):
    proc, data, cards = snap_env
    fake_proc(proc, 10, ["/usr/bin/Xvfb", ":104", "-extension", "GLX"], {})
    fake_proc(proc, 11, ["/usr/bin/Xvfb", ":103"], {})
    fake_proc(proc, 12, ["/usr/bin/Xvfb", ":99"], {})
    fake_proc(proc, 20, ["/mnt/agent-data/apps/reaper/inst-sk-0930/REAPER/reaper", "-nosplash"], {"DISPLAY": ":104"})
    fake_proc(proc, 21, ["/opt/reaper"], {"DISPLAY": ":105"})           # no Xvfb on :105
    fake_proc(proc, 22, ["/opt/reaper"], {"DISPLAY": ":99"})            # outside 100-199
    fake_proc(proc, 23, ["/opt/reaper"], {})                            # another user's process: environ unreadable
    fake_proc(proc, 24, ["vim", "reaper"], {"DISPLAY": ":103"})         # not REAPER
    fake_proc(proc, 25, ["/x/reaper"], {"DISPLAY": ":103.0", "HOME": "/mnt/agent-data/apps/reaper/inst-fs-002/home"})
    (proc / "30").mkdir()                                                # vanished/empty process
    found = {i["display"]: i for i in reaper_snap.scan()}
    assert set(found) == {":103", ":104"}
    a = found[":104"]
    assert a["pid"] == 20 and a["slug"] == "sk-0930" and a["card"] is True and a["project"].endswith("sk-0930.rpp")
    assert a["uptime_s"] == 5000 - 100 / os.sysconf("SC_CLK_TCK")
    assert found[":103"]["slug"] == "fs-002" and found[":103"]["card"] is False and found[":103"]["project"] is None


def test_snap_project_from_cmdline():
    assert reaper_snap.project_for(["reaper", "-nosplash", "/p/a.rpp"], None) == "/p/a.rpp"
    assert reaper_snap.card_slug(["/cards/x-1/reaper/x-1.rpp"]) == "x-1"


def test_snap_tick_idles_unless_watched(snap_env, monkeypatch):
    proc, data, cards = snap_env
    fake_proc(proc, 10, ["Xvfb", ":104"], {})
    fake_proc(proc, 20, ["reaper"], {"DISPLAY": ":104", "HOME": "/a/inst-sk-0930"})
    grabbed = []

    def fake_grab(n):
        grabbed.append(n)
        (data / f"{n}.jpg").write_bytes(JPEG)
        return True
    monkeypatch.setattr(reaper_snap, "grab", fake_grab)
    frames = {}
    now = time.time()
    reaper_snap.tick(frames, now)
    assert grabbed == [] and json.loads((data / "instances.json").read_text())["instances"][0]["frame_ts"] is None
    (data / "viewing").touch()                                          # the Studio page is open
    reaper_snap.tick(frames, now)
    assert grabbed == [104]
    ins = json.loads((data / "instances.json").read_text())["instances"][0]
    assert ins["frame_ok"] is True and ins["frame_ts"] and ins["slug"] == "sk-0930"
    old = now - 60
    os.utime(data / "viewing", (old, old))                              # nobody watches any more
    reaper_snap.tick(frames, now)
    assert grabbed == [104]
    (proc / "20" / "environ").write_bytes(b"")                          # REAPER gone from :104
    reaper_snap.tick(frames, now)
    assert not (data / "104.jpg").exists() and json.loads((data / "instances.json").read_text())["instances"] == []


def test_snap_grab_atomic_and_failure(snap_env, monkeypatch):
    proc, data, cards = snap_env
    import subprocess

    def ok(cmd, **kw):
        assert "x11grab" in cmd and ":104" in cmd and "scale=1280:-2" in cmd and kw["timeout"] <= 10
        Path(cmd[-1]).write_bytes(JPEG)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")
    monkeypatch.setattr(reaper_snap.subprocess, "run", ok)
    assert reaper_snap.grab(104) and (data / "104.jpg").read_bytes() == JPEG
    assert list(data.glob(".*tmp*")) == []
    monkeypatch.setattr(reaper_snap.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, b"", b"no display"))
    assert not reaper_snap.grab(104)
    assert (data / "104.jpg").read_bytes() == JPEG  # the last good frame stays


# ---------------------------------------------------------------- fallback.mode control
def fb_settings(settings, tmp_path, mode="auto"):
    fb = tmp_path / "fb"
    fb.mkdir()
    (fb / "MODE").write_text(mode + "\n")
    settings.fallback_dir = fb
    return fb


def test_fallback_mode_action(settings, tmp_path):
    from fastapi.testclient import TestClient
    from tekno_panel.server import create_app
    calls = []
    app = create_app(settings, start_collector=False, action_runner=lambda argv, **kw: calls.append(argv) or (0, "", ""))
    with TestClient(app) as client:
        client.passcode = app.state.auth.passcode
        fallback_mode_checks(client, fb_settings(settings, tmp_path), calls)


def fallback_mode_checks(client, fb, calls):
    assert client.post("/api/action", json={"action": "fallback.mode", "params": {"mode": "off"}},
                       headers={"x-tekno": "1"}).status_code == 401
    login(client)
    h = {"x-tekno": "1"}
    r = client.post("/api/action", json={"action": "fallback.mode", "params": {"mode": "off"}}, headers=h)
    assert r.status_code == 403 and (fb / "MODE").read_text() == "auto\n"  # passcode required
    for bad in ("force", "AUTO", "", "off; rm -rf /", "auto\nforce"):
        r = client.post("/api/action", json={"action": "fallback.mode", "params": {"mode": bad, "passcode": client.passcode}}, headers=h)
        assert r.status_code == 400, bad
    assert (fb / "MODE").read_text() == "auto\n"
    r = client.post("/api/action", json={"action": "fallback.mode", "params": {"mode": "off", "passcode": client.passcode}}, headers=h)
    assert r.status_code == 200, r.text
    assert (fb / "MODE").read_text() == "off\n" and calls == []  # no process was started
    rec = client.get("/api/audit").json()["audit"][0]
    assert rec["action"] == "fallback.mode" and rec["ok"] and "passcode" not in json.dumps(rec)


def test_fallback_mode_not_writable(settings, tmp_path):
    fb = fb_settings(settings, tmp_path)
    c = Controller(settings, tmp_path / "d")
    (fb / "MODE").chmod(0o444)
    if os.access(fb / "MODE", os.W_OK):  # running as root: permissions don't apply
        pytest.skip("root can write anything")
    assert c.set_fallback_mode("off")[0] == 126


def test_fallback_mode_through_helper(settings, tmp_path):
    fb = fb_settings(settings, tmp_path)
    ctrl = Controller(settings, tmp_path / "d")
    assert handle(ctrl, {"action": "fallback.mode", "params": {"mode": "off"}}, scan=lambda r: [])["code"] == 0
    assert (fb / "MODE").read_text() == "off\n"
    assert handle(ctrl, {"action": "fallback.mode", "params": {"mode": "force"}}, scan=lambda r: [])["code"] == 125
    assert (fb / "MODE").read_text() == "off\n"
    (fb / "MODE").unlink()
    assert ctrl.set_fallback_mode("auto")[0] == 0 and oct((fb / "MODE").stat().st_mode & 0o777) == "0o664"


def test_acestep_restart_hidden_when_ace_off(settings, tmp_path):
    settings.ace_enabled = False
    c = Controller(settings, tmp_path / "d")
    assert not any(k.startswith("acestep") for k in c.services) and "litellm" in c.services
