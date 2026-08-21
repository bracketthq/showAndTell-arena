"""ERPNext profile seeding: seed-block dispatch diagnostics."""
from __future__ import annotations

import pytest

from showAndTell.applications.erpnext.profiles import (
    ERPNextSeedContext,
    _custom_field,
    seed_profile,
)


CONTEXT = ERPNextSeedContext(
    company="ShowAndTell Manufacturing",
    warehouse="Stores - STM",
    currency="USD",
    expense_account="Opening Stock - STM",
    cost_center="Main - STM",
)


def test_seed_profile_rejects_an_unknown_profile_by_name():
    block = {"schema_version": 1, "source": {}, "profile": "mystery"}
    with pytest.raises(NotImplementedError, match="'mystery'"):
        seed_profile(object(), block, CONTEXT)


def test_seed_profile_rejects_a_non_string_profile_by_value():
    """An authoring mistake (a list where a name belongs) must name the bad
    value, not die as an unhashable-type TypeError inside the dispatch dict."""
    block = {"schema_version": 1, "source": {}, "profile": ["sales_order"]}
    with pytest.raises(NotImplementedError, match=r"\['sales_order'\]"):
        seed_profile(object(), block, CONTEXT)


def test_custom_field_rejects_mixed_case_database_identifiers():
    with pytest.raises(ValueError, match="must be lowercase"):
        _custom_field(
            object(),
            dt="Customer",
            fieldname="custom_showAndTell_reference",
            label="ShowAndTell Reference",
        )
