"""Read-only access to the Hermes kanban SQLite DB.

The DB is always opened with ``mode=ro`` so the panel can never write to it,
even by accident. The only write the panel performs (Review comments) goes
through the ``hermes kanban comment`` CLI, see :mod:`tekno_panel.review`.
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import LANE_ALIASES, LANES

TITLE_RE = re.compile(r"^\s*\[(?P<slug>[^\]]+)\]\s*(?P<lane>[A-Za-z][A-Za-z0-9 _-]*?)\s*(?::\s*(?P<rest>.*))?$")
# Routing markers in Score/QC notes: "-> Mix", "-> would route: Filter (...)", "route to Mix: ...", "send back to Edit - ..."
MARK_RE = re.compile(
    r"->|→|\b(?:would\s+route(?:\s+to)?|route[sd]?(?:\s+back)?\s+to|sen[dt]\s+back\s+to|go(?:es)?\s+back\s+to|back\s+to)\b\s*:?",
    re.I)
FAIL_HINT_RE = re.compile(r"\b(FAIL|NO-GO|BLOCKED|request-changes|changes requested|reject)", re.I)

ACTIVE = {"running"}
WAITING = {"todo", "ready", "scheduled", "triage"}


def normalize_lane(token: str | None) -> str | None:
    if not token:
        return None
    t = token.strip().upper().replace("_", "-").replace(" ", "-")
    if t in LANE_ALIASES:
        return LANE_ALIASES[t]
    for lane in LANES:
        if lane.upper().replace(" ", "-") == t:
            return lane
    return None


def parse_title(title: str) -> tuple[str | None, str | None, str]:
    m = TITLE_RE.match(title or "")
    if not m:
        return None, None, title or ""
    return m.group("slug").strip(), normalize_lane(m.group("lane")), (m.group("rest") or "").strip()


def _mask_parens(text: str) -> tuple[str, list[str]]:
    """Replace top-level (...) groups with \x00<n>\x00 so commas/slashes inside them don't split items."""
    out, groups, depth, buf = [], [], 0, []
    for ch in text:
        if ch == "(":
            if depth == 0:
                buf = []
            else:
                buf.append(ch)
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
            if depth == 0:
                out.append(f"\x00{len(groups)}\x00")
                groups.append("".join(buf))
            else:
                buf.append(ch)
        elif depth:
            buf.append(ch)
        else:
            out.append(ch)
    if depth:  # unbalanced: keep the rest as plain text
        out.append("(" + "".join(buf))
    return "".join(out), groups


def _unmask(text: str, groups: list[str]) -> str:
    return re.sub(r"\x00(\d+)\x00", lambda m: f"({groups[int(m.group(1))]})", text)


def _clause_before(text: str) -> str:
    """Last sentence-ish fragment of ``text`` (split on '. ' / '; ' outside parentheses)."""
    masked, groups = _mask_parens(text)
    frag = re.split(r"(?:\.\s+|;\s*|->|→)", masked)[-1]
    frag = re.sub(r"^\s*(BLOCKED|FAIL(ED)?|NO-GO)\s*:\s*", "", frag.strip(), flags=re.I)
    return _unmask(frag, groups).strip(" :-–")


def _lane_prefix(item: str) -> tuple[list[str], str]:
    """'Edit/Generate (why)' -> (['Edit', 'Generate'], '(why)'). Lanes may be one to three words."""
    lanes, rest = [], item.strip()
    while rest:
        words = re.split(r"(\s+|/)", rest)
        found = None
        tokens = [w for w in words if w and not w.isspace() and w != "/"]
        for n in range(min(3, len(tokens)), 0, -1):
            lane = normalize_lane(" ".join(t.strip(":,.-") for t in tokens[:n]))
            if lane:
                found = (lane, n)
                break
        if not found:
            break
        lanes.append(found[0])
        # drop the matched words from the front
        consumed, k = 0, 0
        while consumed < found[1] and k < len(words):
            if words[k] and not words[k].isspace() and words[k] != "/":
                consumed += 1
            k += 1
        rest = "".join(words[k:]).lstrip()
        if rest.startswith("/"):
            rest = rest[1:].lstrip()
            continue
        break
    return lanes, rest


def parse_routes(text: str, from_lane: str | None) -> list[dict]:
    """All "sent back to <lane>" routes in a Score/QC note. 'would route' marks a dry run (not a real loop)."""
    if not text or (not FAIL_HINT_RE.search(text) and "route" not in text.lower()):
        return []
    out: list[dict] = []
    for line in text.splitlines():
        marks = list(MARK_RE.finditer(line))
        for i, m in enumerate(marks):
            seg = line[m.end(): marks[i + 1].start() if i + 1 < len(marks) else len(line)]
            if m.group(0) in ("->", "→"):
                prev = re.split(r"[\s/(),;:]+", line[: m.start()].strip())[-1:] or [""]
                if normalize_lane(prev[0].strip(".")):
                    continue  # "Mix -> Polish -> Master": a chain of lanes that ran, not a route
            masked, groups = _mask_parens(seg)
            masked = re.split(r"\.\s+|\.$|;", masked)[0]  # the routing ends at the sentence end
            before = _clause_before(line[: m.start()])
            for item in masked.split(","):
                lanes, rest = _lane_prefix(item)
                if not lanes:
                    break
                specific = _unmask(rest, groups).strip().strip(":-– ").strip()
                if specific.startswith("(") and specific.endswith(")"):
                    specific = specific[1:-1].strip()
                if specific and before and re.search(r"\d", before):
                    reason = f"{before} — {specific}"
                else:
                    reason = specific or before
                for ln in lanes:
                    out.append({"from": from_lane, "to": ln, "reason": reason[:300],
                                "hypothetical": "would" in m.group(0).lower()})
    return out


def parse_sendback(text: str, from_lane: str | None) -> dict | None:
    """First route in a note (kept for callers that only want one)."""
    routes = parse_routes(text, from_lane)
    return routes[0] if routes else None


@dataclass
class Task:
    id: str
    title: str
    slug: str | None
    lane: str | None
    status: str
    assignee: str | None
    created_at: int
    started_at: int | None
    completed_at: int | None
    failures: int
    last_error: str | None
    body: str | None = None
    comments: list[dict] = field(default_factory=list)
    runs: list[dict] = field(default_factory=list)


def _connect(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
    con.row_factory = sqlite3.Row
    return con


def read_board(db: Path) -> dict:
    """Return tasks, links, comments, runs and recent events from the board DB."""
    if not db.exists():
        return {"ok": False, "error": f"kanban DB not found: {db}", "tasks": [], "links": [], "events": []}
    try:
        con = _connect(db)
    except sqlite3.Error as e:  # pragma: no cover - environment specific
        return {"ok": False, "error": str(e), "tasks": [], "links": [], "events": []}
    try:
        tasks: dict[str, Task] = {}
        for r in con.execute(
            "select id,title,body,assignee,status,created_at,started_at,completed_at,"
            "consecutive_failures,last_failure_error from tasks where status != 'archived' order by created_at"
        ):
            slug, lane, _ = parse_title(r["title"])
            tasks[r["id"]] = Task(
                id=r["id"], title=r["title"], slug=slug, lane=lane, status=r["status"],
                assignee=r["assignee"], created_at=r["created_at"] or 0, started_at=r["started_at"],
                completed_at=r["completed_at"], failures=r["consecutive_failures"] or 0,
                last_error=r["last_failure_error"], body=r["body"],
            )
        links = [(r[0], r[1]) for r in con.execute("select parent_id, child_id from task_links")]
        for r in con.execute("select task_id, author, body, created_at from task_comments order by created_at"):
            t = tasks.get(r["task_id"])
            if t:
                t.comments.append({"author": r["author"], "body": r["body"], "at": r["created_at"]})
        for r in con.execute(
            "select id, task_id, profile, status, outcome, summary, error, started_at, ended_at "
            "from task_runs order by started_at"
        ):
            t = tasks.get(r["task_id"])
            if t:
                t.runs.append({k: r[k] for k in r.keys()})
        events = [
            {k: r[k] for k in r.keys()}
            for r in con.execute(
                "select id, task_id, kind, payload, created_at from task_events order by id desc limit 200"
            )
        ]
        return {"ok": True, "error": None, "tasks": list(tasks.values()), "links": links, "events": events}
    except sqlite3.Error as e:
        return {"ok": False, "error": f"kanban read failed: {e}", "tasks": [], "links": [], "events": []}
    finally:
        con.close()


def _task_state(status: str) -> str:
    if status in ACTIVE:
        return "working"
    if status in ("blocked", "failed"):
        return "failed"
    if status == "review":
        return "review"
    if status == "done":
        return "done"
    return "queued"


def build_tracks(board: dict, now: float | None = None) -> list[dict]:
    """Group lane tasks into tracks and work out where each track is."""
    now = now or time.time()
    by_slug: dict[str, list[Task]] = {}
    for t in board.get("tasks", []):
        if t.slug:
            by_slug.setdefault(t.slug, []).append(t)
    tracks = []
    for slug, ts in by_slug.items():
        ts.sort(key=lambda t: (t.created_at, t.id))
        open_tasks = [t for t in ts if t.status not in ("done", "archived")]
        review = [t for t in ts if t.status == "review"]
        approved = any(
            c["body"].upper().startswith("APPROVED") for t in ts for c in t.comments
        )
        if review and not approved:
            current, lane = review[0], "Review"
        elif open_tasks:
            current = next((t for t in open_tasks if t.status in ("running", "blocked")), open_tasks[0])
            lane = current.lane or "Plan"
        else:
            current, lane = ts[-1], "Done"
        # loops: a lane that shows up again after a later lane already ran
        seen: list[str] = []
        loops = 0
        for t in ts:
            if t.lane and t.lane in seen and seen[-1] != t.lane:
                loops += 1
            if t.lane:
                seen.append(t.lane)
        sendbacks, seen_keys, routed_texts = [], set(), set()
        for t in ts:
            texts = [(c["body"], c["at"]) for c in t.comments] + \
                    [(r.get("summary") or "", r.get("ended_at") or r.get("started_at")) for r in t.runs]
            for txt, at in texts:
                for sb in parse_routes(txt, t.lane):
                    key = (sb["to"], sb["reason"])  # a comment and its run summary often repeat the same note
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    sendbacks.append({**sb, "task_id": t.id, "at": at})
                    if not sb["hypothetical"]:
                        routed_texts.add(txt.strip())
        loops = max(loops, len(routed_texts))
        real = [x for x in sendbacks if not x["hypothetical"]]
        state = "done" if lane == "Done" else ("review" if lane == "Review" else _task_state(current.status))
        lane_idx = LANES.index(lane) if lane in LANES else 0
        frac = 0.5 if state == "working" else 0.0
        progress = 100 if lane == "Done" else round(100 * (lane_idx + frac) / (len(LANES) - 1))
        last_activity = max(
            [t.completed_at or t.started_at or t.created_at for t in ts]
            + [c["at"] for t in ts for c in t.comments]
        )
        tracks.append({
            "slug": slug,
            "lane": lane,
            "state": state,
            "progress": progress,
            "loops": loops,
            "sendbacks": sendbacks,
            "last_sendback": (real or sendbacks)[-1] if sendbacks else None,
            "current_task": {"id": current.id, "title": current.title, "status": current.status,
                              "assignee": current.assignee,
                              # blocked by hand (no worker error): the latest comment says why
                              "last_error": current.last_error or (
                                  current.comments[-1]["body"][:800] if current.status == "blocked" and current.comments else None)},
            "review_task_id": review[0].id if review else None,
            "tasks": [{"id": t.id, "lane": t.lane, "status": t.status, "title": t.title} for t in ts],
            "created_at": ts[0].created_at,
            "last_activity": last_activity,
            "age_s": int(now - ts[0].created_at),
        })
    tracks.sort(key=lambda x: -x["last_activity"])
    return tracks


def lane_activity(board: dict, now: float | None = None, handoff_window: int = 20) -> dict:
    """Per-lane agent state for the Live bots screen."""
    now = now or time.time()
    lanes: dict[str, dict] = {
        lane: {"lane": lane, "state": "idle", "task": None, "log": [], "handoff_to": None, "handoff_at": None}
        for lane in LANES if lane not in ("Done",)
    }
    tasks = {t.id: t for t in board.get("tasks", [])}
    for t in board.get("tasks", []):
        if not t.lane or t.lane not in lanes:
            continue
        L = lanes[t.lane]
        if t.status == "running":
            L["state"] = "working"
            L["task"] = {"id": t.id, "title": t.title, "assignee": t.assignee}
        elif t.status in ("blocked", "failed") and L["state"] != "working":
            L["state"] = "failed"
            # blocked by hand (no worker error): the latest comment says why
            why = t.last_error or (t.comments[-1]["body"] if t.comments else None) or \
                (t.runs[-1].get("summary") if t.runs else None)
            L["task"] = {"id": t.id, "title": t.title, "assignee": t.assignee, "error": (why or "")[:800] or None}
        elif t.status == "review" and L["state"] == "idle":
            L["state"] = "waiting"
            L["task"] = {"id": t.id, "title": t.title, "assignee": t.assignee}
        for c in t.comments[-3:]:
            L["log"].append({"at": c["at"], "text": f"{c['author']}: {c['body']}"})
        for r in t.runs[-3:]:
            if r.get("summary") or r.get("error"):
                L["log"].append({"at": r.get("ended_at") or r.get("started_at") or 0,
                                 "text": f"run {r.get('outcome') or r.get('status')}: {r.get('summary') or r.get('error')}"})
    # hand-offs: recent completions fly particles to the next lane
    children: dict[str, list[str]] = {}
    for p, c in board.get("links", []):
        children.setdefault(p, []).append(c)
    for e in board.get("events", []):
        if e["kind"] != "completed" or now - (e["created_at"] or 0) > handoff_window:
            continue
        t = tasks.get(e["task_id"])
        if not t or t.lane not in lanes:
            continue
        nxt = next((tasks[c].lane for c in children.get(t.id, []) if c in tasks and tasks[c].lane), None)
        if not nxt:
            i = LANES.index(t.lane)
            nxt = LANES[min(i + 1, len(LANES) - 1)]
        lanes[t.lane]["handoff_to"] = nxt
        lanes[t.lane]["handoff_at"] = e["created_at"]
    for L in lanes.values():
        L["log"] = sorted(L["log"], key=lambda x: x["at"])[-8:]
    return lanes
