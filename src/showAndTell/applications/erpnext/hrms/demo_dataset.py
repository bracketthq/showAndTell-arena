"""Seed a bounded, relational workforce into first-time Frappe HR sites.

The vendored data is a deterministic 100-employee subset of Doug Trajano's
Apache-2.0 ``HR Synthetic Database`` plus a compact synthetic recruitment
pipeline.  The upstream 15,700 employee corpus is excellent source material
but too large to replay through Frappe's REST API on first use.  This module
keeps every source job, every department and every business unit, maps them
onto native Frappe HR records, then links job openings, applicants, interviews,
and offers to those masters. The same fixture owns reusable expense-audit and
payroll-cutoff records linked to that workforce.

The last job offer marks the workforce/recruitment section; a separate Note
marks the operational workflow section. An older golden snapshot can therefore
backfill only the missing section, while a bare site receives the full
idempotent seed. Set
``SHOWANDTELL_FRAPPE_HR_DEMO_DATA=0`` to prepare or seed a deliberately bare site.
"""
from __future__ import annotations

import base64
import json
import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import UUID

from showAndTell.applications.erpnext.api import ERPNextClient, ResourceRef
from showAndTell.applications.erpnext.profiles import (
    ERPNextSeedContext,
    _custom_field,
    _money,
    _users,
)


DATASET_PATH = Path(__file__).with_name("demo_dataset.json")

_EMPLOYEE_CUSTOM_FIELDS = (
    ("custom_showtell_source_id", "Source Employee ID", "Data",
     "employee_number", None, 1),
    ("custom_showtell_job_level", "Job Level", "Data",
     "custom_showtell_source_id", None, 0),
    ("custom_showtell_job_family", "Job Family", "Data",
     "custom_showtell_job_level", None, 0),
    ("custom_showtell_contract_type", "Contract Type", "Data",
     "custom_showtell_job_family", None, 0),
    ("custom_showtell_workplace_type", "Workplace Type", "Data",
     "custom_showtell_contract_type", None, 0),
    ("custom_showtell_ethnicity", "Synthetic Ethnicity", "Data",
     "custom_showtell_workplace_type", None, 0),
    ("custom_showtell_generation", "Generation", "Data",
     "custom_showtell_ethnicity", None, 0),
    ("custom_showtell_annual_base_salary", "Annual Base Salary", "Currency",
     "ctc", "salary_currency", 0),
    ("custom_showtell_annual_bonus", "Annual Bonus", "Currency",
     "custom_showtell_annual_base_salary", "salary_currency", 0),
    ("custom_showtell_annual_commission", "Annual Commission", "Currency",
     "custom_showtell_annual_bonus", "salary_currency", 0),
    ("custom_showtell_rate_type", "Compensation Rate Type", "Data",
     "custom_showtell_annual_commission", None, 0),
)


def load_demo_dataset() -> dict[str, Any]:
    dataset = json.loads(DATASET_PATH.read_text())
    _validate_dataset(dataset)
    _validate_enterprise_workflows(
        dataset["enterprise_workflows"], dataset["employees"]
    )
    return dataset


def _validate_dataset(dataset: Mapping[str, Any]) -> None:
    units = {row["id"] for row in dataset["business_units"]}
    departments = {row["id"] for row in dataset["departments"]}
    jobs = {row["id"] for row in dataset["jobs"]}
    employees: set[str] = set()
    for row in dataset["departments"]:
        if row["business_unit_id"] not in units or row["manager_job_id"] not in jobs:
            raise ValueError(f"invalid department relationship: {row['id']}")
    for row in dataset["employees"]:
        if row["id"] in employees:
            raise ValueError(f"duplicate source employee id: {row['id']}")
        if row["business_unit_id"] not in units or row["job_id"] not in jobs:
            raise ValueError(f"invalid employee relationship: {row['id']}")
        department = row["department_id"]
        if department not in (None, "None") and department not in departments:
            raise ValueError(f"invalid employee department: {row['id']}")
        manager = row.get("reports_to_source_id")
        if manager and manager not in employees:
            raise ValueError(f"manager must precede employee: {row['id']}")
        employees.add(row["id"])

    recruitment = dataset["recruitment"]
    source_names = {row["name"] for row in recruitment["sources"]}
    skill_names = {row["name"] for row in recruitment["skills"]}
    interview_types = {row["name"] for row in recruitment["interview_types"]}
    opening_ids = {row["id"] for row in recruitment["job_openings"]}
    applicant_ids = {row["id"] for row in recruitment["job_applicants"]}
    if not recruitment["job_offers"]:
        raise ValueError("recruitment demo dataset needs a final Job Offer marker")
    if len(opening_ids) != len(recruitment["job_openings"]):
        raise ValueError("duplicate recruitment job opening id")
    if len(applicant_ids) != len(recruitment["job_applicants"]):
        raise ValueError("duplicate recruitment job applicant id")
    for row in recruitment["interview_types"]:
        if row["job_id"] not in jobs or not set(row["skills"]) <= skill_names:
            raise ValueError(f"invalid interview type relationship: {row['name']}")
    for row in recruitment["job_openings"]:
        if (row["job_id"] not in jobs or row["department_id"] not in departments
                or row["business_unit_id"] not in units):
            raise ValueError(f"invalid job opening relationship: {row['id']}")
    for row in recruitment["job_applicants"]:
        if row["opening_id"] not in opening_ids or row["source"] not in source_names:
            raise ValueError(f"invalid job applicant relationship: {row['id']}")
    for row in recruitment["interviews"]:
        if (row["applicant_id"] not in applicant_ids
                or row["interview_type"] not in interview_types):
            raise ValueError(f"invalid interview relationship: {row['id']}")
    for row in recruitment["job_offers"]:
        if row["applicant_id"] not in applicant_ids:
            raise ValueError(f"invalid job offer relationship: {row['id']}")


def _validate_enterprise_workflows(
    dataset: Mapping[str, Any],
    workforce: list[Mapping[str, Any]],
) -> None:
    if set(dataset) != {"metadata", "expenses", "payroll"}:
        raise ValueError("Frappe HR enterprise fixture has unsupported sections")
    metadata = dataset["metadata"]
    if metadata.get("dataset_id") != "enterprise-operations" or metadata.get("version") != 1:
        raise ValueError("Frappe HR enterprise fixture identity/version is unsupported")

    workforce_ids = {str(row["id"]) for row in workforce}
    expenses = dataset["expenses"]
    claims = expenses["claims"]
    claim_refs = {str(row["claim_reference"]) for row in claims}
    if len(claims) != 6 or len(claim_refs) != len(claims):
        raise ValueError("Frappe HR enterprise fixture needs six unique claims")
    for row in claims:
        employee_id = str(row["employee_source_id"])
        UUID(employee_id)
        if employee_id not in workforce_ids:
            raise ValueError(f"expense references unknown employee: {row['claim_reference']}")
        date.fromisoformat(str(row["expense_date"]))
        if Decimal(str(row["amount_usd"])) <= 0:
            raise ValueError(f"expense amount must be positive: {row['claim_reference']}")
    boundary = next(
        row for row in claims if row["claim_reference"] == "ECLM-26002"
    )
    if Decimal(str(boundary["amount_usd"])) != Decimal(
        str(expenses["receipt_threshold_usd"])
    ):
        raise ValueError("Frappe HR expense fixture lost its receipt threshold")

    payroll = dataset["payroll"]
    cases = payroll["cases"]
    case_refs = {str(row["case_reference"]) for row in cases}
    shift_names = {str(row["name"]) for row in payroll["shift_types"]}
    if len(cases) != 6 or len(case_refs) != len(cases):
        raise ValueError("Frappe HR enterprise fixture needs six unique payroll cases")
    case_employee_ids = {str(row["employee_source_id"]) for row in cases}
    for row in cases:
        employee_id = str(row["employee_source_id"])
        UUID(employee_id)
        if employee_id not in workforce_ids or row["shift"] not in shift_names:
            raise ValueError(f"invalid payroll case: {row['case_reference']}")
    for row in payroll["checkins"]:
        if (
            row["case_reference"] not in case_refs
            or str(row["employee_source_id"]) not in case_employee_ids
            or row["log_type"] not in {"IN", "OUT"}
        ):
            raise ValueError("payroll checkin has a broken relationship")
        datetime.fromisoformat(str(row["time"]))
    date.fromisoformat(str(payroll["period_start"]))
    date.fromisoformat(str(payroll["period_end"]))
    datetime.fromisoformat(str(payroll["cutoff_at"]))


def seed_demo_dataset(
    client: ERPNextClient,
    context: ERPNextSeedContext,
    *,
    force: bool = False,
) -> None:
    if not force and os.environ.get("SHOWANDTELL_FRAPPE_HR_DEMO_DATA", "1") == "0":
        return
    dataset = load_demo_dataset()
    employees = dataset["employees"]
    if not employees:
        return
    recruitment = dataset["recruitment"]
    applicants_by_id = {
        row["id"]: row for row in recruitment["job_applicants"]
    }
    marker_offer = recruitment["job_offers"][-1]
    marker_email = applicants_by_id[marker_offer["applicant_id"]]["email"]
    if not force and client.find_one(
            "Job Offer", {"applicant_email": marker_email}) is not None:
        _seed_enterprise_workflows(
            client, context, dataset["enterprise_workflows"],
            workforce=employees, force=force,
        )
        return

    _ensure_employee_fields(client)
    genders = sorted({str(row["gender"]) for row in employees})
    for gender in genders:
        client.ensure(
            "Gender",
            {"name": gender},
            {"doctype": "Gender", "gender": gender},
        )

    branches: dict[str, ResourceRef] = {}
    for row in dataset["business_units"]:
        branches[row["id"]] = client.ensure(
            "Branch",
            {"branch": row["name"]},
            {"doctype": "Branch", "branch": row["name"]},
        )

    departments: dict[str, ResourceRef] = {}
    for row in dataset["departments"]:
        departments[row["id"]] = client.ensure(
            "Department",
            {"department_name": row["frappe_name"], "company": context.company},
            {
                "doctype": "Department",
                "department_name": row["frappe_name"],
                "company": context.company,
                "is_group": 0,
            },
        )

    jobs: dict[str, ResourceRef] = {}
    job_rows: dict[str, Mapping[str, Any]] = {}
    for row in dataset["jobs"]:
        job_rows[row["id"]] = row
        jobs[row["id"]] = client.ensure(
            "Designation",
            {"designation_name": row["frappe_name"]},
            {
                "doctype": "Designation",
                "designation_name": row["frappe_name"],
                "description": row["description"],
            },
        )

    employee_refs: dict[str, ResourceRef] = {}
    for row in employees:
        job = job_rows[row["job_id"]]
        department_id = row["department_id"]
        values: dict[str, Any] = {
            "doctype": "Employee",
            "employee_number": row["employee_number"],
            "first_name": row["first_name"],
            "last_name": row["last_name"],
            "company": context.company,
            "status": "Active",
            "gender": row["gender"],
            "date_of_birth": row["birth_date"],
            "date_of_joining": row["date_of_joining"],
            "designation": jobs[row["job_id"]].name,
            "branch": branches[row["business_unit_id"]].name,
            "company_email": row["company_email"],
            "attendance_device_id": row["attendance_device_id"],
            "salary_currency": context.currency,
            "ctc": row["total_compensation"],
            "bio": job["description"],
            "education": [{
                "qualification": row["education_level"],
                "level": _education_level(row["education_level"]),
                "maj_opt_subj": row["education_field"],
            }],
            "custom_showtell_source_id": row["id"],
            "custom_showtell_job_level": job["job_level"],
            "custom_showtell_job_family": job["job_family"],
            "custom_showtell_contract_type": job["contract_type"],
            "custom_showtell_workplace_type": job["workplace_type"],
            "custom_showtell_ethnicity": row["ethnicity"],
            "custom_showtell_generation": row["generation"],
            "custom_showtell_annual_base_salary": row["annual_base_salary"],
            "custom_showtell_annual_bonus": row["annual_bonus_amount"],
            "custom_showtell_annual_commission": row["annual_commission_amount"],
            "custom_showtell_rate_type": row["rate_type"],
        }
        if department_id not in (None, "None"):
            values["department"] = departments[department_id].name
        manager = row.get("reports_to_source_id")
        if manager:
            values["reports_to"] = employee_refs[manager].name
        employee_refs[row["id"]] = client.ensure(
            "Employee",
            {"custom_showtell_source_id": row["id"]},
            values,
        )

    _seed_recruitment(
        client,
        context,
        recruitment,
        jobs=jobs,
        job_rows=job_rows,
        departments=departments,
        branches=branches,
    )
    _seed_enterprise_workflows(
        client, context, dataset["enterprise_workflows"],
        workforce=employees, force=force,
    )


def _seed_recruitment(
    client: ERPNextClient,
    context: ERPNextSeedContext,
    recruitment: Mapping[str, Any],
    *,
    jobs: Mapping[str, ResourceRef],
    job_rows: Mapping[str, Mapping[str, Any]],
    departments: Mapping[str, ResourceRef],
    branches: Mapping[str, ResourceRef],
) -> None:
    sources: dict[str, ResourceRef] = {}
    for row in recruitment["sources"]:
        sources[row["name"]] = client.ensure(
            "Job Applicant Source",
            {"source_name": row["name"]},
            {
                "doctype": "Job Applicant Source",
                "source_name": row["name"],
                "details": row["details"],
            },
        )

    skills: dict[str, ResourceRef] = {}
    for row in recruitment["skills"]:
        skills[row["name"]] = client.ensure(
            "Skill",
            {"skill_name": row["name"]},
            {
                "doctype": "Skill",
                "skill_name": row["name"],
                "description": row["description"],
            },
        )

    interview_types: dict[str, ResourceRef] = {}
    for row in recruitment["interview_types"]:
        interview_types[row["name"]] = client.ensure(
            "Interview Type",
            {"interview_type_name": row["name"]},
            {
                "doctype": "Interview Type",
                "interview_type_name": row["name"],
                "designation": jobs[row["job_id"]].name,
                "expected_average_rating": row["expected_average_rating"],
                "expected_skill_set": [
                    {
                        "skill": skills[name].name,
                        "description": next(
                            skill["description"] for skill in recruitment["skills"]
                            if skill["name"] == name
                        ),
                    }
                    for name in row["skills"]
                ],
                "description": row["description"],
            },
        )

    employment_types: dict[str, ResourceRef] = {}
    for row in recruitment["job_openings"]:
        name = str(job_rows[row["job_id"]]["contract_type"])
        if name in employment_types:
            continue
        employment_types[name] = client.ensure(
            "Employment Type",
            {"employee_type_name": name},
            {"doctype": "Employment Type", "employee_type_name": name},
        )

    openings: dict[str, ResourceRef] = {}
    opening_rows: dict[str, Mapping[str, Any]] = {}
    for row in recruitment["job_openings"]:
        opening_rows[row["id"]] = row
        job = job_rows[row["job_id"]]
        employment_type = employment_types[str(job["contract_type"])]
        openings[row["id"]] = client.ensure(
            "Job Opening",
            {"job_title": row["title"]},
            {
                "doctype": "Job Opening",
                "job_title": row["title"],
                "designation": jobs[row["job_id"]].name,
                "status": "Open",
                "posted_on": (
                    f"{_relative_date(context.posting_date, -row['posted_days_ago'])} "
                    "09:00:00"
                ),
                "closes_on": _relative_date(
                    context.posting_date, row["closes_in_days"]
                ),
                "company": context.company,
                "department": departments[row["department_id"]].name,
                "employment_type": employment_type.name,
                "location": branches[row["business_unit_id"]].name,
                "vacancies": row["vacancies"],
                "description": row["description"],
                "currency": context.currency,
                "lower_range": row["lower_range"],
                "upper_range": row["upper_range"],
                "salary_per": "Year",
            },
        )

    applicants: dict[str, ResourceRef] = {}
    applicant_rows: dict[str, Mapping[str, Any]] = {}
    for row in recruitment["job_applicants"]:
        applicant_rows[row["id"]] = row
        opening = opening_rows[row["opening_id"]]
        applicants[row["id"]] = client.ensure(
            "Job Applicant",
            {"email_id": row["email"]},
            {
                "doctype": "Job Applicant",
                "applicant_name": row["name"],
                "email_id": row["email"],
                "phone_number": row["phone"],
                "job_title": openings[row["opening_id"]].name,
                "designation": jobs[opening["job_id"]].name,
                "country": "United States",
                "status": row["status"],
                "applicant_rating": row["rating"],
                "cover_letter": row["cover_letter"],
                "notes": row["notes"],
                "source": sources[row["source"]].name,
                "currency": context.currency,
                "lower_range": opening["lower_range"],
                "upper_range": opening["upper_range"],
            },
        )

    for row in recruitment["interviews"]:
        applicant = applicant_rows[row["applicant_id"]]
        opening = opening_rows[applicant["opening_id"]]
        scheduled_on = _relative_date(context.posting_date, row["scheduled_day_offset"])
        client.ensure(
            "Interview",
            {
                "job_applicant": applicants[row["applicant_id"]].name,
                "interview_type": interview_types[row["interview_type"]].name,
                "scheduled_on": scheduled_on,
            },
            {
                "doctype": "Interview",
                "interview_type": interview_types[row["interview_type"]].name,
                "job_applicant": applicants[row["applicant_id"]].name,
                "job_opening": openings[applicant["opening_id"]].name,
                "designation": jobs[opening["job_id"]].name,
                "status": row["status"],
                "scheduled_on": scheduled_on,
                "from_time": row["from_time"],
                "to_time": row["to_time"],
                "interview_summary": row["summary"],
            },
        )

    for row in recruitment["job_offers"]:
        applicant = applicant_rows[row["applicant_id"]]
        opening = opening_rows[applicant["opening_id"]]
        client.ensure(
            "Job Offer",
            {"job_applicant": applicants[row["applicant_id"]].name},
            {
                "doctype": "Job Offer",
                "job_applicant": applicants[row["applicant_id"]].name,
                "applicant_name": applicant["name"],
                "applicant_email": applicant["email"],
                "status": row["status"],
                "offer_date": _relative_date(
                    context.posting_date, row["offer_day_offset"]
                ),
                "designation": jobs[opening["job_id"]].name,
                "company": context.company,
                "terms": row["terms"],
            },
        )


def _relative_date(anchor: str, days: int) -> str:
    return (date.fromisoformat(anchor) + timedelta(days=int(days))).isoformat()


def _ensure_employee_fields(client: ERPNextClient) -> None:
    for row in _EMPLOYEE_CUSTOM_FIELDS:
        fieldname, label, fieldtype, insert_after, options, unique = row
        values: dict[str, Any] = {
            "doctype": "Custom Field",
            "dt": "Employee",
            "fieldname": fieldname,
            "label": label,
            "fieldtype": fieldtype,
            "insert_after": insert_after,
            "in_standard_filter": 1,
        }
        if options:
            values["options"] = options
        if unique:
            values["unique"] = 1
        client.ensure(
            "Custom Field",
            {"dt": "Employee", "fieldname": fieldname},
            values,
        )


def _education_level(value: str) -> str:
    lowered = value.lower()
    if "master" in lowered or "doctor" in lowered or "post" in lowered:
        return "Post Graduate"
    if "bachelor" in lowered or "graduate" in lowered:
        return "Graduate"
    return "Under Graduate"


ENTERPRISE_MARKER_TITLE = "Enterprise HR Operations Base v1"


def _seed_enterprise_workflows(
    client: ERPNextClient,
    context: ERPNextSeedContext,
    workflows: Mapping[str, Any],
    *,
    workforce: Sequence[Mapping[str, Any]],
    force: bool = False,
) -> None:
    """Seed Frappe HR-owned workflow data only for capture preparation.

    ``workflows`` is the dataset's ``enterprise_workflows`` block; it carries
    no employee table, so ``workforce`` must supply the top-level employee
    rows its expense and payroll records reference.
    """
    if not force and os.environ.get("SHOWANDTELL_ENTERPRISE_DEMO_DATA", "1") == "0":
        return
    if not force and client.find_one("Note", {"title": ENTERPRISE_MARKER_TITLE}) is not None:
        return
    employees = _employee_refs(client, workflows, workforce)
    _seed_expenses(client, context, workflows, employees)
    _seed_payroll(client, context, workflows, employees)
    client.ensure(
        "Note",
        {"title": ENTERPRISE_MARKER_TITLE},
        {
            "doctype": "Note",
            "title": ENTERPRISE_MARKER_TITLE,
            "public": 1,
            "content": (
                "Deterministic reusable Frappe HR data for expense audit and "
                "payroll cutoff workflows. "
                f"Business date: {workflows['metadata']['as_of_date']}."
            ),
        },
    )


def _employee_refs(
    client: ERPNextClient,
    workflows: Mapping[str, Any],
    workforce: Sequence[Mapping[str, Any]],
) -> dict[str, ResourceRef]:
    source_ids = {
        row["employee_source_id"] for row in workflows["expenses"]["claims"]
    } | {
        row["employee_source_id"] for row in workflows["payroll"]["cases"]
    }
    refs: dict[str, ResourceRef] = {}
    attendance_ids = {
        row["id"]: row["attendance_device_id"]
        for row in workforce
    }
    employees_by_device = {
        str(row.get("attendance_device_id")): ResourceRef("Employee", str(row["name"]))
        for row in client.list_all(
            "Employee", fields=("name", "attendance_device_id"), page_length=200
        )
        if row.get("name") and row.get("attendance_device_id")
    }
    for source_id in source_ids:
        device_id = attendance_ids.get(source_id)
        ref = employees_by_device.get(str(device_id)) if device_id else None
        if ref is None:
            raise RuntimeError(f"enterprise workflow references missing employee {source_id}")
        refs[str(source_id)] = ref
    return refs


def _seed_expenses(
    client: ERPNextClient,
    context: ERPNextSeedContext,
    workflows: Mapping[str, Any],
    employees: Mapping[str, ResourceRef],
) -> None:
    for fieldname, label, fieldtype, options in (
        ("custom_showtell_reference", "Claim Reference", "Data", ""),
        ("custom_showtell_workflow_state", "Review Queue State", "Select", "Draft\nSubmitted\nReviewed"),
        ("custom_showtell_receipt_type", "Receipt Evidence", "Select", "itemized\ntotal-only\nmissing"),
        ("custom_showtell_card_transaction", "Card Transaction", "Data", ""),
        ("custom_showtell_manager_approval", "Manager Approval", "Select", "approved\nmissing\nrejected"),
        ("custom_showtell_scenario_role", "Scenario Role", "Data", ""),
    ):
        _custom_field(
            client,
            dt="Expense Claim",
            fieldname=fieldname,
            label=label,
            fieldtype=fieldtype,
            options=options,
            insert_after="expense_approver",
        )

    _users(
        client,
        [{"email": "expense.approver@showAndTell.example", "name": "Morgan Price"}],
        ("Expense Approver", "HR User"),
    )
    expense_account = client.find_one(
        "Account", {"company": context.company, "root_type": "Expense", "is_group": 0}
    )
    if expense_account is None:
        raise RuntimeError("enterprise expense claims need a leaf expense account")
    for category in {row["category"] for row in workflows["expenses"]["claims"]}:
        client.ensure(
            "Expense Claim Type",
            {"expense_type": category},
            {
                "doctype": "Expense Claim Type",
                "expense_type": category,
                "accounts": [{
                    "company": context.company,
                    "default_account": expense_account.name,
                }],
            },
        )

    for row in workflows["expenses"]["claims"]:
        reference = str(row["claim_reference"])
        claim = client.find_one("Expense Claim", {"custom_showtell_reference": reference})
        if claim is None:
            claim = client.create(
                "Expense Claim",
                {
                    "doctype": "Expense Claim",
                    "employee": employees[str(row["employee_source_id"])].name,
                    "company": context.company,
                    "expense_approver": "expense.approver@showAndTell.example",
                    "currency": context.currency,
                    "exchange_rate": 1,
                    "posting_date": row["expense_date"],
                    "approval_status": "Draft",
                    "custom_showtell_reference": reference,
                    "custom_showtell_workflow_state": row["workflow_state"],
                    "custom_showtell_receipt_type": row["receipt_type"],
                    "custom_showtell_card_transaction": row["card_transaction"],
                    "custom_showtell_manager_approval": row["manager_approval"],
                    "custom_showtell_scenario_role": row["scenario_role"],
                    "remark": f"Reusable expense-audit fixture {reference}",
                    "expenses": [{
                        "expense_date": row["expense_date"],
                        "expense_type": row["category"],
                        "default_account": expense_account.name,
                        "description": f"Synthetic {row['category'].lower()} expense for audit training",
                        "amount": _money(row["amount_usd"]),
                        "sanctioned_amount": _money(row["amount_usd"]),
                        "cost_center": context.cost_center,
                    }],
                },
            )
        if row["receipt_type"] == "missing":
            continue
        file_name = f"{reference.lower()}-{row['receipt_type']}-receipt.txt"
        if client.find_one(
            "File",
            {"attached_to_doctype": "Expense Claim", "attached_to_name": claim.name, "file_name": file_name},
        ) is not None:
            continue
        body = (
            f"Synthetic receipt\nClaim: {reference}\nType: {row['receipt_type']}\n"
            f"Amount: ${row['amount_usd']}\nDate: {row['expense_date']}\n"
        ).encode()
        client.create(
            "File",
            {
                "doctype": "File",
                "file_name": file_name,
                "attached_to_doctype": "Expense Claim",
                "attached_to_name": claim.name,
                "is_private": 1,
                "content": base64.b64encode(body).decode(),
                "decode": 1,
            },
        )


def _seed_payroll(
    client: ERPNextClient,
    context: ERPNextSeedContext,
    workflows: Mapping[str, Any],
    employees: Mapping[str, ResourceRef],
) -> None:
    payroll = workflows["payroll"]
    for dt in ("Shift Assignment", "Employee Checkin", "Leave Application", "Timesheet", "Attendance"):
        _custom_field(
            client,
            dt=dt,
            fieldname="custom_showtell_reference",
            label="Payroll Case Reference",
            fieldtype="Data",
            insert_after="employee",
        )
        _custom_field(
            client,
            dt=dt,
            fieldname="custom_showtell_scenario_role",
            label="Scenario Role",
            fieldtype="Data",
            insert_after="custom_showtell_reference",
        )

    for row in payroll["shift_types"]:
        client.ensure(
            "Shift Type",
            {"name": row["name"]},
            {
                "doctype": "Shift Type",
                "name": row["name"],
                "start_time": row["start_time"],
                "end_time": row["end_time"],
                "enable_auto_attendance": 0,
            },
        )

    cases = {row["case_reference"]: row for row in payroll["cases"]}
    for row in payroll["cases"]:
        reference = str(row["case_reference"])
        if client.find_one("Shift Assignment", {"custom_showtell_reference": reference}) is None:
            client.create(
                "Shift Assignment",
                {
                    "doctype": "Shift Assignment",
                    "employee": employees[str(row["employee_source_id"])].name,
                    "company": context.company,
                    "shift_type": row["shift"],
                    "status": "Active",
                    "start_date": payroll["period_start"],
                    "end_date": payroll["period_end"],
                    "custom_showtell_reference": reference,
                    "custom_showtell_scenario_role": row["scenario_role"],
                    "docstatus": 1,
                },
            )

    checkin_counts: dict[str, int] = {}
    for row in payroll["checkins"]:
        reference = str(row["case_reference"])
        checkin_counts[reference] = checkin_counts.get(reference, 0) + 1
        checkin_reference = f"{reference}-{row['log_type']}-{checkin_counts[reference]}"
        if client.find_one("Employee Checkin", {"custom_showtell_reference": checkin_reference}) is not None:
            continue
        case = cases[reference]
        client.create(
            "Employee Checkin",
            {
                "doctype": "Employee Checkin",
                "employee": employees[str(row["employee_source_id"])].name,
                "log_type": row["log_type"],
                "time": row["time"],
                "shift": case["shift"],
                "skip_auto_attendance": 1,
                "custom_showtell_reference": checkin_reference,
                "custom_showtell_scenario_role": case["scenario_role"],
            },
        )

    client.ensure(
        "Leave Type",
        {"leave_type_name": "ShowAndTell Personal Leave"},
        {
            "doctype": "Leave Type",
            "leave_type_name": "ShowAndTell Personal Leave",
            "is_lwp": 1,
            "include_holiday": 0,
        },
    )
    leave_case = cases["PAY-26002"]
    holiday_list = client.ensure(
        "Holiday List",
        {"holiday_list_name": "ShowAndTell 2026 Working Calendar"},
        {
            "doctype": "Holiday List",
            "holiday_list_name": "ShowAndTell 2026 Working Calendar",
            "from_date": "2026-01-01",
            "to_date": "2026-12-31",
            "holidays": [],
        },
    )
    client.update(
        employees[str(leave_case["employee_source_id"])],
        {"holiday_list": holiday_list.name},
    )
    holiday_assignment = client.ensure(
        "Holiday List Assignment",
        {
            "applicable_for": "Employee",
            "assigned_to": employees[str(leave_case["employee_source_id"])].name,
            "holiday_list": holiday_list.name,
        },
        {
            "doctype": "Holiday List Assignment",
            "applicable_for": "Employee",
            "assigned_to": employees[str(leave_case["employee_source_id"])].name,
            "holiday_list": holiday_list.name,
            "from_date": "2026-01-01",
            "to_date": "2026-12-31",
        },
    )
    if client.get(holiday_assignment).get("docstatus") != 1:
        client.update(holiday_assignment, {"docstatus": 1})
    if client.find_one("Leave Application", {"custom_showtell_reference": "PAY-26002"}) is None:
        client.create(
            "Leave Application",
            {
                "doctype": "Leave Application",
                "employee": employees[str(leave_case["employee_source_id"])].name,
                "leave_type": "ShowAndTell Personal Leave",
                "company": context.company,
                "from_date": "2026-08-01",
                "to_date": "2026-08-01",
                "posting_date": "2026-07-28",
                "status": "Approved",
                "description": "Approved personal leave; valid payroll exception evidence.",
                "custom_showtell_reference": "PAY-26002",
                "custom_showtell_scenario_role": leave_case["scenario_role"],
            },
        )

    client.ensure(
        "Activity Type",
        {"activity_type": "ShowAndTell Operations"},
        {"doctype": "Activity Type", "activity_type": "ShowAndTell Operations"},
    )
    timesheet_case = cases["PAY-26003"]
    if client.find_one("Timesheet", {"custom_showtell_reference": "PAY-26003"}) is None:
        client.create(
            "Timesheet",
            {
                "doctype": "Timesheet",
                "title": "Completed hours awaiting manager approval",
                "company": context.company,
                "currency": context.currency,
                "employee": employees[str(timesheet_case["employee_source_id"])].name,
                "start_date": "2026-08-01",
                "end_date": "2026-08-01",
                "custom_showtell_reference": "PAY-26003",
                "custom_showtell_scenario_role": timesheet_case["scenario_role"],
                "time_logs": [{
                    "activity_type": "ShowAndTell Operations",
                    "from_time": "2026-08-01 09:00:00",
                    "to_time": "2026-08-01 17:00:00",
                    "hours": 8,
                    "completed": 1,
                    "description": "Operational shift completed; timesheet remains draft.",
                }],
            },
        )

    history_case = cases["PAY-26006"]
    if client.find_one("Attendance", {"custom_showtell_reference": "PAY-26006"}) is None:
        client.create(
            "Attendance",
            {
                "doctype": "Attendance",
                "employee": employees[str(history_case["employee_source_id"])].name,
                "attendance_date": "2026-07-25",
                "company": context.company,
                "status": "Present",
                "working_hours": 8,
                "custom_showtell_reference": "PAY-26006",
                "custom_showtell_scenario_role": history_case["scenario_role"],
                "docstatus": 1,
            },
        )
