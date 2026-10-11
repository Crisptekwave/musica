"""Scan the Tekno Machine card folders: renders, stems and score JSON."""
from __future__ import annotations

import json
import re
import statistics
from pathlib import Path

from .config import FOLDER_LANES

AUDIO_EXT = {".wav", ".flac", ".mp3", ".ogg", ".m4a"}
STEM_DIRS = {"stems", "swap", "var"}
# Folder -> human label for the Listen screen.
STAGE_LABEL = {
    "gen": "Generate", "edit": "Edit", "stems": "Stem", "swap": "Sound Swap", "var": "Variation",
    "filter": "Filter", "mix": "Mix", "polish": "Master", "review": "Review",
}
LUFS_TARGET, LUFS_TOL, TP_MAX, SUBMONO_MAX, JUDGE_MIN = -9.0, 0.5, -1.0, -30.0, 6.5
NP_REPR_RE = re.compile(r"np\.\w+\(([^()]*)\)")  # "np.float64(165.4)" -> "165.4" in score_card.py notes


def list_cards(cards_dir: Path) -> list[Path]:
    if not cards_dir.is_dir():
        return []
    return sorted((p for p in cards_dir.iterdir() if p.is_dir() and not p.name.startswith(".")),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def card_audio(card: Path) -> list[dict]:
    out = []
    for sub in card.iterdir():
        if not sub.is_dir() or sub.name not in STAGE_LABEL:
            continue
        for f in sub.iterdir():
            if f.suffix.lower() in AUDIO_EXT and f.is_file():
                st = f.stat()
                out.append({
                    "card": card.name,
                    "path": f"{sub.name}/{f.name}",
                    "name": f.stem,
                    "stage": STAGE_LABEL[sub.name],
                    "kind": "stem" if sub.name in STEM_DIRS else "render",
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                })
    out.sort(key=lambda x: -x["mtime"])
    return out


def files_lane(card: Path) -> str | None:
    """Furthest lane that left output on disk."""
    lane = None
    for folder, ln in FOLDER_LANES:
        d = card / folder
        if d.is_dir() and any(d.iterdir()):
            lane = ln
    return lane


def _badge(ok: bool | None, value, label: str, target: str) -> dict:
    return {"label": label, "value": value, "target": target,
            "status": "na" if ok is None else ("pass" if ok else "fail")}


def _judge(card: Path, score: dict) -> dict:
    j = score.get("mix_judge")
    verdict, overall = None, None
    if isinstance(j, dict):
        overall = j.get("overall")
        verdict = j.get("verdict")
    md = card / "score" / "judge.md"
    if md.exists():
        txt = md.read_text(errors="replace")[:20000]
        m = re.search(r"overall[^0-9\-]{0,20}(\d+(?:\.\d+)?)", txt, re.I)
        if m:
            overall = float(m.group(1))
        m = re.search(r"\b(GO|NO-GO|PASS|FAIL)\b", txt)
        if m:
            verdict = m.group(1)
    if overall is None:
        return {"label": "Mix Judge", "value": "pending", "target": f">= {JUDGE_MIN}", "status": "na",
                "draft": score.get("auto_draft_overall")}
    ok = float(overall) >= JUDGE_MIN and (verdict or "").upper() not in ("FAIL", "NO-GO")
    return {"label": "Mix Judge", "value": f"{overall}" + (f" {verdict}" if verdict else ""),
            "target": f">= {JUDGE_MIN}", "status": "pass" if ok else "fail"}


def score_badges(card: Path, score: dict) -> list[dict]:
    hard = " ".join(score.get("hard_fails") or []).lower()
    bpms = [b for b in score.get("bpm_acf_4_16_64") or [] if isinstance(b, (int, float))]
    bpm = round(statistics.median(bpms), 2) if bpms else score.get("bpm")
    bpm_ok = None if bpm is None else ("bpm" not in hard and 160 <= float(bpm) <= 180)
    key = (score.get("key_top3") or [[score.get("key")]])[0][0]
    key_t = score.get("key_target")
    key_ok = None if not key else ((key == key_t) if key_t else "minor" in str(key).lower())
    lufs = score.get("LUFS_I")
    tp = score.get("true_peak_dBTP")
    sub = score.get("sub_S_over_M_dB")
    corr = score.get("corr") or {}
    c = corr.get("250-12k", corr.get("full"))
    mono = score.get("monotony")
    if isinstance(mono, dict):
        mono_ok, mono_v = bool(mono.get("pass", mono.get("PASS"))), mono.get("max_bars_without_change", "ok")
    elif isinstance(mono, str):
        mono_ok, mono_v = ("pass" in mono.lower() or "ok" in mono.lower()) or None, mono
    else:
        mono_ok, mono_v = None, "not measured"
    if "monoton" in hard:
        mono_ok = False
    ear = score.get("earloop")
    if isinstance(ear, (int, float)):
        ear_badge = _badge(ear >= JUDGE_MIN, ear, "EarLoop", f">= {JUDGE_MIN}")
    elif isinstance(ear, dict) and isinstance(ear.get("score"), (int, float)):
        ear_badge = _badge(ear["score"] >= JUDGE_MIN, ear["score"], "EarLoop", f">= {JUDGE_MIN}")
    else:
        ear_badge = _badge(None, "not measured", "EarLoop", f">= {JUDGE_MIN}")
    return [
        _badge(bpm_ok, bpm, "BPM", "160-180 const"),
        _badge(key_ok, key, "Key", key_t or "minor"),
        _badge(None if lufs is None else abs(lufs - LUFS_TARGET) <= LUFS_TOL, lufs, "LUFS-I", "-9 ±0.5"),
        _badge(None if tp is None else tp <= TP_MAX, tp, "True peak", "<= -1.0 dBTP"),
        _badge(None if sub is None else sub <= SUBMONO_MAX, sub, "Sub mono S/M", "<= -30 dB"),
        _badge(None if c is None else c >= 0.0, c, "Stereo corr", ">= 0 (250-12k)"),
        _badge(mono_ok, mono_v, "Monotony", "change <= 32 bars"),
        _judge(card, score),
        ear_badge,
    ]


def card_score(card: Path) -> dict | None:
    f = card / "score" / "score.json"
    if not f.exists():
        return None
    try:
        score = json.loads(f.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return {"card": card.name, "error": f"score.json unreadable: {e}", "badges": [], "mtime": f.stat().st_mtime}
    return {
        "card": card.name,
        "mtime": f.stat().st_mtime,
        "verdict": score.get("verdict") or ("GO" if score.get("PASS") else "NO-GO"),
        "pass": bool(score.get("PASS")),
        "hard_fails": [NP_REPR_RE.sub(r"\1", str(x)) for x in score.get("hard_fails") or []],
        "badges": score_badges(card, score),
        "metrics": {
            "lufs": score.get("LUFS_I"), "tp": score.get("true_peak_dBTP"),
            "sub": score.get("sub_S_over_M_dB"), "corr": (score.get("corr") or {}).get("full"),
            "draft": score.get("auto_draft_overall"),
        },
        "ear": score.get("EAR") or [],
        "duration_s": score.get("duration_s"),
    }


def scan(cards_dir: Path, max_cards: int = 40) -> dict:
    cards = list_cards(cards_dir)[:max_cards]
    audio, scores, lanes = [], [], {}
    for c in cards:
        try:
            audio.extend(card_audio(c))
            s = card_score(c)
            if s:
                scores.append(s)
            lanes[c.name] = files_lane(c)
        except OSError:
            continue
    audio.sort(key=lambda x: -x["mtime"])
    scores.sort(key=lambda x: x["mtime"])
    return {"audio": audio, "scores": scores, "files_lane": lanes}


def tail(path: Path, lines: int = 8, max_bytes: int = 16384) -> list[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    rows = [r for r in data.replace("\r", "\n").split("\n") if r.strip()]
    return rows[-lines:]
