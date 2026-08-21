"""The viewer's agent wiring: per-application, loud failures, lazy token."""
import os
from pathlib import Path

import pytest

from tests.conftest import asgi_sync_transport

from showAndTell.viewer import fixture_host  # noqa: E402

from showAndTell.applications.host import client as host_client  # noqa: E402
from showAndTell.applications.host.client import (  # noqa: E402
    FixtureHostClient,
    FixtureHostError,
    FixtureHostUnsupported,
)
from showAndTell.applications.host.api import build_agent_api  # noqa: E402
from showAndTell.applications.host.leases import LeaseStore  # noqa: E402

from tests.test_application_host_api import FakeDriver, NoSnapshotDriver  # noqa: E402

TOKEN = "viewer-token"


class OnlyofficeDriver(NoSnapshotDriver):
    """NoSnapshotDriver's inherited secrets() answers erpnext's shape.

    ONLYOFFICE's real driver returns a connector_token; this fake needs the
    same shape so fixture_host.apply's lazy token fetch has something to read.
    """

    def secrets(self) -> dict:
        return {"connector_token": "onlyoffice-connector-token"}

    def status(self) -> dict:
        return {"state": "healthy", "ports": {"app": 8081, "connector": 8085}}


class RoundcubeDriver(NoSnapshotDriver):
    def secrets(self) -> dict:
        return {"mail_password": "showAndTell-mail", "imap_port": 1143,
                "imap_starttls": True}

    def status(self) -> dict:
        return {"state": "healthy", "ports": {"app": 8082, "imap": 1143}}


@pytest.fixture()
def agent_client(tmp_path):
    drivers = {
        "erpnext": FakeDriver("erpnext", tmp_path),
        "onlyoffice": OnlyofficeDriver("onlyoffice", tmp_path),
        "roundcube": RoundcubeDriver("roundcube", tmp_path),
    }
    api = build_agent_api(drivers, TOKEN, leases=LeaseStore())
    transport = asgi_sync_transport(api)
    return FixtureHostClient("http://agent.test:8090", TOKEN, transport=transport), drivers


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("SHOWANDTELL_"):
            monkeypatch.delenv(key, raising=False)


def test_vm_fixtures_module_is_gone():
    assert not (Path(fixture_host.__file__).parent / "vm_fixtures.py").exists()


# -- snapshot capability ---------------------------------------------------

def test_an_application_without_snapshots_answers_unsupported(agent_client):
    """Capability comes from the driver's 501, not a hard-coded app table."""
    client, _ = agent_client
    lease = client.acquire_lease(["roundcube"], holder="test")
    with pytest.raises(FixtureHostUnsupported) as excinfo:
        client.snapshot("roundcube", "draft-x", lease_id=lease)
    assert excinfo.value.app == "roundcube"


def test_an_application_with_snapshots_does_not_raise(agent_client):
    client, drivers = agent_client
    lease = client.acquire_lease(["erpnext"], holder="test")
    client.snapshot("erpnext", "draft-x", lease_id=lease)
    assert "draft-x" in drivers["erpnext"].known_snapshots


def test_unsupported_is_a_fixture_host_error(agent_client):
    """Callers that do not care about the distinction keep working unchanged."""
    assert issubclass(FixtureHostUnsupported, FixtureHostError)


# -- agent reachability ----------------------------------------------------

def test_configured_but_unreachable_agent_fails_loudly(monkeypatch):
    monkeypatch.setenv(host_client.URL_ENV, "http://127.0.0.1:9")
    monkeypatch.setenv(host_client.TOKEN_ENV, "t")
    with pytest.raises(FixtureHostError) as excinfo:
        fixture_host.client_for_capture()
    assert "http://127.0.0.1:9" in str(excinfo.value)


def test_auto_spawned_agent_stays_local_after_its_url_is_published(
        monkeypatch, agent_client):
    """A second capture must still bootstrap the viewer-owned local agent.

    The first capture publishes URL_ENV for adapters.  Treating that internal
    publication as operator configuration makes a later ERPNext capture skip
    golden creation and fail at its first reset.
    """
    client, _ = agent_client

    class RunningProcess:
        def poll(self):
            return None

    monkeypatch.setattr(host_client, "_local_client", client)
    monkeypatch.setattr(host_client, "_local_process", RunningProcess())
    monkeypatch.setenv(host_client.URL_ENV, client.base_url)
    monkeypatch.setenv(host_client.TOKEN_ENV, TOKEN)

    selected = fixture_host.client_for_capture()

    assert selected.client is client
    assert selected.is_local is True


def test_client_exposes_the_agent_host_for_non_http_services(agent_client):
    """IMAP is not a URL, so the host has to be reachable on its own."""
    client, _ = agent_client
    assert client.host == "agent.test"
