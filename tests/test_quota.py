import json
from datetime import UTC, datetime

from agent_quota.quota import (
    FIVE_HOURS,
    WEEK,
    countdown,
    parse_agy,
    parse_claude,
    parse_codex,
    reset_badge,
)
from agent_quota.state import capture_claude, notify_expiring, read_snapshot, state_dir

NOW = 1_800_000_000


def test_codex_uses_reported_duration_and_keeps_reset_count():
    snapshot = parse_codex(
        {
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {
                        "windowDurationMins": WEEK,
                        "usedPercent": 100,
                        "resetsAt": NOW + 100,
                    },
                    "secondary": {
                        "windowDurationMins": FIVE_HOURS,
                        "usedPercent": 24.5,
                    },
                }
            },
            "rateLimitResetCredits": {"availableCount": 2, "credits": None},
        },
        NOW,
    )
    assert [(window.minutes, window.remaining) for window in snapshot.windows] == [
        (FIVE_HOURS, 75.5),
        (WEEK, 0),
    ]
    assert snapshot.windows[0].resets_at is None
    assert reset_badge(snapshot, NOW) == "2 resets available"


def test_codex_absent_fields_and_invalid_values_do_not_become_empty_quotas():
    snapshot = parse_codex(
        {
            "rateLimits": {
                "primary": {"windowDurationMins": FIVE_HOURS, "usedPercent": True},
                "secondary": {"windowDurationMins": WEEK, "usedPercent": float("nan")},
            },
            "rateLimitResetCredits": None,
        },
        NOW,
    )
    assert not snapshot.windows
    assert snapshot.reset_count is None
    assert reset_badge(snapshot, NOW) is None


def test_agy_prefers_structured_buckets_and_keeps_model_groups():
    data = {
        "command": {
            "name": "usage",
            "data": {
                "groups": [
                    {
                        "name": "Gemini Models",
                        "buckets": [
                            {
                                "window": "weekly",
                                "remaining_fraction": 0.617,
                                "reset_time": "2027-01-16T08:00:00Z",
                            },
                            {"window": "5h", "remaining_fraction": 0},
                            {"window": "unsupported", "remaining_fraction": 1},
                        ],
                    },
                    {
                        "name": "Claude and GPT models",
                        "buckets": [{"window": "5h", "remaining_fraction": 1}],
                    },
                ],
            },
        },
        "response": "Misleading\tWeekly Limit Remaining\t1%\t2027-01-16T08:00:00Z",
    }
    snapshot = parse_agy(data, NOW)
    assert [
        (item.group, item.minutes, item.remaining) for item in snapshot.windows
    ] == [
        ("Gemini Models", FIVE_HOURS, 0),
        ("Gemini Models", WEEK, 61.7),
        ("Claude and GPT models", FIVE_HOURS, 100),
    ]
    assert snapshot.windows[1].resets_at == int(
        datetime(2027, 1, 16, 8, tzinfo=UTC).timestamp()
    )


def test_agy_text_fallback_skips_malformed_rows():
    snapshot = parse_agy(
        {
            "response": "Gemini Models\tWeekly Limit Remaining\tbad%\t-\n"
            "Gemini Models\tFive Hour Limit Remaining\t20%\t2027-01-16T08:00:00Z\n"
            "Unrecognized content",
        },
        NOW,
    )
    (window,) = snapshot.windows
    assert (window.group, window.minutes, window.remaining) == (
        "Gemini Models",
        FIVE_HOURS,
        20,
    )


def test_claude_helper_persists_only_quota_and_keeps_last_good_data(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    payload = {
        "rate_limits": {
            "five_hour": {"used_percentage": 17.25, "resets_at": NOW + 100},
            "seven_day": {"used_percentage": 60, "resets_at": NOW + 800},
            "extra_usage": {"sensitive": "drop me"},
        },
        "cwd": "private-workspace",
    }
    assert capture_claude(json.dumps(payload).encode(), NOW)
    assert not capture_claude(b'{"rate_limits":{}}', NOW + 10)
    assert not capture_claude(b"invalid", NOW + 20)
    assert read_snapshot("claude") == parse_claude(payload, NOW)
    cache = state_dir() / "claude.json"
    assert b"private-workspace" not in cache.read_bytes()
    assert b"drop me" not in cache.read_bytes()
    assert cache.stat().st_mode & 0o777 == 0o600


def test_window_is_stale_after_ten_minutes_or_at_refill():
    snapshot = parse_claude(
        {
            "rate_limits": {
                "five_hour": {"used_percentage": 77, "resets_at": NOW + 1200}
            }
        },
        NOW,
    )
    (window,) = snapshot.windows
    assert not snapshot.is_stale(NOW + 599, window)
    assert snapshot.is_stale(NOW + 600, window)
    assert snapshot.is_stale(NOW + 1200, window)
    assert countdown(NOW + 1200, NOW + 1199) == "Refills in <1m"
    assert countdown(NOW + 1200, NOW + 1200) == "Refill due"
    assert countdown(None, NOW) == "Refill time unavailable"


def codex_credit(expires_at, *, status="available"):
    fields = {
        "availableCount": 1,
        "credits": [{"id": "raw-credit-id", "status": status, "expiresAt": expires_at}],
    }
    return parse_codex({"rateLimitResetCredits": fields}, NOW)


def test_expiry_alert_threshold_and_restart_deduplication(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    sent = []

    def notify(credit):
        sent.append(credit)
        return True

    assert notify_expiring(codex_credit(NOW + 86_401), NOW, notify) == 0
    assert notify_expiring(codex_credit(NOW + 86_400), NOW, notify) == 1
    assert notify_expiring(codex_credit(NOW + 86_400), NOW + 1, notify) == 0
    assert len(sent) == 1
    assert b"raw-credit-id" not in (state_dir() / "notified.json").read_bytes()


def test_expired_unknown_and_unavailable_resets_do_not_send_alerts(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    for snapshot in (
        codex_credit(NOW),
        codex_credit(None),
        codex_credit(NOW + 2, status="used"),
    ):
        assert notify_expiring(snapshot, NOW, lambda _: True) == 0
    assert (
        reset_badge(codex_credit(NOW), NOW)
        == "Reset expiry reached · refresh to confirm"
    )


def test_failed_notification_can_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    snapshot = codex_credit(NOW + 50)
    assert notify_expiring(snapshot, NOW, lambda _: False) == 0
    assert notify_expiring(snapshot, NOW + 1, lambda _: True) == 1


def test_older_claude_capture_cannot_replace_newer_session(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    first = b'{"rate_limits":{"five_hour":{"used_percentage":10}}}'
    second = b'{"rate_limits":{"five_hour":{"used_percentage":35}}}'
    assert capture_claude(second, NOW + 0.5)
    assert not capture_claude(first, NOW + 0.1)
    snapshot = read_snapshot("claude")
    assert snapshot is not None
    assert snapshot.windows[0].remaining == 65
    assert snapshot.observed_at == NOW + 0.5
