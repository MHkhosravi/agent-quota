import argparse
import os
import subprocess
import sys
import time

from .desktop import install_launcher, installed_executable
from .state import capture_claude


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        description="View your AI agent quotas in the Linux tray."
    )
    parser.add_argument("--background", action="store_true", help="Start in the tray")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "install-launcher", help="Add Agent Quota to your desktop app menu"
    )
    capture = subparsers.add_parser(
        "capture-claude", help="Receive Claude Code status-line JSON"
    )
    capture.add_argument(
        "--then", help="Keep running your original Claude status-line command"
    )
    parsed = parser.parse_args(args)

    if parsed.command == "capture-claude":
        payload = sys.stdin.buffer.read()
        capture_claude(payload, time.time())
        if parsed.then:
            original = subprocess.run(
                ["/bin/sh", "-c", parsed.then], input=payload, check=False
            )
            return original.returncode
        return 0
    if parsed.command == "install-launcher":
        print(install_launcher(installed_executable()))
        return 0

    # Research environments may add system Qt to the dynamic linker's search path.
    # Start this GUI with its bundled Qt and leave the parent shell untouched.
    if os.environ.get("LD_LIBRARY_PATH"):
        clean_env = os.environ.copy()
        clean_env.pop("LD_LIBRARY_PATH")
        os.execve(
            sys.executable, [sys.executable, "-m", "agent_quota", *args], clean_env
        )

    from .app import run

    return run(background=parsed.background)


if __name__ == "__main__":
    raise SystemExit(main())
