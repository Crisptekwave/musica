import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import fallback_watchdog as fw  # noqa: E402
import team_mail  # noqa: E402


def setup(tmp_path, monkeypatch, note_status="open", quiet_s=3600):
    fb, cards = tmp_path / "fallback", tmp_path / "cards"
    (fb / "handoff" / "rm-vite").mkdir(parents=True)
    (cards / "rm-vite" / "out").mkdir(parents=True)
    (fb / "FALLBACK_SOUL.md").write_text("rules")
    step = fb / "handoff" / "rm-vite" / "01-hello.sh"
    step.write_text("echo hello-from-step\n")
    old = time.time() - quiet_s
    for p in [cards / "rm-vite", cards / "rm-vite" / "out"]:
        os.utime(p, (old, old))
    hb = fb / "claude_heartbeat"
    hb.touch()
    os.utime(hb, (old, old))
    note = fb / "handoff" / "rm-vite.md"
    note.write_text(f"STATUS: {note_status}\nCARD: t_225ce01d\nDIR: {cards / 'rm-vite'}\nNEXT:\n1. 01-hello.sh\nLOG:\n")
    monkeypatch.setattr(fw, "FB", fb)
    monkeypatch.setattr(fw, "CARDS", cards)
    monkeypatch.setattr(fw, "llm_up", lambda: True)
    monkeypatch.setattr(fw, "mail", lambda body, thread: None)
    return fb, note


def script(monkeypatch, turns):
    """Fake model: each turn is a list of (tool, args) calls."""
    it = iter(turns)

    def chat(messages):
        calls = next(it, [("finish", {"status": "blocked", "reason": "script ran out"})])
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
            for i, (n, a) in enumerate(calls)]}
    monkeypatch.setattr(fw, "chat", chat)


def test_mode_off_never_runs(tmp_path, monkeypatch):
    fb, note = setup(tmp_path, monkeypatch)
    (fb / "MODE").write_text("off\n")
    ran = []
    monkeypatch.setattr(fw, "run", lambda n, d: ran.append(n) or 0)
    assert fw.main([]) == 0 and ran == []


def test_waits_while_claude_is_active(tmp_path, monkeypatch, capsys):
    setup(tmp_path, monkeypatch, quiet_s=60)
    ran = []
    monkeypatch.setattr(fw, "run", lambda n, d: ran.append(n) or 0)
    fw.main([])
    assert ran == [] and "Claude active" in capsys.readouterr().out


def test_claims_only_open_notes(tmp_path, monkeypatch, capsys):
    setup(tmp_path, monkeypatch, note_status="done")
    fw.main([])
    assert "no open hand-off notes" in capsys.readouterr().out


def test_runs_prepared_step_and_finishes_done(tmp_path, monkeypatch):
    fb, note = setup(tmp_path, monkeypatch)
    script(monkeypatch, [[("read_note", {})], [("run_step", {"name": "01-hello.sh"})],
                         [("append_log", {"text": "01 ok"}), ("finish", {"status": "done", "reason": "all steps ok"})]])
    fw.main([])
    text = note.read_text()
    assert text.startswith("STATUS: done") and "01 ok" in text
    log = next((fb / "runs").glob("*.log")).read_text()
    assert "hello-from-step" in log
    assert not (fb / "run.lock").exists()


def test_model_cannot_run_anything_but_prepared_steps(tmp_path, monkeypatch):
    fb, note = setup(tmp_path, monkeypatch)
    card = fw.Card(note)
    for bad in ["../../evil.sh", "rm -rf /", "01-hello.sh; id", "02-missing.sh", "/bin/sh"]:
        assert card.call("run_step", {"name": bad}).startswith("refused")
    assert card.call("bash", {"cmd": "id"}).startswith("refused")
    assert card.call("finish", {"status": "approved"}).startswith("refused")


def test_run_without_finish_is_stuck_not_retried(tmp_path, monkeypatch):
    fb, note = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(fw, "MAX_TURNS", 2)
    script(monkeypatch, [[("read_note", {})], [("list_steps", {})], [("list_steps", {})]])
    fw.main([])
    assert note.read_text().startswith("STATUS: stuck")
    assert fw.open_notes() == []


def test_model_error_ends_stuck(tmp_path, monkeypatch):
    fb, note = setup(tmp_path, monkeypatch)

    def boom(messages):
        raise OSError("connection refused")
    monkeypatch.setattr(fw, "chat", boom)
    fw.main([])
    assert note.read_text().startswith("STATUS: stuck")


def test_card_dir_must_be_under_cards(tmp_path, monkeypatch):
    fb, note = setup(tmp_path, monkeypatch)
    note.write_text(note.read_text().replace(f"DIR: {tmp_path / 'cards' / 'rm-vite'}", "DIR: /etc"))
    assert fw.Card(note).dir == tmp_path / "cards" / "rm-vite"


def test_only_claude_posts_beat(tmp_path, monkeypatch):
    hb = tmp_path / "claude_heartbeat"
    monkeypatch.setattr(team_mail, "HEARTBEAT", hb)
    team_mail.beat("qwen")
    assert not hb.exists()
    team_mail.beat("claude")
    assert hb.exists()
