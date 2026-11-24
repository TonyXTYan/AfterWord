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

from . import alerts as alerting
from . import state
from .lab import LabError, pronounce_view, save_pronounce
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
    async def sessions(limit: int = 100):
        activity = await asyncio.to_thread(daemon.events.session_activity, 5000)
        return {
            "muted_all": daemon.muted_all,
            "profiles": list(daemon.store.get().profiles),
            "sessions": state.list_sessions(activity, limit),
        }

    @app.post("/api/sessions/{session_id}")
    async def set_session(session_id: str, request: Request):
        body = await json_body(request)
        if not isinstance(body, dict) or not ({"profile", "muted"} & set(body)):
            raise LabError(400, "send profile and/or muted")
        try:
            state.set_session(
                session_id,
                profile=body.get("profile") if "profile" in body else ...,
                muted=body.get("muted") if "muted" in body else ...,
                source="gui",
                known_profiles=list(daemon.store.get().profiles),
            )
        except state.SessionError as exc:
            raise LabError(400, str(exc)) from None
        daemon.apply_session_change(session_id, state.get_session(session_id))
        daemon.publish_session(session_id)
        return {"session": daemon.session_activity_view(session_id)}

    @app.post("/api/mute")
    async def mute(request: Request):
        body = await json_body(request)
        if not isinstance(body, dict) or not isinstance(body.get("muted"), bool):
            raise LabError(400, "send {\"muted\": true|false}")
        return {"muted_all": daemon.set_muted_all(body["muted"])}

    @app.post("/api/queue/remove/{event_id}")
    async def queue_remove(event_id: int):
        if not daemon.remove_queued(event_id):
            raise LabError(404, f"#{event_id} is not waiting in the queue")
        return {"removed": event_id}

    @app.get("/api/history")
    async def history(limit: int = 50, before: int | None = None):
        return {"items": await asyncio.to_thread(daemon.events.history, limit, before)}

    @app.get("/api/stats")
    async def stats(limit: int = 200):
        return await asyncio.to_thread(daemon.events.stats, limit)

    def config_view():
        store = daemon.store
        try:
            text = store.path.read_text(encoding="utf-8")[:1_000_000] if store.path.is_file() else ""
        except OSError as exc:
            text = f"# could not read {store.path}: {exc}"
        return {
            "path": str(store.path),
            "text": text,
            "error": store.error,
            "warnings": list(store.warnings),
            "loaded_at": store.loaded_at,
            "synth": daemon.synth.name,
            "configured_synth": store.get().daemon.synth,
            "engine": daemon.engine.status(),
        }

    @app.get("/api/config")
    async def config():
        return await asyncio.to_thread(config_view)

    @app.post("/api/config/reload")
    async def config_reload():
        daemon.store.reload(force=True)
        return await asyncio.to_thread(config_view)

    @app.get("/api/pronounce")
    async def pronounce_get():
        return await asyncio.to_thread(pronounce_view, daemon.store)

    @app.post("/api/pronounce")
    async def pronounce_set(request: Request):
        body = await json_body(request)
        return await asyncio.to_thread(save_pronounce, daemon.store, body)

    # -- spoken alerts (contract: docs/gui-api.md, "Alerts") ---------------

    generating = asyncio.Lock()

    def alerts_view():
        cfg = daemon.store.get()
        phrases = daemon.alerts
        pools = phrases.pools()
        return {
            "settings": cfg.alerts.as_dict(),
            "events": list(alerting.ALERT_EVENTS),
            "placeholders": {k: list(v) for k, v in alerting.PLACEHOLDERS.items()},
            "limits": {"phrase": alerting.PHRASE_MAX, "pool": alerting.POOL_MAX, "count": alerting.COUNT_MAX},
            "profiles": [{"name": p.name, "lang": p.lang, "pool_lang": alerting.pool_lang(p), "style": p.style,
                          "persona": p.persona} for p in cfg.profiles.values()],
            "default_profile": alerting.alert_profile_name(cfg, None),
            "pools": pools,
            "custom": {lang: sorted(e for e, v in ev.items() if v) for lang, ev in phrases.file_pools.items()},
            "builtin_langs": sorted(alerting.BUILTIN),
            "path": str(phrases.path),
            "error": phrases.error,
            "warnings": list(phrases.warnings) + [w for w in daemon.store.warnings if w.startswith("alerts")],
            "generating": generating.locked(),
        }

    def alert_error(exc: alerting.AlertError) -> LabError:
        return LabError(400, str(exc))

    @app.get("/api/alerts")
    async def alerts_get():
        return await asyncio.to_thread(alerts_view)

    @app.post("/api/alerts")
    async def alerts_set(request: Request):
        body = await json_body(request)
        try:
            fields = alerting.settings_body(body)
            await asyncio.to_thread(alerting.write_settings, daemon.store.path, fields, list(daemon.store.get().profiles))
        except alerting.AlertError as exc:
            raise alert_error(exc) from None
        daemon.store.reload(force=True)
        return await asyncio.to_thread(alerts_view)

    @app.post("/api/alerts/phrases")
    async def alerts_phrases(request: Request):
        body = await json_body(request)
        if not isinstance(body, dict) or not isinstance(body.get("lang"), str) \
                or not (body.get("pools") is None or isinstance(body.get("pools"), dict)):
            raise LabError(400, "send {\"lang\": \"en\", \"pools\": {event: [phrase, ...]}} (pools null: built-in)")
        try:
            await asyncio.to_thread(daemon.alerts.save_lang, body["lang"], body.get("pools"))
        except alerting.AlertError as exc:
            raise alert_error(exc) from None
        return await asyncio.to_thread(alerts_view)

    @app.post("/api/alerts/generate")
    async def alerts_generate(request: Request):
        body = await json_body(request)
        if not isinstance(body, dict):
            raise LabError(400, "expected a JSON object")
        if generating.locked():
            raise LabError(409, "phrases are already being generated")
        async with generating:
            try:
                result = await asyncio.to_thread(
                    alerting.generate, daemon.store.get(), daemon.alerts, body.get("profile") or None,
                    body.get("count", 12), body.get("persona"), daemon.alert_runner,
                )
            except alerting.AlertError as exc:
                raise LabError(502 if isinstance(exc, alerting.GenerateFailed) else 400, str(exc)) from None
        return result

    @app.post("/api/alerts/hear")
    async def alerts_hear(request: Request):
        body = await json_body(request)
        if not isinstance(body, dict) or body.get("event") not in alerting.ALERT_EVENTS:
            raise LabError(400, f"send {{\"event\": one of {', '.join(alerting.ALERT_EVENTS)}}}")
        cfg = daemon.store.get()
        event = body["event"]
        name = body.get("profile") or alerting.alert_profile_name(cfg, None)
        if not isinstance(name, str) or name not in cfg.profiles:
            raise LabError(404, f"no profile named {name!r}" if name else "no profiles configured")
        lang = alerting.pool_lang(cfg.profiles[name])
        phrase = body.get("phrase")
        if phrase is not None:
            why = alerting.check_phrase(phrase, event)
            if why:
                raise LabError(400, f"phrase: {why}")
            used = alerting.lang_code(body.get("lang")) or lang  # the pool it was written for
        else:
            pool = daemon.alerts.pool(lang, event)
            used = lang if pool else "en"
            pool = pool or daemon.alerts.pool("en", event)
            import random

            phrase = random.choice(pool)
        text = alerting.fill(phrase.strip(), alerting.SAMPLE_VALUES.get(used, alerting.SAMPLE_VALUES["en"]), used)
        preview = {"text": text, "profile": name}
        if used != lang:
            preview["lang"] = used
        res = await daemon.lab.preview(preview)
        return {**res, "phrase": phrase, "alert_text": text, "event": event, "alert_profile": name}

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
    async def clip(event_id: int, download: int = 0):
        path = daemon.clips_dir / f"{event_id}.wav"
        if not path.is_file():
            raise LabError(404, "clip not kept")
        if download:
            return FileResponse(path, media_type="audio/wav", filename=f"afterword-{event_id}.wav")
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

    # -- seed explorer, stars, stability check (docs/lab-api.md) ---------

    @app.get("/api/lab/explore")
    async def lab_explore_get():
        return daemon.lab.explore_view()

    @app.post("/api/lab/explore")
    async def lab_explore_start(request: Request):
        return await daemon.lab.explore_start(await json_body(request))

    @app.post("/api/lab/explore/cursor")
    async def lab_explore_cursor(request: Request):
        return daemon.lab.explore_cursor(await json_body(request))

    @app.post("/api/lab/explore/stop")
    async def lab_explore_stop():
        return daemon.lab.explore_stop()

    @app.get("/api/lab/stars")
    async def lab_stars():
        return daemon.lab.stars_view()

    @app.post("/api/lab/stars")
    async def lab_star(request: Request):
        body = await json_body(request)
        return await asyncio.to_thread(daemon.lab.star, body)

    @app.get("/api/lab/stars/{name}")
    async def lab_star_audio(name: str):
        return FileResponse(daemon.lab.star_path(name.removesuffix(".wav")), media_type="audio/wav")

    @app.delete("/api/lab/stars/{star_id}")
    async def lab_unstar(star_id: str):
        return await asyncio.to_thread(daemon.lab.unstar, star_id)

    @app.post("/api/lab/stability")
    async def lab_stability(request: Request):
        return await daemon.lab.stability(await json_body(request))

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
