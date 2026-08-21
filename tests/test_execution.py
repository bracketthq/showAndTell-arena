"""Common application execution ownership."""
from __future__ import annotations

import pytest

from showAndTell.execution import ExecutionManager


class Client:
    def __init__(self):
        self.cleared = []
        self.annotations = []

    def force_release(self, applications):
        self.cleared.append(list(applications))
        return []

    def annotate_lease(self, lease_id, metadata):
        self.annotations.append((lease_id, metadata))


class Session:
    lease_id = "lease-test04"

    def __init__(self, applications, **kwargs):
        self.applications = applications
        self.kwargs = kwargs
        self.started = False
        self.closed = False

    def start(self, ensure_started=None):
        self.started = True

    def close(self):
        self.closed = True


def test_manager_owns_apps_as_one_lease():
    client = Client()
    manager = ExecutionManager(client, session_factory=Session)

    lease = manager.acquire(
        ["erpnext", "onlyoffice"], holder="task-test04", clear_stale=True)

    assert lease.session.started is True
    assert client.cleared == [["erpnext", "onlyoffice"]]
    lease.annotate(task="test04", kind="run", product="Codex")
    assert client.annotations == [("lease-test04", {
        "task": "test04", "kind": "run", "product": "Codex",
    })]
    lease.close()
    assert lease.session.closed is True


def test_manager_propagates_session_construction_failure():
    def fail(*_args, **_kwargs):
        raise RuntimeError("bad application")

    manager = ExecutionManager(Client(), session_factory=fail)
    with pytest.raises(RuntimeError, match="bad application"):
        manager.acquire(["erpnext"], holder="task-test04")
