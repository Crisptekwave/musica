import json

from tekno_panel import demo, files, machine, mail
from tekno_panel.state import merge_tracks


def test_scan_cards(cards):
    s = files.scan(cards)
    paths = {(a["card"], a["path"]) for a in s["audio"]}
    assert ("e2e-001", "polish/master.wav") in paths
    assert ("e2e-001", "stems/bass.wav") in paths
    assert ("dry-001", "gen/take_1.flac") in paths
    assert not any(a["path"].endswith(".json") for a in s["audio"])
    kinds = {a["path"]: a["kind"] for a in s["audio"]}
    assert kinds["stems/bass.wav"] == "stem" and kinds["polish/master.wav"] == "render"
    assert s["files_lane"]["e2e-001"] == "Score"
    assert s["files_lane"]["dry-001"] == "Generate"


def test_score_badges_real_score(cards):
    sc = files.card_score(cards / "e2e-001")
    b = {x["label"]: x for x in sc["badges"]}
    assert sc["verdict"] == "NO-GO" and not sc["pass"]
    assert b["LUFS-I"]["status"] == "fail"          # -9.8 is outside -9 +-0.5
    assert b["True peak"]["status"] == "pass"       # -1.1 <= -1.0
    assert b["Sub mono S/M"]["status"] == "pass"
    assert b["Stereo corr"]["status"] == "pass"
    assert b["BPM"]["status"] == "fail"             # listed in hard_fails
    assert b["Key"]["status"] == "pass"
    assert b["EarLoop"]["status"] == "na"
    assert b["Mix Judge"]["status"] == "na" and b["Mix Judge"]["draft"] == 6.4
    assert b["Monotony"]["status"] == "na"


def test_judge_md_parsed(cards):
    (cards / "e2e-001" / "score" / "judge.md").write_text("# Mix Judge\nOverall: 7.2 / 10\nVerdict: GO\n")
    b = {x["label"]: x for x in files.card_score(cards / "e2e-001")["badges"]}
    assert b["Mix Judge"]["status"] == "pass" and "7.2" in b["Mix Judge"]["value"]


def test_tail(tmp_path):
    p = tmp_path / "log"
    p.write_text("a\nb\r 12%|###\nc\n")
    assert files.tail(p, 2) == [" 12%|###", "c"]
    assert files.tail(tmp_path / "missing") == []


def test_disk(tmp_path):
    d = machine.disk(tmp_path, 10**9)
    assert d["ok"] and d["warn"] is True
    assert machine.disk(tmp_path, 0)["warn"] is False


def test_ace_job_from_lock(tmp_path):
    lock = tmp_path / ".gpu.lock"
    import os
    lock.write_text(f"{os.getpid()} /mnt/agent-data/tekno_machine/cards/e2e-001\n")
    log = tmp_path / "ace.log"
    log.write_text("x | INFO | step\n 40%|####      | 20/50 [00:10<00:15]\n")
    a = machine.ace("http://127.0.0.1:9", lock, log)
    assert a["job"]["card"] == "e2e-001" and a["status"] == "generating"
    assert a["progress"] == 40


def test_ace_stale_lock(tmp_path):
    lock = tmp_path / ".gpu.lock"
    lock.write_text("999999999 /x/cards/old\n")
    a = machine.ace("http://127.0.0.1:9", lock, tmp_path / "none")
    assert a["job"] is None and a["status"] == "down"


def test_service_falls_back_to_down():
    s = machine.service({"name": "x", "http": "http://127.0.0.1:9/health"})
    assert s["status"] == "down"


def test_merge_tracks_adds_offboard_and_ace_progress():
    board = [{"slug": "a", "lane": "Generate", "state": "queued", "progress": 8, "created_at": 1}]
    disk = {"files_lane": {"a": "Generate", "b": "Mix"}}
    ace = {"job": {"card": "a"}, "progress": 50}
    t = {x["slug"]: x for x in merge_tracks(board, disk, ace, 0)}
    assert t["a"]["state"] == "working" and t["a"]["progress"] == round(100 * 1.5 / 12)
    assert t["b"]["state"] == "offboard" and t["b"]["lane"] == "Mix"


def test_mail_is_read_only():
    m = mail.AgentMail("http://127.0.0.1:9/mcp/", "p", None)
    import pytest
    with pytest.raises(PermissionError):
        m._post("tools/call", {"name": "send_message"})
    with pytest.raises(PermissionError):
        m.read("resource://anything-else/x")


def test_mail_normalize():
    msgs = mail.normalize_messages({"messages": [
        {"id": 3, "thread_id": "card-x", "from": "Grok Bot", "to": ["Mix Judge"], "subject": "s", "body_md": "hi",
         "created_ts": "2026-10-11T09:00:00Z"}]})
    assert msgs[0]["from"] == "Grok Bot" and msgs[0]["thread"] == "card-x" and msgs[0]["body"] == "hi"


def test_bearer_from_hermes(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("mcp_servers:\n  agent-mail:\n    url: http://x/mcp/\n    headers:\n      Authorization: Bearer abc.DEF-123\n")
    assert mail.bearer_from_hermes(cfg) == "abc.DEF-123"
    assert mail.bearer_from_hermes(tmp_path / "missing") is None


def test_demo_snapshot_shape():
    s = demo.snapshot(1_791_700_000)
    assert s["mode"] == "demo" and len(s["tracks"]) == 7
    assert set(s) >= {"tracks", "lanes", "audio", "scores", "health", "chat", "reference"}
    assert all(t["lane"] in ("Done",) or t["lane"] in s["lanes"] for t in s["tracks"])
    json.dumps(s)  # must be JSON-serialisable for the WebSocket


def test_demo_tracks_move_over_time():
    a = {t["slug"]: t["lane"] for t in demo.tracks(1_791_700_000)}
    b = {t["slug"]: t["lane"] for t in demo.tracks(1_791_700_000 + demo.STEP_S * 2)}
    assert a != b


def test_demo_wav_is_valid():
    import io
    import wave
    with wave.open(io.BytesIO(demo.demo_wav("kick"))) as w:
        assert w.getframerate() == demo.SR and w.getnframes() > demo.SR * 5
