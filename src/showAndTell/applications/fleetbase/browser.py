"""Minimal semantic browser entry for Fleetbase's native login surface."""
from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


# Fleetbase serves its login form from an Ember bundle, so a cold console
# answers with an empty document for a while before either surface exists.
# Module scope keeps ``login``'s dispatch signature fixed while still letting
# a test reach the failure paths without waiting out a real boot.
READY_TIMEOUT_MS = 60_000


def _console_shell(page):
    """The console's extension navigation, which only a session paints.

    Fleet-Ops sits inside this menubar as one of five extension entries, so
    the menubar is the part that belongs to the console itself rather than to
    whichever extensions an install happens to carry.
    """
    return page.get_by_role("menubar", name="Extension navigation", exact=True)


def _is_authenticated(page) -> bool:
    """Whether this page is a signed-in Fleetbase console.

    Deliberately no route test. Fleetbase's Ember ``console`` route is mounted
    at ``/``, so its authenticated URLs are the bare root and ``/fleet-ops``
    -- and the root is equally where an unauthenticated visitor is bounced. A
    URL cannot settle this in either direction: a route that reads as signed
    in can still be an Ember bundle that has painted nothing at all. The
    console's own navigation is the only on-screen proof either way.
    """
    shell = _console_shell(page)
    return bool(shell.count() and shell.first.is_visible())


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Fleetbase app_url must be an absolute http(s) URL")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("Fleetbase app_url has an invalid port") from exc
    return parsed.scheme, parsed.hostname.casefold(), port


def _assert_same_origin(page, app_url: str) -> None:
    try:
        final = _origin(str(page.url))
    except ValueError:
        raise RuntimeError(
            "Fleetbase login returned an invalid final URL") from None
    if final != _origin(app_url):
        raise RuntimeError("Fleetbase login crossed to an unexpected origin")


def _base_url(app_url: str) -> str:
    _origin(app_url)
    return app_url.rstrip("/")


def login(page, app_url: str, credentials: Mapping[str, str]) -> None:
    """Sign in through Fleetbase's visible form or reuse the current session."""

    base = _base_url(app_url)
    page.goto(f"{base}/auth", wait_until="domcontentloaded")
    email = page.get_by_role("textbox", name="Email address", exact=True)
    shell = _console_shell(page)
    # Either surface settles the question. Waiting for the form alone would
    # read an already-signed-in dashboard as a console that never booted.
    try:
        email.or_(shell).first.wait_for(
            state="visible", timeout=READY_TIMEOUT_MS)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(
            "Fleetbase did not expose a login or authenticated surface") from exc

    if email.count() and email.first.is_visible():
        email.first.fill(credentials["email"])
        page.get_by_label("Password", exact=True).fill(credentials["password"])
        page.get_by_role("button", name=re.compile(r"^Sign in$", re.I)).click()
        # Waited on the console itself rather than the URL it lands on:
        # authenticated is not the same as usable, and a replay handed a route
        # whose bundle has painted nothing fails later, at the first gesture,
        # with the cause long out of sight.
        try:
            shell.first.wait_for(state="visible", timeout=READY_TIMEOUT_MS)
        except PlaywrightTimeoutError as exc:
            raise RuntimeError(
                "Fleetbase login did not expose its authenticated surface"
            ) from exc

    _assert_same_origin(page, base)
    if not _is_authenticated(page):
        raise RuntimeError("Fleetbase login did not reach an authenticated console")
