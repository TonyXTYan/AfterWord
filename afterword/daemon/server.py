"""afterwordd: Unix-socket ingest, playback worker, and the HTTP UI, all in
one asyncio loop in one process."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import fcntl
import json
import logging
import os
import socket
import time
from pathlib import Path
from typing import Any

from afterword import __version__
from afterword.hooks import common, tags

from .config import ConfigStore
from .log import EventLog, ingest_spool, setup_text_logging
from .player import make_player
from .queue import PlaybackQueue, QueueItem
from .lab import Lab
from .qwen import EngineStopped, QwenEngine, mlx_audio_available
from .synth import SaySynth, make_synth
from .voices import MODEL_FOR_KIND, ClipStore, VoiceError, parse_voice

log = logging.getLogger("afterword.daemon")

SOCKET_LINE_LIMIT = 2 * 1024 * 1024
SOCKET_READ_TIMEOUT_S = 2.0
SPOOL_POLL_S = 10.0
MAX_TEXT_CHARS = 5000
UI_DIR = Path(__file__).resolve().parent.parent / "ui"


class AlreadyRunning(RuntimeError):
    pass


def _str_or_none(value: Any, limit: int = 4096) -> str | None:
    return value[:limit] if isinstance(value, str) else None


class Daemon:
    def __init__(self, home: Path, store: ConfigStore, events: EventLog):
        self.home = home
        self.store = store
        self.events = events
        cfg = store.get()
        self.queue = PlaybackQueue(cfg.daemon.max_queue)
        self.voice_clips = ClipStore(home / "voices")
        self.engine = QwenEngine(cfg.qwen.repos(), cfg.qwen.keep_loaded)
        self.synth = self._make_synth(cfg.daemon.synth)
        self.lab = Lab(home, store, self.engine, self.voice_clips, active_synth=lambda: self.synth.name)
        self._stopping = False
        self.muted_all = self._load_daemon_state().get("muted_all") is True
        # [qwen] edits apply on every reload, not just when a message plays.
        store.on_reload(lambda c: self.engine.configure(c.qwen.repos(), c.qwen.keep_loaded))
        try:
            self.player = make_player(cfg.daemon.player)
        except ValueError as exc:
            log.error("%s; using afplay", exc)
            self.player = make_player("afplay")
        self.clips_dir = home / "clips"
        self.clips_dir.mkdir(mode=0o700, exist_ok=True)
        self.started_at = time.time()
        self.http_url: str | None = None
        self._skip_current = False
        self._skip_reason: str | None = None
        self._uvicorn = None
        self._stop_event = asyncio.Event()

    # ------------------------------------------------------------------ #
    # Socket ingest                                                      #
    # ------------------------------------------------------------------ #

    async def handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        t_recv = time.time()
        try:
            try:
                line = await asyncio.wait_for(reader.readline(), SOCKET_READ_TIMEOUT_S)
            except (asyncio.LimitOverrunError, ValueError):
                ack = {"ok": False, "error": "message too large"}
            except asyncio.TimeoutError:
                ack = {"ok": False, "error": "read timeout"}
            else:
                ack = self.dispatch(line, t_recv)
                if b'"no_ack": true' in line:
                    return  # fire-and-forget: the sender has already gone
            writer.write(json.dumps(ack).encode() + b"\n")
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            log.debug("client left before the ack")
        except Exception:
            log.exception("socket connection failed")
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    def dispatch(self, line: bytes, t_recv: float) -> dict[str, Any]:
        try:
            msg = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            return {"ok": False, "error": "not JSON"}
        if not isinstance(msg, dict):
            return {"ok": False, "error": "not an object"}
        kind = msg.get("type")
        try:
            if kind == "hook":
                return self.ingest_hook(msg, t_recv)
            if kind == "test":
                return self.ingest_test(msg, t_recv)
            if kind == "ping":
                return {"ok": True, "status": self.status()}
            if kind == "skip":
                return {"ok": True, "skipped": self.skip()}
            if kind == "stop":
                return {"ok": True, "stopped": self.stop_all()}
        except Exception as exc:
            log.exception("handling %s message failed", kind)
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": False, "error": f"unknown message type {kind!r}"}

    def ingest_hook(self, msg: dict[str, Any], t_recv: float) -> dict[str, Any]:
        t_hook = msg.get("t_hook") if isinstance(msg.get("t_hook"), (int, float)) else None
        t_sent = msg.get("t_sent") if isinstance(msg.get("t_sent"), (int, float)) else None
        timings: dict[str, Any] = {}
        if t_hook is not None:
            timings["hook_ms"] = round(((t_sent or t_recv) - t_hook) * 1000, 1)
            timings["hook_to_daemon_ms"] = round((t_recv - t_hook) * 1000, 1)
        decision = msg.get("decision") if msg.get("decision") in ("speak", "skip", "info") else "error"
        reason = _str_or_none(msg.get("reason"), 200)
        text = _str_or_none(msg.get("text"), MAX_TEXT_CHARS) or ""
        attrs = msg.get("attrs") if isinstance(msg.get("attrs"), dict) else {}
        if decision == "error":
            reason = "error: malformed hook message"
        elif decision == "speak" and not text.strip():
            decision, reason = "skip", "no <say> tag"
        fields = dict(
            ts=t_hook or t_recv,
            kind="hook",
            hook_event=_str_or_none(msg.get("event"), 64),
            session_id=_str_or_none(msg.get("session_id"), 256),
            cwd=_str_or_none(msg.get("cwd")),
            profile=_str_or_none(msg.get("profile"), 64),
            decision=decision,
            text=text,
            attrs=attrs,
            parsed=msg.get("parsed") if isinstance(msg.get("parsed"), dict) else None,
            payload=common.cap_strings(msg.get("payload")),
            timings=timings,
            delivery="socket",
            error=_str_or_none(msg.get("config_error")),
        )
        if msg.get("state_changed") is True and fields["session_id"]:
            self.publish_session(fields["session_id"])
        if decision != "speak":
            status = {"error": "error", "info": "info"}.get(decision, "skipped")
            event = self.events.insert(status=status, reason=reason, **fields)
            return {"ok": True, "event_id": event["id"], "status": event["status"], "reason": reason}
        return self._enqueue(fields, t_hook)

    def ingest_test(self, msg: dict[str, Any], t_recv: float) -> dict[str, Any]:
        cfg = self.store.get()
        profile = _str_or_none(msg.get("profile"), 64) or next(iter(cfg.profiles), None)
        raw = _str_or_none(msg.get("text"), MAX_TEXT_CHARS) or ""
        parsed = tags.parse(raw) if "<say" in raw.lower() else None
        if parsed is not None and parsed.say is not None:
            text, attrs = parsed.say.text, dict(parsed.say.attrs)
        else:
            text, attrs = tags.clean_text(raw), {}
        if isinstance(msg.get("attrs"), dict):
            attrs.update(msg["attrs"])
        text = tags.truncate(text, cfg.profile(profile).max_chars)
        fields = dict(
            ts=t_recv,
            kind="test",
            hook_event=None,
            session_id=_str_or_none(msg.get("session_id"), 256) or "cli-test",
            cwd=_str_or_none(msg.get("cwd")),
            profile=profile,
            decision="speak" if text else "skip",
            text=text,
            attrs=attrs,
            parsed=parsed.to_dict() if parsed else None,
            payload={"text": raw},
            timings={},
            delivery="socket",
        )
        if not text:
            event = self.events.insert(status="skipped", reason="empty text", **fields)
            return {"ok": True, "event_id": event["id"], "status": "skipped", "reason": "empty text"}
        return self._enqueue(fields, t_recv)

    def _enqueue(self, fields: dict[str, Any], t_hook: float | None) -> dict[str, Any]:
        self.queue.max_size = self.store.get().daemon.max_queue
        muted = self._muted_reason(fields.get("session_id"))
        if muted:
            event = self.events.insert(status="skipped", reason=muted, **fields)
            return {"ok": True, "event_id": event["id"], "status": "skipped", "reason": muted}
        event = self.events.insert(status="queued", **fields)
        item = QueueItem(
            event_id=event["id"],
            session_id=fields["session_id"] or "",
            text=fields["text"],
            profile=fields["profile"],
            attrs=fields["attrs"],
            cwd=fields["cwd"],
            t_hook=t_hook,
        )
        result = self.queue.put(item)
        if not result.accepted:
            self.events.update(event["id"], status="skipped", reason="daemon busy")
            self._publish_queue()
            return {"ok": True, "event_id": event["id"], "status": "skipped", "reason": "daemon busy"}
        if result.superseded is not None:
            self.events.update(
                result.superseded.event_id,
                status="skipped",
                reason=f"superseded by #{event['id']}",
            )
        self._publish_queue()
        return {"ok": True, "event_id": event["id"], "status": "queued", "position": result.position}

    # ------------------------------------------------------------------ #
    # Playback worker                                                    #
    # ------------------------------------------------------------------ #

    async def worker(self) -> None:
        while True:
            item = await self.queue.get()
            self.queue.now_playing = item
            self._skip_current = False
            self._publish_queue()
            try:
                await self._play_item(item)
            except asyncio.CancelledError:
                self.events.update(item.event_id, status="skipped", reason="daemon shutting down")
                raise
            except Exception as exc:
                if self._stopping:
                    self.events.update(item.event_id, status="skipped", reason="daemon shutting down")
                    continue
                log.exception("playback of event %s failed", item.event_id)
                self.events.update(item.event_id, status="error", reason="error", error=f"{type(exc).__name__}: {exc}")
            finally:
                self.queue.now_playing = None
                self._publish_queue()

    def _make_synth(self, name: str):
        if name == "qwen" and not os.environ.get("AFTERWORD_SYNTH"):
            ok, error = mlx_audio_available()
            if not ok:
                log.warning("%s; using say", error)
                name = "say"
        try:
            return make_synth(name, engine=self.engine, clips=self.voice_clips)
        except ValueError as exc:
            log.error("%s; using say", exc)
            return make_synth("say")

    def _refresh_backends(self) -> None:
        cfg = self.store.get()
        self.queue.max_size = cfg.daemon.max_queue
        if not os.environ.get("AFTERWORD_SYNTH") and cfg.daemon.synth != self.synth.name:
            new = self._make_synth(cfg.daemon.synth)
            if new.name != self.synth.name:
                self.synth = new
                log.info("synth backend switched to %s", self.synth.name)
        if not os.environ.get("AFTERWORD_PLAYER") and cfg.daemon.player != self.player.name:
            try:
                self.player = make_player(cfg.daemon.player)
                log.info("player switched to %s", self.player.name)
            except ValueError as exc:
                log.error("%s; keeping %s", exc, self.player.name)

    def _synthesize(self, text, profile, attrs, out):
        """Render with the configured backend. If Qwen3-TTS fails (model not
        downloaded, bad voice spec, …) fall back to `say` so the line is
        still heard; the reason is recorded on the event."""
        try:
            return self.synth.synth(text, profile, attrs, out), None
        except Exception as exc:
            if self.synth.name != "qwen" or self._stopping or isinstance(
                exc, (EngineStopped, concurrent.futures.CancelledError)
            ):
                raise
            note = f"qwen failed ({type(exc).__name__}: {exc}); spoke with say"
            log.warning("%s", note)
            return SaySynth().synth(text, profile, attrs, out), note

    async def _play_item(self, item: QueueItem) -> None:
        self._refresh_backends()
        muted = self._muted_reason(item.session_id) if item.session_id != "cli-test" else (
            "all muted" if self.muted_all else None)
        if muted:
            self.events.update(item.event_id, status="skipped", reason=muted)
            return
        cfg = self.store.get()
        profile = cfg.profile(item.profile)
        text = item.text
        if cfg.daemon.announce_session and item.cwd:
            text = f"{Path(item.cwd).name}. {text}"
        t_start = time.time()
        self.events.update(
            item.event_id,
            status="synthesizing",
            merge_timings={"queue_wait_ms": round((t_start - item.enqueued_at) * 1000, 1)},
        )
        out = self.clips_dir / f"{item.event_id}.wav"
        result, note = await asyncio.to_thread(self._synthesize, text, profile, dict(item.attrs), out)
        timings = {
            "synth_ms": round(result.synth_s * 1000, 1),
            "audio_s": round(result.duration_s, 3),
            "rtf": round(result.rtf, 3) if result.rtf is not None else None,
            "synth_backend": result.backend or self.synth.name,
        }
        if result.load_s:
            timings["load_ms"] = round(result.load_s * 1000, 1)
        if result.model:
            timings["model"] = result.model
        if result.seed is not None:
            timings["seed"] = result.seed
        if abs(result.playback_rate - 1.0) > 0.005:
            timings["playback_rate"] = result.playback_rate
        if self._skip_current:
            reason, self._skip_reason = self._skip_reason or "skipped by user", None
            self.events.update(item.event_id, status="skipped", reason=reason, clip=str(out), merge_timings=timings)
            self._prune_clips(cfg.daemon.history)
            return
        t_play = time.time()
        origin = item.t_hook or item.enqueued_at
        timings["first_audio_ms"] = round((t_play - origin) * 1000, 1)
        self.events.update(item.event_id, status="playing", clip=str(out), merge_timings=timings, reason=note)
        finished = await self.player.play(out, result.playback_rate)
        done = {"play_s": round(time.time() - t_play, 3)}
        if finished and not self._skip_current:
            self.events.update(item.event_id, status="spoken", reason=note, merge_timings=done)
        else:
            reason, self._skip_reason = self._skip_reason or "stopped by user", None
            self.events.update(item.event_id, status="skipped", reason=reason, merge_timings=done)
        self._prune_clips(cfg.daemon.history)

    def models_in_use(self) -> list[str]:
        """Qwen model kinds the configured profiles need, in profile order."""
        kinds: list[str] = []
        for profile in self.store.get().profiles.values():
            try:
                kind = MODEL_FOR_KIND.get(parse_voice(profile.voice).kind)
            except VoiceError:
                continue
            if kind and kind not in kinds:
                kinds.append(kind)
        return kinds

    async def warm_models(self) -> None:
        cfg = self.store.get()
        if self.synth.name != "qwen" or not cfg.qwen.warm_on_start:
            return
        for kind in self.models_in_use()[: cfg.qwen.keep_loaded]:
            if self.engine.snapshot_path(kind) is None:
                log.warning("not warming %s: model not downloaded", kind)
                continue
            try:
                load_s = await self.engine.warm_async(kind)
                if load_s:
                    self.events.insert(kind="system", status="info", reason=f"loaded Qwen3-TTS {kind} in {load_s:.1f} s")
            except Exception as exc:
                log.warning("warming %s failed: %s", kind, exc)
                self.events.insert(kind="system", status="error", reason=f"could not load Qwen3-TTS {kind}", error=str(exc))

    def _prune_clips(self, keep: int) -> None:
        try:
            clips = sorted(self.clips_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
            for old in clips[keep:]:
                old.unlink(missing_ok=True)
                if old.stem.isdigit():
                    self.events.forget_clip(int(old.stem))
        except OSError:
            log.warning("clip pruning failed", exc_info=True)

    # ------------------------------------------------------------------ #
    # Controls and status                                                #
    # ------------------------------------------------------------------ #

    def skip(self) -> int | None:
        """Stop the current item; the queue continues."""
        item = self.queue.now_playing
        if item is None:
            return None
        self._skip_current = True
        self.player.stop()
        return item.event_id

    def stop_all(self) -> dict[str, Any]:
        """Stop the current item and drop everything queued."""
        dropped = self.queue.clear()
        for item in dropped:
            self.events.update(item.event_id, status="skipped", reason="stopped by user")
        current = self.skip()
        self._publish_queue()
        return {"current": current, "dropped": [i.event_id for i in dropped]}

    def apply_session_change(self, session_id: str, state: dict[str, Any]) -> list[int]:
        """After a session is muted or turned off, drop its waiting lines and
        stop its current one. Returns the affected event ids."""
        reason = "session muted" if state.get("muted") is True else ("session off" if not state.get("profile") else None)
        if reason is None:
            return []
        affected = []
        for item in list(self.queue._items):
            if item.session_id == session_id:
                self.queue.remove(item.event_id)
                self.events.update(item.event_id, status="skipped", reason=reason)
                affected.append(item.event_id)
        now = self.queue.now_playing
        if now is not None and now.session_id == session_id:
            self._skip_reason = reason
            self.skip()
            affected.append(now.event_id)
        if affected:
            self._publish_queue()
        return affected

    def _muted_reason(self, session_id: str | None) -> str | None:
        if self.muted_all:
            return "all muted"
        if session_id and common.load_state(session_id).get("muted") is True:
            return "session muted"
        return None

    def remove_queued(self, event_id: int) -> bool:
        item = self.queue.remove(event_id)
        if item is None:
            return False
        self.events.update(event_id, status="skipped", reason="removed by user")
        self._publish_queue()
        return True

    # -- mute and sessions --------------------------------------------------

    def _daemon_state_path(self) -> Path:
        return self.home / "daemon.json"

    def _load_daemon_state(self) -> dict[str, Any]:
        try:
            data = json.loads(common.read_regular_file(self._daemon_state_path(), 65536).decode("utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def set_muted_all(self, muted: bool) -> bool:
        self.muted_all = bool(muted)
        state = self._load_daemon_state()
        state["muted_all"] = self.muted_all
        tmp = self._daemon_state_path().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, self._daemon_state_path())
        if self.muted_all:
            self.stop_all()
        self.events.publish("mute", {"muted_all": self.muted_all})
        return self.muted_all

    def session_activity_view(self, session_id: str) -> dict[str, Any]:
        from . import state

        activity = self.events.session_activity_for(session_id)
        return state.session_view(session_id, state.get_session(session_id), activity)

    def publish_session(self, session_id: str) -> None:
        try:
            self.events.publish("sessions", {"session_id": session_id, "session": self.session_activity_view(session_id)})
        except Exception:
            log.warning("could not publish session %s", session_id, exc_info=True)

    def _publish_queue(self) -> None:
        self.events.publish("queue", self.queue.snapshot())

    def status(self) -> dict[str, Any]:
        cfg = self.store.get()
        return {
            "version": __version__,
            "pid": os.getpid(),
            "started_at": self.started_at,
            "uptime_s": round(time.time() - self.started_at, 1),
            "home": str(self.home),
            "socket": str(common.socket_path()),
            "http": self.http_url,
            "synth": self.synth.name,
            "player": self.player.name,
            "engine": self.engine.status(),
            "config": {
                "path": str(self.store.path),
                "error": self.store.error,
                "warnings": self.store.warnings,
                "loaded_at": self.store.loaded_at,
                "profiles": sorted(cfg.profiles),
                "fallback": cfg.daemon.fallback,
            },
            "queue": self.queue.snapshot(),
            "muted_all": self.muted_all,
            "events": self.events.count(),
        }

    async def spool_poller(self) -> None:
        while True:
            await asyncio.sleep(SPOOL_POLL_S)
            try:
                await asyncio.to_thread(ingest_spool, self.events)
            except Exception:
                log.exception("spool import failed")

    def request_shutdown(self) -> None:
        self._stopping = True
        self._stop_event.set()
        self.events.publish("shutdown", None)  # ends open SSE streams promptly
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True


# ---------------------------------------------------------------------- #
# Process lifecycle                                                      #
# ---------------------------------------------------------------------- #


def _acquire_lock(home: Path):
    fh = open(home / "afterwordd.lock", "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise AlreadyRunning(f"another afterwordd is running (lock: {home / 'afterwordd.lock'})")
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def _clear_stale_socket(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.2)
    try:
        s.connect(str(path))
    except OSError:
        path.unlink(missing_ok=True)  # nobody listening: stale
        return
    finally:
        s.close()
    raise AlreadyRunning(f"something is already listening on {path}")


async def run_daemon(port: int | None = None, http: bool = True, on_ready=None) -> None:
    import signal

    os.umask(0o077)  # events.db, clips, logs: owner-only
    home = common.ensure_home()
    os.chmod(home, 0o700)
    lock = _acquire_lock(home)
    sock_path = common.socket_path()
    loop = asyncio.get_running_loop()
    server = None
    daemon = None
    tasks: list[asyncio.Task] = []
    events = None
    signals_installed: list[int] = []
    try:
        # Refuse to start before touching anything if another daemon owns
        # the socket, so a refused start has no side effects.
        _clear_stale_socket(sock_path)
        store = ConfigStore()
        if store.ensure_default_file():
            log.info("wrote default config to %s", store.path)
        events = EventLog(home / "events.db")
        events.bind_loop(loop)
        recovered = events.recover_unfinished("daemon restarted")
        imported = ingest_spool(events)
        daemon = Daemon(home, store, events)

        server = await asyncio.start_unix_server(daemon.handle_connection, path=str(sock_path), limit=SOCKET_LINE_LIMIT)
        os.chmod(sock_path, 0o600)
        tasks.append(asyncio.create_task(daemon.worker(), name="worker"))
        tasks.append(asyncio.create_task(daemon.spool_poller(), name="spool"))
        tasks.append(asyncio.create_task(daemon.warm_models(), name="warm"))

        # Our own SIGINT/SIGTERM handling in both modes, so shutdown always
        # runs the cleanup below (uvicorn's own capture re-raises the signal
        # after serve() returns, which would kill the process mid-cleanup).
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, daemon.request_shutdown)
                signals_installed.append(sig)
            except (NotImplementedError, RuntimeError, ValueError):
                pass  # not the main thread (tests)

        events.insert(
            kind="system", status="info", decision=None,
            reason=(
                f"daemon started (pid {os.getpid()}, synth {daemon.synth.name}, "
                f"imported {imported} spooled, recovered {recovered} unfinished)"
            ),
        )
        log.info("listening on %s", sock_path)

        if http:
            import uvicorn

            from .web import build_app

            class _Server(uvicorn.Server):
                @contextlib.contextmanager
                def capture_signals(self):
                    yield

            cfg = store.get()
            port = port or cfg.daemon.port
            app = build_app(daemon, port)
            config = uvicorn.Config(
                app, host="127.0.0.1", port=port, log_level="warning",
                timeout_graceful_shutdown=2, access_log=False, lifespan="off",
            )
            daemon._uvicorn = _Server(config)
            daemon.http_url = f"http://127.0.0.1:{port}"
            log.info("UI at %s", daemon.http_url)
            if on_ready:
                loop.call_later(0.3, on_ready, daemon)
            await daemon._uvicorn.serve()
            if not daemon._uvicorn.started and not daemon._stop_event.is_set():
                raise RuntimeError(f"HTTP UI could not start on 127.0.0.1:{port} (port in use?)")
        else:
            if on_ready:
                on_ready(daemon)
            await daemon._stop_event.wait()
    finally:
        for sig in signals_installed:
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(sig)
        if daemon is not None:
            daemon._stopping = True
            daemon.engine.shutdown()  # cancel queued renders before the worker is cancelled
            with contextlib.suppress(Exception):
                daemon.player.stop()
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(BaseException):
                await t
        if daemon is not None and events is not None:
            for item in daemon.queue.clear():
                with contextlib.suppress(Exception):
                    events.update(item.event_id, status="skipped", reason="daemon shutting down")
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
            # Only unlink the socket this process bound.
            with contextlib.suppress(OSError):
                sock_path.unlink()
        if daemon is not None:
            daemon.engine.shutdown()
        if events is not None:
            with contextlib.suppress(Exception):
                events.insert(kind="system", status="info", reason="daemon stopped")
            events.close()
        lock.close()
        log.info("stopped")


def main(port: int | None = None, http: bool = True, verbose: bool = False) -> int:
    home = common.ensure_home()
    setup_text_logging(home, level=logging.DEBUG if verbose else logging.INFO)
    try:
        asyncio.run(run_daemon(port=port, http=http))
    except (AlreadyRunning, RuntimeError) as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        pass
    return 0
