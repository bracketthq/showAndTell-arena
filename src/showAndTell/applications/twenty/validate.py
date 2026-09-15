"""Input validation shared by Twenty's API, browser, and state planes.

Each plane takes the same two kinds of untrusted value — an origin it will
address, and a required string it will send — and rejects them on the same
terms. Keeping one copy means a plane cannot quietly become the lenient one.
"""
from __future__ import annotations

from showAndTell.applications.lifecycle.public_url import validate_public_url


def required_text(value: object, label: str) -> str:
    """A non-empty string carrying no leading or trailing whitespace."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty stripped string")
    return value


def base_url(value: str, label: str) -> str:
    """An exact http(s) origin: no credentials, path, query, or fragment.

    The origin rule itself is the generic ``validate_public_url``; Twenty only
    adds its input contract — the value must already be a stripped string —
    because these addresses are composed verbatim into REST paths.
    """
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be an exact http(s) origin")
    return validate_public_url(value, label)
