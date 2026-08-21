from __future__ import annotations

from datetime import date

from showAndTell.applications.erpnext.api import ResourceRef
from showAndTell.applications.erpnext.profiles import ERPNextSeedContext
from showAndTell.applications.erpnext.hrms import demo_dataset as hr_demo_dataset
from showAndTell.applications.erpnext.hrms.demo_dataset import (
    load_demo_dataset,
    seed_demo_dataset,
)


CONTEXT = ERPNextSeedContext(
    company="ShowAndTell Manufacturing",
    warehouse="Stores - STM",
    currency="USD",
    expense_account="Opening Stock - STM",
    cost_center="Main - STM",
)


class RecordingClient:
    def __init__(self, *, marker_exists: bool = False) -> None:
        self.marker_exists = marker_exists
        self.finds = []
        self.ensured = []

    def find_one(self, doctype, filters):
        self.finds.append((doctype, filters))
        if self.marker_exists and doctype == "Job Offer":
            return ResourceRef("Job Offer", "REC-OFFER-005")
        return None

    def ensure(self, doctype, filters, values):
        self.ensured.append((doctype, filters, values))
        native_name = (
            values.get("employee_number")
            or values.get("designation_name")
            or values.get("department_name")
            or values.get("branch")
            or values.get("gender")
            or values.get("fieldname")
            or values.get("source_name")
            or values.get("skill_name")
            or values.get("interview_type_name")
            or values.get("employee_type_name")
            or values.get("job_title")
            or values.get("email_id")
            or str(len(self.ensured))
        )
        return ResourceRef(doctype, native_name)


def test_vendored_hr_dataset_is_bounded_relational_and_complete():
    dataset = load_demo_dataset()
    assert dataset["source"]["license"] == "Apache-2.0"
    assert dataset["source"]["revision"] == (
        "54c51c47e1ce420e1ab7614ed61ddd9f3c2728c5"
    )
    assert dataset["source"]["source_employee_count"] == 15_700
    assert len(dataset["business_units"]) == 4
    assert len(dataset["departments"]) == 17
    assert len(dataset["jobs"]) == 94
    assert len(dataset["employees"]) == 100
    assert len(dataset["recruitment"]["job_openings"]) == 10
    assert len(dataset["recruitment"]["job_applicants"]) == 20
    assert len(dataset["recruitment"]["interviews"]) == 13
    assert len(dataset["recruitment"]["job_offers"]) == 5

    assert len({row["job_id"] for row in dataset["employees"]}) == 89
    assert {row["job_id"] for row in dataset["employees"]} <= {
        row["id"] for row in dataset["jobs"]
    }
    assert len({row["frappe_name"] for row in dataset["departments"]}) == 17
    assert len({row["frappe_name"] for row in dataset["jobs"]}) == 94

    seen = set()
    for employee in dataset["employees"]:
        manager = employee["reports_to_source_id"]
        assert manager is None or manager in seen
        born = date.fromisoformat(employee["birth_date"])
        joined = date.fromisoformat(employee["date_of_joining"])
        joining_age = joined.year - born.year - (
            (joined.month, joined.day) < (born.month, born.day)
        )
        assert joining_age >= 18
        seen.add(employee["id"])


def test_first_seed_maps_the_dataset_to_native_hr_records(monkeypatch):
    monkeypatch.delenv("SHOWANDTELL_FRAPPE_HR_DEMO_DATA", raising=False)
    enterprise_calls = []
    monkeypatch.setattr(
        hr_demo_dataset,
        "_seed_enterprise_workflows",
        lambda client, context, workflows, *, workforce, force=False:
            enterprise_calls.append(
                (client, context, workflows, workforce, force)
            ),
    )
    client = RecordingClient()
    seed_demo_dataset(client, CONTEXT, force=True)
    assert enterprise_calls == [
        (client, CONTEXT, load_demo_dataset()["enterprise_workflows"],
         load_demo_dataset()["employees"], True)
    ]

    counts = {}
    for doctype, _, _ in client.ensured:
        counts[doctype] = counts.get(doctype, 0) + 1
    assert counts == {
        "Custom Field": 11,
        "Gender": 4,
        "Branch": 4,
        "Department": 17,
        "Designation": 94,
        "Employee": 100,
        "Job Applicant Source": 4,
        "Skill": 19,
        "Interview Type": 10,
        "Employment Type": 3,
        "Job Opening": 10,
        "Job Applicant": 20,
        "Interview": 13,
        "Job Offer": 5,
    }

    employees = [values for doctype, _, values in client.ensured
                 if doctype == "Employee"]
    assert all(row["salary_currency"] == "USD" and row["ctc"] > 0
               for row in employees)
    assert all(row["company_email"].endswith("@showAndTell.example")
               for row in employees)
    assert any("reports_to" in row for row in employees)
    assert all(row["education"] for row in employees)

    openings = [values for doctype, _, values in client.ensured
                if doctype == "Job Opening"]
    applicants = [values for doctype, _, values in client.ensured
                  if doctype == "Job Applicant"]
    interviews = [values for doctype, _, values in client.ensured
                  if doctype == "Interview"]
    offers = [values for doctype, _, values in client.ensured
              if doctype == "Job Offer"]
    assert all(row["company"] == "ShowAndTell Manufacturing"
               and row["status"] == "Open" for row in openings)
    assert {row["status"] for row in applicants} >= {
        "Open", "Shortlisted", "Hold", "Rejected", "Accepted",
    }
    assert all(row["job_opening"] in {opening["job_title"]
                                      for opening in openings}
               for row in interviews)
    assert {row["status"] for row in offers} == {
        "Accepted", "Awaiting Response",
    }


def test_completion_marker_skips_workforce_and_still_seeds_workflows(monkeypatch):
    monkeypatch.delenv("SHOWANDTELL_FRAPPE_HR_DEMO_DATA", raising=False)
    enterprise_calls = []
    monkeypatch.setattr(
        hr_demo_dataset,
        "_seed_enterprise_workflows",
        lambda client, context, workflows, *, workforce, force=False:
            enterprise_calls.append(
                (client, context, workflows, workforce, force)
            ),
    )
    client = RecordingClient(marker_exists=True)
    seed_demo_dataset(client, CONTEXT)
    assert len(client.finds) == 1
    assert client.finds[0] == (
        "Job Offer",
        {"applicant_email": "rachel.davis@candidates.showAndTell.example"},
    )
    assert client.ensured == []
    assert enterprise_calls == [
        (client, CONTEXT, load_demo_dataset()["enterprise_workflows"],
         load_demo_dataset()["employees"], False)
    ]


def test_workforce_only_snapshot_backfills_recruitment(monkeypatch):
    monkeypatch.delenv("SHOWANDTELL_FRAPPE_HR_DEMO_DATA", raising=False)
    enterprise_calls = []
    monkeypatch.setattr(
        hr_demo_dataset,
        "_seed_enterprise_workflows",
        lambda client, context, workflows, *, workforce, force=False:
            enterprise_calls.append(
                (client, context, workflows, workforce, force)
            ),
    )
    client = RecordingClient()
    seed_demo_dataset(client, CONTEXT)
    assert client.finds[0] == (
        "Job Offer",
        {"applicant_email": "rachel.davis@candidates.showAndTell.example"},
    )
    assert sum(doctype == "Employee" for doctype, _, _ in client.ensured) == 100
    assert sum(doctype == "Job Opening" for doctype, _, _ in client.ensured) == 10
    assert sum(doctype == "Job Offer" for doctype, _, _ in client.ensured) == 5
    assert enterprise_calls == [
        (client, CONTEXT, load_demo_dataset()["enterprise_workflows"],
         load_demo_dataset()["employees"], False)
    ]


class EnterpriseSeedClient(RecordingClient):
    """A site whose workforce already exists (restored golden snapshot): the
    Job Offer marker is found, so seeding goes straight to the
    enterprise-workflow section against the pre-existing Employee records."""

    def __init__(self, employees):
        super().__init__(marker_exists=True)
        self.employees = employees
        self.created = []

    def find_one(self, doctype, filters):
        if doctype == "Account":
            return ResourceRef("Account", "Travel Expenses - STM")
        return super().find_one(doctype, filters)

    def list_all(self, doctype, fields=(), page_length=0):
        assert doctype == "Employee"
        return self.employees

    def create(self, doctype, values):
        self.created.append((doctype, values))
        return ResourceRef(
            doctype,
            values.get("custom_showtell_reference") or str(len(self.created)),
        )

    def update(self, ref, values):
        pass

    def get(self, ref):
        return {"docstatus": 1}


def test_enterprise_workflows_seed_survives_the_marker_short_circuit(monkeypatch):
    """The workflow block carries no employee table: its references must
    resolve through the full dataset's workforce, keyed by attendance device."""
    monkeypatch.delenv("SHOWANDTELL_FRAPPE_HR_DEMO_DATA", raising=False)
    monkeypatch.delenv("SHOWANDTELL_ENTERPRISE_DEMO_DATA", raising=False)
    dataset = load_demo_dataset()
    workforce = [
        {"name": f"HR-EMP-{index:05d}",
         "attendance_device_id": row["attendance_device_id"]}
        for index, row in enumerate(dataset["employees"], 1)
    ]
    client = EnterpriseSeedClient(workforce)

    seed_demo_dataset(client, CONTEXT)

    claims = [values for doctype, values in client.created
              if doctype == "Expense Claim"]
    assert len(claims) == 6
    name_by_device = {str(row["attendance_device_id"]): row["name"]
                      for row in workforce}
    device_by_source = {str(row["id"]): str(row["attendance_device_id"])
                        for row in dataset["employees"]}
    claim_rows = {
        row["claim_reference"]: row
        for row in dataset["enterprise_workflows"]["expenses"]["claims"]
    }
    for values in claims:
        source = claim_rows[values["custom_showtell_reference"]]["employee_source_id"]
        assert values["employee"] == name_by_device[device_by_source[str(source)]]
    assert sum(doctype == "Shift Assignment"
               for doctype, _ in client.created) == 6
    assert any(
        doctype == "Note"
        and filters == {"title": hr_demo_dataset.ENTERPRISE_MARKER_TITLE}
        for doctype, filters, _ in client.ensured
    )


def test_demo_workforce_can_be_disabled_for_a_bare_site(monkeypatch):
    monkeypatch.setenv("SHOWANDTELL_FRAPPE_HR_DEMO_DATA", "0")
    client = RecordingClient()
    seed_demo_dataset(client, CONTEXT)
    assert client.finds == []
    assert client.ensured == []
