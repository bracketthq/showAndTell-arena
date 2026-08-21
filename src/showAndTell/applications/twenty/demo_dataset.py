"""Twenty CRM-owned reusable company and opportunity fixture."""
from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from showAndTell.applications.twenty.api import TwentyClient


DATASET_PATH = Path(__file__).with_name("demo_dataset.json")


def load_demo_dataset() -> dict[str, Any]:
    dataset = json.loads(DATASET_PATH.read_text())
    if set(dataset) != {"metadata", "crm"}:
        raise ValueError("Twenty demo fixture has unsupported sections")
    metadata = dataset["metadata"]
    if metadata.get("dataset_id") != "enterprise-operations" or metadata.get("version") != 1:
        raise ValueError("Twenty demo fixture identity/version is unsupported")
    date.fromisoformat(str(metadata["as_of_date"]))
    companies = dataset["crm"]["companies"]
    opportunities = dataset["crm"]["opportunities"]
    account_codes = {str(row["account_code"]) for row in companies}
    domains = {str(row["domain"]) for row in companies}
    opportunity_codes = {str(row["opportunity_code"]) for row in opportunities}
    if len(companies) != 6 or len(account_codes) != 6 or len(domains) != 6:
        raise ValueError("Twenty demo fixture needs six unique companies")
    if len(opportunities) != 7 or len(opportunity_codes) != 7:
        raise ValueError("Twenty demo fixture needs seven unique opportunities")
    for row in opportunities:
        if row["account_code"] not in account_codes:
            raise ValueError(f"opportunity references unknown company: {row['opportunity_code']}")
        date.fromisoformat(str(row["close_date"]))
    return dataset


def load_demo_seed() -> dict[str, list[dict[str, Any]]]:
    crm = load_demo_dataset()["crm"]
    return {
        "companies": [
            {
                "source_id": row["account_code"],
                "name": row["name"],
                "domain": row["domain"],
                "employees": row["employees"],
            }
            for row in crm["companies"]
        ],
        "opportunities": [
            {
                "source_id": row["opportunity_code"],
                "name": row["name"],
                "company_source_id": row["account_code"],
                "amount_usd": row["amount_usd"],
                "stage": row["stage"],
                "close_date": row["close_date"],
            }
            for row in crm["opportunities"]
        ],
    }


def _domain(row: Mapping[str, Any]) -> str:
    value = row.get("domainName")
    if not isinstance(value, Mapping):
        return ""
    url = str(value.get("primaryLinkUrl") or "")
    return url.removeprefix("https://").removeprefix("http://").rstrip("/")


def seed_demo_dataset(client: TwentyClient, owner_id: str) -> None:
    """Idempotently upsert base companies/opportunities without task markers."""
    seed = load_demo_seed()
    companies_by_domain = {
        _domain(row): row for row in client.list_all("companies") if _domain(row)
    }
    company_ids: dict[str, str] = {}
    for row in seed["companies"]:
        values = {
            "name": row["name"],
            "domainName": {
                "primaryLinkUrl": f"https://{row['domain']}",
                "primaryLinkLabel": "",
                "secondaryLinks": [],
            },
            "employees": row["employees"],
        }
        existing = companies_by_domain.get(str(row["domain"]))
        if existing is None:
            existing = client.create("companies", values)
        else:
            existing = client.update("companies", str(existing["id"]), values)
        company_ids[str(row["source_id"])] = str(existing["id"])

    opportunities_by_name = {
        str(row.get("name")): row for row in client.list_all("opportunities")
    }
    for row in seed["opportunities"]:
        values = {
            "name": row["name"],
            "companyId": company_ids[str(row["company_source_id"])],
            "amount": {
                "amountMicros": int(row["amount_usd"]) * 1_000_000,
                "currencyCode": "USD",
            },
            "stage": row["stage"],
            "closeDate": f"{row['close_date']}T12:00:00.000Z",
            "ownerId": owner_id,
        }
        existing = opportunities_by_name.get(str(row["name"]))
        if existing is None:
            client.create("opportunities", values)
        else:
            client.update("opportunities", str(existing["id"]), values)
