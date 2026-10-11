"""The agent roster: status files written by ``tekno-agent`` (tools/agent_status.py), one JSON per agent.

Claude agents run in a cloud session, so the panel can't see them as processes. It only reads the files:
a working agent that hasn't reported for STALE_S is stale (amber, not failed); finished ones vanish after a day.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9-]{1,40}$")
MODELS = ("sonnet", "opus", "fable", "qwen")
STATES = ("working", "done", "failed", "stopped")
STALE_S = 20 * 60
HIDE_DONE_S = 24 * 3600


def _s(x, n: int = 160):
    return " ".join(str(x).split())[:n] if isinstance(x, (str, int, float)) and not isinstance(x, bool) else None


def roster(root: Path, now: float | None = None) -> list[dict]:
    now = now or time.time()
    try:
        files = sorted(root.glob("*.json"))
    except OSError:
        return []
    out = []
    for f in files[:60]:
        if not NAME_RE.match(f.stem):
            continue
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        upd = d.get("updated") if isinstance(d, dict) else None
        if not isinstance(upd, (int, float)) or isinstance(upd, bool):
            continue
        state = d.get("state") if d.get("state") in STATES else "working"
        age = max(0.0, now - upd)
        if state != "working" and age > HIDE_DONE_S:
            continue
        hist = [{"at": h.get("at"), "step": _s(h.get("step"))} for h in d.get("history") or []
                if isinstance(h, dict) and isinstance(h.get("at"), (int, float))][-8:]
        out.append({"name": f.stem, "model": d.get("model") if d.get("model") in MODELS else None,
                    "card": _s(d.get("card"), 64), "task": _s(d.get("task")), "step": _s(d.get("step")),
                    "state": state, "result": _s(d.get("result")), "started": d.get("started"), "updated": upd,
                    "age_s": round(age, 1), "stale": state == "working" and age > STALE_S, "history": hist})
    return sorted(out, key=lambda r: (r["state"] != "working", -r["updated"]))
