"""ONLYOFFICE driver: Python port of scripts/vm-fixtures/remote/onlyoffice.sh.

Two services, two kinds of state: Document Server holds only cached
conversions; the connector holds the authoritative workbooks.  Reset
clears both.  There is no task state worth snapshotting, so the whole
snapshot family is Unsupported.
"""
from __future__ import annotations

import os
import secrets
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from showAndTell.applications.lifecycle.public_url import validate_public_url
from showAndTell.applications.host.protocol import (
    CommandRunner, DriverError, Unsupported, bind_host)

CACHE = "/var/lib/onlyoffice/documentserver/App_Data/cache/files"
DOCBUILDER = "/var/lib/onlyoffice/documentserver/App_Data/docbuilder"
PACKAGE_PARENT = Path(__file__).resolve().parents[3]


def _default_admin_call(method: str, url: str, token: str) -> None:
    try:
        response = httpx.request(
            method, url, headers={"X-ShowAndTell-Connector-Token": token},
            timeout=30.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise DriverError(f"connector admin call failed: {exc}") from exc


class OnlyOfficeDriver:
    name = "onlyoffice"

    def __init__(self, manifest=None, *, root: Path | None = None,
                 runner: CommandRunner | None = None,
                 compose_file: Path | None = None,
                 project: str | None = None,
                 port: int | None = None,
                 connector_port: int | None = None,
                 probe: Callable[[str], bool] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 admin_call: Callable[[str, str, str], None] | None = None):
        self.manifest = manifest
        self.name = manifest.name if manifest is not None else "onlyoffice"
        self.root = Path(root or Path.home() / ".showAndTell" / "fixture-host")
        self.runner = runner or CommandRunner()
        self.compose_file = compose_file or manifest.compose_file
        self.project = project or os.environ.get(
            "SHOWANDTELL_ONLYOFFICE_PROJECT", "showandtell-onlyoffice")
        # Explicit kwargs > environment > manifest (see ErpnextDriver).
        self.port = int(port if port is not None else os.environ.get(
            "SHOWANDTELL_ONLYOFFICE_PORT", manifest.ports["app"].host))
        self.connector_port = int(
            connector_port if connector_port is not None else os.environ.get(
                "SHOWANDTELL_CONNECTOR_PORT", manifest.ports["connector"].host))
        self.bind_host = bind_host(manifest.name)
        self.jwt_secret = os.environ.get(
            "SHOWANDTELL_ONLYOFFICE_JWT_SECRET", "showAndTell-onlyoffice-local-only")
        self.document_server_url = os.environ.get(
            "SHOWANDTELL_CONNECTOR_DOCUMENT_SERVER_URL",
            f"http://127.0.0.1:{self.port}")
        self.token_file = self.root / "connector.token"
        self._probe = probe or self._http_probe
        self._sleep = sleep
        self._admin_call = admin_call or _default_admin_call

    def set_public_url(self, value: str) -> None:
        self.document_server_url = validate_public_url(value)

    def connector_token(self) -> str:
        if not self.token_file.is_file() or not self.token_file.read_text().strip():
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            self.token_file.touch(mode=0o600, exist_ok=True)
            self.token_file.chmod(0o600)
            self.token_file.write_text(secrets.token_hex(32))
        return self.token_file.read_text().strip()

    def _compose_env(self) -> dict[str, str]:
        return {
            "SHOWANDTELL_ONLYOFFICE_BIND_HOST": self.bind_host,
            "SHOWANDTELL_ONLYOFFICE_PORT": str(self.port),
            "SHOWANDTELL_ONLYOFFICE_JWT_SECRET": self.jwt_secret,
            "SHOWANDTELL_CONNECTOR_BIND_HOST": self.bind_host,
            "SHOWANDTELL_CONNECTOR_PORT": str(self.connector_port),
            "SHOWANDTELL_CONNECTOR_ADMIN_TOKEN": self.connector_token(),
            "SHOWANDTELL_CONNECTOR_DOCUMENT_SERVER_URL": self.document_server_url,
            # The connector image copies the installed ``showAndTell`` package.
            # Its parent is ``src`` in a checkout and ``site-packages`` from a
            # wheel, so this remains self-contained in either environment.
            "SHOWANDTELL_CONNECTOR_BUILD_CONTEXT": str(PACKAGE_PARENT),
            "SHOWANDTELL_CONNECTOR_BUILD_DOCKERFILE":
                "showAndTell/applications/onlyoffice/connector.Dockerfile",
        }

    def _dc(self, *args: str, timeout: float = 900.0, check: bool = True):
        argv = ["docker", "compose", "-p", self.project,
                "-f", str(self.compose_file), "--profile", "connector", *args]
        return self.runner.run(argv, env=self._compose_env(), timeout=timeout, check=check)

    def _http_probe(self, url: str) -> bool:
        try:
            response = httpx.get(url, timeout=5.0)
            # For /healthcheck, require exact "true" body; for others, status < 400
            if url.endswith("/healthcheck"):
                return response.status_code < 400 and response.text.strip() == "true"
            else:
                return response.status_code < 400
        except httpx.HTTPError:
            return False

    def start(self, *, wait: bool = True) -> None:
        # Rebuilding an already healthy Document Server + connector is both
        # unnecessary and, on some Docker Desktop releases, can leave
        # ``compose up --build`` waiting forever after the containers are up.
        # The fixture reset below clears all task-owned state, so a healthy
        # running pair is ready to reuse as-is.
        already_healthy = (
            self._probe(f"http://127.0.0.1:{self.port}/healthcheck")
            and self._probe(f"http://127.0.0.1:{self.connector_port}/health")
        )
        if not already_healthy:
            self._dc("up", "-d", "--build", "--quiet-pull", timeout=1800.0)
        if wait:
            self._wait_healthy()

    def _wait_healthy(self, budget_s: float = 600.0) -> None:
        # Wait for docserver first (up to 600s)
        deadline = time.monotonic() + budget_s
        while not self._probe(f"http://127.0.0.1:{self.port}/healthcheck"):
            if time.monotonic() >= deadline:
                diagnostics = self._get_diagnostics()
                raise DriverError(
                    f"Document Server did not become healthy\n{diagnostics}")
            self._sleep(3.0)

        # Then wait for connector (fresh 120s budget)
        deadline = time.monotonic() + 120.0
        while not self._probe(f"http://127.0.0.1:{self.connector_port}/health"):
            if time.monotonic() >= deadline:
                diagnostics = self._get_diagnostics()
                raise DriverError(
                    f"Connector did not become healthy\n{diagnostics}")
            self._sleep(3.0)

    def _get_diagnostics(self) -> str:
        """Get docker compose ps output for diagnostics."""
        try:
            result = self._dc("ps", timeout=60, check=False)
            # Use stdout; if empty, use stderr
            output = result.stdout.decode("utf-8", errors="replace").strip()
            if not output:
                output = result.stderr.decode("utf-8", errors="replace").strip()
            # Truncate to ~500 chars
            if len(output) > 500:
                output = output[:500] + "..."
            return f"Docker Compose State:\n{output}"
        except Exception as e:
            return f"Failed to get diagnostics: {e}"

    def stop(self) -> None:
        self._dc("stop", timeout=300.0)

    def status(self) -> dict:
        healthy = (self._probe(f"http://127.0.0.1:{self.port}/healthcheck")
                   and self._probe(f"http://127.0.0.1:{self.connector_port}/health"))
        return {"state": "healthy" if healthy else "unreachable",
                "ports": {"app": self.port, "connector": self.connector_port}}

    def reset(self) -> None:
        # Workbooks are the real task state; the cache would otherwise serve
        # a previous capture's contents for a reused document id.
        self._admin_call(
            "DELETE", f"http://127.0.0.1:{self.connector_port}/admin/workbooks",
            self.connector_token())
        self._dc("exec", "-T", "onlyoffice", "sh", "-c",
                 f"rm -rf {CACHE}/* 2>/dev/null || true")
        self._dc("exec", "-T", "onlyoffice", "sh", "-c",
                 f"rm -rf {DOCBUILDER}/* 2>/dev/null || true")

    # No task state lives in Document Server; nothing to snapshot or bless.
    def baseline(self) -> None:
        raise Unsupported("onlyoffice has no baseline stage")

    def golden(self) -> None:
        raise Unsupported("onlyoffice has no golden state")

    def snapshot(self, name: str) -> None:
        raise Unsupported("onlyoffice has no task state to snapshot")

    def fetch_snapshot(self, name: str) -> Path:
        raise Unsupported("onlyoffice has no snapshots")

    def load_snapshot(self, name: str, tar_path: Path) -> None:
        raise Unsupported("onlyoffice has no snapshots")

    def restore(self, name: str) -> None:
        raise Unsupported("onlyoffice has no snapshots")

    def secrets(self) -> dict:
        # The port is published alongside the token because the data plane
        # runs off-host and cannot see an override this driver applied. Reading
        # the manifest default instead would send it to the wrong connector.
        return {"connector_token": self.connector_token(),
                "connector_port": self.connector_port}


Driver = OnlyOfficeDriver
