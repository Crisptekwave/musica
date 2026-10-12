import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import agent_status  # noqa: E402
import watchdog as wd  # noqa: E402

NOW = 1_800_000_000.0


def ps_with_job(start):
    return {10: {"cmd": ["bash", "run_job.sh", "tm001v2", "opus"], "ppid": 1, "start": start},
            11: {"cmd": ["timeout", "3000", "claude", "-p"], "ppid": 10, "start": start},
            12: {"cmd": ["claude", "-p"], "ppid": 11, "start": start},
            99: {"cmd": ["bash", "run_job.sh"], "ppid": 1, "start": start},  # no job name: not a job
            50: {"cmd": ["vllm"], "ppid": 1, "start": start}}


def test_long_job_warns_and_a_runaway_job_is_stopped_with_only_its_own_tree(monkeypatch):
    killed = []
    monkeypatch.setattr(wd.os, "kill", lambda pid, sig: killed.append(pid))
    found, actions = wd.check_jobs(ps_with_job(NOW - 2.5 * 3600), NOW, dry=False)
    assert found[0]["key"] == "job-long:tm001v2" and not killed
    found, actions = wd.check_jobs(ps_with_job(NOW - 5 * 3600), NOW, dry=False)
    assert found[0]["level"] == "crit" and found[0]["claude"] and sorted(killed) == [10, 11, 12]
    assert "stopped VM job tm001v2" in actions[0]


def transcript(path, calls, tokens_each=100_000, age=60, base=NOW):
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(base - age))
    with open(path, "w") as f:
        for c in calls:
            f.write(json.dumps({"type": "assistant", "timestamp": ts, "message": {
                "usage": {"input_tokens": tokens_each, "output_tokens": 10, "cache_read_input_tokens": 9_999_999},
                "content": [{"type": "tool_use", "name": "Bash", "input": {"command": c}}]}}) + "\n")


def test_tokens_per_hour_ignore_cache_reads_and_a_repeated_call_is_a_loop(tmp_path):
    p = tmp_path / "s.jsonl"
    transcript(p, ["ls", "cat a"] + ["render.sh"] * 8 + ["sleep 60; ls"] * 20)
    s = wd.transcript_stats(p, NOW)
    assert s["tokens_h"] == 30 * 100_010 and s["repeat"] == 8 and "render.sh" in s["last"]  # sleep polling is not a loop
    transcript(p, ["a", "b"], age=7200)
    assert wd.transcript_stats(p, NOW)["tokens_h"] == 0


def test_check_tokens_flags_burn_and_loop(tmp_path, monkeypatch):
    proj = tmp_path / "projects" / "-mnt-agent-data-tekno-machine"
    proj.mkdir(parents=True)
    now = time.time()
    transcript(proj / "abc12345.jsonl", ["x"] * 8, tokens_each=1_000_000, base=now)
    monkeypatch.setattr(wd, "CC_HOME", tmp_path)
    found = {i["key"]: i for i in wd.check_tokens(now)}
    assert set(found) == {"burn:abc12345", "loop:abc12345"} and found["loop:abc12345"]["claude"]
    assert wd.check_tokens(now + 3600) == []  # not written in the last 15 min: not an active session


def test_failed_service_is_restarted_once_then_escalated(monkeypatch):
    calls = []

    def fake(*args):
        calls.append(args)
        out = "tekno-panel.service loaded failed failed X\nvllm-x.service loaded failed failed Y\n" if args[0] == "--failed" else ""
        return subprocess.CompletedProcess(args, 0, out, "")
    monkeypatch.setattr(wd, "systemctl", fake)
    state = {}
    found, actions = wd.check_services(state, NOW, dry=False)
    assert ("restart", "tekno-panel.service") in calls and actions == ["restarted tekno-panel.service (failed)"]
    assert any(i["key"] == "failed:vllm-x.service" and i["claude"] for i in found)  # not on the allowlist
    calls.clear()
    found, actions = wd.check_services(state, NOW + 60, dry=False)
    assert ("restart", "tekno-panel.service") not in calls and not actions
    assert any(i["key"] == "failed:tekno-panel.service" and i["level"] == "crit" for i in found)


def test_tick_mails_new_issues_once_and_keeps_state(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, "WD", tmp_path / "wd")
    monkeypatch.setattr(agent_status, "DIR", tmp_path / "agents")
    monkeypatch.setattr(wd, "procs", lambda: {})
    for name in ("check_tokens", "check_agents", "check_health"):
        monkeypatch.setattr(wd, name, lambda *a: [])
    monkeypatch.setattr(wd, "check_services", lambda *a: ([], []))
    monkeypatch.setattr(wd, "check_logs", lambda *a: [wd.issue("jobfail:x:1", "crit", "VM job x ended with an error",
                                                               claude=True, log="Error: Reached max turns (60)")])
    monkeypatch.setattr(wd, "summarize", lambda i: "The job hit its turn limit.")
    sent = []
    monkeypatch.setattr(wd, "mail", sent.append)
    s = wd.tick(now=NOW)
    assert len(sent) == 1 and "needs Claude" in sent[0] and "turn limit" in sent[0]
    assert s["needs_claude"] == ["VM job x ended with an error"] and "log" not in s["open"][0]
    wd.tick(now=NOW + 300)
    assert len(sent) == 1  # still open, not posted again within 6 h
    wd.tick(now=NOW + 7 * 3600)
    assert len(sent) == 2
    monkeypatch.setattr(wd, "check_logs", lambda *a: [])
    s = wd.tick(now=NOW + 8 * 3600)
    assert s["open"] == [] and s["seen"] == {}
    assert agent_status.read("watchdog")["result"] == "all clear"


def test_a_broken_check_does_not_stop_the_others(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, "WD", tmp_path / "wd")
    monkeypatch.setattr(agent_status, "DIR", tmp_path / "agents")
    monkeypatch.setattr(wd, "procs", lambda: {})
    monkeypatch.setattr(wd, "check_tokens", lambda *a: 1 / 0)
    for name in ("check_agents", "check_health", "check_logs"):
        monkeypatch.setattr(wd, name, lambda *a: [])
    monkeypatch.setattr(wd, "check_services", lambda *a: ([wd.issue("failed:a", "crit", "a failed", claude=True)], []))
    monkeypatch.setattr(wd, "mail", lambda b: None)
    s = wd.tick(now=NOW)
    assert {i["key"] for i in s["open"]} == {"watchdog:ZeroDivisionError", "failed:a"}


def test_usage_is_saved_and_warns_near_the_biggest_block(tmp_path, monkeypatch):
    cc = tmp_path / "ccusage"
    cc.write_text("")
    monkeypatch.setattr(wd, "CCUSAGE", str(cc))
    monkeypatch.setattr(wd, "MACHINE", tmp_path)
    blocks = {"blocks": [
        {"startTime": "2026-10-11T16:00:00Z", "isActive": False, "totalTokens": 40_000_000},
        {"isGap": True, "totalTokens": 0},
        {"startTime": "2026-10-12T00:00:00Z", "endTime": "2026-10-12T05:00:00Z", "isActive": True, "totalTokens": 20_000_000,
         "costUSD": 12.5, "burnRate": {"tokensPerMinute": 150000}, "projection": {"totalTokens": 38_000_000, "remainingMinutes": 140}}]}
    daily = {"daily": [{"date": "2026-10-12", "totalTokens": 25_000_000, "totalCost": 15.2}]}
    monkeypatch.setattr(wd, "ccusage", lambda *a: blocks if a[0] == "blocks" else daily)
    found = wd.check_usage(NOW, dry=False)
    u = json.loads((tmp_path / "usage.json").read_text())
    assert u["block"]["projected"] == 38_000_000 and u["biggest_block"] == 40_000_000 and u["today_tokens"] == 25_000_000
    assert found and "95% of the biggest block" in found[0]["text"]
    blocks["blocks"][2]["projection"]["totalTokens"] = 21_000_000
    assert wd.check_usage(NOW, dry=False) == []


def test_usage_is_skipped_without_ccusage(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, "CCUSAGE", str(tmp_path / "missing"))
    assert wd.check_usage(NOW, dry=False) == []
