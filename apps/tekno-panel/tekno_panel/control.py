"""Operator controls (approved by the machine owner): a fixed allowlist of actions with validated parameters.

Nothing here ever builds a shell string from user input. Every action is
- allowlisted in :data:`ACTIONS` (unknown names are refused),
- validated (task ids, card slugs, run steps, services and pids against strict patterns and live state),
- confirmed in the UI; ``danger`` actions also need the passcode again (checked server-side, counts toward the
  login lockout),
- rate limited, and written to an append-only audit log with its outcome.

The panel itself runs with NoNewPrivileges: the sudo-backed actions are executed by the separate control
helper (:mod:`tekno_panel.helper`), which rebuilds the argv from this same allowlist.

Identities: the panel's own user for the PAUSED flag and the hermes kanban CLI; ``sudo -n -u <ace user>``
for the lane scripts and the ACE/agent-mail user units; ``sudo -n systemctl`` for the two system units;
``sudo -n kill`` only for pids that the live process scan attributes to the card being stopped.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from pathlib import Path

from . import procs

TASK_RE = re.compile(r"^t_[0-9a-f]{6,32}$")
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RUN_STEPS = ("plan", "gen", "edit", "stems", "swap", "var", "mix", "master")
MAX_NOTE = 1000


class ActionError(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


def _note(text, required: bool, what: str = "note") -> str:
    t = " ".join(str(text or "").split())[:MAX_NOTE]
    if required and not t:
        raise ActionError(f"a {what} is required")
    return t


def services_map(ace_user: str, ace_uid: int) -> dict:
    """Restartable services -> fixed argv. Anything not listed here can't be restarted from the panel."""
    def user_units(*units):
        return ["sudo", "-n", "-u", ace_user, "env", f"XDG_RUNTIME_DIR=/run/user/{ace_uid}", "systemctl", "--user",
                "restart", *units]
    return {
        "acestep-api": user_units("acestep-api.service"),
        "acestep-lora": user_units("acestep-lora.service"),
        "acestep-ui": user_units("acestep-ui-backend.service", "acestep-ui-frontend.service"),
        "agent-mail": user_units("agent-mail.service"),
        "hermes-gateway": ["sudo", "-n", "systemctl", "restart", "hermes-gateway"],
        "litellm": ["sudo", "-n", "systemctl", "restart", "aihub-litellm"],
    }


ACTIONS = {
    "machine.pause": {"tier": "safe", "label": "Pause the machine",
                      "help": "Writes PAUSED: ACE jobs refuse to start (ace_job, ace_sections, regen_exact). Running work finishes."},
    "machine.resume": {"tier": "safe", "label": "Resume the machine", "help": "Removes PAUSED (also one the watchdog wrote)."},
    "card.note": {"tier": "safe", "label": "Add a note to a card", "help": "hermes kanban comment"},
    "card.unblock": {"tier": "safe", "label": "Unblock / retry a card",
                     "help": "hermes kanban unblock: back to ready; the gateway's dispatcher may start a worker."},
    "card.block": {"tier": "safe", "label": "Block a card", "help": "hermes kanban block with your reason."},
    "kanban.dispatch": {"tier": "danger", "label": "Run one dispatcher pass",
                        "help": "hermes kanban dispatch --max 3: promotes ready cards and spawns workers."},
    "lane.run": {"tier": "danger", "label": "Run the pipeline from a step",
                 "help": "run_e2e.sh <card> <step> as the lane user, logged to the card's next runN.log."},
    "lane.stop": {"tier": "danger", "label": "Stop the running lane",
                  "help": "SIGTERM to the lane scripts and run_e2e.sh running for that card."},
    "service.restart": {"tier": "danger", "label": "Restart a service", "help": "systemctl restart (allowlisted units only)."},
}


class Controller:
    def __init__(self, s, data_dir: Path, runner=None, clock=time.time, helper=None):
        self.s = s
        self.helper = helper  # (action, params) -> (code, out, err) for sudo-backed actions
        self.data_dir = data_dir
        self.runner = runner or self._run
        self.clock = clock
        self.lock = threading.Lock()
        self.recent: list[float] = []
        self.audit_path = data_dir / "audit.jsonl"
        self.services = services_map(s.ace_user, s.ace_uid)

    # ---------- helpers ----------
    @staticmethod
    def _run(argv, timeout=25):
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout or "")[-2000:], (p.stderr or "")[-2000:]

    @property
    def paused_path(self) -> Path:
        return self.s.machine_root / "PAUSED"

    def paused(self) -> dict:
        p = self.paused_path
        try:
            st = p.stat()
            reason = p.read_text(errors="replace").strip()[:200]
        except OSError:
            return {"paused": False}
        return {"paused": True, "since": st.st_mtime, "reason": reason}

    def audit(self, rec: dict) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with open(self.audit_path, "a") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
            if self.audit_path.stat().st_size > 2_000_000:
                lines = self.audit_path.read_text().splitlines()[-2000:]
                self.audit_path.write_text("\n".join(lines) + "\n")
        except OSError:
            pass

    def audit_tail(self, n: int = 50) -> list[dict]:
        try:
            lines = self.audit_path.read_text().splitlines()[-n:]
        except OSError:
            return []
        out = []
        for ln in reversed(lines):
            try:
                rec = json.loads(ln)
            except ValueError:
                continue
            rec.pop("argv", None)
            out.append(rec)
        return out

    def _rate(self, limit: int = 8, window: float = 60.0):
        now = self.clock()
        self.recent = [t for t in self.recent if now - t < window]
        if len(self.recent) >= limit:
            raise ActionError("too many actions in a minute, wait a bit", 429)
        self.recent.append(now)

    def registry(self) -> dict:
        return {"actions": ACTIONS, "services": sorted(self.services), "steps": list(RUN_STEPS)}

    # ---------- run ----------
    def run(self, name: str, params: dict, running: list[dict], ip: str, verify_passcode) -> dict:
        spec = ACTIONS.get(name)
        if not spec:
            raise ActionError("unknown action", 404)
        params = params if isinstance(params, dict) else {}
        if spec["tier"] == "danger" and not verify_passcode(str(params.get("passcode", ""))):
            self.audit({"ts": self.clock(), "action": name, "ip": ip, "ok": False, "summary": "refused: wrong passcode"})
            raise ActionError("passcode required for this action (wrong or missing)", 403)
        with self.lock:
            self._rate()
            argv, summary, detached = self._plan(name, params, running)
            rec = {"ts": self.clock(), "action": name, "params": {k: v for k, v in params.items() if k != "passcode"},
                   "ip": ip, "summary": summary}
            if argv is None:  # handled in-process (PAUSED flag)
                result = self._flag(name, params)
                rec.update(ok=True, result=result)
                self.audit(rec)
                return {"ok": True, "summary": summary, "result": result}
            try:
                if argv[0] == "sudo" and self.helper:
                    code, out, err = self.helper(name, {k: v for k, v in params.items() if k != "passcode"})
                else:
                    code, out, err = self.runner(argv)
            except (OSError, subprocess.SubprocessError) as e:
                code, out, err = 127, "", str(e)
            ok = code == 0
            rec.update(ok=ok, code=code, out=out[-400:], err=err[-400:], argv=argv)
            self.audit(rec)
            if not ok:
                raise ActionError(f"{summary} failed: {(err or out).strip()[-300:] or f'exit {code}'}", 502)
            return {"ok": True, "summary": summary, "result": "started" if detached else (out.strip()[-400:] or "done")}

    def _flag(self, name: str, params: dict) -> str:
        p = self.paused_path
        if name == "machine.pause":
            reason = _note(params.get("reason"), False) or "paused from TEKNO CONTROL"
            p.write_text(f"{reason} ({time.strftime('%Y-%m-%d %H:%M:%S')})\n")
            return "PAUSED written"
        try:
            p.unlink()
            return "PAUSED removed"
        except FileNotFoundError:
            return "was not paused"

    def _task(self, params) -> str:
        t = str(params.get("task_id", ""))
        if not TASK_RE.match(t):
            raise ActionError("bad task id")
        return t

    def _card(self, params) -> str:
        slug = str(params.get("card", ""))
        if not SLUG_RE.match(slug) or not (self.s.cards_path / slug).is_dir():
            raise ActionError("unknown card")
        return slug

    def _plan(self, name: str, params: dict, running: list[dict]):
        """Validate and return (argv or None, summary, detached)."""
        hermes = [str(self.s.hermes_bin), "kanban", "--board", self.s.board]
        if name in ("machine.pause", "machine.resume"):
            return None, "Pause the machine" if name == "machine.pause" else "Resume the machine", False
        if name == "card.note":
            t = self._task(params)
            return hermes + ["comment", "--author", "tekno-control", t, _note(params.get("note"), True)], f"Note on {t}", False
        if name == "card.unblock":
            t = self._task(params)
            note = _note(params.get("note"), False)
            return hermes + ["unblock", t] + (["--reason", f"TEKNO CONTROL: {note}"] if note else []), f"Unblock {t}", False
        if name == "card.block":
            t = self._task(params)
            return hermes + ["block", t, f"TEKNO CONTROL: {_note(params.get('note'), True, 'reason')}"], f"Block {t}", False
        if name == "kanban.dispatch":
            if self.paused()["paused"]:
                raise ActionError("the machine is paused", 409)
            return hermes + ["dispatch", "--max", "3"], "Dispatcher pass", False
        if name == "lane.run":
            slug = self._card(params)
            step = str(params.get("step", ""))
            if step not in RUN_STEPS:
                raise ActionError("unknown step")
            if self.paused()["paused"]:
                raise ActionError("the machine is paused: resume first", 409)
            if any(p.get("card") == slug for p in running):
                raise ActionError(f"{slug} already has a lane running", 409)
            if step in ("plan", "gen") and procs.gpu_lock(self.s.machine_root / ".gpu.lock", self.s.proc_root):
                raise ActionError("the GPU lock is held by another job", 409)
            card = self.s.cards_path / slug
            n = 1 + max([int(m.group(1) or 1) for f in card.glob("run*.log")
                         for m in [re.match(r"run(\d*)\.log$", f.name)] if m] or [0])
            log = card / f"run{n}.log"
            script = str(self.s.scripts_path / "run_e2e.sh")
            # positional args only: nothing from the request is interpolated into the shell text
            # umask 022 so the new card files stay readable like the ones from manual runs
            body = 'umask 022; cd "$1" && setsid nohup bash "$2" "$3" "$4" > "$5" 2>&1 < /dev/null & echo started'
            argv = ["sudo", "-n", "-u", self.s.ace_user, "bash", "-c", body, "tekno-run",
                    str(self.s.ace_workdir), script, slug, step, str(log)]
            return argv, f"Run {slug} from {step} → {log.name}", True
        if name == "lane.stop":
            slug = self._card(params)
            pids = sorted({int(p["pid"]) for p in running if p.get("card") == slug and not p.get("worker")})
            if not pids:
                raise ActionError(f"nothing is running for {slug}", 409)
            return ["sudo", "-n", "kill", "-TERM"] + [str(p) for p in pids], f"Stop {slug} (pids {', '.join(map(str, pids))})", False
        if name == "service.restart":
            svc = str(params.get("service", ""))
            if svc not in self.services:
                raise ActionError("this service can't be restarted from the panel")
            return self.services[svc], f"Restart {svc}", False
        raise ActionError("unknown action", 404)
