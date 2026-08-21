"""Magento 2 admin browser operations for teach demonstrations.

Selectors are written for a stock Magento 2 admin panel (the WebArena
`shopping_admin` image is Magento 2.x) but — unlike gitlab_ui — they have NOT
yet been verified against the live WebArena image, which wasn't available on
the authoring machine. They stick to Magento's stable hooks (form input names
like `login[username]` / `product[price]`, button ids like
`#order-view-cancel-button`, the `table.data-grid` markup both grid flavours
render), so they should hold, but expect to re-verify on the first live run.
Each shopping_admin task's `demonstrate.py` composes these primitives, so the
selectors live in exactly one place.

Magento admin grids are JS-heavy — UI-component grids hydrate client-side
behind a loading mask well after `networkidle` — so readers poll rather than
trusting the first render, same as gitlab_ui.open_issue_iids.
"""
from __future__ import annotations

import re
from datetime import datetime

# A read pass over the grid races its async re-render: handles snapshotted a
# moment ago go stale when Knockout replaces the DOM or the legacy grid
# reloads. core/pwerrors owns the error taxonomy (and its guarded Playwright import)
# so the signatures live in exactly one place.
from showAndTell.core.pwerrors import PlaywrightError, PWTimeout, is_stale_read

_SETTLE_MS = 800
_GRID_POLLS = 15  # ~15s: UI-component grids hydrate well after networkidle


# --- session ---------------------------------------------------------------

def login(page, app_url: str, creds: dict, timeout: int = 15000) -> None:
    # Idempotent: the managed Chrome profile persists the admin session across
    # runs, so /admin may land on the dashboard with no login form.
    page.goto(f"{app_url}/admin", wait_until="networkidle")
    if page.query_selector("input[name='login[username]']") is None:
        _dismiss_popup(page)
        return  # already signed in
    page.fill("input[name='login[username]']", creds["email"])
    page.fill("input[name='login[password]']", creds["password"])
    page.click(".action-login, .actions button[type=submit]")
    # Exit-verify: the admin menu only renders for an authenticated session.
    # Without this, a missed OS-input click on Sign In leaves the demo
    # silently signed out, reading every grid as empty (mirrors
    # shopping_ui.login's logout-link wait).
    page.wait_for_selector(".admin__menu, #nav", timeout=timeout)
    _dismiss_popup(page)


def _dismiss_popup(page) -> None:
    """Close the usage-statistics modal Magento shows on a fresh admin session
    so it can't swallow the next click. Locator, not ElementHandle: the
    OS-input proxy routes locator clicks as real gestures (handles are
    read-only)."""
    btn = page.locator(".modal-popup._show .action-dismiss, "
                       ".modal-popup._show button.action-secondary")
    if btn.count():
        btn.first.click()
        page.wait_for_timeout(_SETTLE_MS)


# --- grids -----------------------------------------------------------------

def grid_rows(page) -> list[dict[str, str]]:
    """Visible rows of the current admin grid, each as {column header: cell text}.

    Generic over both grid flavours — new UI-component grids (orders, products)
    and legacy widget grids (reviews) both render `table.data-grid`. Poll until
    real rows or the explicit empty-records placeholder appear, because the UI
    grids hydrate behind a spinner long after the page 'loads'."""
    for _ in range(_GRID_POLLS):
        try:
            # The busy probe races the re-render just like the read does —
            # keep both under the stale guard.
            rows = None if _grid_busy(page) else _read_grid(page)
        except PlaywrightError as exc:
            if not is_stale_read(exc):
                raise
            rows = None  # grid re-rendered mid-read: not ready, poll again
        if rows is not None:
            return rows
        page.wait_for_timeout(1000)
    return []


def _grid_busy(page) -> bool:
    mask = page.query_selector(".admin__data-grid-loading-mask, .loading-mask")
    return bool(mask and mask.is_visible())


def _read_grid(page) -> list[dict[str, str]] | None:
    """One pass over the first visible data grid; None means 'not ready, keep
    polling', [] means the grid is genuinely empty."""
    table = next((t for t in page.query_selector_all("table.data-grid") if t.is_visible()), None)
    if table is None:
        return None
    head = table.query_selector("thead tr")  # first thead row only: legacy grids add a filter row
    if head is None:
        return None
    headers = []
    for i, th in enumerate(head.query_selector_all("th, td")):
        first = ((th.inner_text() or "").strip().splitlines() or [""])[0].strip()
        headers.append(first or f"col{i}")
    rows: list[dict[str, str]] = []
    for tr in table.query_selector_all("tbody tr"):
        cells = tr.query_selector_all("td")
        if not cells:
            continue
        if len(cells) == 1 and len(headers) > 1:
            return []  # the "We couldn't find any records." placeholder row
        rows.append({(headers[i] if i < len(headers) else f"col{i}"): (c.inner_text() or "").strip()
                     for i, c in enumerate(cells)})
    return rows or None


def _row_with_cell(page, value: str):
    """Grid row with a cell matching `value` exactly — substring matching would
    confuse e.g. order 000000012 with 000000123."""
    cell = page.locator("td", has_text=re.compile(rf"^\s*{re.escape(str(value))}\s*$"))
    return page.locator("table.data-grid tbody tr").filter(has=cell).first


# --- orders ----------------------------------------------------------------

def goto_orders_grid(page, app_url: str) -> None:
    page.goto(f"{app_url}/admin/sales/order/", wait_until="networkidle")


def open_order(page, increment_id: str) -> None:
    """Open an order from the loaded grid by increment id. Order view URLs key
    on the internal entity id, which the grid exposes only inside the row's
    View link, so click through the row instead of composing a URL."""
    row = _row_with_cell(page, increment_id)
    link = row.locator("a", has_text=re.compile("view", re.I))
    (link.first if link.count() else row).click()
    page.wait_for_selector("#order_status", timeout=60000)
    page.wait_for_timeout(_SETTLE_MS)


def order_status(page) -> str:
    el = page.query_selector("#order_status")
    return (el.inner_text() or "").strip() if el else ""


def add_order_comment(page, text: str) -> None:
    page.fill("#history_comment", text)
    page.click("[data-ui-id='order-history-submit-button'], #submit_comment_button")
    # the posted note renders in the order's comments history
    page.wait_for_selector(f".note-list:has-text({text[:24]!r})", timeout=30000)


def cancel_order(page) -> None:
    page.click("#order-view-cancel-button")
    _confirm_modal(page)
    _wait_for_status(page, "Canceled")


def hold_order(page) -> None:
    page.click("#order-view-hold-button")
    _confirm_modal(page)  # some versions confirm, some submit straight through
    _wait_for_status(page, "On Hold")


def _confirm_modal(page, timeout_ms: int = 8000) -> None:
    """Accept Magento's 'Are you sure?' confirm modal if one appears."""
    try:
        page.wait_for_selector(".modal-popup._show .action-accept", timeout=timeout_ms)
    except Exception:
        return  # no confirmation step on this action/version
    page.click(".modal-popup._show .action-accept")
    page.wait_for_load_state("networkidle")


def _wait_for_status(page, status: str) -> None:
    last = ""  # last successfully READ status — kept across stale polls so
    for _ in range(10):  # the failure message reports real state, not ''
        try:
            cur = order_status(page)
        except PlaywrightError as exc:
            if not is_stale_read(exc):
                raise
            cur = None  # the action's reload is mid-flight: poll again
        if cur is not None:
            last = cur
            if cur.lower() == status.lower():
                return
        page.wait_for_timeout(1000)
    raise RuntimeError(f"order did not reach status {status!r} (now {last!r})")


# --- reviews ---------------------------------------------------------------

def goto_reviews(page, app_url: str) -> None:
    """Open the product-reviews grid filtered to Pending — the moderation queue.

    Reviews is a legacy widget grid whose filter row sits inside the table, so
    pick Pending in the Status filter and run Search. If the filter select
    isn't found the grid is left as-is (legacy filters are sticky per session,
    so it may already be applied) and callers must still check row status."""
    page.goto(f"{app_url}/admin/review/product/index/", wait_until="networkidle")
    grid_rows(page)  # let the grid settle before driving its filter row
    if page.query_selector("table.data-grid select[name=status]") is None:
        return
    # Locator, not ElementHandle: the OS-input proxy routes locator
    # select_option as a real gesture (handles stay read-only).
    page.locator("table.data-grid select[name=status]").first.select_option(
        label="Pending")
    # The GRID's Search button — NOT the collapsed global header search, which
    # also carries title="Search" (and is invisible); pick the visible one.
    btn = page.locator("button[title='Search']:visible, "
                       "button.action-default.scalable:has-text('Search')").first
    if btn.count():
        btn.click()
    else:
        # Routed press (click + Enter) — page.keyboard would be a silent CDP
        # keystroke on the OS-input path, which the proxy now denies.
        page.press("table.data-grid select[name=status]", "Enter")
    page.wait_for_load_state("networkidle")


def open_review(page, review_id: str) -> None:
    """Open a review's edit form by clicking its grid row (uniform with the
    other grids; edit URLs key on the review id the row shows anyway).

    Click-and-verify with retries: the click can race the legacy grid's
    post-filter reload — the row detaches mid-click (or the row locator times
    out) and nothing navigates — so a single 60s wait would just expire."""
    last: Exception | None = None
    for _ in range(4):
        if page.query_selector("select[name=status_id]"):  # edit form is up
            page.wait_for_timeout(_SETTLE_MS)
            return
        try:
            _row_with_cell(page, review_id).click(timeout=15000)
            page.wait_for_selector("select[name=status_id]", timeout=15000)
            page.wait_for_timeout(_SETTLE_MS)
            return
        except (PWTimeout, RuntimeError) as exc:
            # RuntimeError covers the OS actuator's 'element has no bounding
            # box' when the row detaches between its visibility wait and the
            # coordinate read — the same mid-reload race as a click timeout.
            last = exc
    raise last


def review_rating(page) -> int:
    """Star rating (1-5) from the review edit form. Prefer the checked star
    radio (ids end in the star ordinal); fall back to the rendered rating bar's
    percentage width (20% per star)."""
    for radio in page.query_selector_all(
            "#detailed_rating input[type=radio], .field-detailed-rating input[type=radio]"):
        if radio.is_checked():
            m = re.search(r"([1-5])$", radio.get_attribute("id") or "")
            if m:
                return int(m.group(1))
    bar = page.query_selector(".rating-result > span, .rating-result span")
    if bar:
        m = re.search(r"(\d+)\s*%", bar.get_attribute("style") or "") or \
            re.search(r"(\d+)\s*%", bar.inner_text() or "")
        if m:
            return max(1, round(int(m.group(1)) / 20))
    raise RuntimeError("could not read the review's star rating")


def approve_review(page) -> None:
    _moderate_review(page, "Approved")


def reject_review(page) -> None:
    _moderate_review(page, "Not Approved")


def _moderate_review(page, status_label: str) -> None:
    # Entry-guard: a re-run after the save landed (OSOps retries on a verify
    # miss) finds the grid again, not the edit form — nothing left to do.
    if page.query_selector("select[name=status_id]") is None:
        return
    page.select_option("select[name=status_id]", label=status_label)
    page.click("#save_button, #save, button[title='Save Review']")
    # saving returns to the grid with a success banner
    page.wait_for_selector(".message-success, table.data-grid", timeout=30000)
    page.wait_for_timeout(_SETTLE_MS)


# --- products --------------------------------------------------------------

def goto_products(page, app_url: str) -> None:
    page.goto(f"{app_url}/admin/catalog/product/", wait_until="networkidle")


def open_product(page, sku: str) -> None:
    """Open a product's edit form from the grid via its row's Edit link (edit
    URLs key on the entity id, which the grid exposes only in that link)."""
    row = _row_with_cell(page, sku)
    if not row.count():
        # The catalog contains more rows than the UI component renders on one
        # page. Filter by the exact SKU so task-owned organic examples remain
        # reachable without depending on the grid's current sort or page.
        filters = page.locator(
            "button[data-action='grid-filter-expand'], button:has-text('Filters')"
        ).first
        if filters.count():
            filters.click()
        page.fill("input[name='sku']", sku)
        apply_button = page.locator(
            "button[data-action='grid-filter-apply'], button:has-text('Apply Filters')"
        ).first
        apply_button.click()
        row = _row_with_cell(page, sku)
        # Applying a Magento UI-component filter is asynchronous. The old grid
        # can remain visible briefly before its loading mask appears, so the
        # generic grid reader may otherwise return the stale rows too early.
        row.wait_for(state="visible", timeout=30000)
    if not row.count():
        raise RuntimeError(f"product SKU not found in grid: {sku}")
    link = row.locator("a", has_text=re.compile("edit", re.I))
    (link.first if link.count() else row).click()
    page.wait_for_selector("input[name='product[price]']", timeout=60000)
    page.wait_for_timeout(_SETTLE_MS)


def product_enabled(page) -> bool:
    """The 'Enable Product' switch is a checkbox under the hood."""
    return page.is_checked("input[name='product[status]']")


def product_qty(page) -> float:
    val = page.input_value("input[name='product[quantity_and_stock_status][qty]']")
    return float(val.replace(",", "")) if val.strip() else 0.0


def product_stock_status(page) -> str:
    val = page.input_value("select[name='product[quantity_and_stock_status][is_in_stock]']")
    return "In Stock" if val == "1" else "Out of Stock"


def set_stock_status(page, in_stock: bool) -> None:
    page.select_option("select[name='product[quantity_and_stock_status][is_in_stock]']",
                       "1" if in_stock else "0")
    save_product(page)


def product_prices(page) -> dict:
    """{'price', 'special_price'} for the open product, floats or None. The
    special price lives in the Advanced Pricing modal, so open, read, close."""
    price = parse_price(page.input_value("input[name='product[price]']"))
    # Configurable/grouped products in the stock WebArena catalog expose the
    # Advanced Pricing control disabled.  They have no product-level special
    # price to inspect, so treat them like an ordinary no-special-price row
    # instead of blocking the whole organic-data scan on an unclickable button.
    advanced = page.locator("button[data-index=advanced_pricing_button]")
    if not advanced.is_visible() or not advanced.is_enabled():
        return {"price": price, "special_price": None}
    _open_advanced_pricing(page)
    special = parse_price(page.input_value("input[name='product[special_price]']"))
    _close_advanced_pricing(page)
    return {"price": price, "special_price": special}


def remove_special_price(page) -> None:
    _open_advanced_pricing(page)
    page.fill("input[name='product[special_price]']", "")
    _close_advanced_pricing(page)
    save_product(page)


def flag_product(page, note: str) -> None:
    """Magento products have no admin-notes field, so the closest low-touch
    flag is stamping the note into the SEO meta description: invisible in the
    storefront UI, harmless, and easy for a reviewing human to find. Prices
    are untouched."""
    _open_section(page, "search-engine-optimization")
    page.fill("textarea[name='product[meta_description]']", note)
    save_product(page)


def save_product(page) -> None:
    page.click("#save-button")
    # Wait for EITHER banner — a configurable parent (no qty/stock of its own)
    # or a validation failure yields an error, not success; surface it instead
    # of hanging on a success banner that will never come.
    page.wait_for_selector(".message-success, .message-error", timeout=45000)
    err = page.query_selector(".message-error")
    if err:
        raise RuntimeError(f"product save failed: {(err.inner_text() or '').strip()[:140]}")
    page.wait_for_timeout(_SETTLE_MS)


def _open_advanced_pricing(page) -> None:
    page.click("button[data-index=advanced_pricing_button]")
    page.wait_for_selector("input[name='product[special_price]']", timeout=15000)
    page.wait_for_timeout(_SETTLE_MS)


def _close_advanced_pricing(page) -> None:
    # 'Done' commits the modal fields back into the form model
    # In this Magento snapshot Advanced Pricing is rendered as a slide-out;
    # its container does not receive Magento's usual ``_show`` modal class.
    done = page.locator("button.action-primary:visible", has_text="Done")
    if not done.count():
        raise RuntimeError("Advanced Pricing Done button not found")
    done.first.click()
    page.wait_for_timeout(_SETTLE_MS)


def _open_section(page, index: str) -> None:
    """Expand a collapsed product-form section (they render collapsed and
    their inputs are unfillable until opened)."""
    section = page.locator(f"div[data-index={index}]").first
    section.scroll_into_view_if_needed()
    if not section.locator("textarea, input, select").first.is_visible():
        section.locator(".fieldset-wrapper-title, [data-state-collapsible]").first.click()
        page.wait_for_timeout(_SETTLE_MS)


# --- text parsing (Magento's grid rendering formats) -------------------------

def parse_price(text: str | None) -> float | None:
    """Number out of a Magento-rendered price ('$1,299.99'), or None."""
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", text or "")
    return float(m.group(0).replace(",", "")) if m else None


_DATE_FORMATS = (
    "%b %d, %Y %I:%M:%S %p",   # Jul 1, 2023 10:15:32 AM   (2.3 grid default)
    "%b %d, %Y, %I:%M:%S %p",  # Jul 1, 2023, 10:15:32 AM  (2.4 adds a comma)
    "%b %d, %Y %I:%M %p",
    "%b %d, %Y, %I:%M %p",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%y %I:%M %p",
    "%Y-%m-%d %H:%M:%S",
)


def parse_grid_date(text: str | None) -> datetime | None:
    """Datetime from an admin-grid date cell, or None. Magento renders grid
    dates in the admin locale's medium format, which varies across 2.x — try
    the known shapes rather than assuming one."""
    cleaned = " ".join((text or "").split())
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


def age_days(purchased: datetime, now: datetime | None = None) -> float:
    """Age of a grid timestamp in days. Grid times are store-local with no
    zone marker, so compare against naive local now."""
    now = now or datetime.now()
    return (now - purchased).total_seconds() / 86400.0
