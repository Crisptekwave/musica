"""Demo mode: a fake but lively Tekno Machine so the design can be seen before real data flows.

Everything is derived from the wall clock, so every client sees the same story and
tracks visibly move through the lanes, loop back, get scored and land in Review.
"""
from __future__ import annotations

import io
import math
import random
import struct
import time
import wave
from functools import lru_cache

from .config import LANES

SLUGS = ["acid-rave-017", "squat-303-018", "bunker-f-019", "teknival-020", "sound-sys-021",
         "free-party-022", "raw-kick-023"]
AGENTS = ["Grok Bot", "Mix Judge", "Claude", "Hermes"]
PIPE = LANES[:-1]  # Plan..Review
STEP_S = 9  # seconds per lane in the demo


def _rng(*key) -> random.Random:
    return random.Random("|".join(map(str, key)))


def _track(i: int, slug: str, now: float) -> dict:
    period = STEP_S * (len(PIPE) + 3)
    t = (now + i * period / len(SLUGS) * 1.7) % period
    pos = int(t // STEP_S)
    cycle = int((now + i * period / len(SLUGS) * 1.7) // period)
    r = _rng(slug, cycle)
    loops = r.choice([0, 0, 1, 1, 2, 3])
    sendbacks = []
    for k in range(loops):
        to = r.choice(["Mix", "Polish", "Filter", "Edit", "Generate"])
        reason = {
            "Mix": "kick buried under bass, knock100-250 -4.1 dB (want >= +3)",
            "Polish": f"LUFS {r.uniform(-10.4, -9.6):.1f} not -9 ±0.5",
            "Filter": "mid/high stereo decorrelated, corr 250-12k -0.05",
            "Edit": "monotony: no change for 48 bars",
            "Generate": "key drifted to F major (0.41 vs 0.33)",
        }[to]
        sendbacks.append({"from": "Score", "to": to, "reason": reason, "task_id": f"t_demo{i}{k:02d}"})
    if pos >= len(PIPE):
        lane, state = "Done", "done"
    else:
        lane = PIPE[pos]
        state = "review" if lane == "Review" else ("failed" if r.random() < 0.06 else "working")
    frac = (t % STEP_S) / STEP_S
    idx = LANES.index(lane)
    progress = 100 if lane == "Done" else round(100 * (idx + (frac if state == "working" else 0)) / (len(LANES) - 1))
    tid = f"t_{abs(hash((slug, cycle, lane))) % 16**8:08x}"
    return {
        "slug": slug, "lane": lane, "state": state, "progress": progress, "loops": loops,
        "sendbacks": sendbacks, "last_sendback": sendbacks[-1] if sendbacks else None,
        "current_task": {"id": tid, "title": f"[{slug}] {lane.upper()}", "status": "review" if lane == "Review" else "running",
                         "assignee": f"tekno-{lane.lower().replace(' ', '-')}", "last_error": None},
        "review_task_id": tid if lane == "Review" else None,
        "tasks": [{"id": f"t_{abs(hash((slug, cycle, ln))) % 16**8:08x}", "lane": ln,
                   "status": "done" if k < pos else ("review" if ln == "Review" else ("blocked" if state == "failed" else "running")),
                   "title": f"[{slug}] {ln.upper()}"} for k, ln in enumerate(PIPE[:pos + 1])],
        "created_at": int(now - t), "last_activity": int(now - (t % STEP_S)),
        "age_s": int(t), "files_lane": lane if lane != "Done" else "Review", "lane_frac": round(frac, 3),
    }


def tracks(now: float) -> list[dict]:
    return sorted((_track(i, s, now) for i, s in enumerate(SLUGS)), key=lambda x: -x["progress"])


def lanes(trs: list[dict], now: float) -> dict:
    out = {}
    for lane in PIPE:
        here = [t for t in trs if t["lane"] == lane]
        st = "idle"
        if any(t["state"] == "failed" for t in here):
            st = "failed"
        elif any(t["state"] == "working" for t in here):
            st = "working"
        elif here and lane == "Review":
            st = "waiting"
        handoff = None
        for t in here:
            if t["lane_frac"] < 0.12:  # just arrived: previous lane handed off
                handoff = t
        task = here[0] if here else None
        out[lane] = {
            "lane": lane, "state": st,
            "task": {"id": task["current_task"]["id"], "title": task["current_task"]["title"],
                     "assignee": task["current_task"]["assignee"]} if task else None,
            "log": [{"at": now - 30 + j * 5, "text": line} for j, line in enumerate(_lane_log(lane, task, now))],
            "handoff_to": None, "handoff_at": None,
        }
        if handoff and PIPE.index(lane) > 0:
            prev = PIPE[PIPE.index(lane) - 1]
            out.setdefault(prev, {})
            out[prev]["handoff_to"] = lane
            out[prev]["handoff_at"] = now - handoff["lane_frac"] * STEP_S
    return out


def _lane_log(lane: str, task: dict | None, now: float) -> list[str]:
    if not task:
        return ["sleeping… no card in this lane"]
    s = task["slug"]
    r = _rng(lane, s, int(now // 3))
    lines = {
        "Plan": [f"study/brief.json: 165 BPM, F minor, LoRA trigger hermestek", "plan.json: 6 sections, 126 bars"],
        "Generate": [f"ace_job.py: seed {r.randint(10**8, 10**9)}, LoRA v3ep30 @1.0", f"diffusion step {r.randint(5, 50)}/50"],
        "Stems": ["demucs htdemucs_ft: drums/bass/other/vocals", f"vocals residual {r.uniform(-48, -38):.1f} dB"],
        "Sound Swap": ["kick one-shot swapped (43 Hz body)", "bass ducked on kick, 4 ms attack"],
        "Edit": ["bar-grid cut at 18.0 ms, no stretch", "126 bars, change every 16"],
        "Filter": ["HPF 25 Hz, DC removed", f"sub S/M {r.uniform(-56, -40):.1f} dB"],
        "Variation": ["riser fx at bar 64, filter sweep on acid", "variation report.json written"],
        "Mix": [f"fair.py sub dominance +{r.uniform(2, 12):.1f} dB", f"knock100-250 {r.uniform(-4, 4):+.1f} dB"],
        "Polish": ["clipper -> limiter chain v09", f"max GR {r.uniform(0.6, 5.5):.1f} dB"],
        "Master": [f"master.wav: {r.uniform(-9.4, -8.6):.1f} LUFS, {r.uniform(-1.4, -1.0):.1f} dBTP", "24-bit 44.1k"],
        "Score": ["score_card.py: qc_gate + fair + key", f"auto-draft {r.uniform(5.5, 8.2):.1f}/10"],
        "Review": ["waiting for a human ear", "Mix Judge verdict attached"],
    }[lane]
    return [f"[{s}] {ln}" for ln in lines]


def scores(now: float) -> list[dict]:
    out = []
    for k in range(14):
        r = _rng("score", k)
        lufs, tp = r.uniform(-10.2, -8.6), r.uniform(-1.6, -0.6)
        sub, corr = r.uniform(-58, -24), r.uniform(-0.1, 0.99)
        judge, ear = r.uniform(5.2, 8.6), r.uniform(5.0, 9.0)
        bpm = r.choice([165.0, 165.02, 168.0, 170.0, 172.01, 175.0])
        mono_ok = r.random() > 0.2

        def b(ok, v, label, tgt):
            return {"label": label, "value": v, "target": tgt, "status": "pass" if ok else "fail"}

        badges = [
            b(True, bpm, "BPM", "160-180 const"), b(True, "F minor", "Key", "F minor"),
            b(abs(lufs + 9) <= 0.5, round(lufs, 1), "LUFS-I", "-9 ±0.5"),
            b(tp <= -1.0, round(tp, 1), "True peak", "<= -1.0 dBTP"),
            b(sub <= -30, round(sub, 1), "Sub mono S/M", "<= -30 dB"),
            b(corr >= 0, round(corr, 2), "Stereo corr", ">= 0 (250-12k)"),
            b(mono_ok, "max 16 bars" if mono_ok else "48 bars flat", "Monotony", "change <= 32 bars"),
            b(judge >= 6.5, f"{judge:.1f} {'GO' if judge >= 6.5 else 'NO-GO'}", "Mix Judge", ">= 6.5"),
            b(ear >= 6.5, round(ear, 1), "EarLoop", ">= 6.5"),
        ]
        ok = all(x["status"] == "pass" for x in badges)
        out.append({
            "card": SLUGS[k % len(SLUGS)].rsplit("-", 1)[0] + f"-{k + 4:03d}", "mtime": now - (14 - k) * 3600 * 2,
            "verdict": "GO" if ok else "NO-GO", "pass": ok,
            "hard_fails": [f"{x['label']} {x['value']}" for x in badges if x["status"] == "fail"],
            "badges": badges,
            "metrics": {"lufs": round(lufs, 2), "tp": round(tp, 2), "sub": round(sub, 1), "corr": round(corr, 2),
                        "draft": round(judge, 1), "earloop": round(ear, 1)},
            "ear": ["acid identity/evolution", "kick punch vs tick-thump", "pumping"],
            "duration_s": 185.0,
        })
    return out


def audio(now: float) -> list[dict]:
    items = []
    base = now - (now % 40)
    for j, (name, stage, kind) in enumerate([
        ("master", "Master", "render"), ("premaster", "Mix", "render"), ("take_190013209", "Generate", "render"),
        ("kick", "Sound Swap", "stem"), ("bass", "Stem", "stem"), ("acid", "Stem", "stem"),
        ("drums", "Stem", "stem"),
    ]):
        items.append({"card": "acid-rave-017", "path": f"{name}.wav", "name": name, "stage": stage, "kind": kind,
                      "size": 0, "mtime": base - j * 300, "url": f"/demo-audio/{name}.wav"})
    return items


def health(now: float, trs: list[dict]) -> dict:
    gen = next((t for t in trs if t["lane"] == "Generate"), None)
    util = 92 + 6 * math.sin(now / 3) if gen else 4 + 3 * math.sin(now)
    return {
        "gpu": {"ok": True, "name": "NVIDIA L4", "util": round(util), "mem_used": 20566 + (900 if gen else 0) +
                round(120 * math.sin(now / 5)), "mem_total": 23034, "temp": round(58 + (14 if gen else 0) + 2 * math.sin(now / 7)),
                "power": round(38 + (32 if gen else 0) + 3 * math.sin(now))},
        "disk": {"ok": True, "path": "/mnt/agent-data", "free_gb": 34.2, "total_gb": 196.0, "used_pct": 82.6,
                 "warn": False, "warn_gb": 20.0},
        "services": [
            {"name": "acestep-api", "status": "up", "via": "http"},
            {"name": "acestep-lora", "status": "up", "via": "lora loaded"},
            {"name": "hermes-gateway", "status": "up", "via": "systemd:hermes-gateway"},
            {"name": "litellm", "status": "up" if int(now // 60) % 7 else "failed", "via": "systemd:aihub-litellm"},
        ],
        "ace": {"ok": True, "status": "generating" if gen else "idle",
                "job": {"pid": 4242, "card": gen["slug"], "since": now - 40} if gen else None,
                "progress": round(gen["lane_frac"] * 100) if gen else None,
                "log": [f"diffusion step {int((gen or {}).get('lane_frac', 0) * 50)}/50"] if gen else ["idle"]},
        "load": {"1m": 2.1, "5m": 1.8, "15m": 1.6, "cpus": 8},
        "tracks_today": 1 + int((now % 86400) // 14400), "tracks_cap": 6,
    }


def chat(now: float) -> dict:
    lines = [
        ("Grok Bot", "Backlog refilled: 3 ideas from the Study list. {s} goes first."),
        ("Hermes", "Study brief for {s}: 165 BPM, F minor, rolling offbeat bass, no bells."),
        ("Grok Bot", "ACE take ready for {s} (seed 190013209, 159 s on the L4)."),
        ("Claude", "Took the Edit card for {s}: 126 bars, cut on the bar grid, no stretch."),
        ("Mix Judge", "{s} Score: kick buried under bass, knock100-250 -4.1 dB. Sending back to Mix."),
        ("Claude", "Mix v2 for {s}: kick +2 dB at 60 Hz, bass sidechain 4 ms. Knock now +3.4 dB."),
        ("Mix Judge", "{s} overall 7.1, no hard fail. GO from me."),
        ("Grok Bot", "{s} is in Review. @Hermes your ear?"),
        ("Hermes", "LUFS -9.1, TP -1.2. Approve once the acid rolls a bit more."),
    ]
    msgs = []
    tick = int(now // 6)
    for k in range(tick - 24, tick + 1):
        who, txt = lines[k % len(lines)]
        s = SLUGS[(k // len(lines)) % len(SLUGS)]
        msgs.append({"id": k, "thread": f"card-{s}", "from": who, "to": [], "subject": f"card-{s}",
                     "body": txt.format(s=s), "ts": now - (tick - k) * 6, "importance": "normal"})
    return {"status": "ok", "error": None, "agents": [{"name": a, "program": a.lower().replace(" ", "-")} for a in AGENTS],
            "messages": msgs}


def snapshot(now: float | None = None) -> dict:
    now = now or time.time()
    trs = tracks(now)
    return {
        "mode": "demo", "ts": now, "tracks": trs, "lanes": lanes(trs, now), "audio": audio(now),
        "reference": {"name": "SIFRES v02 (demo)", "url": "/demo-audio/reference.wav"},
        "scores": scores(now), "health": health(now, trs), "chat": chat(now),
        "sources": {"kanban": {"ok": True, "error": None}, "files": {"ok": True}, "mail": {"ok": True}},
    }


# ---------- synthetic audio for the demo player ----------
SR, BPM = 22050, 165.0


@lru_cache(maxsize=16)
def demo_wav(name: str) -> bytes:
    beat = 60.0 / BPM
    bars = 8
    n = int(SR * beat * 4 * bars)
    buf = [0.0] * n
    parts = {"master": ("kick", "bass", "acid", "hat"), "premaster": ("kick", "bass", "acid", "hat"),
             "take_190013209": ("kick", "bass", "acid"), "kick": ("kick",), "bass": ("bass",),
             "acid": ("acid",), "drums": ("kick", "hat"), "reference": ("kick", "bass", "hat", "acid2")}
    use = parts.get(name, ("kick",))
    rnd = random.Random(7)
    if "kick" in use:
        kl = int(SR * 0.28)
        for b in range(bars * 4):
            s0 = int(b * beat * SR)
            ph = 0.0
            for i in range(min(kl, n - s0)):
                t = i / SR
                f = 45 + 160 * math.exp(-t * 38)
                ph += 2 * math.pi * f / SR
                buf[s0 + i] += math.tanh(3.2 * math.sin(ph) * math.exp(-t * 9)) * 0.75
    if "bass" in use:
        f0 = 43.65  # F1
        for b in range(bars * 4):
            s0 = int((b + 0.5) * beat * SR)
            L = int(beat * 0.45 * SR)
            for i in range(min(L, n - s0)):
                t = i / SR
                saw = 2 * ((f0 * t) % 1) - 1
                buf[s0 + i] += math.tanh(2 * saw) * 0.32 * math.exp(-t * 7)
    if "hat" in use:
        for b in range(bars * 4):
            s0 = int((b + 0.5) * beat * SR)
            for i in range(min(int(0.04 * SR), n - s0)):
                buf[s0 + i] += (rnd.random() * 2 - 1) * 0.12 * math.exp(-i / SR * 90)
    for key in ("acid", "acid2"):
        if key not in use:
            continue
        notes = [0, 0, 12, 0, 3, 0, 15, 7, 0, 12, 0, 10, 0, 3, 5, 0] if key == "acid" else [0, 7, 0, 12, 0, 5, 3, 0] * 2
        step = beat / 4
        lp = bp = 0.0
        for s in range(bars * 16):
            f = 87.3 * 2 ** (notes[s % 16] / 12)
            s0 = int(s * step * SR)
            L = int(step * SR)
            sweep = 0.5 + 0.5 * math.sin(2 * math.pi * s / 64)
            for i in range(min(L, n - s0)):
                t = i / SR
                saw = 2 * ((f * t) % 1) - 1
                cutoff = 200 + 2400 * sweep * math.exp(-t * 18)
                fc = 2 * math.sin(math.pi * min(cutoff, SR / 6) / SR)
                hp = saw - lp - 0.25 * bp  # resonant state-variable filter
                bp += fc * hp
                lp += fc * bp
                buf[s0 + i] += math.tanh(1.8 * lp) * 0.22
    peak = max(1e-9, max(abs(x) for x in buf))
    pcm = b"".join(struct.pack("<h", int(32767 * 0.89 * x / peak)) for x in buf)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)
    return out.getvalue()
