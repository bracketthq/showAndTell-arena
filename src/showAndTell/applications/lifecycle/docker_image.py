"""Reusable lifecycle plane for applications shipped as one Docker image.

This is application infrastructure, not a task fixture.  A concrete
``applications/<name>/driver.py`` supplies the image, container port and any
product-specific configuration while the host agent supplies the lease.
"""
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


class DockerImageDriver:
    image = ""
    container_port = 0
    health_path = "/"
    run_command: tuple[str, ...] = ()
    boot_timeout = 900.0
    poll_interval = 3.0
    recreate_on_reset = True
    # Some archived WebArena fixtures are distributed as side-loaded images,
    # not pullable registry references. Marking them explicitly prevents
    # ``docker run`` from spending minutes trying a registry that can never
    # satisfy the request after a clean Docker prune.
    local_only_image = False

    def __init__(self, manifest, *, root: Path | None = None,
                 runner: CommandRunner | None = None,
                 probe: Callable[[str], bool] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.manifest = manifest
        self.name = manifest.name
        self.root = Path(root or Path.home() / ".showAndTell" / "fixture-host")
        self.runner = runner or CommandRunner()
        self.container = os.environ.get(
            f"SHOWANDTELL_{self.name.upper()}_CONTAINER", f"showAndTell-{self.name}")
        self.port = int(os.environ.get(
            f"SHOWANDTELL_{self.name.upper()}_PORT", manifest.app_port.host))
        self.bind_host = bind_host(self.name)
        self.public_url = validate_public_url(os.environ.get(
            f"SHOWANDTELL_{self.name.upper()}_PUBLIC_URL",
            f"http://127.0.0.1:{self.port}",
        ))
        self._probe = probe or self._http_probe
        self._sleep = sleep

    def set_public_url(self, value: str) -> None:
        self.public_url = validate_public_url(value)

    def _docker(self, *args: str, check: bool = True,
                timeout: float = 900.0):
        return self.runner.run(["docker", *args], check=check, timeout=timeout)

    def _assume_running(self) -> bool:
        return os.environ.get(
            f"SHOWANDTELL_{self.name.upper()}_ASSUME_RUNNING", "").lower() in {
                "1", "true", "yes", "on"}

    def _exists(self) -> bool:
        result = self._docker(
            "ps", "-a", "--filter", f"name=^{self.container}$",
            "--format", "{{.Names}}", check=False)
        return self.container in result.stdout.decode().split()

    def _running(self) -> bool:
        result = self._docker(
            "ps", "--filter", f"name=^{self.container}$",
            "--format", "{{.Names}}", check=False)
        return self.container in result.stdout.decode().split()

    def _image_available(self) -> bool:
        result = self._docker(
            "image", "inspect", self.image, check=False, timeout=30.0)
        return result.returncode == 0

    def _run_flags(self) -> tuple[str, ...]:
        return ()

    def _http_probe(self, url: str) -> bool:
        try:
            return httpx.get(url, timeout=5.0, follow_redirects=False).status_code < 500
        except httpx.HTTPError:
            return False

    def _health_url(self) -> str:
        return f"http://127.0.0.1:{self.port}{self.health_path}"

    def _wait_healthy(self) -> None:
        deadline = time.monotonic() + self.boot_timeout
        while not self._probe(self._health_url()):
            if time.monotonic() >= deadline:
                raise DriverError(
                    f"{self.name} did not become healthy at {self._health_url()}")
            self._sleep(self.poll_interval)

    def _configure(self) -> bool:
        """Product-specific post-boot configuration; true means recheck health."""
        return False

    def start(self, *, wait: bool = True) -> None:
        if not self._assume_running():
            if self._exists():
                self._docker("start", self.container)
            else:
                if not self.image or not self.container_port:
                    raise DriverError(f"{self.name} driver has no image/port")
                if self.local_only_image and not self._image_available():
                    raise DriverError(
                        f"{self.name} requires the side-loaded WebArena Docker "
                        f"image {self.image!r}, but it is not installed. Import "
                        "the matching WebArena release image, then retry.")
                self._docker(
                    "run", "--name", self.container, "-d", *self._run_flags(),
                    "-p", f"{self.bind_host}:{self.port}:{self.container_port}",
                    self.image, *self.run_command, timeout=1800.0)
        if wait:
            self._wait_healthy()
        if not self._assume_running() and self._configure() and wait:
            self._wait_healthy()

    def stop(self) -> None:
        if not self._assume_running() and self._exists():
            self._docker("stop", self.container, check=False, timeout=300.0)

    def status(self) -> dict:
        return {
            "state": "healthy" if self._probe(self._health_url()) else "unreachable",
            "ports": {"app": self.port},
        }

    def reset(self) -> None:
        if self._assume_running():
            if not self.manifest.external:
                raise DriverError(
                    f"{self.name} reset cannot mutate an assumed-running instance")
            return
        if self.recreate_on_reset and self._exists():
            self._docker("stop", self.container, check=False, timeout=300.0)
            self._docker("rm", self.container, check=False, timeout=300.0)
            self.start(wait=True)
        elif self._exists():
            self._docker("restart", self.container, timeout=300.0)
            self._wait_healthy()
        else:
            self.start(wait=True)

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
