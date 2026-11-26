import fcntl
import json
import os
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from .quota import (
    FIVE_HOURS,
    WEEK,
    Provider,
    ResetCredit,
    Snapshot,
    Window,
    epoch,
    nonnegative_int,
    obj,
    parse_claude,
    percentage,
    rows,
)


def xdg_dir(variable: str, default: str) -> Path:
    candidate = Path(os.environ.get(variable, ""))
    return candidate if candidate.is_absolute() else Path.home() / default


def state_dir() -> Path:
    return xdg_dir("XDG_STATE_HOME", ".local/state") / "agent-quota"


def write_atomic(path: Path, contents: str, mode: int = 0o600) -> None:
    if not path.parent.exists():
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    with NamedTemporaryFile(
        dir=path.parent, mode="w", encoding="utf-8", delete=False
    ) as file:
        pending = Path(file.name)
        try:
            file.write(contents)
            file.flush()
            pending.chmod(mode)
            pending.replace(path)
        finally:
            pending.unlink(missing_ok=True)


def _read_object(path: Path) -> dict[str, Any]:
    try:
        return obj(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, RecursionError):
        return {}


def store_snapshot(snapshot: Snapshot) -> None:
    write_atomic(
        state_dir() / f"{snapshot.provider}.json", json.dumps(asdict(snapshot))
    )


def read_snapshot(provider: Provider) -> Snapshot | None:
    data = _read_object(state_dir() / f"{provider}.json")
    observed = data.get("observed_at")
    if data.get("provider") != provider or observed is None or epoch(observed) is None:
        return None

    windows = []
    for row in rows(data.get("windows")):
        minutes = nonnegative_int(row.get("minutes"))
        remaining = percentage(row.get("remaining"))
        group = row.get("group")
        if (
            minutes in (FIVE_HOURS, WEEK)
            and remaining is not None
            and isinstance(group, str)
        ):
            windows.append(
                Window(group, minutes, remaining, epoch(row.get("resets_at")))
            )

    count = nonnegative_int(data.get("reset_count")) if provider == "codex" else None
    credits = []
    if count:
        for row in rows(data.get("credits")):
            key = row.get("key")
            if isinstance(key, str) and len(key) == 64:
                credits.append(ResetCredit(key, epoch(row.get("expires_at"))))
    if not windows and count is None:
        return None
    return Snapshot(provider, float(observed), tuple(windows), count, tuple(credits))


def capture_claude(payload: bytes, now: float) -> bool:
    try:
        snapshot = parse_claude(obj(json.loads(payload)), now)
        if not snapshot.windows:
            return False
        directory = state_dir()
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        with (directory / "claude.lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            previous = read_snapshot("claude")
            if previous is not None and previous.observed_at > snapshot.observed_at:
                return False
            store_snapshot(snapshot)
    except (OSError, ValueError, RecursionError):
        return False
    return True


def notify_expiring(
    snapshot: Snapshot, now: int, notify: Callable[[ResetCredit], bool]
) -> int:
    if (
        snapshot.provider != "codex"
        or not snapshot.reset_count
        or snapshot.is_stale(now)
    ):
        return 0
    path = state_dir() / "notified.json"
    seen = {
        key: expiry
        for key, value in _read_object(path).items()
        if (expiry := epoch(value)) is not None and expiry > now
    }
    sent = 0
    for credit in snapshot.credits:
        expiry = credit.expires_at
        if expiry is None or not 0 < expiry - now <= 86_400 or credit.key in seen:
            continue
        if notify(credit):
            seen[credit.key] = expiry
            write_atomic(path, json.dumps(seen))
            sent += 1
    return sent
