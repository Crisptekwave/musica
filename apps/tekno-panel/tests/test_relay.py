import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import alert_relay  # noqa: E402


def db(tmp_path, rows):
    p = tmp_path / "history.db"
    con = sqlite3.connect(p)
    con.execute("create table events (id text primary key, ts real, kind text, card text, level text, text text)")
    con.executemany("insert into events values (?, ?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()
    return p


def test_problems_dedupe_and_filter(tmp_path):
    now = 10_000.0
    p = db(tmp_path, [
        ("alert:failed:e2e-001:9000", 9000, "alert", "e2e-001", "warn", "e2e-001 stuck in Score"),
        ("alert:failed:e2e-001:9500", 9500, "alert", "e2e-001", "warn", "e2e-001 stuck in Score"),  # same problem, re-seen
        ("alert:review:x:9600", 9600, "alert", "x", "info", "x waits for your review"),            # info: not posted
        ("alert:disk:9700", 9700, "alert", None, "crit", "Disk low: 9 GB free"),
        ("act:9800:lane.run", 9800, "action", "e2e-001", "warn", "you: Run e2e-001 from plan (failed)"),
        ("act:9801:card.note", 9801, "action", "e2e-001", "ok", "you: Note on t_1"),
        ("alert:old:1000", 1000, "alert", None, "warn", "too old"),
    ])
    items = alert_relay.problems(p, now)
    assert [i["key"] for i in items] == ["act:9800:lane.run", "alert:disk", "alert:failed:e2e-001"]


def test_due_reposts_only_after_cooldown():
    items = [{"key": "a"}, {"key": "b"}, {"key": "c"}]
    now = 100_000.0
    state = {"a": now - 60, "b": now - alert_relay.REPOST_S - 1}
    assert [i["key"] for i in alert_relay.due(items, state, now)] == ["b", "c"]


def test_message_mentions_card_and_critical():
    subject, body = alert_relay.message([
        {"level": "crit", "text": "Disk low: 9 GB free", "card": None},
        {"level": "warn", "text": "stuck in Score", "card": "e2e-001"},
    ])
    assert subject == "Panel: 2 new problems (1 critical)"
    assert "[crit] Disk low" in body and "(card e2e-001)" in body


def test_first_run_remembers_without_posting(tmp_path, monkeypatch):
    p = db(tmp_path, [("alert:disk:9700", 9700, "alert", None, "crit", "Disk low")])
    monkeypatch.setattr(alert_relay, "DB", p)
    monkeypatch.setattr(alert_relay, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(alert_relay.time, "time", lambda: 10_000.0)
    sent = []
    monkeypatch.setattr(alert_relay.team_mail, "call", lambda *a, **k: sent.append(a))
    assert alert_relay.main([]) == 0
    assert not sent and "alert:disk" in json.loads((tmp_path / "state.json").read_text())
    # a new problem on the next run is posted, the remembered one is not
    con = sqlite3.connect(p)
    con.execute("insert into events values ('alert:svc:acestep-api:9900', 9900, 'alert', null, 'crit', 'acestep-api is down')")
    con.commit()
    con.close()
    monkeypatch.setattr(alert_relay, "panel_token", lambda: ("Tekno-Panel", "tok"))
    monkeypatch.setattr(alert_relay.team_mail, "team", lambda: {"hermes": "CopperHeron", "grok": "LavenderElk"})
    assert alert_relay.main([]) == 0
    args = sent[0][1]
    assert args["to"] == ["CopperHeron", "LavenderElk"] and args["thread_id"] == "panel-errors"
    assert "acestep-api is down" in args["body_md"] and "Disk low" not in args["body_md"]


def test_team_mail_finds_identities_without_home(tmp_path, monkeypatch):
    import importlib
    import team_mail
    am = tmp_path / "real" / ".config" / "agent-mail"
    am.mkdir(parents=True)
    (am / "identities.json").write_text(json.dumps({"hermes": {"name": "CopperHeron", "token_file": str(am / "h.token")}}))
    (tmp_path / "real" / ".hermes").mkdir()
    (tmp_path / "real" / ".hermes" / "config.yaml").write_text(
        "mcp_servers:\n  agent-mail:\n    url: http://x:8765/mcp/\n    headers:\n      Authorization: Bearer abc123\n")
    monkeypatch.setenv("HOME", str(tmp_path / "profile-home"))  # a Hermes lane worker's HOME
    monkeypatch.delenv("AGENT_MAIL_DIR", raising=False)
    monkeypatch.delenv("AGENT_MAIL_BEARER", raising=False)
    monkeypatch.setattr(team_mail, "HERE", am)
    monkeypatch.setattr(team_mail, "DIR", team_mail._dir())
    assert team_mail.DIR == am
    assert team_mail.team() == {"hermes": "CopperHeron", "grok": "LavenderElk"}
    assert team_mail.bearer() == "abc123"
