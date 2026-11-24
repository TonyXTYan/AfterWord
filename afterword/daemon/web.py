"""HTTP UI: one static page plus a small JSON API and an SSE stream.

Bound to 127.0.0.1 only. Two guards against other web pages driving it from
the user's browser:
- the Host header must be 127.0.0.1/localhost (stops DNS rebinding);
- POSTs need an `X-AfterWord: 1` header, which a cross-site form cannot
  send and a cross-site fetch cannot send without a CORS preflight that we
  never approve.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from . import state
from .lab import LabError
from .voices import CLIP_MAX_BYTES

if TYPE_CHECKING:
    from .server import Daemon

UI_INDEX = Path(__file__).resolve().parent.parent / "ui" / "index.html"
SSE_HEARTBEAT_S = 15.0
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def build_app(daemon: "Daemon", port: int) -> FastAPI:
    app = FastAPI(title="AfterWord", docs_url=None, redoc_url=None, openapi_url=None)
    allowed_origins = {f"http://{h}:{port}" for h in ALLOWED_HOSTS}

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if host.startswith("["):
            hostname, _, rest = host.partition("]")
            hostname += "]"
            host_port = rest[1:] if rest.startswith(":") else ""
        else:
            hostname, _, host_port = host.partition(":")
        if hostname not in ALLOWED_HOSTS or host_port not in ("", str(port)):
            return JSONResponse({"error": "bad host"}, status_code=403)
        if request.method not in ("GET", "HEAD") and request.headers.get("x-afterword") != "1":
            return JSONResponse({"error": "missing X-AfterWord header"}, status_code=403)
        # Other sites may not embed or read anything (e.g. <audio src=…/clips/N>).
        site = request.headers.get("sec-fetch-site")
        if site is not None and site not in ("same-origin", "none"):
            return JSONResponse({"error": "cross-site request refused"}, status_code=403)
        origin = request.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            return JSONResponse({"error": "cross-origin request refused"}, status_code=403)
        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    @app.get("/")
    async def index():
        return FileResponse(UI_INDEX, media_type="text/html")

    @app.get("/api/status")
    async def status():
        return daemon.status()

    @app.get("/api/events")
    async def events(limit: int = 200, before: int | None = None):
        return {"events": daemon.events.recent(limit=limit, before_id=before)}

    @app.get("/api/events/{event_id}")
    async def event(event_id: int):
        ev = daemon.events.get(event_id)
        if ev is None:
            raise HTTPException(404, "no such event")
        return ev

    @app.get("/api/sessions")
    async def sessions():
        return {"sessions": state.list_sessions()}

    @app.get("/api/queue")
    async def queue():
        return daemon.queue.snapshot()

    @app.post("/api/queue/skip")
    async def skip():
        return {"skipped": daemon.skip()}

    @app.post("/api/queue/stop")
    async def stop():
        return daemon.stop_all()

    @app.get("/api/clips/{event_id}")
    async def clip(event_id: int):
        path = daemon.clips_dir / f"{event_id}.wav"
        if not path.is_file():
            raise HTTPException(404, "clip not kept")
        return FileResponse(path, media_type="audio/wav")

    # -- voice lab (contract: docs/lab-api.md) ---------------------------

    @app.exception_handler(LabError)
    async def lab_error(request: Request, exc: LabError):
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        import logging

        logging.getLogger("afterword.web").exception("%s %s failed", request.method, request.url.path)
        return JSONResponse({"error": f"internal error: {type(exc).__name__}: {exc}"}, status_code=500)

    async def json_body(request: Request):
        try:
            return await request.json()
        except ValueError:
            raise LabError(400, "request body is not valid JSON") from None

    @app.get("/api/lab/catalog")
    async def lab_catalog():
        return await asyncio.to_thread(daemon.lab.catalog)

    @app.post("/api/lab/preview")
    async def lab_preview(request: Request):
        return await daemon.lab.preview(await json_body(request))

    @app.get("/api/lab/previews/{name}")
    async def lab_preview_audio(name: str):
        pid = name.removesuffix(".wav")
        path = daemon.lab.preview_path(pid)
        if path is None or not path.is_file():
            raise LabError(404, "preview not found")
        return FileResponse(path, media_type="audio/wav")

    @app.post("/api/lab/profiles")
    async def lab_save_profile(request: Request):
        return daemon.lab.save_profile(await json_body(request))

    @app.delete("/api/lab/profiles/{name}")
    async def lab_delete_profile(name: str):
        return daemon.lab.delete_profile(name)

    @app.post("/api/lab/clips")
    async def lab_add_clip(request: Request, name: str = "", consent: str = "", ref_text: str = "", overwrite: str = ""):
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > CLIP_MAX_BYTES:
            raise LabError(413, "clip is larger than 20 MB")
        chunks, total = [], 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > CLIP_MAX_BYTES:
                raise LabError(413, "clip is larger than 20 MB")
            chunks.append(chunk)
        truthy = ("1", "true", "yes", "on")
        return await daemon.lab.add_clip(
            name, b"".join(chunks), consent.lower() in truthy, ref_text, overwrite.lower() in truthy
        )

    @app.get("/api/lab/clips/{name}")
    async def lab_clip_audio(name: str):
        return FileResponse(daemon.lab.clip_path(name), media_type="audio/wav")

    @app.delete("/api/lab/clips/{name}")
    async def lab_delete_clip(name: str):
        return daemon.lab.delete_clip(name)

    @app.post("/api/lab/freeze")
    async def lab_freeze(request: Request):
        body = await json_body(request)
        return await asyncio.to_thread(daemon.lab.freeze, body)

    @app.post("/api/lab/warm")
    async def lab_warm(request: Request):
        return await daemon.lab.warm(await json_body(request))

    @app.get("/api/stream")
    async def stream(request: Request):
        q = daemon.events.subscribe()

        async def gen():
            try:
                yield "retry: 2000\n\n"
                yield _sse("queue", daemon.queue.snapshot())
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        msg = await asyncio.wait_for(q.get(), SSE_HEARTBEAT_S)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    if msg["type"] == "shutdown":
                        break
                    yield _sse(msg["type"], msg["data"])
            finally:
                daemon.events.unsubscribe(q)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    return app


def _sse(kind: str, data) -> str:
    body = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {kind}\ndata: {body}\n\n"
