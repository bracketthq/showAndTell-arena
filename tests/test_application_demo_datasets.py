from __future__ import annotations

from showAndTell.applications.erpnext.demo_dataset import (
    load_demo_dataset as load_erpnext_demo_dataset,
)
from showAndTell.applications.fleetbase.demo_dataset import (
    load_demo_dataset as load_fleetbase_demo_dataset,
    render_enterprise_seed_sql,
)
from showAndTell.applications.erpnext.hrms.demo_dataset import (
    load_demo_dataset as load_hr_demo_dataset,
)
from showAndTell.applications.twenty.demo_dataset import (
    load_demo_dataset as load_twenty_demo_dataset,
    load_demo_seed,
)


def test_each_application_owns_and_validates_its_fixture() -> None:
    erpnext = load_erpnext_demo_dataset()["enterprise_workflows"]
    hr = load_hr_demo_dataset()["enterprise_workflows"]
    twenty = load_twenty_demo_dataset()
    fleetbase = load_fleetbase_demo_dataset()

    assert set(erpnext) == {
        "metadata", "crm", "accounts_receivable", "maintenance_inventory",
    }
    assert set(hr) == {"metadata", "expenses", "payroll"}
    assert set(twenty) == {"metadata", "crm"}
    assert set(fleetbase) == {"metadata", "fleet"}
    assert len(erpnext["accounts_receivable"]["invoices"]) == 6
    assert len(erpnext["maintenance_inventory"]["kits"]) == 3
    assert len(hr["expenses"]["claims"]) == 6
    assert len(hr["payroll"]["cases"]) == 6
    assert len(twenty["crm"]["companies"]) == 6
    assert len(fleetbase["fleet"]["vehicles"]) == 6


def test_independent_fixtures_preserve_intentional_business_keys() -> None:
    erpnext = load_erpnext_demo_dataset()["enterprise_workflows"]
    twenty = load_twenty_demo_dataset()
    fleetbase = load_fleetbase_demo_dataset()

    erp_account_codes = {
        row["account_code"] for row in erpnext["crm"]["companies"]
    }
    twenty_account_codes = {
        row["account_code"] for row in twenty["crm"]["companies"]
    }
    assert erp_account_codes == twenty_account_codes

    twenty_seed = load_demo_seed()
    assert {row["source_id"] for row in twenty_seed["companies"]} == {
        "ACCT-4101", "ACCT-4102", "ACCT-4103",
        "ACCT-4104", "ACCT-4105", "ACCT-4106",
    }
    assert all(
        row["company_source_id"].startswith("ACCT-")
        for row in twenty_seed["opportunities"]
    )

    erp_kit_codes = {
        row["item_code"]
        for row in erpnext["maintenance_inventory"]["kits"]
    }
    assert {
        row["kit_item_code"]
        for row in fleetbase["fleet"]["maintenance_schedules"]
    } <= erp_kit_codes

    sql = render_enterprise_seed_sql()
    assert sql.count("INSERT INTO vehicles ") == 6
    assert sql.count("INSERT INTO maintenance_schedules ") == 6
    assert sql.count("INSERT INTO work_orders ") == 2
    assert sql.count("INSERT INTO orders ") == 4
    assert "enterprise-operations:" in sql
    assert "fleetops-testing:" not in sql
