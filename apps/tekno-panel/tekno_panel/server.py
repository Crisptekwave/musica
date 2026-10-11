"""TEKNO CONTROL web server: FastAPI + WebSocket live push."""
from __future__ import annotations

import asyncio
import json
import math
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response

from . import demo, palette, procs, review, studio
from .control import ActionError
from .auth import COOKIE, SESSION_TTL, Auth
from .config import Settings, load_settings
from .files import AUDIO_EXT

# handoff deliverables besides audio (DAW projects, MIDI, reports): served as downloads only
HANDOFF_DOC_EXT = frozenset({".rpp", ".mid", ".md", ".txt", ".json", ".lua", ".zip"})
from .state import Collector

STATIC = Path(__file__).resolve().parent.parent / "static"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
       "media-src 'self' blob:; connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def _finite(o):
    """Replace NaN / Infinity (not valid JSON) anywhere in a snapshot with null."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _finite(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_finite(v) for v in o]
    return o


def dumps(snap: dict) -> str:
    try:
        return json.dumps(snap, default=str, allow_nan=False)
    except ValueError:
        return json.dumps(_finite(snap), default=str, allow_nan=False)


def create_app(settings: Settings | None = None, start_collector: bool = True, runner=None, action_runner=None) -> FastAPI:
    s = settings or load_settings()
    s.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    auth = Auth(s.state_dir)
    collector = Collector(s, runner=action_runner)

    @asynccontextmanager
    async def lifespan(app):
        if start_collector and not s.demo:
            collector.start()
        yield
        collector.stop()

    app = FastAPI(title="TEKNO CONTROL", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.auth, app.state.collector = s, auth, collector

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith(("/api/", "/media", "/palette/media", "/reaper/")):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    def authed(request: Request) -> bool:
        return auth.valid(request.cookies.get(COOKIE))

    def need_auth(request: Request):
        if not authed(request):
            raise HTTPException(401, "login required")

    def want_demo(flag: str | None) -> bool:
        return s.demo or flag in ("1", "true")

    def snapshot(demo_flag: bool) -> dict:
        if demo_flag:
            return demo.snapshot()
        snap = collector.get()
        if not snap.get("ts"):
            snap = collector.refresh()
        return snap

    # ---------- auth ----------
    @app.post("/api/login")
    async def login(request: Request):
        ip = request.client.host if request.client else "?"
        if auth.locked(ip):
            return JSONResponse({"ok": False, "error": "Too many tries. Wait 5 minutes."}, status_code=429)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        if not auth.check(ip, str(body.get("passcode", ""))):
            return JSONResponse({"ok": False, "error": "Wrong passcode"}, status_code=401)
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, auth.issue(), max_age=SESSION_TTL, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", path="/")
        return resp

    @app.post("/api/logout")
    async def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @app.get("/api/session")
    async def session(request: Request):
        return {"authed": authed(request), "demo_default": s.demo}

    # ---------- data ----------
    @app.get("/api/state")
    async def state(request: Request, demo: str | None = None):
        need_auth(request)
        snap = await asyncio.to_thread(snapshot, want_demo(demo))
        return Response(dumps(snap), media_type="application/json")

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        if not auth.valid(websocket.cookies.get(COOKIE)):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        use_demo = want_demo(websocket.query_params.get("demo"))
        last = None
        try:
            while True:
                snap = await asyncio.to_thread(snapshot, use_demo)
                if snap.get("ts") != last:
                    last = snap.get("ts")
                    await websocket.send_text(dumps(snap))
                await asyncio.sleep(1.0 if use_demo else s.push_interval)
        except (WebSocketDisconnect, RuntimeError):
            return

    @app.post("/api/review/{task_id}")
    async def do_review(task_id: str, request: Request):
        need_auth(request)
        if request.headers.get("x-tekno") != "1":  # CSRF guard on top of SameSite=Strict
            raise HTTPException(403, "missing x-tekno header")
        body = await request.json()
        decision, note = str(body.get("decision", "")), str(body.get("note", ""))
        if body.get("demo") or s.demo:
            try:
                return {"ok": True, "demo": True, "comment": review.build_comment(decision, note)}
            except review.ReviewError as e:
                raise HTTPException(e.status, str(e))
        try:
            kw = {"runner": runner} if runner else {}
            text = await asyncio.to_thread(review.submit, s.kanban_path, s.hermes_bin, s.board, task_id,
                                           decision, note, **kw)
        except review.ReviewError as e:
            raise HTTPException(e.status, str(e))
        return {"ok": True, "comment": text}

    # ---------- controls (allowlisted, audited; danger tier needs the passcode again) ----------
    @app.post("/api/action")
    async def action(request: Request):
        need_auth(request)
        if request.headers.get("x-tekno") != "1":
            raise HTTPException(403, "missing x-tekno header")
        if not s.controls:
            raise HTTPException(403, "controls are switched off (TEKNO_CONTROLS=0)")
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        name, params = str(body.get("action", "")), body.get("params") or {}
        ip = request.client.host if request.client else "?"
        if body.get("demo") or s.demo:
            return {"ok": True, "demo": True, "summary": f"Demo: {name} not run"}
        if auth.locked(ip):
            raise HTTPException(429, "Too many wrong passcodes. Wait 5 minutes.")
        running = await asyncio.to_thread(procs.scan, s.proc_root)
        try:
            res = await asyncio.to_thread(collector.controller.run, name, params, running, ip,
                                          lambda code: auth.check(ip, code))
        except ActionError as e:
            raise HTTPException(e.status, str(e))
        collector._slow_at = 0  # rescan now so the result shows on the next push
        return res

    @app.get("/api/audit")
    async def audit_log(request: Request):
        need_auth(request)
        return {"audit": collector.controller.audit_tail(200)}

    # ---------- history ----------
    @app.get("/api/activity")
    async def activity(request: Request, limit: int = 100, before: float | None = None, kind: str | None = None,
                       card: str | None = None, q: str | None = None):
        need_auth(request)
        return {"events": await asyncio.to_thread(collector.history.events, limit, before, kind, card, q)}

    @app.get("/api/metrics")
    async def metrics(request: Request, range: str = "1h"):
        need_auth(request)
        return Response(dumps(await asyncio.to_thread(collector.history.series, range)), media_type="application/json")

    # ---------- studio (view-only live REAPER frames) ----------
    @app.get("/api/reaper")
    async def reaper(request: Request):
        need_auth(request)
        await asyncio.to_thread(studio.touch_viewing, s.data_dir)  # wakes the frame grabber (it idles otherwise)
        data = await asyncio.to_thread(studio.instances, s.data_dir, s.cards_path)
        tracks = {t["slug"]: t for t in collector.get().get("tracks") or []}
        for i in data["instances"]:
            t = tracks.get(i["slug"])
            i["card"] = {k: t.get(k) for k in ("lane", "state", "progress")} if t else None
        return Response(dumps(data), media_type="application/json")

    @app.get("/reaper/frame/{display}.jpg")
    async def reaper_frame(display: str, request: Request):
        need_auth(request)
        p = studio.frame_path(s.data_dir, display)
        if p is None or not p.is_file():
            raise HTTPException(404, "no frame")
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    # ---------- palette ----------
    @app.get("/api/palette")
    async def palette_index(request: Request):
        need_auth(request)
        return Response(dumps(await asyncio.to_thread(palette.load, s.palette_dir)), media_type="application/json")

    # ---------- media ----------
    def safe_file(root: Path, rel: str, exts=AUDIO_EXT) -> Path:
        root = root.resolve()
        p = (root / rel).resolve()
        if not p.is_relative_to(root) or p.suffix.lower() not in exts or not p.is_file():
            raise HTTPException(404, "not found")
        return p

    @app.get("/media/{card}/{path:path}")
    async def media(card: str, path: str, request: Request):
        need_auth(request)
        if "/" in card or card.startswith("."):
            raise HTTPException(404, "not found")
        p = safe_file(s.cards_path, f"{card}/{path}")
        return FileResponse(p, headers={"Cache-Control": "private, max-age=60"})

    @app.get("/palette/media/{path:path}")
    async def palette_media(path: str, request: Request):
        need_auth(request)
        p = safe_file(s.palette_dir, path)  # inside the palette folder, audio extensions only
        return FileResponse(p, headers={"Cache-Control": "private, max-age=300"})

    @app.get("/handoff/{project}/{path:path}")
    async def handoff_media(project: str, path: str, request: Request):
        need_auth(request)
        if "/" in project or project.startswith("."):
            raise HTTPException(404, "not found")
        p = safe_file(s.handoff_path, f"{project}/{path}", AUDIO_EXT | HANDOFF_DOC_EXT)
        if p.suffix.lower() in HANDOFF_DOC_EXT:  # project files and reports: download, never render
            return FileResponse(p, filename=p.name, media_type="application/octet-stream",
                                headers={"Cache-Control": "private, max-age=60", "X-Content-Type-Options": "nosniff"})
        return FileResponse(p, headers={"Cache-Control": "private, max-age=60"})

    @app.get("/media-ref")
    async def media_ref(request: Request):
        need_auth(request)
        p = s.reference_path
        if not p.is_file():
            raise HTTPException(404, "reference not found")
        return FileResponse(p)

    @app.get("/demo-audio/{name}.wav")
    async def demo_audio(name: str, request: Request):
        need_auth(request)
        if not name.replace("_", "").isalnum():
            raise HTTPException(404)
        data = await asyncio.to_thread(demo.demo_wav, name)
        return Response(data, media_type="audio/wav", headers={"Cache-Control": "private, max-age=3600"})

    # ---------- static / PWA ----------
    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/desktop")
    async def desktop():
        return FileResponse(STATIC / "desktop.html", headers={"Cache-Control": "no-cache"})

    @app.get("/sw.js")
    async def sw():
        return FileResponse(STATIC / "sw.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    @app.get("/manifest.webmanifest")
    async def manifest():
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "ts": time.time()}

    @app.get("/static/{path:path}")
    async def static(path: str):
        root = STATIC.resolve()
        p = (root / path).resolve()
        if not p.is_relative_to(root) or not p.is_file():
            raise HTTPException(404)
        return FileResponse(p, headers={"Cache-Control": "no-cache"})

    return app


def main():  # pragma: no cover
    import uvicorn
    s = load_settings()
    uvicorn.run(create_app(s), host=s.host, port=s.port, proxy_headers=False, log_level="info", ws_ping_interval=20)


if __name__ == "__main__":  # pragma: no cover
    main()
