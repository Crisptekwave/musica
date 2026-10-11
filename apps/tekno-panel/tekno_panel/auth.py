"""Single-passcode login with an HMAC-signed session cookie."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

COOKIE = "tekno_session"
SESSION_TTL = 30 * 24 * 3600  # stay logged in on the phone for 30 days


def _read_or_create(path: Path, factory) -> str:
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        val = factory()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(val + "\n")
        return val


class Auth:
    def __init__(self, state_dir: Path, passcode: str | None = None):
        self.secret = _read_or_create(state_dir / "session.key", lambda: secrets.token_hex(32)).encode()
        self.passcode = passcode or os.environ.get("TEKNO_PASSCODE") or _read_or_create(
            state_dir / "passcode", lambda: f"{secrets.randbelow(10**6):06d}")
        self._fails: dict[str, list[float]] = {}

    # -- brute-force guard: 5 wrong tries per IP per 5 minutes --
    def locked(self, ip: str) -> bool:
        now = time.time()
        self._fails[ip] = [t for t in self._fails.get(ip, []) if now - t < 300]
        return len(self._fails[ip]) >= 5

    def check(self, ip: str, code: str) -> bool:
        if self.locked(ip):
            return False
        ok = hmac.compare_digest(hashlib.sha256(code.strip().encode()).digest(),
                                 hashlib.sha256(self.passcode.encode()).digest())
        if not ok:
            self._fails.setdefault(ip, []).append(time.time())
        return ok

    def issue(self) -> str:
        exp = str(int(time.time()) + SESSION_TTL)
        sig = hmac.new(self.secret, exp.encode(), hashlib.sha256).digest()
        return exp + "." + base64.urlsafe_b64encode(sig).decode().rstrip("=")

    def valid(self, token: str | None) -> bool:
        if not token or "." not in token:
            return False
        exp, sig = token.split(".", 1)
        if not exp.isdigit() or int(exp) < time.time():
            return False
        want = base64.urlsafe_b64encode(hmac.new(self.secret, exp.encode(), hashlib.sha256).digest()).decode().rstrip("=")
        return hmac.compare_digest(want, sig)
