"""Privilege-separated helper for the owner-approved sudo actions.

The web panel runs with NoNewPrivileges and can't use sudo. This helper is a separate user service with no
network port: it listens on a unix socket (mode 600, same user only, checked with SO_PEERCRED) and accepts
one JSON request per connection: ``{"action": ..., "params": {...}}``. It never takes a command line from
the panel: it re-validates the action against the same allowlist (:class:`tekno_panel.control.Controller`),
re-reads the live process table itself, builds the fixed argv and runs it. Only the sudo-backed actions (and the fallback MODE write) are
served here; passcode checks, rate limits and the audit log stay in the panel.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
from pathlib import Path

from . import procs
from .config import load_settings
from .control import ActionError, Controller

HELPER_ACTIONS = {"lane.run", "lane.stop", "service.restart", "fallback.mode"}


def socket_path() -> Path:
    return Path(os.environ.get("TEKNO_HELPER_SOCKET") or
                Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "tekno-control.sock")


def handle(ctrl: Controller, req: dict, scan=procs.scan) -> dict:
    name = str(req.get("action", ""))
    if name not in HELPER_ACTIONS:
        return {"code": 126, "out": "", "err": "action not served by the helper"}
    params = req.get("params") if isinstance(req.get("params"), dict) else {}
    try:
        argv, _summary, _detached = ctrl._plan(name, params, scan(ctrl.s.proc_root))
    except ActionError as e:
        return {"code": 125, "out": "", "err": str(e)}
    if name == "fallback.mode":  # no sudo: the helper itself writes the MODE file (group-writable)
        code, out, err = ctrl.set_fallback_mode(argv[1])
        return {"code": code, "out": out, "err": err}
    if not argv or argv[0] != "sudo":
        return {"code": 126, "out": "", "err": "unexpected plan"}
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=25, stdin=subprocess.DEVNULL)
        return {"code": p.returncode, "out": (p.stdout or "")[-2000:], "err": (p.stderr or "")[-2000:]}
    except (OSError, subprocess.SubprocessError) as e:
        return {"code": 127, "out": "", "err": str(e)}


def client(name: str, params: dict, path: Path | None = None, timeout: float = 30) -> tuple[int, str, str]:
    """Called by the panel: ask the helper to run one allowlisted sudo action."""
    path = path or socket_path()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect(str(path))
        except OSError as e:
            return 127, "", f"control helper not running ({e}); start tekno-control-helper.service"
        s.sendall(json.dumps({"action": name, "params": params}).encode() + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    try:
        r = json.loads(data or b"{}")
    except ValueError:
        return 127, "", "bad reply from the control helper"
    return int(r.get("code", 127)), str(r.get("out", "")), str(r.get("err", ""))


def serve(path: Path | None = None) -> None:  # pragma: no cover - long-running service
    s = load_settings()
    ctrl = Controller(s, s.data_dir)
    path = path or socket_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    old = os.umask(0o177)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    os.umask(old)
    os.chmod(path, 0o600)
    srv.listen(4)
    me = os.getuid()
    print(f"tekno control helper listening on {path}", flush=True)
    while True:
        conn, _ = srv.accept()
        with conn:
            try:
                pid, uid, gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != me:
                    conn.sendall(b'{"code": 126, "err": "wrong user"}\n')
                    continue
                conn.settimeout(10)
                data = b""
                while not data.endswith(b"\n") and len(data) < 65536:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                req = json.loads(data or b"{}")
                res = handle(ctrl, req if isinstance(req, dict) else {})
                print(f"{req.get('action')} -> {res['code']}", flush=True)
                conn.sendall(json.dumps(res).encode() + b"\n")
            except (OSError, ValueError) as e:
                try:
                    conn.sendall(json.dumps({"code": 127, "err": str(e)}).encode() + b"\n")
                except OSError:
                    pass


if __name__ == "__main__":  # pragma: no cover
    serve(Path(sys.argv[1]) if len(sys.argv) > 1 else None)
