import fcntl
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from hashlib import sha256
from threading import Event

from PySide6.QtCore import QObject, QRect, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from .desktop import autostart_enabled, installed_executable, set_autostart
from .providers import ProviderError, fetch_agy, fetch_codex
from .quota import Provider, ResetCredit, Snapshot, local_expiry, parse_agy, parse_codex
from .state import notify_expiring, read_snapshot, state_dir, store_snapshot
from .window import UsageWindow, tray_icon

REFRESH_MILLISECONDS = 300_000
POPUP_REFRESH_SECONDS = 60


def _codex_snapshot(*, cancel: Event) -> Snapshot:
    result = fetch_codex(cancel=cancel)
    now = int(time.time())
    snapshot = parse_codex(result, now)
    if not cancel.is_set():
        with suppress(OSError):
            notify_expiring(
                snapshot,
                now,
                lambda credit: not cancel.is_set() and _send_expiry_alert(credit),
            )
    return snapshot


def _agy_snapshot(*, cancel: Event) -> Snapshot:
    payload = fetch_agy(cancel=cancel)
    return parse_agy(payload, int(time.time()))


def _send_expiry_alert(credit: ResetCredit) -> bool:
    executable = shutil.which("notify-send")
    if credit.expires_at is None or not executable:
        return False
    when = local_expiry(credit.expires_at, int(time.time()))
    try:
        result = subprocess.run(
            [
                executable,
                "--app-name=Agent Quota",
                "--icon=utilities-system-monitor",
                "Codex reset expires soon",
                f"One of your available resets expires {when}.",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _crossed_refill(snapshot: Snapshot | None, previous: float, now: float) -> bool:
    if snapshot is None:
        return False
    timestamps = [window.resets_at for window in snapshot.windows]
    timestamps.extend(credit.expires_at for credit in snapshot.credits)
    return any(stamp is not None and previous < stamp <= now for stamp in timestamps)


class Monitor(QObject):
    def __init__(
        self,
        window: UsageWindow,
        *,
        fetchers: dict[Provider, Callable[..., Snapshot]] | None = None,
    ):
        super().__init__()
        self.window = window
        self.fetchers = (
            fetchers
            if fetchers is not None
            else {
                "codex": _codex_snapshot,
                "agy": _agy_snapshot,
            }
        )
        self.snapshots = {
            provider: read_snapshot(provider) for provider in window.cards
        }
        self.errors: dict[Provider, str] = {}
        self.attempted: dict[Provider, float] = {}
        self.pending: dict[Provider, Future[Snapshot]] = {}
        self.cancel = Event()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent-quota")
        self.claude_file = state_dir() / "claude.json"
        self.claude_mtime: int | None = None
        window.refresh_requested.connect(self.refresh)

        self.live_timer = QTimer(self)
        self.live_timer.timeout.connect(self.tick)
        self.live_timer.start(500)
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.refresh)
        self.poll_timer.start(REFRESH_MILLISECONDS)

    def refresh(self, *, only_old: bool = False) -> None:
        scheduled = time.time()
        for provider, fetch in self.fetchers.items():
            previous = self.attempted.get(provider)
            if provider in self.pending or (
                only_old
                and previous is not None
                and 0 <= scheduled - previous < POPUP_REFRESH_SECONDS
                and not _crossed_refill(self.snapshots[provider], previous, scheduled)
            ):
                continue
            self.attempted[provider] = scheduled
            self.pending[provider] = self.pool.submit(fetch, cancel=self.cancel)
        self.tick()

    def _read_claude(self) -> None:
        try:
            mtime = self.claude_file.stat().st_mtime_ns
        except OSError:
            return
        if mtime != self.claude_mtime:
            self.claude_mtime = mtime
            if snapshot := read_snapshot("claude"):
                self.snapshots["claude"] = snapshot

    def _collect(self) -> None:
        for provider, future in tuple(self.pending.items()):
            if not future.done():
                continue
            del self.pending[provider]
            try:
                snapshot = future.result()
                if not snapshot.windows and snapshot.reset_count is None:
                    raise ProviderError("Quota unavailable")
            except ProviderError as exc:
                self.errors[provider] = str(exc)
                continue
            except (OSError, ValueError, RecursionError):
                self.errors[provider] = "Quota refresh failed"
                continue
            self.snapshots[provider] = snapshot
            self.errors.pop(provider, None)
            try:
                store_snapshot(snapshot)
            except OSError:
                self.errors[provider] = "Could not save quota locally"

    def render(self) -> None:
        now = int(time.time())
        for provider, card in self.window.cards.items():
            card.display(self.snapshots[provider], self.errors.get(provider, ""), now)

    def tick(self) -> None:
        self._read_claude()
        self._collect()
        if self.window.isVisible():
            self.render()

    def shutdown(self) -> None:
        self.live_timer.stop()
        self.poll_timer.stop()
        self.cancel.set()
        self.pool.shutdown(wait=True, cancel_futures=True)


def _activate_existing(name: str) -> int:
    for _ in range(10):
        socket = QLocalSocket()
        socket.connectToServer(name)
        if socket.waitForConnected(100):
            socket.disconnectFromServer()
            return 0
        time.sleep(0.06)
    return 1


def _run_instance(app: QApplication, name: str, *, background: bool) -> int:
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    QLocalServer.removeServer(name)
    if not server.listen(name):
        return 1

    has_tray = QSystemTrayIcon.isSystemTrayAvailable()
    app.setQuitOnLastWindowClosed(not has_tray)
    window = UsageWindow(popup=has_tray)
    monitor = Monitor(window)
    icon = tray_icon()
    window.setWindowIcon(icon)
    tray = QSystemTrayIcon(icon)
    tray.setToolTip("Agent Quota")

    def reveal() -> None:
        monitor.refresh(only_old=True)
        monitor.render()
        window.reveal(tray.geometry() if has_tray else QRect())

    def new_connections() -> None:
        while server.hasPendingConnections():
            if socket := server.nextPendingConnection():
                socket.disconnectFromServer()
                socket.deleteLater()
        reveal()

    server.newConnection.connect(new_connections)
    menu = QMenu()
    menu.addAction("Open Agent Quota", reveal)
    menu.addAction("Refresh", monitor.refresh)
    startup = menu.addAction("Start at login")
    startup.setCheckable(True)
    startup.setChecked(autostart_enabled())

    def toggle_startup(enabled: bool) -> None:
        try:
            set_autostart(enabled, installed_executable())
        except OSError:
            startup.setChecked(autostart_enabled())
            QMessageBox.warning(
                window, "Agent Quota", "Could not update the login setting."
            )

    startup.triggered.connect(toggle_startup)
    menu.addSeparator()
    menu.addAction("Quit", app.quit)
    tray.setContextMenu(menu)

    def tray_activated(reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            reveal()

    tray.activated.connect(tray_activated)
    tray.show()
    QTimer.singleShot(0, monitor.refresh if background and has_tray else reveal)
    try:
        return app.exec()
    finally:
        tray.hide()
        server.close()
        monitor.shutdown()


def run(*, background: bool = False) -> int:
    app = QApplication([])
    app.setApplicationName("Agent Quota")
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = sha256(os.fsencode(directory.resolve())).hexdigest()[:16]
    name = f"agent-quota-{os.getuid()}-{digest}"
    with (directory / "instance.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0 if background else _activate_existing(name)
        return _run_instance(app, name, background=background)
