"""The unified ERPNext + HRMS data plane: one Frappe site, not a document.

Everything else in this folder can describe its own state as data. ERPNext
cannot: its export round-trips only a seed it created itself, so records an
operator entered by hand in the UI are invisible to it. That is why the
manifest declares ``snapshots = true`` and ``capture = false`` — hand-made
state travels as a database snapshot taken through the driver instead.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from showAndTell.applications.erpnext.api import ERPNextClient, ResourceRef
from showAndTell.applications.erpnext.demo_dataset import seed_demo_dataset as seed_erp_demo_dataset
from showAndTell.applications.erpnext.profiles import (
    ProfileSeedResult,
    seed_profile,
)
from showAndTell.applications.erpnext.baseline import ensure_baseline
from showAndTell.applications.erpnext.hrms.demo_dataset import (
    seed_demo_dataset as seed_hr_demo_dataset,
)
from showAndTell.applications.erpnext.hrms.seeder import (
    reset_frappe_hr,
    seed_frappe_hr,
)


_SETUP_WIZARD_APPS = ("frappe", "erpnext", "hrms")


def _finish_setup_wizard(client: ERPNextClient) -> None:
    """Mark every installed app complete so Desk routes to a real workspace."""
    for app_name in _SETUP_WIZARD_APPS:
        ref = client.find_one("Installed Application", {"app_name": app_name})
        if ref is None:
            raise RuntimeError(
                f"Frappe is missing the Installed Application row for {app_name}"
            )
        client.update(ref, {"is_setup_complete": 1})


class State:
    application = "erpnext"

    def __init__(self, manifest, *, client_factory=None) -> None:
        self.manifest = manifest
        self._client_factory = client_factory
        self._last_result: ProfileSeedResult | None = None
        self._hr_block: dict[str, Any] | None = None

    def _login(self, ctx) -> ERPNextClient:
        if self._client_factory is not None:
            return self._client_factory(ctx)
        return ERPNextClient.password_login(
            ctx.url, ctx.credentials["email"], ctx.credentials["password"])

    # -- contract ----------------------------------------------------------
    def prepare(self, ctx) -> None:
        """Plant the shared ERP catalog and HR workforce, but no task records.

        Both modules live on this site. A capture therefore starts with the
        Northwind-derived ERP catalog and the bounded synthetic workforce,
        regardless of which workspace the author opens.
        """
        with self._login(ctx) as client:
            baseline = ensure_baseline(client)
            seed_erp_demo_dataset(client, baseline)
            seed_hr_demo_dataset(client, baseline)
            client.ensure(
                "Gender",
                {"name": "Unspecified"},
                {"doctype": "Gender", "gender": "Unspecified"},
            )
            _finish_setup_wizard(client)
        self._last_result = None
        self._hr_block = None

    def reset(self, ctx) -> None:
        # The coarse reset is the driver's: it restores the golden snapshot,
        # which is a whole-database operation this plane cannot express.
        if self._hr_block is not None:
            with self._login(ctx) as client:
                reset_frappe_hr(client, self._hr_block)
        self._last_result = None
        self._hr_block = None

    def seed(self, ctx, block: Mapping[str, Any]) -> None:
        if not isinstance(block, Mapping):
            raise ValueError("erpnext seed block must be an object")
        hr_block = block.get("hrms")
        if hr_block is not None:
            if not isinstance(hr_block, Mapping):
                raise ValueError("erpnext.hrms seed block must be an object")
            owned = deepcopy(dict(hr_block))
            with self._login(ctx) as client:
                baseline = ensure_baseline(client)
                # A golden snapshot normally carries both datasets. These calls
                # reduce to marker probes and also make a bare site deterministic.
                seed_erp_demo_dataset(client, baseline)
                seed_hr_demo_dataset(client, baseline)
                seed_frappe_hr(client, owned)
                _finish_setup_wizard(client)
                company = owned["company"]
                client.update(ResourceRef("Global Defaults", "Global Defaults"), {
                    "default_company": company["name"],
                    "default_currency": company["default_currency"],
                    "country": company["country"],
                })
                client.update(ResourceRef("System Settings", "System Settings"), {
                    "setup_complete": 1, "language": "en", "time_zone": "UTC"})
            self._hr_block = owned
            self._last_result = None
            return

        profile = (block or {}).get("profile")
        if not profile:
            # A captured draft's ERPNext block names no profile, because the
            # operator built that state by hand and the snapshot carries it.
            # Applying nothing is the correct answer, not a silent skip.
            return
        with self._login(ctx) as client:
            baseline = ensure_baseline(client)
            seed_erp_demo_dataset(client, baseline)
            seed_hr_demo_dataset(client, baseline)
            self._last_result = seed_profile(client, block, baseline)
        self._hr_block = None

    def export(self, ctx) -> dict[str, Any]:
        if self._hr_block is not None:
            with self._login(ctx) as client:
                for row in self._hr_block.get("job_requisitions", []):
                    ref = client.find_one(
                        "Job Requisition",
                        {"custom_showtell_external_id": row["external_id"]})
                    if ref is None:
                        raise RuntimeError(
                            "Frappe HR is missing requisition "
                            f"{row['external_id']}")
                    client.get(ref)
            return {"hrms": deepcopy(self._hr_block)}
        result = self._last_result
        if result is None:
            return {"profile": None, "items": []}
        items: list[dict[str, Any]] = []
        with self._login(ctx) as client:
            for source_sku, native_name in sorted(result.aliases.items()):
                record = client.get(ResourceRef("Item", native_name))
                items.append({
                    "source_sku": source_sku,
                    "name": native_name,
                    "item_name": record.get("item_name"),
                    "stock_status": record.get("custom_showtell_stock_status"),
                })
        return {"profile": result.profile, "items": items}

    def browser_metadata(self, _ctx) -> dict[str, Any]:
        result = self._last_result
        return {"aliases": dict(result.aliases)} if result is not None else {}
