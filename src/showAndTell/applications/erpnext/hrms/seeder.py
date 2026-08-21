"""Translate the recruiting task seed into native Frappe HR resources."""
from __future__ import annotations

from typing import Any, Mapping

from showAndTell.applications.erpnext.api import ERPNextClient, ResourceRef


def seed_frappe_hr(client: ERPNextClient, block: Mapping[str, Any]) -> list[ResourceRef]:
    """Upsert configuration and requisitions by stable external ID."""
    resources: list[ResourceRef] = []
    # A new non-interactive ERPNext site has not run the setup wizard. Company
    # insertion still creates a Goods In Transit warehouse linked to this
    # standard master, so establish the same prerequisite explicitly.
    resources.append(client.ensure(
        "Warehouse Type",
        {"name": "Transit"},
        {"doctype": "Warehouse Type", "name": "Transit"},
    ))
    company = block["company"]
    company_ref = client.ensure("Company", {"company_name": company["name"]}, {
        "doctype": "Company", "company_name": company["name"], "abbr": company["abbr"],
        "country": company["country"], "default_currency": company["default_currency"],
    })
    resources.append(company_ref)
    operator = block["operator"]
    user_ref = client.ensure("User", {"email": operator["email"]}, {
        "doctype": "User", "email": operator["email"],
        "first_name": operator["full_name"], "enabled": 1,
        "send_welcome_email": 0, "roles": [{"role": role} for role in operator["roles"]],
    })
    resources.append(user_ref)
    for field in block.get("custom_fields", []):
        values = {"doctype": "Custom Field", "dt": field["doctype"],
                  **{key: value for key, value in field.items() if key != "doctype"}}
        resources.append(client.ensure("Custom Field", {
            "dt": values["dt"], "fieldname": values["fieldname"]}, values))
    rows = list(block.get("job_requisitions", []))
    if not rows:
        return resources
    designations: dict[str, ResourceRef] = {}
    departments: dict[str, ResourceRef] = {}
    for row in rows:
        title = str(row["designation"])
        if title not in designations:
            designations[title] = client.ensure("Designation", {"designation_name": title}, {
                "doctype": "Designation", "designation_name": title,
                "description": row["job_description"] or title,
            })
            resources.append(designations[title])
        department = str(row["department"])
        if department not in departments:
            departments[department] = client.ensure("Department", {
                "department_name": department, "company": company_ref.name}, {
                "doctype": "Department", "department_name": department,
                "company": company_ref.name,
            })
            resources.append(departments[department])
    # Gender is another setup-wizard master absent from a headless new site,
    # while current HRMS makes both it and date_of_birth mandatory on Employee.
    gender_ref = client.ensure(
        "Gender",
        {"name": "Unspecified"},
        {"doctype": "Gender", "gender": "Unspecified"},
    )
    resources.append(gender_ref)
    first = rows[0]
    employee_ref = client.ensure("Employee", {"user_id": operator["email"]}, {
        "doctype": "Employee", "first_name": operator["full_name"],
        "user_id": user_ref.name, "company": company_ref.name, "status": "Active",
        "date_of_joining": "2026-01-01", "date_of_birth": "1990-01-01",
        "gender": gender_ref.name, "designation": first["designation"],
        "department": departments[str(first["department"])].name,
    })
    resources.append(employee_ref)
    requesters: dict[str, ResourceRef] = {}
    for row in rows:
        external_id = str(row["external_id"])
        requester_number = f"SHOWANDTELL-REQ-{external_id}"
        requesters[external_id] = client.ensure(
            "Employee",
            {"employee_number": requester_number},
            {
                "doctype": "Employee",
                "employee_number": requester_number,
                "first_name": row["requested_by_name"] or "Unspecified Requester",
                "company": company_ref.name,
                "status": "Active",
                "date_of_joining": "2026-01-01",
                "date_of_birth": "1990-01-01",
                "gender": gender_ref.name,
                "designation": row["designation"],
                "department": departments[str(row["department"])].name,
            },
        )
        resources.append(requesters[external_id])
    for row in rows:
        external_id = str(row["external_id"])
        payload = {
            "doctype": "Job Requisition",
            "designation": designations[str(row["designation"])].name,
            "department": departments[str(row["department"])].name,
            "company": company_ref.name,
            "status": row["status"],
            "requested_by": requesters[external_id].name,
            "no_of_positions": 1,
            "expected_compensation": row["expected_compensation"],
            "posting_date": row["posting_date"],
            "expected_by": row["expected_by"],
            "description": row["job_description"] or row["designation"],
            "custom_showtell_external_id": external_id,
            "custom_showtell_location": row["location"],
            "custom_showtell_requested_by_name": row["requested_by_name"],
            "custom_showtell_position_number": row["position_number"],
        }
        resources.append(client.ensure(
            "Job Requisition", {"custom_showtell_external_id": row["external_id"]}, payload))
    return resources


def reset_frappe_hr(client: ERPNextClient, block: Mapping[str, Any]) -> None:
    """Delete only task-owned requisitions; shared config remains reusable."""
    for row in reversed(block.get("job_requisitions", [])):
        ref = client.find_one(
            "Job Requisition", {"custom_showtell_external_id": row["external_id"]})
        if ref is not None:
            client.delete(ref)
