import asyncio
import json
import os
import shutil
import signal
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from threading import Event
from typing import Any

from .quota import obj

AUTH_HINTS = (
    "authentication required",
    "unauthenticated",
    "unauthorized",
    "not authenticated",
    "not logged in",
    "sign in",
    "login required",
    "expired credentials",
    "missing credentials",
)


class ProviderError(Exception):
    """An error suitable for the popup, without raw CLI output."""


def _is_auth_error(value: Any) -> bool:
    message = str(value).casefold()
    return any(hint in message for hint in AUTH_HINTS)


def _resolve_binary(binary: str | None, name: str, label: str) -> str:
    if binary is not None:
        resolved = shutil.which(os.path.expanduser(binary))
    else:
        resolved = shutil.which(name) or shutil.which(
            name, path=str(Path.home() / ".local/bin")
        )
    if not resolved:
        raise ProviderError(f"{label} CLI was not found")
    return str(Path(resolved).absolute())


async def _drain(stream: asyncio.StreamReader | None) -> None:
    if stream is not None:
        while await stream.read(65_536):
            pass


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.stdin is not None:
        process.stdin.close()
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    # A paused pipe can prevent asyncio's wait() from completing after exit.
    drained = asyncio.gather(
        _drain(process.stdout), _drain(process.stderr), process.wait()
    )
    try:
        await asyncio.wait_for(asyncio.shield(drained), timeout=0.4)
    except TimeoutError:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await asyncio.wait_for(drained, timeout=1)
    finally:
        # A descendant can ignore TERM while keeping no relation to our pipes.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)


async def _request(
    argv: list[str],
    label: str,
    exchange: Callable[[asyncio.subprocess.Process], Awaitable[dict[str, Any]]],
    timeout: float,
    cancel: Event | None,
    *,
    rpc: bool = False,
) -> dict[str, Any]:
    process: asyncio.subprocess.Process | None = None
    work: asyncio.Future[dict[str, Any]] | None = None
    try:
        if cancel is not None and cancel.is_set():
            raise ProviderError(f"{label} check cancelled")
        async with asyncio.timeout(timeout):
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=Path.home(),
                stdin=asyncio.subprocess.PIPE if rpc else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL if rpc else asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=1024 * 1024,
            )
            work = asyncio.ensure_future(exchange(process))
            if cancel is not None:
                while not work.done():
                    if cancel.is_set():
                        raise ProviderError(f"{label} check cancelled")
                    await asyncio.wait({work}, timeout=0.1)
            return await work
    except TimeoutError:
        raise ProviderError(f"{label} quota check timed out") from None
    except (OSError, ValueError, RecursionError):
        raise ProviderError(f"{label} quota check failed") from None
    finally:
        if work is not None and not work.done():
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
        if process is not None:
            with suppress(TimeoutError, OSError):
                await _stop(process)


async def _send(stdin: asyncio.StreamWriter, message: dict[str, Any]) -> None:
    stdin.write((json.dumps(message) + "\n").encode())
    await stdin.drain()


async def _read_rpc_result(
    stdout: asyncio.StreamReader, expected_id: int
) -> dict[str, Any]:
    while line := await stdout.readline():
        if not line.strip():
            continue
        try:
            event = obj(json.loads(line))
        except (UnicodeError, ValueError):
            raise ProviderError("Codex returned malformed quota data") from None
        if type(event.get("id")) is not int or event["id"] != expected_id:
            continue
        if (error := event.get("error")) is not None:
            if obj(error).get("code") in (401, 403, -32001) or _is_auth_error(error):
                raise ProviderError("Codex authentication is required")
            raise ProviderError("Codex rejected the quota request")
        if isinstance(result := event.get("result"), dict):
            return result
        raise ProviderError("Codex returned an invalid quota response")
    raise ProviderError("Codex closed the connection before sending quota")


async def _codex_exchange(process: asyncio.subprocess.Process) -> dict[str, Any]:
    if process.stdin is None or process.stdout is None:
        raise ProviderError("Could not open the Codex connection")
    init = {
        "id": 1,
        "method": "initialize",
        "params": {
            "clientInfo": {
                "name": "agent_quota",
                "title": "Agent Quota",
                "version": "0.1.0",
            }
        },
    }
    await _send(process.stdin, init)
    await _read_rpc_result(process.stdout, 1)
    await _send(process.stdin, {"method": "initialized"})
    await _send(process.stdin, {"id": 2, "method": "account/rateLimits/read"})
    return await _read_rpc_result(process.stdout, 2)


async def _agy_exchange(process: asyncio.subprocess.Process) -> dict[str, Any]:
    output, stderr = await process.communicate()
    try:
        payload = obj(json.loads(output))
    except (UnicodeError, ValueError):
        payload = {}
    if (
        process.returncode
        or not payload
        or payload.get("status") not in ("SUCCESS", None)
    ):
        diagnostics = stderr.decode(errors="replace")
        if _is_auth_error(diagnostics) or _is_auth_error(payload.get("error")):
            raise ProviderError("Antigravity authentication is required")
        raise ProviderError("Antigravity could not report quota")
    if "num_turns" in payload and (
        type(payload["num_turns"]) is not int or payload["num_turns"] != 0
    ):
        raise ProviderError("Antigravity returned an unexpected usage response")
    return payload


def fetch_codex(
    *, binary: str | None = None, timeout: float = 10.0, cancel: Event | None = None
) -> dict[str, Any]:
    executable = _resolve_binary(binary, "codex", "Codex")
    return asyncio.run(
        _request(
            [executable, "app-server", "--listen", "stdio://"],
            "Codex",
            _codex_exchange,
            timeout,
            cancel,
            rpc=True,
        )
    )


def fetch_agy(
    *, binary: str | None = None, timeout: float = 12.0, cancel: Event | None = None
) -> dict[str, Any]:
    executable = _resolve_binary(binary, "agy", "Antigravity")
    return asyncio.run(
        _request(
            [executable, "-p", "/usage", "--output-format", "json"],
            "Antigravity",
            _agy_exchange,
            timeout,
            cancel,
        )
    )
