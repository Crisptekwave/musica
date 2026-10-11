"""TEKNO CONTROL web server: FastAPI + WebSocket live push."""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response

from . import demo, review
from .auth import COOKIE, SESSION_TTL, Auth
from .config import Settings, load_settings
from .files import AUDIO_EXT
from .state import Collector

STATIC = Path(__file__).resolve().parent.parent / "static"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
       "media-src 'self' blob:; connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def create_app(settings: Settings | None = None, start_collector: bool = True, runner=None) -> FastAPI:
    s = settings or load_settings()
    s.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    auth = Auth(s.state_dir)
    collector = Collector(s)

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
        if request.url.path.startswith(("/api/", "/media")):
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
        return await asyncio.to_thread(snapshot, want_demo(demo))

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
                    await websocket.send_text(json.dumps(snap, default=str))
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

    # ---------- media ----------
    def safe_file(root: Path, rel: str) -> Path:
        root = root.resolve()
        p = (root / rel).resolve()
        if not p.is_relative_to(root) or p.suffix.lower() not in AUDIO_EXT or not p.is_file():
            raise HTTPException(404, "not found")
        return p

    @app.get("/media/{card}/{path:path}")
    async def media(card: str, path: str, request: Request):
        need_auth(request)
        if "/" in card or card.startswith("."):
            raise HTTPException(404, "not found")
        p = safe_file(s.cards_path, f"{card}/{path}")
        return FileResponse(p, headers={"Cache-Control": "private, max-age=60"})

    @app.get("/handoff/{project}/{path:path}")
    async def handoff_media(project: str, path: str, request: Request):
        need_auth(request)
        if "/" in project or project.startswith("."):
            raise HTTPException(404, "not found")
        p = safe_file(s.handoff_path, f"{project}/{path}")
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
