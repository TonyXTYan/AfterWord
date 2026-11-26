"""The GUI's clip player (afterword/ui/index.html). Every clip plays through
Web Audio in <aw-audio>, never an <audio> element: a playing media element
makes the browser the macOS Now Playing app, so the keyboard's media keys
would drive AfterWord's clips instead of the user's music. The pitch-kept
time-stretch behind playbackRate runs in node (skipped without node)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parent.parent / "afterword" / "ui" / "index.html"
NODE = shutil.which("node")


def _script() -> str:
    src = UI.read_text(encoding="utf-8")
    return src[src.index("<script>"):]


def test_no_media_elements_anywhere():
    src = UI.read_text(encoding="utf-8")
    script = _script()
    assert not re.search(r"<audio[\s>]", src.replace("<audio> element", ""))
    assert not re.search(r"<video[\s>]", src)
    assert "new Audio(" not in script
    assert not re.search(r"""createElement\(\s*["']audio""", script)
    assert not re.search(r"""\bh\(\s*["']audio["']""", script)
    assert 'customElements.define("aw-audio", AwAudio)' in script


def test_css_targets_the_player_not_audio():
    css = UI.read_text(encoding="utf-8").split("<script>")[0]
    assert not re.search(r"(^|[\s,>+~])audio\s*[{,:\[]", css, re.M)


HARNESS = r"""
const block = require("fs").readFileSync(0, "utf8");
const stretchChannels = new Function(block + "\nreturn stretchChannels;")();
const sr = 24000, f = 220, secs = 1.0, len = Math.round(sr * secs);
const sine = (amp, freq) => Float32Array.from({length: len}, (_, i) => amp * Math.sin(2 * Math.PI * freq * i / sr));
function freqOf(x) {   // zero crossings in the middle 80%
  const a = Math.floor(x.length * 0.1), b = Math.floor(x.length * 0.9);
  let n = 0;
  for (let i = a + 1; i < b; i++) if ((x[i - 1] < 0) !== (x[i] < 0)) n++;
  return n / 2 / ((b - a) / sr);
}
function rms(x) {
  const a = Math.floor(x.length * 0.1), b = Math.floor(x.length * 0.9);
  let s = 0; for (let i = a; i < b; i++) s += x[i] * x[i];
  return Math.sqrt(s / (b - a));
}
const out = {};
for (const rate of [0.5, 0.8, 1.25, 2.0]) {
  const [l, r] = stretchChannels([sine(0.5, f), sine(0.25, f)], sr, rate);
  out[rate] = {len: l.length, rlen: r.length, freq: freqOf(l), rms: rms(l), rrms: rms(r)};
}
const same = stretchChannels([sine(0.5, f)], sr, 1.002)[0];
out.unity = {len: same.length, rms: rms(same)};
const t0 = Date.now();
stretchChannels([Float32Array.from({length: 48000 * 10}, (_, i) => Math.sin(i / 7) * Math.sin(i / 3000))], 48000, 1.3);
out.ms_10s_48k = Date.now() - t0;
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def stretch(tmp_path_factory):
    if NODE is None:
        pytest.skip("node is not installed")
    src = UI.read_text(encoding="utf-8")
    block = src[src.index("function stretchChannels("):src.index("// ---------- end stretch")]
    harness = tmp_path_factory.mktemp("ui") / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run([NODE, str(harness)], input=block, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize("rate", ["0.5", "0.8", "1.25", "2"])
def test_stretch_changes_length_not_pitch(stretch, rate):
    r = stretch[rate]
    assert r["len"] == r["rlen"] == round(24000 / float(rate))
    assert abs(r["freq"] - 220) < 220 * 0.03
    assert abs(r["rms"] - 0.5 / 2 ** 0.5) < 0.05    # level kept, no gaps or doubling
    assert abs(r["rrms"] - 0.25 / 2 ** 0.5) < 0.03  # every channel, same alignment


def test_stretch_near_one_is_a_copy(stretch):
    assert stretch["unity"]["len"] == 24000
    assert abs(stretch["unity"]["rms"] - 0.5 / 2 ** 0.5) < 1e-3


def test_stretch_is_fast_enough_to_run_on_play(stretch):
    assert stretch["ms_10s_48k"] < 1500


def test_a_play_from_silence_opens_the_current_default_output():
    """A long-lived AudioContext can stay tied to an output that is gone
    (headphones unplugged): a play that starts from silence gets a fresh one,
    the old one is closed, and decoding does not depend on either."""
    script = _script()
    play = script[script.index("  play() {"):script.index("  pause() {")]
    assert "awPlayContext()" in play
    pick = script[script.index("function awPlayContext()"):script.index("let awDecoder")]
    assert "AW_CTX_IDLE_MS" in pick and "awNewContext()" in pick
    assert "old.close()" in script[script.index("function awNewContext()"):script.index("function awContext()")]
    assert "decodeAudioData" not in script[script.index("class AwAudio"):script.index('customElements.define("aw-audio"')]
    assert "OfflineAudioContext" in script[script.index("function awDecode("):script.index("function awDecode(") + 300]
    assert 'addEventListener("devicechange"' in script
    # the clock of a playing clip is read from the context it plays on
    assert "awCtx.currentTime" not in script
