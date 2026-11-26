from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from math import isfinite
from typing import Any, Literal

Provider = Literal["claude", "codex", "agy"]
FIVE_HOURS = 300
WEEK = 10_080
STALE_SECONDS = 600


@dataclass(frozen=True, slots=True)
class Window:
    group: str
    minutes: int
    remaining: float
    resets_at: int | None


@dataclass(frozen=True, slots=True)
class ResetCredit:
    key: str
    expires_at: int | None


@dataclass(frozen=True, slots=True)
class Snapshot:
    provider: Provider
    observed_at: float
    windows: tuple[Window, ...]
    reset_count: int | None = None
    credits: tuple[ResetCredit, ...] = ()

    def is_stale(self, now: int, window: Window | None = None) -> bool:
        if now - self.observed_at >= STALE_SECONDS or self.observed_at > now + 60:
            return True
        return (
            window is not None
            and window.resets_at is not None
            and now >= window.resets_at
        )


def obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def rows(value: Any) -> list[dict[str, Any]]:
    return (
        [row for row in value if isinstance(row, dict)]
        if isinstance(value, list)
        else []
    )


def percentage(value: Any) -> float | None:
    if type(value) in (float, int) and 0 <= value <= 100 and isfinite(value):
        return float(value)
    return None


def epoch(value: Any) -> int | None:
    # Keep values representable in a local-time datetime and reject booleans.
    if type(value) in (int, float) and 0 < value < 253_402_214_400:
        return int(value)
    return None


def _iso_epoch(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        date = datetime.fromisoformat(value)
        return epoch(date.timestamp()) if date.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def nonnegative_int(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _codex_windows(bucket: dict[str, Any]) -> tuple[Window, ...]:
    windows = {}
    for key in ("primary", "secondary"):
        data = obj(bucket.get(key))
        minutes = nonnegative_int(data.get("windowDurationMins"))
        used = percentage(data.get("usedPercent"))
        if minutes in (FIVE_HOURS, WEEK) and used is not None:
            windows[minutes] = Window(
                "", minutes, 100 - used, epoch(data.get("resetsAt"))
            )
    return tuple(windows[minutes] for minutes in sorted(windows))


def parse_codex(result: dict[str, Any], now: int) -> Snapshot:
    limits = obj(result.get("rateLimitsByLimitId"))
    windows = _codex_windows(obj(limits.get("codex"))) or _codex_windows(
        obj(result.get("rateLimits"))
    )
    offer = obj(result.get("rateLimitResetCredits"))
    count = nonnegative_int(offer.get("availableCount"))
    credits = {}
    if count:
        for detail in rows(offer.get("credits")):
            credit_id = detail.get("id")
            if (
                detail.get("status") != "available"
                or not isinstance(credit_id, str)
                or not credit_id
            ):
                continue
            key = sha256(credit_id.encode()).hexdigest()
            credits[key] = ResetCredit(key, epoch(detail.get("expiresAt")))
    return Snapshot("codex", now, windows, count, tuple(credits.values()))


def parse_claude(payload: dict[str, Any], now: float) -> Snapshot:
    rates = obj(payload.get("rate_limits"))
    windows = []
    for key, minutes in (("five_hour", FIVE_HOURS), ("seven_day", WEEK)):
        data = obj(rates.get(key))
        used = percentage(data.get("used_percentage"))
        if used is not None:
            windows.append(
                Window("", minutes, 100 - used, epoch(data.get("resets_at")))
            )
    return Snapshot("claude", now, tuple(windows))


def _agy_structured(data: dict[str, Any]) -> list[Window]:
    windows = []
    for group in rows(data.get("groups")):
        name = group.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        for bucket in rows(group.get("buckets")):
            window_name = bucket.get("window")
            minutes = {"5h": FIVE_HOURS, "weekly": WEEK}.get(
                window_name if isinstance(window_name, str) else ""
            )
            ratio = bucket.get("remaining_fraction")
            if (
                minutes is None
                or not isinstance(ratio, (float, int))
                or isinstance(ratio, bool)
                or not 0 <= ratio <= 1
            ):
                continue
            windows.append(
                Window(
                    name.strip(),
                    minutes,
                    round(ratio * 100, 6),
                    _iso_epoch(bucket.get("reset_time")),
                )
            )
    return windows


def _agy_text(report: Any) -> list[Window]:
    windows = []
    if not isinstance(report, str):
        return windows
    for line in report.splitlines():
        fields = line.split("\t")
        if len(fields) != 4:
            continue
        group, label, amount, reset = (field.strip() for field in fields)
        minutes = {
            "five hour limit remaining": FIVE_HOURS,
            "weekly limit remaining": WEEK,
        }.get(label.casefold())
        if not group or minutes is None or not amount.endswith("%"):
            continue
        try:
            remaining = percentage(float(amount[:-1]))
        except ValueError:
            continue
        if remaining is not None:
            windows.append(Window(group, minutes, remaining, _iso_epoch(reset)))
    return windows


def parse_agy(payload: dict[str, Any], now: int) -> Snapshot:
    command = obj(payload.get("command"))
    windows = _agy_structured(obj(command.get("data"))) or _agy_text(
        payload.get("response")
    )
    groups: dict[str, dict[int, Window]] = {}
    for window in windows:
        groups.setdefault(window.group, {})[window.minutes] = window
    ordered = tuple(
        group[minutes] for group in groups.values() for minutes in sorted(group)
    )
    return Snapshot("agy", now, ordered)


def countdown(resets_at: int | None, now: int) -> str:
    if resets_at is None:
        return "Refill time unavailable"
    seconds = resets_at - now
    if seconds <= 0:
        return "Refill due"
    if seconds < 60:
        return "Refills in <1m"
    hours, minutes = divmod(seconds // 60, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"Refills in {days}d {hours}h"
    if hours:
        return f"Refills in {hours}h {minutes}m"
    return f"Refills in {minutes}m"


def local_expiry(expires_at: int, now: int) -> str:
    date = datetime.fromtimestamp(expires_at)
    if date.year == datetime.fromtimestamp(now).year:
        return date.strftime("%b %-d at %-I:%M %p")
    return date.strftime("%b %-d, %Y at %-I:%M %p")


def reset_badge(snapshot: Snapshot, now: int, failed: bool = False) -> str | None:
    count = snapshot.reset_count
    if not count:
        return None
    noun = "reset" if count == 1 else "resets"
    expiries = [
        credit.expires_at
        for credit in snapshot.credits
        if credit.expires_at is not None
    ]
    if expiries and min(expiries) <= now:
        return "Reset expiry reached · refresh to confirm"
    if failed or snapshot.is_stale(now):
        return f"Last reported · {count} {noun}"
    label = f"{count} {noun} available"
    if expiries:
        return f"{label} · expires {local_expiry(min(expiries), now)}"
    return label
