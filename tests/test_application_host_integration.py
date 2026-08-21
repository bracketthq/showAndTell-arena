"""Acceptance round-trip against a REAL local ERPNext stack.

Gated: export SHOWANDTELL_HOSTAGENT_INTEGRATION=1 and have the stack warm
(`showAndTell fixture-host` running is NOT needed — the agent app is driven
in-process; Docker and a started showAndTell-erpnext project are).

The bar is the one the vm-fixtures README verified by hand: state created
via the app's API is present in a snapshot, gone after a golden reset,
and back after restoring the snapshot.
"""
import os
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from showAndTell.applications.host.api import build_agent_api
from showAndTell.applications.erpnext.driver import ErpnextDriver
from showAndTell.applications.host.server import DEFAULT_ROOT

from tests._app_fixtures import app_manifest

pytestmark = pytest.mark.skipif(
    not os.environ.get("SHOWANDTELL_HOSTAGENT_INTEGRATION"),
    reason="integration: needs Docker and a warm local ERPNext stack",
)

ERP = "http://127.0.0.1:8080"
ADMIN = ("Administrator", os.environ.get(
    "SHOWANDTELL_ERPNEXT_ADMIN_PASSWORD", "showAndTell-admin"))


@pytest.fixture(scope="module")
def api_client():
    driver = ErpnextDriver(app_manifest("erpnext"), root=DEFAULT_ROOT)
    api = build_agent_api({"erpnext": driver}, "integration-token")
    with TestClient(api, headers={"authorization": "Bearer integration-token"}) as client:
        yield client


@pytest.fixture(scope="module")
def erp_session():
    session = httpx.Client(base_url=ERP, timeout=60.0)
    login = session.post("/api/method/login", data={
        "usr": ADMIN[0], "pwd": ADMIN[1]})
    assert login.status_code == 200, login.text
    return session


def test_snapshot_reset_restore_round_trip(api_client, erp_session):
    lease = api_client.post(
        "/v1/leases", json={"apps": ["erpnext"], "holder": "integration"}).json()
    headers = {"x-showAndTell-lease": lease["lease_id"]}

    # Golden must exist for reset; take it from the current clean state.
    assert api_client.post(
        "/v1/apps/erpnext/golden", headers=headers).status_code == 200

    marker = f"IT-{uuid.uuid4().hex[:8]}"
    created = erp_session.post("/api/resource/Item", json={
        "item_code": marker, "item_name": marker,
        "item_group": "All Item Groups", "stock_uom": "Nos"})
    assert created.status_code == 200, created.text

    name = f"integration-{marker}"
    assert api_client.post(
        f"/v1/apps/erpnext/snapshots/{name}", headers=headers).status_code == 200

    assert api_client.post(
        "/v1/apps/erpnext/reset", headers=headers).status_code == 200
    gone = erp_session.get(f"/api/resource/Item/{marker}")
    assert gone.status_code == 404  # golden predates the marker

    assert api_client.post(
        f"/v1/apps/erpnext/snapshots/{name}/restore", headers=headers).status_code == 200
    back = erp_session.get(f"/api/resource/Item/{marker}")
    assert back.status_code == 200

    api_client.delete(f"/v1/leases/{lease['lease_id']}")
