import json
import shlex
from pathlib import Path

from agent_quota.desktop import (
    autostart_enabled,
    claude_setup,
    desktop_path,
    install_launcher,
    set_autostart,
)
from agent_quota.state import read_snapshot


def test_launcher_and_autostart_are_separate_and_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    executable = Path("/test/project with spaces/.venv/bin/agent-quota")
    launcher = install_launcher(executable)
    assert launcher == tmp_path / "data/applications/agent-quota.desktop"
    assert f'Exec="{executable}"\n' in launcher.read_text()
    assert not autostart_enabled()

    set_autostart(True, executable)
    assert autostart_enabled()
    assert (
        f'Exec="{executable}" --background\n'
        in desktop_path(autostart=True).read_text()
    )
    set_autostart(False, executable)
    assert not autostart_enabled()
    assert launcher.exists()


def test_helper_preserves_original_status_line(tmp_path, monkeypatch, capfd):
    from agent_quota.__main__ import main

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    payload = {
        "rate_limits": {"five_hour": {"used_percentage": 20}},
        "context_window": {},
    }
    with (tmp_path / "stdin").open("w+") as stream:
        json.dump(payload, stream)
        stream.seek(0)
        monkeypatch.setattr("sys.stdin", stream)
        assert main(["capture-claude", "--then", "cat"]) == 0
    assert json.loads(capfd.readouterr().out) == payload
    snapshot = read_snapshot("claude")
    assert snapshot is not None
    assert snapshot.windows[0].remaining == 80


def test_copied_claude_setup_keeps_prior_command_and_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    executable = Path("/test/project with spaces/.venv/bin/agent-quota")
    previous = """jq -r '.model.display_name // "No model"'"""
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {"statusLine": {"type": "command", "command": previous, "padding": 2}}
        )
    )
    setup = json.loads(claude_setup(executable))
    assert shlex.split(setup["statusLine"]["command"]) == [
        str(executable),
        "capture-claude",
        "--then",
        previous,
    ]
    assert setup["statusLine"]["padding"] == 2
    settings.write_text(json.dumps(setup))
    assert json.loads(claude_setup(executable)) == setup
