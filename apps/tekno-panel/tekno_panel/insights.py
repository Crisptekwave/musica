"""Activity events and alerts derived from the live sources (pure functions, no I/O)."""
from __future__ import annotations

import datetime as dt
import hashlib
import json

NOISY_JOURNAL = ("Consumed ", "Failed with result 'exit-code'", "code=exited, status=143")
KANBAN_KINDS = {"created": "created", "completed": "done", "blocked": "blocked", "commented": "comment",
                "unblocked": "unblocked", "review_requested": "review", "archived": "archived", "claimed": "claimed"}


def _iso_ts(s) -> float | None:
    if isinstance(s, (int, float)):
        return float(s)
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _h(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:12]


def events(board: dict, disk: dict, ace: dict, journal: list, chat: dict, audit: list, alerts: list) -> list[dict]:
    out: list[dict] = []
    tasks = {t.id: t for t in board.get("tasks", [])}
    for e in board.get("events", []):
        kind = KANBAN_KINDS.get(e.get("kind"))
        if not kind:
            continue
        t = tasks.get(e.get("task_id"))
        try:
            payload = json.loads(e.get("payload") or "{}") or {}
        except ValueError:
            payload = {}
        detail = payload.get("summary") or payload.get("reason") or ""
        name = (t.title if t else e.get("task_id")) or "?"
        out.append({"id": f"kb:{e['id']}", "ts": e.get("created_at") or 0, "kind": "kanban",
                    "card": t.slug if t else None, "level": "warn" if kind == "blocked" else "info",
                    "text": f"{name} · {kind}" + (f": {detail}" if detail else "")})
    for slug, info in (disk.get("cards") or {}).items():
        for r in info.get("runs") or []:
            for st in r.get("steps") or []:
                if st.get("at"):
                    out.append({"id": f"run:{slug}:{r['name']}:{st['step']}:{int(st['at'])}", "ts": st["at"], "kind": "run",
                                "card": slug, "level": "info", "text": f"{r['name']} → {st['step']}"})
            if r.get("finished"):
                bad = r.get("exit") not in (0, None)
                out.append({"id": f"runend:{slug}:{r['name']}:{int(r['mtime'])}", "ts": r["mtime"], "kind": "run", "card": slug,
                            "level": "warn" if bad else "ok",
                            "text": f"{r['name']} finished: {r.get('verdict') or ('exit ' + str(r.get('exit')))}"
                                    + (f" · draft {r['draft']}" if r.get("draft") is not None else "")
                                    + (f" · {'; '.join(r['fails'][:2])}" if r.get("fails") else "")})
    for a in disk.get("audio") or []:
        if a.get("kind") == "render":
            out.append({"id": f"file:{a.get('root')}:{a['card']}:{a['path']}:{int(a['mtime'])}", "ts": a["mtime"],
                        "kind": "render", "card": a["card"], "level": "info",
                        "text": f"new {a['stage'].lower()} render {a['path'].split('/')[-1]}"})
    d = (ace or {}).get("last_done")
    if d and d.get("at"):
        out.append({"id": f"ace:{int(d['at'])}", "ts": d["at"], "kind": "ace", "card": None, "level": "info",
                    "text": f"ACE take saved" + (f" in {d['took_s']:.0f} s" if d.get("took_s") else "")
                            + (f" · seed {d['seed']}" if d.get("seed") else "")})
    err = (ace or {}).get("last_error")
    if err and err.get("at"):
        out.append({"id": f"acerr:{int(err['at'])}:{_h(err['text'])}", "ts": err["at"], "kind": "ace", "card": None,
                    "level": "warn", "text": f"ACE error: {err['text']}"})
    for r in journal or []:
        if any(n in r["text"] for n in NOISY_JOURNAL) or not r.get("at"):
            continue
        out.append({"id": f"j:{int(r['at'])}:{_h(r['unit'], r['text'])}", "ts": r["at"], "kind": "service", "card": None,
                    "level": "warn" if "Fail" in r["text"] else "info",
                    "text": r["text"].split(" - ")[0]})
    for m in (chat or {}).get("messages") or []:
        ts = _iso_ts(m.get("ts"))
        if not ts:
            continue
        th = str(m.get("thread") or "")
        out.append({"id": f"mail:{m.get('project')}:{m.get('id')}", "ts": ts, "kind": "mail",
                    "card": th[5:] if th.startswith("card-") else None, "level": "info",
                    "text": f"{m.get('from')}: {(m.get('subject') or m.get('body') or '')[:160]}"})
    for a in audit or []:
        out.append({"id": f"act:{a.get('ts')}:{a.get('action')}", "ts": a.get("ts") or 0, "kind": "action",
                    "card": (a.get("params") or {}).get("card"), "level": "ok" if a.get("ok") else "warn",
                    "text": f"you: {a.get('summary') or a.get('action')}" + ("" if a.get("ok", True) else " (failed)")})
    for al in alerts or []:
        out.append({"id": f"alert:{al['key']}:{int(al['since'])}", "ts": al["since"], "kind": "alert", "card": al.get("card"),
                    "level": al["level"], "text": f"{al['title']}" + (f": {al['detail']}" if al.get("detail") else "")})
    return out


def alerts(snap: dict, now: float) -> list[dict]:
    """Current problems, most severe first. 'since' is filled in by the caller (first time seen)."""
    out = []
    h = snap.get("health") or {}
    d = h.get("disk") or {}
    if d.get("ok") and d.get("warn"):
        out.append({"key": "disk", "level": "crit", "title": f"Disk low: {d['free_gb']} GB free",
                    "detail": f"under {d['warn_gb']} GB on {d['path']}: the watchdog pauses the machine"})
    for s in h.get("services") or []:
        if s.get("status") in ("down", "failed"):
            out.append({"key": f"svc:{s['name']}", "level": "crit", "title": f"{s['name']} is {s['status']}", "detail": s.get("via")})
    for k, v in (snap.get("sources") or {}).items():
        if v and v.get("ok") is False:
            out.append({"key": f"src:{k}", "level": "warn", "title": f"source {k} unavailable", "detail": v.get("error")})
    g = h.get("gpu") or {}
    if g.get("ok") and (g.get("temp") or 0) >= 85:
        out.append({"key": "gpu-hot", "level": "warn", "title": f"GPU at {g['temp']:.0f} °C", "detail": None})
    a = h.get("ace") or {}
    if a.get("last_error") and now - (a["last_error"].get("at") or 0) < 1800:
        out.append({"key": "ace-err", "level": "warn", "title": "ACE error in the last 30 min", "detail": a["last_error"]["text"]})
    m = snap.get("machine") or {}
    if m.get("paused"):
        out.append({"key": "paused", "level": "info", "title": "Machine paused", "detail": m.get("reason")})
    for t in snap.get("tracks") or []:
        if t.get("state") == "failed":
            why = (t.get("current_task") or {}).get("last_error") or ""
            out.append({"key": f"failed:{t['slug']}", "level": "warn", "card": t["slug"],
                        "title": f"{t['slug']} stuck in {t['lane']}", "detail": why[:160] or None})
        if t.get("board_behind") and now - (t.get("board_activity") or now) > 3600:
            out.append({"key": f"behind:{t['slug']}", "level": "info", "card": t["slug"],
                        "title": f"Board behind disk for {t['slug']}", "detail": "manual runs happened without updating the kanban"})
        if t.get("state") == "review":
            out.append({"key": f"review:{t['slug']}", "level": "info", "card": t["slug"],
                        "title": f"{t['slug']} waits for your review", "detail": None})
    if (h.get("tracks_today") or 0) >= (h.get("tracks_cap") or 99):
        out.append({"key": "cap", "level": "info", "title": "Daily track cap reached", "detail": f"{h['tracks_today']}/{h['tracks_cap']}"})
    order = {"crit": 0, "warn": 1, "info": 2}
    return sorted(out, key=lambda x: order.get(x["level"], 3))
