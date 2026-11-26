import json
import os
import shlex
import sys
from pathlib import Path

from .quota import obj
from .state import write_atomic, xdg_dir


def installed_executable() -> Path:
    return Path(sys.executable).with_name("agent-quota")


def claude_setup(executable: Path) -> str:
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    try:
        settings = obj(
            json.loads((config / "settings.json").read_text(encoding="utf-8"))
        )
        status_line = obj(settings.get("statusLine")).copy()
    except (OSError, ValueError, RecursionError):
        status_line = {}

    command = [str(executable), "capture-claude"]
    wrapper = shlex.join(command)
    previous = status_line.get("command")
    if isinstance(previous, str) and previous:
        if previous == wrapper or previous.startswith(f"{wrapper} --then "):
            wrapper = previous
        else:
            wrapper = shlex.join([*command, "--then", previous])
    status_line.update(type="command", command=wrapper)
    return json.dumps({"statusLine": status_line}, indent=2)


def desktop_path(*, autostart: bool = False) -> Path:
    if autostart:
        directory = xdg_dir("XDG_CONFIG_HOME", ".config") / "autostart"
    else:
        directory = xdg_dir("XDG_DATA_HOME", ".local/share") / "applications"
    return directory / "agent-quota.desktop"


def _desktop_contents(executable: Path, *, auto: bool) -> str:
    # Desktop entries apply general string escapes before the Exec quoting rules.
    quoted = str(executable).replace("\\", "\\\\\\\\").replace("%", "%%")
    for character in ('"', "$", "\x60"):
        quoted = quoted.replace(character, "\\\\" + character)
    suffix = " --background" if auto else ""
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Agent Quota\n"
        "Comment=View Claude Code, Codex, and Antigravity quotas\n"
        f'Exec="{quoted}"{suffix}\n'
        "Icon=utilities-system-monitor\n"
        "Terminal=false\n"
        "Categories=Utility;\n"
    )


def install_launcher(executable: Path) -> Path:
    path = desktop_path()
    write_atomic(path, _desktop_contents(executable, auto=False), mode=0o644)
    return path


def autostart_enabled() -> bool:
    return desktop_path(autostart=True).is_file()


def set_autostart(enabled: bool, executable: Path) -> None:
    path = desktop_path(autostart=True)
    if enabled:
        write_atomic(path, _desktop_contents(executable, auto=True), mode=0o644)
    else:
        path.unlink(missing_ok=True)
