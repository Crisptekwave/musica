"""Machine health: GPU, disk, services and the ACE-Step job. All read-only probes."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from . import acelog, procs


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
            "temp": num(f[4]), "power": num(f[5]) if len(f) > 5 else None, "apps": gpu_apps()}


def gpu_apps(proc_root: Path = Path("/proc")) -> list[dict]:
    """Processes holding GPU memory, labelled from their command line."""
    code, out = _run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"])
    apps = []
    for line in out.strip().splitlines() if code == 0 else []:
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        try:
            argv = (proc_root / parts[0] / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            argv = ""
        label = "ACE-Step" if "acestep" in argv else (Path(argv.split()[0]).name if argv.strip() else f"pid {parts[0]}")
        try:
            mem = float(parts[1])
        except ValueError:
            mem = None
        apps.append({"pid": int(parts[0]), "name": label, "mem": mem})
    return apps


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


def service(spec: dict, journal_rows: list[dict] | None = None, lora: dict | None = None,
            cgroup_root: Path = Path("/sys/fs/cgroup")) -> dict:
    """Try unit -> cgroup -> journal -> http -> LoRA -> process checks; first conclusive answer wins."""
    name = spec["name"]
    detail = []
    if spec.get("unit"):
        st = _unit_state(spec["unit"])
        if st:
            return {"name": name, "status": "up" if st == "active" else ("failed" if st == "failed" else "starting"),
                    "via": f"systemd:{spec['unit']}"}
        detail.append("unit not visible")
    if spec.get("cgroup"):
        # another account's user unit: its cgroup still shows whether it has processes
        if acelog.cgroup_active(cgroup_root, spec["cgroup"]):
            return {"name": name, "status": "up", "via": f"cgroup:{spec['cgroup']}"}
    if spec.get("journal") and journal_rows is not None:
        st = acelog.unit_state(journal_rows, spec["journal"])
        if st:
            verb = st["verb"]
            if spec.get("lora_log") and lora and verb == "Finished":
                return {"name": name, "status": "up" if lora["ok"] else "failed",
                        "via": f"journal: {verb.lower()} · {lora['text'][:80]}"}
            status = {"Finished": "up", "Started": "up", "Starting": "starting", "Reloaded": "up",
                      "Stopping": "down", "Stopped": "down", "Failed": "failed", "Failed to start": "failed"}[verb]
            return {"name": name, "status": status, "via": f"journal: {verb.lower()}"}
        detail.append("no journal lines")
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


def ace(ace_url: str, lock: Path, log: Path, gen_procs: list[dict] | None = None,
        proc_root: Path = Path("/proc"), now: float | None = None) -> dict:
    """ACE status: API health, the take in flight (log + GPU lock + lane process) and recent events."""
    now = now or time.time()
    h = http_json(ace_url.rstrip("/") + "/health")
    ok = bool(h and (h.get("data") or {}).get("status") == "ok")
    lines, mtime = acelog.read_tail(log)
    parsed = acelog.parse(lines)
    st = acelog.ace_state(ok, parsed, mtime, procs.gpu_lock(lock, proc_root), gen_procs or [], now)
    st["log"] = [e["text"] for e in parsed["events"][-6:]]
    st["events"] = parsed["events"][-8:]
    return st


def load() -> dict:
    try:
        a, b, c = os.getloadavg()
        return {"1m": round(a, 2), "5m": round(b, 2), "15m": round(c, 2), "cpus": os.cpu_count()}
    except OSError:
        return {}
