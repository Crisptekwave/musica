"""Ryan's palette: clips cut from his own tracks, indexed in index.json next to a README.

index.json is read defensively: the clips may sit in a list or a dict, under "files" / "clips" / a mapping of
path -> entry, with the path under "file" or "path". Audio is only ever served from inside the palette folder.
"""
from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

from .files import AUDIO_EXT
from .local import _read

KEEP = ("source", "type", "bpm", "key", "bars", "dur_s", "character", "hard_kick", "tune_note", "tune_hz", "decay_ms",
        "bass_bleed_db", "hat_bleed_db", "gain_db", "track_key")


def _entries(d) -> list[dict]:
    if isinstance(d, dict):
        for k in ("files", "clips", "items", "entries"):
            if isinstance(d.get(k), (list, dict)):
                return _entries(d[k])
        rows = []  # a plain {path: {...}} mapping
        for k, v in d.items():
            if isinstance(v, dict) and k not in ("meta", "sources", "rejects"):
                rows.append({"file": k, **v} if "file" not in v and "path" not in v else v)
        return rows
    return [x for x in d if isinstance(x, dict)] if isinstance(d, list) else []


def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and abs(x) != float("inf") else None


def load(root: Path) -> dict:
    """{"clips": [...], "sources": {...}, "readme": str, "types": [...], "styles": [...]}; empty when there is no palette."""
    try:
        raw = json.loads((root / "index.json").read_text(encoding="utf-8-sig", errors="replace"))
    except (OSError, ValueError):
        raw = None
    clips, base = [], root.resolve()
    for e in _entries(raw):
        rel = e.get("file") or e.get("path")
        if not isinstance(rel, str) or Path(rel).suffix.lower() not in AUDIO_EXT:
            continue
        p = (base / rel).resolve()
        if not p.is_relative_to(base):
            continue
        c = {k: e[k] for k in KEEP if k in e and (isinstance(e[k], (str, bool)) or _num(e[k]) is not None)}
        c["file"] = rel
        c["name"] = Path(rel).name
        c["styles"] = [s for s in e.get("fits_styles") or [] if isinstance(s, str)][:8]
        c["url"] = "/palette/media/" + urllib.parse.quote(rel)
        c["hard_kick"] = e.get("hard_kick") is True
        clips.append(c)
    meta = raw.get("meta") if isinstance(raw, dict) and isinstance(raw.get("meta"), dict) else {}
    src = meta.get("sources") if isinstance(meta.get("sources"), dict) else raw.get("sources") if isinstance(raw, dict) else None
    sources = {str(k): (v if isinstance(v, str) else "")[:120] for k, v in (src or {}).items()} if isinstance(src, dict) else {}
    return {"ok": raw is not None, "clips": clips, "sources": sources,
            "types": sorted({c["type"] for c in clips if isinstance(c.get("type"), str)}),
            "styles": sorted({s for c in clips for s in c["styles"]}),
            "readme": _read(root / "README.md", 30_000)}
