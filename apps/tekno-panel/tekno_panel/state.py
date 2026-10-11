"""Collects all live sources into one snapshot that the WebSocket pushes."""
from __future__ import annotations

import datetime as dt
import threading
import time
import urllib.parse

from . import acelog, files, insights, kanban, machine, procs
from .control import Controller
from .history import History
from .config import LANES, Settings
from .mail import AgentMail, bearer_from_hermes, identity, load_agents, redact, threads


def media_url(card: str, path: str) -> str:
    return f"/media/{urllib.parse.quote(card)}/{urllib.parse.quote(path)}"


def audio_url(a: dict) -> str:
    """URL with the file's mtime: lanes overwrite the same paths (polish/master.wav) on every pass,
    so a new version gets a new URL. Follow newest notices it and the browser never plays a cached old take."""
    v = f"?v={int(a.get('mtime') or 0)}"
    if a.get("root") == "handoff":
        return f"/handoff/{urllib.parse.quote(a['path'])}{v}"
    return media_url(a["card"], a["path"]) + v


def _lane_idx(lane: str | None) -> int:
    return LANES.index(lane) if lane in LANES else 0


def _progress(lane: str, frac: float = 0.0) -> int:
    return 100 if lane == "Done" else round(100 * (_lane_idx(lane) + frac) / (len(LANES) - 1))


def run_summary(r: dict) -> dict:
    last = r.get("last") or {}
    return {"name": r["name"], "from": r.get("from"), "step": last.get("step"), "lane": last.get("lane"),
            "started": r.get("started"), "ended": r["mtime"], "finished": r["finished"], "exit": r.get("exit"),
            "verdict": r.get("verdict"), "draft": r.get("draft"), "fails": r.get("fails") or []}


def active_run(info: dict, now: float, window: float = 600) -> dict | None:
    """The newest run log while it is still being written (no 'score exit' yet, touched recently)."""
    runs = info.get("runs") or []
    r = runs[-1] if runs else None
    if r and not r["finished"] and now - r["mtime"] < window and r.get("last"):
        return r
    return None


def merge_tracks(board_tracks: list[dict], disk: dict, ace: dict, now: float,
                 running: list[dict] | None = None) -> list[dict]:
    """Board tracks + what the disk and the process table say right now."""
    running = running or []
    info_all = disk.get("cards") or {}
    trs, seen = [], set()
    rows = [dict(t) for t in board_tracks]
    # cards that exist on disk but not (yet) on the board
    for slug, lane in disk["files_lane"].items():
        if slug in {t["slug"] for t in board_tracks} or not lane:
            continue
        rows.append({
            "slug": slug, "lane": lane, "state": "offboard", "progress": _progress(lane),
            "loops": 0, "sendbacks": [], "last_sendback": None, "current_task": None, "review_task_id": None,
            "tasks": [], "created_at": 0, "last_activity": 0, "age_s": 0,
        })
    job = ace.get("job") or {}
    for t in rows:
        slug = t["slug"]
        if slug in seen:
            continue
        seen.add(slug)
        info = info_all.get(slug) or {}
        t["files_lane"] = disk["files_lane"].get(slug)
        t["board_lane"], t["board_state"] = t["lane"], t["state"]
        t["plan"] = info.get("plan")
        t["reports"] = info.get("reports") or {}
        runs = info.get("runs") or []
        t["runs"] = [run_summary(r) for r in runs[-8:]]
        t["attempts"] = info.get("attempts", 0)
        t["handoff"] = [dict(p, note=(p.get("note") or "")[:400]) for p in info.get("handoff") or []]
        t["disk_mtime"] = info.get("mtime")
        # each pass after the first on disk is a loop back, even when the board was not updated
        if len(runs) - 1 > t["loops"]:
            t["loops"], t["loops_src"] = len(runs) - 1, "disk"
        t["last_activity"] = max(t.get("last_activity") or 0, info.get("mtime") or 0)
        # the card folder moved on (new passes, new score) but nobody updated the board
        board_at = t.get("board_activity", t.get("last_activity")) or 0
        t["board_behind"] = bool(t["tasks"] and info.get("mtime") and info["mtime"] - board_at > 300)
        # live work wins over the board status: a lane script running on this card, or a run still being written
        live = [p for p in running if p.get("card") == slug and p.get("lanes")]
        # an unfinished run log alone is not proof of work (the run may have crashed): need its process
        run = active_run(info, now) if any(p.get("card") == slug for p in running) else None
        if live:
            lane = max((ln for p in live for ln in p["lanes"] if ln in LANES), key=_lane_idx, default=t["lane"])
            p = max(live, key=lambda x: max(_lane_idx(ln) for ln in x["lanes"]))
            t["live"] = {"script": p["script"], "pid": p["pid"], "since": p["since"], "lane": lane, "via": "process"}
        elif run:
            lane = run["last"]["lane"] or t["lane"]
            t["live"] = {"script": f"{run['name']}: {run['last']['step']}", "pid": None, "since": run["last"]["at"],
                         "lane": lane, "via": "run log"}
        if t.get("live"):
            t["lane"], t["state"] = t["live"]["lane"], "working"
            t["progress"] = _progress(t["lane"], 0.5)
        if job.get("card") == slug and ace.get("status") == "generating":
            t["lane"], t["state"] = "Generate", "working"
            frac = (ace.get("progress") or 0) / 100
            sec = ace.get("sections")
            if sec and sec.get("total"):
                frac = min(1.0, (sec["done"] + frac) / sec["total"])
            t["progress"] = _progress("Generate", frac)
        trs.append(t)
    trs.sort(key=lambda x: -(x.get("last_activity") or 0))
    return trs


def tracks_today(board_tracks: list[dict], now: float) -> int:
    today = dt.datetime.fromtimestamp(now).date()
    return sum(1 for t in board_tracks if t["created_at"] and dt.datetime.fromtimestamp(t["created_at"]).date() == today)


def apply_processes(lanes: dict, running: list[dict], disk: dict, now: float) -> None:
    """A lane is working while one of its scripts runs, whatever the board says."""
    for p in running:
        for ln in p.get("lanes") or []:
            L = lanes.get(ln)
            if not L:
                continue
            L["state"] = "working"
            L["task"] = {"id": None, "title": f"[{p.get('card') or '?'}] {p['script']}", "assignee": f"pid {p['pid']}",
                         "since": p.get("since"), "via": "process", "card": p.get("card")}
    # between two quick steps of run_e2e.sh nothing may be running for a moment: use the run log
    for slug, info in (disk.get("cards") or {}).items():
        run = active_run(info, now)
        if not run or not any(p.get("card") == slug for p in running):
            continue
        L = lanes.get(run["last"]["lane"] or "")
        if L and L["state"] != "working":
            L["state"] = "working"
            L["task"] = {"id": None, "title": f"[{slug}] {run['last']['step']}", "assignee": run["name"],
                         "since": run["last"]["at"], "via": "run log", "card": slug}


def section_progress(cards_dir, card: str | None, since: float | None, plan: dict | None) -> dict | None:
    """ace_sections.py writes gen/<i>_<name>.flac one by one: count the ones written since it started."""
    if not card or not since:
        return None
    gen = cards_dir / card / "gen"
    try:
        done = sum(1 for f in gen.iterdir() if f.suffix == ".flac" and f.name[:1].isdigit() and "_r" not in f.stem
                   and f.stat().st_mtime >= since)
    except OSError:
        return None
    total = len((plan or {}).get("sections") or []) or None
    return {"done": done, "total": total}


class Collector:
    """Background thread that refreshes sources at different cadences."""

    def __init__(self, s: Settings, runner=None):
        self.s = s
        self.history = History(s.data_dir / "history.db")  # the panel's own memory, not a data source
        from .helper import client as helper_client
        # sudo-backed actions go through the separate control helper (the panel runs with NoNewPrivileges);
        # tests inject a runner instead
        self.controller = Controller(s, s.data_dir, runner=runner, helper=None if runner else helper_client)
        self._alert_since: dict[str, float] = {}
        self._activity: list[dict] = []
        self._journal: list[dict] = []
        self.lock = threading.Lock()
        self.snap: dict = {"mode": "live", "ts": 0, "tracks": [], "lanes": {}, "audio": [], "scores": [],
                           "history": [], "handoff": [], "processes": [],
                           "health": {}, "chat": {"status": "connecting", "agents": [], "messages": [], "threads": []},
                           "reference": None, "sources": {}, "freshness": {}}
        self._slow_at = self._mail_at = 0.0
        self._disk = {"audio": [], "scores": [], "files_lane": {}, "cards": {}, "handoff": [], "history": []}
        self._hist: dict[str, dict] = {}
        self._health_slow: dict = {}
        self._chat = self.snap["chat"]
        self._slugs: list[str] = []
        self._mail_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.agents = load_agents(s.state_dir)
        self.mail = AgentMail(s.agent_mail_url, s.agent_mail_project,
                              bearer_from_hermes(s.hermes_home / "config.yaml"), self.agents)

    def start(self):
        self._mail_thread = threading.Thread(target=self._mail_loop, name="tekno-mail", daemon=True)
        self._mail_thread.start()
        threading.Thread(target=self._loop, name="tekno-collector", daemon=True).start()

    def _mail_loop(self):
        """agent-mail on its own clock: a slow or unreachable server never delays the live snapshot."""
        wait = 8.0
        while not self._stop.is_set():
            chat = self._read_mail(self._slugs)
            self._chat = chat
            wait = 8.0 if chat["status"] in ("ok", "no_tokens") else min(120.0, wait * 2)  # back off while down
            self._stop.wait(wait)

    def stop(self):
        self._stop.set()

    def get(self) -> dict:
        with self.lock:
            return self.snap

    def _loop(self):
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self.refresh()
            except Exception as e:  # never let the collector die
                with self.lock:
                    self.snap = {**self.snap, "error": f"collector: {e}"}
            self._stop.wait(max(0.2, self.s.push_interval - (time.time() - t0)))

    def _history(self) -> list[dict]:
        """Score history from disk (attempt archives, handoff) plus versions this panel saw get overwritten."""
        on_disk = {e["sha"] for e in self._disk.get("history") or []}
        for e in self._disk.get("history") or []:
            self._hist.setdefault(e["sha"], e)
        extra = [dict(e, run="earlier", runs=["earlier (seen live)"]) for sha, e in self._hist.items() if sha not in on_disk]
        if len(self._hist) > 300:
            for sha in sorted(self._hist, key=lambda k: self._hist[k]["mtime"])[:-300]:
                self._hist.pop(sha, None)
        return sorted((self._disk.get("history") or []) + extra, key=lambda x: x["mtime"])

    def refresh(self, now: float | None = None):
        s = self.s
        now = now or time.time()
        if now - self._slow_at > 6:
            self._slow_at = now
            try:
                self._disk = files.scan(s.cards_path, s.handoff_path)
                self._disk["ok"] = True
            except Exception as e:  # keep the last good scan, flag the source
                self._disk = {**self._disk, "ok": False, "error": f"card scan failed: {e}"}
            try:
                journal = acelog.journal(s.ace_units)
                lora = acelog.lora_log(s.lora_log)
                self._journal = journal
                self._health_slow = {
                    "disk": machine.disk(s.disk_path, s.disk_warn_gb),
                    "services": [machine.service(x, journal, lora, s.cgroup_root) for x in s.services],
                    "journal": journal[-10:], "lora": lora,
                }
            except Exception as e:
                self._health_slow = {**self._health_slow, "error": f"health probes failed: {e}"}
        board = kanban.read_board(s.kanban_path)
        slugs = sorted({t.slug for t in board["tasks"] if t.slug} | set(self._disk["files_lane"]))
        self._slugs = slugs
        if not self._mail_thread and now - self._mail_at > 8:  # tests / one-shot use: read inline
            self._mail_at = now
            self._chat = self._read_mail(slugs)
        board_tracks = kanban.build_tracks(board, now)
        lanes = kanban.lane_activity(board, now)
        running = procs.scan(s.proc_root)
        task_card = {t.id: t.slug for t in board["tasks"]}
        for p in running:  # kanban workers name a task, not a card
            if not p.get("card") and p.get("task"):
                p["card"] = task_card.get(p["task"])
        gen = [p for p in running if "Generate" in (p.get("lanes") or [])]
        ace = machine.ace(s.ace_url, s.machine_root / ".gpu.lock", s.ace_log, gen, s.proc_root, now)
        job = ace.get("job")
        if job and (job.get("script") or "").startswith("ace_sections"):
            plan = ((self._disk.get("cards") or {}).get(job.get("card")) or {}).get("plan")
            ace["sections"] = section_progress(s.cards_path, job.get("card"), job.get("since"), plan)
        apply_processes(lanes, running, self._disk, now)
        if job:
            g = lanes["Generate"]
            g["state"] = "working"
            g["task"] = g["task"] or {"id": None, "title": f"[{job.get('card') or 'ACE'}] ACE take",
                                      "assignee": "ace-step", "since": job.get("since"), "via": "ace log",
                                      "card": job.get("card")}
        if ace.get("events"):
            g = lanes["Generate"]
            g["log"] = (g["log"] + [{"at": e["at"] or now, "text": f"ACE: {e['text']}"} for e in ace["events"][-3:]])[-8:]
        cards_info = self._disk.get("cards") or {}
        for lane, L in lanes.items():
            if L["state"] == "working" and lane != "Generate" and L["task"]:
                slug = L["task"].get("card") or kanban.parse_title(L["task"]["title"])[0]
                runs = (cards_info.get(slug) or {}).get("runs") or []
                if runs:
                    log = s.cards_path / slug / runs[-1]["name"]
                    L["log"] = (L["log"] + [{"at": now, "text": x[:200]} for x in files.tail(log, 3)])[-8:]
        for t in board_tracks:  # last board change, to tell when the disk has moved on without the board
            t["board_activity"] = t["last_activity"]
        audio = [{**a, "url": audio_url(a)} for a in self._disk["audio"]]
        ref = s.reference_path
        tracks = merge_tracks(board_tracks, self._disk, ace, now, running)
        events = board.get("events") or []
        msgs = self._chat.get("messages") or []
        snap = {
            "mode": "live", "ts": now,
            "tracks": tracks,
            "lanes": lanes, "audio": audio,
            "reference": {"name": "SIFRES v02", "url": "/media-ref"} if ref.exists() else None,
            "scores": self._disk["scores"],
            "history": self._history(),
            "handoff": self._disk.get("handoff") or [],
            "processes": [{k: p.get(k) for k in ("pid", "script", "lanes", "card", "since", "task")} for p in running],
            "health": {
                "gpu": machine.gpu(), "ace": ace, "load": machine.load(),
                **self._health_slow,
                "tracks_today": tracks_today(board_tracks, now), "tracks_cap": s.tracks_per_day_cap,
            },
            "chat": self._chat,
            "freshness": {
                "kanban": max((e.get("created_at") or 0 for e in events), default=None),
                "files": max((c.get("mtime") or 0 for c in cards_info.values()), default=None),
                "ace_log": (now - ace["log_age_s"]) if ace.get("log_age_s") is not None else None,
                "mail": msgs[-1]["ts"] if msgs else None,
            },
            "sources": {"kanban": {"ok": board["ok"], "error": board["error"], "path": str(s.kanban_path)},
                        "files": {"ok": self._disk.get("ok", True), "error": self._disk.get("error"),
                                  "path": str(s.cards_path)},
                        "mail": {"ok": self._chat["status"] == "ok", "error": self._chat.get("error")}},
        }
        self._enrich(snap, board, ace, now)
        with self.lock:
            self.snap = snap
        return snap

    def _enrich(self, snap: dict, board: dict, ace: dict, now: float) -> None:
        """Pause state, alerts, audit, activity timeline and metrics history."""
        snap["machine"] = self.controller.paused()
        al = insights.alerts(snap, now)
        keys = {a["key"] for a in al}
        self._alert_since = {k: v for k, v in self._alert_since.items() if k in keys}
        for a in al:
            a["since"] = self._alert_since.setdefault(a["key"], now)
        snap["alerts"] = al
        audit = self.controller.audit_tail(30)
        snap["audit"] = audit[:12]
        snap["controls"] = {"enabled": self.s.controls, **self.controller.registry()}
        if now - self._slow_at < 0.5 or not self._activity:  # once per slow cycle
            try:
                self.history.add_events(insights.events(board, self._disk, ace, self._journal, self._chat, audit, al))
                self._activity = self.history.events(60)
            except Exception as e:  # history is a nice-to-have, never break the live snapshot
                snap.setdefault("errors", []).append(f"history: {e}")
        snap["activity"] = self._activity
        g = snap["health"].get("gpu") or {}
        d = snap["health"].get("disk") or {}
        self.history.sample({
            "gpu_util": g.get("util"), "vram_used": g.get("mem_used"), "gpu_temp": g.get("temp"),
            "gpu_power": g.get("power"), "load1": (snap["health"].get("load") or {}).get("1m"),
            "disk_free": d.get("free_gb"), "ace_busy": 1.0 if ace.get("status") == "generating" else 0.0,
            "working": float(sum(1 for L in snap["lanes"].values() if L["state"] == "working")),
        }, now)
        snap["sources"]["history"] = {"ok": self.history.ok, "error": self.history.error,
                                      "path": str(self.history.path)}

    def _read_mail(self, slugs: list[str]) -> dict:
        ident = identity(self.agents)
        try:
            agents = self.mail.directory()
        except Exception as e:
            return {"status": "down", "error": redact(f"agent-mail unreachable: {e}"), "agents": [], "messages": [],
                    "threads": threads([], slugs), "identity": ident}
        try:
            msgs, source = self.mail.unified(400), "unified inbox"
        except Exception as e:
            if not self.mail.agents:
                return {"status": "no_tokens", "agents": agents, "messages": [], "threads": threads([], slugs),
                        "identity": ident, "error": redact(f"unified inbox unavailable ({e}); add agent tokens to "
                                                           "agent_mail_agents.json to read mailboxes.")}
            try:
                msgs, source = self.mail.messages(), "agent mailboxes"
            except Exception as e2:
                return {"status": "down", "error": redact(str(e2)), "agents": agents, "messages": [],
                        "threads": threads([], slugs), "identity": ident}
        keep = set(self.s.agent_mail_projects)
        msgs = [m for m in msgs if str(m.get("thread") or "").startswith("card-") or not m.get("project")
                or m.get("project") in keep]
        return {"status": "ok", "error": None, "agents": agents, "messages": msgs[-200:], "source": source,
                "threads": threads(msgs, slugs), "identity": ident}
