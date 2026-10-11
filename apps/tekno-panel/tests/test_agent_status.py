import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import agent_status as ag  # noqa: E402


def test_start_step_done(tmp_path):
    r = ag.start("panel-v3", "sonnet", "panel", "panel v3 " + "x" * 400, tmp_path, now=1000)
    assert r["state"] == "working" and len(r["task"]) == 160
    ag.step("panel-v3", "writing\ntests", tmp_path, now=1100)
    d = json.loads((tmp_path / "panel-v3.json").read_text())
    assert d["step"] == "writing tests" and d["updated"] == 1100 and d["started"] == 1000
    assert oct((tmp_path / "panel-v3.json").stat().st_mode & 0o777) == "0o664"
    assert not list(tmp_path.glob(".*.tmp"))
    ag.done("panel-v3", "ok", "all green", tmp_path, now=1200)
    d = ag.read("panel-v3", tmp_path)
    assert d["state"] == "done" and d["result"] == "all green"
    assert ag.done("panel-v3", "failed", "", tmp_path, now=1300)["state"] == "failed"


def test_history_capped(tmp_path):
    ag.start("a", "opus", "", "t", tmp_path, now=1)
    for i in range(30):
        ag.step("a", f"s{i}", tmp_path, now=2 + i)
    h = ag.read("a", tmp_path)["history"]
    assert len(h) == ag.HISTORY and h[-1]["step"] == "s29"


def test_validation(tmp_path):
    for bad in ("Panel", "a/b", "", "x" * 41, "../etc", "a_b"):
        with pytest.raises(ValueError):
            ag.start(bad, "sonnet", "", "t", tmp_path)
    with pytest.raises(ValueError):
        ag.start("ok", "gpt", "", "t", tmp_path)
    with pytest.raises(ValueError):
        ag.start("ok", "qwen", "../x", "t", tmp_path)
    with pytest.raises(ValueError):
        ag.step("never-started", "x", tmp_path)
    with pytest.raises(ValueError):
        ag.done("never-started", "ok", "", tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_roster_staleness_and_hiding(tmp_path):
    now = 10_000_000
    ag.start("busy", "sonnet", "c1", "t", tmp_path, now=now - 60)
    ag.start("silent", "opus", "c2", "t", tmp_path, now=now - 3000)
    ag.start("old-done", "fable", "c3", "t", tmp_path, now=now - 200_000)
    ag.done("old-done", "ok", "", tmp_path, now=now - 100_000)
    ag.start("fresh-done", "qwen", "c4", "t", tmp_path, now=now - 500)
    ag.done("fresh-done", "failed", "boom", tmp_path, now=now - 400)
    (tmp_path / "junk.json").write_text("{not json")
    (tmp_path / "Bad Name.json").write_text("{}")
    r = {x["name"]: x for x in ag.roster(tmp_path, now)}
    assert set(r) == {"busy", "silent", "fresh-done"}
    assert r["silent"]["stale"] and not r["busy"]["stale"] and not r["fresh-done"]["stale"]
    assert r["fresh-done"]["state"] == "failed"
    assert [x["name"] for x in ag.roster(tmp_path, now)][0] in ("busy", "silent")  # working first


def test_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ag, "DIR", tmp_path)
    assert ag.main(["start", "cli-agent", "--model", "sonnet", "--card", "x-1", "--task", "demo"]) == 0
    assert ag.main(["step", "cli-agent", "mixing"]) == 0
    assert ag.main(["done", "cli-agent", "--result", "stopped", "bye"]) == 0
    assert ag.read("cli-agent")["state"] == "stopped"
    assert ag.main(["step", "ghost", "x"]) == 2
    assert "cli-agent" in (ag.main(["list"]), capsys.readouterr().out)[1]
