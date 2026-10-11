"""ACE-Step activity from its log file and the systemd journal (both read-only).

The ACE units run as user units of another account and write their program
output to ``acestep.log`` (loguru lines plus tqdm bars split by ``\\r``). The
journal only carries their lifecycle (Started/Finished/Stopped...), which the
panel reads with ``journalctl USER_UNIT=...`` (allowed through the adm group).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import time
from pathlib import Path

LOGURU_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) \| (?P<level>[A-Z]+)\s*\| (?P<src>\S+) - (?P<msg>.*)$")
CHUNK_RE = re.compile(r"LM chunk \d+/\d+ \(infer_type=\w+\) \(size: \d+, seeds: \[(?P<seeds>[\d, ]+)\]\)")
LM_DONE_RE = re.compile(r"Phase 2 completed in (?P<s>[\d.]+)s\. Generated (?P<codes>\d+) audio codes")
DIT_RE = re.compile(r"^\s*(?P<pct>\d{1,3})%\|[^|]*\|\s*(?P<step>\d+)/(?P<total>\d+) \[")
LMBAR_RE = re.compile(r"^Generating:\s+\d+%\|[^|]*\|\s*\d+/\d+ \[(?P<elapsed>[\d:]+)<")
SAVED_RE = re.compile(r"\[AudioSaver\] Saved audio to (?P<path>\S+)")
LORA_RE = re.compile(r"LoRA adapter '(?P<name>[^']+)' loaded from")
LORA_SCALE_RE = re.compile(r"LoRA scale for '(?P<name>[^']+)' set to (?P<scale>[\d.]+)")
ERROR_HINTS = ("VRAM pre-flight failed", "CUDA requested but unavailable", "Failed to load LoRA adapter",
               "is_available() returned False", "out of memory")
LORA_LOG_RE = re.compile(r'^(?P<fn>load_lora|set_use_lora|set_lora_scale) event: (?P<ev>\w+) \| data: \["(?P<data>.*)"\]$')
UNIT_MSG_RE = re.compile(r"^(?P<verb>Started|Starting|Finished|Stopping|Stopped|Failed to start|Reloaded) (?P<unit>[\w@.-]+\.service)")


def _ts(s: str) -> float:
    # loguru writes the machine's local time; the panel runs on the same machine.
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f").timestamp()


def read_tail(path: Path, max_bytes: int = 131072) -> tuple[list[str], float | None]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read().decode("utf-8", "replace")
        mtime = os.stat(path).st_mtime
    except OSError:
        return [], None
    lines = [x for x in re.split(r"[\r\n]+", data) if x.strip()]
    return (lines[1:] if size > max_bytes else lines), mtime  # first line may be cut


def parse(lines: list[str]) -> dict:
    """Walk the log tail and return the current take (if any), the last finished take and events."""
    job = None          # take in progress
    last_done = None    # last finished take
    events: list[dict] = []
    lora = None
    last_error = None
    last_ts = None

    def ev(text, level="info"):
        events.append({"at": last_ts, "text": text, "level": level})

    for ln in lines:
        m = LOGURU_RE.match(ln)
        if m:
            try:
                last_ts = _ts(m.group("ts"))
            except ValueError:
                pass
            msg, level = m.group("msg"), m.group("level")
            if "[memory] Set M_MMAP_THRESHOLD=" in msg:
                job, last_error = None, None  # errors from before a restart are history
                ev("ACE service (re)started")
            elif (c := CHUNK_RE.search(msg)):
                seeds = [s.strip() for s in c.group("seeds").split(",") if s.strip()]
                job = {"seed": seeds[0] if seeds else None, "started": last_ts, "phase": "lm", "step": 0,
                       "total": None, "lm_s": None}
                ev(f"take started · seed {job['seed']}")
            elif job and (d := LM_DONE_RE.search(msg)):
                job["lm_s"] = float(d.group("s"))
                ev(f"LM done in {float(d.group('s')):.1f} s ({d.group('codes')} audio codes)")
            elif job and ("[generate_music] Starting generation" in msg or "DiT diffusion via" in msg):
                job["phase"] = "dit"
            elif job and "Model generation completed. Decoding latents" in msg:
                job["phase"] = "decode"
                ev("diffusion done · decoding latents")
            elif (sv := SAVED_RE.search(msg)):
                took = (last_ts - job["started"]) if job and job.get("started") and last_ts else None
                last_done = {"seed": job["seed"] if job else None, "at": last_ts, "took_s": round(took, 1) if took else None,
                             "file": Path(sv.group("path")).name}
                ev(f"take saved{f' in {took:.0f} s' if took else ''}")
                job = None
            elif (lr := LORA_RE.search(msg)):
                lora = {"name": lr.group("name"), "scale": None, "at": last_ts}
                ev(f"LoRA {lr.group('name')} loaded")
            elif (ls := LORA_SCALE_RE.search(msg)):
                lora = {**(lora or {"name": ls.group("name")}), "scale": float(ls.group("scale"))}
            if level in ("ERROR", "CRITICAL") or any(h in msg for h in ERROR_HINTS):
                last_error = {"at": last_ts, "text": msg[:240]}
                ev(msg[:200], "error")
            continue
        if job and (d := DIT_RE.match(ln)):
            total = int(d.group("total"))
            if total > 1:  # LM bars are always 0/1 or 1/1
                job.update(step=int(d.group("step")), total=total, phase="dit" if job["phase"] != "decode" else "decode")
        elif any(h in ln for h in ERROR_HINTS) or re.match(r"^\w*(Error|Exception): ", ln):
            last_error = {"at": last_ts, "text": ln.strip()[:240]}
            ev(ln.strip()[:200], "error")
    return {"job": job, "last_done": last_done, "events": events[-10:], "lora": lora, "last_error": last_error}


def take_progress(job: dict | None, now: float) -> int | None:
    """0-100 for the take in flight: LM ~30 %, DiT steps ~60 %, decode the rest."""
    if not job:
        return None
    if job["phase"] == "decode":
        return 95
    if job["phase"] == "dit" and job.get("total"):
        return round(30 + 60 * job["step"] / job["total"])
    if job["phase"] == "dit":
        return 30
    el = now - (job.get("started") or now)
    return round(min(28, 28 * el / 20))


def journal(units, n: int = 40, runner=None) -> list[dict]:
    """Lifecycle lines for user units of any account (needs read access to the journal files)."""
    args = ["journalctl", "--no-pager", "-q", "-o", "json", "-n", str(n)]
    first = True
    for u in units:
        for field in ("USER_UNIT", "_SYSTEMD_USER_UNIT"):
            if not first:
                args.append("+")
            args.append(f"{field}={u}")
            first = False
    run = runner or subprocess.run
    try:
        p = run(args, capture_output=True, text=True, timeout=3)
        out = p.stdout if p.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        try:
            j = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = j.get("MESSAGE")
        if isinstance(msg, list):  # binary-safe form
            msg = bytes(msg).decode("utf-8", "replace")
        unit = j.get("USER_UNIT") or j.get("_SYSTEMD_USER_UNIT") or ""
        try:
            at = int(j.get("__REALTIME_TIMESTAMP", "0")) / 1e6
        except ValueError:
            at = 0
        rows.append({"at": at, "unit": unit, "text": str(msg or "")[:240]})
    rows.sort(key=lambda r: r["at"])
    return rows


def unit_state(rows: list[dict], unit: str) -> dict | None:
    """Last lifecycle verb the journal recorded for a unit."""
    for r in reversed(rows):
        if r["unit"] != unit:
            continue
        m = UNIT_MSG_RE.match(r["text"])
        if m and m.group("unit") == unit:
            return {"verb": m.group("verb"), "at": r["at"], "text": r["text"]}
        if r["text"].startswith(f"{unit}: Failed with result") and "exit-code" not in r["text"]:
            return {"verb": "Failed", "at": r["at"], "text": r["text"]}
    return None


def lora_log(path: Path) -> dict | None:
    """Result of the last LoRA load run (lora_default.log, appended each boot)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 8192))
            lines = fh.read().decode("utf-8", "replace").splitlines()
        mtime = os.stat(path).st_mtime
    except OSError:
        return None
    last = None
    for ln in lines:
        m = LORA_LOG_RE.match(ln.strip())
        if m and m.group("fn") == "load_lora":
            try:
                data = json.loads('"' + m.group("data") + '"')
            except json.JSONDecodeError:
                data = m.group("data")
            text = re.sub(r" from /\S+", "", data.lstrip("\u2705\u274c\u26a0\ufe0f ").strip())  # drop the checkpoint path
            last = {"ok": data.startswith("\u2705"), "text": text[:200], "at": mtime}
    return last


def cgroup_active(cgroup_root: Path, unit: str) -> bool | None:
    """True when a systemd unit (system or any user's) has live processes; None when cgroups are not visible."""
    if not cgroup_root.is_dir():
        return None
    pats = [f"system.slice/{unit}/cgroup.procs", f"user.slice/user-*.slice/user@*.service/app.slice/{unit}/cgroup.procs"]
    seen = False
    for pat in pats:
        for f in cgroup_root.glob(pat):
            seen = True
            try:
                if f.read_text().strip():
                    return True
            except OSError:
                continue
    return False if seen else None


def ace_state(health_ok: bool, parsed: dict, log_mtime: float | None, lock: dict | None,
              gen_procs: list[dict], now: float | None = None) -> dict:
    """Combine log, GPU lock and lane processes into one ACE status."""
    now = now or time.time()
    job = parsed.get("job")
    fresh = bool(log_mtime and now - log_mtime < 120)
    # a kanban worker starts ace_sections.py as a child: report the script, not the worker
    proc = next((p for p in gen_procs if not p.get("worker")), gen_procs[0] if gen_procs else None)
    generating = bool(proc or lock or (job and fresh))
    card = (proc or {}).get("card") or (lock or {}).get("card")
    info = None
    if generating:
        info = {"card": card, "pid": (proc or lock or {}).get("pid"), "script": (proc or {}).get("script"),
                "since": (proc or {}).get("since") or (lock or {}).get("since") or (job or {}).get("started"),
                "tag": (lock or {}).get("tag"), "seed": (job or {}).get("seed") if fresh else None,
                "phase": (job or {}).get("phase") if fresh else None,
                "step": (job or {}).get("step") if fresh else None, "total": (job or {}).get("total") if fresh else None}
    status = "generating" if generating else ("idle" if health_ok else "down")
    return {"ok": health_ok, "status": status, "job": info,
            "progress": take_progress(job if fresh else None, now) if generating else None,
            "last_done": parsed.get("last_done"), "lora": parsed.get("lora"), "last_error": parsed.get("last_error"),
            "log_age_s": round(now - log_mtime) if log_mtime else None}
