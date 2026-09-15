"""Task-centric, fixture-host-owned execution dashboard tests."""
from __future__ import annotations

from tests._viewer_fixture import load_serve


class Client:
    def __init__(self):
        self.released = []
        self.leases = [{
            "lease_id": "1" * 16,
            "holder": "task-test04-holder",
            "apps": {
                "erpnext": "erpnext-2",
                "onlyoffice": "onlyoffice-2",
                "roundcube": "roundcube-2",
            },
            "execution": {
                "task": "test04", "kind": "replay",
                "product": "Brackett",
            },
            "granted_at": 1785748800.0,
        }]

    def list_leases(self):
        return list(self.leases)

    def release(self, lease_id):
        self.released.append(lease_id)
        self.leases = [row for row in self.leases if row["lease_id"] != lease_id]


def test_dashboard_maps_host_assignment_to_the_execution():
    serve = load_serve()
    registry = serve.executions.HostExecutionRegistry(lambda: Client())

    assert registry.list_running() == [{
        "id": "1" * 16,
        "task": "test04",
        "kind": "replay",
        "product": "Brackett",
        "state": "running",
        "applications": [
            {"name": "erpnext", "instance": "erpnext-2"},
            {"name": "onlyoffice", "instance": "onlyoffice-2"},
            {"name": "roundcube", "instance": "roundcube-2"},
        ],
        "started_at": "2026-08-03T09:20:00+00:00",
    }]


def test_force_release_revokes_exact_lease_without_touching_processes():
    serve = load_serve()
    client = Client()
    registry = serve.executions.HostExecutionRegistry(lambda: client)

    result = registry.force_release("1" * 16)

    assert result["state"] == "released"
    assert client.released == ["1" * 16]
    assert registry.list_running() == []


def test_local_supervisor_only_runs_cleanup_and_has_no_terminate_operation():
    serve = load_serve()
    events = []
    supervisor = serve.executions.ExecutionSupervisor()
    supervisor.register("2" * 32, cleanup=lambda: events.append("cleanup"))

    assert not hasattr(supervisor, "force_terminate")
    supervisor.complete("2" * 32)
    supervisor.complete("2" * 32)
    assert events == ["cleanup"]


def test_local_supervisor_reaps_only_after_natural_process_exit():
    serve = load_serve()
    events = []

    class Process:
        returncode = None

        def poll(self):
            return self.returncode

    process = Process()
    supervisor = serve.executions.ExecutionSupervisor()
    supervisor.register(
        "3" * 32, process=process, cleanup=lambda: events.append("cleanup"))
    supervisor.reap_completed()
    assert events == []
    process.returncode = 0
    supervisor.reap_completed()
    assert events == ["cleanup"]
    supervisor.close()
