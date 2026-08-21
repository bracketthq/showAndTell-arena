"""Magento 2 Luma storefront browser operations for demonstrations.

Each shopping task's `demonstrate.py` composes these primitives, so the UI
selectors live in exactly one place. They target the stock Magento 2 Luma
theme WebArena's OneStopMarket image ships (customer login at
/customer/account/login with #email/#pass, catalog search at
/catalogsearch/result/, li.product-item grids, #product-addtocart-button,
a.towishlist, #my-orders-table). Unlike gitlab_ui, these selectors are written
from Luma's documented markup but are NOT yet verified against the live
WebArena `shopping_final_0712` image — treat them as best-effort until a live
pass confirms them.
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import quote_plus

_SETTLE_MS = 800


def login(page, app_url: str, creds: dict, timeout: int = 15000) -> None:
    # Idempotent: the managed Chrome profile persists the session across runs,
    # so /customer/account/login may redirect to the dashboard with no form.
    page.goto(f"{app_url}/customer/account/login/", wait_until="networkidle")
    if page.query_selector("#email") is None:
        return  # already signed in
    page.fill("#email", creds["email"])
    page.fill("#pass", creds["password"])
    page.click("#send2, .action.login[type=submit]")
    # Exit-verify: the header's Sign Out link only renders for an authenticated
    # session. Without this, a missed OS-input click on Sign In leaves the demo
    # silently signed out (My Orders then bounces to the login form and reads
    # as an empty history).
    page.wait_for_selector("a[href*='customer/account/logout']", timeout=timeout)


def search(page, app_url: str, term: str) -> None:
    page.goto(f"{app_url}/catalogsearch/result/?q={quote_plus(term)}",
              wait_until="networkidle")
    page.wait_for_timeout(_SETTLE_MS)


def _parse_price(text: str | None) -> float | None:
    m = re.search(r"[\d,]+(?:\.\d+)?", text or "")
    return float(m.group(0).replace(",", "")) if m else None


def _parse_rating(text: str | None) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text or "")
    return float(m.group(1)) if m else None


def search_results(page) -> list[dict]:
    """{"name", "price", "in_stock", "rating_pct"} per tile on the current
    results page. Luma only marks out-of-stock tiles (div.stock.unavailable),
    so an unmarked tile is purchasable; rating_pct is None for unrated items."""
    items: list[dict] = []
    for li in page.query_selector_all("li.product-item"):
        link = li.query_selector("a.product-item-link")
        if link is None:
            continue
        price_el = li.query_selector("[data-price-type=finalPrice] .price") or \
            li.query_selector(".price")
        rating_el = li.query_selector(".rating-result")
        items.append({
            "name": (link.inner_text() or "").strip(),
            "price": _parse_price(price_el.inner_text() if price_el else None),
            "in_stock": li.query_selector(".stock.unavailable") is None,
            "rating_pct": _parse_rating(rating_el.get_attribute("title")
                                        if rating_el else None),
        })
    return items


def open_product(page, name: str) -> None:
    """Open a product from the current grid by its tile name, and wait until the
    product page has actually arrived. `wait_for_load_state('networkidle')` alone
    can settle on the *pre-navigation* search page (the click's navigation hasn't
    started yet) — seen under the slower, instrumented Teach-recording tab, where
    it left the demo on the results page and add-to-cart then found no product.
    The add-to-cart button is the product page's arrival signal."""
    page.locator("a.product-item-link",
                 has_text=re.compile(re.escape(name), re.I)).first.click()
    page.wait_for_selector("#product-addtocart-button", timeout=30000)
    page.wait_for_load_state("networkidle")


def product_price(page) -> float | None:
    el = page.query_selector(".product-info-main [data-price-type=finalPrice] .price") or \
        page.query_selector(".product-info-main .price")
    return _parse_price(el.inner_text()) if el else None


def product_in_stock(page) -> bool:
    # Luma always renders one of the two stock badges on the product page.
    if page.query_selector(".product-info-main .stock.unavailable"):
        return False
    return page.query_selector(".product-info-main .stock.available") is not None


def product_rating_pct(page) -> float | None:
    """Summary rating as 0-100 (Luma encodes it in the .rating-result title,
    e.g. title='80%'), or None for an unrated product."""
    el = page.query_selector(".product-info-main .rating-result")
    if el is None:
        return None
    return _parse_rating(el.get_attribute("title") or el.inner_text())


def _select_required_options(page) -> None:
    """Many WebArena products are configurable and require choosing each option
    (Color/Size) before Add to Cart, or it errors 'This is a required field'.
    The catalog renders them mostly as radios, sometimes swatches/dropdowns —
    pick the first available in each group.

    Actuation goes through page.locator(...), never ElementHandles, so the
    OS-input proxy (codex-record) can route each click as a real gesture.
    Magento style-hides the option radio behind its label, so the click targets
    the label — the visible hit-target a by-coordinate click can land on.
    Chosen groups are skipped (entry-guard) and each click is awaited
    (exit-verify), which makes an OSOps re-run of the primitive safe."""
    seen: set[str] = set()
    for radio in page.query_selector_all(
            ".product-options-wrapper input[type=radio], "
            "#product-options-wrapper input[type=radio], "
            ".product-add-form input[type=radio]"):
        name = radio.get_attribute("name")
        if not name or name in seen:
            continue
        seen.add(name)
        group = f'input[type=radio][name="{name}"]'
        if page.query_selector(f"{group}:checked"):
            continue
        rid = radio.get_attribute("id")
        if rid and page.query_selector(f'label[for="{rid}"]'):
            page.locator(f'label[for="{rid}"]').first.click()
        else:
            page.locator(group).first.click(force=True)
        page.wait_for_selector(f"{group}:checked", state="attached", timeout=5000)
    swatches = page.locator("div.swatch-attribute")
    for i in range(swatches.count()):
        sw = swatches.nth(i)
        if sw.locator(".swatch-option.selected").count():
            continue
        opts = sw.locator(".swatch-option:not([disabled])")
        if opts.count():
            opts.first.click()
            sw.locator(".swatch-option.selected").first.wait_for(
                state="attached", timeout=5000)
    dds = page.locator("select.super-attribute-select")
    for i in range(dds.count()):
        dd = dds.nth(i)
        if dd.evaluate("el => !!el.value"):
            continue  # entry-guard: this attribute is already chosen
        label = dd.evaluate(
            "el => { const o = Array.from(el.options).find(o => o.value);"
            " return o ? o.label : null; }")
        if label:
            dd.select_option(label=label)


def add_to_cart(page, qty: int = 1) -> None:
    _select_required_options(page)
    if qty != 1:
        page.fill("#qty", str(qty))
    page.click("#product-addtocart-button")
    # Magento answers with a page-top banner either way; an error banner (e.g.
    # "The requested qty is not available") means the add failed — surface its
    # message instead of timing out waiting for a success that never comes.
    banner = page.wait_for_selector(
        ".message-success, .messages .success, .message-error", timeout=15000)
    if "error" in (banner.get_attribute("class") or ""):
        raise RuntimeError(
            f"add to cart failed: {(banner.inner_text() or '').strip()}")


def add_to_wishlist(page) -> None:
    # Luma redirects to /wishlist/ with a success banner after the add.
    page.click("a.towishlist, .action.towishlist")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(_SETTLE_MS)


def wishlist_names(page, app_url: str) -> list[str]:
    page.goto(f"{app_url}/wishlist/", wait_until="networkidle")
    return [(e.inner_text() or "").strip()
            for e in page.query_selector_all(
                ".form-wishlist-items .product-item-name, "
                ".products-grid.wishlist .product-item-name")
            if (e.inner_text() or "").strip()]


def cart_names(page, app_url: str) -> list[str]:
    page.goto(f"{app_url}/checkout/cart/", wait_until="networkidle")
    return [(e.inner_text() or "").strip()
            for e in page.query_selector_all(
                "#shopping-cart-table .product-item-name")
            if (e.inner_text() or "").strip()]


def _iso_date(text: str) -> str:
    """Order-history dates as ISO yyyy-mm-dd so task rules can compare them
    lexicographically. Luma renders locale short dates (e.g. 3/1/23)."""
    text = (text or "").strip()
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return text


def orders(page, app_url: str, timeout: int = 15000) -> list[dict]:
    """{"id", "date" (ISO), "status"} per row of My Orders, newest first.
    Magento paginates at 10 rows; the task rules only need the first page."""
    page.goto(f"{app_url}/sales/order/history/", wait_until="networkidle")
    # Arrival signal: the grid, or Magento's explicit no-orders message. A
    # signed-out bounce to the login form has neither — fail loudly
    # (retryably) rather than read the bounce as an empty history.
    page.wait_for_selector("#my-orders-table, .message.info.empty",
                           timeout=timeout)
    out: list[dict] = []
    for row in page.query_selector_all("#my-orders-table tbody tr"):
        def col(name: str) -> str:
            el = row.query_selector(f"td.col.{name}")
            return (el.inner_text() or "").strip() if el else ""
        out.append({"id": col("id").lstrip("#"),
                    "date": _iso_date(col("date")),
                    "status": col("status")})
    return out


def reorder(page, app_url: str, order_id: str) -> None:
    """Click Reorder on the My Orders row for `order_id`; Magento puts the
    order's items back in the cart."""
    page.goto(f"{app_url}/sales/order/history/", wait_until="networkidle")
    row = page.locator(f"#my-orders-table tbody tr:has(td.col.id:has-text('{order_id}'))")
    row.locator("a.action.order").first.click()
    page.wait_for_load_state("networkidle")
