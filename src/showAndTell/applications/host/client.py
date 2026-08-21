"""HTTP client for the fixture host agent.

The rule this module enforces: when SHOWANDTELL_FIXTURE_HOST_URL is set and
the agent does not answer, fail loudly naming the URL — never fall back
to local Docker silently.  That silent fallback is the failure mode the
deleted showAndTell.viewer.vm_fixtures hard-coding existed to prevent.
"""
from __future__ import annotations

import atexit
import contextlib
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

URL_ENV = "SHOWANDTELL_FIXTURE_HOST_URL"
TOKEN_ENV = "SHOWANDTELL_FIXTURE_HOST_TOKEN"
LEASE_ENV = "SHOWANDTELL_FIXTURE_HOST_LEASE"
ASSIGNMENT_ENV = "SHOWANDTELL_FIXTURE_HOST_ASSIGNMENT"


class FixtureHostError(RuntimeError):
    """The agent could not be reached or refused the operation."""


class FixtureHostUnsupported(FixtureHostError):
    """The app has no meaning for this operation (the agent answered 501).

    Snapshot capability is a property of the driver, not of a table the caller
    keeps: an application that cannot freeze state says so here.
    """

    def __init__(self, app: str, operation: str):
        self.app, self.operation = app, operation
        super().__init__(f"{app} does not support {operation}")


class FixtureHostBusy(FixtureHostError):
    def __init__(self, held_by: str, since: float | None):
        message = f"fixture host busy: held by {held_by}"
        if since is not None:
            # `since` is the lease grant's wall-clock time (see
            # LeaseStore._granted_at); render it human-readable rather than
            # a raw unix float in an error a person actually reads.
            when = datetime.fromtimestamp(since).isoformat(timespec="seconds")
            message += f" since {when}"
        super().__init__(message)
        self.held_by = held_by
        self.since = since


class FixtureHostClient:
    def __init__(self, base_url: str, token: str,
                 *, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._assignment: dict[str, str] = {}
        self._http = httpx.Client(
            base_url=self.base_url, transport=transport, timeout=30.0,
            headers={"authorization": f"Bearer {token}"},
        )

    def _call(self, method: str, path: str, *, lease_id: str | None = None,
              timeout: float = 30.0, **kwargs) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        ambient = lease_id or os.environ.get(LEASE_ENV)
        if ambient:
            headers["x-showAndTell-lease"] = ambient
        try:
            response = self._http.request(
                method, path, headers=headers, timeout=timeout, **kwargs)
        except httpx.HTTPError as exc:
            raise FixtureHostError(
                f"fixture host at {self.base_url} did not answer: {exc}") from exc
        if response.status_code == 409:
            body = response.json()
            if body.get("code") == "lease_held":
                raise FixtureHostBusy(body.get("held_by", "?"), body.get("since"))
        if response.status_code == 501:
            body = response.json() if response.content else {}
            raise FixtureHostUnsupported(
                path.split("/")[3] if len(path.split("/")) > 3 else "?",
                str(body.get("operation") or method.lower()))
        if response.status_code >= 400:
            raise FixtureHostError(
                f"{method} {path} on {self.base_url} → "
                f"{response.status_code}: {response.text[:300]}")
        return response

    def _resolve(self, app: str) -> str:
        """The instance actually granted for a logical application name.

        The lease taken through this client wins; a subprocess that inherited
        a capture's lease reads the same mapping from the ambient environment.
        Unmapped names pass through, so single-instance agents see no change.
        """
        if app in self._assignment:
            return self._assignment[app]
        ambient = os.environ.get(ASSIGNMENT_ENV)
        if ambient:
            try:
                mapping = json.loads(ambient)
            except ValueError:
                return app
            if isinstance(mapping, dict):
                return str(mapping.get(app, app))
        return app

    @property
    def assignment(self) -> dict[str, str]:
        """Non-identity grants from this client's lease, for subprocess handoff."""
        return dict(self._assignment)

    # -- reads ------------------------------------------------------------
    def health(self) -> dict:
        return self._call("GET", "/v1/health").json()

    def apps(self) -> list[dict]:
        return self._call("GET", "/v1/apps").json()

    def status(self, app: str) -> dict:
        app = self._resolve(app)
        return self._call("GET", f"/v1/apps/{app}/status").json()

    def secrets(self, app: str) -> dict:
        app = self._resolve(app)
        return self._call("GET", f"/v1/apps/{app}/secrets").json()

    @property
    def host(self) -> str:
        """The agent's hostname, for services that are not addressed by URL."""
        return urlparse(self.base_url).hostname or "127.0.0.1"

    def _url_for_port(self, port: int) -> str:
        return f"http://{self.host}:{port}"

    def app_url(self, app: str) -> str:
        return self._url_for_port(self.status(app)["ports"]["app"])

    # -- lifecycle and golden state ----------------------------------------
    def start(self, app: str, *, wait: bool = True) -> None:
        app = self._resolve(app)
        self._call(
            "POST", f"/v1/apps/{app}/start",
            json={"wait": wait, "public_url": self.app_url(app)},
            timeout=1800.0,
        )

    def stop(self, app: str) -> None:
        app = self._resolve(app)
        self._call("POST", f"/v1/apps/{app}/stop", timeout=300.0)

    def reset(self, app: str, *, lease_id: str | None = None,
              timeout: float = 900.0) -> None:
        app = self._resolve(app)
        self._call("POST", f"/v1/apps/{app}/reset", lease_id=lease_id, timeout=timeout)

    def baseline(self, app: str, *, lease_id: str | None = None) -> None:
        app = self._resolve(app)
        self._call("POST", f"/v1/apps/{app}/baseline", lease_id=lease_id, timeout=900.0)

    def golden(self, app: str, *, lease_id: str | None = None) -> None:
        app = self._resolve(app)
        self._call("POST", f"/v1/apps/{app}/golden", lease_id=lease_id, timeout=900.0)

    # -- named snapshots ----------------------------------------------------
    def snapshot(self, app: str, name: str, *, lease_id: str | None = None) -> None:
        app = self._resolve(app)
        self._call("POST", f"/v1/apps/{app}/snapshots/{name}",
                   lease_id=lease_id, timeout=900.0)

    def pull_snapshot(self, app: str, name: str, dest: Path) -> Path:
        app = self._resolve(app)
        response = self._call(
            "GET", f"/v1/apps/{app}/snapshots/{name}", timeout=600.0)
        want = response.headers.get("x-showAndTell-sha256")
        got = hashlib.sha256(response.content).hexdigest()
        if want and want != got:
            raise FixtureHostError(f"snapshot '{name}' was corrupted in transit")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(response.content)
        return dest

    def push_snapshot(self, app: str, name: str, source: Path,
                      *, lease_id: str | None = None) -> None:
        app = self._resolve(app)
        data = source.read_bytes()
        self._call("PUT", f"/v1/apps/{app}/snapshots/{name}", lease_id=lease_id,
                   content=data, timeout=600.0,
                   headers={"x-showAndTell-sha256": hashlib.sha256(data).hexdigest()})

    def restore_snapshot(self, app: str, name: str,
                         *, lease_id: str | None = None) -> None:
        app = self._resolve(app)
        self._call("POST", f"/v1/apps/{app}/snapshots/{name}/restore",
                   lease_id=lease_id, timeout=900.0)

    # -- leases --------------------------------------------------------------
    def acquire_lease(self, apps: Sequence[str], holder: str,
                      ttl_s: float = 900.0) -> str:
        response = self._call("POST", "/v1/leases", json={
            "apps": list(apps), "holder": holder, "ttl_s": ttl_s})
        body = response.json()
        # An older agent sends no assignment; identity entries add nothing.
        self._assignment.update({
            str(requested): str(granted)
            for requested, granted in (body.get("apps") or {}).items()
            if requested != granted})
        return body["lease_id"]

    def list_leases(self) -> list[dict]:
        """Live execution ownership known to this fixture host."""
        return self._call("GET", "/v1/leases").json()["leases"]

    def annotate_lease(self, lease_id: str, execution: Mapping[str, object]) -> dict:
        """Publish task display metadata for one owned lease."""
        return self._call(
            "PUT", f"/v1/leases/{lease_id}/execution",
            lease_id=lease_id, json=dict(execution)).json()

    def heartbeat(self, lease_id: str) -> None:
        self._call("POST", f"/v1/leases/{lease_id}/heartbeat")

    def release(self, lease_id: str) -> None:
        self._call("DELETE", f"/v1/leases/{lease_id}")
        self._assignment.clear()

    def force_release(self, apps: Sequence[str]) -> list[dict]:
        """Break whatever leases block ``apps``; returns the broken holders."""
        response = self._call("POST", "/v1/leases/force-release",
                              json={"apps": list(apps)})
        return response.json()["released"]

    @contextlib.contextmanager
    def lease_scope(self, apps: Sequence[str], holder: str):
        """Reuse the ambient capture lease when present, else hold one briefly."""
        ambient = os.environ.get(LEASE_ENV)
        if ambient:
            yield ambient
            return
        lease_id = self.acquire_lease(apps, holder)
        try:
            yield lease_id
        finally:
            self.release(lease_id)


class LeaseKeeper:
    """Background heartbeat so a long capture outlives the lease TTL."""

    def __init__(self, client: FixtureHostClient, lease_id: str,
                 interval_s: float = 300.0):
        self._client = client
        self._lease_id = lease_id
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                self._client.heartbeat(self._lease_id)
            except FixtureHostError:
                return  # agent gone or lease released; nothing to keep alive

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


def configured_client() -> FixtureHostClient | None:
    """Client for an explicitly configured agent; None when unset."""
    url = os.environ.get(URL_ENV)
    if not url:
        return None
    return FixtureHostClient(url, os.environ.get(TOKEN_ENV, ""))


_local_lock = threading.Lock()
_local_client: FixtureHostClient | None = None
_local_process: "subprocess.Popen | None" = None


def auto_spawned_client() -> FixtureHostClient | None:
    """Return the live agent this process spawned, if it still owns the URL.

    ``local_agent_client`` publishes the loopback agent through ``URL_ENV`` so
    downstream adapters use the same lifecycle plane.  That publication must
    not make the next caller mistake our private agent for an operator-
    configured/shared one.  A different URL still wins, so deliberately
    switching this process to a remote agent keeps working.
    """
    client = _local_client
    process = _local_process
    if client is None or process is None or process.poll() is not None:
        return None
    configured_url = os.environ.get(URL_ENV)
    if configured_url and configured_url.rstrip("/") != client.base_url:
        return None
    return client


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _cleanup_local_process() -> None:
    """Stop the auto-spawned agent so it never outlives this process.

    Registered once, at spawn time. Best-effort: terminate, give it a short
    grace period, then kill — an orphaned agent otherwise keeps holding
    Docker containers (and the loopback port) after the viewer exits.
    """
    process = _local_process
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5.0)


def local_agent_client() -> FixtureHostClient:
    """The configured agent, or a process-wide auto-spawned local one.

    Publishing URL_ENV/TOKEN_ENV means everything downstream in this
    process (fixture remote_reset, adapters) resolves the same agent.
    """
    global _local_client, _local_process
    configured = configured_client()
    if configured is not None:
        return configured
    with _local_lock:
        if _local_client is not None and _local_process is not None \
                and _local_process.poll() is None:
            return _local_client
        from showAndTell.applications.host import server as agent_server

        port = _free_port()
        token = agent_server.ensure_token(agent_server.DEFAULT_TOKEN_PATH)
        stderr_log = tempfile.NamedTemporaryFile(
            prefix="showAndTell-fixture-host-", suffix=".log", delete=False)
        stderr_path = Path(stderr_log.name)
        try:
            _local_process = subprocess.Popen(
                [sys.executable, "-m", "showAndTell.cli", "fixture-host",
                 "--host", "127.0.0.1", "--port", str(port)],
                stdout=subprocess.DEVNULL, stderr=stderr_log,
            )
        finally:
            stderr_log.close()
        atexit.register(_cleanup_local_process)
        client = FixtureHostClient(f"http://127.0.0.1:{port}", token)
        deadline = time.monotonic() + 30.0
        while True:
            try:
                client.health()
                break
            except FixtureHostError:
                if time.monotonic() >= deadline:
                    _local_process.terminate()
                    tail = ""
                    with contextlib.suppress(OSError):
                        tail = stderr_path.read_bytes()[-500:].decode(
                            "utf-8", errors="replace")
                    detail = f"\n--- agent stderr (tail) ---\n{tail}" if tail else ""
                    raise FixtureHostError(
                        "auto-spawned local fixture host did not become "
                        f"healthy{detail}")
                time.sleep(0.3)
        os.environ[URL_ENV] = f"http://127.0.0.1:{port}"
        os.environ[TOKEN_ENV] = token
        _local_client = client
        return client
