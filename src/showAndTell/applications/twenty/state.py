"""Deterministic task state for the isolated Twenty CRM fixture."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from showAndTell.applications.twenty import validate
from showAndTell.applications.twenty.api import TwentyClient
from showAndTell.applications.twenty.demo_dataset import seed_demo_dataset


APPLICATION = "twenty"
MARKER = "[ST] "
STAGES = frozenset({"NEW", "SCREENING", "MEETING", "PROPOSAL", "CUSTOMER"})


def _rows(value: object, path: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
        raise ValueError(f"{path} must be a list of objects")
    return list(value)


def validate_seed(spec: object) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    if not isinstance(spec, Mapping) or set(spec) != {"companies", "opportunities"}:
        raise ValueError("Twenty seed must contain exactly companies and opportunities")
    companies = _rows(spec["companies"], "companies")
    opportunities = _rows(spec["opportunities"], "opportunities")
    company_ids: set[str] = set()
    for index, row in enumerate(companies):
        if set(row) != {"source_id", "name", "domain", "employees"}:
            raise ValueError(f"companies[{index}] has unsupported keys")
        source_id = validate.required_text(row["source_id"], f"companies[{index}].source_id")
        if source_id in company_ids:
            raise ValueError(f"duplicate Twenty company source_id {source_id!r}")
        company_ids.add(source_id)
        validate.required_text(row["name"], f"companies[{index}].name")
        validate.required_text(row["domain"], f"companies[{index}].domain")
        if type(row["employees"]) is not int or row["employees"] < 1:
            raise ValueError(f"companies[{index}].employees must be a positive integer")
    source_ids: set[str] = set()
    for index, row in enumerate(opportunities):
        expected = {"source_id", "name", "company_source_id", "amount_usd", "stage", "close_date"}
        if set(row) != expected:
            raise ValueError(f"opportunities[{index}] has unsupported keys")
        source_id = validate.required_text(row["source_id"], f"opportunities[{index}].source_id")
        if source_id in source_ids:
            raise ValueError(f"duplicate Twenty opportunity source_id {source_id!r}")
        source_ids.add(source_id)
        validate.required_text(row["name"], f"opportunities[{index}].name")
        if row["company_source_id"] not in company_ids:
            raise ValueError(f"opportunities[{index}] names an unknown company")
        if type(row["amount_usd"]) is not int or row["amount_usd"] < 0:
            raise ValueError(f"opportunities[{index}].amount_usd must be a non-negative integer")
        if row["stage"] not in STAGES:
            raise ValueError(f"opportunities[{index}].stage is unsupported")
        close_date = validate.required_text(row["close_date"], f"opportunities[{index}].close_date")
        if len(close_date) != 10 or close_date[4] != "-" or close_date[7] != "-":
            raise ValueError(f"opportunities[{index}].close_date must be YYYY-MM-DD")
    return companies, opportunities


class State:
    def __init__(self, _manifest, *, client_factory=TwentyClient) -> None:
        self.client_factory = client_factory

    def _client(self, context):
        token = context.secrets.get("api_token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("Twenty state needs the driver's API token")
        return self.client_factory(context.url, token)

    def prepare(self, context) -> None:
        """Populate the reusable CRM base without touching task-owned rows."""
        owner_id = context.secrets.get("workspace_member_id")
        if not isinstance(owner_id, str) or not owner_id:
            raise RuntimeError("Twenty state needs the fixture workspace member id")
        with self._client(context) as client:
            seed_demo_dataset(client, owner_id)

    @staticmethod
    def _owned(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [row for row in rows if str(row.get("name", "")).startswith(MARKER)]

    def reset(self, context) -> None:
        with self._client(context) as client:
            # Tasks are outcomes created through the UI, not authored seed rows,
            # so they cannot carry the [ST] marker used by seeded companies and
            # opportunities.  This fixture is workspace-isolated: clear every
            # task between runs so interrupted replays cannot accumulate
            # duplicate titles and make a later semantic click ambiguous.
            for row in client.list_all("tasks"):
                client.delete("tasks", row["id"])
            for row in self._owned(client.list_all("opportunities")):
                client.delete("opportunities", row["id"])
            for row in self._owned(client.list_all("companies")):
                client.delete("companies", row["id"])

    def seed(self, context, spec: dict[str, Any]) -> None:
        companies, opportunities = validate_seed(spec)
        self.reset(context)
        owner_id = context.secrets.get("workspace_member_id")
        if not isinstance(owner_id, str) or not owner_id:
            raise RuntimeError("Twenty state needs the fixture workspace member id")
        with self._client(context) as client:
            company_ids: dict[str, str] = {}
            for row in companies:
                created = client.create(
                    "companies",
                    {
                        "name": f"{MARKER}{row['name']}",
                        "domainName": {
                            "primaryLinkUrl": f"https://{row['domain']}",
                            "primaryLinkLabel": "",
                            "secondaryLinks": [],
                        },
                        "employees": row["employees"],
                    },
                )
                company_ids[str(row["source_id"])] = created["id"]
            for row in opportunities:
                client.create(
                    "opportunities",
                    {
                        "name": f"{MARKER}{row['name']}",
                        "companyId": company_ids[str(row["company_source_id"])],
                        "amount": {
                            "amountMicros": int(row["amount_usd"]) * 1_000_000,
                            "currencyCode": "USD",
                        },
                        "stage": row["stage"],
                        "closeDate": f"{row['close_date']}T12:00:00.000Z",
                        "ownerId": owner_id,
                    },
                )

    def export(self, context) -> dict[str, Any]:
        with self._client(context) as client:
            companies = self._owned(client.list_all("companies"))
            opportunities = self._owned(client.list_all("opportunities"))
            tasks = client.list_all("tasks")
        company_names = {row["id"]: str(row["name"])[len(MARKER):] for row in companies}
        return {
            "companies": sorted(
                [
                    {
                        "id": row["id"],
                        "name": str(row["name"])[len(MARKER):],
                        "employees": row.get("employees"),
                    }
                    for row in companies
                ],
                key=lambda row: row["name"],
            ),
            "opportunities": sorted(
                [
                    {
                        "id": row["id"],
                        "name": str(row["name"])[len(MARKER):],
                        "company": company_names.get(row.get("companyId")),
                        "amount_usd": (
                            row.get("amount", {}).get("amountMicros", 0) // 1_000_000
                            if isinstance(row.get("amount"), Mapping)
                            else None
                        ),
                        "stage": row.get("stage"),
                        "close_date": str(row.get("closeDate", ""))[:10],
                    }
                    for row in opportunities
                ],
                key=lambda row: row["name"],
            ),
            "tasks": sorted(
                [
                    {
                        "id": row["id"],
                        "title": row.get("title"),
                        "status": row.get("status"),
                        "due_at": row.get("dueAt"),
                        "assignee_id": row.get("assigneeId"),
                        "body": row.get("bodyV2"),
                    }
                    for row in tasks
                ],
                key=lambda row: (str(row.get("title") or ""), row["id"]),
            ),
        }
