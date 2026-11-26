"""Voice lab seed explorer, stars and stability check (docs/design-v2.md, A).

The explorer renders one voice setting with seeds start, start+1, ... one
render at a time at lab priority, staying `ahead` takes past the page's
cursor. Its renders stream and are preemptible: live speech queued while one
runs stops it at its next chunk, and the same seed is rendered again after
the speech (RenderPreempted). It does not start a take while the daemon has
live speech to say, or within LIVE_QUIET_S of a live render: a reply's
segments are rendered one after another, and a take started in the gap
between two would cost the next one a pre-emption (and maybe a model swap).
Takes are previews (playable, freezable,
starrable) kept until the exploration is replaced or the daemon restarts.

Stars are copies of takes (or any preview) in ~/.afterword/lab/starred/ with
an index in ~/.afterword/lab/starred.json, so they survive restarts (the
previews directory is cleared at start; starred/ is not).

HTTP contract: docs/lab-api.md ("Seed explorer").
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import secrets
import shutil
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from afterword.hooks import common

from . import qwen, voices

if TYPE_CHECKING:
    from .lab import Lab

log = logging.getLogger("afterword.explore")

MAX_TAKES = 500  # an exploration stops after this many takes
IDLE_PAUSE_S = 600.0  # pause when the cursor has not moved for this long
AHEAD_DEFAULT = 4
AHEAD_MAX = 20
SEED_MAX = 2**31 - 1
MAX_FAILURES = 3  # consecutive failed renders before the exploration gives up
LIVE_QUIET_S = 1.0  # after live speech, wait this long with no live render before the next take
LIVE_POLL_S = 0.2
LIVE_HOLD = "holding for live speech"  # the state's reason meanwhile

MAX_STARS = 1000
NOTE_MAX = 300
STAR_ID_RE = re.compile(r"^s[0-9a-f]{10}$")
STAR_PREFIX = "s"

# Stability check: two sentences in the take's language and one in another
# (English for every language but English, which gets Chinese).
STABILITY_TEXTS = {
    "en": ["Quick update: the build is green and the fix is pushed.",
           "Hmm, that is odd. It passes on my machine, so let me read the logs before we change anything."],
    "zh": ["好消息：构建通过了，修复也已经推上去了。",
           "嗯，有点奇怪。在我这儿是通过的，我先看看日志再说。"],
    "ja": ["お知らせです。ビルドは成功して、修正もプッシュしました。",
           "うーん、変ですね。手元では通るので、まずログを見てみます。"],
    "ko": ["간단히 알려 드려요. 빌드는 통과했고 수정도 올렸어요.",
           "음, 이상하네요. 제 컴퓨터에서는 통과하니까 먼저 로그를 볼게요."],
    "es": ["Una novedad rápida: la compilación pasó y ya subí el arreglo.",
           "Mmm, qué raro. En mi máquina funciona, así que voy a revisar los registros primero."],
    "fr": ["Petite nouvelle : la compilation passe et le correctif est envoyé.",
           "Hum, c'est étrange. Ça marche chez moi, alors je vais d'abord lire les journaux."],
    "de": ["Kurze Info: Der Build ist grün und der Fix ist hochgeladen.",
           "Hm, seltsam. Bei mir läuft es, also schaue ich mir zuerst die Logs an."],
    "it": ["Breve aggiornamento: la build è verde e la correzione è stata inviata.",
           "Mmm, strano. Sul mio computer funziona, quindi prima guardo i log."],
    "pt": ["Uma novidade rápida: a compilação passou e a correção já foi enviada.",
           "Hum, que estranho. Na minha máquina funciona, então vou olhar os registros primeiro."],
    "ru": ["Короткая новость: сборка прошла, исправление уже отправлено.",
           "Хм, странно. У меня всё работает, так что сначала посмотрю логи."],
}


def stability_plan(lang: str | None, text: str) -> list[tuple[str, str]]:
    """[(lang, sentence)] x3: two in `lang` (en for auto/unknown), one in
    another language. A sentence equal to the take's own text is swapped for
    the language's sample sentence."""
    from .voices import SAMPLES

    code = lang if lang in STABILITY_TEXTS else "en"
    other = "zh" if code == "en" else "en"
    own = [t for t in STABILITY_TEXTS[code] if t.strip() != text.strip()]
    if len(own) < 2 and SAMPLES.get(code) and SAMPLES[code].strip() != text.strip():
        own.append(SAMPLES[code])
    return [(code, t) for t in own[:2]] + [(other, STABILITY_TEXTS[other][0])]


def semitones(f: Any, base: Any) -> float | None:
    if not isinstance(f, (int, float)) or not isinstance(base, (int, float)) or f <= 0 or base <= 0:
        return None
    return round(12 * math.log2(f / base), 2)


class _Exploration:
    def __init__(self, xid: str, prep: dict[str, Any], start_seed: int, ahead: int):
        self.id = xid
        self.prep = prep  # Lab._prepare: text, spoken, settings, spec, skip, pron_name
        self.start_seed = start_seed
        self.ahead = ahead
        self.cursor = start_seed
        self.next_seed = start_seed
        self.takes: OrderedDict[str, dict[str, Any]] = OrderedDict()  # preview id -> entry
        self.status = "running"
        self.reason: str | None = None
        self.rendering: int | None = None
        self.count = 0
        self.preempted = 0
        self.failed = 0
        self.started_at = time.time()
        self.moved_at = time.monotonic()
        self.moved_wall = self.started_at
        self.stopped = False
        self.cancel: threading.Event | None = None
        self.wake = asyncio.Event()
        self.task: asyncio.Task | None = None


class Explorer:
    """One exploration at a time; a new one replaces (and discards) the old."""

    def __init__(self, lab: "Lab"):
        self.lab = lab
        self.x: _Exploration | None = None

    # ------------------------------------------------------------------ #
    # Requests (event loop)                                              #
    # ------------------------------------------------------------------ #

    async def start(self, body: Any) -> dict[str, Any]:
        from .lab import LabError, _opt_num

        if not isinstance(body, dict):
            raise LabError(400, "expected a JSON object")
        settings = body.get("settings")
        if not isinstance(settings, dict):
            raise LabError(400, "settings must be an object (the fields of /api/lab/preview, with text)")
        if settings.get("seed") not in (None, ""):
            raise LabError(400, "settings.seed is not used here: give start_seed")
        prep = self.lab._prepare(settings)
        if prep["spec"].kind not in voices.SEEDED_KINDS:
            raise LabError(400, f"{prep['spec'].kind} voices have no seed to explore; "
                                "pick a Qwen3-TTS or Chatterbox voice")
        start_seed = _opt_num(body, "start_seed", int, 0, SEED_MAX)
        ahead = _opt_num(body, "ahead", int, 1, AHEAD_MAX)
        start_seed = 1 if start_seed is None else start_seed
        ahead = AHEAD_DEFAULT if ahead is None else ahead
        # Before the old exploration (and its takes) goes: a missing clip or
        # model is an error now, not after the takes are deleted.
        self.lab.check_voice(prep)
        self._discard(self.x)
        x = _Exploration(secrets.token_hex(4), prep, start_seed, ahead)
        self.x = x
        x.task = asyncio.get_running_loop().create_task(self._run(x), name="explore")
        self._publish_state(x)
        return self.view()

    def move(self, body: Any) -> dict[str, Any]:
        from .lab import LabError, _opt_num

        x = self.x
        if x is None:
            raise LabError(404, "no exploration; start one first")
        if not isinstance(body, dict):
            raise LabError(400, "expected a JSON object")
        seed = _opt_num(body, "seed", int, 0, SEED_MAX)
        if seed is None:
            raise LabError(400, "seed is required")
        auto = body.get("auto", False)
        if not isinstance(auto, bool):
            raise LabError(400, "auto must be true or false")
        # The cursor is a take the page shows: never past the next seed to render.
        x.cursor = max(x.start_seed, min(seed, x.next_seed))
        if not auto:  # auto-advance does not count as being there
            x.moved_at = time.monotonic()
            x.moved_wall = time.time()
            if x.status == "paused":
                x.status, x.reason = "running", None
        x.wake.set()
        self._publish_state(x)
        return {"state": self.state()}

    def stop(self) -> dict[str, Any]:
        x = self.x
        if x is not None and not x.stopped and x.status not in ("done", "error"):
            self._halt(x, "stopped", None)
        return {"state": self.state()}

    def view(self) -> dict[str, Any]:
        x = self.x
        takes = [e["response"] for e in x.takes.values()] if x else []
        return {"state": self.state(), "takes": takes}

    def state(self) -> dict[str, Any]:
        x = self.x
        base = {"max_takes": MAX_TAKES, "pause_after_s": IDLE_PAUSE_S, "ahead_max": AHEAD_MAX}
        if x is None:
            return {"id": None, "status": "idle", "reason": None, **base}
        s = x.prep["settings"]
        return {
            "id": x.id,
            "status": x.status,
            "reason": x.reason,
            "text": x.prep["text"],
            "spoken_text": x.prep["spoken"] if x.prep["spoken"] != x.prep["text"] else None,
            "settings": {k: s.get(k) for k in ("voice", "ref_text", "lang", "pace", "style", "mood", "temperature")},
            "start_seed": x.start_seed,
            "ahead": x.ahead,
            "cursor": x.cursor,
            "next_seed": x.next_seed,
            "rendering": x.rendering,
            "count": x.count,
            "preempted": x.preempted,
            "failed": x.failed,
            "started_at": x.started_at,
            "moved_at": x.moved_wall,
            **base,
        }

    def entry(self, pid: str) -> dict[str, Any] | None:
        x = self.x
        return x.takes.get(pid) if x else None

    def close(self) -> None:
        """Daemon shutdown: stop the loop (files stay; cleared at next start)."""
        x = self.x
        if x is not None:
            x.stopped = True
            if x.cancel is not None:
                x.cancel.set()
            if x.task is not None:
                x.task.cancel()

    # ------------------------------------------------------------------ #
    # Internals                                                          #
    # ------------------------------------------------------------------ #

    def _publish_state(self, x: _Exploration) -> None:
        if x is self.x:
            self.lab.publish("explore", {"type": "state", "state": self.state()})

    def _halt(self, x: _Exploration, status: str, reason: str | None) -> None:
        x.stopped = True
        x.status, x.reason = status, reason
        x.rendering = None
        if x.cancel is not None:
            x.cancel.set()
        x.wake.set()
        self._publish_state(x)

    def _discard(self, x: _Exploration | None) -> None:
        """Replace: stop the old exploration and delete its take files."""
        if x is None:
            return
        x.stopped = True
        x.status, x.reason = "stopped", "replaced by a new exploration"
        if x.cancel is not None:
            x.cancel.set()
        x.wake.set()
        for e in x.takes.values():
            Path(e["path"]).unlink(missing_ok=True)
        x.takes.clear()

    def _set(self, x: _Exploration, status: str, reason: str | None = None) -> None:
        if (x.status, x.reason) != (status, reason):
            x.status, x.reason = status, reason
            self._publish_state(x)

    async def _sleep(self, x: _Exploration, timeout: float | None) -> None:
        try:
            await asyncio.wait_for(x.wake.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        x.wake.clear()

    def _live_busy(self) -> bool:
        if self.lab.live_busy():
            return True
        quiet = getattr(self.lab.engine, "live_quiet_s", None)
        return quiet is not None and quiet() < LIVE_QUIET_S

    async def _run(self, x: _Exploration) -> None:
        try:
            await self._loop(x)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a bug must not leave the state "running"
            log.exception("seed explorer failed")
            if not x.stopped:
                self._halt(x, "error", f"{type(exc).__name__}: {exc}")

    async def _loop(self, x: _Exploration) -> None:
        from .lab import LabError

        failures = 0
        while not x.stopped:
            if x.count >= MAX_TAKES:
                self._halt(x, "done", f"stopped after {MAX_TAKES} takes; start again to go on")
                return
            if x.next_seed > SEED_MAX:
                self._halt(x, "done", "no more seeds")
                return
            if x.next_seed > x.cursor + x.ahead:
                self._set(x, "waiting", f"{x.ahead} takes ready past the cursor")
                await self._sleep(x, None)
                continue
            if time.monotonic() - x.moved_at >= IDLE_PAUSE_S:
                self._set(x, "paused", f"the cursor has not moved for {int(IDLE_PAUSE_S // 60)} minutes; move it to go on")
                await self._sleep(x, None)
                continue
            if self._live_busy():
                if (x.status, x.reason, x.rendering) != ("running", LIVE_HOLD, None):
                    x.status, x.reason, x.rendering = "running", LIVE_HOLD, None
                    self._publish_state(x)
                await self._sleep(x, LIVE_POLL_S)
                continue
            seed = x.next_seed
            x.rendering = seed
            x.cancel = threading.Event()
            if x.status != "running" or x.reason is not None:
                x.status, x.reason = "running", None
            self._publish_state(x)
            prep = x.prep
            settings = dict(prep["settings"], seed=seed)
            out = self.lab.new_preview_path()
            try:
                meta = await self.lab._render(prep["spec"], prep["spoken"], settings, out,
                                              cancel=x.cancel, preemptible=True)
            except qwen.RenderPreempted:
                out.unlink(missing_ok=True)
                x.preempted += 1
                x.rendering = None
                continue  # the same seed again, after the live speech
            except qwen.RenderCancelled:
                out.unlink(missing_ok=True)
                x.rendering = None
                continue  # stopped or replaced: the loop condition ends it
            except LabError as exc:
                out.unlink(missing_ok=True)
                if x.stopped:
                    return
                self._halt(x, "error", exc.message)
                return
            except Exception as exc:
                out.unlink(missing_ok=True)
                if x.stopped:
                    return
                failures += 1
                x.failed += 1
                log.warning("seed %d failed: %s: %s", seed, type(exc).__name__, exc)
                if failures >= MAX_FAILURES:
                    self._halt(x, "error", f"{failures} renders in a row failed; last: {type(exc).__name__}: {exc}")
                    return
                x.next_seed += 1
                x.rendering = None
                continue
            finally:
                x.cancel = None
            failures = 0
            if x is not self.x:  # replaced while the file was written
                out.unlink(missing_ok=True)
                return
            pid = out.stem
            response = self.lab._response(pid, prep, meta, settings)
            response["explore_id"] = x.id
            x.takes[pid] = {"path": out, "text": prep["spoken"], "response": response}
            x.count += 1
            x.next_seed += 1
            x.rendering = None
            self.lab.publish("explore", {"type": "take", "take": response})
            self._publish_state(x)


class StarStore:
    """Starred takes: lab/starred/<id>.wav plus lab/starred.json."""

    def __init__(self, lab_dir: Path):
        self.dir = lab_dir / "starred"
        self.index = lab_dir / "starred.json"
        self._lock = threading.Lock()
        self.stars: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.error: str | None = None
        self._load()

    def _load(self) -> None:
        if not self.index.is_file():
            return
        try:
            data = json.loads(self.index.read_text(encoding="utf-8"))
            items = data.get("stars") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise ValueError("no stars list")
        except (OSError, ValueError) as exc:
            # Keep the unreadable file for the user; start a fresh index.
            aside = self.index.with_name(f"starred.json.bad-{int(time.time())}")
            try:
                os.replace(self.index, aside)
            except OSError:
                pass
            self.error = f"starred.json could not be read ({exc}); moved to {aside.name}"
            log.warning("%s", self.error)
            return
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not STAR_ID_RE.match(item["id"]):
                continue
            if not (self.dir / f"{item['id']}.wav").is_file():
                continue
            if not isinstance(item.get("settings"), dict):
                continue
            self.stars[item["id"]] = item

    def _save(self) -> None:
        self.index.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        text = json.dumps({"version": 1, "stars": list(self.stars.values())}, ensure_ascii=False, indent=1)
        common.atomic_write(self.index, text)

    def path(self, sid: str) -> Path | None:
        if not isinstance(sid, str) or sid not in self.stars:
            return None
        return self.dir / f"{sid}.wav"

    def get(self, sid: Any) -> dict[str, Any] | None:
        return self.stars.get(sid) if isinstance(sid, str) else None

    @staticmethod
    def view(star: dict[str, Any]) -> dict[str, Any]:
        return {**star, "url": f"/api/lab/stars/{star['id']}.wav"}

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self.view(s) for s in reversed(self.stars.values())]  # newest first

    def add(self, pid: str, entry: dict[str, Any], note: str | None, f0: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Copy a preview into the store. Returns (star, created); starring the
        same preview again returns the existing star."""
        with self._lock:
            for star in self.stars.values():
                if star.get("preview_id") == pid:
                    if note is not None and note != star.get("note"):
                        star["note"] = note
                        self._save()
                    return self.view(star), False
            if len(self.stars) >= MAX_STARS:
                raise OverflowError(f"at most {MAX_STARS} stars; delete some first")
            sid = STAR_PREFIX + secrets.token_hex(5)
            self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            dest = self.dir / f"{sid}.wav"
            shutil.copyfile(entry["path"], dest)
            res = entry["response"]
            star = {
                "id": sid,
                "created": time.time(),
                "note": note or "",
                "preview_id": pid,
                "text": res.get("text"),
                "spoken": entry.get("text"),
                "skip_pronounce": bool(res.get("skip_pronounce")),
                "pronounce_profile": res.get("pronounce_profile"),
                "settings": dict(res.get("settings") or {}),
                "seed": res.get("seed"),
                "audio_s": res.get("audio_s"),
                "model": res.get("model"),
                "backend": res.get("backend"),
                "f0_hz": f0.get("f0_hz"),
                "f0_spread_hz": f0.get("f0_spread_hz"),
            }
            self.stars[sid] = star
            try:
                self._save()
            except BaseException:
                self.stars.pop(sid, None)
                dest.unlink(missing_ok=True)
                raise
            return self.view(star), True

    def remove(self, sid: str) -> bool:
        with self._lock:
            star = self.stars.pop(sid, None) if isinstance(sid, str) else None
            if star is None:
                return False
            try:
                self._save()
            except BaseException:
                self.stars[sid] = star
                raise
            (self.dir / f"{sid}.wav").unlink(missing_ok=True)
            return True
