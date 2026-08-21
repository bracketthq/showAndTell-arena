"""Driver contract between the agent's HTTP surface and each app's operations."""
from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable


class DriverError(RuntimeError):
    """An operation failed; the message is safe to surface to the client."""


class Unsupported(DriverError):
    """The app does not implement this operation (HTTP 501)."""


class CommandRunner:
    """Thin subprocess wrapper so drivers are testable with a fake."""

    def run(
        self,
        argv: Sequence[str],
        *,
        stdin_bytes: bytes | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float = 900.0,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        merged = {**os.environ, **env} if env else None
        try:
            result = subprocess.run(
                list(argv), input=stdin_bytes, env=merged,
                capture_output=True, timeout=timeout,
                stdin=subprocess.DEVNULL if stdin_bytes is None else None,
            )
        except subprocess.TimeoutExpired as exc:
            raise DriverError(
                f"`{' '.join(list(argv)[:6])}` timed out after {timeout}s"
            ) from exc
        except FileNotFoundError as exc:
            command = str(argv[0]) if argv else "command"
            if command == "docker":
                raise DriverError(
                    "Docker is required to run this application but `docker` "
                    "was not found on PATH. Install and start Docker Desktop "
                    "(macOS/Windows) or Docker Engine with Compose (Linux), "
                    "then retry."
                ) from exc
            raise DriverError(
                f"required command `{command}` was not found on PATH"
            ) from exc
        if check and result.returncode:
            detail = (result.stderr or result.stdout or b"no output")[:1000]
            decoded = detail.decode(errors="replace")
            if argv and argv[0] == "docker" and (
                    "cannot connect to the docker daemon" in decoded.lower()
                    or "is the docker daemon running" in decoded.lower()):
                raise DriverError(
                    "Docker is installed but its daemon is not running. Start "
                    "Docker Desktop (or Docker Engine), wait until it is ready, "
                    "then retry."
                )
            raise DriverError(
                f"`{' '.join(list(argv)[:6])}` failed "
                f"(exit {result.returncode}): {decoded}"
            )
        return result


@runtime_checkable
class AppDriver(Protocol):
    """One app's operations, implemented next to its containers.

    Drivers raise Unsupported for operations the app has no meaning for
    (the route answers 501), and DriverError for real failures.
    """

    name: str

    def start(self, *, wait: bool = True) -> None: ...
    def stop(self) -> None: ...
    def status(self) -> dict: ...
    def reset(self) -> None: ...
    def baseline(self) -> None: ...
    def golden(self) -> None: ...
    def snapshot(self, name: str) -> None: ...
    def fetch_snapshot(self, name: str) -> Path: ...
    def load_snapshot(self, name: str, tar_path: Path) -> None: ...
    def restore(self, name: str) -> None: ...
    def secrets(self) -> dict: ...


def bind_host(application: str) -> str:
    """Which address this application's published ports should bind to.

    Loopback by default. A host that serves other machines — the shared VM —
    sets SHOWANDTELL_BIND_HOST once rather than one variable per application,
    because a per-application list silently omits every application added
    after it was written. The specific form still wins where it is set.
    """
    import os

    return (os.environ.get(f"SHOWANDTELL_{application.upper()}_BIND_HOST")
            or os.environ.get("SHOWANDTELL_BIND_HOST")
            or "127.0.0.1")
