#!/usr/bin/env python3
"""qwen-night: two unattended jobs for the local Qwen (approved by Ryan, 2026-10-11). Neither gives the model any tools.

  tag     Measure every sample in the library (duration, level, brightness, low end, attack, BPM and key for loops), let
          the CLAP audio model listen to it (zero-shot role and sound-character scores, plus an embedding for search),
          and ask Qwen, 20 at a time, for a role, a few tags from a fixed vocabulary and a short note. The runner, not
          the model, has the last word: a role hinted by the file path wins, then a confident CLAP role; a tag is kept
          only when the measurements or CLAP back it up; BPM and key always come from the measurements; notes lose any
          digits. Resumable; MAX_TAG per night. Without CLAP installed it tags from the measurements alone.
  find    Text search over the CLAP embeddings: `find "distorted gabber kick"`, or `find --like <path>` for samples
          that sound like one you have.
  digest  Gather the facts of the last 24 h (agents, Claude Code jobs, fallback runs, kanban, failed services, disk,
          local model, tagging progress), ask the model for a <= 5 line summary of what matters, and post the
          summary AND the raw facts to agent-mail thread "morning-report" as qwen. The facts are the record: if the
          model is down the facts go out alone.

  qwen_night.py tag [--limit N] [--dry-run]
  qwen_night.py find "<words>" | --like <library path> [-n 20]
  qwen_night.py digest [--dry-run]

Runs with the models venv python (numpy + soundfile; librosa for BPM/key; torch + transformers for CLAP).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_status  # noqa: E402
import team_mail  # noqa: E402

LIB = Path(os.environ.get("TEKNO_LIBRARY_DIR", "/mnt/agent-data/music_library"))
NIGHT = Path(os.environ.get("TEKNO_NIGHT_DIR", "/mnt/agent-data/tekno_machine/night"))
FB = Path(os.environ.get("TEKNO_FALLBACK_DIR", "/mnt/agent-data/tekno_machine/fallback"))
HANDOFF = Path(os.environ.get("TEKNO_HANDOFF_DIR", "/mnt/agent-data/tekno_machine/handoff"))
CC_JOBS = Path(os.environ.get("TEKNO_CC_JOBS_DIR", "/mnt/agent-data/apps/tekno-panel-next/cc_jobs"))
KANBAN_DB = Path(os.environ.get("TEKNO_KANBAN_DB", os.path.expanduser("~/.hermes/kanban/boards/tekno-machine/kanban.db")))
DATA_DISK = os.environ.get("TEKNO_DATA_DISK", "/mnt/agent-data")
LLM = os.environ.get("TEKNO_FALLBACK_LLM", "http://127.0.0.1:8001/v1")
MODEL = os.environ.get("TEKNO_FALLBACK_MODEL", "qwen3-coder")
MAX_TAG = int(os.environ.get("TEKNO_NIGHT_MAX_TAG", 1500))
BATCH = 20
CLAP_ID = os.environ.get("TEKNO_CLAP_MODEL", "laion/larger_clap_music")
CLAP_SR = 48000
CLAP_ROLE_P = 0.5   # CLAP must be this sure (softmax over the role prompts) to decide a role
CLAP_TAG_P = 0.25   # and this sure (softmax over the character prompts) to back up a tag
AUDIO = {".wav", ".aif", ".aiff", ".flac"}
MAX_BYTES = 40 * 1024 * 1024
DAY = 24 * 3600

ROLES = ("kick", "bass", "hat", "snare", "clap", "perc", "acid", "synth", "stab", "pad", "fx", "noise", "vocal", "loop",
         "other")
# path keyword -> role; the folder/file name a sample pack gave it beats the model's guess
HINTS = [(r"kick|\bbd\b|bassdrum", "kick"), (r"\bhh\b|hihat|hi-hat|\bhats?\b|cymbal|ride", "hat"),
         (r"snare|\bsd\b", "snare"), (r"clap", "clap"), (r"\b303\b|acid", "acid"), (r"bass", "bass"),
         (r"perc|tom|rim|shaker|conga", "perc"), (r"vox|vocal|voice|speech", "vocal"), (r"\bfx\b|sfx|riser|sweep|impact",
         "fx"), (r"noise", "noise"), (r"\bpad", "pad"), (r"stab|chord", "stab"), (r"synth|lead", "synth"),
         (r"loop", "loop")]
# tag -> test on the measurements; a tag is kept only when its test passes
TAGS = {
    "short": lambda m: m["dur_s"] < 0.6, "long": lambda m: m["dur_s"] > 2.0,
    "bright": lambda m: m["centroid_hz"] > 3000, "dark": lambda m: m["centroid_hz"] < 1500,
    "subby": lambda m: m["low_ratio"] > 0.45, "boomy": lambda m: m["low_ratio"] > 0.35,
    "rumble": lambda m: m["low_ratio"] > 0.35 and m["dur_s"] > 0.8,
    "airy": lambda m: m["high_ratio"] > 0.15, "metallic": lambda m: m["high_ratio"] > 0.1,
    "punchy": lambda m: m["crest_db"] > 12 and m["attack_ms"] < 30, "soft-attack": lambda m: m["attack_ms"] > 60,
    "distorted": lambda m: m["crest_db"] < 10, "gritty": lambda m: m["crest_db"] < 11, "hard": lambda m: m["crest_db"] < 12,
    "clean": lambda m: m["crest_db"] >= 10, "loud": lambda m: m["rms_db"] > -14, "quiet": lambda m: m["rms_db"] < -28,
    "tonal": lambda m: m.get("key") is not None, "loopable": lambda m: m["dur_s"] >= 1.5,
}
# what CLAP listens for; role prompts are mutually exclusive, character prompts back up tags of the same name
ROLE_PROMPTS = {"kick": "a kick drum hit", "bass": "a bass synth note", "hat": "a hi-hat or cymbal", "snare": "a snare drum hit",
                "clap": "a hand clap", "perc": "a percussion hit", "acid": "a squelchy acid 303 synth line",
                "synth": "a synth lead", "stab": "a rave synth stab chord", "pad": "a sustained synth pad",
                "fx": "a sound effect riser or impact", "noise": "white noise", "vocal": "a human voice",
                "loop": "a full drum loop"}
CLAP_TAGS = {"distorted": "a heavily distorted overdriven sound", "metallic": "a metallic industrial clang",
             "acid": "a resonant squelchy acid sound", "gabber": "a hard distorted gabber kick",
             "industrial": "dark industrial noise", "rave": "an old school rave hoover or stab",
             "tribal": "tribal hand percussion", "reverb": "a sound drenched in reverb", "clean": "a clean dry sound",
             "dark": "a dark gloomy sound", "bright": "a bright sharp sound"}
TAGS.update({t: (lambda m: False) for t in CLAP_TAGS if t not in TAGS})  # CLAP-only tags: kept only on CLAP's word
ROLE_CHECK = {"kick": lambda m: m["low_ratio"] > 0.2 and m["dur_s"] < 3, "bass": lambda m: m["low_ratio"] > 0.25,
              "hat": lambda m: m["centroid_hz"] > 3500, "loop": lambda m: m["dur_s"] >= 1.5}
KEYS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

TAG_SYSTEM = """You label drum and synth samples for a free-party tekno producer. For each sample you get its file path and
measurements. Answer ONLY with a JSON object {"items": [{"id": <id>, "role": <role>, "tags": [..], "note": "<max 10 words>"}]}.
role is one of: %s. tags come only from: %s (pick 1-4 that the measurements or the clap_* fields support;
clap_role and clap_tags are what an audio model heard).
The note describes the sound in plain words (no numbers, no opinions about quality)."""
DIGEST_SYSTEM = """You write the morning report for a music production machine. You get a list of facts from the last 24
hours. Write at most 5 short lines, most important first: what failed or is stuck, what finished, what waits on Ryan.
Only state what the facts say. No greetings, no advice beyond the facts, no numbers that are not in the facts."""


# ---------------------------------------------------------------- shared
def chat(system: str, user: str, max_tokens: int = 2048) -> str:
    body = {"model": MODEL, "temperature": 0.1, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    req = urllib.request.Request(LLM + "/chat/completions", json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())["choices"][0]["message"].get("content") or ""


def roster(fn: str, *args) -> None:
    try:
        getattr(agent_status, fn)("qwen-night", *args)
    except (ValueError, OSError):
        pass


def mail(body: str, thread: str) -> bool:
    try:
        return team_mail.main(["send", "--as", "qwen", "--thread", thread, body]) in (0, None)
    except SystemExit as e:
        return e.code in (0, None)
    except Exception:  # noqa: BLE001 - agent-mail down: the caller records that the post failed
        return False


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def save_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
    tmp.replace(p)


# ---------------------------------------------------------------- CLAP
class Clap:
    """laion CLAP via transformers, CPU. Loaded once per run; text prompts are embedded once."""

    def __init__(self):
        import torch
        from transformers import ClapModel, ClapProcessor
        self.torch = torch
        self.model = ClapModel.from_pretrained(CLAP_ID).eval()
        self.proc = ClapProcessor.from_pretrained(CLAP_ID)
        self.scale = float(self.model.logit_scale_a.exp())
        self.roles = list(ROLE_PROMPTS)
        self.role_txt = self.text(list(ROLE_PROMPTS.values()))
        self.tags = list(CLAP_TAGS)
        self.tag_txt = self.text(list(CLAP_TAGS.values()))

    def text(self, prompts: list[str]):
        import numpy as np
        with self.torch.no_grad():
            e = self.model.get_text_features(**self.proc(text=prompts, return_tensors="pt", padding=True)).numpy()
        return e / np.linalg.norm(e, axis=1, keepdims=True)

    def audio(self, clips: list):
        import numpy as np
        with self.torch.no_grad():
            e = self.model.get_audio_features(**self.proc(audios=clips, sampling_rate=CLAP_SR, return_tensors="pt")).numpy()
        return e / np.linalg.norm(e, axis=1, keepdims=True)

    def scores(self, emb, txt) -> list[list[float]]:
        import numpy as np
        z = self.scale * emb @ txt.T
        z = np.exp(z - z.max(axis=1, keepdims=True))
        return (z / z.sum(axis=1, keepdims=True)).tolist()

    def listen(self, clips: list) -> tuple[list[dict], object]:
        """Per clip: the most likely role with its probability and the character tags CLAP backs up."""
        emb = self.audio(clips)
        out = []
        for rp, tp in zip(self.scores(emb, self.role_txt), self.scores(emb, self.tag_txt)):
            best = max(range(len(rp)), key=rp.__getitem__)
            out.append({"clap_role": self.roles[best], "clap_role_p": round(rp[best], 2),
                        "clap_tags": [t for t, p in sorted(zip(self.tags, tp), key=lambda x: -x[1]) if p >= CLAP_TAG_P][:2]})
        return out, emb


def clap_or_none(log=None):
    try:
        return Clap()
    except Exception as e:  # noqa: BLE001 - no CLAP: tag from the measurements alone
        if log:
            log.write(f"CLAP unavailable ({type(e).__name__}: {e}); measurements only\n")
        return None


def clip48(path: Path):
    """The first 10 s, mono, at CLAP's 48 kHz."""
    import numpy as np
    import soundfile as sf
    y, sr = sf.read(str(path), always_2d=True, dtype="float32", frames=sr_frames(path, 10))
    y = y.mean(axis=1)
    if sr != CLAP_SR:
        import librosa
        y = librosa.resample(y, orig_sr=sr, target_sr=CLAP_SR)
    return np.asarray(y, dtype="float32")


def sr_frames(path: Path, seconds: float) -> int:
    import soundfile as sf
    return int(sf.info(str(path)).samplerate * seconds)


def save_emb(rels: list[str], emb) -> None:
    """Append to night/clap/emb.npy + paths.json (same order)."""
    import numpy as np
    d = NIGHT / "clap"
    d.mkdir(parents=True, exist_ok=True)
    paths = load_json(d / "paths.json", [])
    old = np.load(d / "emb.npy") if (d / "emb.npy").exists() and paths else np.zeros((0, emb.shape[1]), "float32")
    have = set(paths)  # a crash between this and tags.json re-embeds a batch; keep each path once
    keep = [i for i, r in enumerate(rels) if r not in have]
    np.save(d / "emb.npy", np.vstack([old, emb[keep].astype("float32")]))
    save_json(d / "paths.json", paths + [rels[i] for i in keep])


def find(words: str | None, like: str | None, n: int) -> int:
    import numpy as np
    d = NIGHT / "clap"
    paths = load_json(d / "paths.json", [])
    if not paths:
        print("no CLAP index yet: run `qwen_night.py tag` first")
        return 1
    emb = np.load(d / "emb.npy")
    if like:
        if like not in paths:
            print(f"{like} is not in the index")
            return 1
        q = emb[paths.index(like)]
    else:
        clap = Clap()
        q = clap.text([words])[0]
    db = load_json(NIGHT / "tags.json", {})
    order = np.argsort(-(emb @ q))
    shown = 0
    for i in order:
        if paths[i] == like:
            continue
        t = db.get(paths[i], {})
        print(f"{float(emb[i] @ q):.3f}  {paths[i]}  [{t.get('role', '?')}: {', '.join(t.get('tags', []))}]")
        shown += 1
        if shown >= n:
            break
    return 0


# ---------------------------------------------------------------- tag
def measure(path: Path) -> dict:
    import numpy as np
    import soundfile as sf

    y, sr = sf.read(str(path), always_2d=True, dtype="float32")
    y = y.mean(axis=1)
    dur = len(y) / sr
    if not len(y) or not np.any(y):
        raise ValueError("silent or empty")
    peak = float(np.max(np.abs(y)))
    rms = float(np.sqrt(np.mean(y ** 2)))
    seg = y[: int(sr * 4)]  # spectrum of the first 4 s is enough for a one-shot or a loop's character
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) ** 2
    freqs = np.fft.rfftfreq(len(seg), 1 / sr)
    tot = float(spec.sum()) or 1.0
    m = {"dur_s": round(dur, 3), "peak_db": round(20 * np.log10(peak), 1), "rms_db": round(20 * np.log10(max(rms, 1e-9)), 1),
         "crest_db": round(20 * np.log10(peak / max(rms, 1e-9)), 1),
         "centroid_hz": round(float((freqs * spec).sum() / tot)), "low_ratio": round(float(spec[freqs < 150].sum() / tot), 3),
         "high_ratio": round(float(spec[freqs > 6000].sum() / tot), 3),
         "attack_ms": round(1000 * int(np.argmax(np.abs(y[: int(sr * 0.5)]))) / sr, 1), "bpm": None, "key": None}
    if dur >= 2.0:
        try:
            import librosa
            seg = y[: int(sr * 30)]
            tempo = librosa.beat.beat_track(y=seg, sr=sr)[0]
            bpm = float(np.atleast_1d(tempo)[0])
            m["bpm"] = round(bpm * 2 if bpm < 100 else bpm, 1) if bpm > 0 else None  # tekno lives above 100
            chroma = librosa.feature.chroma_cqt(y=seg, sr=sr).mean(axis=1)
            if chroma.max() > 1.5 * np.median(chroma):  # a clear pitch centre, not noise or drums
                m["key"] = KEYS[int(np.argmax(chroma))]
        except Exception:  # noqa: BLE001 - BPM/key are a bonus; the rest still counts
            pass
    return m


def role_hint(rel: str) -> str | None:
    """Role from the folders and file name below the pack folder (a pack called "... Loops" or "Acid Techno" says
    nothing about each file in it)."""
    parts = Path(rel).parts
    low = "/".join(parts[1:] if len(parts) > 1 else parts).lower().replace("_", " ")
    for pat, role in HINTS:
        if re.search(pat, low):
            return role
    return None


def check(rel: str, m: dict, answer: dict) -> tuple[dict, list[str]]:
    """The runner's verdict on one model answer: the tags the measurements back up, and what was dropped."""
    dropped = []
    hint = role_hint(rel)
    role = answer.get("role") if answer.get("role") in ROLES else "other"
    if hint:
        if role != hint:
            dropped.append(f"role {role}->{hint} (path)")
        role = hint
    elif m.get("clap_role") in ROLES and m.get("clap_role_p", 0) >= CLAP_ROLE_P:
        if role != m["clap_role"]:
            dropped.append(f"role {role}->{m['clap_role']} (clap)")
        role = m["clap_role"]
    elif role in ROLE_CHECK and not ROLE_CHECK[role](m):
        dropped.append(f"role {role} (measurements)")
        role = "other"
    tags = []
    heard = set(m.get("clap_tags") or [])
    for t in answer.get("tags") or []:
        t = str(t).lower().strip()
        if t in TAGS and (TAGS[t](m) or t in heard) and t not in tags:
            tags.append(t)
        else:
            dropped.append(f"tag {t}")
    note = re.sub(r"\d+", "", " ".join(str(answer.get("note", "")).split()))[:80].strip(" ,.-")
    return {"role": role, "tags": tags[:4], "note": note}, dropped


def library_files() -> list[Path]:
    out = []
    for p in LIB.rglob("*"):
        if p.suffix.lower() in AUDIO and p.is_file():
            try:
                if p.stat().st_size <= MAX_BYTES:
                    out.append(p)
            except OSError:
                pass
    return sorted(out)


def parse_items(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    items = json.loads(m.group(0)).get("items", []) if m else []
    return {int(i["id"]): i for i in items if isinstance(i, dict) and str(i.get("id", "")).isdigit()}


def tag(limit: int, dry: bool) -> int:
    db_path = NIGHT / "tags.json"
    db = load_json(db_path, {})
    todo = [p for p in library_files() if str(p.relative_to(LIB)) not in db][:limit]
    print(f"{len(db)} tagged, {len(todo)} to do tonight")
    if dry or not todo:
        return 0
    roster("start", "qwen", "library", f"tag {len(todo)} samples")
    system = TAG_SYSTEM % (", ".join(ROLES), ", ".join(TAGS))
    stats = {"tagged": 0, "unreadable": 0, "model_errors": 0, "dropped": 0}
    log = (NIGHT / "runs").joinpath(time.strftime("%Y%m%d-%H%M%S") + "-tag.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w") as lf:
        clap = clap_or_none(lf)
        stats["clap"] = clap is not None
        for i in range(0, len(todo), BATCH):
            batch = []
            for p in todo[i:i + BATCH]:
                rel = str(p.relative_to(LIB))
                try:
                    batch.append((rel, measure(p)))
                except Exception as e:  # noqa: BLE001 - one broken file never stops the night
                    stats["unreadable"] += 1
                    db[rel] = {"error": f"{type(e).__name__}: {e}"[:120], "at": int(time.time())}
            if not batch:
                continue
            if clap:
                try:
                    heard, emb = clap.listen([clip48(LIB / rel) for rel, _ in batch])
                    for (rel, m), h in zip(batch, heard):
                        m.update(h)
                    save_emb([rel for rel, _ in batch], emb)
                except Exception as e:  # noqa: BLE001 - this batch goes without CLAP
                    lf.write(f"batch {i}: CLAP error {type(e).__name__}: {e}\n")
            listing = "\n".join(json.dumps({"id": n, "path": rel, **{k: v for k, v in m.items() if v is not None}})
                                for n, (rel, m) in enumerate(batch))
            try:
                answers = parse_items(chat(system, listing))
            except Exception as e:  # noqa: BLE001
                stats["model_errors"] += 1
                lf.write(f"batch {i}: model error {type(e).__name__}: {e}\n")
                answers = {}
            for n, (rel, m) in enumerate(batch):
                verdict, dropped = check(rel, m, answers.get(n, {}))
                stats["dropped"] += len(dropped)
                db[rel] = {**m, **verdict, "by": MODEL if n in answers else "runner", "at": int(time.time())}
                stats["tagged"] += 1
                if dropped:
                    lf.write(f"{rel}: dropped {', '.join(dropped)}\n")
            save_json(db_path, db)
            roster("step", f"tagged {stats['tagged']}/{len(todo)}")
        lf.write(json.dumps(stats) + "\n")
    save_json(NIGHT / "tag_last.json", {**stats, "at": int(time.time()), "total": len(db), "log": str(log)})
    roster("done", "ok" if not stats["model_errors"] else "failed",
           f"tagged {stats['tagged']}, unreadable {stats['unreadable']}, model errors {stats['model_errors']}")
    return 0


# ---------------------------------------------------------------- digest
def ago(ts: float, now: float) -> str:
    h = (now - ts) / 3600
    return f"{h:.0f} h ago" if h >= 1 else f"{(now - ts) / 60:.0f} min ago"


def job_running(name: str) -> bool:
    for cmd in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            if f"run_job.sh\0{name}\0" in cmd.read_text(errors="replace"):
                return True
        except OSError:
            pass
    return False


def last_line(p: Path) -> str:
    try:
        lines = [x for x in p.read_text(errors="replace").splitlines() if x.strip()]
    except OSError:
        return ""
    return " ".join(lines[-1].split())[:140] if lines else "(empty)"


def facts(now: float | None = None) -> list[str]:
    now = now or time.time()
    out = []
    for a in agent_status.roster(now=now):
        if a["state"] == "working" or now - a["updated"] < DAY:
            st = "no update for 20+ min" if a.get("stale") else a["state"]
            out.append(f"agent {a['name']} ({a.get('model') or '?'}): {st}: {a.get('step') or a.get('result') or a.get('task') or ''}")
    for log in sorted(CC_JOBS.glob("*.log")):
        if now - log.stat().st_mtime < DAY:
            if job_running(log.stem):
                out.append(f"Claude Code job {log.stem}: running (its log is written when it ends)")
            else:
                out.append(f"Claude Code job {log.stem} ({ago(log.stat().st_mtime, now)}): {last_line(log)}")
    for note in sorted((FB / "handoff").glob("*.md")):
        st = (re.search(r"^STATUS:\s*(\S+)", note.read_text(errors="replace"), re.M) or [None, "?"])[1]
        if st != "done":
            out.append(f"fallback hand-off note {note.stem}: {st}")
    runs = [r for r in (FB / "runs").glob("*.log") if now - r.stat().st_mtime < DAY]
    if runs:
        out.append(f"fallback runs in the last 24 h: {len(runs)} ({', '.join(sorted(r.stem for r in runs)[-3:])})")
    for d in sorted(HANDOFF.glob("*")):
        if d.is_dir() and now - d.stat().st_mtime < DAY:
            out.append(f"delivery folder {d.name} updated {ago(d.stat().st_mtime, now)}")
    out += kanban_facts()
    try:
        r = subprocess.run(["systemctl", "--user", "--failed", "--plain", "--no-legend"], capture_output=True, text=True, timeout=20)
        failed = [ln.split()[0] for ln in r.stdout.splitlines() if ln.strip()]
        if r.returncode:
            out.append(f"failed services: unknown (systemctl: {' '.join(r.stderr.split())[:80]})")
        else:
            out.append(f"failed services: {', '.join(failed)}" if failed else "failed services: none")
    except (OSError, subprocess.SubprocessError):
        pass
    for label, path in (("root disk", "/"), ("data disk", DATA_DISK)):
        try:
            u = shutil.disk_usage(path)
            out.append(f"{label}: {u.free / 2**30:.1f} GB free ({100 * u.used / (u.used + u.free):.0f}% used)")  # as df
        except OSError:
            pass
    try:
        with urllib.request.urlopen(LLM + "/models", timeout=5):
            out.append("local model: answering")
    except OSError:
        out.append("local model: NOT answering")
    t = load_json(NIGHT / "tag_last.json", None)
    if t:
        out.append(f"sample tagging: {t.get('tagged', 0)} tagged last night ({t.get('total', 0)} total), "
                   f"unreadable {t.get('unreadable', 0)}, model errors {t.get('model_errors', 0)}")
    return out


def kanban_facts() -> list[str]:
    if not KANBAN_DB.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{KANBAN_DB}?mode=ro", uri=True, timeout=2.0)
        rows = con.execute("select title, status from tasks where status not in ('archived', 'done')").fetchall()
        con.close()
    except sqlite3.Error as e:
        return [f"kanban: unreadable ({e})"]
    out = [f"kanban card '{t}' is {s}" for t, s in rows if s in ("review", "blocked", "failed")]
    counts = {}
    for _, s in rows:
        counts[s] = counts.get(s, 0) + 1
    out.append("kanban open cards: " + (", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "none"))
    return out


def digest(dry: bool) -> int:
    fl = facts()
    try:
        summary = chat(DIGEST_SYSTEM, "\n".join(f"- {f}" for f in fl), max_tokens=400).strip()
        summary = "\n".join(summary.splitlines()[:5])
    except Exception as e:  # noqa: BLE001 - the facts go out anyway
        summary = f"(local model unavailable: {type(e).__name__}; facts only)"
    body = f"Morning report ({time.strftime('%a %d %b %H:%M')} UTC)\n\n{summary}\n\nFacts:\n" + "\n".join(f"- {f}" for f in fl)
    if dry:
        print(body)
        return 0
    roster("start", "qwen", "report", "morning report")
    ok = mail(body, "morning-report")
    roster("done", "ok" if ok else "failed", "morning report posted" if ok else "morning report: mail failed")
    save_json(NIGHT / "digest_last.json", {"at": int(time.time()), "body": body, "sent": ok})
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="job", required=True)
    t = sub.add_parser("tag")
    t.add_argument("--limit", type=int, default=MAX_TAG)
    t.add_argument("--dry-run", action="store_true")
    f = sub.add_parser("find")
    f.add_argument("words", nargs="?")
    f.add_argument("--like")
    f.add_argument("-n", type=int, default=20)
    d = sub.add_parser("digest")
    d.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.job == "tag":
        return tag(a.limit, a.dry_run)
    if a.job == "find":
        if not (a.words or a.like):
            ap.error("find needs words or --like")
        return find(a.words, a.like, a.n)
    return digest(a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
