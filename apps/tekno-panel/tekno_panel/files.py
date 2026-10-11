"""Scan the Tekno Machine folders: card renders, stems, plan, lane reports, run logs, score JSON, handoff."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import math
import os
import re
import statistics
from pathlib import Path

from .config import FOLDER_LANES, RUN_STEP_LANES

AUDIO_EXT = {".wav", ".flac", ".mp3", ".ogg", ".m4a"}
STEM_DIRS = {"stems", "swap", "var"}
# Folder -> human label for the Listen screen.
STAGE_LABEL = {
    "gen": "Generate", "edit": "Edit", "stems": "Stem", "swap": "Sound Swap", "var": "Variation",
    "filter": "Filter", "mix": "Mix", "polish": "Master", "review": "Review",
}
REPORT_DIRS = ("edit", "stems", "swap", "var", "filter", "mix", "polish")
LUFS_TARGET, LUFS_TOL, TP_MAX, SUBMONO_MAX, JUDGE_MIN = -9.0, 0.5, -1.0, -30.0, 6.5
BPM_TOL, CORR_MIN, KNOCK_MIN, MONOTONY_MAX = 0.05, 0.3, 3.0, 32
CORR_GATE_BANDS = ("250-1k", "1k-4k", "4k-12k", "250-12k")  # qc_gate.py: each must be >= 0.3
NP_REPR_RE = re.compile(r"np\.\w+\(([^()]*)\)")  # "np.float64(165.4)" -> "165.4" in score_card.py notes
RUN_STEP_RE = re.compile(r"^=== (?P<step>[a-z_]+)(?: (?P<hms>\d{1,2}:\d{2}:\d{2}))?\s*$")
SCORE_EXIT_RE = re.compile(r"^score exit (?P<code>\d+)\s*$")


log = logging.getLogger("tekno.files")


def _num(x):
    """A finite number or None (score_card.py can write NaN / -Infinity, which JSON can't carry to the browser)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return x if math.isfinite(x) else None


def _loads(text):
    return json.loads(text, parse_constant=lambda c: None)  # NaN / Infinity -> null


def read_json(path: Path, max_bytes: int = 2_000_000):
    try:
        if path.stat().st_size > max_bytes:
            return None
        return _loads(path.read_text(errors="replace"))
    except (OSError, ValueError):
        return None


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
                    "root": "cards",
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


def card_mtime(card: Path) -> float:
    """Newest change in the live card (stage folders and run logs; attempt archives excluded)."""
    newest = 0.0
    try:
        for e in os.scandir(card):
            if e.name.startswith((".", "attempt")):
                continue
            if e.is_file():
                newest = max(newest, e.stat().st_mtime)
            elif e.is_dir():
                newest = max(newest, e.stat().st_mtime)
                for f in os.scandir(e.path):
                    newest = max(newest, f.stat().st_mtime)
    except OSError:
        pass
    return newest


def plan_summary(card: Path) -> dict | None:
    """plan/plan.json (song_plan.py), falling back to study/brief.json for bpm/key."""
    p = read_json(card / "plan" / "plan.json")
    if isinstance(p, dict):
        secs = [{"name": str(s.get("name", "?")), "bars": _num(s.get("bars")), "energy": _num(s.get("energy")),
                 "start_bar": _num(s.get("start_bar"))}
                for s in p.get("sections") or [] if isinstance(s, dict)]
        return {"source": "plan", "bpm": _num(p.get("bpm")), "key": p.get("key"), "total_bars": _num(p.get("total_bars")),
                "total_s": _num(p.get("total_s")), "sections": secs[:24],
                "rule": str(p.get("variation_rule") or "")[:200]}
    b = read_json(card / "study" / "brief.json")
    if isinstance(b, dict) and (b.get("bpm") or b.get("key")):
        return {"source": "brief", "bpm": _num(b.get("bpm")), "key": b.get("key"), "total_bars": None,
                "total_s": None, "sections": [], "rule": ""}
    return None


def stage_reports(card: Path) -> dict:
    """Small summaries of each lane's report.json (the numbers the lanes print)."""
    out = {}
    for d in REPORT_DIRS:
        r = read_json(card / d / "report.json", 200_000)
        if not isinstance(r, dict):
            continue
        keep = {k: v for k, v in r.items() if k not in ("in", "out", "sections", "events") and
                isinstance(v, (int, float, str, bool)) and (not isinstance(v, str) or len(v) < 120)}
        if isinstance(r.get("events"), list):
            keep["events"] = len(r["events"])
        if isinstance(r.get("sections"), list):
            keep["sections"] = len(r["sections"])
        try:
            keep["mtime"] = (card / d / "report.json").stat().st_mtime
        except OSError:
            pass
        out[d] = keep
    return out


def _step_time(hms: str | None, ref: float) -> float | None:
    """'11:39:41' from a run log -> epoch, taking the date from the log file's mtime."""
    if not hms:
        return None
    try:
        d = dt.datetime.fromtimestamp(ref)
        h, m, s = (int(x) for x in hms.split(":"))
        t = d.replace(hour=h, minute=m, second=s, microsecond=0).timestamp()
    except ValueError:
        return None
    return t - 86400 if t > ref + 120 else t


def parse_run_log(path: Path) -> dict | None:
    """One run_e2e.sh pass: '=== <step> HH:MM:SS' headers, the score summary line and 'score exit N'."""
    try:
        st = path.stat()
        with open(path, "rb") as fh:
            if st.st_size > 512_000:
                fh.seek(st.st_size - 512_000)
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    steps, verdict, draft, fails, code = [], None, None, [], None
    for line in text.splitlines():
        line = line.strip()
        m = RUN_STEP_RE.match(line)
        if m:
            steps.append({"step": m.group("step"), "lane": RUN_STEP_LANES.get(m.group("step")),
                          "at": _step_time(m.group("hms"), st.st_mtime)})
            continue
        m = SCORE_EXIT_RE.match(line)
        if m:
            code = int(m.group("code"))
            continue
        if line.startswith('{"verdict"'):
            try:
                j = _loads(line)
            except ValueError:
                continue
            if not isinstance(j, dict):
                continue
            verdict, draft = j.get("verdict"), _num(j.get("overall_draft"))
            fails = [NP_REPR_RE.sub(r"\1", str(x))[:200] for x in j.get("fails") or []]
    if not steps and code is None:
        return None
    return {"name": path.name, "mtime": st.st_mtime, "steps": steps,
            "from": steps[0]["step"] if steps else None, "last": steps[-1] if steps else None,
            "started": steps[0]["at"] if steps else None, "finished": code is not None, "exit": code,
            "verdict": verdict, "draft": draft, "fails": fails}


def _run_order(p: Path) -> tuple:
    m = re.match(r"run(\d*)\.log$", p.name)
    return (int(m.group(1) or 1) if m else 0, p.stat().st_mtime)


def run_logs(card: Path) -> list[dict]:
    try:
        logs = sorted((p for p in card.glob("run*.log") if p.is_file()), key=_run_order)
    except OSError:
        return []
    return [r for r in (parse_run_log(p) for p in logs) if r]


# ---------------------------------------------------------------- scores
def _badge(ok: bool | None, value, label: str, target: str, status: str | None = None, **extra) -> dict:
    st = status or ("na" if ok is None else ("pass" if ok else "fail"))
    return {"label": label, "value": value, "target": target, "status": st, **extra}


def _judge(root: Path, score: dict) -> dict:
    j = score.get("mix_judge")
    verdict, overall = None, None
    if isinstance(j, dict):
        overall = _num(j.get("overall"))
        verdict = j.get("verdict") if isinstance(j.get("verdict"), str) else None
    md = root / "score" / "judge.md"
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
                "draft": score.get("auto_draft_overall"),
                "detail": "auto-draft only; Mix Judge has not scored this master yet"}
    ok = overall >= JUDGE_MIN and (verdict or "").upper() not in ("FAIL", "NO-GO")
    return {"label": "Mix Judge", "value": f"{overall}" + (f" {verdict}" if verdict else ""),
            "target": f">= {JUDGE_MIN}", "status": "pass" if ok else "fail"}


def ref_compare(score: dict) -> dict | None:
    """ref_compare.py output (score.json 'vs_sifres', kept as the key name): band levels vs the reference (the house
    master since 2026-10-11, SIFRES v02 before), crest, 1-4k width."""
    vs = score.get("vs_sifres")
    if isinstance(vs, str):
        return {"error": vs[:200]}
    if not isinstance(vs, dict):
        return None
    bands = vs.get("band_diff_dB_vs_SIFRES")
    return {
        "bands": {str(k): _num(v) for k, v in bands.items()} if isinstance(bands, dict) else {},
        "dist": _num(vs.get("band_rms_dist_dB")), "tilt": _num(vs.get("hf_tilt_dB")),
        "crest": _num(vs.get("crest_dB")), "crest_ref": _num(vs.get("crest_ref_dB")),
        "corr14": _num(vs.get("corr_1-4k")), "corr14_ref": _num(vs.get("corr_1-4k_ref")),
        "ref_name": ref_short(vs.get("reference")),
    }


def ref_short(name) -> str:
    """'Filter Sweep (extended rework)' -> 'Filter Sweep'; scores from before the house style named no reference."""
    if not isinstance(name, str) or not name.strip():
        return "SIFRES"
    return name.split("(")[0].strip()[:24] or "SIFRES"


def _list(x) -> list:
    return x if isinstance(x, list) else []


def _top_key(score: dict):
    top = _list(score.get("key_top3"))
    first = top[0] if top else None
    if isinstance(first, (list, tuple)) and first and isinstance(first[0], str):
        return first[0]
    return score.get("key") if isinstance(score.get("key"), str) else None


def score_badges(root: Path, score: dict) -> list[dict]:
    hard = " ".join(str(x) for x in _list(score.get("hard_fails"))).lower()
    # BPM: qc_gate checks the 16- and 64-beat ACF values against the brief's target (+-0.05)
    bpms = [b for b in _list(score.get("bpm_acf_4_16_64")) if _num(b) is not None]
    target = _num(score.get("bpm_target"))
    if len(bpms) >= 3:
        bpm = round(bpms[1], 2)
        detail = "ACF 4/16/64-beat: " + " / ".join(f"{b:g}" for b in bpms[:3])
        bpm_ok = (abs(bpms[1] - target) <= BPM_TOL and abs(bpms[2] - target) <= BPM_TOL) if target else (160 <= bpm <= 180)
    else:
        bpm = round(statistics.median(bpms), 2) if bpms else _num(score.get("bpm"))
        detail, bpm_ok = None, (None if bpm is None else 160 <= float(bpm) <= 180)
    if "bpm" in hard:
        bpm_ok = False
    key = _top_key(score)
    key_t = score.get("key_target") if isinstance(score.get("key_target"), str) else None
    key_ok = None if not key else ((key == key_t) if key_t else "minor" in str(key).lower())
    if "key" in hard:
        key_ok = False
    kts, kps = _num(score.get("key_target_score")), _num(score.get("key_parallel_score"))
    lufs = _num(score.get("LUFS_I"))
    tp = _num(score.get("true_peak_dBTP"))
    sub = _num(score.get("sub_S_over_M_dB"))
    corr = score.get("corr") if isinstance(score.get("corr"), dict) else {}
    c_full = _num(corr.get("full"))
    gate = {b: _num(corr.get(b)) for b in CORR_GATE_BANDS if _num(corr.get(b)) is not None}
    corr_ok = None if not gate and c_full is None else (all(v >= CORR_MIN for v in gate.values()) and "corr" not in hard)
    knock = _num(score.get("fair_knock100_250_median"))
    ref = ref_compare(score)
    # monotony: variation.py reports bars without a change; score_card fails it above 32
    var = read_json(root / "var" / "report.json", 200_000)
    gap = _num(var.get("max_bars_without_change")) if isinstance(var, dict) else None
    mono = score.get("monotony")
    if gap is not None:
        mono_ok, mono_v = gap <= MONOTONY_MAX, f"{gap:g} bars"
    elif isinstance(mono, dict):
        mono_ok, mono_v = bool(mono.get("pass", mono.get("PASS"))), str(mono.get("max_bars_without_change", "ok"))
    else:
        mono_ok, mono_v = None, "not measured"
    if "monoton" in hard:
        mono_ok = False
    ear = score.get("earloop")
    if _num(ear) is not None:
        ear_badge = _badge(ear >= JUDGE_MIN, ear, "EarLoop", f">= {JUDGE_MIN}")
    elif isinstance(ear, dict) and _num(ear.get("score")) is not None:
        ear_badge = _badge(ear["score"] >= JUDGE_MIN, ear["score"], "EarLoop", f">= {JUDGE_MIN}")
    else:
        ear_badge = _badge(None, "not measured", "EarLoop", f">= {JUDGE_MIN}",
                           detail=str(ear)[:120] if isinstance(ear, str) else None)
    if ref and "error" not in ref and ref["dist"] is not None:
        ref_badge = _badge(None, f"{ref['dist']:.1f} dB", f"vs {ref['ref_name']}", "band distance (lower = closer)", status="info",
                           detail=f"HF tilt {ref['tilt']:+.1f} dB · crest {ref['crest']} vs {ref['crest_ref']} dB"
                           if ref["tilt"] is not None else None)
    else:
        ref_badge = _badge(None, "not measured", f"vs {(ref or {}).get('ref_name') or 'reference'}", "band distance (lower = closer)",
                           detail=(ref or {}).get("error"))
    return [
        _badge(bpm_ok, bpm, "BPM", f"{target:g} ±{BPM_TOL}" if target else "160-180 const", detail=detail),
        _badge(key_ok, key, "Key", key_t or "minor",
               detail=f"corr {kts:.3f} vs parallel {kps:.3f}" if kts is not None and kps is not None else None),
        _badge(None if lufs is None else abs(lufs - LUFS_TARGET) <= LUFS_TOL, lufs, "LUFS-I", "-9 ±0.5"),
        _badge(None if tp is None else tp <= TP_MAX, tp, "True peak", "<= -1.0 dBTP"),
        _badge(None if sub is None else sub <= SUBMONO_MAX, sub, "Sub mono S/M", "<= -30 dB (20-120 Hz)"),
        _badge(corr_ok, c_full, "Stereo corr", ">= 0.3 per band",
               detail=" · ".join(f"{b} {v:.2f}" for b, v in gate.items()) or None),
        # documented target (DESIGN.md: knock >= +3 dB), not a hard gate in score_card.py -> warn, not fail
        _badge(None, None if knock is None else round(knock, 1), "Kick knock", ">= +3 dB",
               status="na" if knock is None else ("pass" if knock >= KNOCK_MIN else "warn"),
               detail="fair.py KNOCK100-250 median · design target, not a hard gate"),
        ref_badge,
        _badge(mono_ok, mono_v, "Monotony", f"change <= {MONOTONY_MAX} bars"),
        _judge(root, score),
        ear_badge,
    ]


def _sha(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()[:12]


def score_entry(card_name: str, f: Path, root: Path, run: str) -> dict | None:
    """Parse one score.json written by score_card.py. ``root`` is the folder its lane reports live in."""
    try:
        raw = f.read_bytes()
        mtime = f.stat().st_mtime
    except OSError:
        return None
    try:
        score = _loads(raw)
        if not isinstance(score, dict):
            raise ValueError("not an object")
    except ValueError as e:
        return {"card": card_name, "run": run, "error": f"score.json unreadable: {e}", "badges": [], "mtime": mtime,
                "sha": _sha(raw), "metrics": {}, "hard_fails": [], "verdict": "?", "pass": False}
    verdict = score.get("verdict") or ("GO" if score.get("PASS") else "NO-GO")
    ref = ref_compare(score)
    bpms = [b for b in _list(score.get("bpm_acf_4_16_64")) if _num(b) is not None]
    drafts = score.get("auto_draft_scores")
    corr = score.get("corr") if isinstance(score.get("corr"), dict) else {}
    return {
        "card": card_name,
        "run": run,
        "sha": _sha(raw),
        "mtime": mtime,
        "verdict": verdict,
        "go": str(verdict).upper().startswith("GO") or bool(score.get("PASS") and "verdict" not in score),
        "pass": bool(score.get("PASS")),
        "hard_fails": [NP_REPR_RE.sub(r"\1", str(x)) for x in _list(score.get("hard_fails"))],
        "badges": score_badges(root, score),
        "metrics": {
            "lufs": _num(score.get("LUFS_I")), "tp": _num(score.get("true_peak_dBTP")),
            "sub": _num(score.get("sub_S_over_M_dB")), "corr": _num(corr.get("full")),
            "draft": _num(score.get("auto_draft_overall")), "knock": _num(score.get("fair_knock100_250_median")),
            "ref_dist": (ref or {}).get("dist"), "tilt": (ref or {}).get("tilt"),
            "bpm16": bpms[1] if len(bpms) > 1 else None, "bpm64": bpms[2] if len(bpms) > 2 else None,
            "lra": _num(score.get("LRA")),
        },
        "draft_scores": {k: _num(v) for k, v in drafts.items()} if isinstance(drafts, dict) else None,
        "ref": ref,
        "ear": [str(x) for x in _list(score.get("EAR"))],
        "duration_s": _num(score.get("duration_s")),
    }


def card_score(card: Path) -> dict | None:
    f = card / "score" / "score.json"
    if not f.exists():
        return None
    return score_entry(card.name, f, card, "current")


def attempt_scores(card: Path) -> list[dict]:
    """Hand-archived earlier passes (attemptN/score/score.json or attemptN/score.json)."""
    out = []
    for d in sorted(card.glob("attempt*"), key=lambda p: (len(p.name), p.name)):
        if not d.is_dir():
            continue
        for f in (d / "score" / "score.json", d / "score.json"):
            if f.is_file():
                e = score_entry(card.name, f, d, d.name)
                if e:
                    out.append(e)
                break
    return out


# ---------------------------------------------------------------- handoff
def handoff_card(project: str, card_names: list[str]) -> str:
    """'e2e-001_tekno_165_Fmin' -> 'e2e-001' (longest known card that prefixes the project)."""
    for c in sorted(card_names, key=len, reverse=True):
        if project == c or project.startswith(c + "_"):
            return c
    return project.split("_tekno_")[0].split("_")[0]


def scan_handoff(handoff_dir: Path, card_names: list[str]) -> tuple[list[dict], list[dict], list[dict]]:
    """handoff.py packages: (packages, audio entries, score entries)."""
    packages, audio, scores = [], [], []
    if not handoff_dir or not handoff_dir.is_dir():
        return packages, audio, scores

    def mtime(p):
        try:
            return p.stat().st_mtime
        except OSError:  # dangling symlink, or a package renamed while handoff.py works
            return 0.0
    dirs = [d for d in handoff_dir.iterdir() if not d.name.startswith(".") and d.is_dir()]
    for d in sorted(dirs, key=mtime, reverse=True)[:40]:
        try:
            _scan_package(d, card_names, packages, audio, scores)
        except Exception as e:  # one broken package must not hide the others
            log.warning("handoff %s skipped: %s", d.name, e)
    return packages, audio, scores


def _scan_package(d: Path, card_names: list[str], packages: list, audio: list, scores: list) -> None:
    card = handoff_card(d.name, card_names)
    items = []
    for f in list(d.iterdir()) + (list((d / "stems").iterdir()) if (d / "stems").is_dir() else []):
        if f.is_file() and f.suffix.lower() in AUDIO_EXT:
            st = f.stat()
            stem = f.parent.name == "stems"
            items.append({"card": card, "root": "handoff", "project": d.name,
                          "path": f"{d.name}/{f.relative_to(d).as_posix()}", "name": f.stem,
                          "stage": "Handoff stem" if stem else "Handoff", "kind": "stem" if stem else "render",
                          "size": st.st_size, "mtime": st.st_mtime})
    sc = score_entry(card, d / "score.json", d, "handoff") if (d / "score.json").is_file() else None
    master = next((i for i in items if i["kind"] == "render"), None)
    note = ""
    try:
        note = (d / "HANDOFF.md").read_text(errors="replace")[:800]
    except OSError:
        pass
    pkg = {"project": d.name, "card": card, "mtime": d.stat().st_mtime,
           "master": master["path"] if master else None, "master_mtime": master["mtime"] if master else None,
           "stems": sum(1 for i in items if i["kind"] == "stem"),
           "verdict": sc["verdict"] if sc else None, "draft": (sc or {}).get("metrics", {}).get("draft"),
           "score_sha": sc["sha"] if sc else None, "note": note}
    # all or nothing per package
    audio.extend(items)
    if sc:
        scores.append(sc)
    packages.append(pkg)


# ---------------------------------------------------------------- whole scan
def scan(cards_dir: Path, handoff_dir: Path | None = None, max_cards: int = 40) -> dict:
    cards = list_cards(cards_dir)[:max_cards]
    audio, scores, history, lanes, info = [], [], [], {}, {}
    for c in cards:
        try:
            audio.extend(card_audio(c))
            s = card_score(c)
            hist = attempt_scores(c)
            if s:
                scores.append(s)
            history.extend(hist + ([s] if s else []))
            lanes[c.name] = files_lane(c)
            masters = [c / "polish" / n for n in ("master.wav", "master.flac")]
            info[c.name] = {
                "plan": plan_summary(c), "reports": stage_reports(c), "runs": run_logs(c), "mtime": card_mtime(c),
                "attempts": len(hist), "handoff": [],
                "master_mtime": max((m.stat().st_mtime for m in masters if m.exists()), default=None),
            }
        except Exception as e:  # one odd card (old score format, file vanishing mid-run) must not blank the rest
            log.warning("card %s skipped: %s", c.name, e)
            continue
    packages, h_audio, h_scores = scan_handoff(handoff_dir, [c.name for c in cards]) if handoff_dir else ([], [], [])
    audio.extend(h_audio)
    for p in packages:
        ci = info.get(p["card"])
        if ci is not None:
            # a package is stale when the card has a newer master than the one that was handed off
            p["stale"] = bool(ci["master_mtime"] and p["master_mtime"] and ci["master_mtime"] > p["master_mtime"] + 1)
            ci["handoff"].append(p)
    # history: one entry per distinct score.json content (handoff copies an attempt's score)
    by_sha: dict[str, dict] = {}
    for e in sorted(history + h_scores, key=lambda x: x["mtime"]):
        if e["sha"] in by_sha:
            if e["run"] not in by_sha[e["sha"]]["runs"]:
                by_sha[e["sha"]]["runs"].append(e["run"])
            continue
        by_sha[e["sha"]] = {"card": e["card"], "run": e["run"], "runs": [e["run"]], "sha": e["sha"], "mtime": e["mtime"],
                            "verdict": e["verdict"], "go": e.get("go"), "metrics": e["metrics"]}
    audio.sort(key=lambda x: -x["mtime"])
    scores.sort(key=lambda x: x["mtime"])
    return {"audio": audio, "scores": scores, "files_lane": lanes, "cards": info, "handoff": packages,
            "history": sorted(by_sha.values(), key=lambda x: x["mtime"])}


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
