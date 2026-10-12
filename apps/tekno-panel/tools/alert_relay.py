#!/usr/bin/env python3
"""alert-relay: post new TEKNO CONTROL alerts and failed actions to the team chat (thread panel-errors).

The panel stays read-only: this is a separate one-shot job (run by a user timer). It reads the panel's own
history DB read-only, and posts as the dashboard identity Tekno-Panel to Hermes, Grok Bot and Claude. Each problem
is posted once and again only if it is still there after REPOST_S. The first run only remembers what is already
open, so it never floods the chat with old alerts.

  alert_relay.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import team_mail  # noqa: E402

DB = Path(os.environ.get("TEKNO_DATA_DIR", "/mnt/agent-data/apps/tekno-panel-data")) / "history.db"
PANEL_AGENTS = Path(os.environ.get("TEKNO_STATE_DIR") or Path.home() / ".config" / "tekno-panel") / "agent_mail_agents.json"
STATE = team_mail.DIR / "alert_relay_state.json"
THREAD = "panel-errors"
LOOKBACK_S = 3600
REPOST_S = 12 * 3600
ALERT_TS = re.compile(r":\d+$")


def problems(db: Path, now: float) -> list[dict]:
    """warn/crit alerts and failed control actions from the last hour, one per key (newest first)."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    rows = con.execute("select id, ts, kind, card, level, text from events where ts > ? and "
                       "((kind = 'alert' and level in ('warn', 'crit')) or (kind = 'action' and level = 'warn')) "
                       "order by ts desc", (now - LOOKBACK_S,)).fetchall()
    con.close()
    out, seen = [], set()
    for id_, ts, kind, card, level, text in rows:
        key = ALERT_TS.sub("", id_) if kind == "alert" else id_  # an alert's id ends in its first-seen time
        if key not in seen:
            seen.add(key)
            out.append({"key": key, "ts": ts, "kind": kind, "card": card, "level": level, "text": text})
    return out


def due(items: list[dict], state: dict, now: float) -> list[dict]:
    return [p for p in items if p["key"] not in state or now - state[p["key"]] >= REPOST_S]


def message(items: list[dict]) -> tuple[str, str]:
    crit = sum(p["level"] == "crit" for p in items)
    subject = f"Panel: {len(items)} new problem{'s' if len(items) != 1 else ''}" + (f" ({crit} critical)" if crit else "")
    lines = [f"- [{p['level']}] {p['text']}" + (f" (card {p['card']})" if p.get("card") and p["card"] not in p["text"] else "")
             for p in items]
    body = ("TEKNO CONTROL raised:\n" + "\n".join(lines) +
            "\n\nOwner of the lane: reply here with what you do about it. Claude follows up on panel and pipeline bugs.")
    return subject, body


def panel_token() -> tuple[str, str]:
    for a in json.loads(PANEL_AGENTS.read_text()):
        if a.get("token"):
            return a["name"], a["token"]
    sys.exit("no Tekno-Panel token in agent_mail_agents.json")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    now = time.time()
    try:
        state, first = json.loads(STATE.read_text()), False
    except (OSError, ValueError):  # missing, or cut short by a full disk: start over quietly, like a first run
        state, first = {}, True
    items = due(problems(DB, now), state, now)
    if items and not first:
        subject, body = message(items)
        if a.dry_run:
            print(subject + "\n" + body)
            return 0
        me, tok = panel_token()
        to = [n for role, n in team_mail.team().items() if n != me]
        team_mail.call("send_message", {"project_key": team_mail.PROJECT, "sender_name": me, "sender_token": tok,
                                        "to": to, "subject": subject, "body_md": body, "thread_id": THREAD,
                                        "importance": "high" if any(p["level"] == "crit" for p in items) else "normal"})
        print(f"posted {len(items)} to {', '.join(to)}")
    elif first:
        print(f"first run: remembering {len(items)} open problem(s) without posting")
    if a.dry_run:
        return 0
    for p in items:
        state[p["key"]] = now
    state = {k: v for k, v in state.items() if now - v < 7 * 86400}
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1) + "\n")
    tmp.replace(STATE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
