"""Which lane scripts are running right now, from a read-only look at /proc.

Every lane script in ``<machine>/scripts`` gets the absolute card folder in its
argv (``.../cards/<slug>``), so one pass over ``/proc/*/cmdline`` tells which
lane is working on which card. Nothing here signals or touches a process.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .config import LANE_DSP_MODES, PROFILE_LANES, SCRIPT_LANES

SCRIPT_RE = re.compile(r"(?:^|/)scripts/(?P<name>[A-Za-z0-9_]+)\.(?P<ext>py|sh)$")
CARD_RE = re.compile(r"/cards/(?P<slug>[A-Za-z0-9][A-Za-z0-9._-]{0,63})(?:/|$)")
TASK_RE = re.compile(r"\bwork kanban task (?P<task>t_[0-9a-f]{6,32})\b")


def _read(path: Path, limit: int = 65536) -> bytes | None:
    try:
        with open(path, "rb") as fh:
            return fh.read(limit)
    except OSError:
        return None


def _boot_time(proc_root: Path) -> float:
    raw = _read(proc_root / "stat") or b""
    for line in raw.decode("ascii", "replace").splitlines():
        if line.startswith("btime "):
            return float(line.split()[1])
    return 0.0


def _start_time(proc_root: Path, pid: str, btime: float) -> float | None:
    raw = _read(proc_root / pid / "stat")
    if not raw:
        return None
    txt = raw.decode("ascii", "replace")
    rest = txt[txt.rfind(")") + 2:].split()  # fields from #3 (state) on
    try:
        ticks = int(rest[19])  # field 22: starttime in clock ticks after boot
    except (IndexError, ValueError):
        return None
    return btime + ticks / os.sysconf("SC_CLK_TCK") if btime else None


def card_from_argv(argv: list[str]) -> str | None:
    for a in argv:
        m = CARD_RE.search(a)
        if m:
            return m.group("slug")
    return None


def classify(argv: list[str]) -> dict | None:
    """Map one command line to {script, lanes, card, task}; None when it is not lane work."""
    for i, a in enumerate(argv):
        m = SCRIPT_RE.search(a)
        if not m:
            continue
        name = m.group("name")
        if m.group("ext") == "sh":
            if name in ("run_e2e", "run_lanes"):
                card = card_from_argv(argv[i + 1:])
                if not card and name == "run_e2e" and i + 1 < len(argv) and re.fullmatch(r"[A-Za-z0-9][\w.-]{0,63}", argv[i + 1]):
                    card = argv[i + 1]
                return {"script": f"{name}.sh", "lanes": [], "card": card, "task": None, "orchestrator": True}
            return None
        if name == "lane_dsp":
            mode = argv[i + 1] if i + 1 < len(argv) else ""
            lanes = LANE_DSP_MODES.get(mode)
            if not lanes:
                return None
            return {"script": f"lane_dsp.py {mode}", "lanes": lanes, "card": card_from_argv(argv[i + 1:]), "task": None}
        if name in SCRIPT_LANES:
            return {"script": f"{name}.py", "lanes": SCRIPT_LANES[name], "card": card_from_argv(argv[i + 1:]),
                    "task": None}
        return None
    # Demucs child of stems.py: ".../venv-demucs/bin/python -m demucs ... -o <card>/stems/_demucs"
    if "-m" in argv and "demucs" in argv:
        return {"script": "demucs", "lanes": ["Stems"], "card": card_from_argv(argv), "task": None}
    # Hermes kanban worker: "hermes -p tekno-mix ... chat -q 'work kanban task t_x'"
    if "-p" in argv:
        prof = argv[argv.index("-p") + 1] if argv.index("-p") + 1 < len(argv) else ""
        if prof in PROFILE_LANES:
            task = next((m.group("task") for a in argv for m in [TASK_RE.search(a)] if m), None)
            return {"script": f"hermes -p {prof}", "lanes": [PROFILE_LANES[prof]], "card": card_from_argv(argv),
                    "task": task, "worker": True}
    return None


def scan(proc_root: Path = Path("/proc"), skip_pids: set[int] | None = None) -> list[dict]:
    """All running lane processes, oldest first. Unreadable or vanished PIDs are skipped."""
    skip = {str(p) for p in (skip_pids or {os.getpid()})}
    btime = _boot_time(proc_root)
    out = []
    try:
        pids = [p for p in os.listdir(proc_root) if p.isdigit() and p not in skip]
    except OSError:
        return []
    for pid in pids:
        raw = _read(proc_root / pid / "cmdline", 16384)
        if not raw:
            continue
        argv = [a.decode("utf-8", "replace") for a in raw.rstrip(b"\0").split(b"\0")]
        info = classify(argv)
        if not info:
            continue
        info["pid"] = int(pid)
        info["since"] = _start_time(proc_root, pid, btime)
        out.append(info)
    out.sort(key=lambda x: (x["since"] or 0, x["pid"]))
    return out


def flock_holder(lock: Path, proc_root: Path = Path("/proc")) -> int | None:
    """PID holding a write flock on ``lock`` according to /proc/locks, or None."""
    try:
        st = lock.stat()
    except OSError:
        return None
    key = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    raw = _read(proc_root / "locks", 262144) or b""
    for line in raw.decode("ascii", "replace").splitlines():
        parts = line.split()
        if "->" in parts or "FLOCK" not in parts or key not in parts:
            continue  # "->" lines are waiters, not holders
        try:
            return int(parts[parts.index(key) - 1])
        except (ValueError, IndexError):
            return None
    return None


def gpu_lock(lock: Path, proc_root: Path = Path("/proc")) -> dict | None:
    """The lane GPU lock: '<pid> <card dir>[ tag]'. Only reported while its holder is alive."""
    try:
        parts = lock.read_text().split()
    except OSError:
        return None
    holder = flock_holder(lock, proc_root)
    if not parts:
        return {"pid": holder, "card": None, "tag": None, "since": None} if holder else None
    pid = int(parts[0]) if parts[0].isdigit() else None
    # The file keeps its last line after the job ends, so a live flock is the real signal.
    # Without /proc/locks fall back to "is that PID still around".
    if (proc_root / "locks").exists():
        alive = holder is not None
    else:
        alive = pid is not None and (proc_root / str(pid)).exists()
    if not alive:
        return None
    try:
        since = lock.stat().st_mtime
    except OSError:
        since = None
    return {"pid": holder or pid, "card": Path(parts[1]).name if len(parts) > 1 else None,
            "tag": parts[2] if len(parts) > 2 else None, "since": since}
