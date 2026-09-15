"""Northwind-derived demo catalog for capture-mode ERPNext sites.

``prepare_capture`` used to boot the ERP for a Task Inspector recording
session with only company/warehouse scaffolding, which left task authors
hand-entering every customer, item, and order before they could demonstrate
anything. This module pre-seeds a realistic catalog — customers with
addresses, an item catalog with buying/selling prices and opening stock,
suppliers, recent sales-order history, and reusable purchasing transactions —
through the same REST primitives the profile seeders use, so a capture starts
on a populated ERP.

The same application-owned fixture also carries renewal receivables and
preventive-maintenance inventory. Those sections use their own completion
marker so an older catalog-bearing snapshot can be upgraded idempotently.

Only the capture path calls this. Task seeding (``hooks.seed``) never does:
a task's world stays exactly what its seed.json declares, so deliberately
unseeded records cannot leak in from the demo catalog.

Master records go through ``ensure`` (natural-key upsert): re-running the
preparation repairs hand-modified masters back to catalog values. Transaction
records are guarded by stable external references instead of being upserted,
and fixture reset (bench reinstall / snapshot restore) owns restoring them.

The fixture host's ``golden`` snapshot is expected to carry this catalog
(managed by the ERPNext application driver), so a normal
capture preparation restores it in seconds and this seeder reduces to a
single existence probe. Whenever demo_dataset.json changes, regenerate the
snapshot with scripts/vm-fixtures/refresh_demo_snapshot.py.

Set ``SHOWANDTELL_CAPTURE_DEMO_DATA=0`` to prepare a bare site instead.
"""
from __future__ import annotations

import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from showAndTell.applications.erpnext.api import ERPNextClient, ResourceRef
from showAndTell.applications.erpnext.profiles import ERPNextSeedContext, _custom_field, _money

DATASET_PATH = Path(__file__).with_name("demo_dataset.json")


def load_demo_dataset() -> dict[str, Any]:
    dataset = json.loads(DATASET_PATH.read_text())
    _validate_enterprise_workflows(dataset["enterprise_workflows"])
    return dataset


def _validate_enterprise_workflows(dataset: Mapping[str, Any]) -> None:
    if set(dataset) != {
        "metadata", "crm", "accounts_receivable", "maintenance_inventory",
    }:
        raise ValueError("ERPNext enterprise fixture has unsupported sections")
    metadata = dataset["metadata"]
    if metadata.get("dataset_id") != "enterprise-operations" or metadata.get("version") != 1:
        raise ValueError("ERPNext enterprise fixture identity/version is unsupported")
    as_of = date.fromisoformat(str(metadata["as_of_date"]))

    companies = dataset["crm"]["companies"]
    account_codes = {str(row["account_code"]) for row in companies}
    if len(companies) != 6 or len(account_codes) != len(companies):
        raise ValueError("ERPNext enterprise fixture needs six unique companies")

    invoices = dataset["accounts_receivable"]["invoices"]
    references = {str(row["invoice_reference"]) for row in invoices}
    if len(invoices) != 6 or len(references) != len(invoices):
        raise ValueError("ERPNext enterprise fixture needs six unique invoices")
    for row in invoices:
        if row["account_code"] not in account_codes:
            raise ValueError(f"invoice references unknown account: {row['invoice_reference']}")
        posting = date.fromisoformat(str(row["posting_date"]))
        due = date.fromisoformat(str(row["due_date"]))
        amount = Decimal(str(row["amount_usd"]))
        paid = Decimal(str(row["paid_usd"]))
        if due < posting or amount <= 0 or paid < 0 or paid > amount:
            raise ValueError(f"invalid invoice: {row['invoice_reference']}")
    overdue = {
        row["invoice_reference"]: (
            as_of - date.fromisoformat(str(row["due_date"]))
        ).days
        for row in invoices
    }
    if overdue["AR-26001"] != 31 or overdue["AR-26003"] != 30:
        raise ValueError("ERPNext receivables fixture lost its 30/31-day boundary")

    kits = dataset["maintenance_inventory"]["kits"]
    kit_codes = {str(row["item_code"]) for row in kits}
    vehicle_classes = {str(row["vehicle_class"]) for row in kits}
    if len(kits) != 3 or len(kit_codes) != 3 or len(vehicle_classes) != 3:
        raise ValueError("ERPNext maintenance fixture needs three unique kits")
    for row in kits:
        on_hand, reserved = int(row["on_hand"]), int(row["reserved"])
        if on_hand < 0 or reserved < 0 or reserved > on_hand:
            raise ValueError(f"invalid kit quantities: {row['item_code']}")


def seed_demo_dataset(client: ERPNextClient, context: ERPNextSeedContext, *,
                      force: bool = False) -> None:
    if not force and os.environ.get("SHOWANDTELL_CAPTURE_DEMO_DATA", "1") == "0":
        return
    dataset = load_demo_dataset()
    # Purchase invoices are seeded last, so the final invoice's presence proves
    # the whole catalog landed — one probe instead of ~900 upsert round-trips
    # whenever the site was restored from a catalog-bearing golden snapshot.
    invoice_matches = dataset["purchase_invoice_matches"]
    final_ref = invoice_matches["chains"][-1]["ref"]
    if not force and client.find_one(
            "Purchase Invoice", {"bill_no": f"INV-3WM-{final_ref}"}) is not None:
        _seed_enterprise_workflows(
            client, context, dataset["enterprise_workflows"], force=force
        )
        return
    customers = _masters(client, context, dataset)
    _opening_stock(client, context, dataset)
    _order_history(client, context, dataset, customers)
    _purchase_invoice_matches_allowing_fixture_variances(
        client, context, invoice_matches
    )
    _seed_enterprise_workflows(
        client, context, dataset["enterprise_workflows"], force=force
    )


def _masters(client: ERPNextClient, context: ERPNextSeedContext,
             dataset: Mapping[str, Any]) -> dict[str, str]:
    """Ensure the catalog masters; return customer code -> ERPNext docname."""
    # The same Customer/Item custom fields _sales_masters installs, so a task
    # captured on this catalog and later re-seeded through seed_sales_order
    # upserts the identical records instead of duplicating them.
    for dt, fields in {
        "Customer": (
            ("custom_showtell_reference", "Source Customer Number", "Data"),
            ("custom_showtell_business_partner", "Business Partner", "Data"),
            ("custom_showtell_search_term", "Search Term", "Data"),
        ),
        "Item": (
            ("custom_showtell_pcr", "PCR Cost", "Currency"),
            ("custom_showtell_ptr", "PTR", "Currency"),
            ("custom_showtell_pcip", "PCIP", "Currency"),
        ),
    }.items():
        for fieldname, label, fieldtype in fields:
            _custom_field(
                client, dt=dt, fieldname=fieldname, label=label,
                fieldtype=fieldtype,
                insert_after="customer_name" if dt == "Customer" else "item_group")

    for group in dataset["item_groups"]:
        client.ensure("Item Group", {"item_group_name": group["name"]}, {
            "doctype": "Item Group", "item_group_name": group["name"],
            "parent_item_group": "All Item Groups", "is_group": 0,
        })

    for supplier in dataset["suppliers"]:
        client.ensure("Supplier", {"supplier_name": supplier["name"]}, {
            "doctype": "Supplier", "supplier_name": supplier["name"],
            "supplier_group": "All Supplier Groups", "supplier_type": "Company",
            "country": supplier["country"],
        })

    customer_names: dict[str, str] = {}
    for customer in dataset["customers"]:
        ref = client.ensure(
            "Customer", {"custom_showtell_reference": customer["number"]}, {
                "doctype": "Customer", "customer_name": customer["name"],
                "customer_type": "Company", "customer_group": "Commercial",
                "territory": "All Territories",
                "custom_showtell_reference": customer["number"],
                "custom_showtell_business_partner": customer["business_partner"],
                "custom_showtell_search_term": customer["code"],
            })
        customer_names[customer["code"]] = ref.name
        client.ensure("Address", {
            "address_title": customer["name"], "address_type": "Shipping",
        }, {
            "doctype": "Address",
            "address_title": customer["name"], "address_type": "Shipping",
            "address_line1": customer["address_line1"],
            "city": customer["city"], "country": customer["country"],
            "pincode": customer["postal_code"],
            "links": [{"link_doctype": "Customer", "link_name": ref.name}],
            "is_shipping_address": 1,
        })

    for item in dataset["items"]:
        cost = _money(item["cost"])
        client.ensure("Item", {"item_code": item["code"]}, {
            "doctype": "Item", "item_code": item["code"],
            "item_name": item["name"], "item_group": item["group"],
            "stock_uom": "Nos", "is_stock_item": 1, "standard_rate": cost,
            "custom_showtell_pcr": cost,
            "custom_showtell_ptr": 0,
            "custom_showtell_pcip": cost,
            "reorder_levels": [{
                "warehouse": context.warehouse,
                "warehouse_group": context.warehouse,
                "warehouse_reorder_level": item["reorder_level"],
                "warehouse_reorder_qty": item["reorder_qty"],
                "material_request_type": "Purchase",
            }] if item["reorder_level"] else [],
        })
        for price_list, flag, rate in (
                ("Standard Selling", "selling", _money(item["selling_price"])),
                ("Standard Buying", "buying", cost)):
            client.ensure("Item Price", {
                "item_code": item["code"], "price_list": price_list,
            }, {
                "doctype": "Item Price", "item_code": item["code"],
                "price_list": price_list, flag: 1,
                "currency": context.currency, "price_list_rate": rate,
            })
    return customer_names


def _opening_stock(client: ERPNextClient, context: ERPNextSeedContext,
                   dataset: Mapping[str, Any]) -> None:
    # A submitted transaction, so guarded rather than upserted. Captures run
    # this on a freshly reset site; the guard only spares a re-preparation
    # from stacking a second reconciliation onto the first.
    if client.find_one("Stock Reconciliation",
                       {"company": context.company, "docstatus": 1}) is not None:
        return
    reconciliation: dict[str, Any] = {
        "doctype": "Stock Reconciliation",
        "company": context.company,
        "purpose": "Stock Reconciliation",
        "posting_date": context.posting_date,
        "posting_time": context.posting_time,
        "items": [{
            "item_code": item["code"], "warehouse": context.warehouse,
            "qty": item["stock"], "valuation_rate": _money(item["cost"]),
        } for item in dataset["items"] if item["stock"]],
        "docstatus": 1,
    }
    if context.expense_account:
        reconciliation["expense_account"] = context.expense_account
    if context.cost_center:
        reconciliation["cost_center"] = context.cost_center
    client.create("Stock Reconciliation", reconciliation)


def _order_history(client: ERPNextClient, context: ERPNextSeedContext,
                   dataset: Mapping[str, Any],
                   customer_names: Mapping[str, str]) -> None:
    for order in dataset["sales_orders"]:
        # Submitted transactions are never upserted; the customer PO number
        # carries the Northwind order id as the existence key.
        if client.find_one("Sales Order", {"po_no": order["po_no"]}) is not None:
            continue
        client.create("Sales Order", {
            "doctype": "Sales Order", "company": context.company,
            "customer": customer_names[order["customer_code"]],
            "po_no": order["po_no"],
            "transaction_date": order["transaction_date"],
            "delivery_date": order["delivery_date"],
            "currency": context.currency,
            "selling_price_list": "Standard Selling",
            "items": [{
                "item_code": line["item_code"], "qty": line["qty"],
                "rate": _money(line["rate"]),
                "delivery_date": order["delivery_date"],
                # Submitting a stock item's order line requires its source
                # warehouse (erpnext WarehouseRequired).
                "warehouse": context.warehouse,
            } for line in order["lines"]],
            "docstatus": 1,
        })


def _purchase_invoice_matches(
    client: ERPNextClient,
    context: ERPNextSeedContext,
    matches: Mapping[str, Any],
) -> None:
    """Seed reusable PO/receipt/invoice neighborhoods for future tasks."""
    po_date = str(matches["purchase_order_date"])
    receipt_date = str(matches["receipt_date"])
    invoice_date = str(matches["invoice_date"])

    for chain in matches["chains"]:
        ref = str(chain["ref"])
        po_number = f"PO-3WM-{ref}"
        receipt_number = f"GR-3WM-{ref}"
        invoice_number = f"INV-3WM-{ref}"
        if client.find_one("Purchase Invoice", {"bill_no": invoice_number}) is not None:
            continue

        supplier = client.find_one(
            "Supplier", {"supplier_name": str(chain["supplier"])}
        )
        item = client.find_one("Item", {"item_code": str(chain["item_code"])})
        if supplier is None or item is None:
            raise RuntimeError(
                f"invoice-match chain {ref} references a missing supplier or item"
            )

        po_ref = None
        po_item_name = None
        if chain["po_qty"] is not None:
            po_ref = client.find_one(
                "Purchase Order", {"order_confirmation_no": po_number}
            )
            if po_ref is None:
                po_ref = client.create("Purchase Order", {
                    "doctype": "Purchase Order",
                    "supplier": supplier.name,
                    "order_confirmation_no": po_number,
                    "order_confirmation_date": po_date,
                    "company": context.company,
                    "transaction_date": po_date,
                    "schedule_date": receipt_date,
                    "currency": context.currency,
                    "items": [{
                        "item_code": item.name,
                        "qty": chain["po_qty"],
                        "rate": _money(chain["po_rate"]),
                        "warehouse": context.warehouse,
                        "schedule_date": receipt_date,
                        "uom": "Nos",
                        "stock_uom": "Nos",
                        "conversion_factor": 1,
                    }],
                    "docstatus": 1,
                })
            po_doc = client.get(po_ref)
            po_item_name = str(po_doc["items"][0]["name"])

        receipt_ref = None
        receipt_item_name = None
        if chain["received_qty"] is not None:
            if po_ref is None or po_item_name is None:
                raise RuntimeError(
                    f"invoice-match chain {ref} has a receipt without a PO"
                )
            receipt_ref = client.find_one(
                "Purchase Receipt", {"supplier_delivery_note": receipt_number}
            )
            if receipt_ref is None:
                receipt_ref = client.create("Purchase Receipt", {
                    "doctype": "Purchase Receipt",
                    "supplier": supplier.name,
                    "supplier_delivery_note": receipt_number,
                    "company": context.company,
                    "posting_date": receipt_date,
                    "posting_time": context.posting_time,
                    "items": [{
                        "item_code": item.name,
                        "qty": chain["received_qty"],
                        "rate": _money(chain["po_rate"]),
                        "warehouse": context.warehouse,
                        "uom": "Nos",
                        "stock_uom": "Nos",
                        "conversion_factor": 1,
                        "purchase_order": po_ref.name,
                        "purchase_order_item": po_item_name,
                    }],
                    "docstatus": 1,
                })
            receipt_doc = client.get(receipt_ref)
            receipt_item_name = str(receipt_doc["items"][0]["name"])

        invoice_item = {
            "item_code": item.name,
            "qty": chain["invoice_qty"],
            "rate": _money(chain["invoice_rate"]),
            "uom": "Nos",
            "stock_uom": "Nos",
            "conversion_factor": 1,
        }
        if context.expense_account:
            invoice_item["expense_account"] = context.expense_account
        if context.cost_center:
            invoice_item["cost_center"] = context.cost_center
        if po_ref is not None:
            invoice_item.update({
                "purchase_order": po_ref.name,
                "po_detail": po_item_name,
            })
        if receipt_ref is not None:
            invoice_item.update({
                "purchase_receipt": receipt_ref.name,
                "pr_detail": receipt_item_name,
            })

        client.create("Purchase Invoice", {
            "doctype": "Purchase Invoice",
            "supplier": supplier.name,
            "company": context.company,
            "posting_date": invoice_date,
            "bill_no": invoice_number,
            "bill_date": invoice_date,
            "due_date": invoice_date,
            "currency": context.currency,
            "items": [invoice_item],
        })


def _purchase_invoice_matches_allowing_fixture_variances(
    client: ERPNextClient,
    context: ERPNextSeedContext,
    matches: Mapping[str, Any],
) -> None:
    """Seed intentional rate variances without weakening the final snapshot.

    ERPNext's default buying settings reject a Purchase Invoice whose rate
    differs from its linked Purchase Order or Receipt. Several three-way-match
    examples deliberately exercise that condition, so downgrade the validation
    to a warning only while those fixtures are inserted and always restore the
    site's original setting afterward.
    """
    settings_ref = ResourceRef("Buying Settings", "Buying Settings")
    settings = client.get(settings_ref)
    original_action = settings.get("maintain_same_rate_action")
    should_relax = (
        bool(settings.get("maintain_same_rate")) and original_action == "Stop"
    )
    if should_relax:
        client.update(settings_ref, {"maintain_same_rate_action": "Warn"})
    try:
        _purchase_invoice_matches(client, context, matches)
    finally:
        if should_relax:
            client.update(
                settings_ref, {"maintain_same_rate_action": original_action}
            )


ENTERPRISE_MARKER_TITLE = "Enterprise ERP Operations Base v1"


def _seed_enterprise_workflows(
    client: ERPNextClient,
    context: ERPNextSeedContext,
    dataset: Mapping[str, Any],
    *,
    force: bool = False,
) -> None:
    """Seed ERPNext-owned workflow data only for capture preparation.

    Task execution never calls this function. Existing task snapshots and
    task-owned profile blocks therefore retain their exact record worlds.
    """
    if not force and os.environ.get("SHOWANDTELL_ENTERPRISE_DEMO_DATA", "1") == "0":
        return
    if not force and client.find_one("Note", {"title": ENTERPRISE_MARKER_TITLE}) is not None:
        return
    customers = _seed_renewal_and_receivables(client, context, dataset)
    _seed_maintenance_inventory(client, context, dataset, customers)
    client.ensure(
        "Note",
        {"title": ENTERPRISE_MARKER_TITLE},
        {
            "doctype": "Note",
            "title": ENTERPRISE_MARKER_TITLE,
            "public": 1,
            "content": (
                "Deterministic reusable ERPNext data for renewal, receivables, "
                "and preventive-maintenance workflows. "
                f"Business date: {dataset['metadata']['as_of_date']}."
            ),
        },
    )


def _seed_renewal_and_receivables(
    client: ERPNextClient,
    context: ERPNextSeedContext,
    dataset: Mapping[str, Any],
) -> dict[str, ResourceRef]:
    for dt, fields in {
        "Customer": (
            ("custom_showtell_account_code", "Account Code", "Data", "customer_name"),
            ("custom_showtell_domain", "Company Domain", "Data", "custom_showtell_account_code"),
        ),
        "Sales Invoice": (
            ("custom_showtell_reference", "Source Invoice Reference", "Data", "customer"),
            ("custom_showtell_scenario_role", "Scenario Role", "Data", "custom_showtell_reference"),
        ),
    }.items():
        for fieldname, label, fieldtype, insert_after in fields:
            _custom_field(
                client,
                dt=dt,
                fieldname=fieldname,
                label=label,
                fieldtype=fieldtype,
                insert_after=insert_after,
            )

    customers: dict[str, ResourceRef] = {}
    for row in dataset["crm"]["companies"]:
        account_code = str(row["account_code"])
        customer = client.ensure(
            "Customer",
            {"custom_showtell_account_code": account_code},
            {
                "doctype": "Customer",
                "customer_name": row["name"],
                "customer_type": "Company",
                "customer_group": "Commercial",
                "territory": "All Territories",
                "custom_showtell_account_code": account_code,
                "custom_showtell_domain": row["domain"],
            },
        )
        customers[account_code] = customer
        contact_email = f"renewals@{row['domain']}"
        first_name, *rest = str(row["name"]).split()
        client.ensure(
            "Contact",
            {"email_id": contact_email},
            {
                "doctype": "Contact",
                "first_name": first_name,
                "last_name": " ".join(rest) + " Renewal Contact",
                "is_primary_contact": 1,
                "email_ids": [{"email_id": contact_email, "is_primary": 1}],
                "links": [{"link_doctype": "Customer", "link_name": customer.name}],
            },
        )

    service = dataset["accounts_receivable"]["service_item"]
    income = client.find_one(
        "Account", {"company": context.company, "root_type": "Income", "is_group": 0}
    )
    receivable = client.find_one(
        "Account", {"company": context.company, "account_type": "Receivable", "is_group": 0}
    )
    bank = client.find_one(
        "Account", {"company": context.company, "account_type": "Bank", "is_group": 0}
    ) or client.find_one(
        "Account", {"company": context.company, "account_type": "Cash", "is_group": 0}
    )
    if income is None or receivable is None or bank is None or context.cost_center is None:
        raise RuntimeError("enterprise receivables need income, receivable, bank/cash, and cost-center masters")
    service_ref = client.ensure(
        "Item",
        {"item_code": service["item_code"]},
        {
            "doctype": "Item",
            "item_code": service["item_code"],
            "item_name": service["name"],
            "item_group": "Services",
            "stock_uom": "Nos",
            "is_stock_item": 0,
            "is_sales_item": 1,
            "is_purchase_item": 0,
        },
    )

    for row in dataset["accounts_receivable"]["invoices"]:
        reference = str(row["invoice_reference"])
        invoice = client.find_one(
            "Sales Invoice", {"custom_showtell_reference": reference}
        )
        if invoice is None:
            invoice = client.create(
                "Sales Invoice",
                {
                    "doctype": "Sales Invoice",
                    "company": context.company,
                    "customer": customers[str(row["account_code"])].name,
                    "posting_date": row["posting_date"],
                    "posting_time": "10:00:00",
                    "set_posting_time": 1,
                    "due_date": row["due_date"],
                    "currency": context.currency,
                    "selling_price_list": "Standard Selling",
                    "debit_to": receivable.name,
                    "custom_showtell_reference": reference,
                    "custom_showtell_scenario_role": row["scenario_role"],
                    "remarks": f"Reusable enterprise renewal fixture {reference}",
                    "items": [{
                        "item_code": service_ref.name,
                        "qty": 1,
                        "rate": _money(row["amount_usd"]),
                        "income_account": income.name,
                        "cost_center": context.cost_center,
                    }],
                    "docstatus": 1,
                },
            )
        paid = _money(row["paid_usd"])
        if paid <= 0:
            continue
        payment_reference = f"PAY-{reference}"
        if client.find_one("Payment Entry", {"reference_no": payment_reference}) is not None:
            continue
        client.create(
            "Payment Entry",
            {
                "doctype": "Payment Entry",
                "payment_type": "Receive",
                "posting_date": dataset["metadata"]["as_of_date"],
                "company": context.company,
                "party_type": "Customer",
                "party": customers[str(row["account_code"])].name,
                "paid_from": receivable.name,
                "paid_from_account_currency": context.currency,
                "paid_to": bank.name,
                "paid_to_account_currency": context.currency,
                "paid_amount": paid,
                "received_amount": paid,
                "source_exchange_rate": 1,
                "target_exchange_rate": 1,
                "reference_no": payment_reference,
                "reference_date": dataset["metadata"]["as_of_date"],
                "references": [{
                    "reference_doctype": "Sales Invoice",
                    "reference_name": invoice.name,
                    "allocated_amount": paid,
                }],
                "docstatus": 1,
            },
        )
    return customers


def _seed_maintenance_inventory(
    client: ERPNextClient,
    context: ERPNextSeedContext,
    dataset: Mapping[str, Any],
    customers: Mapping[str, ResourceRef],
) -> None:
    _custom_field(
        client,
        dt="Stock Reconciliation",
        fieldname="custom_showtell_reference",
        label="Source Reference",
        fieldtype="Data",
        insert_after="purpose",
    )
    _custom_field(
        client,
        dt="Item",
        fieldname="custom_showtell_vehicle_class",
        label="Vehicle Class",
        fieldtype="Data",
        insert_after="item_group",
    )
    group = str(dataset["maintenance_inventory"]["item_group"])
    client.ensure(
        "Item Group",
        {"item_group_name": group},
        {
            "doctype": "Item Group",
            "item_group_name": group,
            "parent_item_group": "All Item Groups",
            "is_group": 0,
        },
    )
    kits = dataset["maintenance_inventory"]["kits"]
    for row in kits:
        client.ensure(
            "Item",
            {"item_code": row["item_code"]},
            {
                "doctype": "Item",
                "item_code": row["item_code"],
                "item_name": row["name"],
                "item_group": group,
                "stock_uom": "Nos",
                "is_stock_item": 1,
                "standard_rate": _money(row["valuation_rate"]),
                "custom_showtell_vehicle_class": row["vehicle_class"],
            },
        )
    reconciliation_reference = "MNT-OPENING-2026"
    if client.find_one(
        "Stock Reconciliation", {"custom_showtell_reference": reconciliation_reference}
    ) is None:
        values: dict[str, Any] = {
            "doctype": "Stock Reconciliation",
            "company": context.company,
            "purpose": "Stock Reconciliation",
            "posting_date": dataset["metadata"]["as_of_date"],
            "posting_time": "07:30:00",
            "custom_showtell_reference": reconciliation_reference,
            "remarks": "Enterprise maintenance opening stock",
            "items": [
                {
                    "item_code": row["item_code"],
                    "warehouse": context.warehouse,
                    "qty": row["on_hand"],
                    "valuation_rate": _money(row["valuation_rate"]),
                }
                for row in kits
            ],
            "docstatus": 1,
        }
        if context.expense_account:
            values["expense_account"] = context.expense_account
        if context.cost_center:
            values["cost_center"] = context.cost_center
        client.create("Stock Reconciliation", values)

    reservation_customer = customers["ACCT-4104"]
    for row in kits:
        reserved = int(row["reserved"])
        if reserved == 0:
            continue
        po_no = f"MNT-RES-{row['item_code']}"
        if client.find_one("Sales Order", {"po_no": po_no}) is not None:
            continue
        client.create(
            "Sales Order",
            {
                "doctype": "Sales Order",
                "company": context.company,
                "customer": reservation_customer.name,
                "po_no": po_no,
                "transaction_date": dataset["metadata"]["as_of_date"],
                "delivery_date": "2026-08-10",
                "currency": context.currency,
                "selling_price_list": "Standard Selling",
                "items": [{
                    "item_code": row["item_code"],
                    "qty": reserved,
                    "rate": _money(row["valuation_rate"]),
                    "warehouse": context.warehouse,
                    "delivery_date": "2026-08-10",
                }],
                "docstatus": 1,
            },
        )
