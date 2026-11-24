import json
import os
import stat

import pytest

from afterword import cli
from conftest import wait_for


def run_install(path, *extra):
    return cli.main(["install-hooks", "--settings", str(path), "--python", "/usr/bin/python3", *extra])


def test_install_is_idempotent_and_keeps_other_hooks(tmp_path):
    path = tmp_path / "settings.json"
    other = {"type": "command", "command": "other-tool hook"}
    path.write_text(json.dumps({"env": {"K": "v"}, "hooks": {"Stop": [{"hooks": [other]}]}}))
    assert run_install(path) == 0
    assert run_install(path) == 0
    data = json.loads(path.read_text())
    assert data["env"] == {"K": "v"}
    stop = [h for g in data["hooks"]["Stop"] for h in g["hooks"]]
    assert other in stop
    ours = [h for h in stop if "afterword_hook.py" in h["command"]]
    assert len(ours) == 1 and ours[0]["async"] is True
    assert ours[0]["command"].startswith("/usr/bin/python3 ") and ours[0]["command"].endswith(" stop")
    assert run_install(path, "--uninstall") == 0
    data = json.loads(path.read_text())
    assert [h for g in data["hooks"]["Stop"] for h in g["hooks"]] == [other]


def test_install_keeps_mode_and_symlink(tmp_path):
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text("{}")
    os.chmod(real, 0o600)
    link = tmp_path / "settings.json"
    link.symlink_to(real)
    assert run_install(link) == 0
    assert link.is_symlink()
    assert stat.S_IMODE(real.stat().st_mode) == 0o600
    assert "afterword_hook.py" in real.read_text()
    backups = list(real.parent.glob("settings.json.afterword-backup-*"))
    assert backups and stat.S_IMODE(backups[0].stat().st_mode) == 0o600


def test_new_settings_file_is_private(tmp_path):
    path = tmp_path / "new" / "settings.json"
    assert run_install(path) == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_install_refuses_invalid_json(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json")
    assert run_install(path) == 1
    assert path.read_text() == "{not json"


def test_hook_command_quotes_paths_with_spaces():
    cmd = cli.hook_command("/Applications/My Python/bin/python3")
    assert cmd.startswith("'/Applications/My Python/bin/python3' ")


def test_cli_test_fallback_respects_none(aw_home, say_stub, monkeypatch):
    monkeypatch.setenv("AFTERWORD_FALLBACK", "none")
    assert cli.main(["test", "quiet please"]) == 1
    assert not say_stub.exists()


def test_cli_test_fallback_uses_say(aw_home, say_stub):
    assert cli.main(["test", "Spoken by say. [[slnc 9999]]"]) == 0
    said = wait_for(lambda: say_stub.exists() and say_stub.read_text().strip())
    assert said == "Spoken by say."


def test_status_when_down(aw_home, capsys):
    assert cli.main(["status"]) == 1
    assert "not running" in capsys.readouterr().out
