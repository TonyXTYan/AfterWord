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
import os
import re
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from afterword import __version__, service
from afterword.hooks import common

from . import alerts as alerting
from . import state
from .config import lang_code
from .lab import LabError, pronounce_view, save_pronounce
from .log import LOG_FILE
from .voices import CLIP_MAX_BYTES

if TYPE_CHECKING:
    from .server import Daemon

UI_INDEX = Path(__file__).resolve().parent.parent / "ui" / "index.html"
SSE_HEARTBEAT_S = 15.0
# The page's CSP. Scripts need the per-response nonce; styles may be inline
# (the page sets style attributes and builds a <style> for the speaker colours
# and one inside <aw-audio>'s shadow root). Everything else is same-origin:
# fetch and EventSource (connect-src), clips from /clips (media-src), blob:
# URLs for the voice lab's recording, data: images and fonts.
PAGE_CSP = (
    "default-src 'none'; script-src 'nonce-{nonce}'; connect-src 'self'; img-src 'self' data:; "
    "media-src 'self' blob:; style-src 'self' 'unsafe-inline'; font-src 'self' data:; base-uri 'none'; "
    "form-action 'none'; object-src 'none'; frame-ancestors 'none'"
)
_SCRIPT_TAG_RE = re.compile(r"<script(?=[\s>])", re.IGNORECASE)
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
SPEAK_MAX_CHARS = 2000  # POST /api/speak, after trimming
SPEAK_SESSION = "gui"  # the session id of lines typed in the GUI
REPLAY_KEY_CHARS = 40  # characters of a block's preview used to find it again
SERVICE_CACHE_S = 3.0  # GET /api/service reuses launchctl's answers this long (the Config page polls)
RESTART_DELAY_S = 0.1  # after the 202 is sent, before the restart begins
INSTALL_COMMAND = "afterword install-service"
MANUAL_RESTART = "started by hand: restart it in its terminal, or run afterword install-service"


def render_export(lines: list[dict], fmt: str, one_session: bool) -> str:
    """Spoken lines (oldest first) as Markdown or plain text."""
    def when(ts):
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts or 0))
    text = lambda l: " ".join(str(l.get("text") or "").split())
    who = lambda l: l.get("profile") or "voice"
    if fmt == "txt":
        return "".join(f"[{when(l['ts'])}] {who(l)}: {text(l)}\n" for l in lines)
    out = ["# AfterWord transcript", ""]
    if one_session or not lines:
        out += [f"**{who(l)}** · {when(l['ts'])} — {text(l)}\n" for l in lines]
    else:
        groups: dict[str, list[dict]] = {}
        for l in lines:
            folder = Path(l.get("cwd") or "").name or str(l.get("session_id") or "unknown session")[:8]
            groups.setdefault(f"{folder} ({str(l.get('session_id') or '')[:8]})", []).append(l)
        for title, ls in groups.items():
            out += [f"## {title}", ""] + [f"**{who(l)}** · {when(l['ts'])} — {text(l)}\n" for l in ls]
    return "\n".join(out).rstrip() + "\n"


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
        nonce = secrets.token_urlsafe(18)
        page = await asyncio.to_thread(UI_INDEX.read_text, "utf-8")
        page = _SCRIPT_TAG_RE.sub(f'<script nonce="{nonce}"', page)
        return Response(page, media_type="text/html", headers={"Content-Security-Policy": PAGE_CSP.format(nonce=nonce)})

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
            "voice_in_use_s": common.VOICE_IN_USE_S,
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

    @app.get("/api/export")
    async def export(format: str = Query("md", pattern="^(md|txt)$"), session_id: str | None = None,
                     limit: int = Query(1000, ge=1, le=5000)):
        lines = await asyncio.to_thread(daemon.events.spoken_lines, session_id or None, limit)
        body = render_export(lines, format, bool(session_id))
        stem = "afterword-" + (re.sub(r"[^A-Za-z0-9_-]", "", session_id)[:12] if session_id else "all") + "-transcript"
        return Response(body, media_type="text/markdown; charset=utf-8" if format == "md" else "text/plain; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{stem}.{format}"'})

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

    # -- the daemon itself (contract: docs/gui-api.md, "Daemon") ---------

    probe_lock = asyncio.Lock()
    probed: dict = {"at": 0.0, "info": None}

    async def service_probe(force: bool = False) -> service.ServiceInfo:
        """launchctl's answers about the agent (with start at login), from a
        thread and reused for SERVICE_CACHE_S, so a polling page does not run
        launchctl every time; concurrent requests share one probe."""
        async with probe_lock:
            info = probed["info"]
            if force or info is None or time.monotonic() - probed["at"] >= SERVICE_CACHE_S:
                info = await asyncio.to_thread(service.service_info, True)
                probed.update(at=time.monotonic(), info=info)
            return info

    def storage_view() -> dict:
        cfg = daemon.store.get().daemon
        db = Path(daemon.events.db_path)
        db_bytes = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                db_bytes += os.stat(f"{db}{suffix}").st_size
            except OSError:
                pass
        clips, clip_bytes = 0, 0
        try:
            with os.scandir(daemon.clips_dir) as it:
                for entry in it:
                    if entry.name.endswith(".wav") and entry.is_file(follow_symlinks=False):
                        clips += 1
                        clip_bytes += entry.stat(follow_symlinks=False).st_size
        except OSError:
            pass
        return {
            "db_path": str(db), "db_bytes": db_bytes, "events": daemon.events.count(),
            "clips_dir": str(daemon.clips_dir), "clips": clips, "clips_bytes": clip_bytes,
            "keep_days": cfg.keep_days, "history": cfg.history, "config": str(daemon.store.path),
        }

    def log_paths() -> dict[str, Path]:
        return {"daemon": daemon.home / LOG_FILE, "service": daemon.home / service.LOG_NAME}

    async def service_view(force: bool = False) -> dict:
        info = await service_probe(force)
        svc_home = await asyncio.to_thread(service.plist_home) if info.installed else None
        storage = await asyncio.to_thread(storage_view)
        return {
            "label": service.LABEL,
            "installed": info.installed,
            "plist": info.plist,
            "loaded": info.loaded,
            "running": info.running,
            "state": info.state,
            "service_pid": info.pid,
            "last_exit": info.last_exit,
            "error": info.error,
            "login": info.login,
            "summary": info.summary(),
            "mode": service.daemon_mode(info),
            "service_home": svc_home,
            "other_home": bool(svc_home) and not service.same_home(svc_home, daemon.home),
            "install_command": INSTALL_COMMAND,
            "restart_exit": service.RESTART_EXIT,
            "logs": {k: str(v) for k, v in log_paths().items()},
            "version": __version__,
            "pid": os.getpid(),
            "started_at": daemon.started_at,
            "uptime_s": round(time.time() - daemon.started_at, 1),
            "home": str(daemon.home),
            "storage": storage,
        }

    @app.get("/api/service")
    async def service_get():
        return await service_view()

    @app.post("/api/service")
    async def service_set(request: Request):
        body = await json_body(request)
        if not isinstance(body, dict) or not isinstance(body.get("login"), bool):
            raise LabError(400, "send {\"login\": true|false}")
        info = await service_probe(force=True)
        if not info.installed:
            raise LabError(409, f"the service is not installed: run `{INSTALL_COMMAND}` first")
        svc_home = await asyncio.to_thread(service.plist_home)
        if svc_home and not service.same_home(svc_home, daemon.home):
            raise LabError(409, f"the installed service runs AfterWord home {svc_home}, not this daemon's "
                                f"{daemon.home}; change it from that daemon or with `afterword login`")
        r = await asyncio.to_thread(service.set_login, body["login"])
        if not r.ok:
            verb = "enable" if body["login"] else "disable"
            raise LabError(502, f"launchctl {verb} failed: {r.error()}")
        return await service_view(force=True)

    @app.post("/api/service/restart")
    async def service_restart():
        can_queue()  # 503 while already stopping
        info = await service_probe(force=True)
        mode = service.daemon_mode(info)
        if mode == "manual":
            raise LabError(409, MANUAL_RESTART)
        if mode != "service":
            raise LabError(409, f"cannot tell whether launchd started this daemon ({info.error}); "
                                "restart it with `afterword restart`")

        async def later():
            await asyncio.sleep(RESTART_DELAY_S)
            daemon.request_restart()

        # The answer goes out first; the shutdown starts once it is sent.
        return JSONResponse({"restarting": True, "pid": os.getpid(), "exit_code": service.RESTART_EXIT},
                            status_code=202, background=BackgroundTask(later))

    @app.post("/api/service/prune")
    async def service_prune():
        pruned = await asyncio.to_thread(daemon.prune_events)
        return {"pruned": pruned, "storage": await asyncio.to_thread(storage_view)}

    @app.post("/api/service/reveal")
    async def service_reveal():
        r = await asyncio.to_thread(service.reveal, daemon.home)  # a fixed path, never one from the request
        if not r.ok:
            raise LabError(502, f"open -R failed: {r.error()}")
        return {"revealed": str(daemon.home)}

    @app.get("/api/logs/{name}")
    async def logs(name: str, lines: int = 200):
        path = log_paths().get(name)
        if path is None:
            raise LabError(404, "no such log (daemon or service)")
        try:
            body = await asyncio.to_thread(service.tail, path, lines)
        except OSError as exc:
            raise LabError(500, f"could not read {path.name}: {exc.strerror or exc}") from None
        return {"name": name, **body}

    @app.get("/api/queue")
    async def queue():
        return daemon.queue.snapshot()

    @app.post("/api/queue/skip")
    async def skip():
        return {"skipped": daemon.skip()}

    @app.post("/api/queue/stop")
    async def stop():
        return daemon.stop_all()

    # -- speak anything, replay (contract: docs/gui-api.md, "Speak and replay") --

    def can_queue() -> None:
        if getattr(daemon, "_stopping", False):
            raise LabError(503, "the daemon is stopping")

    def join_turn(session_id: str, fallback: str) -> str:
        """The turn of the session's waiting lines, so a new line goes after
        them instead of superseding them (else `fallback`)."""
        waiting = [i for i in daemon.queue._items if i.session_id == session_id and i.kind != "alert"]
        return waiting[-1].turn if waiting and waiting[-1].turn else fallback

    def queued(ack: dict) -> dict:
        if not ack.get("ok"):
            raise LabError(503, ack.get("error") or "could not queue the line")
        if ack.get("reason") == "daemon busy":
            raise LabError(503, f"daemon busy: the queue is full (#{ack.get('event_id')} skipped)")
        return ack

    @app.post("/api/speak")
    async def speak(request: Request):
        body = await json_body(request)
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            raise LabError(400, "send {\"text\": \"…\", \"profile\": \"name\"} (\"lang\" optional)")
        text = body["text"].strip()
        if not text:
            raise LabError(400, "text is empty")
        if len(text) > SPEAK_MAX_CHARS:
            raise LabError(400, f"text is longer than {SPEAK_MAX_CHARS} characters")
        profiles = list(daemon.store.get().profiles)
        name = body.get("profile")
        if not isinstance(name, str) or name not in profiles:
            known = ", ".join(profiles) or "none configured"
            raise LabError(400, f"unknown profile {name!r} (profiles: {known})" if name else f"send a profile ({known})")
        lang = body.get("lang")
        if lang is not None and not isinstance(lang, str):
            raise LabError(400, "lang must be a string")
        lang = (lang or "").strip()
        if lang and lang.lower() != "auto":
            lang = lang_code(lang) or ""
            if not lang:
                raise LabError(400, f"unknown language {body['lang']!r}")
        can_queue()
        msg = {"type": "test", "text": text, "profile": name, "session_id": SPEAK_SESSION,
               "turn": join_turn(SPEAK_SESSION, SPEAK_SESSION)}
        if lang:
            msg["attrs"] = {"lang": lang.lower()}
        return queued(daemon.ingest_test(msg, time.time()))

    @app.post("/api/events/{event_id}/replay")
    async def replay(event_id: int):
        ev = daemon.events.get(event_id)
        if ev is None:
            raise LabError(404, "no such event")
        text = ev.get("text")
        if not isinstance(text, str) or not text.strip():
            raise LabError(400, f"#{event_id} has no spoken text")
        can_queue()
        says = replay_says(ev)
        session_id = ev.get("session_id") or SPEAK_SESSION
        fields = dict(
            ts=time.time(),
            kind="test",
            hook_event="replay",
            session_id=session_id,
            cwd=ev.get("cwd"),
            profile=ev.get("profile"),
            decision="speak",
            text=" ".join(s["text"] for s in says),
            attrs=dict(says[0]["attrs"]),
            parsed=ev.get("parsed") if isinstance(ev.get("parsed"), dict) else None,
            payload={"replay_of": event_id},
            timings={},
        )
        ack = queued(daemon._enqueue(fields, None, says=says,
                                     turn=join_turn(session_id, f"replay-{event_id}-{time.time_ns()}")))
        if ack.get("status") == "queued":
            daemon.events.update(ack["event_id"], reason=f"replay of #{event_id}")
        return ack

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

    @app.get("/api/voices")
    async def voices_view():
        def build():
            body = daemon.lab.voices()
            stats = daemon.events.voice_stats()
            for p in body["profiles"]:
                p["stats"] = stats.get(p["name"]) or {"lines_7d": 0, "audio_s_7d": 0.0, "synth_ms_avg_7d": None}
            return body
        return await asyncio.to_thread(build)

    @app.patch("/api/lab/profiles/{name}")
    async def lab_describe_profile(name: str, request: Request):
        return await asyncio.to_thread(daemon.lab.set_description, name, await json_body(request))

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


def replay_says(ev: dict) -> list[dict]:
    """A past event's spoken text as <say> blocks again. The event keeps the
    joined text plus a preview of each block (parsed.segments: the first
    characters and the attrs), so each block is found again in the text by
    its preview, in order; blocks not found (dropped for length or spoken
    early) are left out. One block of the whole text when that fails."""
    from .server import _say_attrs

    text = ev["text"].strip()
    parsed = ev.get("parsed") if isinstance(ev.get("parsed"), dict) else {}
    segs = parsed.get("segments")
    if isinstance(segs, list) and len(segs) > 1:
        starts: list[tuple[int, dict]] = []
        pos = 0
        for seg in segs:
            t = seg.get("text") if isinstance(seg, dict) else None
            if not isinstance(t, str) or not t.strip():
                continue
            t = t.strip()
            key = t[:REPLAY_KEY_CHARS]
            i = text.find(key, pos)
            if i < 0:
                continue
            starts.append((i, _say_attrs(seg.get("attrs"))))
            # A whole block (its preview is all of it) is skipped past, so a
            # later block's words inside it are not taken for its start.
            whole = seg.get("chars") == len(t) and text.startswith(t, i)
            pos = i + (len(t) if whole else len(key))
        if len(starts) > 1 and starts[0][0] == 0:
            out = []
            for n, (i, attrs) in enumerate(starts):
                end = starts[n + 1][0] if n + 1 < len(starts) else len(text)
                piece = text[i:end].strip()
                if piece:
                    out.append({"text": piece, "attrs": attrs})
            if len(out) > 1:
                return out
    return [{"text": text, "attrs": _say_attrs(ev.get("attrs"))}]


def _sse(kind: str, data) -> str:
    body = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {kind}\ndata: {body}\n\n"
