"""Contract tests for the pinned Twenty CRM application plane."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from showAndTell.applications.twenty.api import PrivateToken, TwentyClient, TwentyError
from showAndTell.applications.twenty.bootstrap import (
    _MetadataClient,
    _workspace_token,
    TwentyBootstrapError,
)
from showAndTell.applications.twenty.state import State, validate_seed
from showAndTell.applications.registry import default_registry


def test_twenty_is_discovered_with_a_mutable_application_plane() -> None:
    registry = default_registry()
    manifest = registry.manifest("twenty")

    assert manifest.label == "Twenty CRM"
    assert manifest.app_port.host == 8044
    assert manifest.credentials["email"] == "operator@showAndTell.test"
    assert registry.has_driver("twenty")
    assert registry.has_state("twenty")
    assert registry.has_browser("twenty")


def test_twenty_compose_is_pinned_private_and_outbound_quiet() -> None:
    compose = default_registry().manifest("twenty").compose_file.read_text()

    assert compose.count("@sha256:") == 4
    assert "platform: linux/amd64" not in compose
    assert "redis:6.2.18-alpine@sha256:" in compose
    assert "${SHOWANDTELL_TWENTY_BIND_HOST:-127.0.0.1}" in compose
    assert "internal: true" in compose
    assert 'ALLOW_REQUESTS_TO_TWENTY_ICONS: "false"' in compose
    assert 'MARKETPLACE_CATALOG_SYNC_CRON_ENABLED: "false"' in compose
    assert 'ANALYTICS_ENABLED: "false"' in compose
    assert 'TELEMETRY_ENABLED: "false"' in compose
    assert 'max-size: "10m"' in compose
    assert 'max-file: "3"' in compose


def test_metadata_client_rejects_graphql_errors_even_with_data() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"operation": {"id": "partial"}}, "errors": [{"message": "denied"}]},
        )

    client = _MetadataClient("https://twenty.test")
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(TwentyBootstrapError, match="denied"):
            client.graphql("query { operation { id } }")
    finally:
        client.close()


def test_workspace_login_canonicalizes_the_manifest_email() -> None:
    class Client:
        base_url = "https://twenty.test"

        def __init__(self) -> None:
            self.variables = []

        def graphql(self, query, variables=None, *, token=None):
            self.variables.append(variables)
            if "getLoginTokenFromCredentials" in query:
                return {
                    "getLoginTokenFromCredentials": {
                        "loginToken": {"token": "login-token"}
                    }
                }
            return {
                "getAuthTokensFromLoginToken": {
                    "tokens": {
                        "accessOrWorkspaceAgnosticToken": {"token": "access-token"}
                    }
                }
            }

    client = Client()

    assert (
        _workspace_token(
            client, "operator@showAndTell.test", "ShowAndTell-Twenty1!"
        )
        == "access-token"
    )
    assert client.variables[0]["email"] == "operator@showandtell.test"


def test_core_rest_pagination_uses_the_server_end_cursor() -> None:
    cursors: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cursors.append(request.url.params.get("starting_after"))
        if len(cursors) == 1:
            return httpx.Response(
                200,
                json={
                    "data": {"companies": [{"id": "company-1"}]},
                    "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {"companies": [{"id": "company-2"}]},
                "pageInfo": {"hasNextPage": False, "endCursor": "cursor-2"},
            },
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = TwentyClient("https://twenty.test", "exact-token", client=http)
    try:
        assert [row["id"] for row in client.list_all("companies", limit=1)] == [
            "company-1",
            "company-2",
        ]
        assert cursors == [None, "cursor-1"]
    finally:
        http.close()


def test_mutation_transport_failure_is_reported_as_outcome_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection dropped", request=request)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = TwentyClient("https://twenty.test", "exact-token", client=http)
    try:
        with pytest.raises(TwentyError) as caught:
            client.update("opportunities", "opp-1", {"stage": "PROPOSAL"})
        assert caught.value.outcome_unknown is True
        assert caught.value.method == "PATCH"
        assert "exact-token" not in str(caught.value)
    finally:
        http.close()


def test_private_token_never_displays_its_value() -> None:
    token = PrivateToken("fixture-secret")
    assert repr(token) == "PrivateToken(<redacted>)"
    assert str(token) == "PrivateToken(<redacted>)"


class _MemoryClient:
    def __init__(self) -> None:
        self.rows = {
            "companies": [{"id": "default-company", "name": "Default Co"}],
            "opportunities": [{"id": "default-opportunity", "name": "Default Deal"}],
            "tasks": [],
        }
        self.next_id = 1

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def list_all(self, resource: str):
        return [dict(row) for row in self.rows[resource]]

    def create(self, resource: str, values):
        row = {"id": f"fixture-{self.next_id}", **dict(values)}
        self.next_id += 1
        self.rows[resource].append(row)
        return dict(row)

    def delete(self, resource: str, record_id: str):
        row = next(item for item in self.rows[resource] if item["id"] == record_id)
        self.rows[resource].remove(row)
        return dict(row)

    def update(self, resource: str, record_id: str, values):
        row = next(item for item in self.rows[resource] if item["id"] == record_id)
        row.update(dict(values))
        return dict(row)


SEED = {
    "companies": [
        {
            "source_id": "northstar",
            "name": "Northstar Labs",
            "domain": "northstar.showAndTell.test",
            "employees": 240,
        }
    ],
    "opportunities": [
        {
            "source_id": "renewal",
            "name": "Northstar Renewal",
            "company_source_id": "northstar",
            "amount_usd": 80_000,
            "stage": "MEETING",
            "close_date": "2026-08-10",
        }
    ],
}


def test_state_seed_is_idempotent_and_preserves_non_fixture_records() -> None:
    memory = _MemoryClient()
    context = SimpleNamespace(
        url="https://twenty.test",
        secrets={"api_token": "token", "workspace_member_id": "member-1"},
    )
    state = State(None, client_factory=lambda _url, _token: memory)

    state.seed(context, SEED)
    state.seed(context, SEED)
    exported = state.export(context)

    assert [(row["name"], row["stage"]) for row in exported["opportunities"]] == [
        ("Northstar Renewal", "MEETING")
    ]
    assert any(row["id"] == "default-company" for row in memory.rows["companies"])
    assert any(
        row["id"] == "default-opportunity" for row in memory.rows["opportunities"]
    )


def test_prepare_adds_reusable_base_and_task_reset_preserves_it() -> None:
    memory = _MemoryClient()
    context = SimpleNamespace(
        url="https://twenty.test",
        secrets={"api_token": "token", "workspace_member_id": "member-1"},
    )
    state = State(None, client_factory=lambda _url, _token: memory)

    state.prepare(context)
    state.prepare(context)
    assert len([row for row in memory.rows["companies"] if row["name"] != "Default Co"]) == 6
    assert len([row for row in memory.rows["opportunities"] if row["name"] != "Default Deal"]) == 7

    state.seed(context, SEED)
    state.reset(context)
    assert any(row["name"] == "Northstar Analytics" for row in memory.rows["companies"])
    assert any(
        row["name"] == "Northstar Analytics 2026 Renewal"
        for row in memory.rows["opportunities"]
    )


def test_reset_clears_ui_created_tasks_and_export_reports_task_outcomes() -> None:
    memory = _MemoryClient()
    memory.rows["tasks"] = [
        {
            "id": "task-1",
            "title": "Follow up with Northstar",
            "status": "TODO",
            "dueAt": "2026-08-19T12:00:00.000Z",
            "assigneeId": "member-1",
            "bodyV2": {"markdown": "Call the buyer"},
        }
    ]
    context = SimpleNamespace(
        url="https://twenty.test",
        secrets={"api_token": "token", "workspace_member_id": "member-1"},
    )
    state = State(None, client_factory=lambda _url, _token: memory)

    assert state.export(context)["tasks"] == [
        {
            "id": "task-1",
            "title": "Follow up with Northstar",
            "status": "TODO",
            "due_at": "2026-08-19T12:00:00.000Z",
            "assignee_id": "member-1",
            "body": {"markdown": "Call the buyer"},
        }
    ]

    state.reset(context)
    assert memory.rows["tasks"] == []


def test_seed_validation_rejects_an_unknown_company_reference() -> None:
    invalid = {**SEED, "opportunities": [{**SEED["opportunities"][0], "company_source_id": "missing"}]}
    with pytest.raises(ValueError, match="unknown company"):
        validate_seed(invalid)
