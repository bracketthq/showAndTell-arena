"""Client contract-tested against the real agent app over ASGI."""
import hashlib
from datetime import datetime

import pytest

from showAndTell.applications.host import client as host_client
from showAndTell.applications.host.client import (
    FixtureHostBusy, FixtureHostClient, FixtureHostError,
)
from showAndTell.applications.host.api import build_agent_api
from showAndTell.applications.host.leases import LeaseStore

from tests.conftest import asgi_sync_transport
from tests.test_application_host_api import FakeDriver

TOKEN = "contract-token"


@pytest.fixture()
def harness(tmp_path):
    drivers = {"erpnext": FakeDriver("erpnext", tmp_path)}
    api = build_agent_api(drivers, TOKEN, leases=LeaseStore())
    transport = asgi_sync_transport(api)
    client = FixtureHostClient("http://agent.test:8090", TOKEN, transport=transport)
    return client, drivers


def test_health_apps_status_and_urls(harness):
    client, _ = harness
    assert client.health()["apps"] == ["erpnext"]
    assert client.apps()[0]["name"] == "erpnext"
    assert client.status("erpnext")["state"] == "healthy"
    # URL derived from the agent's host plus the app's reported port.
    assert client.app_url("erpnext") == "http://agent.test:8080"


def test_start_publishes_the_client_visible_application_origin(harness):
    client, drivers = harness
    client.start("erpnext")
    assert ("public_url", "http://agent.test:8080") in drivers["erpnext"].calls


def test_lease_scope_acquires_and_releases(harness, monkeypatch):
    client, drivers = harness
    monkeypatch.delenv(host_client.LEASE_ENV, raising=False)
    with client.lease_scope(["erpnext"], holder="scope-test") as lease_id:
        client.reset("erpnext", lease_id=lease_id)
    assert ("reset",) in drivers["erpnext"].calls
    # Released: a fresh acquire must not conflict.
    client.acquire_lease(["erpnext"], holder="after")


def test_lease_scope_reuses_ambient_lease(harness, monkeypatch):
    client, _ = harness
    ambient = client.acquire_lease(["erpnext"], holder="viewer")
    monkeypatch.setenv(host_client.LEASE_ENV, ambient)
    with client.lease_scope(["erpnext"], holder="fixture") as lease_id:
        assert lease_id == ambient
    # Ambient lease must survive the scope (the viewer still owns it).
    client.heartbeat(ambient)


def test_busy_surfaces_holder(harness):
    client, _ = harness
    client.acquire_lease(["erpnext"], holder="david")
    with pytest.raises(FixtureHostBusy) as excinfo:
        client.acquire_lease(["erpnext"], holder="sam")
    assert excinfo.value.held_by == "david"


def test_force_release_breaks_stale_lease(harness):
    client, _ = harness
    client.acquire_lease(["erpnext"], holder="zombie")
    released = client.force_release(["erpnext"])
    assert [entry["holder"] for entry in released] == ["zombie"]
    client.acquire_lease(["erpnext"], holder="sam")  # stack is free again


def test_client_can_annotate_and_list_exact_leases(harness):
    client, _ = harness
    lease_id = client.acquire_lease(["erpnext"], holder="task-test04")
    client.annotate_lease(lease_id, {
        "task": "test04", "kind": "run", "product": "Claude",
    })

    listed = client.list_leases()
    assert listed[0]["lease_id"] == lease_id
    assert listed[0]["execution"] == {
        "task": "test04", "kind": "run", "product": "Claude",
    }
    client.release(lease_id)
    assert client.list_leases() == []


def test_snapshot_round_trip_verifies_sha(harness, tmp_path):
    client, _ = harness
    lease = client.acquire_lease(["erpnext"], holder="t")
    client.snapshot("erpnext", "draft-a", lease_id=lease)
    pulled = client.pull_snapshot("erpnext", "draft-a", tmp_path / "state.tar")
    assert pulled.is_file()
    digest = hashlib.sha256(pulled.read_bytes()).hexdigest()
    assert len(digest) == 64
    client.push_snapshot("erpnext", "draft-b", pulled, lease_id=lease)
    client.restore_snapshot("erpnext", "draft-b", lease_id=lease)


def test_unreachable_agent_fails_loudly_naming_url():
    client = FixtureHostClient("http://127.0.0.1:9", "t")  # port 9: discard, closed
    with pytest.raises(FixtureHostError) as excinfo:
        client.health()
    assert "http://127.0.0.1:9" in str(excinfo.value)


def test_configured_client_reads_environment(monkeypatch):
    monkeypatch.delenv(host_client.URL_ENV, raising=False)
    assert host_client.configured_client() is None
    monkeypatch.setenv(host_client.URL_ENV, "http://vm.test:8090")
    monkeypatch.setenv(host_client.TOKEN_ENV, "tok")
    client = host_client.configured_client()
    assert client is not None
    assert client.base_url == "http://vm.test:8090"


def test_busy_message_includes_since_when_present():
    since = 1782230531.0
    exc = FixtureHostBusy("david", since)
    when = datetime.fromtimestamp(since).isoformat(timespec="seconds")
    assert str(exc) == f"fixture host busy: held by david since {when}"


def test_busy_message_omits_since_when_absent():
    exc = FixtureHostBusy("david", None)
    assert str(exc) == "fixture host busy: held by david"


def test_cleanup_local_process_terminates_and_waits(monkeypatch):
    calls = []

    class FakeProcess:
        def poll(self):
            return None

        def terminate(self):
            calls.append("terminate")

        def wait(self, timeout=None):
            calls.append(("wait", timeout))

    monkeypatch.setattr(host_client, "_local_process", FakeProcess())
    host_client._cleanup_local_process()
    assert calls == ["terminate", ("wait", 5.0)]


def test_cleanup_local_process_kills_after_grace_period(monkeypatch):
    import subprocess as subprocess_module

    calls = []

    class FakeProcess:
        def poll(self):
            return None

        def terminate(self):
            calls.append("terminate")

        def wait(self, timeout=None):
            calls.append(("wait", timeout))
            raise subprocess_module.TimeoutExpired(cmd="fixture-host", timeout=timeout)

        def kill(self):
            calls.append("kill")

    monkeypatch.setattr(host_client, "_local_process", FakeProcess())
    host_client._cleanup_local_process()
    assert calls == ["terminate", ("wait", 5.0), "kill", ("wait", 5.0)]


def test_cleanup_local_process_is_a_noop_when_already_exited(monkeypatch):
    class FakeProcess:
        def poll(self):
            return 0  # already exited

        def terminate(self):
            raise AssertionError("must not terminate an already-exited process")

    monkeypatch.setattr(host_client, "_local_process", FakeProcess())
    host_client._cleanup_local_process()  # must not raise


def test_local_agent_client_registers_cleanup_and_reports_stderr_on_timeout(
        monkeypatch):
    """Cold-boot failure must register cleanup and surface the child's own
    stderr, so a wedged local agent is both debuggable and never orphaned.
    """
    monkeypatch.setattr(host_client, "_local_client", None)
    monkeypatch.setattr(host_client, "_local_process", None)
    monkeypatch.delenv(host_client.URL_ENV, raising=False)
    monkeypatch.delenv(host_client.TOKEN_ENV, raising=False)

    from showAndTell.applications.host import server as agent_server
    monkeypatch.setattr(agent_server, "ensure_token", lambda path: "tok-local")

    class FakePopen:
        def __init__(self, argv, *, stdout=None, stderr=None):
            self.argv = argv
            stderr.write(b"booting...\nsomething broke: kaboom\n")
            stderr.flush()

        def terminate(self):
            pass

        def poll(self):
            return None

    monkeypatch.setattr(host_client.subprocess, "Popen", FakePopen)

    registered = []
    monkeypatch.setattr(
        host_client.atexit, "register", lambda fn: registered.append(fn))

    # The health probe hits a genuinely free loopback port (nothing is
    # listening, since Popen is faked), which fails fast on its own; forcing
    # the deadline to already be behind avoids a real 30s wait in this test.
    monotonic_calls = {"n": 0}

    def fake_monotonic():
        monotonic_calls["n"] += 1
        return 0.0 if monotonic_calls["n"] == 1 else 1_000.0

    monkeypatch.setattr(host_client.time, "monotonic", fake_monotonic)

    with pytest.raises(FixtureHostError) as excinfo:
        host_client.local_agent_client()

    assert registered == [host_client._cleanup_local_process]
    assert "kaboom" in str(excinfo.value)


class PortedDriver(FakeDriver):
    def __init__(self, name, tmp_path, port):
        super().__init__(name, tmp_path)
        self._port = port

    def status(self):
        return {"state": "healthy", "ports": {"app": self._port}}


@pytest.fixture()
def pooled_harness(tmp_path):
    drivers = {"erpnext": PortedDriver("erpnext", tmp_path, 8080),
               "erpnext_2": PortedDriver("erpnext_2", tmp_path, 8180)}
    families = {"erpnext": ("erpnext", "erpnext_2")}
    api = build_agent_api(drivers, TOKEN,
                          leases=LeaseStore(families=families),
                          families=families)
    transport = asgi_sync_transport(api)
    return (FixtureHostClient("http://agent.test:8090", TOKEN,
                              transport=transport),
            FixtureHostClient("http://agent.test:8090", TOKEN,
                              transport=transport),
            drivers)


def test_lease_assignment_routes_every_by_app_call(pooled_harness, monkeypatch):
    monkeypatch.delenv(host_client.ASSIGNMENT_ENV, raising=False)
    monkeypatch.delenv(host_client.LEASE_ENV, raising=False)
    first, second, drivers = pooled_harness
    first.acquire_lease(["erpnext"], holder="a")
    lease = second.acquire_lease(["erpnext"], holder="b")
    assert second.assignment == {"erpnext": "erpnext_2"}
    assert second.app_url("erpnext") == "http://agent.test:8180"
    second.reset("erpnext", lease_id=lease)
    assert ("reset",) in drivers["erpnext_2"].calls
    assert ("reset",) not in drivers["erpnext"].calls
    # The first client's identity grant recorded nothing.
    assert first.assignment == {}
    assert first.app_url("erpnext") == "http://agent.test:8080"


def test_release_clears_the_recorded_assignment(pooled_harness, monkeypatch):
    monkeypatch.delenv(host_client.ASSIGNMENT_ENV, raising=False)
    first, second, _ = pooled_harness
    first.acquire_lease(["erpnext"], holder="a")
    lease = second.acquire_lease(["erpnext"], holder="b")
    second.release(lease)
    assert second.assignment == {}
    assert second.app_url("erpnext") == "http://agent.test:8080"


def test_ambient_assignment_env_routes_a_fresh_client(pooled_harness, monkeypatch):
    import json

    first, _second, _drivers = pooled_harness
    monkeypatch.setenv(host_client.ASSIGNMENT_ENV,
                       json.dumps({"erpnext": "erpnext_2"}))
    assert first.app_url("erpnext") == "http://agent.test:8180"
    # Garbage in the env falls back to the literal name instead of crashing.
    monkeypatch.setenv(host_client.ASSIGNMENT_ENV, "{not json")
    assert first.app_url("erpnext") == "http://agent.test:8080"
