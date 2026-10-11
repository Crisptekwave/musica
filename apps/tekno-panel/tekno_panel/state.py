"""Collects all live sources into one snapshot that the WebSocket pushes."""
from __future__ import annotations

import datetime as dt
import threading
import time
import urllib.parse

from . import files, kanban, machine
from .config import LANES, Settings
from .mail import AgentMail, bearer_from_hermes, load_agents


def media_url(card: str, path: str) -> str:
    return f"/media/{urllib.parse.quote(card)}/{urllib.parse.quote(path)}"


def merge_tracks(board_tracks: list[dict], disk: dict, ace: dict, now: float) -> list[dict]:
    trs = []
    seen = set()
    job_card = (ace.get("job") or {}).get("card")
    for t in board_tracks:
        t = dict(t)
        t["files_lane"] = disk["files_lane"].get(t["slug"])
        if job_card == t["slug"] and t["lane"] == "Generate" and ace.get("progress") is not None:
            idx = LANES.index("Generate")
            t["state"] = "working"
            t["progress"] = round(100 * (idx + ace["progress"] / 100) / (len(LANES) - 1))
        trs.append(t)
        seen.add(t["slug"])
    # cards that exist on disk but not (yet) on the board
    for slug, lane in disk["files_lane"].items():
        if slug in seen or not lane:
            continue
        idx = LANES.index(lane)
        trs.append({
            "slug": slug, "lane": lane, "state": "offboard", "progress": round(100 * idx / (len(LANES) - 1)),
            "loops": 0, "sendbacks": [], "last_sendback": None, "current_task": None, "review_task_id": None,
            "tasks": [], "created_at": 0, "last_activity": 0, "age_s": 0, "files_lane": lane,
        })
    return trs


def tracks_today(board_tracks: list[dict], now: float) -> int:
    today = dt.datetime.fromtimestamp(now).date()
    return sum(1 for t in board_tracks if t["created_at"] and dt.datetime.fromtimestamp(t["created_at"]).date() == today)


class Collector:
    """Background thread that refreshes sources at different cadences."""

    def __init__(self, s: Settings):
        self.s = s
        self.lock = threading.Lock()
        self.snap: dict = {"mode": "live", "ts": 0, "tracks": [], "lanes": {}, "audio": [], "scores": [],
                           "health": {}, "chat": {"status": "connecting", "agents": [], "messages": []},
                           "reference": None, "sources": {}}
        self._slow_at = self._mail_at = 0.0
        self._disk = {"audio": [], "scores": [], "files_lane": {}}
        self._health_slow: dict = {}
        self._chat = self.snap["chat"]
        self._stop = threading.Event()
        self.mail = AgentMail(s.agent_mail_url, s.agent_mail_project,
                              bearer_from_hermes(s.hermes_home / "config.yaml"), load_agents(s.state_dir))

    def start(self):
        threading.Thread(target=self._loop, name="tekno-collector", daemon=True).start()

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

    def refresh(self, now: float | None = None):
        s = self.s
        now = now or time.time()
        if now - self._slow_at > 6:
            self._slow_at = now
            try:
                self._disk = files.scan(s.cards_path)
                self._disk["ok"] = True
            except OSError as e:
                self._disk = {"audio": [], "scores": [], "files_lane": {}, "ok": False, "error": str(e)}
            self._health_slow = {
                "disk": machine.disk(s.disk_path, s.disk_warn_gb),
                "services": [machine.service(x) for x in s.services],
            }
        if now - self._mail_at > 8:
            self._mail_at = now
            self._chat = self._read_mail()
        board = kanban.read_board(s.kanban_path)
        board_tracks = kanban.build_tracks(board, now)
        lanes = kanban.lane_activity(board, now)
        ace = machine.ace(s.ace_url, s.machine_root / ".gpu.lock", s.ace_log)
        if ace.get("job"):
            g = lanes["Generate"]
            if g["state"] != "failed":
                g["state"] = "working"
                g["task"] = g["task"] or {"id": None, "title": f"[{ace['job']['card']}] ACE take", "assignee": "ace-step"}
            g["log"] = (g["log"] + [{"at": now, "text": ln} for ln in ace["log"][-3:]])[-8:]
        for lane, L in lanes.items():
            if L["state"] == "working" and lane != "Generate" and L["task"]:
                slug = kanban.parse_title(L["task"]["title"])[0]
                run_log = s.cards_path / (slug or "_") / "run.log"
                L["log"] = (L["log"] + [{"at": now, "text": x} for x in files.tail(run_log, 3)])[-8:]
        audio = [{**a, "url": media_url(a["card"], a["path"])} for a in self._disk["audio"]]
        ref = s.reference_path
        snap = {
            "mode": "live", "ts": now,
            "tracks": merge_tracks(board_tracks, self._disk, ace, now),
            "lanes": lanes, "audio": audio,
            "reference": {"name": "SIFRES v02", "url": "/media-ref"} if ref.exists() else None,
            "scores": self._disk["scores"],
            "health": {
                "gpu": machine.gpu(), "ace": ace, "load": machine.load(),
                **self._health_slow,
                "tracks_today": tracks_today(board_tracks, now), "tracks_cap": s.tracks_per_day_cap,
            },
            "chat": self._chat,
            "sources": {"kanban": {"ok": board["ok"], "error": board["error"], "path": str(s.kanban_path)},
                        "files": {"ok": self._disk.get("ok", True), "error": self._disk.get("error")},
                        "mail": {"ok": self._chat["status"] == "ok", "error": self._chat.get("error")}},
        }
        with self.lock:
            self.snap = snap
        return snap

    def _read_mail(self) -> dict:
        try:
            agents = self.mail.directory()
        except Exception as e:
            return {"status": "down", "error": f"agent-mail unreachable: {e}", "agents": [], "messages": []}
        if not self.mail.agents:
            return {"status": "no_tokens", "agents": agents, "messages": [],
                    "error": "Add agent tokens to agent_mail_agents.json to read the threads."}
        try:
            return {"status": "ok", "error": None, "agents": agents, "messages": self.mail.messages()}
        except Exception as e:
            return {"status": "down", "error": str(e), "agents": agents, "messages": []}
