"""`afterword voices`, `afterword voices describe`, and the describe-voices
command file installed by `install-hooks`."""
import json
import time
from pathlib import Path

import pytest

from afterword import cli

ROOT = Path(__file__).resolve().parent.parent

CONFIG = """# my voices
[profiles.nova]
voice = "qwen:Ryan"  # the main one
lang = "en"
style = "calm"
persona = "a dry narrator"
description = "calm narrator"

[profiles.ember]
voice = "qwen:Serena"
lang = "zh"
"""


@pytest.fixture
def home(aw_home):
    (aw_home / "config.toml").write_text(CONFIG, encoding="utf-8")
    return aw_home


def test_list_text(home, capsys):
    now = time.time()
    (home / "voices.json").write_text(json.dumps({"v": 1, "profiles": {
        "nova": {"last_used": now, "last_session": "s1", "sessions": {"s1": {"ts": now, "cwd": "/x"}}}}}))
    assert cli.main(["voices"]) == 0
    out = capsys.readouterr().out
    assert out.index("nova") < out.index("ember")  # config order
    assert "qwen:Ryan" in out and "calm narrator" in out and "a dry narrator" in out
    assert "in use by 1 session" in out
    assert "description: (none)" in out and "never used" in out


def test_list_last_used(home, capsys):
    (home / "voices.json").write_text(json.dumps({"v": 1, "profiles": {
        "ember": {"last_used": time.time() - 3 * 86400, "sessions": {}}}}))
    assert cli.main(["voices"]) == 0
    assert "last used 3d ago" in capsys.readouterr().out


def test_list_json_matches_api_shape(home, capsys):
    assert cli.main(["voices", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    names = [p["name"] for p in data["profiles"]]
    assert names == ["nova", "ember"]
    nova = data["profiles"][0]
    assert nova["description"] == "calm narrator" and nova["persona"] == "a dry narrator"
    assert nova["in_use"] == [] and nova["last_used"] is None


def test_describe_set_and_remove_keeps_comments(home, capsys):
    assert cli.main(["voices", "describe", "ember", "warm Mandarin reader"]) == 0
    out = capsys.readouterr().out
    assert "was: (none)" in out and "now: warm Mandarin reader" in out
    text = (home / "config.toml").read_text()
    assert 'description = "warm Mandarin reader"' in text
    assert "# my voices" in text and "# the main one" in text
    assert cli.main(["voices", "describe", "nova", ""]) == 0
    out = capsys.readouterr().out
    assert "was: calm narrator" in out and "now: (none)" in out
    text = (home / "config.toml").read_text()
    assert "calm narrator" not in text and "# my voices" in text and "# the main one" in text
    assert cli.main(["voices", "--json"]) == 0
    profiles = {p["name"]: p for p in json.loads(capsys.readouterr().out)["profiles"]}
    assert profiles["nova"]["description"] == "" and profiles["ember"]["description"] == "warm Mandarin reader"


def test_describe_unknown_profile_lists_profiles(home, capsys):
    assert cli.main(["voices", "describe", "nope", "x"]) == 1
    err = capsys.readouterr().err
    assert "nope" in err and "nova" in err and "ember" in err
    assert "nope" not in (home / "config.toml").read_text()


def test_describe_too_long(home, capsys):
    assert cli.main(["voices", "describe", "nova", "x" * 301]) == 1
    assert "300" in capsys.readouterr().err
    assert "calm narrator" in (home / "config.toml").read_text()


def test_command_file():
    text = (ROOT / "commands/describe-voices.md").read_text()
    assert text.startswith("---\n") and "description:" in text and "$ARGUMENTS" in text
    assert 'argument-hint: "[profile …]"' in text and "disable-model-invocation: true" in text
    assert cli.DESCRIBE_COMMAND_MARKER in text
    assert "afterword voices --json" in text and "afterword voices describe" in text


def test_install_hooks_writes_both_command_files(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    assert cli.main(["install-hooks", "--settings", str(settings), "--python", "/usr/bin/python3"]) == 0
    cmds = tmp_path / ".claude" / "commands"
    voice = (cmds / "afterword-voice.md").read_text()
    desc = (cmds / "afterword-describe-voices.md").read_text()
    assert cli.VOICE_COMMAND_MARKER in voice and cli.DESCRIBE_COMMAND_MARKER in desc
    assert "afterword voices describe" in desc
    assert cli.main(["install-hooks", "--settings", str(settings), "--uninstall"]) == 0
    assert not (cmds / "afterword-voice.md").exists() and not (cmds / "afterword-describe-voices.md").exists()


def test_install_hooks_leaves_someone_elses_describe_command(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    cmd = tmp_path / ".claude" / "commands" / "afterword-describe-voices.md"
    cmd.parent.mkdir(parents=True)
    cmd.write_text("mine")
    cli.main(["install-hooks", "--settings", str(settings)])
    assert cmd.read_text() == "mine"
    cli.main(["install-hooks", "--settings", str(settings), "--uninstall"])
    assert cmd.read_text() == "mine"
