"""Idempotent first-account onboarding through Fleetbase's public API."""
from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urljoin

import httpx

ONBOARDING_STATUS_PATH = "/int/v1/onboard/should-onboard"
ONBOARDING_CREATE_PATH = "/int/v1/onboard/create-account"
ADMIN_NAME = "Fleet Operations"
ORGANIZATION_NAME = "Bluegrass Freight"
ADMIN_PHONE = "+12025550138"


def _required_credential(credentials: Mapping[str, str], key: str) -> str:
    value = credentials.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Fleetbase {key} credential must be a non-empty string")
    return value.strip()


def bootstrap_fleetbase(
    api_url: str,
    credentials: Mapping[str, str],
    *,
    transport: httpx.BaseTransport | None = None,
) -> bool:
    """Create Fleetbase's first administrator when the instance is empty.

    Returns ``True`` only when this call created the account. Fleetbase owns
    password hashing, company creation, roles, and the Fleet-Ops installation;
    the harness merely submits the same supported onboarding request as the UI.
    """

    email = _required_credential(credentials, "email")
    password = _required_credential(credentials, "password")
    base = api_url.rstrip("/") + "/"

    with httpx.Client(transport=transport, timeout=30.0) as client:
        status = client.get(urljoin(base, ONBOARDING_STATUS_PATH.lstrip("/")))
        status.raise_for_status()
        payload = status.json()
        if not isinstance(payload, dict) or not isinstance(
            payload.get("should_onboard"), bool
        ):
            raise RuntimeError("Fleetbase returned malformed onboarding status")
        if payload["should_onboard"] is False:
            return False

        created = client.post(
            urljoin(base, ONBOARDING_CREATE_PATH.lstrip("/")),
            json={
                "name": ADMIN_NAME,
                "email": email,
                "phone": ADMIN_PHONE,
                "password": password,
                "password_confirmation": password,
                "organization_name": ORGANIZATION_NAME,
                "timezone": "UTC",
            },
        )
        created.raise_for_status()
        result = created.json()
        if not isinstance(result, dict) or result.get("status") != "success":
            raise RuntimeError("Fleetbase returned malformed onboarding result")
        return True
