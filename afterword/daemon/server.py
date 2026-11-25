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
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from afterword import __version__
from afterword.hooks import common, scriptsplit, tags

from . import alerts as alerting
from . import apple
from .config import ConfigStore, Profile, lang_code
from .log import EventLog, ingest_spool, setup_text_logging
from .player import make_player
from .queue import PlaybackQueue, QueueItem
from .lab import Lab
from .qwen import LIVE as QWEN_LIVE, EngineStopped, QwenEngine, RenderCancelled, mlx_audio_available
from .qwen import label as model_label
from .stream import SAMPLE_RATE, PlayerUnavailable, clamp_pace, make_stream_player
from .synth import SaySynth, make_synth, partial_result
from .voices import MODEL_FOR_KIND, ClipStore, VoiceError, parse_voice, write_wav

log = logging.getLogger("afterword.daemon")

SOCKET_LINE_LIMIT = 2 * 1024 * 1024
SOCKET_READ_TIMEOUT_S = 2.0
SPOOL_POLL_S = 10.0
MAX_TEXT_CHARS = 5000
UI_DIR = Path(__file__).resolve().parent.parent / "ui"
SEGMENT_GAP_S = 0.25  # silence between the segments of one item
LEAD_TRIM_S = 0.07  # leading silence trimmed from each later segment
QUIET = 0.01  # -40 dBFS: "silence" for the trim
MAX_SAYS = 8  # <say> blocks accepted per hook message (the hook sends at most tags.MAX_SAYS)
MAX_ITEM_SEGMENTS = 16  # rendered segments per item; the rest joins the last one
EARLY_TURNS_KEEP = 256  # turns whose early-spoken blocks are remembered
EARLY_TURN_TTL_S = 3600.0


def _voice_backend(voice: str | None) -> str | None:
    """The engine family of a voice spec ("qwen", "kokoro", "apple", ...)."""
    try:
        return parse_voice(voice).backend
    except VoiceError:
        return None


@dataclass
class Segment:
    """One part of a queue item, rendered with one voice: a <say> block, or
    one language's part of it (see Daemon._segments). `profile` is the
    effective profile for its language (Config.voice_for)."""

    text: str
    profile: Profile
    attrs: dict[str, Any] = field(default_factory=dict)

    @property
    def lang(self) -> str:
        return str(self.attrs.get("lang") or self.profile.lang or "")

    @property
    def pace(self) -> float:
        return clamp_pace(self.attrs.get("pace") or self.profile.pace or 1.0)


class _DeviceGone(RenderCancelled):
    """The audio device failed or stalled after audio was heard: stop the
    render (nobody would hear the rest)."""


class _Feed:
    """The sink segment renders write into (from the engine or a worker
    thread): keeps the un-stretched clip and passes chunks to the stream.
    Between segments it adds a short gap (not time-stretched) and trims the
    next segment's leading silence."""

    def __init__(self, playback, cancel: threading.Event):
        self.playback = playback
        self.cancel = cancel
        self.clip: list = []
        self.spans: list = []  # [pace, first chunk index] per segment (gaps have pace None)
        self.samples = 0  # of the current segment
        self._trim = 0

    def begin(self, index: int, rate: float) -> None:
        import numpy as np

        self.samples = 0
        if index > 0:
            self.playback.begin_segment(1.0)  # the gap is real time, whatever the pace
            self.spans.append([None, len(self.clip)])
            self._emit(np.zeros(int(SEGMENT_GAP_S * SAMPLE_RATE), np.float32))
            self._trim = int(LEAD_TRIM_S * SAMPLE_RATE)
        self.playback.begin_segment(rate)
        self.spans.append([float(rate), len(self.clip)])

    def __call__(self, chunk) -> None:
        import numpy as np

        if self.cancel.is_set():
            raise RenderCancelled("cancelled")
        pb = self.playback
        if pb.error is not None and pb.played_samples > 0:
            # The device died mid-line. (Before anything was heard the
            # render goes on: the whole clip is then played with afplay.)
            raise _DeviceGone(pb.error)
        chunk = np.asarray(chunk, dtype=np.float32).reshape(-1)
        if self._trim and chunk.size:
            n = min(self._trim, chunk.size)
            quiet = np.abs(chunk[:n]) < QUIET
            k = n if bool(quiet.all()) else int(np.argmin(quiet))  # first loud sample
            chunk = chunk[k:]
            self._trim = self._trim - k if k == n else 0
        if not chunk.size:
            return
        self.samples += chunk.size
        self._emit(chunk)

    def _emit(self, chunk) -> None:
        self.clip.append(chunk)
        self.playback.write(chunk)

    def audio(self):
        import numpy as np

        return np.concatenate(self.clip) if self.clip else np.zeros(0, np.float32)

    def clip_audio(self):
        """(clip, playback rate) for History and the afplay replay. Segments
        that all play at one pace keep the un-stretched audio and that rate.
        Mixed paces: each segment is time-stretched into the clip as it was
        heard (gaps unstretched), and the rate is 1.0."""
        import numpy as np

        from .stream import TimeStretch

        bounds = [(pace, start, self.spans[i + 1][1] if i + 1 < len(self.spans) else len(self.clip))
                  for i, (pace, start) in enumerate(self.spans)]
        bounds = [b for b in bounds if b[2] > b[1]]  # segments that rendered something
        paces = {round(pace, 3) for pace, _a, _b in bounds if pace is not None}
        if len(paces) <= 1:
            return self.audio(), (paces.pop() if paces else 1.0)
        pieces = []
        for pace, a, b in bounds:
            audio = np.concatenate(self.clip[a:b])
            if pace is not None and abs(pace - 1.0) > 0.005:
                ts = TimeStretch(pace)
                audio = np.concatenate([ts.process(audio), ts.flush()])
            pieces.append(audio)
        return np.concatenate(pieces).astype(np.float32, copy=False), 1.0


def _trim_lead(audio):
    """Drop leading samples below QUIET, at most LEAD_TRIM_S (like _Feed)."""
    import numpy as np

    n = min(int(LEAD_TRIM_S * SAMPLE_RATE), len(audio))
    if not n:
        return audio
    quiet = np.abs(audio[:n]) < QUIET
    k = n if bool(quiet.all()) else int(np.argmin(quiet))
    return audio[k:]


class AlreadyRunning(RuntimeError):
    pass


@dataclass
class _TurnSaid:
    """What one turn of one session has had spoken (or queued, or skipped
    as muted): the early_keys, and the block digests by source, so neither
    an early block nor the Stop hook says a block twice."""

    keys: set = field(default_factory=set)
    early: set = field(default_factory=set)  # digests of blocks sent early
    stop: set = field(default_factory=set)  # digests of blocks sent by Stop
    t: float = field(default_factory=time.time)


def _key_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= 64 else None


def _str_or_none(value: Any, limit: int = 4096) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _say_attrs(raw: Any) -> dict[str, Any]:
    """The known <say> attributes of one block, with sane types."""
    out: dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    mood, lang, pace = raw.get("mood"), raw.get("lang"), raw.get("pace")
    if isinstance(mood, str) and mood.strip():
        out["mood"] = mood.strip()[:64]
    if isinstance(lang, str) and lang.strip():
        out["lang"] = lang.strip()[:32]
    if isinstance(pace, (int, float)) and not isinstance(pace, bool) and pace == pace:
        out["pace"] = clamp_pace(pace)
    return out


def clean_says(raw: Any, text: str, attrs: dict[str, Any]) -> list[dict[str, Any]]:
    """The hook's `segments` ([{"text", "attrs"}], one per <say>), checked
    and capped at MAX_TEXT_CHARS in all; without usable segments, one block
    of `text` and `attrs` (older hooks, `afterword test`)."""
    out: list[dict[str, Any]] = []
    if isinstance(raw, list):
        room = MAX_TEXT_CHARS
        for seg in raw[:MAX_SAYS]:
            t = seg.get("text") if isinstance(seg, dict) else None
            if not isinstance(t, str) or not t.strip():
                continue
            t = t.strip()[:room]
            if not t:
                break
            out.append({"text": t, "attrs": _say_attrs(seg.get("attrs"))})
            room -= len(t) + 1
            if room <= 0:
                break
    if not out and text.strip():
        out = [{"text": text, "attrs": dict(attrs)}]
    return out


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
        self.lab = Lab(home, store, self.engine, self.voice_clips, active_synth=lambda: self.synth.name,
                       publish=events.publish, live_busy=self._live_busy)
        self.alerts = alerting.AlertPhrases(home / alerting.FILE_NAME)
        self.alert_runner = None  # `claude -p` runner for Generate (tests inject a fake)
        self._stopping = False
        self.muted_all = self._load_daemon_state().get("muted_all") is True
        # [qwen] edits apply on every reload, not just when a message plays.
        store.on_reload(lambda c: self.engine.configure(c.qwen.repos(), c.qwen.keep_loaded))
        try:
            self.player = make_player(cfg.daemon.player)
        except ValueError as exc:
            log.error("%s; using afplay", exc)
            self.player = make_player("afplay")
        self.stream_player = None
        self.stream_error: str | None = None
        self._stream_key: tuple[str, bool] | None = None
        self._stream_logged: set[str] = set()
        self._current: tuple[threading.Event, Any] | None = None  # (cancel, playback)
        self._setup_stream_player(cfg)
        self.clips_dir = home / "clips"
        self.clips_dir.mkdir(mode=0o700, exist_ok=True)
        self.started_at = time.time()
        self.http_url: str | None = None
        self._skip_current = False
        self._skip_reason: str | None = None
        self._said: dict[tuple[str, str], _TurnSaid] = {}  # (session, turn) -> blocks spoken
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
        hook_event = _str_or_none(msg.get("event"), 64)
        session_id = _str_or_none(msg.get("session_id"), 256)
        turn = _str_or_none(msg.get("turn"), 128) or None
        raw_segments = msg.get("segments")
        early = hook_event == "early"
        taken: list[str] = []  # early_keys the hook may consider handled
        pending = None  # (keys, digests) to remember once the item is queued
        alert = msg.get("alert") if isinstance(msg.get("alert"), dict) else None
        if alert is not None and hook_event in ("PermissionRequest", "Notification"):
            return self.ingest_alert(msg, alert, t_recv, timings)
        if isinstance(msg.get("early_dropped"), int) and not isinstance(msg.get("early_dropped"), bool) \
                and msg["early_dropped"] > 0:
            timings["early_dropped"] = min(msg["early_dropped"], MAX_SAYS)
        if decision == "speak" and early and not self.store.get().daemon.early_say:
            decision, reason = "skip", "early speaking off"
        elif decision == "speak" and early and not (session_id and turn):
            # Nothing could tell this block from Stop's copy or a resend.
            decision, reason = "skip", "early without a turn"
        elif decision == "speak" and isinstance(raw_segments, list) and session_id and turn:
            raw_segments, refused, pending = self._said_filter(session_id, turn, raw_segments[:MAX_SAYS], early)
            taken.extend(refused)
            if refused:
                timings["early_dropped"] = timings.get("early_dropped", 0) + len(refused)
                kept = [s.get("text") for s in raw_segments if isinstance(s.get("text"), str) and s["text"].strip()]
                text = " ".join(t.strip() for t in kept)[:MAX_TEXT_CHARS]
                if not kept:
                    decision, reason = "skip", "already spoken" if early else "spoken early"
        says = clean_says(raw_segments, text, attrs) if decision == "speak" else []
        if says and not text.strip():
            text = " ".join(s["text"] for s in says)[:MAX_TEXT_CHARS]
        if decision == "error":
            reason = "error: malformed hook message"
        elif decision == "speak" and not text.strip():
            decision, reason = "skip", "no <say> tag"
        fields = dict(
            ts=t_hook or t_recv,
            kind="hook",
            hook_event=hook_event,
            session_id=session_id,
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
            ack = {"ok": True, "event_id": event["id"], "status": event["status"], "reason": reason}
            if taken or early:
                ack["taken"] = taken
            if alert is not None and hook_event == "Stop":
                self._done_alert(msg, alert, ack, session_id, turn, t_hook)
            return ack
        ack = self._enqueue(fields, t_hook, says=says, turn=turn)
        if pending is not None and ack.get("reason") != "daemon busy":
            # Queued, or skipped because the session is muted: either way
            # this block is done for the turn. (Busy: Stop may try again.)
            self._said_remember(session_id, turn, *pending, early=early)
            taken.extend(pending[0])
        if taken or early:
            ack["taken"] = taken
        return ack

    def _said_filter(self, session_id: str, turn: str, raw: list, early: bool):
        """Split a hook's segments into the ones still to speak and the
        early_keys of the ones refused: an early block whose key was already
        taken this turn, or whose text Stop already sent; a Stop block whose
        key or text was already spoken early. Returns (kept segments,
        refused keys, (keys, digests) of the kept ones)."""
        said = self._said.get((session_id, turn))
        kept: list[Any] = []
        refused: list[str] = []
        keys: list[str] = []
        digests: list[str] = []
        for seg in raw:
            if not isinstance(seg, dict):
                kept.append(seg)
                continue
            key = _key_or_none(seg.get("early_key"))
            dig = _key_or_none(seg.get("digest"))
            if said is not None and (
                (key is not None and key in said.keys)
                or (dig is not None and dig in (said.stop if early else said.early))
            ):
                if key is not None:
                    refused.append(key)
                else:
                    refused.append(dig or "")
                continue
            kept.append(seg)
            if key is not None:
                keys.append(key)
            if dig is not None:
                digests.append(dig)
        return kept, [k for k in refused if k], (keys, digests)

    def _said_remember(self, session_id: str, turn: str, keys: list[str], digests: list[str], early: bool) -> None:
        now = time.time()
        said = self._said.pop((session_id, turn), None) or _TurnSaid()
        said.keys.update(keys)
        (said.early if early else said.stop).update(digests)
        said.t = now
        self._said[(session_id, turn)] = said  # most recent last
        while len(self._said) > EARLY_TURNS_KEEP:
            del self._said[next(iter(self._said))]
        for k in [k for k, v in self._said.items() if now - v.t > EARLY_TURN_TTL_S]:
            del self._said[k]

    # -- spoken alerts (docs/design-v2.md C) -----------------------------

    @staticmethod
    def _clean_alert(raw: dict[str, Any]) -> dict[str, Any]:
        info: dict[str, Any] = {"event": raw.get("event") if raw.get("event") in alerting.ALERT_EVENTS else None}
        for key, limit in (("trigger", 16), ("action", 200), ("tool", 80), ("notification_type", 64), ("message", 200)):
            value = _str_or_none(raw.get(key), limit)
            if value:
                info[key] = value.strip()
        for key in ("turn_s", "minutes"):
            v = raw.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and 0 <= v <= 1e7:
                info[key] = v
        return info

    def _alert_refusal(self, cfg, info: dict[str, Any]) -> str | None:
        """Why this alert is not spoken (an info row), or None."""
        a = cfg.alerts
        event = info.get("event")
        if event is None:
            return "alert: unknown event"
        if not a.enabled:
            return f"alert {event}: alerts off"
        if event not in a.events:
            return f"alert {event}: not in [alerts] events"
        if event == "permission":
            trigger = info.get("trigger")
            if a.permission_when == "away" and trigger == "request":
                return "permission pending: spoken if the prompt is still waiting after ~6 s"
            if a.permission_when == "now" and trigger == "notification":
                return "permission prompt still waiting (already spoken when it was asked)"
            # trigger "only" (Codex: no Notification follows) is spoken in both modes.
        return None

    def ingest_alert(self, msg: dict[str, Any], raw: dict[str, Any], t_recv: float,
                     timings: dict[str, Any]) -> dict[str, Any]:
        """A PermissionRequest or Notification hook message: an alert if
        [alerts] wants it (a kind "alert" event and queue item), else an
        info row."""
        cfg = self.store.get()
        info = self._clean_alert(raw)
        t_hook = msg.get("t_hook") if isinstance(msg.get("t_hook"), (int, float)) else None
        session_id = _str_or_none(msg.get("session_id"), 256)
        fields = dict(
            ts=t_hook or t_recv,
            hook_event=_str_or_none(msg.get("event"), 64),
            session_id=session_id,
            cwd=_str_or_none(msg.get("cwd")),
            payload=common.cap_strings(msg.get("payload")),
            timings=timings,
            delivery="socket",
        )
        refusal = self._alert_refusal(cfg, info)
        if refusal:
            event = self.events.insert(kind="hook", status="info", decision="info", reason=refusal,
                                       profile=_str_or_none(msg.get("profile"), 64), text="",
                                       parsed={"alert": info}, **fields)
            return {"ok": True, "event_id": event["id"], "status": "info", "reason": refusal}
        return self._speak_alert(cfg, info, fields, _str_or_none(msg.get("profile"), 64), t_hook)

    def _speak_alert(self, cfg, info: dict[str, Any], fields: dict[str, Any], session_profile: str | None,
                     t_hook: float | None) -> dict[str, Any]:
        from afterword.hooks import alerting as hook_alerting

        name = alerting.alert_profile_name(cfg, session_profile)
        profile = cfg.profile(name)
        lang = alerting.pool_lang(profile)
        values = {
            "project": hook_alerting.project_name(fields.get("cwd")),
            "action": info.get("action") or "",
            "tool": info.get("tool") or "",
            # {minutes} only for a turn of a minute or more ("finished after 1
            # minute" for a 12 s turn would be wrong): the phrases with it are
            # then skipped in the pick.
            "minutes": str(int(info["minutes"])) if isinstance(info.get("minutes"), (int, float))
            and info["minutes"] >= 1 and not (isinstance(info.get("turn_s"), (int, float)) and info["turn_s"] < 60)
            else "",
        }
        text, template, used = self.alerts.pick(info["event"], lang, values)
        # The profile's own language needs no lang attr (a profile with
        # [langs] then splits by script: an English {action} in a Chinese
        # phrase gets the English voice); English for a profile without an
        # English pool of its own does.
        attrs = {"lang": used} if used != lang else {}
        info = dict(info, template=template, lang=used)
        fields = dict(fields, kind="alert", profile=name, decision="speak", text=text, attrs=attrs,
                      parsed={"alert": info})
        return self._enqueue(fields, t_hook, says=[{"text": text, "attrs": attrs}], kind="alert")

    def _done_alert(self, msg: dict[str, Any], raw: dict[str, Any], ack: dict[str, Any],
                    session_id: str | None, turn: str | None, t_hook: float | None) -> None:
        """Stop: the "done" alert for a long turn that spoke nothing."""
        cfg = self.store.get()
        info = self._clean_alert(raw)
        if info.get("event") != "done" or self._alert_refusal(cfg, info):
            return
        turn_s = info.get("turn_s")
        if not isinstance(turn_s, (int, float)) or turn_s < cfg.alerts.done_after_s:
            return
        if ack.get("status") == "queued" or ack.get("reason") in ("all muted", "session muted", "daemon busy"):
            return  # something is being spoken for this turn (or nothing may be)
        said = self._said.get((session_id, turn)) if session_id and turn else None
        if said is not None and said.early:
            return  # a line of this turn was spoken early
        if isinstance(msg.get("early_dropped"), int) and msg["early_dropped"] > 0:
            return
        fields = dict(
            ts=time.time(),
            hook_event="Stop",
            session_id=session_id,
            cwd=_str_or_none(msg.get("cwd")),
            payload=None,
            timings={},
            delivery="socket",
        )
        ack["alert"] = self._speak_alert(cfg, info, fields, _str_or_none(msg.get("profile"), 64), None)

    def ingest_test(self, msg: dict[str, Any], t_recv: float) -> dict[str, Any]:
        cfg = self.store.get()
        profile = _str_or_none(msg.get("profile"), 64) or next(iter(cfg.profiles), None)
        raw = _str_or_none(msg.get("text"), MAX_TEXT_CHARS) or ""
        parsed = tags.parse(raw) if "<say" in raw.lower() else None
        max_chars = cfg.profile(profile).max_chars
        if parsed is not None and parsed.says:
            says = tags.limit_says(parsed.says, max_chars)  # several <say>: as the hook does
        else:
            one = tags.truncate(tags.clean_text(raw), max_chars)
            says = [{"text": one, "attrs": {}}] if one else []
        if isinstance(msg.get("attrs"), dict):
            for say in says:
                say["attrs"].update(msg["attrs"])
        text = " ".join(say["text"] for say in says)
        attrs = dict(says[0]["attrs"]) if says else {}
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
        return self._enqueue(fields, t_recv, says=says, turn=_str_or_none(msg.get("turn"), 128) or None)

    def _enqueue(self, fields: dict[str, Any], t_hook: float | None,
                 says: list[dict[str, Any]] | None = None, turn: str | None = None,
                 kind: str = "speech") -> dict[str, Any]:
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
            turn=turn or f"event-{event['id']}",
            segments=says or [],
            kind=kind,
        )
        result = self.queue.put(item)
        if not result.accepted:
            self.events.update(event["id"], status="skipped", reason="daemon busy")
            self._publish_queue()
            return {"ok": True, "event_id": event["id"], "status": "skipped", "reason": "daemon busy"}
        for old in result.superseded:
            self.events.update(old.event_id, status="skipped", reason=f"superseded by #{event['id']}")
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

    def _live_busy(self) -> bool:
        """A line is being said or waits to be (Qwen only: `say` does not
        share the engine). The seed explorer holds back meanwhile."""
        return self.synth.name == "qwen" and (self.queue.now_playing is not None or len(self.queue) > 0)

    def _make_synth(self, name: str):
        # "qwen" stays the prefix-aware synth even without mlx-audio: apple:
        # and say: voices still work, and a model voice fails its render (the
        # engine reports why), so [daemon] model_fallback decides what speaks.
        if name == "qwen" and not os.environ.get("AFTERWORD_SYNTH"):
            ok, error = mlx_audio_available()
            if not ok:
                log.warning("%s; model voices will use the fallback", error)
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
        self._refresh_players(cfg)

    def _refresh_players(self, cfg) -> None:
        """Apply [daemon] player / stream. Only between items (a line keeps
        the player it started with)."""
        if not os.environ.get("AFTERWORD_PLAYER") and cfg.daemon.player != self.player.name:
            try:
                self.player = make_player(cfg.daemon.player)
                log.info("player switched to %s", self.player.name)
            except ValueError as exc:
                log.error("%s; keeping %s", exc, self.player.name)
        self._setup_stream_player(cfg)

    def _setup_stream_player(self, cfg) -> None:
        """The streaming player for the configured output, or None (then the
        v1 path plays whole WAVs with self.player)."""
        name = (os.environ.get("AFTERWORD_PLAYER") or cfg.daemon.player or "afplay").lower()
        key = (name, cfg.daemon.stream)
        if key == self._stream_key:
            return
        self._stream_key = key
        if not cfg.daemon.stream:
            self.stream_player, self.stream_error = None, None
            return
        player, why = make_stream_player("null" if name == "null" else "afplay")
        self.stream_player = player
        self.stream_error = why
        if why:
            self._stream_unavailable(why)

    def _stream_unavailable(self, why: str) -> None:
        """Log each distinct reason once."""
        self.stream_error = why
        if why not in self._stream_logged:
            self._stream_logged.add(why)
            log.warning("streaming playback unavailable (%s); playing whole clips with afplay", why)

    def _fallbacks(self, profile: Profile) -> list[tuple[str, Any]]:
        """What speaks a line whose model voice failed ([daemon]
        model_fallback): the best Apple voice for its language, then `say`."""
        mode = self.store.get().daemon.model_fallback
        if mode == "none":
            return []
        out: list[tuple[str, Any]] = []
        if mode == "apple" and _voice_backend(profile.voice) != "apple" and apple.available()[0]:
            out.append(("apple", apple.AppleSynth(auto_voice=True)))
        out.append(("say", SaySynth()))
        return out

    def _with_fallback(self, exc: Exception, profile: Profile, run, can_retry):
        """After a failed model render: try each fallback in turn (`run(synth)`),
        while `can_retry()` (nothing heard yet, not cancelled). Returns
        (result, note) or raises the first error."""
        failed = _voice_backend(profile.voice) or "qwen"
        reason = f"{type(exc).__name__}: {exc}"
        for name, synth in self._fallbacks(profile):
            if not can_retry():
                break
            try:
                result = run(synth)
            except Exception as exc2:
                if not can_retry():
                    raise  # it was heard, or cancelled: its own error (and partial timings) stand
                log.warning("fallback %s failed too: %s", name, exc2)
                continue
            note = f"{failed} failed ({reason}); spoke with {name}"
            log.warning("%s", note)
            return result, note
        raise exc

    def _synthesize(self, text, profile, attrs, out):
        """Render with the configured backend. If a model voice fails (model
        not downloaded, bad voice spec, …) fall back ([daemon]
        model_fallback: an Apple voice, then `say`) so the line is still
        heard; the reason is recorded on the event."""
        try:
            return self.synth.synth(text, profile, attrs, out), None
        except Exception as exc:
            if self.synth.name != "qwen" or self._stopping or isinstance(
                exc, (EngineStopped, concurrent.futures.CancelledError)
            ):
                raise
            return self._with_fallback(exc, profile, lambda s: s.synth(text, profile, attrs, out),
                                       lambda: not self._stopping)

    def _spoken_says(self, item: QueueItem, cfg, profile: Profile) -> list[tuple[str, dict[str, Any]]]:
        """The item's <say> blocks as they are spoken: the announce prefix
        before the first, then the pronunciation list (events.text keeps the
        original)."""
        says = [(s["text"], dict(s["attrs"])) for s in item.segments] or [(item.text, dict(item.attrs))]
        if cfg.daemon.announce_session and item.cwd and item.kind != "alert":
            says[0] = (f"{Path(item.cwd).name}. {says[0][0]}", says[0][1])
        return [(cfg.speakable(text, profile), attrs) for text, attrs in says]

    def _segments(self, item: QueueItem, cfg, profile: Profile,
                  says: list[tuple[str, dict[str, Any]]]) -> list[Segment]:
        """One segment per <say>, each with its language's voice. A block
        without a lang attr (or lang="auto") of a profile with [langs] is
        split by script first (scriptsplit), one segment per language run."""
        out: list[Segment] = []
        primary = lang_code(profile.lang)
        other = next((c for c in profile.langs if c not in scriptsplit.CJK_LANGS), None)
        for text, attrs in says:
            lang = attrs.get("lang")
            if isinstance(lang, str) and lang and lang.lower() != "auto":
                out.append(Segment(text, cfg.voice_for(profile, lang), attrs))
            elif profile.langs:
                base = {k: v for k, v in attrs.items() if k != "lang"}
                for part_lang, part in scriptsplit.split(text, primary, other):
                    seg_attrs = dict(base, lang=part_lang) if part_lang else dict(base)
                    out.append(Segment(part, cfg.voice_for(profile, part_lang), seg_attrs))
            else:
                out.append(Segment(text, profile, attrs))
        if len(out) > MAX_ITEM_SEGMENTS:
            last = out[MAX_ITEM_SEGMENTS - 1]
            last.text = " ".join([last.text] + [s.text for s in out[MAX_ITEM_SEGMENTS:]])
            del out[MAX_ITEM_SEGMENTS:]
        return out

    async def _play_item(self, item: QueueItem) -> None:
        if item.kind == "alert":
            waited = time.time() - item.enqueued_at
            if waited > alerting.MAX_WAIT_S:
                self.events.update(item.event_id, status="skipped",
                                   reason=f"alert dropped: waited {waited:.0f} s in the queue")
                return
        self._refresh_backends()
        muted = self._muted_reason(item.session_id) if item.session_id != "cli-test" else (
            "all muted" if self.muted_all else None)
        if muted:
            self.events.update(item.event_id, status="skipped", reason=muted)
            return
        cfg = self.store.get()
        profile = cfg.profile(item.profile)
        says = self._spoken_says(item, cfg, profile)
        text = " ".join(t for t, _ in says)
        t_start = time.time()
        first = {"queue_wait_ms": round((t_start - item.enqueued_at) * 1000, 1)}
        if text != item.text:
            first["spoken_text"] = text
        self.events.update(item.event_id, status="synthesizing", merge_timings=first)
        segments = self._segments(item, cfg, profile, says)
        if not segments:
            self.events.update(item.event_id, status="skipped", reason="nothing to speak")
            return
        if self.stream_player is not None:
            if await self._play_streaming(item, cfg, segments):
                return
        await self._play_whole(item, cfg, segments)

    async def _play_whole(self, item: QueueItem, cfg, segments: list[Segment]) -> None:
        """The v1 path: render the whole item to a WAV, then afplay it.
        Several segments are rendered one by one and joined (_render_joined)."""
        out = self.clips_dir / f"{item.event_id}.wav"
        joined = await self._render_joined(item, segments, out) if len(segments) > 1 else None
        if joined is not None:
            timings, rate, note = joined
        else:
            if len(segments) > 1:  # no numpy: one render with the first segment's voice
                segments = [Segment(" ".join(s.text for s in segments), segments[0].profile, segments[0].attrs)]
            seg = segments[0]
            result, note = await asyncio.to_thread(self._synthesize, seg.text, seg.profile, dict(seg.attrs), out)
            rate = result.playback_rate
            timings = {
                "stream": False,
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
        finished = await self.player.play(out, rate)
        done = {"play_s": round(time.time() - t_play, 3)}
        if finished and not self._skip_current:
            self.events.update(item.event_id, status="spoken", reason=note, merge_timings=done)
        else:
            reason, self._skip_reason = self._skip_reason or "stopped by user", None
            self.events.update(item.event_id, status="skipped", reason=reason, merge_timings=done)
        self._prune_clips(cfg.daemon.history)

    async def _render_joined(self, item: QueueItem, segments: list[Segment], out: Path):
        """v1 path, several segments: render each to a WAV (with its own
        voice), decode to 24 kHz, and join them with the same gap and
        lead trim as the stream into `out`. Equal paces stay a playback
        rate (as for one segment); mixed paces are time-stretched into the
        clip. Returns (timings, playback rate, note), or None without numpy."""
        import tempfile

        try:
            import numpy as np

            from .stream import TimeStretch, decode_to_stream_rate
        except ImportError:
            return None
        parts: list[tuple[Any, float]] = []
        metas: list[dict[str, Any]] = []
        results = []
        notes: list[str] = []
        with tempfile.TemporaryDirectory(prefix="afterword-parts-") as tmp:
            for i, seg in enumerate(segments):
                if self._skip_current:
                    break
                path = Path(tmp) / f"{i}.wav"
                result, note = await asyncio.to_thread(self._synthesize, seg.text, seg.profile, dict(seg.attrs), path)
                audio = await asyncio.to_thread(decode_to_stream_rate, path)
                parts.append((audio, float(result.playback_rate or 1.0)))
                results.append(result)
                if note:
                    notes.append(note)
                metas.append({
                    "lang": seg.lang or None, "voice": seg.profile.voice or None, "model": result.model,
                    "backend": result.backend or self.synth.name, "seed": result.seed,
                    "audio_s": round(len(audio) / SAMPLE_RATE, 3), "synth_ms": round(result.synth_s * 1000, 1),
                })
        rates = {round(r, 3) for _, r in parts}
        rate = parts[0][1] if len(rates) == 1 else 1.0

        def stretched(audio, r):
            if abs(r - 1.0) <= 0.005:
                return audio
            ts = TimeStretch(r)
            return np.concatenate([ts.process(audio), ts.flush()])

        pieces = []
        for i, (audio, r) in enumerate(parts):
            if i:
                pieces.append(np.zeros(int(SEGMENT_GAP_S * SAMPLE_RATE), np.float32))
                audio = _trim_lead(audio)
            pieces.append(audio if len(rates) == 1 else stretched(audio, r))
        clip = np.concatenate(pieces) if pieces else np.zeros(0, np.float32)
        duration = await asyncio.to_thread(write_wav, out, clip, SAMPLE_RATE)
        synth_s = sum(r.synth_s for r in results)
        timings: dict[str, Any] = {
            "stream": False,
            "synth_ms": round(synth_s * 1000, 1),
            "audio_s": round(sum(m["audio_s"] for m in metas), 3),
            "rtf": round(synth_s / duration, 3) if duration else None,
            "synth_backend": (results[0].backend if results else "") or self.synth.name,
            "segments": metas,
        }
        load_s = sum(r.load_s for r in results)
        if load_s:
            timings["load_ms"] = round(load_s * 1000, 1)
        if results and results[0].model:
            timings["model"] = results[0].model
        if results and results[0].seed is not None:
            timings["seed"] = results[0].seed
        if abs(rate - 1.0) > 0.005:
            timings["playback_rate"] = rate
        return timings, rate, "; ".join(notes) or None

    # -- streaming --------------------------------------------------------

    def _stream_segment(self, seg: Segment, feed: _Feed, cancel: threading.Event):
        """Worker thread: stream one segment into the feed. A Qwen failure
        before any audio of the segment falls back to `say` (into the same
        stream)."""
        try:
            return self.synth.stream(seg.text, seg.profile, dict(seg.attrs), feed, cancel), None
        except Exception as exc:
            if (
                self.synth.name != "qwen" or self._stopping or cancel.is_set() or feed.samples > 0
                or isinstance(exc, (EngineStopped, RenderCancelled, concurrent.futures.CancelledError))
            ):
                raise
            return self._with_fallback(
                exc, seg.profile, lambda s: s.stream(seg.text, seg.profile, dict(seg.attrs), feed, cancel),
                lambda: not (self._stopping or cancel.is_set() or feed.samples > 0),
            )

    def _on_first_audio(self, item: QueueItem, playback) -> None:
        if playback.first_audio_at is None:
            return
        origin = item.t_hook or item.enqueued_at
        self.events.update(
            item.event_id, status="playing",
            merge_timings={"first_audio_ms": round((playback.first_audio_at - origin) * 1000, 1)},
        )

    async def _play_streaming(self, item: QueueItem, cfg, segments: list[Segment]) -> bool:
        """Render the segments chunk by chunk into one audio stream. Returns
        False (nothing done) when the device cannot be opened, so the caller
        uses the v1 path."""
        loop = asyncio.get_running_loop()
        done = asyncio.Event()
        cancel = threading.Event()
        holder: dict[str, Any] = {}

        def later(fn, *args):
            try:
                loop.call_soon_threadsafe(fn, *args)
            except RuntimeError:
                pass  # the loop is closed (shutdown)

        rate = segments[0].pace
        try:
            playback = await asyncio.to_thread(
                self.stream_player.open, rate,
                on_first_audio=lambda: later(self._on_first_audio, item, holder["pb"]),
                on_done=lambda: later(done.set),
            )
        except PlayerUnavailable as exc:
            self._stream_unavailable(f"the audio device could not be opened: {exc}")
            return False
        holder["pb"] = playback
        self.stream_error = None  # the device opened: no longer falling back
        self._current = (cancel, playback)
        if self._skip_current:  # skipped while the device was opening
            cancel.set()
            playback.abort()
        feed = _Feed(playback, cancel)
        metas: list[dict[str, Any]] = []
        rendered = []  # per-segment StreamResults, partial ones included
        notes: list[str] = []
        failure: Exception | None = None
        try:
            for i, seg in enumerate(segments):
                if cancel.is_set():
                    break
                feed.begin(i, seg.pace)
                try:
                    res, note = await asyncio.to_thread(self._stream_segment, seg, feed, cancel)
                except RenderCancelled as exc:
                    part = partial_result(exc)
                    if part is not None:
                        rendered.append(part)
                    metas.append(self._segment_meta(seg, part, feed.samples, cancelled=True))
                    break
                except Exception as exc:
                    if self._stopping or isinstance(exc, (EngineStopped, concurrent.futures.CancelledError)):
                        raise
                    if not feed.clip:
                        raise  # nothing rendered: the worker logs the error
                    failure = exc  # keep what was rendered and played
                    part = partial_result(exc)
                    if part is not None:
                        rendered.append(part)
                    metas.append(self._segment_meta(seg, part, feed.samples, cancelled=True))
                    break
                rendered.append(res)
                metas.append(self._segment_meta(seg, res, feed.samples))
                if note:
                    notes.append(note)
            if cancel.is_set():
                playback.abort()
            else:
                playback.finish()
            await done.wait()
        except BaseException:
            cancel.set()
            playback.abort()
            raise
        finally:
            self._current = None

        out = self.clips_dir / f"{item.event_id}.wav"
        audio, rate = await asyncio.to_thread(feed.clip_audio)
        if audio.size:
            await asyncio.to_thread(write_wav, out, audio, SAMPLE_RATE)
        # Rendered time counts partial renders too (a skipped or failed line
        # still took engine time); None when nothing was rendered at all.
        synth_s = sum(r.synth_s for r in rendered)
        audio_s = sum(m["audio_s"] for m in metas)
        lead = rendered[0] if rendered else None
        timings: dict[str, Any] = {
            "stream": True,
            "synth_ms": round(synth_s * 1000, 1) if rendered else None,
            "audio_s": round(audio_s, 3),
            "rtf": round(synth_s / audio_s, 3) if audio_s and rendered else None,
            "synth_backend": lead.backend if lead else self.synth.name,
            "segments": metas,
        }
        if lead is not None:
            if lead.first_chunk_s is not None:
                timings["first_chunk_ms"] = round(lead.first_chunk_s * 1000, 1)
            if lead.model:
                timings["model"] = lead.model
            if lead.seed is not None:
                timings["seed"] = lead.seed
        load_s = sum(r.load_s for r in rendered)
        if load_s:
            timings["load_ms"] = round(load_s * 1000, 1)
        if abs(rate - 1.0) > 0.005:
            timings["playback_rate"] = rate
        if playback.first_audio_at is not None:
            timings["first_audio_ms"] = round((playback.first_audio_at - (item.t_hook or item.enqueued_at)) * 1000, 1)
        if playback.started_at is not None and playback.ended_at is not None:
            timings["play_s"] = round(playback.ended_at - playback.started_at, 3)
        if playback.underruns:
            timings["underruns"] = playback.underruns
        clip = str(out) if audio.size else None
        note = "; ".join(notes) or None
        skipped = self._skip_current or cancel.is_set()

        if not skipped and playback.error and playback.played_samples == 0 and failure is None and clip:
            # The device failed before anything was heard: play the clip the
            # v1 way.
            self._stream_unavailable(f"the audio stream failed: {playback.error}")
            timings["stream"] = False
            t_play = time.time()
            timings["first_audio_ms"] = round((t_play - (item.t_hook or item.enqueued_at)) * 1000, 1)
            self.events.update(item.event_id, status="playing", clip=clip, merge_timings=timings, reason=note)
            finished = await self.player.play(out, rate)
            done_t = {"play_s": round(time.time() - t_play, 3)}
            if finished and not self._skip_current:
                self.events.update(item.event_id, status="spoken", reason=note, merge_timings=done_t)
            else:
                reason, self._skip_reason = self._skip_reason or "stopped by user", None
                self.events.update(item.event_id, status="skipped", reason=reason, merge_timings=done_t)
            self._prune_clips(cfg.daemon.history)
            return True

        if skipped:
            default = "stopped by user" if playback.first_audio_at is not None else "skipped by user"
            reason, self._skip_reason = self._skip_reason or default, None
            self.events.update(item.event_id, status="skipped", reason=reason, clip=clip, merge_timings=timings)
        elif failure is not None or playback.error:
            err = failure if failure is not None else playback.error
            msg = f"{type(err).__name__}: {err}" if isinstance(err, Exception) else f"audio device: {err}"
            self.events.update(item.event_id, status="error", reason="error", error=msg, clip=clip, merge_timings=timings)
        else:
            self.events.update(item.event_id, status="spoken", reason=note, clip=clip, merge_timings=timings)
        self._prune_clips(cfg.daemon.history)
        return True

    @staticmethod
    def _segment_meta(seg: Segment, res, samples: int, cancelled: bool = False) -> dict[str, Any]:
        meta = {
            "lang": seg.lang or None,
            "voice": seg.profile.voice or None,
            "model": getattr(res, "model", None),
            "backend": getattr(res, "backend", None),
            "seed": getattr(res, "seed", None),
            "audio_s": round(samples / SAMPLE_RATE, 3),
            "synth_ms": round(res.synth_s * 1000, 1) if res is not None else None,
        }
        if cancelled:
            meta["cancelled"] = True
        return meta

    def models_in_use(self) -> list[str]:
        """Qwen model kinds the configured profiles need (per-language voices
        included), in profile order."""
        kinds: list[str] = []
        for profile in self.store.get().profiles.values():
            specs = [profile.voice] + [lv.voice for lv in profile.langs.values() if lv.voice]
            for spec in specs:
                try:
                    kind = MODEL_FOR_KIND.get(parse_voice(spec).kind)
                except VoiceError:
                    continue
                if kind and kind not in kinds:
                    kinds.append(kind)
        return kinds

    def models_to_warm(self, keep_loaded: int) -> list[str]:
        """The models in use to load at start-up: every light one (they do
        not count against keep_loaded) plus the first keep_loaded others."""
        from .qwen import LIGHT_KINDS

        heavy = [k for k in self.models_in_use() if k not in LIGHT_KINDS][:keep_loaded]
        return [k for k in self.models_in_use() if k in LIGHT_KINDS or k in heavy]

    async def warm_models(self) -> None:
        cfg = self.store.get()
        if self.synth.name != "qwen" or not cfg.qwen.warm_on_start or not mlx_audio_available()[0]:
            return
        for kind in self.models_to_warm(cfg.qwen.keep_loaded):
            if self.engine.snapshot_path(kind) is None:
                log.warning("not warming %s: model not downloaded", kind)
                continue
            try:
                load_s = await self.engine.warm_async(kind, priority=QWEN_LIVE)
                if load_s:
                    self.events.insert(kind="system", status="info", reason=f"loaded {model_label(kind)} in {load_s:.1f} s")
            except Exception as exc:
                log.warning("warming %s failed: %s", kind, exc)
                self.events.insert(kind="system", status="error", reason=f"could not load {model_label(kind)}", error=str(exc))

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
        self._abort_playback()
        return item.event_id

    def _abort_playback(self) -> None:
        """Stop the current audio at once and cancel its render."""
        current = self._current
        if current is not None:
            cancel, playback = current
            cancel.set()
            playback.abort()
        self.player.stop()

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
            if item.session_id == session_id and not (item.kind == "alert" and reason == "session off"):
                self.queue.remove(item.event_id)
                self.events.update(item.event_id, status="skipped", reason=reason)
                affected.append(item.event_id)
        now = self.queue.now_playing
        if now is not None and now.session_id == session_id and not (now.kind == "alert" and reason == "session off"):
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
        if self.queue.now_playing is None and self._current is None:
            # Idle: show a hot-reloaded player / stream setting now, not at
            # the next line. (While a line plays it keeps its player.)
            self._refresh_players(cfg)
        return {
            "version": __version__,
            "pid": os.getpid(),
            "started_at": self.started_at,
            "uptime_s": round(time.time() - self.started_at, 1),
            "home": str(self.home),
            "socket": str(common.socket_path()),
            "http": self.http_url,
            "synth": self.synth.name,
            "player": self.stream_player.name if self.stream_player is not None else self.player.name,
            "stream": {
                "enabled": cfg.daemon.stream,
                "active": self.stream_player is not None,
                "error": self.stream_error,
            },
            "engine": self.engine.status(),
            "config": {
                "path": str(self.store.path),
                "error": self.store.error,
                "warnings": self.store.warnings,
                "loaded_at": self.store.loaded_at,
                "profiles": sorted(cfg.profiles),
                "profile_colors": {n: cfg.profiles[n].color or None for n in sorted(cfg.profiles)},
                "fallback": cfg.daemon.fallback,
                "early_say": cfg.daemon.early_say,
                "alerts": cfg.alerts.enabled,
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
            try:  # hourly: drop old bookkeeping-only session state files
                from . import state as session_state

                await asyncio.to_thread(session_state.prune_states)
            except Exception:
                log.exception("state prune failed")

    def request_shutdown(self) -> None:
        self._stopping = True
        self._stop_event.set()
        alerting.cancel_running()  # a Generate in progress: uvicorn would wait for it
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
        # A Generate request's `claude -p` runs in an executor thread that
        # asyncio.run would wait for at exit: end it now.
        with contextlib.suppress(Exception):
            alerting.cancel_running()
        if daemon is not None:
            daemon._stopping = True
            with contextlib.suppress(Exception):
                daemon.lab.explorer.close()  # the seed explorer's loop
            daemon.engine.shutdown()  # cancel queued renders before the worker is cancelled
            with contextlib.suppress(Exception):
                daemon._abort_playback()
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
