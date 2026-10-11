#!/usr/bin/env python3
"""team-mail: the shared agent-mail CLI for the tekno team (Hermes, Grok Bot, Claude).

Every agent posts as its own registered identity in project /hermes/music. Identities live in
~/.config/agent-mail/identities.json ({role: {name, token_file}}); tokens stay in their 600 files and are never
printed. Grok Bot (LavenderElk) runs off the VM and talks to the same server with its own token.

  team-mail who
  team-mail send  --as hermes --thread card-e2e-001 "mix done: -13.1 LUFS, knock +3.4 dB"
  team-mail send  --as claude --to grok --subject "question" "body"
  team-mail inbox --as hermes [--unread] [--limit 10] [--bodies]
  team-mail reply --as hermes <message_id> "body"
  team-mail ack   --as hermes <message_id>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

URL = os.environ.get("AGENT_MAIL_URL", "http://100.84.117.126:8765/mcp/")
PROJECT = "/hermes/music"
HERE = Path(__file__).resolve().parent


def _dir() -> Path:
    """Hermes lane workers run with a profile HOME, so find the identities next to this script first."""
    for d in (os.environ.get("AGENT_MAIL_DIR"), HERE, Path.home() / ".config" / "agent-mail"):
        if d and (Path(d) / "identities.json").exists():
            return Path(d)
    return Path(os.environ.get("AGENT_MAIL_DIR") or HERE)


DIR = _dir()
GROK = "LavenderElk"
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,}")


def bearer() -> str:
    env = os.environ.get("AGENT_MAIL_BEARER")
    if env:
        return env
    # installed as <home>/.config/agent-mail/team_mail.py: the owner's Hermes config is <home>/.hermes/config.yaml
    cands = [DIR.parents[1] / ".hermes" / "config.yaml", Path.home() / ".hermes" / "config.yaml"]
    cfg = next((c for c in cands if c.exists()), None)
    if not cfg:
        sys.exit("no ~/.hermes/config.yaml with the agent-mail bearer found")
    text = cfg.read_text()
    m = re.search(r"8765/mcp/?\s*\n\s*headers:\s*\n\s*Authorization:\s*Bearer\s+(\S+)", text)
    if not m:
        sys.exit("no agent-mail bearer found in ~/.hermes/config.yaml")
    return m.group(1).strip("'\"")


def _ids() -> dict:
    try:
        return json.loads((DIR / "identities.json").read_text())
    except (OSError, ValueError) as e:
        sys.exit(f"team-mail is not set up here ({e}). Do not register agents yourself: ask Claude in agent-mail.")


def team() -> dict:
    ids = _ids()
    out = {role: v["name"] for role, v in ids.items() if isinstance(v, dict) and v.get("name")}
    out.setdefault("grok", GROK)
    return out


def token(role: str) -> str:
    ids = _ids()
    if role not in ids or not ids[role].get("token_file"):
        sys.exit(f"no token on this machine for '{role}' (known: {', '.join(k for k in ids if ids[k].get('token_file'))})")
    return Path(ids[role]["token_file"]).read_text().strip()


def call(tool: str, args: dict):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}}
    req = urllib.request.Request(URL, json.dumps(body).encode(), {
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
        "Authorization": "Bearer " + bearer()})
    with urllib.request.urlopen(req, timeout=30) as r:
        text = r.read().decode()
    if not text.lstrip().startswith("{"):
        text = next(ln[5:] for ln in text.splitlines() if ln.startswith("data:"))
    res = json.loads(text)
    err = res.get("error", {}).get("message") if "error" in res else None
    r = res.get("result") or {}
    if r.get("isError"):
        err = " ".join(c.get("text", "") for c in r.get("content") or [])
    if err:
        sys.exit(f"{tool} failed: {TOKEN_RE.sub('<redacted>', err)[:500]}")
    sc = r.get("structuredContent")
    if isinstance(sc, dict) and set(sc) == {"result"}:
        sc = sc["result"]
    if sc is not None:
        return sc
    try:
        return json.loads(r["content"][0]["text"])
    except (KeyError, IndexError, ValueError):
        return r.get("content")


def names(spec: list[str], me: str, roster: dict) -> list[str]:
    if not spec or spec == ["all"]:
        return [n for n in roster.values() if n != me]
    return [roster.get(s, s) for s in spec]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="team-mail", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("who")
    s = sub.add_parser("send")
    s.add_argument("--as", dest="role", required=True)
    s.add_argument("--to", nargs="*", default=["all"], help="roles or agent names (default: the rest of the team)")
    s.add_argument("--thread")
    s.add_argument("--subject")
    s.add_argument("--important", action="store_true")
    s.add_argument("body")
    i = sub.add_parser("inbox")
    i.add_argument("--as", dest="role", required=True)
    i.add_argument("--unread", action="store_true")
    i.add_argument("--bodies", action="store_true")
    i.add_argument("--limit", type=int, default=10)
    for c in ("reply", "ack"):
        p = sub.add_parser(c)
        p.add_argument("--as", dest="role", required=True)
        p.add_argument("message_id", type=int)
        if c == "reply":
            p.add_argument("body")
    a = ap.parse_args(argv)
    roster = team()
    if a.cmd == "who":
        for role, n in roster.items():
            print(f"{role:8} {n}")
        return 0
    me = roster.get(a.role) or sys.exit(f"unknown role '{a.role}'")
    if a.cmd == "send":
        to = names(a.to, me, roster)
        subject = a.subject or (a.thread or a.body.splitlines()[0])[:80]
        args = {"project_key": PROJECT, "sender_name": me, "sender_token": token(a.role), "to": to,
                "subject": subject, "body_md": a.body, "importance": "high" if a.important else "normal"}
        if a.thread:
            args["thread_id"] = a.thread
        call("send_message", args)
        print(f"sent as {me} to {', '.join(to)}" + (f" in {a.thread}" if a.thread else ""))
    elif a.cmd == "inbox":
        res = call("fetch_inbox", {"project_key": PROJECT, "agent_name": me, "registration_token": token(a.role),
                                   "limit": a.limit, "unread_only": a.unread, "include_bodies": a.bodies})
        msgs = res if isinstance(res, list) else (res or {}).get("messages", [])
        if not msgs:
            print("inbox empty")
        for m in msgs:
            print(f"#{m.get('id')} {str(m.get('created_ts', ''))[:16]} {m.get('from')} [{m.get('thread_id') or '-'}] {m.get('subject')}")
            if a.bodies and m.get("body_md"):
                print("   " + m["body_md"].replace("\n", "\n   "))
    elif a.cmd == "reply":
        call("reply_message", {"project_key": PROJECT, "message_id": a.message_id, "sender_name": me,
                               "sender_token": token(a.role), "body_md": a.body})
        print(f"replied to #{a.message_id} as {me}")
    elif a.cmd == "ack":
        call("acknowledge_message", {"project_key": PROJECT, "agent_name": me, "message_id": a.message_id,
                                     "registration_token": token(a.role)})
        print(f"acknowledged #{a.message_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
