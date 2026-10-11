"""The panel's own memory: metrics samples and an activity timeline in SQLite on the data disk.

This is the only database the panel writes, and it is its own (``$TEKNO_DATA_DIR/history.db``); the
machine's data sources stay read-only. Samples every 15 s, events deduplicated by a stable id, 14 days kept.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

METRICS = ("gpu_util", "vram_used", "gpu_temp", "gpu_power", "load1", "disk_free", "ace_busy", "working")
RANGES = {"1h": (3600, 30), "6h": (6 * 3600, 120), "24h": (86400, 600), "7d": (7 * 86400, 3600)}
KEEP_S = 14 * 86400


class History:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.ok = True
        self.error = None
        self._last_sample = 0.0
        self._last_prune = 0.0
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.con = sqlite3.connect(str(path), check_same_thread=False, timeout=5)
            self.con.execute("pragma journal_mode=wal")
            self.con.execute(f"create table if not exists samples (ts integer primary key, {', '.join(m + ' real' for m in METRICS)})")
            self.con.execute("create table if not exists events (id text primary key, ts real, kind text, card text, "
                             "level text, text text)")
            self.con.execute("create index if not exists events_ts on events(ts)")
            self.con.commit()
        except (OSError, sqlite3.Error) as e:
            self.ok, self.error, self.con = False, str(e), None

    # ---------- metrics ----------
    def sample(self, values: dict, now: float, every: float = 15) -> None:
        if not self.con or now - self._last_sample < every:
            return
        self._last_sample = now
        row = [int(now)] + [values.get(m) for m in METRICS]
        with self.lock:
            try:
                self.con.execute(f"insert or replace into samples values ({', '.join('?' * len(row))})", row)
                if now - self._last_prune > 3600:
                    self._last_prune = now
                    self.con.execute("delete from samples where ts < ?", (now - KEEP_S,))
                    self.con.execute("delete from events where ts < ?", (now - KEEP_S,))
                self.con.commit()
            except sqlite3.Error as e:
                self.error = str(e)

    def series(self, rng: str, now: float | None = None) -> dict:
        span, bucket = RANGES.get(rng, RANGES["1h"])
        now = now or time.time()
        if not self.con:
            return {"range": rng, "bucket": bucket, "t": [], **{m: [] for m in METRICS}}
        cols = ", ".join(f"avg({m})" if m != "disk_free" else f"min({m})" for m in METRICS)
        with self.lock:
            rows = self.con.execute(
                f"select (ts / ?) * ? as b, {cols} from samples where ts >= ? group by b order by b",
                (bucket, bucket, int(now - span))).fetchall()
        out = {"range": rng, "bucket": bucket, "t": [r[0] for r in rows]}
        for i, m in enumerate(METRICS):
            out[m] = [None if r[i + 1] is None else round(r[i + 1], 2) for r in rows]
        return out

    # ---------- activity ----------
    def add_events(self, events: list[dict]) -> int:
        if not self.con or not events:
            return 0
        with self.lock:
            try:
                cur = self.con.executemany(
                    "insert or ignore into events values (?, ?, ?, ?, ?, ?)",
                    [(e["id"], e["ts"], e["kind"], e.get("card"), e.get("level", "info"), e["text"][:400]) for e in events])
                self.con.commit()
                return cur.rowcount
            except sqlite3.Error as e:
                self.error = str(e)
                return 0

    def events(self, limit: int = 100, before: float | None = None, kind: str | None = None,
               card: str | None = None, q: str | None = None) -> list[dict]:
        if not self.con:
            return []
        sql, args = "select id, ts, kind, card, level, text from events where 1=1", []
        if before:
            sql += " and ts < ?"
            args.append(before)
        if kind:
            sql += " and kind = ?"
            args.append(kind)
        if card:
            sql += " and card = ?"
            args.append(card)
        if q:
            sql += " and text like ?"
            args.append(f"%{q[:80]}%")
        sql += " order by ts desc limit ?"
        args.append(max(1, min(500, limit)))
        with self.lock:
            rows = self.con.execute(sql, args).fetchall()
        return [{"id": r[0], "ts": r[1], "kind": r[2], "card": r[3], "level": r[4], "text": r[5]} for r in rows]
