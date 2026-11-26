# Agent Quota

Agent Quota is a local Linux tray app for Claude Code, Codex, and Google Antigravity. The popup shows five-hour and weekly quota remaining, refill times, and available Codex resets. Ubuntu GNOME is the primary desktop target.

## Development

- Use Python 3.12 and `uv sync --locked` from this directory. Use `uv add` for dependency changes and include the updated `uv.lock`.
- Use `uv run agent-quota` to open the popup. Run `uv run agent-quota install-launcher` from this checkout to register it in the desktop app menu.
- Keep provider calls off the Qt event loop. Keep the last valid reading on refresh errors and label stale, missing, and unsupported data accurately.

## Provider interfaces

- Codex uses the local App Server's `account/rateLimits/read` request. Use the reported reset count and expiry details for badges and notifications.
- Antigravity uses its built-in `agy -p /usage --output-format json` command. Prefer structured quota buckets and keep the text report fallback.
- Claude Code sends quota fields through the opt-in `capture-claude` status-line helper. Preserve an existing status-line command when offering setup. Claude's redeemable resets are available through its Usage page.
- Use existing CLI sign-ins. Keep normalized snapshots and alert history in the user's XDG state directory. Use synthetic provider payloads in committed tests and logs.

## Verification

```sh
uv run --locked pytest
uv run --locked ruff check
uv run --locked ruff format --check
uv run --locked ty check
```

For changes to the popup or launcher, check tray activation and reopening the existing instance on Ubuntu. Confirm the cards fit at 100% and 150% scaling. For provider changes, compare the normalized quota fields with the provider's native output.
