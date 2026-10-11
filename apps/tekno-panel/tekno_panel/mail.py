"""Read-only agent-mail (MCP over streamable HTTP) client.

Only two JSON-RPC methods are ever sent: ``initialize`` (+ the initialized
notification) and ``resources/read``. Tools such as send_message, purge or
hard_delete are never called; :data:`ALLOWED_METHODS` enforces that.

Reading mailboxes needs an agent token per agent. Put them in
``$TEKNO_STATE_DIR/agent_mail_agents.json`` as ``[{"name": "...", "token": "..."}]``.
Without tokens the chat screen shows the agent directory only.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

ALLOWED_METHODS = {"initialize", "notifications/initialized", "resources/read"}
ALLOWED_RESOURCES = ("resource://agents/", "resource://outbox/", "resource://inbox/",
                     "resource://thread/", "resource://tooling/projects")


def bearer_from_hermes(config_path: Path) -> str | None:
    try:
        txt = config_path.read_text()
    except OSError:
        return None
    m = re.search(r"agent-mail:.*?Authorization:\s*Bearer\s+(\S+)", txt, re.S)
    return m.group(1).strip("'\"") if m else None


class AgentMail:
    def __init__(self, url: str, project: str, token: str | None, agents: list[dict] | None = None):
        self.url, self.project, self.token = url, project, token
        self.agents = agents or []
        self.sid: str | None = None
        self._id = 0

    def _post(self, method: str, params: dict | None = None, notify: bool = False):
        if method not in ALLOWED_METHODS:
            raise PermissionError(f"agent-mail method not allowed (read-only panel): {method}")
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self._id += 1
            body["id"] = self._id
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        if self.sid:
            h["mcp-session-id"] = self.sid
        req = urllib.request.Request(self.url, json.dumps(body).encode(), h)
        with urllib.request.urlopen(req, timeout=8) as r:
            self.sid = r.headers.get("mcp-session-id") or self.sid
            text = r.read().decode()
        if not text.strip():
            return None
        if text.lstrip().startswith("{"):
            return json.loads(text)
        for line in text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:])
        return None

    def connect(self):
        self.sid = None
        self._post("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                  "clientInfo": {"name": "tekno-control", "version": "1.0"}})
        self._post("notifications/initialized", notify=True)

    def read(self, uri: str):
        if not uri.startswith(ALLOWED_RESOURCES):
            raise PermissionError(f"resource not allowed: {uri}")
        if not self.sid:
            self.connect()
        try:
            res = self._post("resources/read", {"uri": uri})
        except Exception:
            self.connect()
            res = self._post("resources/read", {"uri": uri})
        contents = ((res or {}).get("result") or {}).get("contents") or []
        if not contents:
            err = (res or {}).get("error")
            raise RuntimeError(err.get("message") if isinstance(err, dict) else "empty resource")
        txt = contents[0].get("text") or "null"
        try:
            return json.loads(txt)
        except json.JSONDecodeError:
            return txt

    def directory(self) -> list[dict]:
        d = self.read(f"resource://agents/{urllib.parse.quote(self.project)}")
        return [
            {"name": a.get("name"), "program": a.get("program"), "task": a.get("task_description"),
             "last_active": a.get("last_active_ts")}
            for a in (d or {}).get("agents", [])
        ] if isinstance(d, dict) else []

    def messages(self, limit: int = 80) -> list[dict]:
        seen: dict = {}
        for ag in self.agents:
            name, tok = ag.get("name"), ag.get("token")
            if not name or not tok:
                continue
            q = urllib.parse.urlencode({"project": self.project, "agent_token": tok,
                                        "include_bodies": "true", "limit": limit})
            for box in ("outbox", "inbox"):
                try:
                    data = self.read(f"resource://{box}/{urllib.parse.quote(name)}?{q}")
                except Exception:
                    continue
                for m in normalize_messages(data):
                    seen[m["id"]] = m
        return sorted(seen.values(), key=lambda m: m["ts"] or "")[-limit:]


def normalize_messages(data) -> list[dict]:
    if isinstance(data, dict):
        data = data.get("messages") or data.get("items") or data.get("inbox") or data.get("outbox") or []
    out = []
    for m in data if isinstance(data, list) else []:
        if not isinstance(m, dict):
            continue
        sender = m.get("from") or m.get("sender") or m.get("sender_name") or "?"
        if isinstance(sender, dict):
            sender = sender.get("name", "?")
        to = m.get("to") or m.get("recipients") or []
        if isinstance(to, str):
            to = [to]
        out.append({
            "id": m.get("id") or m.get("message_id") or hash(json.dumps(m, sort_keys=True, default=str)),
            "thread": m.get("thread_id") or m.get("thread"),
            "from": sender,
            "to": [t.get("name", "?") if isinstance(t, dict) else str(t) for t in to],
            "subject": m.get("subject") or "",
            "body": (m.get("body_md") or m.get("body") or m.get("excerpt") or "")[:4000],
            "ts": m.get("created_ts") or m.get("created_at") or m.get("ts"),
            "importance": m.get("importance") or "normal",
        })
    return out


def load_agents(state_dir: Path) -> list[dict]:
    f = state_dir / "agent_mail_agents.json"
    try:
        data = json.loads(f.read_text())
        return [a for a in data if isinstance(a, dict)]
    except (OSError, json.JSONDecodeError):
        return []
