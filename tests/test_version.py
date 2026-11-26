"""Every copy of the version agrees (see AGENTS.md, Versioning)."""

import json
import re
import tomllib
from pathlib import Path

from afterword import __version__

ROOT = Path(__file__).resolve().parent.parent


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)


def test_pyproject_and_lock_match():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == __version__
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    m = re.search(r'\[\[package\]\]\nname = "afterword"\nversion = "([^"]+)"', lock)
    assert m and m.group(1) == __version__, "run `uv lock` after a bump"


def test_plugin_manifests_match():
    for rel in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
        assert json.loads((ROOT / rel).read_text(encoding="utf-8"))["version"] == __version__, rel
    market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
    assert [p["version"] for p in market["plugins"] if p["name"] == "afterword"] == [__version__]


def test_changelog_has_this_version():
    assert f"## {__version__} " in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
