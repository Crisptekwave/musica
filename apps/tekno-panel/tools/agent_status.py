#!/usr/bin/env python3
"""tekno-agent: tell the TEKNO CONTROL panel what an agent is doing right now.

Claude agents run in a cloud session, not as processes on the VM, so the panel can't see them. Each agent writes a
small status file <AGENTS_DIR>/<name>.json instead (atomic: tmp + rename). The panel only reads them.

  tekno-agent start <name> --model sonnet|opus|fable|qwen --card <slug> --task "<short>"
  tekno-agent step  <name> "<current step>"            (also bumps the heartbeat)
  tekno-agent done  <name> [--result ok|failed|stopped] ["<one line>"]

Names are [a-z0-9-]{1,40}; texts are capped. Nothing here runs a command or reads anything but its own file.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

DIR = Path(os.environ.get("TEKNO_AGENTS_DIR", "/mnt/agent-data/tekno_machine/agents"))
NAME_RE = re.compile(r"^[a-z0-9-]{1,40}$")
CARD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MODELS = ("sonnet", "opus", "fable", "qwen")
RESULTS = ("ok", "failed", "stopped")
STALE_S = 20 * 60        # working but silent this long: shown amber by the panel
HIDE_DONE_S = 24 * 3600  # finished agents leave the roster after this
HISTORY = 20


def clip(text, n: int) -> str:
    return " ".join(str(text or "").split())[:n]


def check_name(name: str) -> str:
    if not NAME_RE.match(str(name)):
        raise ValueError("name must match [a-z0-9-]{1,40}")
    return name


def path_for(name: str, base: Path | None = None) -> Path:
    return (base or DIR) / f"{check_name(name)}.json"


def read(name: str, base: Path | None = None) -> dict | None:
    try:
        d = json.loads(path_for(name, base).read_text())
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def write(rec: dict, base: Path | None = None) -> None:
    """Atomic write; the folder is group-writable (setgid), so keep the file group-writable too."""
    p = path_for(rec["name"], base)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rec, indent=1))
    os.chmod(tmp, 0o664)
    os.replace(tmp, p)


def start(name: str, model: str, card: str, task: str, base: Path | None = None, now: float | None = None) -> dict:
    if model not in MODELS:
        raise ValueError("model must be one of " + "|".join(MODELS))
    if card and not CARD_RE.match(card):
        raise ValueError("bad card slug")
    now = now or time.time()
    rec = {"name": check_name(name), "model": model, "card": card or None, "task": clip(task, 160), "step": "",
           "started": now, "updated": now, "state": "working", "result": None, "history": []}
    write(rec, base)
    return rec


def step(name: str, text: str, base: Path | None = None, now: float | None = None) -> dict:
    rec = read(name, base)
    if rec is None:
        raise ValueError(f"{name} has not started: run tekno-agent start first")
    now = now or time.time()
    rec["step"] = clip(text, 160)
    rec["updated"] = now
    if rec.get("state") != "working":  # a new step means it is working again
        rec["state"], rec["result"] = "working", None
    rec["history"] = (rec.get("history") or [])[-(HISTORY - 1):] + [{"at": now, "step": rec["step"]}]
    write(rec, base)
    return rec


def done(name: str, result: str = "ok", line: str = "", base: Path | None = None, now: float | None = None) -> dict:
    if result not in RESULTS:
        raise ValueError("result must be one of " + "|".join(RESULTS))
    rec = read(name, base)
    if rec is None:
        raise ValueError(f"{name} has not started: run tekno-agent start first")
    now = now or time.time()
    rec["state"] = {"ok": "done", "failed": "failed", "stopped": "stopped"}[result]
    rec["result"] = clip(line, 160) or result
    rec["updated"] = now
    if line:
        rec["history"] = (rec.get("history") or [])[-(HISTORY - 1):] + [{"at": now, "step": rec["result"]}]
    write(rec, base)
    return rec


def roster(base: Path | None = None, now: float | None = None) -> list[dict]:
    """All agents worth showing: finished ones vanish after 24 h; a working agent silent for 20 min is stale."""
    now = now or time.time()
    out = []
    try:
        files = sorted((base or DIR).glob("*.json"))
    except OSError:
        return []
    for f in files:
        if not NAME_RE.match(f.stem):
            continue
        rec = read(f.stem, base)
        if not rec or not isinstance(rec.get("updated"), (int, float)):
            continue
        age = now - rec["updated"]
        if rec.get("state") != "working" and age > HIDE_DONE_S:
            continue
        out.append({**rec, "age_s": max(0.0, age), "stale": rec.get("state") == "working" and age > STALE_S})
    return sorted(out, key=lambda r: (r.get("state") != "working", -r["updated"]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("start")
    a.add_argument("name")
    a.add_argument("--model", required=True, choices=MODELS)
    a.add_argument("--card", default="")
    a.add_argument("--task", default="")
    b = sub.add_parser("step")
    b.add_argument("name")
    b.add_argument("text")
    c = sub.add_parser("done")
    c.add_argument("name")
    c.add_argument("--result", default="ok", choices=RESULTS)
    c.add_argument("line", nargs="?", default="")
    sub.add_parser("list")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "start":
            start(args.name, args.model, args.card, args.task)
        elif args.cmd == "step":
            step(args.name, args.text)
        elif args.cmd == "done":
            done(args.name, args.result, args.line)
        else:
            for r in roster():
                print(f"{r['name']:<20} {r['model']:<7} {r['state']:<8} {'STALE ' if r['stale'] else ''}{r.get('card') or '-'}  {r.get('step') or r.get('task')}")
            return 0
    except (ValueError, OSError) as e:
        print(f"tekno-agent: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
