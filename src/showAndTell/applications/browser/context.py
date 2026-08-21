"""Application URLs and credentials carried into a task's browser adapter.

Multi-application demonstrations receive the primary application's credentials
through their long-standing ``creds`` argument.  The runtime adds this reserved
context block so an adapter can cross into a supporting application without
process-global environment variables or fixture-family wiring.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


CONTEXT_KEY = "_showAndTell_applications"
_PAGE_ATTRIBUTE = "_showAndTell_application_context"


def bind(page, credentials: Mapping[str, Any]) -> None:
    """Attach the runtime-provided application context to a Playwright page."""
    context = credentials.get(CONTEXT_KEY, {})
    if not isinstance(context, Mapping):
        raise ValueError(f"{CONTEXT_KEY} must be an application mapping")
    setattr(page, _PAGE_ATTRIBUTE, dict(context))


def application(page, name: str) -> Mapping[str, Any]:
    context = getattr(page, _PAGE_ATTRIBUTE, {})
    if not isinstance(context, Mapping) or name not in context:
        raise RuntimeError(
            f"browser context has no {name!r} application; bind task credentials first"
        )
    value = context[name]
    if not isinstance(value, Mapping):
        raise RuntimeError(f"browser context for {name!r} is invalid")
    return value


def application_url(page, name: str, *, browser: bool = False) -> str:
    entry = application(page, name)
    key = "browser_url" if browser else "url"
    value = entry.get(key) or entry.get("url")
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"browser context for {name!r} has no {key}")
    return value.rstrip("/")


def application_credentials(page, name: str) -> dict[str, str]:
    value = application(page, name).get("credentials", {})
    if not isinstance(value, Mapping):
        raise RuntimeError(f"browser context for {name!r} has invalid credentials")
    return {str(key): str(secret) for key, secret in value.items()}


def application_metadata(page, name: str) -> Mapping[str, Any]:
    value = application(page, name).get("metadata", {})
    return value if isinstance(value, Mapping) else {}
