"""Reusable lifecycle plane for one application-owned Compose project."""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from showAndTell.applications.host.protocol import (
    CommandRunner,
    DriverError,
    Unsupported,
    bind_host,
)
from showAndTell.applications.lifecycle.public_url import validate_public_url


class ComposeDriver:
    boot_timeout = 900.0
    poll_interval = 3.0

    def __init__(self, manifest, *, root: Path | None = None,
                 runner: CommandRunner | None = None,
                 probe: Callable[[str], bool] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.manifest = manifest
        self.name = manifest.name
        self.root = Path(root or Path.home() / ".showAndTell" / "fixture-host")
        self.runner = runner or CommandRunner()
        self.project = os.environ.get(
            f"SHOWANDTELL_{self.name.upper()}_PROJECT", f"showandtell-{self.name}")
        self.port = int(os.environ.get(
            f"SHOWANDTELL_{self.name.upper()}_PORT", manifest.app_port.host))
        self.bind_host = bind_host(self.name)
        self.public_url = validate_public_url(os.environ.get(
            f"SHOWANDTELL_{self.name.upper()}_PUBLIC_URL",
            f"http://127.0.0.1:{self.port}",
        ))
        self._probe = probe or self._http_probe
        self._sleep = sleep

    def _compose_env(self) -> dict[str, str]:
        return {
            **self.manifest.env,
            f"SHOWANDTELL_{self.name.upper()}_PORT": str(self.port),
            f"SHOWANDTELL_{self.name.upper()}_BIND_HOST": self.bind_host,
            f"SHOWANDTELL_{self.name.upper()}_PUBLIC_URL": self.public_url,
        }

    def set_public_url(self, value: str) -> None:
        self.public_url = validate_public_url(value)

    def _dc(self, *args: str, stdin_bytes: bytes | None = None,
            timeout: float = 900.0, check: bool = True):
        return self.runner.run(
            ["docker", "compose", "-p", self.project,
             "-f", str(self.manifest.compose_file), *args],
            stdin_bytes=stdin_bytes, env=self._compose_env(),
            timeout=timeout, check=check)

    def _http_probe(self, url: str) -> bool:
        try:
            return httpx.get(url, timeout=5.0, follow_redirects=False).status_code < 500
        except httpx.HTTPError:
            return False

    def _assume_running(self) -> bool:
        """Whether this application is already up and not ours to manage.

        Not every compose application can be brought up on the machine driving
        the replay — some are only deployed on the shared fixture host, and one
        whose seeders will not re-run against an existing database cannot be
        started locally at all. Pointing at the deployed instance is then the
        difference between verifying a task and not verifying it.
        """
        return os.environ.get(
            f"SHOWANDTELL_{self.name.upper()}_ASSUME_RUNNING", "").lower() in {
                "1", "true", "yes", "on"}

    def _health_url(self) -> str:
        # Assuming an instance is running is the one case where it need not be
        # on this machine, so its health is asked of wherever it was published.
        base = (self.public_url.rstrip("/") if self._assume_running()
                else f"http://127.0.0.1:{self.port}")
        return f"{base}{self.manifest.health_http or '/'}"

    def _wait_healthy(self) -> None:
        deadline = time.monotonic() + self.boot_timeout
        while not self._probe(self._health_url()):
            if time.monotonic() >= deadline:
                raise DriverError(
                    f"{self.name} did not become healthy at {self._health_url()}")
            self._sleep(self.poll_interval)

    def start(self, *, wait: bool = True) -> None:
        if not self._assume_running():
            self._dc("up", "-d", "--quiet-pull", timeout=1800.0)
        if wait:
            self._wait_healthy()

    def stop(self) -> None:
        if self._assume_running():
            return
        self._dc("stop", timeout=300.0, check=False)

    def status(self) -> dict:
        return {
            "state": "healthy" if self._probe(self._health_url()) else "unreachable",
            "ports": {"app": self.port},
        }

    def reset(self) -> None:
        # An assumed-running instance is shared, so restarting its processes is
        # not this run's to do. Its data plane still resets through the
        # supported APIs, which is where a task's own records are removed.
        if self._assume_running():
            return
        # Application data planes remove task-owned records through supported
        # APIs. The lifecycle plane only restarts processes; deleting volumes
        # here would also delete one-time product bootstrap such as a Zulip
        # realm or WordPress installation.
        self._dc("restart", timeout=600.0)
        self._wait_healthy()

    def baseline(self) -> None:
        raise Unsupported(f"{self.name} has no baseline stage")

    def golden(self) -> None:
        raise Unsupported(f"{self.name} has no golden state")

    def snapshot(self, name: str) -> None:
        raise Unsupported(f"{self.name} has no snapshots")

    def fetch_snapshot(self, name: str) -> Path:
        raise Unsupported(f"{self.name} has no snapshots")

    def load_snapshot(self, name: str, tar_path: Path) -> None:
        raise Unsupported(f"{self.name} has no snapshots")

    def restore(self, name: str) -> None:
        raise Unsupported(f"{self.name} has no snapshots")

    def secrets(self) -> dict:
        return {}
