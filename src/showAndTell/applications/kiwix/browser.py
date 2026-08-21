"""Kiwix / offline-Wikipedia browser operations for demonstrations.

The `wikipedia` fixture is Kiwix 3.3 serving WebArena's read-only
`wikipedia_en_all_maxi_2022-05.zim` snapshot — no accounts, no writes. Each
wikipedia task's `demonstrate.py` composes these primitives, so the Kiwix URL
scheme and the article selectors live in exactly one place.

Verification status: verified against a live Kiwix 3.3.0 serving a modern
(2026) `wikipedia_en_all_mini` zim. The article selectors (`#firstHeading`,
`#mw-content-text`, `table.infobox` with `th.infobox-label`/`td.infobox-data`
rows) match the mirrored Wikipedia markup. Kiwix 3.3's URL scheme for a
single-book server is `/{book}/{Title}` (no `/A/` namespace on modern zims) and
search is `/search?pattern=…` (a `books.name` param 400s on a single book) — the
book is the zim's filename stem. `BOOK` derives from SHOWANDTELL_WIKIPEDIA_ZIM so
the UI and the fixture (which serves that zim) stay in sync.
"""
from __future__ import annotations

import os
import re
from urllib.parse import quote, urljoin
from xml.etree import ElementTree

DEFAULT_ZIM = "wikipedia_en_all_maxi_2022-05.zim"
BOOK = os.environ.get("SHOWANDTELL_WIKIPEDIA_ZIM", DEFAULT_ZIM).removesuffix(".zim")
_BOOKS: dict[str, str] = {}

_REFERS_TO = re.compile(r"\brefers?\s+to\b", re.IGNORECASE)


def _catalog_book(root) -> str:
    """Extract Kiwix's routable book id, not OPDS's generic archive name."""
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] != "link":
            continue
        if el.attrib.get("type") == "text/html" and el.attrib.get("href"):
            return el.attrib["href"].rstrip("/").rsplit("/", 1)[-1]
    return ""


def login(page, app_url: str, creds: dict) -> None:
    """No-op: Kiwix serves a read-only archive with no accounts, and the
    fixture's creds are empty. Kept so every webarena demonstrate driver
    starts with the same ops.login(...) call — the adapters (and codex's
    OS-input driver) rely on that uniform call sequence."""


def goto_home(page, app_url: str) -> None:
    page.goto(f"{app_url}/", wait_until="load")


def discover_book(page, app_url: str) -> str:
    """Return the book ID actually served by this Kiwix instance.

    Reused VM containers may serve a newer archive than the process environment
    names. Kiwix's OPDS catalog is the server authority; the environment-derived
    BOOK remains only a fallback for older servers without that endpoint.
    """
    origin = app_url.rstrip("/")
    if origin in _BOOKS:
        return _BOOKS[origin]
    try:
        response = page.request.get(f"{origin}/catalog/v2/entries")
        if response.ok:
            root = ElementTree.fromstring(response.text())
            book = _catalog_book(root)
            if book:
                _BOOKS[origin] = book
                return book
    except Exception:
        pass
    _BOOKS[origin] = BOOK
    return BOOK


def goto_article(page, app_url: str, title: str) -> None:
    """Open an article by title. Modern kiwix single-book URL scheme is
    /{book}/{Title} (no /A/ namespace)."""
    book = discover_book(page, app_url)
    page.goto(f"{app_url}/{book}/{title.replace(' ', '_')}", wait_until="load")


_SEARCH_BOX = "#kiwixsearchbox, input[name='pattern']"


def search(page, app_url: str, pattern: str) -> list[dict]:
    """Search by TYPING in the Kiwix search box and submitting — the way a
    person does it, so the recorder captures a real interaction rather than a
    silent URL jump. Returns result links [{"title","href"}], best match first.
    Falls back to the /search URL only if no search box is on the page yet."""
    book = discover_book(page, app_url)
    box = page.query_selector(_SEARCH_BOX)
    if box is not None:
        box.fill(pattern)
        box.press("Enter")
        page.wait_for_load_state("load")
    else:
        page.goto(f"{app_url}/search?pattern={quote(pattern)}", wait_until="load")
    anchors = page.query_selector_all(f".results a[href*='/{book}/']") or \
        page.query_selector_all(f"a[href*='/{book}/']")
    results: list[dict] = []
    for a in anchors:
        title = (a.inner_text() or "").strip()
        href = a.get_attribute("href") or ""
        if title and href and f"/{book}/" in href:
            results.append({"title": title, "href": href})
    return results


def _click_href(page, app_url: str, href: str) -> None:
    """Click the on-page link with this href (a captured navigation); fall back
    to a direct visit only if the link isn't present. Matches by attribute value
    (not a CSS selector) so an href containing quotes/apostrophes — common in
    article titles like "Canada_men's_…" — can't break selector parsing."""
    target = href.rstrip("/")
    tail = target.rsplit("/", 1)[-1]
    for a in page.query_selector_all("a[href]"):
        h = (a.get_attribute("href") or "").rstrip("/")
        if h == target or h.rsplit("/", 1)[-1] == tail:
            a.click()
            page.wait_for_load_state("load")
            return
    url = href if href.startswith("http") else (
        f"{app_url}{href}" if href.startswith("/") else urljoin(page.url, href))
    page.goto(url, wait_until="load")


def open_result(page, app_url: str, result: dict) -> None:
    """Open a search result by CLICKING its link on the results page."""
    _click_href(page, app_url, result["href"])


def open_entry(page, app_url: str, entry: dict) -> None:
    """Open a disambiguation entry by CLICKING its link (routing to the chosen
    sense the way a reader would)."""
    _click_href(page, app_url, entry["href"])


def look_up(page, app_url: str, title: str) -> None:
    """Reach an article by typing its name in the search box and CLICKing the
    EXACT-title result. Kiwix full-text search is fuzzy (searching "Canada"
    surfaces "Canada men's junior hockey team" etc.), so a non-exact top hit is
    the wrong article — fall back to the article's own path, which resolves
    directly, rather than opening whatever ranked first."""
    want = title.strip().lower()
    results = search(page, app_url, title)
    best = next((r for r in results if r["title"].strip().lower() == want), None)
    if best is not None:
        open_result(page, app_url, best)
        # Kiwix search snippets can label a fuzzy hit with the query text even
        # when its href targets another article. Verify the landing rather than
        # trusting the result label.
        if article_title(page).strip().lower() == want:
            return
    # Search is still performed as the recorded human gesture, but the archive's
    # canonical article route is authoritative when exact search is absent or
    # mislabeled.
    if article_title(page).strip().lower() != want:
        goto_article(page, app_url, title)


def follow_infobox_link(page, label: str) -> bool:
    """Click the link in the infobox row whose header matches `label` (e.g.
    'Country'), navigating to that article as a reader would. Returns False when
    the row has no link (so the caller can fall back)."""
    for row in page.query_selector_all("table.infobox tr"):
        th = row.query_selector("th")
        if th is None or label.lower() not in (th.inner_text() or "").strip().lower():
            continue
        link = row.query_selector("td a[href]")
        if link is not None:
            link.click()
            page.wait_for_load_state("load")
            return True
    return False


def article_title(page) -> str:
    for sel in ("#firstHeading", "h1"):
        el = page.query_selector(sel)
        if el and (el.inner_text() or "").strip():
            return el.inner_text().strip()
    return ""


def article_exists(page) -> bool:
    """False on Kiwix's 404 page (h1 'Not Found', body 'Content not found'),
    True on a real article."""
    title = article_title(page)
    if not title or "not found" in title.lower():
        return False
    body = page.inner_text("body") or ""
    return "content not found" not in body[:500].lower()


def is_disambiguation(page) -> bool:
    """Disambiguation pages carry a .dmbox/#disambigbox marker in the mirrored
    markup, a '(disambiguation)' title suffix, or a '… may refer to' lead —
    the lead check is a fallback heuristic, checked last."""
    if page.query_selector(".dmbox, #disambigbox"):
        return True
    if article_title(page).lower().endswith("(disambiguation)"):
        return True
    return bool(_REFERS_TO.search(lead_paragraph(page)))


def disambiguation_entries(page) -> list[dict]:
    """The page's list entries as [{"title", "descriptor", "href"}]: title and
    href from each list item's first link, descriptor the remaining item text —
    the domain hint (e.g. 'a programming language') the routing rule matches
    against, and href so the chosen sense can be opened by a click."""
    items = page.query_selector_all("#mw-content-text ul li") or \
        page.query_selector_all("ul li")
    entries: list[dict] = []
    for li in items:
        link = li.query_selector("a[href]")
        if link is None:
            continue
        title = (link.inner_text() or "").strip()
        if not title:
            continue
        text = (li.inner_text() or "").strip()
        descriptor = text[len(title):] if text.startswith(title) else text
        entries.append({"title": title, "href": link.get_attribute("href") or "",
                        "descriptor": descriptor.strip(" ,;:–—-\n\t")})
    return entries


def infobox_field(page, label: str) -> str | None:
    """Value text of the infobox row whose header matches `label` (e.g.
    'Capital', 'Population', 'Country', 'Location'), or None when the row is
    absent. Case-insensitive substring match so nested sub-rows like
    '• Metro' resolve too."""
    section = False
    for row in page.query_selector_all("table.infobox tr"):
        th, td = row.query_selector("th"), row.query_selector("td")
        heading = (th.inner_text() or "").strip() if th is not None else ""
        if label.lower() in heading.lower():
            if td is not None:
                return (td.inner_text() or "").strip() or None
            # Country infoboxes render headings such as Population in a
            # colspan-only row, followed by estimate/census rows. Return the
            # first value row together with its dated sub-label.
            section = True
            continue
        if not section:
            continue
        if th is not None and "infobox-header" in (th.get_attribute("class") or ""):
            return None
        if td is not None:
            value = (td.inner_text() or "").strip()
            if value:
                return f"{heading} {value}".strip()
    return None


def lead_paragraph(page) -> str:
    """First non-empty content paragraph — the article's lead prose."""
    paras = page.query_selector_all("#mw-content-text p") or page.query_selector_all("p")
    for p in paras:
        text = (p.inner_text() or "").strip()
        if text:
            return text
    return ""
