#!/usr/bin/env python3
"""reaper-snap: one JPEG per REAPER instance for the Studio page (view only, no input is ever sent).

Producers run the official REAPER headless, one instance per producer on its own Xvfb display (:100-:199). Every
2 s this loop grabs one frame of each such display with ffmpeg x11grab into <TEKNO_DATA_DIR>/reaper/<n>.jpg
(tmp + rename, so a reader never sees half a file) and lists the instances in reaper/instances.json.

It only works while someone watches: the panel touches reaper/viewing whenever the Studio page polls. When that file
is older than VIEW_S the loop idles (5 s ticks: it only refreshes the instance list, no ffmpeg), so the cost is nil
when nobody looks.

An instance is a display with an Xvfb process AND a REAPER process whose environment has that DISPLAY. Only
processes of this user are read; the others (EACCES) are skipped. Nothing is signalled or killed.

  reaper_snap.py [--once]
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

DATA = Path(os.environ.get("TEKNO_DATA_DIR", "/mnt/agent-data/apps/tekno-panel-data")) / "reaper"
CARDS = Path(os.environ.get("TEKNO_CARDS_DIR") or
             Path(os.environ.get("TEKNO_MACHINE_ROOT", "/mnt/agent-data/tekno_machine")) / "cards")
PROC = Path(os.environ.get("TEKNO_PROC_ROOT", "/proc"))
TICK_S = 2.0
IDLE_S = 5.0
VIEW_S = 30.0
GRAB_TIMEOUT_S = 8
DISPLAY_RE = re.compile(r"^(:(1\d{2}))(?:\.\d+)?$")
INST_RE = re.compile(r"/inst-([A-Za-z0-9][A-Za-z0-9._-]{0,63})(?:/|$)")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _read(path: Path, limit: int = 262144) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read(limit)
    except OSError:
        return b""


def _argv(pid: str) -> list[str]:
    return [a for a in _read(PROC / pid / "cmdline").decode("utf-8", "replace").split("\0") if a]


def _environ(pid: str) -> dict[str, str]:
    env = {}
    for kv in _read(PROC / pid / "environ").decode("utf-8", "replace").split("\0"):
        k, _, v = kv.partition("=")
        if k:
            env[k] = v
    return env


def _uptime(pid: str) -> float | None:
    raw = _read(PROC / pid / "stat").decode("ascii", "replace")
    rest = raw[raw.rfind(")") + 2:].split()
    try:
        started = int(rest[19]) / os.sysconf("SC_CLK_TCK")
        up = float(_read(PROC / "uptime").decode("ascii", "replace").split()[0])
    except (IndexError, ValueError):
        return None
    return round(max(0.0, up - started), 1)


def xvfb_displays() -> set[str]:
    """Displays served by an Xvfb process (argv[0] is Xvfb, the display is its ':N' argument)."""
    out = set()
    for d in os.listdir(PROC) if PROC.is_dir() else []:
        if not d.isdigit():
            continue
        argv = _argv(d)
        if argv and Path(argv[0]).name == "Xvfb":
            out.update(a for a in argv[1:] if re.fullmatch(r":1\d{2}", a))
    return out


def card_slug(strings: list[str]) -> str | None:
    """The card an instance belongs to: the inst-<slug> folder named in its command line, cwd or environment."""
    for s in strings:
        m = INST_RE.search(s if s.endswith("/") else s + "/")
        if m and SLUG_RE.match(m.group(1)):
            return m.group(1)
    for s in strings:  # or a project that lives in cards/<slug>/reaper
        m = re.search(r"/cards/([A-Za-z0-9][A-Za-z0-9._-]{0,63})/reaper/", s)
        if m:
            return m.group(1)
    return None


def project_for(argv: list[str], slug: str | None) -> str | None:
    for a in argv[1:]:
        if a.lower().endswith(".rpp") and not a.startswith("-"):
            return a
    if slug:
        try:
            rpps = sorted((CARDS / slug / "reaper").glob("*.rpp"), key=lambda p: -p.stat().st_mtime)
        except OSError:
            rpps = []
        if rpps:
            return str(rpps[0])
    return None


def scan() -> list[dict]:
    """REAPER processes of this user whose DISPLAY has an Xvfb: [{display, n, pid, slug, project, uptime_s}]."""
    displays = xvfb_displays()
    found: dict[str, dict] = {}
    for d in sorted(os.listdir(PROC), key=lambda x: int(x) if x.isdigit() else 0) if PROC.is_dir() else []:
        if not d.isdigit():
            continue
        argv = _argv(d)
        if not argv or Path(argv[0]).name.lower() not in ("reaper", "reaper.bin"):
            continue
        env = _environ(d)  # empty for another user's process: skipped, never an error
        m = DISPLAY_RE.match(env.get("DISPLAY", ""))
        if not m or m.group(1) not in displays:
            continue
        try:
            cwd = os.readlink(PROC / d / "cwd")
        except OSError:
            cwd = ""
        slug = card_slug(argv + [cwd] + list(env.values()))
        found.setdefault(m.group(1), {"display": m.group(1), "n": int(m.group(2)), "pid": int(d), "slug": slug,
                                      "card": bool(slug and (CARDS / slug).is_dir()),
                                      "project": project_for(argv, slug), "uptime_s": _uptime(d)})
    return sorted(found.values(), key=lambda x: x["n"])


def grab(n: int) -> bool:
    """One frame of display :n as <n>.jpg, written atomically. False when the grab failed."""
    tmp, dst = DATA / f".{n}.tmp.jpg", DATA / f"{n}.jpg"
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "x11grab", "-draw_mouse", "0", "-i", f":{n}",
           "-frames:v", "1", "-vf", "scale=1280:-2", "-q:v", "6", str(tmp)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=GRAB_TIMEOUT_S, stdin=subprocess.DEVNULL)
        if r.returncode != 0 or not tmp.is_file() or tmp.stat().st_size < 200:
            return False
        os.replace(tmp, dst)
        return True
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def watched(now: float) -> bool:
    try:
        return now - (DATA / "viewing").stat().st_mtime < VIEW_S
    except OSError:
        return False


def write_instances(insts: list[dict], now: float) -> None:
    tmp = DATA / ".instances.tmp"
    tmp.write_text(json.dumps({"ts": now, "instances": insts}))
    os.replace(tmp, DATA / "instances.json")


def tick(now: float | None = None) -> list[dict]:
    """One pass: list the instances, grab their frames when watched, delete frames of instances that are gone.
    The panel reads frame age from the JPEG's mtime, so nothing about frames is kept here."""
    now = now or time.time()
    insts = scan()
    if watched(now):
        for i in insts:
            grab(i["n"])
    live = {f"{i['n']}.jpg" for i in insts}
    for f in DATA.glob("*.jpg"):
        if f.name not in live:
            f.unlink(missing_ok=True)
    write_instances(insts, now)
    return insts


def main(argv: list[str]) -> int:  # pragma: no cover - long-running service
    DATA.mkdir(parents=True, exist_ok=True)
    if "--once" in argv:
        print(json.dumps(tick(), indent=1))
        return 0
    while True:
        t0 = time.time()
        try:
            tick(t0)
        except Exception as e:  # never let the loop die
            print(f"reaper-snap: {e}", file=sys.stderr, flush=True)
        time.sleep(max(0.2, (TICK_S if watched(time.time()) else IDLE_S) - (time.time() - t0)))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
