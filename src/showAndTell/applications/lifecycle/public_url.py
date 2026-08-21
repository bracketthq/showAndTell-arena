"""Validation for the browser-visible origin an application is configured with."""
from __future__ import annotations

from urllib.parse import urlsplit


def validate_public_url(value: str, label: str = "public_url") -> str:
    """An exact http(s) origin: no credentials, path, query, or fragment.

    The one definition of "http(s) origin" shared by every application plane.
    Per-application validators layer their own input contracts on top rather
    than re-deciding what an origin is, so the planes cannot drift apart on
    undocumented axes (port validity, path parameters).
    """
    parsed = urlsplit(str(value))
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} has an invalid port") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(f"{label} must be an http(s) origin without credentials")
    return str(value).rstrip("/")
