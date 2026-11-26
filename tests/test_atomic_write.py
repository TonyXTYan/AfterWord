"""hooks.common.atomic_write: temp file in the same directory, fsync, rename."""

import json
import os
import stat
import sys

import pytest

from afterword.hooks import common


def test_writes_text_and_bytes_with_the_mode(tmp_path):
    p = tmp_path / "a.json"
    common.atomic_write(p, '{"x": "é"}')
    assert p.read_text(encoding="utf-8") == '{"x": "é"}'
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    common.atomic_write(p, b"\x00\x01", mode=0o644)
    assert p.read_bytes() == b"\x00\x01" and stat.S_IMODE(p.stat().st_mode) == 0o644
    assert os.listdir(tmp_path) == ["a.json"]  # no temp file left behind


def test_failed_rename_keeps_the_old_file_and_leaves_no_temp(tmp_path, monkeypatch):
    p = tmp_path / "s.json"
    p.write_text("old")

    def boom(src, dst):
        raise OSError("rename failed")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        common.atomic_write(p, "new")
    assert p.read_text() == "old"
    assert os.listdir(tmp_path) == ["s.json"]


def test_fsyncs_the_file_before_the_rename_and_then_the_directory(tmp_path, monkeypatch):
    calls = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        calls.append("fsync")
        real_fsync(fd)

    def replace(a, b):
        calls.append("replace")
        real_replace(a, b)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    common.atomic_write(tmp_path / "f", "x")
    assert calls == ["fsync", "replace", "fsync"]


@pytest.mark.skipif(sys.platform != "darwin", reason="F_FULLFSYNC is macOS")
def test_durable_uses_full_fsync_on_macos(tmp_path, monkeypatch):
    import fcntl

    ops = []
    monkeypatch.setattr(fcntl, "fcntl", lambda fd, op, *a: ops.append(op))
    common.atomic_write(tmp_path / "f", "x", durable=True)
    assert ops == [fcntl.F_FULLFSYNC] * 2  # the file, then the directory
    ops.clear()
    common.atomic_write(tmp_path / "f", "y")
    assert ops == []


def test_state_and_voice_writers_use_it(aw_home, monkeypatch):
    seen = []
    real = common.atomic_write

    def spy(path, *a, **k):
        seen.append(os.path.basename(str(path)))
        real(path, *a, **k)

    monkeypatch.setattr(common, "atomic_write", spy)
    common.save_state("sess-1", {"profile": "nova"})
    common._save_voice_usage({"nova": {"n": 1}})
    assert seen == ["sess-1.json", "voices.json"]
    assert common.load_state("sess-1")["profile"] == "nova"
    assert json.loads(common.voices_path().read_text())["profiles"] == {"nova": {"n": 1}}
