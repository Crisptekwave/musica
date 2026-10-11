"""The panel's only write: an Approve / Reject comment on a Review card.

It goes through the official ``hermes kanban comment`` CLI (never raw SQL) and
is only allowed for tasks whose status is ``review`` right now.
"""
from __future__ import annotations

import re
import sqlite3
import subprocess
from pathlib import Path

TASK_ID_RE = re.compile(r"^t_[0-9a-f]{6,32}$")
AUTHOR = "tekno-control"


class ReviewError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def task_status(db: Path, task_id: str) -> str | None:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
    try:
        r = con.execute("select status from tasks where id = ?", (task_id,)).fetchone()
        return r[0] if r else None
    finally:
        con.close()


def build_comment(decision: str, note: str) -> str:
    note = " ".join((note or "").split())[:1000]
    if decision == "approve":
        return "APPROVED via TEKNO CONTROL" + (f": {note}" if note else "")
    if not note:
        raise ReviewError(400, "Reject needs a note")
    return f"REJECTED via TEKNO CONTROL: {note}"


def submit(db: Path, hermes_bin: Path, board: str, task_id: str, decision: str, note: str,
           runner=subprocess.run) -> str:
    if decision not in ("approve", "reject"):
        raise ReviewError(400, "decision must be approve or reject")
    if not TASK_ID_RE.match(task_id or ""):
        raise ReviewError(400, "bad task id")
    st = task_status(db, task_id)
    if st is None:
        raise ReviewError(404, "task not found")
    if st != "review":
        raise ReviewError(409, f"task is '{st}', not in review")
    text = build_comment(decision, note)
    # argv list, no shell: the note can't inject anything
    p = runner([str(hermes_bin), "kanban", "--board", board, "comment", "--author", AUTHOR, task_id, text],
               capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        raise ReviewError(502, f"hermes kanban comment failed: {(p.stderr or p.stdout or '').strip()[:300]}")
    return text
