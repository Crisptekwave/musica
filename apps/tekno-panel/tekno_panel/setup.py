"""What the machine actually has, read from disk every time, so the panel never drifts from the setup.

Nothing here is a hand-kept list: plugins are the bundles in apps/plugins, models are the Hugging Face caches in
apps/models, tools are the scripts in tekno_machine/tools, deliveries are the hand-off folders, and the changelog is
tekno_machine/CHANGELOG.md (every VM job appends a line to it when it ends). The gateway and night shift come from
the env the fallback and night jobs use.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

BUNDLE = re.compile(r"\.(vst3|clap|lv2|so)$", re.I)


def _names(d: Path, pat: str = "*", limit: int = 80) -> list[str]:
    try:
        return sorted(p.name for p in d.glob(pat))[:limit]
    except OSError:
        return []


def _mtime(p: Path) -> float | None:
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def _json(p: Path):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def plugins(apps: Path) -> dict:
    """{format: [bundle names]} for every plugin bundle in apps/<x>/<format>/ or apps/<x>/usr/lib/<format>/
    (apps/plugins, and synths unpacked from a .deb such as Surge XT)."""
    out = {}
    for fmt in ("vst3", "clap", "lv2"):
        names = set()
        for pat in (f"*/{fmt}/*", f"*/usr/lib/{fmt}/*"):
            try:
                names |= {re.sub(BUNDLE, "", p.name) for p in apps.glob(pat) if BUNDLE.search(p.name)}
            except OSError:
                pass
        if names:
            out[fmt] = sorted(names)
    return out


def changelog(path: Path, n: int = 30) -> list[str]:
    try:
        lines = [ln[2:].strip() for ln in path.read_text(errors="replace").splitlines() if ln.startswith("- ")]
    except OSError:
        return []
    return lines[-n:][::-1]  # newest first


def inventory(machine_root: Path, apps: Path, env=os.environ) -> dict:
    models = apps / "models"
    night = machine_root / "night"
    hand = machine_root / "handoff"
    deliveries = sorted(({"name": p.name, "mtime": _mtime(p)} for p in hand.glob("*") if p.is_dir()),
                        key=lambda d: -(d["mtime"] or 0))[:20] if hand.is_dir() else []
    return {
        "plugins": plugins(apps),
        "models": [n.replace("models--", "").replace("--", "/") for n in _names(models / "hf" / "hub", "models--*")],
        "models_venv": (models / "venv" / "bin" / "python").exists(),
        "tools": [n for n in _names(machine_root / "tools") if n.endswith((".sh", ".py")) and not n.startswith("_")],
        "gateway": {"on": bool(env.get("TEKNO_GATEWAY_LLM")), "model": env.get("TEKNO_GATEWAY_MODEL") or None,
                    "order": env.get("TEKNO_FALLBACK_ORDER", "gateway,local")},
        "night": {"tag": _json(night / "tag_last.json"), "digest_at": (_json(night / "digest_last.json") or {}).get("at")},
        "deliveries": deliveries,
        "changelog": changelog(machine_root / "CHANGELOG.md"),
    }
