from __future__ import annotations

from collections import defaultdict

from showAndTell.applications.erpnext.api import ResourceRef
from showAndTell.applications.erpnext.demo_dataset import (
    _purchase_invoice_matches,
    _purchase_invoice_matches_allowing_fixture_variances,
    load_demo_dataset,
)
from showAndTell.applications.erpnext.profiles import ERPNextSeedContext


CONTEXT = ERPNextSeedContext(
    company="ShowAndTell Manufacturing",
    warehouse="Stores - STM",
    currency="USD",
    expense_account="Opening Stock - STM",
    cost_center="Main - STM",
)


class RecordingClient:
    def __init__(self, dataset):
        self.records = defaultdict(list)
        self.created = []
        self.counter = defaultdict(int)
        self.records["Supplier"] = [
            {"name": row["name"], "supplier_name": row["name"]}
            for row in dataset["suppliers"]
        ]
        self.records["Item"] = [
            {"name": row["code"], "item_code": row["code"]}
            for row in dataset["items"]
        ]

    def find_one(self, doctype, filters):
        for record in self.records[doctype]:
            if all(record.get(key) == value for key, value in filters.items()):
                return ResourceRef(doctype, record["name"])
        return None

    def create(self, doctype, values):
        self.counter[doctype] += 1
        name = f"{doctype.upper().replace(' ', '-')}-{self.counter[doctype]:04d}"
        record = {**values, "name": name}
        if "items" in record:
            record["items"] = [
                {**item, "name": f"{name}-ITEM-{index}"}
                for index, item in enumerate(record["items"], 1)
            ]
        self.records[doctype].append(record)
        self.created.append(doctype)
        return ResourceRef(doctype, name)

    def get(self, ref):
        return next(
            record for record in self.records[ref.doctype]
            if record["name"] == ref.name
        )

    def update(self, ref, values):
        record = self.get(ref)
        record.update(values)


def test_invoice_match_seed_data_uses_existing_masters_and_exact_boundaries():
    dataset = load_demo_dataset()
    matches = dataset["purchase_invoice_matches"]
    chains = {row["ref"]: row for row in matches["chains"]}

    assert len(dataset["suppliers"]) == 29
    assert len(dataset["items"]) == 77
    assert len(chains) == 12
    assert chains["I"]["po_rate"] == "6.20"
    assert chains["I"]["invoice_rate"] == "6.51"
    assert chains["J"]["po_qty"] == 100
    assert chains["J"]["received_qty"] == 93
    assert chains["K"]["po_qty"] is None
    assert chains["L"]["received_qty"] is None

    client = RecordingClient(dataset)
    _purchase_invoice_matches(client, CONTEXT, matches)

    assert client.created.count("Purchase Order") == 11
    assert client.created.count("Purchase Receipt") == 10
    assert client.created.count("Purchase Invoice") == 12
    assert "Supplier" not in client.created
    assert "Item" not in client.created


def test_invoice_match_transactions_have_dates_statuses_and_line_links():
    dataset = load_demo_dataset()
    matches = dataset["purchase_invoice_matches"]
    client = RecordingClient(dataset)
    _purchase_invoice_matches(client, CONTEXT, matches)

    purchase_orders = client.records["Purchase Order"]
    receipts = client.records["Purchase Receipt"]
    invoices = client.records["Purchase Invoice"]
    assert {row["transaction_date"] for row in purchase_orders} == {"2026-07-10"}
    assert {row["posting_date"] for row in receipts} == {"2026-07-24"}
    assert {row["posting_date"] for row in invoices} == {"2026-07-28"}
    assert all(row["docstatus"] == 1 for row in purchase_orders + receipts)
    assert all("docstatus" not in row for row in invoices)

    po_a = next(
        row for row in purchase_orders
        if row["order_confirmation_no"] == "PO-3WM-A"
    )
    receipt_a = next(
        row for row in receipts if row["supplier_delivery_note"] == "GR-3WM-A"
    )
    invoice_a = next(row for row in invoices if row["bill_no"] == "INV-3WM-A")
    assert receipt_a["items"][0]["purchase_order"] == po_a["name"]
    assert receipt_a["items"][0]["purchase_order_item"] == po_a["items"][0]["name"]
    assert invoice_a["items"][0]["purchase_order"] == po_a["name"]
    assert invoice_a["items"][0]["purchase_receipt"] == receipt_a["name"]

    invoice_k = next(row for row in invoices if row["bill_no"] == "INV-3WM-K")
    invoice_l = next(row for row in invoices if row["bill_no"] == "INV-3WM-L")
    assert "purchase_order" not in invoice_k["items"][0]
    assert invoice_l["items"][0]["purchase_order"]
    assert "purchase_receipt" not in invoice_l["items"][0]


def test_invoice_match_transaction_seed_is_idempotent():
    dataset = load_demo_dataset()
    matches = dataset["purchase_invoice_matches"]
    client = RecordingClient(dataset)
    _purchase_invoice_matches(client, CONTEXT, matches)
    counts = {doctype: len(rows) for doctype, rows in client.records.items()}

    _purchase_invoice_matches(client, CONTEXT, matches)

    assert {doctype: len(rows) for doctype, rows in client.records.items()} == counts


def test_invoice_match_seed_temporarily_relaxes_and_restores_rate_validation():
    dataset = load_demo_dataset()
    client = RecordingClient(dataset)
    client.records["Buying Settings"] = [{
        "name": "Buying Settings",
        "maintain_same_rate": 1,
        "maintain_same_rate_action": "Stop",
    }]

    _purchase_invoice_matches_allowing_fixture_variances(
        client, CONTEXT, dataset["purchase_invoice_matches"]
    )

    settings = client.records["Buying Settings"][0]
    assert settings["maintain_same_rate_action"] == "Stop"
    assert client.created.count("Purchase Invoice") == 12
