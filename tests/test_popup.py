import os
import subprocess
import sys
import textwrap


def test_popup_keeps_working_while_another_provider_is_blocked(tmp_path):
    env = os.environ.copy()
    env.pop("LD_LIBRARY_PATH", None)
    env.update(QT_QPA_PLATFORM="offscreen", XDG_STATE_HOME=str(tmp_path))
    script = textwrap.dedent("""
        import time
        from threading import Event
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QLabel
        from agent_quota.app import Monitor
        from agent_quota.providers import ProviderError
        from agent_quota.quota import FIVE_HOURS, WEEK, Snapshot, Window
        from agent_quota.window import UsageWindow

        stopped = Event()

        def slow(*, cancel):
            cancel.wait(10)
            time.sleep(0.05)
            stopped.set()
            raise ProviderError("Synthetic network timeout")

        def fast(*, cancel):
            now = int(time.time())
            return Snapshot("agy", now, (
                Window("Gemini Models", FIVE_HOURS, 38.5, now + 600),
                Window("Gemini Models", WEEK, 90, now + 5_400),
                Window("Claude and GPT models", FIVE_HOURS, 10, now + 900),
            ))

        app = QApplication([])
        window = UsageWindow()
        monitor = Monitor(window, fetchers={"codex": slow, "agy": fast})
        window.show()
        monitor.refresh()
        monitor.refresh()  # Skip requests already in flight.
        labels = []

        def check():
            monitor.tick()
            labels[:] = [label.text() for label in window.findChildren(QLabel)]
            if "10% left" in labels:
                app.quit()

        timer = QTimer()
        timer.timeout.connect(check)
        timer.start(20)
        QTimer.singleShot(2200, app.quit)
        app.exec()
        monitor.shutdown()
        assert stopped.is_set(), "The cancelled worker must exit before releasing the instance"
        assert "10% left" in labels, labels
        assert "Waiting for first update" in labels, labels
    """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_open_refreshes_after_refill_and_failed_notifications_can_retry(tmp_path):
    env = os.environ.copy()
    env.pop("LD_LIBRARY_PATH", None)
    env.update(QT_QPA_PLATFORM="offscreen", XDG_STATE_HOME=str(tmp_path))
    notify = tmp_path / "notify-send"
    notify.write_text("#!/bin/sh\nexit 1\n")
    notify.chmod(0o700)
    env["PATH"] = os.pathsep.join((str(tmp_path), env["PATH"]))
    script = textwrap.dedent("""
        import time
        from PySide6.QtWidgets import QApplication
        from agent_quota.app import Monitor, _send_expiry_alert
        from agent_quota.quota import FIVE_HOURS, ResetCredit, Snapshot, Window
        from agent_quota.window import UsageWindow

        def wait_for_shutdown(*, cancel):
            cancel.wait(5)
            return Snapshot("agy", int(time.time()), ())

        app = QApplication([])
        window = UsageWindow()
        monitor = Monitor(window, fetchers={"agy": wait_for_shutdown})
        now = int(time.time())
        monitor.snapshots["agy"] = Snapshot("agy", now - 10, (
            Window("Gemini Models", FIVE_HOURS, 1, now - 2),
        ))
        monitor.attempted["agy"] = time.time() - 10
        try:
            monitor.refresh(only_old=True)
            assert "agy" in monitor.pending, "A refill since the last poll should refresh"
            assert not _send_expiry_alert(ResetCredit("a" * 64, now + 800))
        finally:
            monitor.shutdown()
    """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
