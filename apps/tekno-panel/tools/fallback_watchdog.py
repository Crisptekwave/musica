#!/usr/bin/env python3
"""fallback-watchdog: let the local Qwen continue Claude's work when Claude hits its usage limit.

Claude leaves, per card, a hand-off note <FALLBACK>/handoff/<slug>.md plus the exact step scripts it wants run in
<FALLBACK>/handoff/<slug>/ (NN-name.sh). Every post as Claude in agent-mail touches <FALLBACK>/claude_heartbeat.
This one-shot job (user timer, every 10 min) starts the local agent only when ALL of these hold:

  - <FALLBACK>/MODE is "auto" (or "force": skip the heartbeat check; "off": never run)
  - Claude's heartbeat AND every card folder are quiet for STALE_S (Claude is not working)
  - a hand-off note says "STATUS: open"
  - the local model answers on vLLM, and no fallback run is already going

Allowlist only (Ryan, 2026-10-11): the model gets NO shell and NO file editing. It is bound to the one note it
claimed and can only call the tools in TOOLS: read the note, run one of Claude's step scripts for that card,
score the card, comment on the kanban card, post in agent-mail as "qwen", log, and finish (done | blocked).
The runner, not the model, enforces the plan: only the steps named in NEXT, in order; a failed step stops the run
(one retry); real exit codes go into the note's LOG; "done" is downgraded to "blocked" unless every step succeeded;
a note that asks for a judgement call is never run. A run that ends any other way marks the note "stuck", so a
note is never retried in a loop.

  fallback_watchdog.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_status  # noqa: E402
import team_mail  # noqa: E402

FB = Path(os.environ.get("TEKNO_FALLBACK_DIR", "/mnt/agent-data/tekno_machine/fallback"))
CARDS = Path(os.environ.get("TEKNO_CARDS_DIR", "/mnt/agent-data/tekno_machine/cards"))
LLM = os.environ.get("TEKNO_FALLBACK_LLM", "http://127.0.0.1:8001/v1")
MODEL = os.environ.get("TEKNO_FALLBACK_MODEL", "qwen3-coder")
PY = os.environ.get("TEKNO_TOOLS_PY", "/mnt/agent-data/ace-step/tools-venv/bin/python")
SCORE = os.environ.get("TEKNO_SCORE_CARD", "/mnt/agent-data/tekno_machine/scripts/score_card.py")
KANBAN = [os.path.expanduser("~/.hermes/hermes-agent/venv/bin/hermes"), "kanban", "--board", "tekno-machine"]
STALE_S = int(os.environ.get("TEKNO_FALLBACK_STALE_S", 45 * 60))
RUN_LIMIT_S = int(os.environ.get("TEKNO_FALLBACK_RUN_S", 40 * 60))
STEP_LIMIT_S = 20 * 60
MAX_TURNS = 30
STATUS_RE = re.compile(r"^STATUS:\s*(.+)$", re.M)
# a hand-off note is a list of literal steps; one that asks for an opinion goes back to Claude unrun
JUDGEMENT_RE = re.compile(r"\b(decide|judge|listen|if you think|if needed|choose|whether|sounds?|better|improve)\b", re.I)
FIELD_RE = r"^{}:\s*(\S+)"
STEP_RE = re.compile(r"^\d{2}-[a-z0-9_-]+\.sh$")

TOOLS = [
    {"name": "read_note", "description": "Read the hand-off note (DONE, NEXT, NOT ALLOWED, LOG).", "params": {}},
    {"name": "list_steps", "description": "List the step scripts Claude prepared for this card, in order.", "params": {}},
    {"name": "run_step", "description": "Run ONE prepared step script by its exact file name. Returns exit code and the last lines of output.",
     "params": {"name": {"type": "string", "description": "file name from list_steps, e.g. 01-render.sh"}}},
    {"name": "score_card", "description": "Score the card's current master (fast mode). Returns the score summary.", "params": {}},
    {"name": "kanban_comment", "description": "Add a comment to this card on the kanban board.",
     "params": {"text": {"type": "string"}}},
    {"name": "send_mail", "description": "Post a short status in this card's agent-mail thread (as qwen).",
     "params": {"text": {"type": "string"}}},
    {"name": "append_log", "description": "Append one line to the LOG of the hand-off note.", "params": {"text": {"type": "string"}}},
    {"name": "finish", "description": "End the run. status 'done' when every NEXT step succeeded, 'blocked' otherwise (explain why).",
     "params": {"status": {"type": "string", "enum": ["done", "blocked"]}, "reason": {"type": "string"}}},
]


def mode() -> str:
    try:
        return (FB / "MODE").read_text().strip().lower() or "auto"
    except OSError:
        return "auto"


def newest_activity() -> float:
    """Latest sign of Claude at work: its heartbeat, or any file written in a card folder (two levels deep)."""
    ts = [0.0]
    try:
        ts.append((FB / "claude_heartbeat").stat().st_mtime)
    except OSError:
        pass
    for card in CARDS.glob("*"):
        for p in [card, *card.glob("*"), *card.glob("*/*")]:
            try:
                ts.append(p.stat().st_mtime)
            except OSError:
                pass
    return max(ts)


def status_of(note: Path) -> str:
    m = STATUS_RE.search(note.read_text(errors="replace"))
    return m.group(1).strip().split()[0].lower() if m else ""


def open_notes() -> list[Path]:
    return sorted((p for p in (FB / "handoff").glob("*.md") if status_of(p) == "open"), key=lambda p: p.stat().st_mtime)


def set_status(note: Path, status: str) -> None:
    note.write_text(STATUS_RE.sub(f"STATUS: {status}", note.read_text(errors="replace"), count=1))


def field(note: Path, name: str) -> str:
    m = re.search(FIELD_RE.format(name), note.read_text(errors="replace"), re.M)
    return m.group(1) if m else ""


def llm_up() -> bool:
    try:
        with urllib.request.urlopen(LLM + "/models", timeout=5) as r:
            return r.status == 200
    except OSError:
        return False


def mail(body: str, thread: str) -> None:
    try:
        team_mail.main(["send", "--as", "qwen", "--thread", thread, body])
    except SystemExit:
        pass  # mail is best effort; the note and the run log are the record


def roster(fn: str, *args, **kw) -> None:
    """Tell the panel's agent roster what this run is doing (best effort: the status is never the record)."""
    try:
        getattr(agent_status, fn)("qwen-fallback", *args, **kw)
    except (ValueError, OSError):
        pass


def tail(text: str, n: int = 40) -> str:
    return "\n".join(text.splitlines()[-n:])[-4000:]


class Card:
    """Everything the model may do, bound to the one note it claimed. No paths or commands come from the model."""

    def __init__(self, note: Path):
        self.note, self.slug = note, note.stem
        self.steps = note.parent / self.slug
        self.card_id = field(note, "CARD")
        d = field(note, "DIR")
        self.dir = Path(d) if d.startswith(str(CARDS) + "/") else CARDS / self.slug
        self.result: tuple[str, str] | None = None
        # The A/B showed a small model will log a failed step as a success, so the runner (not the model) enforces
        # the plan: only steps named in NEXT, in order, a failed step stops the run (one retry), real exit codes logged.
        nxt = note.read_text(errors="replace").split("NEXT:", 1)[-1].split("NOT ALLOWED:", 1)[0].split("LOG:", 1)[0]
        self.plan = sorted({s for s in re.findall(r"\b\d{2}-[a-z0-9_-]+\.sh\b", nxt) if (self.steps / s).is_file()})
        self.passed: list[str] = []
        self.failed: str | None = None
        self.retried = False

    def log(self, text: str, who: str = "qwen") -> str:
        line = f"- {time.strftime('%H:%M')} [{who}] {' '.join(str(text).split())[:300]}\n"
        with open(self.note, "a") as f:
            f.write(line)
        return "logged"

    def gate(self, step: str) -> str | None:
        """Why the runner refuses this step now, or None."""
        if step not in self.plan:
            return f"refused: '{step}' is not a step in this note's NEXT list ({', '.join(self.plan) or 'none'})"
        if self.failed:
            if step != self.failed:
                return f"refused: {self.failed} failed; retry it once or finish blocked"
            if self.retried:
                return f"refused: {step} already failed twice; finish blocked"
        todo = [s for s in self.plan if s not in self.passed]
        if step in self.passed:
            return f"refused: {step} already succeeded"
        if todo and step != todo[0]:
            return f"refused: run the steps in order; next is {todo[0]}"
        return None

    def run_step(self, step: str) -> str:
        why = self.gate(step)
        if why:
            return why
        if self.failed == step:
            self.retried = True
        try:
            r = subprocess.run(["bash", str(self.steps / step)], cwd=str(self.dir), capture_output=True, text=True,
                               timeout=STEP_LIMIT_S)
            code, out = r.returncode, tail(r.stdout + r.stderr)
        except subprocess.TimeoutExpired:
            code, out = 124, f"timeout after {STEP_LIMIT_S // 60} min"
        last = (out.strip().splitlines() or [""])[-1]
        self.log(f"{step} exit {code}: {last}", who="runner")
        if code == 0:
            self.passed.append(step)
            self.failed = None
        else:
            self.failed = step
        return f"exit {code}\n{out}"

    def verdict(self, status: str, reason: str) -> tuple[str, str]:
        """The runner has the last word: 'done' only when every planned step succeeded."""
        missing = [s for s in self.plan if s not in self.passed]
        if status == "done" and (self.failed or missing):
            return "blocked", f"runner: not all steps succeeded (failed: {self.failed or '-'}, not run: " \
                              f"{', '.join(missing) or '-'}); model said done: {reason}"
        return status, reason

    def call(self, name: str, args: dict) -> str:
        roster("step", f"{name} {args.get('name', '')}".strip())
        if name == "read_note":
            return self.note.read_text(errors="replace")[-6000:]
        if name == "list_steps":
            return "\n".join(self.plan) or "(no step scripts in NEXT)"
        if name == "run_step":
            return self.run_step(str(args.get("name", "")))
        if name == "score_card":
            r = subprocess.run([PY, SCORE, str(self.dir)], capture_output=True, text=True, timeout=STEP_LIMIT_S,
                               env=dict(os.environ, HOUSE_ACID="0"))
            return f"exit {r.returncode}\n{tail(r.stdout + r.stderr, 30)}"
        if name == "kanban_comment":
            if not re.fullmatch(r"t_[0-9a-f]{6,}", self.card_id):
                return "refused: the note has no valid CARD id"
            r = subprocess.run([*KANBAN, "comment", self.card_id, "[local-qwen] " + str(args.get("text", ""))[:1500]],
                               capture_output=True, text=True, timeout=60)
            return "commented" if r.returncode == 0 else f"kanban error: {tail(r.stderr, 5)}"
        if name == "send_mail":
            mail(str(args.get("text", ""))[:1500], f"card-{self.slug}")
            return "sent"
        if name == "append_log":
            return self.log(args.get("text", ""))
        if name == "finish":
            st = args.get("status")
            if st not in ("done", "blocked"):
                return "refused: status must be done or blocked"
            self.result = self.verdict(st, str(args.get("reason", ""))[:500])
            return "finished"
        return f"refused: unknown tool '{name}'"


def chat(messages: list) -> dict:
    tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
              "parameters": {"type": "object", "properties": t["params"], "required": list(t["params"])}}} for t in TOOLS]
    body = {"model": MODEL, "messages": messages, "tools": tools, "tool_choice": "auto", "temperature": 0.2,
            "max_tokens": 1024}
    req = urllib.request.Request(LLM + "/chat/completions", json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())["choices"][0]["message"]


def agent(card: Card, log) -> None:
    soul = (FB / "FALLBACK_SOUL.md").read_text()
    messages = [{"role": "system", "content": soul},
                {"role": "user", "content": f"Claude is out of its usage limit. Continue card {card.slug}: read the "
                                            f"note, then do its NEXT steps with your tools, then call finish."}]
    deadline = time.time() + RUN_LIMIT_S
    for _ in range(MAX_TURNS):
        if card.result or time.time() > deadline:
            return
        msg = chat(messages)
        messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
        calls = msg.get("tool_calls") or []
        if not calls:
            log.write(f"model: {msg.get('content')}\n")
            messages.append({"role": "user", "content": "Use your tools. Call finish when you are done or blocked."})
            continue
        for c in calls:
            fn = c["function"]["name"]
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            out = card.call(fn, args)
            log.write(f"tool {fn} {json.dumps(args)[:300]} -> {out[:300]!r}\n")
            log.flush()
            messages.append({"role": "tool", "tool_call_id": c.get("id", fn), "content": out})


def run(note: Path, dry: bool) -> int:
    card = Card(note)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    logp = FB / "runs" / f"{stamp}-{card.slug}.log"
    if dry:
        print(f"would continue {card.slug} (card {card.card_id or '?'}) with {MODEL}")
        return 0
    nxt = note.read_text(errors="replace").split("NEXT:", 1)[-1].split("NOT ALLOWED:", 1)[0]
    if JUDGEMENT_RE.search(nxt) or not card.plan:
        why = "needs Claude: the NEXT steps ask for a judgement call" if card.plan else "no runnable steps in NEXT"
        set_status(note, "blocked")
        card.log(f"not started: {why}", who="runner")
        return 0
    set_status(note, f"claimed-by-qwen {stamp}")
    mail(f"Claude has been quiet for {STALE_S // 60}+ min, so the local Qwen fallback is continuing card {card.slug} "
         f"from its hand-off note. Run log: {logp}", f"card-{card.slug}")
    logp.parent.mkdir(parents=True, exist_ok=True)
    try:
        agent_status.start("qwen-fallback", "qwen", card.slug, f"continue hand-off note {card.slug}")
    except (ValueError, OSError):
        pass
    with open(logp, "w") as log:
        try:
            agent(card, log)
        except Exception as e:  # noqa: BLE001 - any model/network error ends the run as stuck
            log.write(f"run error: {type(e).__name__}: {e}\n")
    status, why = card.result or ("stuck", "the run ended without finish")
    set_status(note, status)
    roster("done", "ok" if status == "done" else "failed", f"{status}: {why}"[:160])
    card.log(f"run ended: {status}: {why}", who="runner")
    mail(f"Qwen fallback ended card {card.slug}: {status} ({why}). Claude: review the note and {logp}.",
         f"card-{card.slug}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    m = mode()
    if m == "off":
        return 0
    quiet = time.time() - newest_activity()
    if m != "force" and quiet < STALE_S:
        print(f"Claude active {int(quiet // 60)} min ago: nothing to do")
        return 0
    notes = open_notes()
    if not notes:
        print("no open hand-off notes")
        return 0
    if not llm_up():
        print("local model not answering on vLLM: skip")
        return 0
    lock = FB / "run.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if time.time() - lock.stat().st_mtime < RUN_LIMIT_S + 300:
            print("a fallback run is already going")
            return 0
        lock.unlink()  # stale lock from a crashed run
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, str(os.getpid()).encode())
        return run(notes[0], a.dry_run)
    finally:
        os.close(fd)
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
