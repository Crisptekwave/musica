"""Read-only agent-mail client (MCP over streamable HTTP plus one GET endpoint).

Only two JSON-RPC methods are ever sent: ``initialize`` (+ the initialized
notification) and ``resources/read``. Tools such as send_message, purge or
hard_delete are never called; :data:`ALLOWED_METHODS` enforces that. The only
plain HTTP call is ``GET /mail/api/unified-inbox`` (:data:`ALLOWED_GET`), which
lists every thread with just the server bearer, so the track threads
(``card-<slug>``) show without other agents' tokens.

The panel's own agent (registered once as read-only, inbox closed with
``block_all``) lives in ``$TEKNO_STATE_DIR/agent_mail_agents.json`` as
``[{"name": "...", "token": "...", "project": "..."}]``. Its token is only used
when the unified inbox is not available, and never shown or logged.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

ALLOWED_METHODS = {"initialize", "notifications/initialized", "resources/read"}
ALLOWED_RESOURCES = ("resource://agents/", "resource://outbox/", "resource://inbox/",
                     "resource://thread/", "resource://tooling/projects")
ALLOWED_GET = ("/mail/api/unified-inbox",)
TOKEN_RE = re.compile(r"((?:agent|registration)_token=)[^&\s'\"]+", re.I)
TRACK_THREAD_RE = re.compile(r"^card-(?P<slug>[A-Za-z0-9][A-Za-z0-9._-]*)$")


def redact(text: str) -> str:
    """agent-mail echoes the full resource URI (token included) in its errors: never pass that on."""
    return TOKEN_RE.sub(r"\1***", str(text))


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
        self._inited = False
        self._id = 0
        self.timeout = 4.0
        u = urllib.parse.urlsplit(url)
        self.base = f"{u.scheme}://{u.netloc}"

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
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
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
        self._inited = True

    def read(self, uri: str):
        if not uri.startswith(ALLOWED_RESOURCES):
            raise PermissionError(f"resource not allowed: {redact(uri)}")
        # The server's /mcp/ endpoint is stateless (no session id), so one initialize is enough.
        if not self._inited:
            self.connect()
        try:
            res = self._post("resources/read", {"uri": uri})
        except Exception:
            self.connect()
            res = self._post("resources/read", {"uri": uri})
        contents = ((res or {}).get("result") or {}).get("contents") or []
        if not contents:
            err = (res or {}).get("error")
            raise RuntimeError(redact(err.get("message")) if isinstance(err, dict) else "empty resource")
        txt = contents[0].get("text") or "null"
        try:
            return json.loads(txt)
        except json.JSONDecodeError:
            return txt

    def get(self, path: str, params: dict | None = None):
        """GET one whitelisted read-only endpoint (bearer auth)."""
        if path not in ALLOWED_GET:
            raise PermissionError(f"agent-mail GET not allowed (read-only panel): {path}")
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        h = {"Accept": "application/json"}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        with urllib.request.urlopen(urllib.request.Request(url, headers=h, method="GET"), timeout=self.timeout) as r:
            return json.loads(r.read().decode() or "null")

    def unified(self, limit: int = 300) -> list[dict]:
        """Every message in every project, newest last. include_projects stays off (that path may write)."""
        data = self.get("/mail/api/unified-inbox", {"limit": max(1, min(1000, limit))})
        msgs = normalize_messages((data or {}).get("messages") if isinstance(data, dict) else data)
        return sorted(msgs, key=lambda m: m["ts"] or "")

    def projects(self) -> list[dict]:
        d = self.read("resource://tooling/projects")
        return [p for p in d if isinstance(p, dict)] if isinstance(d, list) else []

    def directory(self) -> list[dict]:
        d = self.read(f"resource://agents/{urllib.parse.quote(self.project)}")
        return [
            {"name": a.get("name"), "program": a.get("program"), "task": a.get("task_description"),
             "last_active": a.get("last_active_ts")}
            for a in (d or {}).get("agents", [])
        ] if isinstance(d, dict) else []

    def messages(self, limit: int = 80) -> list[dict]:
        """Fallback when the unified inbox is unavailable: the registered agents' own mailboxes."""
        seen: dict = {}
        for ag in self.agents:
            name, tok = ag.get("name"), ag.get("token")
            if not name or not tok:
                continue
            q = urllib.parse.urlencode({"project": ag.get("project") or self.project, "agent_token": tok,
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
        if isinstance(to, str):  # unified inbox: "A, B"
            to = [x.strip() for x in to.split(",") if x.strip()]
        out.append({
            "id": m.get("id") or m.get("message_id") or hash(json.dumps(m, sort_keys=True, default=str)),
            "thread": m.get("thread_id") or m.get("thread"),
            "from": sender,
            "to": [t.get("name", "?") if isinstance(t, dict) else str(t) for t in to],
            "subject": m.get("subject") or "",
            "body": (m.get("body_md") or m.get("body") or m.get("excerpt") or "")[:4000],
            "ts": _iso(m.get("created_ts") or m.get("created_at") or m.get("ts")),
            "importance": m.get("importance") or "normal",
            "project": m.get("project_slug") or m.get("project"),
        })
    return out


def _iso(ts):
    """MCP gives ISO+00:00, the unified inbox naive UTC 'YYYY-MM-DD HH:MM:SS.ffffff': make both ISO UTC."""
    if not isinstance(ts, str) or not ts:
        return ts
    try:
        d = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc).isoformat()


def parse_aliases(spec: str) -> dict:
    """'LavenderElk=Grok Bot,CopperHeron=Hermes' -> {handle: label} (agent-mail only allows adjective+noun names)."""
    out = {}
    for part in (spec or "").split(","):
        k, _, v = part.partition("=")
        if k.strip() and v.strip():
            out[k.strip()] = v.strip()
    return out


def label(chat: dict, aliases: dict) -> dict:
    """Add a human 'label' (Grok Bot, Hermes, Claude) next to each agent-mail handle."""
    for m in chat.get("messages") or []:
        m["label"] = aliases.get(m.get("from"), m.get("from"))
        m["to_labels"] = [aliases.get(t, t) for t in m.get("to") or []]
    for a in chat.get("agents") or []:
        if isinstance(a, dict):
            a["label"] = aliases.get(a.get("name"), a.get("name"))
    return chat


def threads(messages: list[dict], slugs: list[str]) -> list[dict]:
    """Thread list for the chat screen: one 'card-<slug>' thread per track (even when still empty), then others."""
    by: dict[str, dict] = {}
    for s in slugs:
        by[f"card-{s}"] = {"id": f"card-{s}", "slug": s, "count": 0, "last": None, "subject": None}
    for m in messages:
        tid = str(m.get("thread") or "")
        if not tid:
            continue
        mt = TRACK_THREAD_RE.match(tid)
        t = by.setdefault(tid, {"id": tid, "slug": mt.group("slug") if mt else None, "count": 0, "last": None,
                                "subject": None})
        t["count"] += 1
        if not t["last"] or (m["ts"] or "") >= t["last"]:
            t["last"], t["subject"] = m["ts"], m["subject"] or t["subject"]
    return sorted(by.values(), key=lambda t: (t["slug"] is None, -(t["count"] > 0), t["id"]))


def load_agents(state_dir: Path) -> list[dict]:
    f = state_dir / "agent_mail_agents.json"
    try:
        data = json.loads(f.read_text())
        return [a for a in data if isinstance(a, dict)]
    except (OSError, json.JSONDecodeError, TypeError):
        return []


def identity(agents: list[dict]) -> dict | None:
    """Who the panel is on agent-mail (name/project only, never the token)."""
    a = next((x for x in agents if x.get("name") and x.get("token")), None)
    return {"name": a["name"], "project": a.get("project"), "read_only": True} if a else None
