"""Host-owned execution registry and local completion cleanup."""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
from collections.abc import Callable
from typing import Any

from showAndTell.execution import ExecutionManager
from showAndTell.applications.host import client as host_client

from . import _util


class ExecutionError(_util.ViewerError):
    pass


def child_environment(execution=None) -> dict[str, str]:
    """Environment for a runner subprocess launched under our fixture lease.

    Any inherited lease/assignment markers are stripped first; the execution's
    own lease is then re-injected so the child joins it instead of acquiring a
    fresh one. ``None`` means the child runs without a parent lease.
    """
    from showAndTell.applications.host.client import ASSIGNMENT_ENV, LEASE_ENV

    env = {
        key: value for key, value in os.environ.items()
        if key not in {
            LEASE_ENV, ASSIGNMENT_ENV, "SHOWANDTELL_EXECUTION_MANAGER_CHILD",
        }
    }
    env["PYTHONUNBUFFERED"] = "1"
    if execution is not None:
        env[LEASE_ENV] = execution.session.lease_id
        env["SHOWANDTELL_EXECUTION_MANAGER_CHILD"] = "1"
        assignment = dict(getattr(
            execution.session.client, "assignment", None) or {})
        if assignment:
            env[ASSIGNMENT_ENV] = json.dumps(assignment)
    return env


def _available_host_client():
    """Return the already selected fixture host without starting a new one."""
    return host_client.auto_spawned_client() or host_client.configured_client()


class HostExecutionRegistry:
    """Read and release executions where their fixture leases actually live."""

    def __init__(self, client_factory: Callable[[], Any] = _available_host_client):
        self._client_factory = client_factory

    def _client(self):
        client = self._client_factory()
        if client is None:
            raise ExecutionError("no fixture host is configured", 503)
        return getattr(client, "client", client)

    @staticmethod
    def _public(lease: dict[str, Any]) -> dict[str, Any]:
        details = lease.get("execution") or {}
        assignment = lease.get("apps") or {}
        granted_at = lease.get("granted_at")
        if isinstance(granted_at, (int, float)):
            started_at = dt.datetime.fromtimestamp(
                granted_at, tz=dt.timezone.utc).isoformat(timespec="seconds")
        else:
            started_at = None
        return {
            "id": lease["lease_id"],
            "task": details.get("task") or lease.get("holder") or "unknown",
            "kind": details.get("kind") or "lease",
            "product": details.get("product") or "Fixture lease",
            "state": "running",
            "applications": [
                {"name": name, "instance": instance}
                for name, instance in assignment.items()
            ],
            "started_at": started_at,
        }

    def list_running(self) -> list[dict[str, Any]]:
        client = self._client_factory()
        if client is None:
            return []
        client = getattr(client, "client", client)
        rows = [self._public(lease)
                for lease in ExecutionManager(client).list_active()]
        return sorted(rows, key=lambda row: row["started_at"] or "")

    def force_release(self, lease_id: str) -> dict[str, Any]:
        client = self._client()
        manager = ExecutionManager(client)
        leases = {row["lease_id"]: row for row in manager.list_active()}
        try:
            lease = leases[lease_id]
        except KeyError as exc:
            raise ExecutionError("execution lease was not found", 404) from exc
        manager.force_release(lease_id)
        result = self._public(lease)
        result["state"] = "released"
        return result


class ExecutionSupervisor:
    """Clean local resources when a locally launched execution completes.

    This object deliberately has no terminate operation. Administrative
    release belongs to ``HostExecutionRegistry`` and only revokes the fixture
    lease; it never signals the capture or runner process.
    """

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None

    def _ensure_monitor(self) -> None:
        with self._lock:
            if self._monitor is not None:
                return
            self._monitor = threading.Thread(
                target=self._monitor_processes,
                name="showAndTell-execution-cleanup",
                daemon=True,
            )
            self._monitor.start()

    def _monitor_processes(self) -> None:
        while not self._stop.wait(1.0):
            self.reap_completed()

    def reap_completed(self) -> None:
        """Release ownership only after a runner has exited on its own."""
        with self._lock:
            completed = [
                execution_id for execution_id, item in self._items.items()
                if not item["cleaned"] and item.get("process") is not None
                and item["process"].poll() is not None
            ]
        for execution_id in completed:
            self.complete(execution_id)

    def register(
        self,
        execution_id: str,
        *,
        process=None,
        cleanup: Callable[[], None] | None = None,
    ) -> None:
        with self._lock:
            self._items[execution_id] = {
                "process": process,
                "cleanup": cleanup,
                "cleaned": False,
            }
        if process is not None:
            self._ensure_monitor()

    def complete(self, execution_id: str) -> None:
        with self._lock:
            item = self._items.get(execution_id)
            if item is None or item["cleaned"]:
                return
            item["cleaned"] = True
            cleanup = item.get("cleanup")
        if callable(cleanup):
            cleanup()

    def close(self) -> None:
        self._stop.set()
        monitor = self._monitor
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=2.0)
        with self._lock:
            ids = list(self._items)
        for execution_id in ids:
            try:
                self.complete(execution_id)
            except Exception:
                pass
