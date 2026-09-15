"""Native ERPNext seeders for explicit application profile blocks.

Business records always use standard ERPNext/Frappe DocTypes. ``Custom Field``
records are limited to source semantics without a stock home, such as a literal
stock status, carrier-contract terms, structured PCN metadata, and the legacy
commitment totals that cannot be reconstructed from the supplied transactions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import partial
from typing import Any, Mapping, Sequence

from showAndTell.applications.erpnext.api import ERPNextClient, ResourceRef


@dataclass(frozen=True)
class ERPNextSeedContext:
    company: str
    warehouse: str
    posting_date: str = field(default_factory=lambda: date.today().isoformat())
    posting_time: str = "09:00:00"
    currency: str = "USD"
    expense_account: str | None = None
    cost_center: str | None = None


@dataclass(frozen=True)
class ProfileSeedResult:
    profile: str
    resources: tuple[ResourceRef, ...]
    aliases: Mapping[str, str]


def _money(value: Any) -> float:
    text = str(value or "0").replace("$", "").replace(",", "").strip()
    try:
        return float(Decimal(text))
    except InvalidOperation as exc:
        raise ValueError(f"invalid monetary value {value!r}") from exc


def _number(value: Any) -> float:
    text = str(value or "0").replace(",", "").strip()
    try:
        return float(Decimal(text))
    except InvalidOperation as exc:
        raise ValueError(f"invalid numeric value {value!r}") from exc


def _percent(value: Any) -> float:
    text = str(value or "0").replace("%", "").replace("-", "0").strip()
    return _number(text)


def _date(value: Any, *, day_first: bool = False) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("date value cannot be blank")
    formats = ["%Y-%m-%d"]
    formats.extend(
        ["%d/%m/%Y", "%d.%m.%Y", "%m/%d/%Y"] if day_first
        else ["%m/%d/%Y", "%d/%m/%Y", "%d.%m.%Y"]
    )
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"unsupported date value {value!r}")


def _ensure_fiscal_year(
    client: ERPNextClient, posting_date: str, context: ERPNextSeedContext
) -> ResourceRef:
    year = str(datetime.strptime(posting_date, "%Y-%m-%d").year)
    return client.ensure("Fiscal Year", {"name": year}, {
        "doctype": "Fiscal Year", "year": year,
        "year_start_date": f"{year}-01-01", "year_end_date": f"{year}-12-31",
        "disabled": 0, "companies": [{"company": context.company}],
    })


def _required(source: Mapping[str, Any], key: str, expected: type) -> Any:
    value = source.get(key)
    if not isinstance(value, expected):
        raise ValueError(f"ERPNext profile source {key!r} must be {expected.__name__}")
    return value


def _custom_field(client: ERPNextClient, *, dt: str, fieldname: str,
                  label: str, fieldtype: str = "Data", options: str = "",
                  insert_after: str = "", fetch_from: str = "",
                  read_only: bool = False) -> ResourceRef:
    # Frappe persists fieldnames as database column identifiers.  Keep them
    # lowercase: changing only the spelling/case in code makes restored
    # snapshots query a column that does not exist.
    if fieldname != fieldname.lower():
        raise ValueError("ERPNext custom fieldnames must be lowercase")
    values = {
        "doctype": "Custom Field",
        "dt": dt,
        "fieldname": fieldname,
        "label": label,
        "fieldtype": fieldtype,
        "options": options,
        "insert_after": insert_after,
        "fetch_from": fetch_from,
        "read_only": int(read_only),
        "in_list_view": 1,
        "in_standard_filter": 1,
    }
    return client.ensure("Custom Field", {"dt": dt, "fieldname": fieldname}, values)


def _users(client: ERPNextClient, users: Sequence[Mapping[str, Any]],
           roles: Sequence[str]) -> list[ResourceRef]:
    refs: list[ResourceRef] = []
    for user in users:
        email = str(user.get("email") or "").strip()
        if "@" not in email:
            # SAP-era seeds use a login name rather than an address. ERPNext's
            # User DocType requires an email-shaped name, so make the local
            # fixture identity explicit and deterministic.
            email = f"{email or 'operator'}@showAndTell.local"
        full_name = str(user.get("name") or email.split("@", 1)[0]).strip()
        first_name = full_name.split()[0]
        values = {
            "doctype": "User",
            "email": email,
            "first_name": first_name,
            "full_name": full_name,
            "enabled": 1,
            "send_welcome_email": 0,
            "new_password": str(user.get("password") or "showAndTell-demo"),
            "roles": [{"role": role} for role in roles],
        }
        refs.append(client.ensure("User", {"name": email}, values))
    return refs


def seed_inventory_demand(client: ERPNextClient, source: Mapping[str, Any],
                          context: ERPNextSeedContext) -> ProfileSeedResult:
    categories = _required(source, "categories", list)
    products = _required(source, "products", list)
    users = _required(source, "users", list)
    resources: list[ResourceRef] = []
    aliases: dict[str, str] = {}

    resources.append(_custom_field(
        client, dt="Item", fieldname="custom_showtell_stock_status",
        label="Stock Status", fieldtype="Select",
        options="In Stock\nLow Stock\nOut of Stock", insert_after="item_group"))
    resources.extend(_users(client, users, ("Stock User",)))

    for category in categories:
        name = str(category).strip()
        resources.append(client.ensure(
            "Item Group", {"item_group_name": name}, {
                "doctype": "Item Group", "item_group_name": name,
                "parent_item_group": "All Item Groups", "is_group": 0,
            }))

    stock_rows: list[dict[str, Any]] = []
    for product in products:
        sku = str(product["sku"])
        status = str(product["status"])
        item = client.ensure("Item", {"item_code": sku}, {
            "doctype": "Item",
            "item_code": sku,
            "item_name": str(product["name"]),
            "item_group": str(product["category"]),
            "stock_uom": "Nos",
            "is_stock_item": 1,
            "standard_rate": _money(product.get("price")),
            "custom_showtell_stock_status": status,
            # This keeps the task meaningful in native stock reports too. The
            # literal status remains a custom field because ERPNext does not
            # expose a filterable Low Stock enum on Item.
            "reorder_levels": [{
                "warehouse": context.warehouse,
                "warehouse_group": context.warehouse,
                "warehouse_reorder_level": 10 if status == "Low Stock" else 0,
                "warehouse_reorder_qty": 20 if status == "Low Stock" else 0,
                "material_request_type": "Purchase",
            }],
        })
        resources.append(item)
        aliases[sku] = item.name
        stock_rows.append({
            "item_code": item.name,
            "warehouse": context.warehouse,
            "qty": product["stock"],
            "valuation_rate": _money(product.get("price")),
        })

    reconciliation: dict[str, Any] = {
        "doctype": "Stock Reconciliation",
        "company": context.company,
        "purpose": "Stock Reconciliation",
        "posting_date": context.posting_date,
        "posting_time": context.posting_time,
        "items": stock_rows,
        "docstatus": 1,
    }
    if context.expense_account:
        reconciliation["expense_account"] = context.expense_account
    if context.cost_center:
        reconciliation["cost_center"] = context.cost_center
    resources.append(client.create("Stock Reconciliation", reconciliation))
    return ProfileSeedResult("inventory_demand", tuple(resources), aliases)


def seed_three_way_match(client: ERPNextClient, source: Mapping[str, Any],
                         context: ERPNextSeedContext) -> ProfileSeedResult:
    users = _required(source, "users", list)
    suppliers = _required(source, "suppliers", list)
    purchase_orders = _required(source, "purchase_orders", list)
    receipts = _required(source, "grns", list)
    invoices = _required(source, "invoices", list)
    resources: list[ResourceRef] = []
    aliases: dict[str, str] = {}

    for dt in ("Purchase Order", "Purchase Receipt", "Purchase Invoice"):
        resources.append(_custom_field(
            client, dt=dt, fieldname="custom_showtell_reference",
            label="ShowAndTell Reference", insert_after="supplier"))
    resources.extend([
        _custom_field(
            client, dt="Purchase Invoice", fieldname="custom_showtell_review_status",
            label="Review Status", fieldtype="Select",
            options="Pending\nApproved\nHold", insert_after="status"),
        _custom_field(
            client, dt="Purchase Invoice", fieldname="custom_showtell_reason",
            label="Review Reason", fieldtype="Data",
            insert_after="custom_showtell_review_status"),
        _custom_field(
            client, dt="Purchase Invoice", fieldname="custom_showtell_comment",
            label="Review Comment", fieldtype="Small Text",
            insert_after="custom_showtell_reason"),
    ])
    resources.extend(_users(client, users, ("Purchase User", "Accounts User")))
    resources.append(client.ensure(
        "Item Group", {"item_group_name": "ShowAndTell Parts"}, {
            "doctype": "Item Group", "item_group_name": "ShowAndTell Parts",
            "parent_item_group": "All Item Groups", "is_group": 0,
        }))

    supplier_refs: dict[str, ResourceRef] = {}
    for supplier in suppliers:
        source_id = str(supplier["id"])
        name = str(supplier["name"])
        ref = client.ensure("Supplier", {"supplier_name": name}, {
            "doctype": "Supplier", "supplier_name": name,
            "supplier_group": "All Supplier Groups", "supplier_type": "Company",
        })
        resources.append(ref)
        supplier_refs[source_id] = ref
        aliases[source_id] = ref.name

    skus = {
        str(line["sku"])
        for collection in (purchase_orders, invoices)
        for record in collection
        for line in record.get("lines", [])
    }
    for sku in sorted(skus):
        ref = client.ensure("Item", {"item_code": sku}, {
            "doctype": "Item", "item_code": sku, "item_name": sku,
            "item_group": "ShowAndTell Parts", "stock_uom": "Nos", "is_stock_item": 1,
        })
        resources.append(ref)
        aliases[sku] = ref.name

    po_refs: dict[str, ResourceRef] = {}
    po_lines: dict[tuple[str, str], str] = {}
    for po in purchase_orders:
        source_id = str(po["id"])
        supplier = supplier_refs[str(po["supplier_id"])]
        payload = {
            "doctype": "Purchase Order", "supplier": supplier.name,
            "company": context.company, "transaction_date": context.posting_date,
            "schedule_date": context.posting_date, "currency": context.currency,
            "custom_showtell_reference": source_id, "docstatus": 1,
            "items": [{
                "item_code": aliases[str(line["sku"])], "qty": line["qty"],
                "rate": line["unit_price"], "warehouse": context.warehouse,
                "schedule_date": context.posting_date,
            } for line in po.get("lines", [])],
        }
        ref = client.create("Purchase Order", payload)
        resources.append(ref)
        po_refs[source_id] = ref
        aliases[source_id] = ref.name
        stored = client.get(ref)
        for row in stored.get("items", []):
            if isinstance(row, Mapping) and row.get("item_code") and row.get("name"):
                po_lines[(source_id, str(row["item_code"]))] = str(row["name"])

    receipt_refs: dict[str, ResourceRef] = {}
    receipt_lines: dict[tuple[str, str], str] = {}
    for receipt in receipts:
        source_id = str(receipt["id"])
        po_id = str(receipt["po_id"])
        po_ref = po_refs[po_id]
        po_source = next(po for po in purchase_orders if str(po["id"]) == po_id)
        supplier = supplier_refs[str(po_source["supplier_id"])]
        items = []
        for line in receipt.get("lines", []):
            item_code = aliases[str(line["sku"])]
            po_line = next(row for row in po_source["lines"] if str(row["sku"]) == str(line["sku"]))
            items.append({
                "item_code": item_code, "qty": line["qty_received"],
                "rate": po_line["unit_price"], "warehouse": context.warehouse,
                "purchase_order": po_ref.name,
                "purchase_order_item": po_lines[(po_id, item_code)],
            })
        payload = {
            "doctype": "Purchase Receipt", "supplier": supplier.name,
            "company": context.company, "posting_date": context.posting_date,
            "posting_time": context.posting_time,
            "custom_showtell_reference": source_id, "items": items, "docstatus": 1,
        }
        ref = client.create("Purchase Receipt", payload)
        resources.append(ref)
        receipt_refs[po_id] = ref
        aliases[source_id] = ref.name
        stored = client.get(ref)
        for row in stored.get("items", []):
            if isinstance(row, Mapping) and row.get("item_code") and row.get("name"):
                receipt_lines[(po_id, str(row["item_code"]))] = str(row["name"])

    for invoice in invoices:
        source_id = str(invoice["id"])
        po_id = str(invoice["po_id"])
        po_ref = po_refs[po_id]
        receipt_ref = receipt_refs[po_id]
        supplier = supplier_refs[str(invoice["supplier_id"])]
        items = []
        for line in invoice.get("lines", []):
            item_code = aliases[str(line["sku"])]
            items.append({
                "item_code": item_code, "qty": line["qty"],
                "rate": line["unit_price"], "expense_account": context.expense_account,
                "cost_center": context.cost_center,
                "purchase_order": po_ref.name,
                "po_detail": po_lines[(po_id, item_code)],
                "purchase_receipt": receipt_ref.name,
                "pr_detail": receipt_lines[(po_id, item_code)],
            })
        # Omit optional accounts when the target site derives them from company
        # and item defaults.
        for item in items:
            if not item["expense_account"]:
                item.pop("expense_account")
            if not item["cost_center"]:
                item.pop("cost_center")
        payload = {
            "doctype": "Purchase Invoice", "supplier": supplier.name,
            "company": context.company, "posting_date": context.posting_date,
            "bill_no": source_id, "bill_date": context.posting_date,
            "due_date": context.posting_date,
            "currency": context.currency, "custom_showtell_reference": source_id,
            "custom_showtell_review_status": "Pending", "items": items,
        }
        ref = client.create("Purchase Invoice", payload)
        resources.append(ref)
        aliases[source_id] = ref.name

    return ProfileSeedResult("three_way_match", tuple(resources), aliases)


def _item_group(client: ERPNextClient, name: str) -> ResourceRef:
    return client.ensure("Item Group", {"item_group_name": name}, {
        "doctype": "Item Group", "item_group_name": name,
        "parent_item_group": "All Item Groups", "is_group": 0,
    })


def _warehouse(client: ERPNextClient, *, identifier: str,
               context: ERPNextSeedContext) -> ResourceRef:
    return client.ensure("Warehouse", {
        "warehouse_name": identifier, "company": context.company,
    }, {
        "doctype": "Warehouse", "warehouse_name": identifier,
        "company": context.company, "is_group": 0,
    })


def seed_carrier_contracts(client: ERPNextClient, source: Mapping[str, Any],
                           context: ERPNextSeedContext) -> ProfileSeedResult:
    """Represent carrier contracts as native supplier quotations.

    ERPNext has no first-class carrier-rate contract. Supplier and Supplier
    Quotation preserve the commercial relationship and base rate, while five
    quotation custom fields retain the FSC/minimum/discount/source semantics.
    """
    users = _required(source, "users", list)
    carriers = _required(source, "carriers", list)
    resources: list[ResourceRef] = []
    aliases: dict[str, str] = {}
    resources.extend(_users(client, users, ("Purchase User",)))
    resources.append(_item_group(client, "Freight Services"))
    service_item = client.ensure("Item", {"item_code": "FREIGHT-CWT"}, {
        "doctype": "Item", "item_code": "FREIGHT-CWT",
        "item_name": "Freight service per hundredweight",
        "item_group": "Freight Services", "stock_uom": "Nos",
        "is_stock_item": 0,
    })
    resources.append(service_item)
    for fieldname, label, fieldtype in (
        ("custom_showtell_reference", "ShowAndTell Reference", "Data"),
        ("custom_showtell_fsc_percent", "Fuel Surcharge %", "Percent"),
        ("custom_showtell_minimum_charge", "Minimum Charge", "Currency"),
        ("custom_showtell_discount_percent", "Discount %", "Percent"),
        ("custom_showtell_notes", "Contract Notes", "Small Text"),
    ):
        resources.append(_custom_field(
            client, dt="Supplier Quotation", fieldname=fieldname, label=label,
            fieldtype=fieldtype, insert_after="supplier"))

    fiscal_years: set[str] = set()
    for carrier in carriers:
        source_id = str(carrier["id"])
        name = str(carrier["name"])
        supplier = client.ensure("Supplier", {"supplier_name": name}, {
            "doctype": "Supplier", "supplier_name": name,
            "supplier_group": "All Supplier Groups", "supplier_type": "Company",
        })
        resources.append(supplier)
        effective = _date(carrier["effective_date"], day_first=True)
        year = effective[:4]
        if year not in fiscal_years:
            resources.append(_ensure_fiscal_year(client, effective, context))
            fiscal_years.add(year)
        quotation = client.create("Supplier Quotation", {
            "doctype": "Supplier Quotation", "supplier": supplier.name,
            "company": context.company, "transaction_date": effective,
            "currency": context.currency,
            "custom_showtell_reference": source_id,
            "custom_showtell_fsc_percent": _percent(carrier.get("fsc_pct")),
            "custom_showtell_minimum_charge": _money(carrier.get("min_charge")),
            "custom_showtell_discount_percent": _percent(carrier.get("discount_pct")),
            "custom_showtell_notes": str(carrier.get("notes") or ""),
            "items": [{
                "item_code": service_item.name, "qty": 1,
                "uom": "Nos", "rate": _money(carrier.get("base_rate_cwt")),
            }],
        })
        resources.append(quotation)
        aliases[source_id] = quotation.name
    return ProfileSeedResult("carrier_contracts", tuple(resources), aliases)


def seed_pcn_material_master(client: ERPNextClient, source: Mapping[str, Any],
                             context: ERPNextSeedContext) -> ProfileSeedResult:
    users = _required(source, "users", list)
    materials = _required(source, "materials", list)
    documents = _required(source, "pcn_documents", list)
    resources: list[ResourceRef] = []
    aliases: dict[str, str] = {}
    resources.extend(_users(client, users, ("Stock User", "Purchase User")))
    resources.append(_item_group(client, "PCN Materials"))
    resources.append(client.ensure("UOM", {"uom_name": "EA"}, {
        "doctype": "UOM", "uom_name": "EA", "must_be_whole_number": 1,
    }))

    for fieldname, label, fieldtype in (
        ("custom_showtell_material_type", "Source Material Type", "Data"),
        ("custom_showtell_old_material_number", "Old Material Number", "Data"),
        ("custom_showtell_purchasing_group", "Purchasing Group", "Data"),
        ("custom_showtell_valuation_class", "Valuation Class", "Data"),
        ("custom_showtell_price_control", "Price Control", "Data"),
        ("custom_showtell_pcn_number", "PCN Number", "Data"),
    ):
        resources.append(_custom_field(
            client, dt="Item", fieldname=fieldname, label=label,
            fieldtype=fieldtype, insert_after="item_group"))
    # The SAP-era organizational level and native manufacturer mapping are
    # review context, not editable Item attributes. Keep both read-only and
    # visible together on ERPNext's Purchasing tab.
    resources.append(_custom_field(
        client, dt="Item", fieldname="custom_showtell_plant",
        label="Source Plant", fieldtype="Data",
        insert_after="purchase_uom", read_only=True))
    resources.append(_custom_field(
        client, dt="Item", fieldname="custom_showtell_manufacturer_part_no",
        label="Manufacturer Part Number", fieldtype="Data",
        insert_after="custom_showtell_plant", read_only=True))
    for fieldname, label, fieldtype in (
        ("custom_showtell_pcn_number", "PCN Number", "Data"),
        ("custom_showtell_manufacturer", "Manufacturer", "Data"),
        ("custom_showtell_supplier_part", "Supplier Part", "Data"),
        ("custom_showtell_change_type", "Change Type", "Data"),
        ("custom_showtell_category", "PCN Category", "Data"),
        ("custom_showtell_disposition", "Disposition", "Small Text"),
        ("custom_showtell_issue_date", "Issue Date", "Date"),
        ("custom_showtell_effective_date", "Effective Date", "Date"),
        ("custom_showtell_ltb_date", "Last Time Buy Date", "Date"),
        ("custom_showtell_lts_date", "Last Time Ship Date", "Date"),
    ):
        resources.append(_custom_field(
            client, dt="Note", fieldname=fieldname, label=label,
            fieldtype=fieldtype, insert_after="title"))

    pcn_by_part = {str(doc.get("supplier_part")): doc for doc in documents}
    for material in materials:
        code = str(material["material"])
        pcn = pcn_by_part.get(str(material.get("mfr_part_number")))
        manufacturer_name = str(pcn.get("manufacturer") if pcn else "").strip()
        manufacturer = None
        if manufacturer_name:
            manufacturer = client.ensure(
                "Manufacturer", {"short_name": manufacturer_name}, {
                    "doctype": "Manufacturer", "short_name": manufacturer_name,
                    "full_name": manufacturer_name,
                })
            resources.append(manufacturer)
        plant_id = str(material.get("plant") or "").strip()
        plant_warehouse = None
        if plant_id:
            plant_warehouse = _warehouse(
                client, identifier=plant_id, context=context)
            resources.append(plant_warehouse)
        item = client.ensure("Item", {"item_code": code}, {
            "doctype": "Item", "item_code": code,
            "item_name": str(material["description"]),
            "item_group": "PCN Materials", "stock_uom": "EA",
            "is_stock_item": 1, "standard_rate": _money(material.get("price")),
            "valuation_method": "Moving Average",
            "custom_showtell_old_material_number": str(
                material.get("old_material_number") or ""
            ),
            "custom_showtell_manufacturer_part_no": str(
                material.get("mfr_part_number") or ""
            ),
            "custom_showtell_material_type": str(material.get("material_type") or ""),
            "custom_showtell_plant": str(material.get("plant") or ""),
            "custom_showtell_purchasing_group": str(material.get("purchasing_group") or ""),
            "custom_showtell_valuation_class": str(material.get("valuation_class") or ""),
            "custom_showtell_price_control": str(material.get("price_control") or ""),
            "custom_showtell_pcn_number": str(pcn.get("pcn_number") if pcn else ""),
            "item_defaults": ([{
                "company": context.company,
                "default_warehouse": plant_warehouse.name,
            }] if plant_warehouse else []),
        })
        resources.append(item)
        aliases[code] = item.name
        if manufacturer:
            resources.append(client.ensure("Item Manufacturer", {
                "item_code": item.name, "manufacturer": manufacturer.name,
                "manufacturer_part_no": str(material.get("mfr_part_number") or ""),
            }, {
                "doctype": "Item Manufacturer", "item_code": item.name,
                "manufacturer": manufacturer.name,
                "manufacturer_part_no": str(material.get("mfr_part_number") or ""),
                "is_default": 1,
            }))

    for document in documents:
        source_id = str(document["id"])
        content = "\n\n".join(filter(None, (
            str(document.get("description_body") or ""),
            str(document.get("recommendation") or ""),
        )))
        note = client.ensure("Note", {"custom_showtell_pcn_number": source_id}, {
            "doctype": "Note", "title": str(document.get("title") or source_id),
            "content": content, "public": 1,
            "custom_showtell_pcn_number": str(document.get("pcn_number") or source_id),
            "custom_showtell_manufacturer": str(document.get("manufacturer") or ""),
            "custom_showtell_supplier_part": str(document.get("supplier_part") or ""),
            "custom_showtell_change_type": str(document.get("change_type") or ""),
            "custom_showtell_category": str(document.get("category") or ""),
            "custom_showtell_disposition": str(document.get("disposition") or ""),
            "custom_showtell_issue_date": _date(document["issue_date"]),
            "custom_showtell_effective_date": _date(document["effective_date"]),
            "custom_showtell_ltb_date": _date(document["ltb_date"]),
            "custom_showtell_lts_date": _date(document["lts_date"]),
        })
        resources.append(note)
        aliases[source_id] = note.name
    return ProfileSeedResult("pcn_material_master", tuple(resources), aliases)


def seed_purchase_requisition(client: ERPNextClient, source: Mapping[str, Any],
                              context: ERPNextSeedContext) -> ProfileSeedResult:
    users = _required(source, "users", list)
    materials = _required(source, "materials", list)
    plants = _required(source, "plants", list)
    _required(source, "pr", dict)
    resources: list[ResourceRef] = []
    aliases: dict[str, str] = {}
    resources.extend(_users(client, users, ("Stock User", "Purchase User")))
    resources.append(_item_group(client, "Purchase Requisition Materials"))
    resources.append(client.ensure("UOM", {"uom_name": "EA"}, {
        "doctype": "UOM", "uom_name": "EA", "must_be_whole_number": 1,
    }))
    for plant in plants:
        plant_id = str(plant["id"])
        ref = _warehouse(client, identifier=plant_id, context=context)
        resources.append(ref)
        aliases[plant_id] = ref.name
        for location in plant.get("storage_locations", []):
            location_id = str(location["id"])
            location_ref = _warehouse(
                client, identifier=location_id, context=context)
            resources.append(location_ref)
            aliases[location_id] = location_ref.name
    for material in materials:
        code = str(material["number"])
        item = client.ensure("Item", {"item_code": code}, {
            "doctype": "Item", "item_code": code,
            "item_name": str(material["short_text"]),
            "item_group": "Purchase Requisition Materials",
            "stock_uom": str(material.get("unit") or "EA"), "is_stock_item": 1,
            "standard_rate": _money(material.get("price")),
        })
        resources.append(item)
        aliases[code] = item.name
        if str(material.get("price") or "").strip():
            resources.append(client.ensure("Item Price", {
                "item_code": item.name, "price_list": "Standard Buying",
            }, {
                "doctype": "Item Price", "item_code": item.name,
                "price_list": "Standard Buying", "buying": 1,
                "currency": str(material.get("currency") or context.currency),
                "price_list_rate": _money(material["price"]),
            }))
    # No Material Request is inserted: creating it is the demonstrated action.
    return ProfileSeedResult("purchase_requisition", tuple(resources), aliases)


def _sales_masters(client: ERPNextClient, source: Mapping[str, Any],
                   context: ERPNextSeedContext,
                   resources: list[ResourceRef], aliases: dict[str, str]) -> None:
    sales = _required(source, "sales", dict)
    resources.append(_item_group(client, "ShowAndTell Sales Items"))
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
            resources.append(_custom_field(
                client, dt=dt, fieldname=fieldname, label=label,
                fieldtype=fieldtype,
                insert_after="customer_name" if dt == "Customer" else "item_group"))
    for fieldname, label in (
        ("custom_showtell_pcr", "PCR Cost"),
        ("custom_showtell_ptr", "PTR"),
        ("custom_showtell_pcip", "PCIP"),
    ):
        resources.append(_custom_field(
            client,
            dt="Sales Order Item",
            fieldname=fieldname,
            label=label,
            fieldtype="Currency",
            insert_after="gross_profit",
            fetch_from=f"item_code.{fieldname}",
            read_only=True,
        ))
    for customer in sales.get("sold_tos", []):
        source_id = str(customer["number"])
        ref = client.ensure("Customer", {"custom_showtell_reference": source_id}, {
            "doctype": "Customer", "customer_name": str(customer["name"]),
            "customer_type": "Company", "customer_group": "Commercial",
            "territory": "United States",
            "custom_showtell_reference": source_id,
            "custom_showtell_business_partner": str(customer.get("business_partner") or ""),
            "custom_showtell_search_term": str(customer.get("search_term") or ""),
        })
        resources.append(ref)
        aliases[source_id] = ref.name
        shipping = client.ensure("Address", {
            "address_title": str(customer["name"]),
            "address_type": "Shipping",
        }, {
            "doctype": "Address",
            "address_title": str(customer["name"]),
            "address_type": "Shipping",
            "address_line1": str(customer.get("name") or source_id),
            "city": str(customer.get("city") or "New York"),
            "country": "United States",
            "pincode": str(customer.get("postal_code") or "10001"),
            "links": [{"link_doctype": "Customer", "link_name": ref.name}],
            "is_shipping_address": 1,
        })
        resources.append(shipping)
        aliases[f"shipping:{source_id}"] = shipping.name
    for material in sales.get("materials", []):
        code = str(material["number"])
        rate = _money(material.get("internal_price"))
        cost = _money(material.get("pcip") or material.get("pcr") or rate)
        item = client.ensure("Item", {"item_code": code}, {
            "doctype": "Item", "item_code": code,
            "item_name": str(material["description"]),
            "item_group": "ShowAndTell Sales Items", "stock_uom": "Nos",
            "is_stock_item": 1, "standard_rate": cost,
            "custom_showtell_pcr": _money(material.get("pcr")),
            "custom_showtell_ptr": _money(material.get("ptr")),
            "custom_showtell_pcip": _money(material.get("pcip") or material.get("pcr")),
        })
        resources.append(item)
        aliases[code] = item.name
        resources.append(client.ensure("Item Price", {
            "item_code": item.name, "price_list": "Standard Selling",
        }, {
            "doctype": "Item Price", "item_code": item.name,
            "price_list": "Standard Selling", "selling": 1,
            "currency": context.currency, "price_list_rate": rate,
        }))


def seed_sales_order(client: ERPNextClient, source: Mapping[str, Any],
                     context: ERPNextSeedContext, *,
                     profile: str = "sales_order") -> ProfileSeedResult:
    users = _required(source, "users", list)
    resources: list[ResourceRef] = []
    aliases: dict[str, str] = {}
    resources.extend(_users(client, users, ("Sales User",)))
    _sales_masters(client, source, context, resources, aliases)
    # The Sales Order is intentionally absent: creating it is the task action.
    return ProfileSeedResult(profile, tuple(resources), aliases)


def seed_purchase_order_history(client: ERPNextClient, source: Mapping[str, Any],
                                context: ERPNextSeedContext) -> ProfileSeedResult:
    users = _required(source, "users", list)
    purchase_orders = _required(source, "purchase_orders", list)
    resources: list[ResourceRef] = []
    aliases: dict[str, str] = {}
    resources.extend(_users(client, users, ("Purchase User", "Stock User")))
    resources.append(_item_group(client, "Purchase History Materials"))
    resources.extend([
        _custom_field(client, dt="Purchase Order",
                      fieldname="custom_showtell_reference",
                      label="Source Purchase Order", insert_after="supplier"),
        _custom_field(client, dt="Purchase Order",
                      fieldname="custom_showtell_created_on",
                      label="Source Created Date", fieldtype="Date",
                      insert_after="transaction_date"),
        _custom_field(client, dt="Purchase Order Item",
                      fieldname="custom_showtell_source_net_price",
                      label="Source Net Order Price", fieldtype="Currency",
                      insert_after="rate", read_only=True),
        _custom_field(client, dt="Purchase Order Item",
                      fieldname="custom_showtell_source_price_unit",
                      label="Source Price Unit", fieldtype="Float",
                      insert_after="custom_showtell_source_net_price",
                      read_only=True),
        _custom_field(client, dt="Purchase Receipt",
                      fieldname="custom_showtell_reference",
                      label="Source Goods Receipt", insert_after="supplier"),
        _custom_field(client, dt="Purchase Receipt",
                      fieldname="custom_showtell_movement",
                      label="Source Movement", insert_after="posting_date"),
    ])
    suppliers: dict[str, ResourceRef] = {}
    items: dict[str, ResourceRef] = {}
    warehouses: dict[str, ResourceRef] = {}
    for po in purchase_orders:
        vendor = str(po["vendor"])
        if vendor not in suppliers:
            suppliers[vendor] = client.ensure("Supplier", {"supplier_name": vendor}, {
                "doctype": "Supplier", "supplier_name": vendor,
                "supplier_group": "All Supplier Groups", "supplier_type": "Company",
            })
            resources.append(suppliers[vendor])
        code = str(po["material"])
        if code not in items:
            items[code] = client.ensure("Item", {"item_code": code}, {
                "doctype": "Item", "item_code": code,
                "item_name": str(po["description"]),
                "item_group": "Purchase History Materials", "stock_uom": "Nos",
                "is_stock_item": 1,
            })
            resources.append(items[code])
            aliases[code] = items[code].name
        for gr in po.get("gr_docs", []):
            location = str(gr["storage_location"])
            if location not in warehouses:
                warehouses[location] = _warehouse(
                    client, identifier=location, context=context)
                resources.append(warehouses[location])

    for po in purchase_orders:
        source_id = str(po["number"])
        code = str(po["material"])
        schedule = _date(po["delivery_date"], day_first=True)
        source_created = _date(po["created_on"], day_first=True)
        transaction = min(schedule, source_created)
        unit_rate = _money(po["net_price"]) / max(_number(po.get("price_unit")), 1)
        location = str(po.get("gr_docs", [{}])[0].get("storage_location")
                       or context.warehouse)
        warehouse = warehouses.get(location)
        warehouse_name = warehouse.name if warehouse else context.warehouse
        ref = client.create("Purchase Order", {
            "doctype": "Purchase Order", "supplier": suppliers[str(po["vendor"])].name,
            "company": context.company, "transaction_date": transaction,
            "schedule_date": schedule, "currency": str(po.get("currency") or context.currency),
            "custom_showtell_reference": source_id,
            "custom_showtell_created_on": source_created, "docstatus": 1,
            "items": [{
                "item_code": items[code].name, "qty": po["order_qty"],
                "rate": unit_rate, "warehouse": warehouse_name,
                "schedule_date": schedule,
                "custom_showtell_source_net_price": _money(po["net_price"]),
                "custom_showtell_source_price_unit": _number(
                    po.get("price_unit") or 1
                ),
            }],
        })
        resources.append(ref)
        aliases[source_id] = ref.name
        stored = client.get(ref)
        po_line = str(stored["items"][0]["name"])
        for gr in po.get("gr_docs", []):
            gr_id = str(gr["number"])
            gr_warehouse = warehouses[str(gr["storage_location"])].name
            receipt = client.create("Purchase Receipt", {
                "doctype": "Purchase Receipt",
                "supplier": suppliers[str(po["vendor"])].name,
                "company": context.company, "posting_date": context.posting_date,
                "posting_time": context.posting_time,
                "custom_showtell_reference": gr_id,
                "custom_showtell_movement": str(gr.get("movement") or ""),
                "docstatus": 1,
                "items": [{
                    "item_code": items[code].name, "qty": gr["qty_received"],
                    "rate": unit_rate, "warehouse": gr_warehouse,
                    "purchase_order": ref.name, "purchase_order_item": po_line,
                }],
            })
            resources.append(receipt)
            aliases[gr_id] = receipt.name
    return ProfileSeedResult("purchase_order_history", tuple(resources), aliases)


def seed_stock_substitution(client: ERPNextClient, source: Mapping[str, Any],
                            context: ERPNextSeedContext) -> ProfileSeedResult:
    users = _required(source, "users", list)
    stock = _required(source, "stock", list)
    sales = _required(source, "sales", dict)
    resources: list[ResourceRef] = []
    aliases: dict[str, str] = {}
    resources.extend(_users(client, users, ("Sales User", "Stock User")))
    _sales_masters(client, source, context, resources, aliases)
    resources.extend([
        _custom_field(client, dt="Item",
                      fieldname="custom_showtell_sales_orders_qty",
                      label="Source Sales Orders Qty", fieldtype="Float",
                      insert_after="item_group"),
        _custom_field(client, dt="Item",
                      fieldname="custom_showtell_scheduled_delivery_qty",
                      label="Source Scheduled Delivery Qty", fieldtype="Float",
                      insert_after="custom_showtell_sales_orders_qty"),
        _custom_field(client, dt="Sales Order",
                      fieldname="custom_showtell_reference",
                      label="Source Sales Order", insert_after="customer"),
    ])
    warehouse_by_key: dict[tuple[str, str], ResourceRef] = {}
    stock_rows: list[dict[str, Any]] = []
    for row in stock:
        key = (str(row["plant"]), str(row["storage_location"]))
        warehouse = _warehouse(client, identifier=key[1], context=context)
        resources.append(warehouse)
        warehouse_by_key[key] = warehouse
        aliases[str(row["plant"])] = warehouse.name
        aliases[str(row["storage_location"])] = warehouse.name
        item = ResourceRef("Item", aliases[str(row["material"])])
        client.update(item, {
            "custom_showtell_sales_orders_qty": _number(row.get("sales_orders")),
            "custom_showtell_scheduled_delivery_qty": _number(row.get("sched_delivery")),
        })
        stock_rows.append({
            "item_code": item.name, "warehouse": warehouse.name,
            "qty": _number(row.get("unrestricted")),
            "valuation_rate": _money(next(
                (m.get("pcr") for m in sales.get("materials", [])
                 if str(m.get("number")) == str(row["material"])), 0)),
        })
    if stock_rows:
        reconciliation: dict[str, Any] = {
            "doctype": "Stock Reconciliation", "company": context.company,
            "purpose": "Stock Reconciliation", "posting_date": context.posting_date,
            "posting_time": context.posting_time, "items": stock_rows, "docstatus": 1,
        }
        # On a freshly reinstalled site this is an opening stock entry. ERPNext
        # therefore requires the baseline's Asset/Liability difference account
        # and cost center; an already-used site can mask this validation.
        if context.expense_account:
            reconciliation["expense_account"] = context.expense_account
        if context.cost_center:
            reconciliation["cost_center"] = context.cost_center
        resources.append(client.create("Stock Reconciliation", reconciliation))
    material_data = {str(row["number"]): row for row in sales.get("materials", [])}
    for order in sales.get("orders", []):
        source_id = str(order["number"])
        customer = aliases[str(order["sold_to"])]
        material = str(order["material"])
        rate = next((
            _money(condition.get("amount")) for condition in order.get("conditions", [])
            if str(condition.get("name")) in {"PCR", "PCIP"}
        ), _money(material_data.get(material, {}).get("internal_price")))
        matching_stock = next((row for row in stock
                               if str(row["material"]) == material), None)
        warehouse = (
            warehouse_by_key[(str(matching_stock["plant"]),
                              str(matching_stock["storage_location"]))].name
            if matching_stock else context.warehouse
        )
        ref = client.create("Sales Order", {
            "doctype": "Sales Order", "customer": customer,
            "company": context.company, "transaction_date": context.posting_date,
            "delivery_date": context.posting_date, "order_type": "Sales",
            "currency": context.currency, "custom_showtell_reference": source_id,
            "docstatus": 1,
            "items": [{
                "item_code": aliases[material], "qty": _number(order["quantity"]),
                "rate": rate, "warehouse": warehouse,
                "delivery_date": context.posting_date,
                "custom_showtell_pcr": _money(material_data[material].get("pcr")),
                "custom_showtell_ptr": _money(material_data[material].get("ptr")),
                "custom_showtell_pcip": _money(
                    material_data[material].get("pcip")
                    or material_data[material].get("pcr")
                ),
            }],
        })
        resources.append(ref)
        aliases[source_id] = ref.name
    return ProfileSeedResult("stock_substitution", tuple(resources), aliases)


# Every profile a seed block may name, beside the seeders it dispatches to, so
# adding one is a single entry rather than a new rung in a conditional ladder.
_SEEDERS = {
    "inventory_demand": seed_inventory_demand,
    "three_way_match": seed_three_way_match,
    "carrier_contracts": seed_carrier_contracts,
    "pcn_material_master": seed_pcn_material_master,
    "purchase_requisition": seed_purchase_requisition,
    "sales_order": seed_sales_order,
    "purchase_order_history": seed_purchase_order_history,
    "email_sales_order": partial(seed_sales_order, profile="email_sales_order"),
    "stock_substitution": seed_stock_substitution,
}


def seed_profile(client: ERPNextClient, block: Mapping[str, Any],
                 context: ERPNextSeedContext) -> ProfileSeedResult:
    if block.get("schema_version") != 1:
        raise ValueError("unsupported ERPNext seed schema")
    source = block.get("source")
    if not isinstance(source, Mapping):
        raise ValueError("ERPNext seed block needs an object source")
    profile = block.get("profile")
    seeder = _SEEDERS.get(profile) if isinstance(profile, str) else None
    if seeder is None:
        raise NotImplementedError(f"ERPNext profile {profile!r} is not implemented")
    return seeder(client, source, context)
