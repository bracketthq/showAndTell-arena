"""Browser tests for ERPNext's browser plane (slow: chromium).

``ready`` exists because a Frappe list renders one page of rows whose size is a
per-user preference absent from an exported site. These pin the behaviour that
matters to a replay: a truncated list is expanded before gestures land on it,
and a list that is already showing everything is not touched.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from showAndTell.applications.erpnext import browser as erp

pytestmark = pytest.mark.slow


def _list_page(rendered: int, *, total: int = 150, paging: bool = True) -> str:
    """A Frappe list showing ``rendered`` of ``total`` rows.

    Its page-size buttons behave like Desk's: choosing one re-renders the list
    with that many rows, capped by how many the list actually holds.
    """
    rows = "".join(
        f'<div class="list-row-container">'
        f'<input class="list-row-checkbox" data-name="SAL-ORD-2026-{i:05d}">'
        f"</div>"
        for i in range(1, rendered + 1)
    )
    paging_html = (
        """
        <div class="list-paging-area">
          <button type="button">20</button>
          <button type="button">100</button>
          <button type="button">500</button>
          <button type="button">2500</button>
        </div>
        """
        if paging
        else ""
    )
    return f"""
    <div class="frappe-list" id="rows">{rows}</div>
    {paging_html}
    <script>
      window.__clicked = [];
      const TOTAL = {total};
      document.querySelectorAll('.list-paging-area button').forEach(b => {{
        b.addEventListener('click', () => {{
          window.__clicked.push(b.textContent.trim());
          const want = Math.min(parseInt(b.textContent.trim(), 10), TOTAL);
          const host = document.getElementById('rows');
          host.innerHTML = '';
          for (let i = 1; i <= want; i++) {{
            const d = document.createElement('div');
            d.className = 'list-row-container';
            const c = document.createElement('input');
            c.className = 'list-row-checkbox';
            c.dataset.name = 'SAL-ORD-2026-' + String(i).padStart(5, '0');
            d.appendChild(c);
            host.appendChild(d);
          }}
        }});
      }});
    </script>
    """


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        chromium = pw.chromium.launch()
        yield chromium
        chromium.close()


@pytest.fixture
def page(browser):
    # Per test: ``set_content`` replaces the document but keeps the window, so
    # a shared page would carry the previous test's recorded clicks into this
    # one's assertions.
    page = browser.new_page()
    yield page
    page.close()


def test_a_truncated_list_is_expanded_to_the_recorded_page_size(page):
    """A row past the default page is what the recorded click needs to hit."""
    page.set_content(_list_page(erp.DEFAULT_PAGE_LENGTH, total=150))
    missing = 'input.list-row-checkbox[data-name="SAL-ORD-2026-00093"]'
    assert page.locator(missing).count() == 0

    erp.ready(page)

    assert page.evaluate("window.__clicked") == ["100"]
    assert page.locator(erp.LIST_ROWS).count() == 100
    assert page.locator(missing).count() == 1


def test_a_list_showing_everything_it_holds_is_left_alone(page):
    """Expanding costs a refetch, so a short list must not pay for it."""
    page.set_content(_list_page(5, total=5))

    erp.ready(page)

    assert page.evaluate("window.__clicked") == []
    assert page.locator(erp.LIST_ROWS).count() == 5


def test_a_surface_with_no_list_is_not_a_failure(page):
    """``ready`` runs on every ERPNext surface, most of which are not lists."""
    page.set_content("<div class='layout-main'>a form, not a list</div>")

    erp.ready(page)  # must not raise

    assert page.locator(erp.LIST_ROWS).count() == 0
