"""Semantic Playwright operations for the pinned Twenty CRM UI."""

from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from showAndTell.applications.twenty import validate


STAGE_LABELS = {
    "NEW": "New",
    "SCREENING": "Screening",
    "MEETING": "Meeting",
    "PROPOSAL": "Proposal",
    "CUSTOMER": "Customer",
}


def _base_url(value: str) -> str:
    return validate.base_url(value, "Twenty app_url")


def _required_text(value: object, label: str) -> str:
    return validate.required_text(value, f"Twenty {label}")


def _unique_visible(locator, label: str):
    try:
        locator.wait_for(state="visible")
        count = locator.count()
    except (PlaywrightError, PlaywrightTimeoutError) as exc:
        raise RuntimeError(f"Twenty {label} is not visible") from exc
    if count != 1 or not locator.is_visible():
        raise RuntimeError(f"Twenty {label} is not uniquely visible")
    return locator


def login(page, app_url: str, credentials: Mapping[str, str]) -> None:
    """Sign in through Twenty's visible form or reuse an authenticated page."""

    base = _base_url(app_url)
    email = _required_text(credentials.get("email"), "login email")
    password = _required_text(credentials.get("password"), "login password")
    page.goto(base, wait_until="domcontentloaded")

    opportunities_link = page.get_by_role("link", name="Opportunities", exact=True)
    email_input = page.get_by_role("textbox", name="Email", exact=True)
    try:
        opportunities_link.wait_for(state="visible", timeout=3_000)
    except PlaywrightTimeoutError:
        _unique_visible(email_input, "login email input").fill(email)
        _unique_visible(
            page.get_by_role("button", name="Continue", exact=True),
            "login continue button",
        ).click()
        password_input = _unique_visible(
            page.get_by_placeholder("Password", exact=True), "login password input"
        )
        password_input.fill(password)
        _unique_visible(
            page.get_by_role("button", name="Sign in", exact=True),
            "login submit button",
        ).click()

    _unique_visible(
        opportunities_link,
        "authenticated Opportunities navigation",
    )
    final, expected = urlparse(str(page.url)), urlparse(base)
    if (final.scheme, final.hostname, final.port) != (
        expected.scheme, expected.hostname, expected.port,
    ):
        raise RuntimeError("Twenty login crossed to an unexpected origin")


def open_opportunities(page, app_url: str) -> None:
    """Open the opportunity table and close any stale record side panel."""

    base = _base_url(app_url)
    if not urlparse(str(page.url)).path.startswith("/objects/opportunities"):
        _unique_visible(
            page.get_by_role("link", name="Opportunities", exact=True),
            "Opportunities navigation",
        ).click()
        page.wait_for_url(re.compile(r".*/objects/opportunities(?:\?.*)?$"))
    if urlparse(str(page.url)).hostname != urlparse(base).hostname:
        raise RuntimeError("Twenty opportunity navigation crossed origins")
    close_panel = page.get_by_role("button", name="Close side panel", exact=True)
    if close_panel.count() == 1 and close_panel.is_visible():
        close_panel.click()
    _unique_visible(
        page.get_by_role("button", name="Create new Opportunity", exact=True),
        "opportunity table",
    )


def _options_button(page):
    return _unique_visible(
        page.locator("button").filter(has_text=re.compile(r"^\s*Options\s*$")),
        "table options button",
    )


def _ensure_stage_column(page) -> int:
    stage_header = page.get_by_role("button", name="Stage", exact=True)
    if stage_header.count() == 0:
        _options_button(page).click()
        _unique_visible(
            page.get_by_text("Fields", exact=True), "Fields menu item"
        ).click()
        _unique_visible(
            page.get_by_text("Hidden Fields", exact=True), "Hidden Fields menu item"
        ).click()
        hidden_stage = _unique_visible(
            page.get_by_text("Stage", exact=True), "hidden Stage field"
        )
        stage_row = hidden_stage.locator("..").locator("..").locator("..").locator("..")
        _unique_visible(stage_row.locator("button"), "show Stage column button").click()
        _options_button(page).click()
        stage_header = page.get_by_role("button", name="Stage", exact=True)

    header = _unique_visible(stage_header, "Stage column header")
    header_cell_class = header.locator("..").get_attribute("class") or ""
    match = re.search(r"(?:^|\s)record-table-column-field-(\d+)(?:\s|$)", header_cell_class)
    if match is None:
        raise RuntimeError("Twenty Stage column did not expose a table index")
    return int(match.group(1))


def set_opportunity_stage(page, opportunity_name: str, stage: str) -> None:
    """Set one exact opportunity's stage through the editable table cell."""

    name = _required_text(opportunity_name, "opportunity name")
    stage_key = _required_text(stage, "stage").upper()
    if stage_key not in STAGE_LABELS:
        raise ValueError(f"unsupported Twenty stage {stage!r}")
    label = STAGE_LABELS[stage_key]
    column_index = _ensure_stage_column(page)

    opportunity_link = _unique_visible(
        page.get_by_role("link", name=re.compile(rf".*{re.escape(name)}$")),
        f"opportunity row {name!r}",
    )
    row = opportunity_link.locator(
        "xpath=ancestor::div[contains(@class, 'record-table-column-field-')][1]/parent::div"
    )
    cell = _unique_visible(
        row.locator(f'[data-record-table-col="{column_index}"]'),
        f"stage cell for {name!r}",
    )
    if cell.get_by_text(label, exact=True).count():
        return

    cell.click()
    choices = _unique_visible(page.get_by_role("listbox"), "stage choices")
    _unique_visible(
        choices.get_by_text(label, exact=True), f"stage choice {label!r}"
    ).click()
    try:
        choices.wait_for(state="hidden")
        cell.get_by_text(label, exact=True).first.wait_for(state="visible")
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(
            f"Twenty did not display persisted stage {label!r} for {name!r}"
        ) from exc
