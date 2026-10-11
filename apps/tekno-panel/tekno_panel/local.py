"""The local side of the machine: the vLLM model, the Claude-limit fallback folder, style presets and reference tracks.

Everything here is a read-only probe of files or a local HTTP endpoint and tolerates a missing or odd file (shapes
are not fixed yet for the style presets), so a half-written note or an absent folder just shows nothing.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import machine

FALLBACK_MODES = ("auto", "off", "force")
NOTE_STATES = ("open", "claimed", "blocked", "stuck", "done")
FIELD_RE = re.compile(r"^(STATUS|CARD|DIR|NEXT|LOG|DONE|NOT ALLOWED):[ \t]*(.*)$")
FINISH_RE = re.compile(r'tool finish .*?"status":\s*"(\w+)"')


def _read(path: Path, max_bytes: int = 64_000) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read(max_bytes).decode("utf-8-sig", "replace")
    except OSError:
        return ""


def _json(path: Path, max_bytes: int = 2_000_000):
    try:
        if path.stat().st_size > max_bytes:
            return None
        return json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- local model (vLLM)
def local_model(vllm_url: str, gpu: dict | None, timeout: float = 1.5) -> dict:
    """vLLM up/down, the served model id (/v1/models) and the GPU memory it sits in (existing nvidia-smi probe)."""
    d = machine.http_json(vllm_url.rstrip("/") + "/v1/models", timeout)
    rows = d.get("data") if isinstance(d, dict) else None
    row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
    out = {"ok": bool(row), "url": vllm_url, "model": row.get("id") if row else None,
           "max_len": row.get("max_model_len") if row else None}
    g = gpu or {}
    if g.get("ok"):
        mine = [a for a in g.get("apps") or [] if "vllm" in str(a.get("name", "")).lower()]
        out["gpu"] = {"mem_used": g.get("mem_used"), "mem_total": g.get("mem_total"),
                      "model_mem": sum(a.get("mem") or 0 for a in mine) or None}
    return out


# ---------------------------------------------------------------- Claude-limit fallback
def _status_class(text: str) -> str:
    w = (text.split() or [""])[0].lower()
    return "claimed" if w.startswith("claimed") else (w if w in NOTE_STATES else "unknown")


def parse_note(text: str) -> dict:
    """A hand-off note: first line "STATUS: ...", then CARD / DIR / NEXT / LOG blocks (see tools/fallback_watchdog.py)."""
    out = {"status": "", "card": None, "dir": None, "next": [], "log": []}
    block = None
    for raw in text.splitlines():
        m = FIELD_RE.match(raw.strip())
        if m:
            key, val = m.group(1), m.group(2).strip()
            block = key
            if key == "STATUS":
                out["status"] = val
            elif key == "CARD":
                out["card"] = val or None
            elif key == "DIR":
                out["dir"] = val or None
            elif key in ("NEXT", "LOG") and val:
                out[key.lower()].append(val)
            continue
        if raw.strip() and block in ("NEXT", "LOG"):
            out[block.lower()].append(raw.strip())
    out["state"] = _status_class(out["status"])
    return out


def fallback(fb: Path, vllm_ok: bool | None, now: float | None = None, runs: int = 5) -> dict:
    """Mode, Claude's heartbeat age, open/blocked/stuck notes with their last LOG lines, the last runs."""
    now = now or time.time()
    out = {"present": fb.is_dir(), "vllm_ok": vllm_ok, "mode": None, "writable": False, "heartbeat_age": None,
           "notes": [], "done_notes": 0, "runs": []}
    if not out["present"]:
        return out
    mode = _read(fb / "MODE", 64).strip().lower()
    out["mode"] = mode if mode in FALLBACK_MODES else (mode[:20] or "unknown")
    out["writable"] = os.access(fb / "MODE", os.W_OK) if (fb / "MODE").exists() else os.access(fb, os.W_OK)
    try:
        out["heartbeat_age"] = max(0.0, now - (fb / "claude_heartbeat").stat().st_mtime)
    except OSError:
        pass
    notes = []
    try:
        files = sorted((fb / "handoff").glob("*.md"), key=lambda p: -p.stat().st_mtime)
    except OSError:
        files = []
    for f in files[:40]:
        n = parse_note(_read(f))
        if n["state"] == "done":
            out["done_notes"] += 1
            continue
        notes.append({"slug": f.stem, "state": n["state"], "status": n["status"][:120], "card": n["card"],
                      "dir": n["dir"], "next": n["next"][:6], "log": [x[:240] for x in n["log"][-4:]],
                      "mtime": f.stat().st_mtime})
    out["notes"] = notes[:12]
    try:
        logs = sorted((fb / "runs").glob("*.log"), key=lambda p: -p.stat().st_mtime)[:runs]
    except OSError:
        logs = []
    for f in logs:
        text = _read(f, 200_000)
        end = FINISH_RE.findall(text)
        lines = [x for x in text.splitlines() if x.strip()]
        out["runs"].append({"name": f.name, "mtime": f.stat().st_mtime, "result": end[-1] if end else None,
                            "last": (lines[-1][:200] if lines else "")})
    return out


# ---------------------------------------------------------------- style presets (tools/house_profile.json)
def styles(profile: Path) -> list[dict]:
    """Style presets: house_profile.json "presets" = {name: {"bpm": [lo, hi], "status": "calibrated"|"provisional"}}."""
    d = _json(profile)
    rows = d.get("presets") if isinstance(d, dict) else None
    out = []
    for name, v in (rows.items() if isinstance(rows, dict) else []):
        if not isinstance(v, dict):
            continue
        bpm = v.get("bpm")
        ok = isinstance(bpm, list) and len(bpm) == 2 and all(isinstance(x, (int, float)) for x in bpm)
        out.append({"name": str(name)[:40], "bpm": [float(x) for x in bpm] if ok else None,
                    "calibration": str(v.get("status") or "provisional")[:24].lower()})
    return out[:12]


# ---------------------------------------------------------------- reference tracks (measure only)
def references(root: Path) -> dict | None:
    """ref_names.json maps the ASCII file names to the original titles; the README says "measure only"."""
    if not root.is_dir():
        return None
    names = _json(root / "ref_names.json")
    titles = {}
    for r in names if isinstance(names, list) else []:
        if isinstance(r, dict) and isinstance(r.get("name"), str):
            titles[r["name"]] = str(r.get("original") or r["name"])
    items = []
    try:
        mp3 = sorted(p for p in root.iterdir() if p.suffix.lower() == ".mp3" and p.is_file())
    except OSError:
        mp3 = []
    for p in mp3:
        items.append({"file": p.name, "title": titles.get(p.name) or p.stem, "bytes": p.stat().st_size})
    items.sort(key=lambda x: x["title"].lower())
    return {"items": items[:200], "readme": _read(root / "README.md", 4000)}
