"""Playwright operations for the genuine Magento storefront."""
from __future__ import annotations

import re

from showAndTell.applications.browser import context as browser_context
PRODUCTS = {
    "hush-anc": {
        "sku": "showAndTell-hush-anc",
        "name": "Wavecrest Hush ANC Wireless Bluetooth Over-Ear Headphones",
        "url_key": "showAndTell-hush-anc",
        "seller": "Cartline Retail Private Ltd",
    },
}
FINAL_PRICE = '[data-price-type="finalPrice"]'


def login(page, app_url: str, creds: dict) -> None:
    browser_context.bind(page, creds)
    base = app_url.rstrip("/")
    page.goto(
        f"{base}/customer/account/login/",
        wait_until="domcontentloaded",
    )
    email = page.get_by_label(re.compile(r"^email", re.IGNORECASE))
    if not email.count() or not email.first.is_visible():
        page.goto(f"{base}/customer/account/logout/", wait_until="networkidle")
        page.goto(
            f"{base}/customer/account/login/", wait_until="domcontentloaded"
        )
        email = page.get_by_label(re.compile(r"^email", re.IGNORECASE))
    email.fill(creds["email"])
    page.get_by_label(re.compile(r"^password", re.IGNORECASE)).fill(
        creds["password"]
    )
    page.get_by_role(
        "button", name=re.compile(r"^sign in$", re.IGNORECASE)
    ).click()
    page.wait_for_url(re.compile(r"/(customer/account/)?$"))
    setattr(page, "_showAndTell_shopfix_password", creds["password"])


def open_home(page, app_url: str) -> None:
    setattr(page, "_showAndTell_shopfix_app_url", app_url.rstrip("/"))
    page.goto(f"{app_url.rstrip('/')}/", wait_until="networkidle")
    page.get_by_role("main").wait_for()


def open_product(page, product_id: str) -> None:
    try:
        product = PRODUCTS[product_id]
    except KeyError as exc:
        raise ValueError(f"unknown seeded Magento product {product_id!r}") from exc

    product_link = page.locator(
        f'a.product-item-link[href*="/{product["url_key"]}.html"]'
    )
    if not product_link.count():
        search = page.get_by_role("combobox", name=re.compile("search", re.IGNORECASE))
        if not search.count():
            search = page.get_by_role(
                "searchbox", name=re.compile("search", re.IGNORECASE)
            )
        search.first.fill(product["sku"])
        search.first.press("Enter")
        page.locator(".products-grid, .search.results").first.wait_for()
        product_link = page.get_by_role(
            "link", name=product["name"], exact=True
        )
    if product_link.count():
        product_link.first.click()
    else:
        # Magento's search index is asynchronous. The seeded url_key is a
        # native catalog identity, so a direct product route is the honest
        # deterministic fallback when the index has not caught up yet.
        base = str(getattr(page, "_showAndTell_shopfix_app_url", "")).rstrip("/")
        if not base:
            raise RuntimeError("Magento base URL was not recorded by open_home")
        page.goto(
            f"{base}/{product['url_key']}.html", wait_until="networkidle"
        )
    page.locator(".product-info-main").wait_for()
    setattr(page, "_showAndTell_shopfix_product_id", product_id)
    setattr(page, "_showAndTell_shopfix_product_url", page.url)


def click_price(page) -> None:
    page.locator(FINAL_PRICE).first.click()


def _amount(locator) -> str:
    raw = locator.get_attribute("data-price-amount")
    if raw:
        return raw
    amount = locator.locator("[data-price-amount]").first
    return amount.get_attribute("data-price-amount") or ""


def read_product(page) -> dict:
    product_id = str(getattr(page, "_showAndTell_shopfix_product_id", ""))
    meta = PRODUCTS.get(product_id, {})
    final = page.locator(FINAL_PRICE).first
    regular = page.locator('[data-price-type="oldPrice"]').first
    stock = page.locator(".stock.available, .stock.unavailable").first
    price = _amount(final)
    return {
        "id": product_id,
        "title": page.locator(".page-title [itemprop='name'], h1.page-title").first
        .inner_text().strip(),
        "price": price,
        "mrp": _amount(regular) if regular.count() else "",
        "price_display": final.inner_text().strip(),
        "seller": meta.get("seller", ""),
        "stock": stock.inner_text().strip() if stock.count() else "",
        "url": str(getattr(page, "_showAndTell_shopfix_product_url", page.url)),
    }


def read_price(page) -> float | None:
    try:
        return float(_amount(page.locator(FINAL_PRICE).first))
    except (TypeError, ValueError):
        return None


def add_to_cart(page) -> None:
    page.get_by_role(
        "button", name=re.compile(r"^add to cart$", re.IGNORECASE)
    ).click()
    page.locator("[data-ui-id='message-success'], .message-success").first.wait_for()


def read_cart_added(page) -> dict:
    subtotal = page.locator(".minicart-wrapper .subtotal .price").first
    count = page.locator(".minicart-wrapper .counter-number").first
    return {
        "subtotal": subtotal.inner_text().strip() if subtotal.count() else "",
        "colour": "",
        "count": count.inner_text().strip() if count.count() else "1",
    }


def _retired_messaging_surface() -> None:
    raise RuntimeError(
        "the messaging surface for this archived workflow has been retired"
    )


def open_slack(page, app_url: str) -> None:
    del page, app_url
    _retired_messaging_surface()


def post_alert(page, text: str) -> None:
    del page, text
    _retired_messaging_surface()


def list_slack_messages(page) -> list[dict]:
    del page
    _retired_messaging_surface()


__all__ = [
    "add_to_cart",
    "click_price",
    "list_slack_messages",
    "login",
    "open_home",
    "open_product",
    "open_slack",
    "post_alert",
    "read_cart_added",
    "read_price",
    "read_product",
]
