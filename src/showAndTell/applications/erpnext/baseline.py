"""The company, warehouse, fiscal year and price lists a site needs first.

A freshly reinstalled ERPNext site has not run the interactive setup
wizard, so nothing below can be assumed to exist. Every step is an
``ensure``: running this twice is the same as running it once, which is
what lets both a capture and a task run start from it.
"""
from __future__ import annotations

from datetime import date

from showAndTell.applications.erpnext.api import ERPNextClient, ResourceRef
from showAndTell.applications.erpnext.profiles import ERPNextSeedContext


def ensure_baseline(client: ERPNextClient) -> ERPNextSeedContext:
    preferred_company = "ShowAndTell Manufacturing"
    abbreviation = "STM"
    warehouse = f"Stores - {abbreviation}"

    # A freshly reinstalled ERPNext v16 site has not run the interactive
    # setup wizard. Company creation nevertheless builds a Goods In
    # Transit warehouse linked to this master record.
    client.ensure(
        "Warehouse Type",
        {"name": "Transit"},
        {"doctype": "Warehouse Type", "name": "Transit"},
    )
    # The fixture's company name predates the ShowTell -> ShowAndTell product
    # rename. Company is a linked ERPNext master, so an existing snapshot may
    # still use the old display name even though its stable abbreviation is
    # unchanged. Creating the renamed company would fail with a duplicate-STM
    # validation error (surfaced by Frappe as HTTP 417).
    company_ref = client.find_one(
        "Company", {"company_name": preferred_company}
    ) or client.find_one("Company", {"abbr": abbreviation})
    if company_ref is None:
        company_ref = client.ensure(
            "Company",
            {"company_name": preferred_company},
            {
                "doctype": "Company",
                "company_name": preferred_company,
                "abbr": abbreviation,
                "default_currency": "USD",
                "country": "United States",
            },
        )
    else:
        client.update(company_ref, {
            "default_currency": "USD",
            "country": "United States",
        })
    company = company_ref.name
    # Company insertion normally creates this warehouse.  Ensure it for a
    # freshly reinstalled/minimal site without replacing ERPNext behavior.
    if client.find_one("Warehouse", {"name": warehouse}) is None:
        client.ensure(
            "Warehouse",
            {"warehouse_name": "Stores", "company": company},
            {
                "doctype": "Warehouse",
                "warehouse_name": "Stores",
                "company": company,
                "is_group": 0,
            },
        )
    today = date.today()
    fiscal_year = str(today.year)
    client.ensure(
        "Fiscal Year",
        {"name": fiscal_year},
        {
            "doctype": "Fiscal Year",
            "year": fiscal_year,
            "year_start_date": f"{today.year}-01-01",
            "year_end_date": f"{today.year}-12-31",
            "disabled": 0,
            "companies": [{"company": company}],
        },
    )
    for name, buying, selling in (
        ("Standard Buying", 1, 0),
        ("Standard Selling", 0, 1),
    ):
        client.ensure(
            "Price List",
            {"price_list_name": name},
            {
                "doctype": "Price List",
                "price_list_name": name,
                "enabled": 1,
                "buying": buying,
                "selling": selling,
                "currency": "USD",
            },
        )
    client.update(
        ResourceRef("Global Defaults", "Global Defaults"),
        {
            "default_company": company,
            "default_currency": "USD",
            "country": "United States",
            "current_fiscal_year": fiscal_year,
        },
    )
    client.update(
        ResourceRef("System Settings", "System Settings"),
        {
            "setup_complete": 1,
            "enable_onboarding": 0,
            "language": "en",
            "time_zone": "UTC",
        },
    )
    opening_account = client.find_one(
        "Account",
        {"company": company, "account_type": "Temporary", "is_group": 0},
    )
    cost_center = client.find_one(
        "Cost Center", {"company": company, "is_group": 0}
    )
    if opening_account is None or cost_center is None:
        raise RuntimeError("ERPNext setup did not create opening-stock accounts")
    return ERPNextSeedContext(
        company=company,
        warehouse=warehouse,
        currency="USD",
        expense_account=opening_account.name,
        cost_center=cost_center.name,
    )
