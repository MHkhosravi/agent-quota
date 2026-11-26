import os
import signal
import sys
import textwrap
import time
from contextlib import suppress
from pathlib import Path
from threading import Event, Timer

import pytest

from agent_quota.providers import ProviderError, fetch_agy, fetch_codex


def _cli(tmp_path, name, source):
    path = tmp_path / name
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(source))
    path.chmod(0o700)
    return str(path)


def test_codex_handshake_uses_only_read_requests(tmp_path):
    codex = _cli(
        tmp_path,
        "codex",
        """
        import json
        import sys

        assert sys.argv[1:] == ["app-server", "--listen", "stdio://"]
        initialize = json.loads(input())
        assert initialize["method"] == "initialize"
        print(json.dumps({"method": "account/updated"}), flush=True)
        print(json.dumps({"id": initialize["id"], "result": {}}), flush=True)
        assert json.loads(input()) == {"method": "initialized"}
        read = json.loads(input())
        assert read["method"] == "account/rateLimits/read"
        print(json.dumps({"id": read["id"], "result": {"rateLimitResetCredits": None}}), flush=True)
        """,
    )
    assert fetch_codex(binary=codex) == {"rateLimitResetCredits": None}


def test_codex_timeout_reaps_the_child(tmp_path, monkeypatch):
    pid_file = tmp_path / "pid"
    monkeypatch.setenv("QUOTA_TEST_PID", str(pid_file))
    codex = _cli(
        tmp_path,
        "codex",
        """
        import os, signal, time
        from pathlib import Path

        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        Path(os.environ["QUOTA_TEST_PID"]).write_text(str(os.getpid()))
        input()
        time.sleep(30)
        """,
    )
    started = time.monotonic()
    with pytest.raises(ProviderError, match="timed out"):
        fetch_codex(binary=codex, timeout=0.3)
    assert time.monotonic() - started < 3
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_antigravity_builtin_does_not_request_a_model_turn(tmp_path):
    agy = _cli(
        tmp_path,
        "agy",
        """
        import sys
        assert sys.argv[1:] == ["-p", "/usage", "--output-format", "json"]
        print('{"status":"SUCCESS","num_turns":0,"response":"test usage"}')
        """,
    )
    assert fetch_agy(binary=agy)["response"] == "test usage"


def test_provider_auth_errors_are_sanitized(tmp_path):
    agy = _cli(
        tmp_path,
        "agy",
        """
        import sys
        print("Authentication required: private account details", file=sys.stderr)
        raise SystemExit(1)
        """,
    )
    with pytest.raises(ProviderError, match="authentication|Sign in") as exc:
        fetch_agy(binary=agy)
    assert "private account details" not in str(exc.value)


def test_antigravity_cancellation_finishes_before_the_timeout(tmp_path):
    agy = _cli(tmp_path, "agy", "import time\nwhile True: time.sleep(1)\n")
    cancel = Event()
    timer = Timer(0.2, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(ProviderError):
            fetch_agy(binary=agy, timeout=5, cancel=cancel)
    finally:
        timer.cancel()
    assert time.monotonic() - started < 2


def test_timed_out_provider_does_not_leave_subprocess_running(tmp_path, monkeypatch):
    pid_file = tmp_path / "descendant-pid"
    monkeypatch.setenv("QUOTA_TEST_PID", str(pid_file))
    agy = _cli(
        tmp_path,
        "agy",
        """
        import os, subprocess, time
        from pathlib import Path

        child = subprocess.Popen(["sleep", "30"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        Path(os.environ["QUOTA_TEST_PID"]).write_text(str(child.pid))
        time.sleep(30)
        """,
    )
    with pytest.raises(ProviderError, match="timed out"):
        fetch_agy(binary=agy, timeout=0.3)
    pid = int(pid_file.read_text())
    proc = Path(f"/proc/{pid}/stat")
    try:
        for _ in range(20):
            with suppress(FileNotFoundError):
                if not proc.read_text().split(") ", 1)[1].startswith("Z "):
                    time.sleep(0.02)
                    continue
            return
        pytest.fail("The timed-out provider left a live subprocess")
    finally:
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
