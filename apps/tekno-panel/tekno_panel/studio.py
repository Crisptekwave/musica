"""Studio: the REAPER instances the snapshot loop (tools/reaper_snap.py) found, plus a read-only look at their projects.

The panel never talks to REAPER or to X. It reads ``<data>/reaper/instances.json`` and the JPEGs written next to it,
touches ``<data>/reaper/viewing`` so the loop knows someone is watching, and parses the .rpp text file of each
project (track names, item counts, tempo, length) without ever writing to it.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

DISPLAY_RE = re.compile(r"^:?(1\d{2})$")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_RPP = 64_000_000
_cache: dict = {}


def display_number(raw: str) -> int | None:
    """':104' or '104' (100-199) -> 104; anything else -> None."""
    m = DISPLAY_RE.match(str(raw))
    return int(m.group(1)) if m else None


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'`":
        return s[1:-1]
    return s


def _float(s: str):
    try:
        return float(s)
    except ValueError:
        return None


def parse_rpp(path: Path) -> dict | None:
    """Tempo, tracks (name + item count), project length and render target of a REAPER project; None if unreadable.

    The file is a tree of ``<BLOCK ...`` / ``>`` lines with ``KEY value`` lines inside. Only the keys of the project,
    its tracks and the items directly under a track are read; everything else (FX chains, MIDI events, envelopes)
    is skipped without being kept in memory.
    """
    try:
        if path.stat().st_size > MAX_RPP:
            return None
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None
    out = {"tempo": None, "sample_rate": None, "render_file": None, "tracks": [], "length_s": 0.0}
    stack: list[str] = []
    item_pos = item_len = None
    with fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            if line[0] == "<":
                name = line[1:].split(None, 1)[0] if len(line) > 1 else ""
                stack.append(name)
                if stack[-2:] == ["TRACK"] or (len(stack) == 2 and name == "TRACK"):
                    out["tracks"].append({"name": "", "items": 0})
                elif len(stack) == 3 and name == "ITEM" and stack[1] == "TRACK" and out["tracks"]:
                    out["tracks"][-1]["items"] += 1
                    item_pos = item_len = None
                continue
            if line == ">":
                if len(stack) == 3 and stack[2] == "ITEM" and item_pos is not None and item_len is not None:
                    out["length_s"] = max(out["length_s"], item_pos + item_len)
                if stack:
                    stack.pop()
                continue
            key, _, rest = line.partition(" ")
            if len(stack) == 1:
                if key == "TEMPO":
                    out["tempo"] = _float(rest.split()[0]) if rest.split() else None
                elif key == "SAMPLERATE":
                    v = _float(rest.split()[0]) if rest.split() else None
                    out["sample_rate"] = int(v) if v else None
                elif key == "RENDER_FILE":
                    out["render_file"] = _unquote(rest) or None
            elif len(stack) == 2 and stack[1] == "TRACK" and key == "NAME" and out["tracks"]:
                out["tracks"][-1]["name"] = _unquote(rest)
            elif len(stack) == 3 and stack[2] == "ITEM":
                if key == "POSITION":
                    item_pos = _float(rest)
                elif key == "LENGTH":
                    item_len = _float(rest)
    if not stack and not out["tracks"] and out["tempo"] is None:
        return None
    for i, t in enumerate(out["tracks"], 1):
        t["name"] = t["name"] or f"Track {i}"
    out["length_s"] = round(out["length_s"], 2)
    return out


def project_info(path: Path) -> dict | None:
    """parse_rpp cached by (path, mtime, size): a 5 MB project is read once, not on every poll."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    hit = _cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    info = parse_rpp(path)
    if info is not None:
        info["mtime"] = st.st_mtime
    if len(_cache) > 32:
        _cache.clear()
    _cache[path] = (key, info)
    return info


def last_render(card_dir: Path) -> dict | None:
    """Newest WAV in the card's out/ or mix/ folder."""
    best = None
    for sub in ("out", "mix"):
        try:
            for f in (card_dir / sub).iterdir():
                if f.suffix.lower() == ".wav" and f.is_file():
                    m = f.stat().st_mtime
                    if not best or m > best["mtime"]:
                        best = {"file": f"{sub}/{f.name}", "mtime": m, "bytes": f.stat().st_size}
        except OSError:
            continue
    return best


def touch_viewing(data_dir: Path) -> None:
    """Tell the snapshot loop someone is looking at the Studio page."""
    d = data_dir / "reaper"
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / "viewing").touch()
    except OSError:
        pass


def frame_path(data_dir: Path, display: str) -> Path | None:
    n = display_number(display)
    return data_dir / "reaper" / f"{n}.jpg" if n else None


def instances(data_dir: Path, cards_dir: Path, now: float | None = None) -> dict:
    """What the Studio page shows: one row per REAPER instance with its project summary and frame age."""
    now = now or time.time()
    rdir = data_dir / "reaper"
    try:
        raw = json.loads((rdir / "instances.json").read_text())
        snap_ts = float(raw.get("ts") or 0)
        rows = [r for r in raw.get("instances") or [] if isinstance(r, dict) and display_number(r.get("display", ""))]
    except (OSError, ValueError, TypeError, AttributeError):
        snap_ts, rows = 0.0, []
    out = []
    for r in rows:
        n = display_number(r["display"])
        slug = r.get("slug") if isinstance(r.get("slug"), str) and SLUG_RE.match(r["slug"]) else None
        card = cards_dir / slug if slug else None
        try:
            frame_ts = (rdir / f"{n}.jpg").stat().st_mtime
        except OSError:
            frame_ts = None
        proj = Path(r["project"]) if isinstance(r.get("project"), str) else None
        if proj is not None and card is not None:  # a project is only read from inside its card's reaper folder
            try:
                if not proj.resolve().is_relative_to((card / "reaper").resolve()):
                    proj = None
            except OSError:
                proj = None
        elif proj is not None:
            proj = None
        out.append({
            "display": f":{n}", "n": n, "pid": r.get("pid"), "slug": slug, "uptime_s": r.get("uptime_s"),
            "frame_ts": frame_ts, "frame_age": round(now - frame_ts, 1) if frame_ts else None,
            "frame_url": f"/reaper/frame/{n}.jpg" if frame_ts else None,
            "project": proj.name if proj else None,
            "rpp": project_info(proj) if proj else None,
            "render": last_render(card) if card and card.is_dir() else None,
        })
    return {"ts": now, "snapper_age": round(now - snap_ts, 1) if snap_ts else None, "instances": out}
