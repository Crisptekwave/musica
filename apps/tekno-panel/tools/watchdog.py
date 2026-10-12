#!/usr/bin/env python3
"""watchdog: keeps an eye on the agents and the machine every 5 minutes (user timer), mostly without any model.

Checks (plain code, no tokens):
  jobs      VM Claude Code jobs (run_job.sh <job>): runtime past JOB_WARN_S is a warning, past JOB_KILL_S the job's own
            process tree is stopped (never a broad kill).
  tokens    Claude Code transcripts written in the last hour: tokens per hour per session (cache reads excluded) past
            BURN_TOKENS_H, and the same tool call repeated LOOP_REPEAT times in a row (a loop).
  agents    roster agents still "working" but silent for 20+ min.
  services  failed user units (the tekno-* allowlist is restarted once per 12 h), local model, panel, gateway.
  disk      root and data disk free space.
  logs      error lines from the tekno user units' journal, failed or turn-limited VM job logs, night-shift errors.

New problems go to agent-mail thread "watchdog" as qwen, once, and again only if still open after REPOST_S; error logs
get a plain-words summary from the local Qwen / gateway (text only, no tools). Everything lands in
<WD>/state.json (open issues, "needs_claude" for what code can't fix), which the panel and Claude's check-ins read.

  watchdog.py [--dry-run]
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_status  # noqa: E402
import llm  # noqa: E402
import team_mail  # noqa: E402

MACHINE = Path(os.environ.get("TEKNO_MACHINE_ROOT", "/mnt/agent-data/tekno_machine"))
WD = Path(os.environ.get("TEKNO_WATCHDOG_DIR", str(MACHINE / "watchdog")))
CC_JOBS = Path(os.environ.get("TEKNO_CC_JOBS_DIR", "/mnt/agent-data/apps/tekno-panel-next/cc_jobs"))
CC_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", "/mnt/agent-data/apps/claude-home"))
NIGHT = MACHINE / "night"
PANEL = os.environ.get("TEKNO_PANEL_HEALTH", "http://127.0.0.1:3020/healthz")
DATA_DISK = os.environ.get("TEKNO_DATA_DISK", "/mnt/agent-data")
PROC = Path("/proc")

JOB_WARN_S = int(os.environ.get("TEKNO_JOB_WARN_S", 2 * 3600))
JOB_KILL_S = int(os.environ.get("TEKNO_JOB_KILL_S", 4 * 3600))
BURN_TOKENS_H = int(os.environ.get("TEKNO_BURN_TOKENS_H", 3_000_000))
LOOP_REPEAT = 8  # a job waiting on purpose polls with sleep; those calls are left out
REPOST_S = 6 * 3600
RESTART_EVERY_S = 12 * 3600
RESTARTABLE = ("tekno-panel.service", "tekno-control-helper.service", "tekno-reaper-snap.service",
               "tekno-alert-relay.service")
ORDER = os.environ.get("TEKNO_NIGHT_ORDER", "local,gateway").split(",")


def issue(key: str, level: str, text: str, claude: bool = False, log: str = "") -> dict:
    return {"key": key, "level": level, "text": " ".join(text.split())[:300], "claude": claude, "log": log[-3000:]}


# ---------------------------------------------------------------- jobs (process tree from /proc)
def procs() -> dict[int, dict]:
    out = {}
    try:
        boot = time.time() - float((PROC / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        boot = 0.0
    tick = os.sysconf("SC_CLK_TCK")
    for d in PROC.glob("[0-9]*"):
        try:
            cmd = (d / "cmdline").read_bytes().split(b"\0")
            stat = (d / "stat").read_text().rsplit(")", 1)[1].split()
        except OSError:
            continue
        out[int(d.name)] = {"cmd": [c.decode(errors="replace") for c in cmd if c], "ppid": int(stat[1]),
                            "start": boot + int(stat[19]) / tick}
    return out


def jobs(ps: dict[int, dict]) -> list[dict]:
    """Running run_job.sh <job> processes: [{job, pid, start}]."""
    out = []
    for pid, p in ps.items():
        c = p["cmd"]
        if len(c) >= 3 and c[0].endswith("bash") and c[1].endswith("run_job.sh"):
            out.append({"job": c[2], "pid": pid, "start": p["start"]})
    return out


def tree(ps: dict[int, dict], root: int) -> list[int]:
    kids, todo = [], [root]
    while todo:
        cur = todo.pop()
        kids.append(cur)
        todo += [pid for pid, p in ps.items() if p["ppid"] == cur]
    return kids


def stop_tree(ps: dict[int, dict], root: int) -> None:
    for pid in reversed(tree(ps, root)):  # children first
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def check_jobs(ps, now, dry) -> tuple[list[dict], list[str]]:
    found, actions = [], []
    for j in jobs(ps):
        age = now - j["start"]
        if age > JOB_KILL_S:
            if not dry:
                stop_tree(ps, j["pid"])
            actions.append(f"stopped VM job {j['job']} after {age / 3600:.1f} h (budget {JOB_KILL_S // 3600} h)")
            found.append(issue(f"job-killed:{j['job']}", "crit", f"VM job {j['job']} ran {age / 3600:.1f} h and was "
                               f"stopped; its work is unfinished", claude=True))
        elif age > JOB_WARN_S:
            found.append(issue(f"job-long:{j['job']}", "warn", f"VM job {j['job']} has run {age / 3600:.1f} h "
                               f"(stopped at {JOB_KILL_S // 3600} h)"))
    return found, actions


# ---------------------------------------------------------------- tokens and loops (Claude Code transcripts)
def transcript_stats(path: Path, now: float) -> dict:
    tokens, calls = 0, []
    try:
        lines = path.read_text(errors="replace").splitlines()[-3000:]
    except OSError:
        return {"tokens_h": 0, "repeat": 0, "last": ""}
    for ln in lines:
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        msg = e.get("message") if isinstance(e, dict) else None
        if not isinstance(msg, dict) or e.get("type") != "assistant":
            continue
        ts = e.get("timestamp", "")
        try:
            t = calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))  # transcripts stamp UTC
        except ValueError:
            t = now
        u = msg.get("usage") or {}
        if now - t < 3600:
            tokens += sum(int(u.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens"))
        for c in msg.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "tool_use" and "sleep " not in json.dumps(c.get("input")):
                calls.append(c.get("name", "") + json.dumps(c.get("input"), sort_keys=True)[:400])
    repeat = 1
    for a, b in zip(reversed(calls), list(reversed(calls))[1:]):
        if a != b:
            break
        repeat += 1
    return {"tokens_h": tokens, "repeat": repeat if calls else 0, "last": calls[-1][:120] if calls else ""}


def check_tokens(now) -> list[dict]:
    found = []
    for f in (CC_HOME / "projects").glob("*/*.jsonl"):
        try:
            if now - f.stat().st_mtime > 900:  # only sessions active in the last 15 min
                continue
        except OSError:
            continue
        s = transcript_stats(f, now)
        name = f"{f.parent.name[-30:]}/{f.stem[:8]}"
        if s["tokens_h"] > BURN_TOKENS_H:
            found.append(issue(f"burn:{f.stem}", "warn", f"Claude Code session {name} used {s['tokens_h']:,} tokens in "
                               f"the last hour (limit {BURN_TOKENS_H:,})"))
        if s["repeat"] >= LOOP_REPEAT:
            found.append(issue(f"loop:{f.stem}", "crit", f"Claude Code session {name} repeated the same tool call "
                               f"{s['repeat']} times in a row: {s['last']}", claude=True))
    return found


# ---------------------------------------------------------------- agents, services, health, disk
def check_agents(now) -> list[dict]:
    return [issue(f"stale:{a['name']}", "warn", f"agent {a['name']} is working but silent for "
                  f"{a['age_s'] / 60:.0f} min (last step: {a.get('step') or '-'})")
            for a in agent_status.roster(now=now) if a.get("stale")]


def systemctl(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30)


def check_services(state, now, dry) -> tuple[list[dict], list[str]]:
    found, actions = [], []
    r = systemctl("--failed", "--plain", "--no-legend")
    if r.returncode:
        return [issue("systemctl", "warn", f"cannot read user services: {r.stderr.strip()[:120]}")], []
    restarted = state.setdefault("restarted", {})
    for unit in [ln.split()[0] for ln in r.stdout.splitlines() if ln.strip()]:
        if unit in RESTARTABLE and now - restarted.get(unit, 0) > RESTART_EVERY_S:
            if not dry:
                systemctl("reset-failed", unit)
                systemctl("restart", unit)
            restarted[unit] = now
            actions.append(f"restarted {unit} (failed)")
            found.append(issue(f"failed:{unit}", "warn", f"{unit} had failed and was restarted once"))
        else:
            found.append(issue(f"failed:{unit}", "crit", f"{unit} has failed" +
                               (" again after a restart" if unit in RESTARTABLE else ""), claude=True))
    return found, actions


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status == 200
    except (OSError, ValueError):
        return False


def check_health() -> list[dict]:
    found = []
    if not llm.up(["local"]):
        found.append(issue("down:local-model", "crit", "the local model (vLLM) does not answer", claude=True))
    if not http_ok(PANEL):
        found.append(issue("down:panel", "crit", "the panel does not answer on its health check", claude=True))
    if llm.GATEWAY[0] and not llm.up(["gateway"]):
        found.append(issue("down:gateway", "warn", "the FreeLLMAPI gateway does not answer (the fallback uses the "
                           "local model meanwhile)"))
    for label, path, warn, crit in (("root disk", "/", 1.0, 0.3), ("data disk", DATA_DISK, 10.0, 3.0)):
        try:
            free = shutil.disk_usage(path).free / 2**30
        except OSError:
            continue
        if free < warn:
            found.append(issue(f"disk:{label}", "crit" if free < crit else "warn",
                               f"{label} has {free:.1f} GB free", claude=free < crit))
    return found


# ---------------------------------------------------------------- logs
def check_logs(state, now) -> list[dict]:
    found = []
    since = int(state.get("last_run") or now - 600)
    r = subprocess.run(["journalctl", "--user", "-p", "err", "--since", f"@{since}", "-o", "short", "--no-pager",
                        "-u", "tekno-*"], capture_output=True, text=True, timeout=30)
    lines = [ln for ln in r.stdout.splitlines() if ln.strip() and not ln.startswith("--")]
    if lines:
        units = sorted({m.group(1) for ln in lines if (m := re.search(r"\s(tekno-[\w.-]+?)(?:\[\d+\])?:", ln))})
        found.append(issue(f"errlog:{','.join(units) or 'tekno'}:{int(now // REPOST_S)}", "warn",
                           f"{len(lines)} error line(s) in the journal of {', '.join(units) or 'tekno units'}",
                           log="\n".join(lines[-40:])))
    for log in CC_JOBS.glob("*.log"):
        try:
            if log.stat().st_mtime < since:
                continue
            text = log.read_text(errors="replace")
        except OSError:
            continue
        if "Reached max turns" in text or re.search(r"^Error:", text, re.M):
            found.append(issue(f"jobfail:{log.stem}:{int(log.stat().st_mtime)}", "crit",
                               f"VM job {log.stem} ended with an error: {text.strip().splitlines()[-1][:160]}",
                               claude=True, log=text[-2000:]))
    t = _json(NIGHT / "tag_last.json")
    if t and t.get("model_errors"):
        found.append(issue(f"night-tag:{t.get('at')}", "warn", f"night tagging had {t['model_errors']} model error(s)"))
    d = _json(NIGHT / "digest_last.json")
    if d and d.get("sent") is False:
        found.append(issue(f"night-digest:{d.get('at')}", "warn", "the morning report could not be posted"))
    return found


def _json(p: Path):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- report
def summarize(i: dict) -> str:
    """Plain-words reading of an error log by the local Qwen / gateway: text in, text out, no tools."""
    if not i.get("log"):
        return ""
    body = {"temperature": 0.1, "max_tokens": 200, "messages": [
        {"role": "system", "content": "You read error logs for a music production machine. In at most 3 short lines say "
                                      "what failed and the most likely cause. Only what the log shows; no guesses."},
        {"role": "user", "content": i["log"]}]}
    try:
        return (llm.complete(body, ORDER, timeout=120)[0].get("content") or "").strip()
    except Exception:  # noqa: BLE001 - the raw issue still goes out
        return ""


def mail(body: str) -> None:
    try:
        team_mail.main(["send", "--as", "qwen", "--thread", "watchdog", body])
    except (SystemExit, Exception):  # noqa: BLE001 - mail is best effort; state.json is the record
        pass


def tick(dry: bool = False, now: float | None = None) -> dict:
    now = now or time.time()
    state = _json(WD / "state.json") or {}
    ps = procs()
    found, actions = [], []
    for fn in (lambda: check_jobs(ps, now, dry), lambda: (check_tokens(now), []), lambda: (check_agents(now), []),
               lambda: check_services(state, now, dry), lambda: (check_health(), []), lambda: (check_logs(state, now), [])):
        try:
            f, a = fn()
        except Exception as e:  # noqa: BLE001 - one broken check never stops the others
            f, a = [issue(f"watchdog:{type(e).__name__}", "warn", f"a watchdog check failed: {e}")], []
        found += f
        actions += a
    seen = state.get("seen", {})
    fresh = [i for i in found if now - seen.get(i["key"], 0) > REPOST_S]
    if fresh and not dry:
        lines = []
        for i in fresh:
            s = summarize(i)
            lines.append(f"[{i['level']}] {i['text']}" + (f"\n    {s}" if s else "") + ("  → needs Claude" if i["claude"] else ""))
        mail("Watchdog\n" + "\n".join(lines) + ("\nActions: " + "; ".join(actions) if actions else ""))
    fresh_keys = {i["key"] for i in fresh}
    state.update({"last_run": now,
                  "seen": {i["key"]: now if i["key"] in fresh_keys else seen[i["key"]] for i in found},  # closed issues drop out
                  "open": [{k: v for k, v in i.items() if k != "log"} for i in found],
                  "needs_claude": [i["text"] for i in found if i["claude"]],
                  "actions": (state.get("actions", []) + [f"{time.strftime('%F %H:%M', time.gmtime(now))} {a}" for a in actions])[-30:]})
    if not dry:
        WD.mkdir(parents=True, exist_ok=True)
        tmp = WD / "state.tmp"
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(WD / "state.json")
        try:
            rec = agent_status.read("watchdog") or agent_status.start("watchdog", "qwen", "", "keeps an eye on agents and machine")
            crit = sum(i["level"] == "crit" for i in found)
            agent_status.done("watchdog", "failed" if crit else "ok",
                              f"{len(found)} open issue(s), {crit} critical" if found else "all clear")
        except (ValueError, OSError):
            pass
    return state


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    s = tick(a.dry_run)
    print(json.dumps({"open": s["open"], "needs_claude": s["needs_claude"]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
