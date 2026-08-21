"""ERPNext's shared browser lifecycle against the real UI.

The browser plane owns only behavior every ERPNext task needs: authentication
and readiness. Workflow vocabulary belongs to task demonstrations rather than
this application-wide integration boundary.
"""
from __future__ import annotations

import re

LIST_ROWS = ".frappe-list .list-row-container"
LIST_PAGING_AREA = ".list-paging-area"
# Frappe's own default page, and the size demonstrations were recorded at. A
# list rendering a full default page is truncated, not exhausted.
DEFAULT_PAGE_LENGTH = 20
RECORDED_PAGE_LENGTH = "100"


# Where login leaves the unified ERPNext + HRMS browser. The setup-completion
# repair in the lifecycle driver now makes Desk stable, so the neutral desktop
# is the right shared entry point: ERP tasks should not start in Stock and HR
# tasks should not make every ERP task start in Recruitment.
LANDING = "/desk"
DESK_READY = ".navbar, .layout-main"

# What this desk's own 'nothing to show' page says. A replay recovery that
# lands on it has not reproduced the recorded outcome (showAndTell.player.replay.landed
# asks each application's browser plane for this phrasing).
DEAD_PAGE_MARKERS = ("Sorry! I could not find what you were looking for",)


def login(page, app_url: str, creds: dict) -> None:
    base = app_url.rstrip("/")
    # The viewer signs the primary surface in before the product starts, and a
    # generated multi-app replay repeats its captured login markers while it
    # prepares supporting tabs. Do not send an already usable Desk page through
    # /login and the landing route again: on a remote fixture that redundant
    # round trip can consume the navigation timeout while narration is waiting.
    here = str(getattr(page, "url", ""))
    if here.startswith((f"{base}/app", f"{base}/desk")):
        try:
            page.wait_for_selector(DESK_READY, timeout=1_000)
            return
        except Exception:
            pass
    page.goto(f"{base}/login", wait_until="domcontentloaded")
    email = page.locator("#login_email")
    if email.count() and email.is_visible():
        email.fill(creds["email"])
        page.locator("#login_password").fill(creds["password"])
        page.locator(".btn-login:not(.btn-login-with-email-link)").click()
    page.wait_for_url(
        re.compile(r"/(?:app|desk)(?:/.*)?$"), wait_until="commit"
    )
    # Authenticated is not the same as usable, so land somewhere that renders.
    page.goto(f"{base}{LANDING}", wait_until="domcontentloaded")
    page.wait_for_selector(DESK_READY, timeout=60_000)


def ready(page) -> None:
    """Restore the list page size the demonstration was recorded at.

    A Frappe list renders one page of rows, and that page's size is a per-user
    preference Frappe keeps in its ``__UserSettings`` table. An exported site
    carries that table empty, so a replay opens every list at the 20-row
    default while the operator was recorded at 100. The gap is silent in the
    way that matters: the list renders, every selector on screen resolves, and
    only the recorded click on a row past the twentieth finds nothing to hit —
    which a driver then papers over by navigating to the record by URL, having
    never reproduced the click it was demonstrating.

    A list showing less than a full page already has everything it holds, so
    it is left alone; the cost is paid only where rows could still be missing.
    """
    try:
        paging = page.locator(LIST_PAGING_AREA)
        if not paging.count():
            return
        if page.locator(LIST_ROWS).count() < DEFAULT_PAGE_LENGTH:
            return
        button = paging.get_by_role("button", name=RECORDED_PAGE_LENGTH, exact=True)
        if not button.count():
            return
        button.first.click()
        # A list of exactly one default page has nothing more to fetch, so this
        # wait is allowed to lapse rather than gate readiness on growth.
        page.wait_for_function(
            "selector => document.querySelectorAll(selector).length > "
            f"{DEFAULT_PAGE_LENGTH}",
            arg=LIST_ROWS,
            timeout=8_000,
        )
    except Exception:                                            # noqa: BLE001
        return


__all__ = ["login", "ready"]
