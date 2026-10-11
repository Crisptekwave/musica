"""Machine health: GPU, disk, services and the ACE-Step job. All read-only probes."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from .files import tail

PROGRESS_RE = re.compile(r"(\d{1,3})%\|")
STEPS_RE = re.compile(r"\b(\d{1,4})/(\d{1,4})\s*\[")


def _run(cmd: list[str], timeout: float = 3.0) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except (OSError, subprocess.SubprocessError):
        return 127, ""


def http_json(url: str, timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode() or "null")
    except Exception:
        return None


def gpu() -> dict:
    code, out = _run([
        "nvidia-smi",
        "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
        "--format=csv,noheader,nounits",
    ])
    if code != 0 or not out.strip():
        return {"ok": False, "error": "nvidia-smi unavailable"}
    f = [x.strip() for x in out.strip().splitlines()[0].split(",")]

    def num(x):
        try:
            return float(x)
        except ValueError:
            return None

    return {"ok": True, "name": f[0], "util": num(f[1]), "mem_used": num(f[2]), "mem_total": num(f[3]),
            "temp": num(f[4]), "power": num(f[5]) if len(f) > 5 else None}


def disk(path: Path, warn_gb: float) -> dict:
    try:
        u = shutil.disk_usage(path)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    free = u.free / 1e9
    return {"ok": True, "path": str(path), "free_gb": round(free, 1), "total_gb": round(u.total / 1e9, 1),
            "used_pct": round(100 * u.used / u.total, 1), "warn": free < warn_gb, "warn_gb": warn_gb}


def _unit_state(unit: str) -> str | None:
    for args in (["systemctl", "is-active", unit], ["systemctl", "--user", "is-active", unit]):
        code, out = _run(args, 2.0)
        st = out.strip()
        if st in ("active", "activating", "reloading", "failed"):
            return st
    return None


def _process_running(pattern: str) -> bool:
    code, _ = _run(["pgrep", "-f", pattern], 2.0)
    return code == 0


def service(spec: dict) -> dict:
    """Try unit -> http -> process checks; first conclusive answer wins."""
    name = spec["name"]
    detail = []
    if spec.get("unit"):
        st = _unit_state(spec["unit"])
        if st:
            return {"name": name, "status": "up" if st == "active" else ("failed" if st == "failed" else "starting"),
                    "via": f"systemd:{spec['unit']}"}
        detail.append("unit not visible")
    if spec.get("http"):
        if http_json(spec["http"]) is not None:
            return {"name": name, "status": "up", "via": "http"}
        detail.append("http down")
    if spec.get("lora_probe"):
        d = http_json(spec["lora_probe"])
        if d and isinstance(d.get("functions"), dict):
            fl = d["functions"].get("load_lora")
            if fl and fl.get("success_rate", 0) > 0:
                return {"name": name, "status": "up", "via": "lora loaded"}
            return {"name": name, "status": "down", "via": "no lora load seen"}
        detail.append("ace monitoring unavailable")
    if spec.get("process"):
        if _process_running(spec["process"]):
            return {"name": name, "status": "up", "via": "process"}
        detail.append("no process")
    return {"name": name, "status": "down", "via": ", ".join(detail) or "unknown"}


def ace(ace_url: str, lock: Path, log: Path) -> dict:
    h = http_json(ace_url.rstrip("/") + "/health")
    ok = bool(h and (h.get("data") or {}).get("status") == "ok")
    job = None
    try:
        raw = lock.read_text().strip() if lock.exists() else ""
    except OSError:
        raw = ""
    if raw:
        pid_s, _, card = raw.partition(" ")
        alive = pid_s.isdigit() and Path(f"/proc/{pid_s}").exists()
        if alive:
            job = {"pid": int(pid_s), "card": Path(card).name or card, "since": lock.stat().st_mtime}
    lines = tail(log, 40, 32768)
    progress = None
    for ln in reversed(lines):
        if "Done! Generated" in ln or "Saved audio" in ln:
            progress = 100 if job else None
            break
        m = STEPS_RE.search(ln)
        if m and int(m.group(2)) > 1:
            progress = round(100 * int(m.group(1)) / int(m.group(2)))
            break
        m = PROGRESS_RE.search(ln)
        if m:
            progress = int(m.group(1))
            break
    if job and progress is None:
        # ~160 s per 180 s take on the L4: estimate from elapsed time
        progress = min(95, round(100 * (time.time() - job["since"]) / 170))
    return {"ok": ok, "status": "generating" if job else ("idle" if ok else "down"), "job": job,
            "progress": progress if job else None,
            "log": [re.sub(r"^\S+ \S+ \| \w+\s+\| ", "", ln)[:200] for ln in lines[-6:]]}


def load() -> dict:
    try:
        a, b, c = os.getloadavg()
        return {"1m": round(a, 2), "5m": round(b, 2), "15m": round(c, 2), "cpus": os.cpu_count()}
    except OSError:
        return {}
