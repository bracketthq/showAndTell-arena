"""Contract tests for the fixture host agent's HTTP surface."""
import io
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from showAndTell.applications.host.api import build_agent_api
from showAndTell.applications.host.protocol import DriverError, Unsupported
from showAndTell.applications.host.leases import LeaseStore

TOKEN = "test-token"


def make_tar(tmp_path: Path, name: str) -> Path:
    payload = tmp_path / "site.sql.gz"
    payload.write_bytes(b"fake-dump")
    out = tmp_path / f"{name}.tar"
    with tarfile.open(out, "w") as tar:
        tar.add(payload, arcname="site.sql.gz")
    return out


class FakeDriver:
    """Records calls; snapshot bytes round-trip through a real tar file."""

    def __init__(self, name: str, tmp_path: Path):
        self.name = name
        self.tmp_path = tmp_path
        self.calls: list[tuple] = []
        self.known_snapshots: set[str] = set()
        self.fail_reset = False

    def set_public_url(self, value: str) -> None:
        self.calls.append(("public_url", value))

    def start(self, *, wait: bool = True) -> None:
        self.calls.append(("start", wait))

    def stop(self) -> None:
        self.calls.append(("stop",))

    def status(self) -> dict:
        # "connector" is only meaningful for onlyoffice, but including it
        # keeps this single fake reusable for connector_url() tests.
        return {"state": "healthy", "ports": {"app": 8080, "connector": 8085}}

    def reset(self) -> None:
        self.calls.append(("reset",))
        if self.fail_reset:
            raise DriverError("db restore failed")

    def baseline(self) -> None:
        self.calls.append(("baseline",))

    def golden(self) -> None:
        self.calls.append(("golden",))

    def snapshot(self, name: str) -> None:
        self.calls.append(("snapshot", name))
        self.known_snapshots.add(name)

    def fetch_snapshot(self, name: str) -> Path:
        if name not in self.known_snapshots:
            raise DriverError(f"no snapshot named '{name}'")
        return make_tar(self.tmp_path, name)

    def load_snapshot(self, name: str, tar_path: Path) -> None:
        self.calls.append(("load_snapshot", name, tar_path.read_bytes()[:4]))
        self.known_snapshots.add(name)

    def restore(self, name: str) -> None:
        self.calls.append(("restore", name))

    def secrets(self) -> dict:
        return {"admin_password": "showAndTell-admin"}


class NoSnapshotDriver(FakeDriver):
    def snapshot(self, name: str) -> None:
        raise Unsupported("onlyoffice has no task state to snapshot")


class ExplodingDriver(FakeDriver):
    """Raises a plain, non-DriverError exception from restore.

    Models a genuine bug in a driver (an unhandled AttributeError, a bare
    docker-py exception, ...) rather than the DriverError the rest of the
    surface expects — the route must not let that bypass the degraded
    invariant or the JSON error contract.
    """

    def restore(self, name: str) -> None:
        self.calls.append(("restore", name))
        raise RuntimeError("unexpected driver bug")


@pytest.fixture()
def harness(tmp_path):
    drivers = {
        "erpnext": FakeDriver("erpnext", tmp_path),
        "onlyoffice": NoSnapshotDriver("onlyoffice", tmp_path),
    }
    api = build_agent_api(drivers, TOKEN, leases=LeaseStore())
    client = TestClient(api, headers={"authorization": f"Bearer {TOKEN}"})
    return client, drivers


def lease_for(client, apps):
    response = client.post("/v1/leases", json={"apps": apps, "holder": "tests"})
    assert response.status_code == 200
    return {"x-showAndTell-lease": response.json()["lease_id"]}


def test_requests_without_token_are_rejected(harness):
    client, _ = harness
    assert client.get("/v1/health", headers={"authorization": ""}).status_code == 401
    bad = client.get("/v1/health", headers={"authorization": "Bearer wrong"})
    assert bad.json()["code"] == "bad_token"


def test_health_and_apps_listing(harness):
    client, _ = harness
    assert client.get("/v1/health").json()["apps"] == ["erpnext", "onlyoffice"]
    listing = client.get("/v1/apps").json()
    assert [entry["name"] for entry in listing] == ["erpnext", "onlyoffice"]
    assert listing[0]["state"] == "healthy"
    assert listing[0]["degraded"] is False


def test_unknown_app_is_404(harness):
    client, _ = harness
    assert client.get("/v1/apps/nope/status").json()["code"] == "unknown_app"
    assert client.get("/v1/apps/nope/status").status_code == 404


def test_start_and_stop_need_no_lease(harness):
    client, drivers = harness
    assert client.post(
        "/v1/apps/erpnext/start",
        json={"wait": True, "public_url": "http://agent.test:8080"},
    ).status_code == 200
    assert client.post("/v1/apps/erpnext/stop").status_code == 200
    assert ("public_url", "http://agent.test:8080") in drivers["erpnext"].calls
    assert ("start", True) in drivers["erpnext"].calls
    assert ("stop",) in drivers["erpnext"].calls


def test_reset_requires_lease_then_succeeds(harness):
    client, drivers = harness
    denied = client.post("/v1/apps/erpnext/reset")
    assert denied.status_code == 409
    assert denied.json()["code"] == "lease_required"
    headers = lease_for(client, ["erpnext"])
    assert client.post("/v1/apps/erpnext/reset", headers=headers).status_code == 200
    assert ("reset",) in drivers["erpnext"].calls


def test_foreign_lease_reports_holder(harness):
    client, _ = harness
    lease_for(client, ["erpnext"])  # someone else holds it
    other = client.post("/v1/leases", json={"apps": ["erpnext"], "holder": "sam"})
    assert other.status_code == 409
    assert other.json()["code"] == "lease_held"
    assert other.json()["held_by"] == "tests"
    denied = client.post("/v1/apps/erpnext/reset")
    assert denied.json()["code"] == "lease_held"


def test_force_release_breaks_a_foreign_lease(harness):
    client, _ = harness
    lease_for(client, ["erpnext"])  # a stale holder blocks the stack
    freed = client.post("/v1/leases/force-release", json={"apps": ["erpnext"]})
    assert freed.status_code == 200
    assert freed.json()["released"] == [{"holder": "tests", "apps": ["erpnext"]}]
    # The stack is genuinely free again.
    assert client.post(
        "/v1/leases", json={"apps": ["erpnext"], "holder": "sam"}).status_code == 200
    # Clearing an idle stack is a harmless no-op…
    idle = client.post("/v1/leases/force-release", json={"apps": ["onlyoffice"]})
    assert idle.status_code == 200
    assert idle.json()["released"] == []
    # …but the request still validates its inputs.
    unknown = client.post("/v1/leases/force-release", json={"apps": ["nope"]})
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "unknown_app"
    empty = client.post("/v1/leases/force-release", json={"apps": []})
    assert empty.status_code == 400


def test_execution_metadata_is_listed_with_the_host_lease(harness):
    client, _ = harness
    lease = client.post(
        "/v1/leases", json={"apps": ["erpnext"], "holder": "task-test04"}
    ).json()
    response = client.put(
        f"/v1/leases/{lease['lease_id']}/execution",
        json={"task": "test04", "kind": "run", "product": "Codex"},
    )
    assert response.status_code == 200
    listed = client.get("/v1/leases").json()["leases"]
    assert listed[0]["lease_id"] == lease["lease_id"]
    assert listed[0]["apps"] == {"erpnext": "erpnext"}
    assert listed[0]["execution"] == {
        "task": "test04", "kind": "run", "product": "Codex"}
    assert isinstance(listed[0]["granted_at"], float)


def test_failed_reset_marks_app_degraded_until_success(harness):
    client, drivers = harness
    drivers["erpnext"].fail_reset = True
    headers = lease_for(client, ["erpnext"])
    failed = client.post("/v1/apps/erpnext/reset", headers=headers)
    assert failed.status_code == 500
    assert failed.json()["code"] == "operation_failed"
    assert client.get("/v1/apps/erpnext/status").json()["degraded"] is True
    drivers["erpnext"].fail_reset = False
    assert client.post("/v1/apps/erpnext/reset", headers=headers).status_code == 200
    assert client.get("/v1/apps/erpnext/status").json()["degraded"] is False


def test_snapshot_round_trip_with_sha256(harness):
    client, drivers = harness
    headers = lease_for(client, ["erpnext"])
    assert client.post(
        "/v1/apps/erpnext/snapshots/draft-x", headers=headers).status_code == 200
    pulled = client.get("/v1/apps/erpnext/snapshots/draft-x")
    assert pulled.status_code == 200
    assert "x-showAndTell-sha256" in pulled.headers
    with tarfile.open(fileobj=io.BytesIO(pulled.content)) as tar:
        assert "site.sql.gz" in tar.getnames()
    pushed = client.put(
        "/v1/apps/erpnext/snapshots/draft-y", headers=headers, content=pulled.content)
    assert pushed.status_code == 200
    assert client.post(
        "/v1/apps/erpnext/snapshots/draft-y/restore", headers=headers).status_code == 200
    assert ("restore", "draft-y") in drivers["erpnext"].calls


def test_unknown_snapshot_and_bad_name(harness):
    client, _ = harness
    assert client.get("/v1/apps/erpnext/snapshots/ghost").json()["code"] == "unknown_snapshot"
    headers = lease_for(client, ["erpnext"])
    bad = client.post("/v1/apps/erpnext/snapshots/bad%2Fname", headers=headers)
    assert bad.json()["code"] == "bad_snapshot_name"


def test_unsupported_operation_is_501(harness):
    client, _ = harness
    headers = lease_for(client, ["onlyoffice"])
    response = client.post("/v1/apps/onlyoffice/snapshots/x", headers=headers)
    assert response.status_code == 501
    assert response.json()["code"] == "unsupported"


def test_secrets_and_lease_lifecycle(harness):
    client, _ = harness
    assert client.get("/v1/apps/erpnext/secrets").json()["admin_password"] == "showAndTell-admin"
    lease = client.post("/v1/leases", json={"apps": ["erpnext"], "holder": "t"}).json()
    beat = client.post(f"/v1/leases/{lease['lease_id']}/heartbeat")
    assert beat.json()["expires_at"] >= lease["expires_at"]
    assert client.delete(f"/v1/leases/{lease['lease_id']}").status_code == 200
    assert client.post("/v1/leases/ghost/heartbeat").json()["code"] == "unknown_lease"


def test_unexpected_exception_during_restore_marks_degraded(tmp_path):
    """A driver bug (plain RuntimeError, not DriverError) must not bypass the
    degraded invariant or the JSON error contract the way an unhandled 500
    from ASGI middleware would.
    """
    drivers = {"erpnext": ExplodingDriver("erpnext", tmp_path)}
    api = build_agent_api(drivers, TOKEN, leases=LeaseStore())
    client = TestClient(api, headers={"authorization": f"Bearer {TOKEN}"})
    headers = lease_for(client, ["erpnext"])
    response = client.post(
        "/v1/apps/erpnext/snapshots/golden/restore", headers=headers)
    assert response.status_code == 500
    assert response.json()["code"] == "operation_failed"
    assert ("restore", "golden") in drivers["erpnext"].calls
    assert client.get("/v1/apps/erpnext/status").json()["degraded"] is True


def test_push_snapshot_rejects_body_that_does_not_match_sha_header(harness):
    client, drivers = harness
    headers = lease_for(client, ["erpnext"])
    headers["x-showAndTell-sha256"] = "0" * 64  # deliberately wrong digest
    response = client.put(
        "/v1/apps/erpnext/snapshots/draft-corrupt", headers=headers,
        content=b"tar-bytes")
    assert response.status_code == 400
    assert response.json()["code"] == "snapshot_corrupt"
    assert "draft-corrupt" not in drivers["erpnext"].known_snapshots


def test_non_ascii_authorization_header_is_401_not_500(harness):
    """secrets.compare_digest raises TypeError on a non-ASCII str, which an
    unencoded comparison would surface as an unhandled 500 instead of 401.
    """
    client, _ = harness
    # A list of raw byte-tuples bypasses httpx's client-side ASCII header
    # validation, matching how a non-ASCII byte sequence actually arrives
    # over the wire (ASGI servers decode headers as latin-1).
    response = client.get(
        "/v1/health", headers=[(b"authorization", b"Bearer caf\xc3\xa9")])
    assert response.status_code == 401
    assert response.json()["code"] == "bad_token"


@pytest.fixture()
def pooled(tmp_path):
    drivers = {
        "erpnext": FakeDriver("erpnext", tmp_path),
        "erpnext_2": FakeDriver("erpnext_2", tmp_path),
        "onlyoffice": NoSnapshotDriver("onlyoffice", tmp_path),
    }
    families = {"erpnext": ("erpnext", "erpnext_2"),
                "onlyoffice": ("onlyoffice",)}
    api = build_agent_api(
        drivers, TOKEN, leases=LeaseStore(families=families),
        families=families)
    client = TestClient(api, headers={"authorization": f"Bearer {TOKEN}"})
    return client, drivers


def test_lease_response_carries_the_assignment(pooled):
    client, _ = pooled
    first = client.post(
        "/v1/leases", json={"apps": ["erpnext"], "holder": "a"}).json()
    second = client.post(
        "/v1/leases", json={"apps": ["erpnext", "onlyoffice"], "holder": "b"})
    assert first["apps"] == {"erpnext": "erpnext"}
    assert second.status_code == 200
    assert second.json()["apps"] == {"erpnext": "erpnext_2",
                                     "onlyoffice": "onlyoffice"}


def test_health_reports_logical_apps_and_pool_sizes(pooled):
    client, _ = pooled
    health = client.get("/v1/health").json()
    assert health["apps"] == ["erpnext", "onlyoffice"]
    assert health["pools"] == {"erpnext": 2, "onlyoffice": 1}


def test_mutations_are_gated_by_the_granted_instance(pooled):
    client, drivers = pooled
    client.post("/v1/leases", json={"apps": ["erpnext"], "holder": "a"})
    grant = client.post(
        "/v1/leases", json={"apps": ["erpnext"], "holder": "b"}).json()
    instance = grant["apps"]["erpnext"]
    assert instance == "erpnext_2"
    headers = {"x-showAndTell-lease": grant["lease_id"]}
    assert client.post(
        f"/v1/apps/{instance}/reset", headers=headers).status_code == 200
    # The same lease does not cover the base instance.
    denied = client.post("/v1/apps/erpnext/reset", headers=headers)
    assert denied.status_code == 409
    assert ("reset",) in drivers["erpnext_2"].calls
    assert ("reset",) not in drivers["erpnext"].calls


def test_exhausted_pool_is_a_lease_held_conflict(pooled):
    client, _ = pooled
    client.post("/v1/leases", json={"apps": ["erpnext"], "holder": "a"})
    client.post("/v1/leases", json={"apps": ["erpnext"], "holder": "b"})
    full = client.post("/v1/leases", json={"apps": ["erpnext"], "holder": "c"})
    assert full.status_code == 409
    assert full.json()["code"] == "lease_held"
    assert full.json()["held_by"] == "a (2/2 in use)"
