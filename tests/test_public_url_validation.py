"""The shared definition of an http(s) origin, and the planes layered on it."""
from __future__ import annotations

import pytest

from showAndTell.applications.lifecycle.public_url import validate_public_url
from showAndTell.applications.twenty import validate as twenty_validate


def test_accepts_an_origin_and_strips_the_trailing_slash():
    assert validate_public_url("http://agent.test:8080/") == "http://agent.test:8080"


@pytest.mark.parametrize("value", [
    "ftp://host",
    "http://",
    "http://user:pw@host",
    "http://host/app",
    "http://host/?q=1",
    "http://host/#frag",
    "http://host/;params",
])
def test_rejects_everything_that_is_not_a_bare_origin(value):
    with pytest.raises(ValueError):
        validate_public_url(value)


def test_rejects_an_invalid_port_naming_the_label():
    with pytest.raises(ValueError, match="app_url has an invalid port"):
        validate_public_url("http://host:notaport", "app_url")


def test_twenty_base_url_layers_its_input_contract_over_the_shared_rule():
    assert twenty_validate.base_url(
        "https://crm.test:3000", "Twenty app_url") == "https://crm.test:3000"
    with pytest.raises(ValueError, match="Twenty app_url"):
        twenty_validate.base_url(" https://crm.test:3000", "Twenty app_url")
    with pytest.raises(ValueError, match="Twenty app_url"):
        twenty_validate.base_url("https://crm.test:3000/;x", "Twenty app_url")
