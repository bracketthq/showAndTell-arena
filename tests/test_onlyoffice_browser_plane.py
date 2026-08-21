"""Browser tests for ONLYOFFICE's browser plane (slow: chromium).

The plane drives a spreadsheet the way a replay does, so it is pinned against
the DOM shapes Document Server actually serves: the Name Box changed from a
wrapper to a bare input, and DS raises its own warnings as modal alerts that
silently swallow cell navigation while leaving every selector resolvable.
"""
from __future__ import annotations

import html

import pytest
from playwright.sync_api import sync_playwright

from showAndTell.applications.onlyoffice import browser as oo
from showAndTell.demonstration.compiler import render_demonstrate
from showAndTell.player import replay

pytestmark = pytest.mark.slow

GRID = ('<div id="ws-canvas-outer"><canvas id="ws-canvas" width="400"'
        ' height="300"></canvas></div>')

# Document Server 9.4: #ce-cell-name IS the input.
CURRENT_EDITOR = GRID + """
<input id="ce-cell-name" type="text" autocomplete="off">
<textarea id="ce-cell-content"></textarea>
<textarea id="area_id"></textarea>
<script>
  window.__navigated = [];
  document.getElementById('ce-cell-name').addEventListener('keydown', e => {
    if (e.key === 'Enter') window.__navigated.push(e.target.value);
  });
</script>
"""

# Older Document Server: the box wraps its input.
LEGACY_EDITOR = GRID + """
<div id="ce-cell-name"><input type="text" autocomplete="off"></div>
<textarea id="ce-cell-content"></textarea>
<textarea id="area_id"></textarea>
<script>
  window.__navigated = [];
  document.querySelector('#ce-cell-name input').addEventListener('keydown', e => {
    if (e.key === 'Enter') window.__navigated.push(e.target.value);
  });
</script>
"""

# A warning DS raises on load ("This file is opened from a server backup
# copy…"), with the full-viewport mask that makes it modal. Its OK button
# removes it, exactly as Document Server's own does.
MODAL_ALERT = GRID + """
<input id="ce-cell-name" type="text">
<div class="modals-mask" counter="1" style="position:fixed; inset:0"></div>
<div class="asc-window modal alert" id="window-view1" role="alertdialog"
     aria-modal="true" style="position:fixed; left:100px; top:100px">
  <div class="header"><div class="title">Warning</div></div>
  <div class="body">This file is opened from a server backup copy.</div>
  <div class="footer"><button class="btn normal dlg-btn primary">OK</button></div>
</div>
<script>
  document.querySelector('.footer button').addEventListener('click', () => {
    document.getElementById('window-view1').remove();
    document.querySelector('.modals-mask').remove();
  });
</script>
"""

# A failed autosave can appear while another replay page is in front. OK is
# explicitly a request to open Download As; the title-bar close dismisses the
# warning without replacing the sheet with that blocking panel.
SAVE_FAILURE_ALERT = GRID + """
<input id="ce-cell-name" type="text">
<textarea id="area_id"></textarea>
<div class="modals-mask" counter="1" style="position:fixed; inset:0"></div>
<div class="asc-window modal alert" id="window-view2" role="alertdialog"
     aria-modal="true" style="position:fixed; left:100px; top:100px">
  <div class="header"><div class="tools"><div class="tool close"
    style="width:20px; height:20px"></div></div>
    <div class="title">Warning</div></div>
  <div class="body">The document could not be saved. When you click the
    'OK' button, you will be prompted to download the document.</div>
  <div class="footer"><button class="btn normal dlg-btn primary">OK</button></div>
</div>
<script>
  window.__downloadAsOpened = false;
  window.__navigated = [];
  const box = document.getElementById('ce-cell-name');
  box.disabled = true;
  box.addEventListener('keydown', e => {
    if (e.key === 'Enter') window.__navigated.push(e.target.value);
  });
  function dismiss() {
    document.getElementById('window-view2').remove();
    document.querySelector('.modals-mask').remove();
    box.disabled = false;
  }
  document.querySelector('.tool.close').addEventListener('click', dismiss);
  document.querySelector('.footer button').addEventListener('click', () => {
    dismiss();
    window.__downloadAsOpened = true;
    document.body.insertAdjacentHTML('beforeend',
      '<div id="panel-saveas">Download As</div>');
  });
</script>
"""

FIRST_USE_TIP = CURRENT_EDITOR + """
<div class="synch-tip-root" style="position:fixed; left:500px; top:100px">
  <div class="btn-div">Got it</div>
</div>
<script>
  document.querySelector('.btn-div').addEventListener('click', () => {
    document.querySelector('.synch-tip-root').remove();
  });
</script>
"""

# Removing the card on mouseover models it vanishing on its own after the
# visibility wait has already succeeded: the pointer reaches it, the node is
# gone before the click can land, and the click can never complete.
SELF_DISMISSING_TIP = CURRENT_EDITOR + """
<div class="synch-tip-root" style="position:fixed; left:500px; top:100px">
  <div class="btn-div">Got it</div>
</div>
<script>
  document.querySelector('.btn-div').addEventListener('mouseover', () => {
    document.querySelector('.synch-tip-root').remove();
  });
</script>
"""

EDITING_CELL = GRID + """
<input id="ce-cell-name" type="text" autocomplete="off" disabled>
<textarea id="ce-cell-content" autofocus></textarea>
<textarea id="area_id"></textarea>
<script>
  window.__commits = 0;
  window.__navigated = [];
  const box = document.getElementById('ce-cell-name');
  const content = document.getElementById('ce-cell-content');
  content.focus();
  content.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      window.__commits += 1;
      box.disabled = false;
    }
  });
  box.addEventListener('keydown', e => {
    if (e.key === 'Enter') window.__navigated.push(e.target.value);
  });
</script>
"""

# Document Server's keyboard sink is a rendered textarea under the worksheet
# canvas. It is visually covered, but Playwright's Locator.press does not use
# pointer visibility or hit-target checks: it focuses the sink and sends the
# key directly.
COVERED_KEYBOARD_SINK = """
<div id="ws-canvas-outer" style="position:relative; width:400px; height:300px">
  <div id="area_id_main" style="position:absolute; inset:0; z-index:0;
       pointer-events:none">
    <textarea id="area_id" style="position:absolute; left:0; top:0;
         width:400px; height:50px; color:transparent; background:transparent;
         pointer-events:auto"></textarea>
  </div>
  <canvas id="ws-canvas" width="400" height="300"
          style="position:absolute; inset:0; z-index:1"></canvas>
</div>
<script>
  window.__editorKeys = [];
  document.getElementById('area_id').addEventListener('keydown', event => {
    window.__editorKeys.push(event.key);
  });
</script>
"""

PASTE_EDITOR = GRID + """
<textarea id="area_id" autofocus></textarea>
<script>
  window.__pastes = 0;
  window.__cells = [];
  const area = document.getElementById('area_id');
  area.addEventListener('paste', event => {
    event.preventDefault();
    window.__pastes += 1;
    const text = event.clipboardData.getData('text/plain');
    window.__cells = text.split(/\\r?\\n/).map(row => row.split('\\t'));
  });
  area.focus();
</script>
"""


def _editor_page(pw, inner: str):
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 900, "height": 700})
    body = (f'<iframe name="frameEditor" srcdoc="{html.escape(inner, quote=True)}"'
            ' style="width:880px; height:660px; border:0"></iframe>')
    page.route(
        "http://127.0.0.1/editor",
        lambda route: route.fulfill(content_type="text/html", body=body),
    )
    page.goto("http://127.0.0.1/editor")
    page.wait_for_load_state()
    return browser, page


def _frame(page):
    return next(f for f in page.frames
                if f != page.main_frame
                and f.evaluate("() => !!document.querySelector('#ws-canvas-outer')"))


@pytest.mark.parametrize("shape", [CURRENT_EDITOR, LEGACY_EDITOR],
                         ids=["bare-input", "wrapper"])
def test_cell_navigation_reaches_the_name_box_in_both_shapes(shape):
    with sync_playwright() as pw:
        browser, page = _editor_page(pw, shape)
        oo.select_cell(page, "A5")
        navigated = _frame(page).evaluate("() => window.__navigated")
        browser.close()
    assert navigated == ["A5"], navigated


def test_reselecting_active_cell_is_idempotent_and_focuses_cell_input():
    """A captured double-click must not submit the reference as cell text."""
    with sync_playwright() as pw:
        browser, page = _editor_page(pw, CURRENT_EDITOR)
        oo.select_cell(page, "G2")
        oo.select_cell(page, "G2")
        state = _frame(page).evaluate(
            "() => ({navigated: window.__navigated, "
            "active: document.activeElement && document.activeElement.id})")
        browser.close()
    assert state == {"navigated": ["G2"], "active": "area_id"}


def test_locator_press_reaches_the_covered_cell_keyboard_sink():
    """The generic locator press already handles ONLYOFFICE's textarea."""
    with sync_playwright() as pw:
        browser, page = _editor_page(pw, COVERED_KEYBOARD_SINK)
        frame = _frame(page)
        sink = frame.locator("#area_id")
        covered_by = frame.evaluate("""() => {
          const sink = document.getElementById('area_id');
          const bounds = sink.getBoundingClientRect();
          const hit = document.elementFromPoint(
            bounds.left + bounds.width / 2,
            bounds.top + bounds.height / 2);
          return hit && hit.id;
        }""")
        sink.press("Tab", timeout=2_000)
        keys = frame.evaluate("() => window.__editorKeys")
        browser.close()

    assert covered_by == "ws-canvas"
    assert keys == ["Tab"]


def test_generated_press_reaches_the_covered_cell_keyboard_sink():
    """Exercise the generated replay, rather than only its source text."""
    events = [{
        "type": "press", "page": "page", "url": "http://127.0.0.1/editor",
        "frame_url": "about:srcdoc", "selector": "#area_id",
        "selectors": ["#area_id", "#area_id_parent textarea"],
        "key": "Tab", "target": {
            "tag": "textarea", "role": "textbox", "name": "",
        },
        "description": "press worksheet editor", "at_ms": 0,
    }]
    surfaces = [{
        "id": "onlyoffice", "application": "onlyoffice",
        "label": "ONLYOFFICE spreadsheet",
        "url": "http://127.0.0.1/editor", "credentials": {},
    }]
    namespace = {}
    exec(render_demonstrate(events, surfaces), namespace)

    with sync_playwright() as pw:
        browser, page = _editor_page(pw, COVERED_KEYBOARD_SINK)
        steps = []
        namespace["demonstrate"](
            page, "http://127.0.0.1/editor", {},
            lambda key, _page, description: steps.append((key, description)),
        )
        keys = _frame(page).evaluate("() => window.__editorKeys")
        browser.close()

    assert keys == ["Tab"]
    assert steps == [("action-001", "press worksheet editor")]


def test_cell_navigation_commits_an_active_edit_before_using_the_name_box():
    """A direct canvas click commits the edit as it changes cells.

    Its semantic replay goes through the Name Box, which Document Server
    disables while a cell edit is active, so the browser plane must preserve
    that commit side effect before navigating to the recorded cell.
    """
    with sync_playwright() as pw:
        browser, page = _editor_page(pw, EDITING_CELL)
        oo.select_cell(page, "F4")
        state = _frame(page).evaluate(
            "() => ({commits: window.__commits, navigated: window.__navigated})")
        browser.close()
    assert state == {"commits": 1, "navigated": ["F4"]}


def test_structured_text_replays_as_one_real_spreadsheet_paste():
    """Tabs and newlines must reach the editor through ClipboardEvent data.

    ``keyboard.insert_text`` only emits ``insertText`` and cannot reproduce a
    spreadsheet paste that distributes one clipboard rectangle across cells.
    """
    with sync_playwright() as pw:
        browser, page = _editor_page(pw, PASTE_EDITOR)
        replay.paste_text(
            replay.new_replay_state(), page,
            "4\t4467.6\t532.4\tHold\n5\t1200\t0\tRelease",
        )
        state = _frame(page).evaluate(
            "() => ({pastes: window.__pastes, cells: window.__cells})")
        browser.close()

    assert state == {
        "pastes": 1,
        "cells": [
            ["4", "4467.6", "532.4", "Hold"],
            ["5", "1200", "0", "Release"],
        ],
    }


def test_ready_answers_a_modal_alert_through_its_own_control():
    """A masked editor resolves every selector and accepts Name Box text, but
    the grid never receives the navigation — so the value lands in whatever
    cell was selected, with nothing raised. Readiness must include clearing
    it, and by clicking DS's own button rather than tearing out DOM."""
    with sync_playwright() as pw:
        browser, page = _editor_page(pw, MODAL_ALERT)
        frame = _frame(page)
        assert frame.evaluate("() => !!document.querySelector('.modals-mask')")
        oo.ready(page)
        remaining = frame.evaluate(
            "() => document.querySelectorAll('.modals-mask, .asc-window').length")
        browser.close()
    assert remaining == 0


def test_select_cell_closes_a_save_failure_without_opening_download_as():
    """An autosave warning must not turn Enter into acceptance of a download."""
    with sync_playwright() as pw:
        browser, page = _editor_page(pw, SAVE_FAILURE_ALERT)
        oo.select_cell(page, "D3")
        state = _frame(page).evaluate(
            "() => ({"
            "downloadAsOpened: window.__downloadAsOpened, "
            "panel: !!document.querySelector('#panel-saveas'), "
            "alert: !!document.querySelector('[role=alertdialog]'), "
            "navigated: window.__navigated"
            "})")
        browser.close()
    assert state == {
        "downloadAsOpened": False,
        "panel": False,
        "alert": False,
        "navigated": ["D3"],
    }


def test_ready_is_a_no_op_when_nothing_is_in_the_way():
    with sync_playwright() as pw:
        browser, page = _editor_page(pw, CURRENT_EDITOR)
        oo.ready(page)                      # must simply return
        browser.close()


def test_ready_dismisses_the_first_use_tip_covering_sheet_cells():
    with sync_playwright() as pw:
        browser, page = _editor_page(pw, FIRST_USE_TIP)
        frame = _frame(page)
        assert frame.evaluate("() => !!document.querySelector('.synch-tip-root')")
        oo.ready(page)
        remaining = frame.evaluate(
            "() => document.querySelectorAll('.synch-tip-root').length")
        browser.close()
    assert remaining == 0


def test_ready_accepts_a_tip_that_dismisses_itself_mid_click():
    """ready() runs after every replay navigation, outside the gesture
    recovery wrapper — a tip that vanishes between the visibility wait and
    the dismissal click is a cleared editor, not a replay-ending failure."""
    with sync_playwright() as pw:
        browser, page = _editor_page(pw, SELF_DISMISSING_TIP)
        oo.ready(page)                      # must not raise
        remaining = _frame(page).evaluate(
            "() => document.querySelectorAll('.synch-tip-root').length")
        browser.close()
    assert remaining == 0
