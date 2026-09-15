"""Browser tests for the managed-capture recorder script (slow: chromium).

The recorder's selector generation is what makes a captured gesture
replayable; these tests exercise it against the DOM shape that broke real
captures — deeply nested dialog controls whose only stable anchor is an
ancestor's data attribute (ERPNext quick-entry link fields).
"""
from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from showAndTell.player import replay
from showAndTell.demonstration.events import _dedupe
from showAndTell.capture.transactions import merge_recorded_clicks
from showAndTell.demonstration.compiler import render_demonstrate
from showAndTell.capture.runtime import _INJECT_SOURCE as _INJECT

pytestmark = pytest.mark.slow

# The recorder reports a click as click_begin + click_settled; tests observe
# the merged view the capture host would persist.

# Two identical link-field inputs inside a dialog nested deeper than a short
# positional CSS path can anchor, plus a decoy control with the same
# data-fieldname outside the dialog (ERPNext's list-view filter section).
FRAPPE_LIKE_PAGE = """
<div class="filters">
  <div data-fieldname="item_group"><input class="input-with-feedback"></div>
</div>
<div data-testid="quick-entry"><div class="modal"><div class="modal-dialog">
  <div class="modal-content"><div class="modal-body"><div class="form">
    <div class="section"><div class="column"><div class="frappe-control"
         data-fieldname="item_group">
      <div class="control-input"><input class="input-with-feedback"
           type="text" autocomplete="off"></div>
    </div></div></div>
    <div class="section"><div class="column"><div class="frappe-control"
         data-fieldname="stock_uom">
      <div class="control-input"><input class="input-with-feedback"
           type="text" autocomplete="off"></div>
    </div></div></div>
  </div></div></div>
</div></div></div>
"""


@pytest.fixture(scope="module")
def recorded():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        events: list[dict] = []
        page.expose_binding(
            "__showAndTellRecord", lambda _source, payload: events.append(payload))
        page.set_content(FRAPPE_LIKE_PAGE)
        page.evaluate(_INJECT)
        uom = page.locator('[data-fieldname="stock_uom"] input')
        uom.click()
        uom.press_sequentially("Nos")
        page.locator('div[data-testid="quick-entry"] '
                     '[data-fieldname="item_group"] input').click()
        page.wait_for_timeout(700)  # recorder debounce for the fill event
        browser.close()
        return merge_recorded_clicks(events)


def test_dialog_link_field_gets_an_ancestor_scoped_selector(recorded):
    click = next(e for e in recorded if e["type"] == "click")
    selectors = click["target"]["selectors"]
    assert selectors, "a control with no own attributes must not record zero selectors"
    assert '[data-fieldname="stock_uom"] input' in selectors


def test_duplicated_fieldname_is_disambiguated_through_a_further_ancestor(recorded):
    clicks = [e for e in recorded if e["type"] == "click"]
    group = clicks[-1]["target"]["selectors"]
    # The bare fieldname selector collides with the filter decoy, so the
    # recorder must anchor it through the next attributed ancestor.
    assert '[data-testid="quick-entry"] [data-fieldname="item_group"] input' in group
    assert '[data-fieldname="item_group"] input' not in group


def test_typed_value_never_becomes_the_accessible_name(recorded):
    fill = next(e for e in recorded if e["type"] == "fill")
    assert fill["value"] == "Nos"
    # The name is identity, not state: resolving get_by_role("combobox",
    # name="Nos") on a fresh dialog matches nothing once the field is empty.
    assert fill["target"]["name"] != "Nos"


# The dialog control collides with an identical, HIDDEN control — an SPA
# keeping the previously visited form in the DOM (Frappe's desk does exactly
# this, and it is why real quick-entry captures recorded zero selectors).
SPA_WITH_HIDDEN_TWIN = """
<div class="form-page" style="display: none">
  <div data-fieldname="stock_uom"><input class="input-with-feedback"></div>
</div>
<div class="modal"><div class="modal-dialog">
  <div data-fieldname="stock_uom"><input class="input-with-feedback"
       type="text" autocomplete="off"></div>
</div></div>
"""


def test_commit_active_field_blurs_only_an_editable_focus():
    """The helper is the whole fix, so pin what it does to a live document."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content('<input id="qty"><div id="plain" tabindex="0"></div>')

        page.locator("#qty").focus()
        committed = replay.commit_active_field(page)
        blurred = page.evaluate("document.activeElement.id")

        page.locator("#plain").focus()
        skipped = replay.commit_active_field(page)
        kept = page.evaluate("document.activeElement.id")

        browser.close()

    assert committed is True and blurred != "qty"
    assert skipped is False and kept == "plain"


def test_hidden_spa_twin_does_not_defeat_selector_uniqueness():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        events: list[dict] = []
        page.expose_binding(
            "__showAndTellRecord", lambda _source, payload: events.append(payload))
        page.set_content(SPA_WITH_HIDDEN_TWIN)
        page.evaluate(_INJECT)
        page.locator(".modal input").click()
        browser.close()
    click = next(e for e in merge_recorded_clicks(events)
                 if e["type"] == "click")
    assert '[data-fieldname="stock_uom"] input' in click["target"]["selectors"]


def test_autocomplete_click_promotes_option_descendant_to_named_option():
    """An option's inner text node must not own the click's identity."""
    event = _record_click_targets(
        '<div role="listbox"><div role="option"><p id="label">Stores - STM</p></div></div>',
        ["#label"],
    )[0]

    assert event["target"]["role"] == "option"
    assert event["target"]["name"] == "Stores - STM"
    assert 'role=option[name="Stores - STM"]' in event["target"]["selectors"]


def _record_blur_gesture(markup: str, click_selector: str,
                         position: dict | None = None) -> dict:
    """Type into #qty, then click elsewhere — the blur-to-commit gesture.

    Returns the single recorded click on `click_selector`. The editor is
    focused rather than clicked so that fixtures may cover it (a modal
    backdrop) without the setup itself being intercepted.
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        events: list[dict] = []
        page.expose_binding(
            "__showAndTellRecord", lambda _source, payload: events.append(payload))
        page.set_content(markup)
        page.evaluate(_INJECT)
        page.locator("#qty").focus()
        page.locator("#qty").fill("40")
        page.locator(click_selector).click(position=position, force=True)
        page.wait_for_timeout(50)
        browser.close()

    recorded = [event for event in merge_recorded_clicks(events)
                if event["type"] == "click"
                and event["target"]["selectors"][0] == click_selector]
    assert len(recorded) == 1, f"expected one {click_selector} click"
    return recorded[0]


def test_background_click_that_commits_focused_edit_is_semantic():
    """The form container ENCLOSES the field, as a real Frappe form does."""
    background = _record_blur_gesture(
        '<main id="form" style="width:800px;height:400px">'
        '<div class="grid-row"><input id="qty"></div></main>',
        "#form", {"x": 700, "y": 300})

    assert background["commit_only"] is True
    assert "position" not in background


def test_click_on_a_large_control_keeps_its_position():
    """The guards are conjunctive: an interactive target is never commit_only."""
    canvas = _record_blur_gesture(
        '<input id="qty"><canvas id="sheet" width="800" height="400"></canvas>',
        "#sheet", {"x": 700, "y": 300})

    assert "commit_only" not in canvas
    assert canvas["position"]


def test_backdrop_click_that_dismisses_a_dialog_is_not_a_commit():
    """A backdrop is not blank form space: it does not enclose the field.

    Typing into a dialog and clicking outside it dismisses the dialog. Blurring
    instead leaves the dialog open and every later step on the wrong screen.
    """
    backdrop = _record_blur_gesture(
        '<div id="dialog" style="position:fixed;left:0;top:0;'
        'width:400px;height:200px"><input id="qty"></div>'
        '<div id="backdrop" style="position:fixed;inset:0"></div>',
        "#backdrop", {"x": 700, "y": 300})

    assert "commit_only" not in backdrop
    assert backdrop["position"]


def test_dismissing_a_modal_by_clicking_its_scroll_container_is_not_a_commit():
    """Bootstrap's .modal ENCLOSES the dialog, so enclosure alone cannot see it.

    Verified on the live app: clicking outside a real frappe.ui.Dialog lands on
    `div.modal` (1440x900, role=dialog), which contains the field it would
    "commit" and dismisses the dialog. What separates it from a form gutter is
    that it is fixed-positioned -- overlay chrome, not space the form owns.
    """
    modal = _record_blur_gesture(
        '<div id="modal" role="dialog" aria-modal="true" '
        'style="position:fixed;inset:0">'
        '<div class="modal-content" style="width:400px;height:200px">'
        '<input id="qty"></div></div>',
        "#modal", {"x": 700, "y": 700})

    assert "commit_only" not in modal


def test_blank_space_inside_a_dialog_still_commits():
    """The guard rejects the overlay, not everything drawn on top of the page.

    A dialog's own padding is in the document flow and does not dismiss it, so
    blurring a dialog field by clicking it stays a commit.
    """
    content = _record_blur_gesture(
        '<div id="modal" role="dialog" style="position:fixed;inset:0">'
        '<div id="sheet" style="position:relative;width:400px;height:300px">'
        '<input id="qty"></div></div>',
        "#sheet", {"x": 350, "y": 250})

    assert content["commit_only"] is True


def test_click_in_another_editable_region_keeps_the_caret_position():
    """editingHost keeps a position on purpose; commit_only must not strip it."""
    editor = _record_blur_gesture(
        '<input id="qty"><div id="doc" contenteditable '
        'style="width:800px;height:400px"><p>body text</p></div>',
        "#doc", {"x": 700, "y": 300})

    assert "commit_only" not in editor
    assert editor["position"]


def test_a_thin_wide_strip_is_not_blank_form_space():
    """Encloses the editor, so only the size guard can reject it.

    Toolbars, breadcrumb bars and grid rows are wide and short. Their clicks
    do things, and a lone width test lets every one of them through.
    """
    strip = _record_blur_gesture(
        '<div id="bar" style="width:900px;height:40px">'
        '<div id="qty" contenteditable style="display:inline-block;'
        'width:60px">x</div></div>',
        "#bar", {"x": 850, "y": 20})

    assert "commit_only" not in strip
    assert strip["position"]


def test_a_click_that_slipped_off_its_pointerdown_target_is_not_a_commit():
    """An overlay opening under the pointer throws the click up to <body>.

    The gesture never moves, and <body> encloses the field and is large, so
    neither distance, time, enclosure nor size can reject it. Only the element
    the pointer actually went down on can. This is the shape of the one real
    annotation #148 flagged as doubtful: a click recorded against
    `body.no-list-sidebar.no-breadcrumbs` around an open validation modal.
    """
    slipped = _record_blur_gesture(
        '<main id="form" style="width:800px;height:400px">'
        '<div class="grid-row"><input id="qty"></div></main>'
        '<script>document.addEventListener("pointerdown", () => {'
        'const overlay = document.createElement("div");'
        'overlay.style.cssText = "position:fixed;inset:0";'
        'document.body.appendChild(overlay);}, true)</script>',
        "body", {"x": 700, "y": 300})

    assert "commit_only" not in slipped


def _record_click_targets(markup: str, selectors: list[str]) -> list[dict]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        events: list[dict] = []
        page.expose_binding(
            "__showAndTellRecord", lambda _source, payload: events.append(payload))
        page.set_content(markup)
        page.evaluate(_INJECT)
        for selector in selectors:
            page.locator(selector).click()
        browser.close()
    return [event for event in merge_recorded_clicks(events)
            if event["type"] == "click"]


def test_capture_keeps_identity_channel_role_and_selector_provenance():
    events = _record_click_targets(
        '<label id="subject-label" for="subject">Subject</label>'
        '<input id="subject" aria-labelledby="subject-label" '
        'value="Quarterly update">',
        ["#subject"],
    )

    target = events[0]["target"]
    assert target["name"] == "Subject"
    assert target["name_source"] == "accessible"
    assert target["identity_kind"] == "aria-labelledby"
    assert target["aria_role"] == "textbox"
    assert len(target["selectors"]) == len(target["selector_kinds"])
    assert len(target["selectors"]) == len(target["selector_scores"])
    assert target["selector_kinds"][0] == "semantic"


def test_capture_prefers_scoped_text_over_a_full_page_structural_path():
    events = _record_click_targets(
        '<div data-testid="record-fields-widget">'
        '<div class="field-label">Assignee</div>'
        '<div class="field-value">Assignee</div>'
        '</div>',
        ["div.field-value"],
    )

    target = events[0]["target"]
    scoped = ('[data-testid="record-fields-widget"] >> '
              'text="Assignee" >> nth=1')
    assert scoped in target["selectors"]
    assert target["selector_kinds"][target["selectors"].index(scoped)] == "weak"
    assert target["selectors"].index(scoped) < next(
        index for index, selector in enumerate(target["selectors"])
        if "nth-of-type" in selector)

    # On replay the old class now names a menu, while the field's class and
    # full-page position have changed. The stable-widget text locator survives.
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div class="field-value">Filter<br>Sort<br>Move left</div>'
            '<div data-testid="record-fields-widget">'
            '<div class="field-label">Assignee</div>'
            '<div class="renamed-field-value">Assignee</div>'
            '</div>')

        resolved = replay.locator(
            page, target["selectors"], target, timeout=0.5)

        assert resolved.get_attribute("class") == "renamed-field-value"
        browser.close()


def test_replay_waits_past_a_drifted_menu_for_the_named_generic_target():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div id="slot">Filter<br>Sort<br>Move left<br>Move right<br>Hide</div>')
        page.evaluate(
            "setTimeout(() => document.getElementById('slot').textContent = "
            "'Assignee', 120)")

        # Legacy shape: no aria_role, identity_kind, or selector provenance.
        target = {
            "tag": "div", "role": "div",
            "name": "Assignee", "name_source": "accessible",
        }
        resolved = replay.locator(
            page, ["body > div:nth-of-type(1)"], target, timeout=1.0)

        assert resolved.inner_text() == "Assignee"
        browser.close()


def test_replay_accepts_populated_fields_using_computed_accessible_names():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<label id="subject-label" for="subject">Subject</label>'
            '<input id="subject" class="generated-field" '
            'aria-labelledby="subject-label" value="Quarterly update">'
            '<input id="search" class="generated-field" '
            'placeholder="Search" value="john">')

        labelled = replay.locator(page, ["#subject"], {
            "tag": "input", "role": "textbox", "aria_role": "textbox",
            "name": "Subject", "name_source": "accessible",
            "identity_kind": "aria-labelledby",
            "selector_kinds": ["weak"],
        }, timeout=0.2)
        placeholder = replay.locator(page, ["#search"], {
            "tag": "input", "role": "textbox", "aria_role": "textbox",
            "name": "Search", "name_source": "accessible",
            "identity_kind": "placeholder", "selector_kinds": ["weak"],
        }, timeout=0.2)

        assert labelled.get_attribute("value") == "Quarterly update"
        assert placeholder.get_attribute("value") == "john"
        browser.close()


def test_replay_semantically_narrows_a_weak_selector_with_many_matches():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<button class="toolbar-action">Save</button>'
            '<button class="toolbar-action">Submit</button>')

        resolved = replay.locator(page, ["button.toolbar-action"], {
            "tag": "button", "role": "button", "aria_role": "button",
            "name": "Submit", "name_source": "accessible",
            "identity_kind": "text", "selector_kinds": ["weak"],
        }, timeout=0.2)

        assert resolved.inner_text() == "Submit"
        browser.close()


def test_rich_text_click_promotes_replaceable_descendant_to_editing_host():
    events = _record_click_targets(
        '<div id="editor" contenteditable="true" '
        'style="width:400px;height:100px;padding:10px">'
        '<p id="paragraph"><span id="leaf">Draft text</span></p></div>',
        ["#leaf"],
    )

    assert len(events) == 1
    assert events[0]["target"]["tag"] == "div"
    assert "#editor" in events[0]["target"]["selectors"]
    assert events[0]["position"]["x"] >= 0
    assert events[0]["position"]["y"] >= 0


def test_editing_host_promotion_preserves_controls_and_false_islands():
    events = _record_click_targets(
        '<div id="editor" contenteditable="true">'
        '<p><span id="text-leaf">Text</span></p>'
        '<a id="editor-link" href="#linked">Link</a>'
        '<button id="editor-button">Button</button>'
        '<span contenteditable="false"><span id="locked">Locked</span></span>'
        '<div contenteditable="true"><p id="nested-leaf">Nested</p></div>'
        '</div>',
        ["#text-leaf", "#editor-link", "#editor-button", "#locked",
         "#nested-leaf"],
    )

    expected_ids = [
        "#editor", "#editor-link", "#editor-button", "#locked", "#editor",
    ]
    assert all(expected in event["target"]["selectors"]
               for event, expected in zip(events, expected_ids, strict=True))


def _boolean_events(markup: str, gestures) -> list[dict]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        events: list[dict] = []
        page.expose_binding(
            "__showAndTellRecord", lambda _source, payload: events.append(payload))
        page.set_content(markup)
        page.evaluate(_INJECT)
        gestures(page)
        # The click_settled patch arrives one browser task after the click,
        # once cancelled activation has restored the native checkedness.
        page.wait_for_timeout(50)
        browser.close()
    return [event for event in merge_recorded_clicks(events)
            if event["type"] == "click"]


def test_native_checkbox_capture_records_settled_before_and_after_state():
    events = _boolean_events(
        '<input id="variants" type="checkbox" aria-label="Has Variants">',
        lambda page: page.locator("#variants").click(),
    )

    assert len(events) == 1
    assert events[0]["checked_before"] is False
    assert events[0]["checked_after"] is True


def test_cancelled_checkbox_click_records_restored_state():
    events = _boolean_events(
        '<input id="variants" type="checkbox" aria-label="Has Variants">'
        '<script>variants.addEventListener("click", event => '
        'event.preventDefault())</script>',
        lambda page: page.locator("#variants").click(),
    )

    assert len(events) == 1
    assert events[0]["checked_before"] is False
    assert events[0]["checked_after"] is False


def test_native_radio_capture_records_selected_state():
    events = _boolean_events(
        '<input id="standard" type="radio" name="shipping" checked>'
        '<input id="express" type="radio" name="shipping">',
        lambda page: page.locator("#express").click(),
    )

    assert len(events) == 1
    assert events[0]["checked_before"] is False
    assert events[0]["checked_after"] is True


def test_aria_checkbox_capture_records_aria_checked_state():
    events = _boolean_events(
        '<div id="custom" role="checkbox" tabindex="0" aria-checked="false">'
        'Include archived</div>'
        '<script>custom.addEventListener("click", () => custom.setAttribute('
        '"aria-checked", custom.getAttribute("aria-checked") === "false" '
        '? "true" : "false"))</script>',
        lambda page: page.locator("#custom").click(),
    )

    assert len(events) == 1
    assert events[0]["checked_before"] is False
    assert events[0]["checked_after"] is True


def _value_events(gestures) -> list[dict]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        events: list[dict] = []
        page.expose_binding(
            "__showAndTellRecord", lambda _source, payload: events.append(payload))
        # A spreadsheet's shape: ONE navigation field reused for every cell, and
        # a worksheet tab beside it.
        page.set_content('<input id="namebox" type="text" autocomplete="off">'
                         '<span id="tab-ops">Ops</span><span id="tab-rates">Rates</span>')
        page.evaluate(_INJECT)
        gestures(page)
        page.wait_for_timeout(800)      # outlast the recorder's input debounce
        browser.close()
    return merge_recorded_clicks(events)


def test_a_value_re_entered_after_another_gesture_is_recorded_again():
    """The Name Box takes the same cell reference again on the next worksheet.

    Suppressing that as a duplicate loses the navigation while KEEPING the tab
    switch, so every later value is typed into whatever cell the new sheet had
    selected — a silent wrong-cell write on a replay that reports success.
    """
    def gestures(page):
        page.fill("#namebox", "A5")
        page.wait_for_timeout(700)
        page.click("#tab-rates")
        page.fill("#namebox", "A5")

    events = _value_events(gestures)
    kinds = [(e["type"], e.get("value") or e["target"].get("name")) for e in events]
    fills = [e for e in events if e["type"] == "fill" and e.get("value") == "A5"]
    assert len(fills) == 2, kinds
    # and in the order that makes the second one meaningful
    types = [e["type"] for e in events]
    assert types.index("click") < len(types) - 1, kinds


def test_one_edit_is_recorded_once_though_input_and_change_both_fire():
    """The debounced input and change-on-blur are two reports of one edit."""
    def gestures(page):
        page.fill("#namebox", "B7")
        page.wait_for_timeout(700)
        page.click("#tab-ops")          # blurs the field, firing change

    events = _value_events(gestures)
    fills = [e for e in events if e["type"] == "fill"]
    assert len(fills) == 1, [(e["type"], e.get("value")) for e in events]


def test_committing_a_value_with_enter_does_not_report_it_twice():
    """Enter commits the edit, and the browser then fires change on the same
    field. That is still one edit: the key that committed it is not "the
    operator did something else", so the confirming report must collapse."""
    def gestures(page):
        page.fill("#namebox", "A5")
        page.wait_for_timeout(700)
        page.press("#namebox", "Enter")

    events = _value_events(gestures)
    kinds = [(e["type"], e.get("value") or e.get("key")) for e in events]
    assert kinds.count(("fill", "A5")) == 1, kinds
    assert ("press", "Enter") in kinds


def test_a_late_debounce_cannot_land_after_the_gesture_that_blurred_it():
    """Blur arrives before the input timer when an edit is committed fast.

    The pending timer must be cancelled by the change it duplicates, or the
    fill is emitted AFTER the click and replays in the wrong order.
    """
    def gestures(page):
        page.fill("#namebox", "C9")     # no wait: the timer is still armed
        page.click("#tab-rates")

    events = _value_events(gestures)
    types = [e["type"] for e in events]
    assert types.count("fill") == 1, [(e["type"], e.get("value")) for e in events]
    assert types.index("fill") < types.index("click"), types


def test_tokenizing_recipient_keeps_value_cleared_by_the_widget():
    """Roundcube turns a typed address into a recipient chip on comma.

    Its widget clears the backing input during that same event, before the
    recorder's debounce runs. The capture must retain the address observed at
    input time or the replay reaches Send with an empty To field.
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        events: list[dict] = []
        page.expose_binding(
            "__showAndTellRecord", lambda _source, payload: events.append(payload))
        page.set_content(
            '<input id="recipient" type="text">'
            '<div id="chips"></div>'
            '<script>recipient.addEventListener("input", event => {'
            ' if (event.target.value.endsWith(",")) {'
            ' chips.textContent = event.target.value; event.target.value = "";'
            ' }'
            '})</script>')
        page.evaluate(_INJECT)
        page.locator("#recipient").press_sequentially(
            "sales-desk@showAndTell.test,")
        page.wait_for_timeout(700)
        browser.close()

    fills = [event for event in events if event["type"] == "fill"]
    assert [event["value"] for event in fills] == [
        "sales-desk@showAndTell.test,"]


def test_ime_textarea_typing_is_recorded_as_keystrokes_not_fills():
    """Canvas editors route keys through a hidden textarea and consume its
    content as they process; debounced fill snapshots capture truncated junk
    ('=C2*D') that corrupts the editor when replayed. Typing into an IME-like
    element must be recorded as coalesced keystrokes, with Tab preserved,
    and produce no fill events at all."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        events: list[dict] = []
        page.expose_binding(
            "__showAndTellRecord", lambda _source, payload: events.append(payload))
        page.set_content(
            '<canvas id="grid" width="300" height="200"></canvas>'
            '<textarea id="area_id" style="position:absolute; left:-9999px;'
            ' width:1px; height:1px"></textarea>'
            "<script>document.getElementById('grid').addEventListener('click',"
            " () => document.getElementById('area_id').focus())</script>")
        page.evaluate(_INJECT)
        page.locator("#grid").click()
        page.keyboard.type("PC-003", delay=30)
        page.keyboard.press("Tab")
        # A real canvas editor consumes Tab and keeps IME focus; the bare test
        # page loses it, so refocus the way the editor would.
        page.locator("#grid").click()
        page.keyboard.type("=SUM(E2:E4)", delay=30)
        page.keyboard.press("Enter")
        page.wait_for_timeout(800)
        browser.close()
    kinds = [(e["type"], e.get("text") or e.get("key") or "")
             for e in merge_recorded_clicks(events)]
    assert ("type", "PC-003") in kinds
    assert ("type", "=SUM(E2:E4)") in kinds
    assert ("press", "Tab") in kinds
    assert ("press", "Enter") in kinds
    assert not any(e["type"] in ("fill", "select") for e in events), kinds
    # keystrokes flush in order: text, then the key that committed it
    type_index = kinds.index(("type", "PC-003"))
    assert kinds.index(("press", "Tab")) == type_index + 1


def test_ime_textarea_hidden_under_a_canvas_is_detected():
    """OnlyOffice's #area_id is normal-sized and fully opaque — hidden only
    by z-order beneath the canvas. Occlusion, not geometry, is the tell."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        events: list[dict] = []
        page.expose_binding(
            "__showAndTellRecord", lambda _source, payload: events.append(payload))
        page.set_content(
            '<div style="position:relative; width:400px; height:200px">'
            '<textarea id="area_id" style="position:absolute; left:49px;'
            ' top:26px; width:200px; height:50px"></textarea>'
            '<canvas id="grid" width="400" height="200"'
            ' style="position:absolute; left:0; top:0"></canvas></div>'
            "<script>document.getElementById('grid').addEventListener('click',"
            " () => document.getElementById('area_id').focus())</script>")
        page.evaluate(_INJECT)
        page.locator("#grid").click()
        page.keyboard.type("120", delay=30)
        page.keyboard.press("Enter")
        page.wait_for_timeout(800)
        browser.close()
    kinds = [(e["type"], e.get("text") or e.get("key") or "")
             for e in merge_recorded_clicks(events)]
    assert ("type", "120") in kinds and ("press", "Enter") in kinds
    assert not any(e["type"] in ("fill", "select") for e in events), kinds


def test_recorder_preserves_modifiers_on_submit_shortcuts():
    """A captured Command/Ctrl+Enter must not degrade into a newline."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        events: list[dict] = []
        page.expose_binding(
            "__showAndTellRecord", lambda _source, payload: events.append(payload))
        page.set_content('<div id="comment" contenteditable="true"></div>')
        page.evaluate(_INJECT)
        page.locator("#comment").click()
        page.keyboard.press("Meta+Enter")
        page.wait_for_timeout(100)
        browser.close()

    presses = [event for event in events if event["type"] == "press"]
    assert presses[-1]["key"] == "Enter"
    assert presses[-1]["modifiers"] == ["Meta"]


def test_canvas_commands_are_not_lost_when_the_editor_emits_no_text_event():
    """Copy/paste and spreadsheet commands can mutate only the canvas."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        events: list[dict] = []
        page.expose_binding(
            "__showAndTellRecord", lambda _source, payload: events.append(payload))
        page.set_content(
            '<textarea id="area_id" style="position:absolute;left:-9999px;'
            'width:1px;height:1px"></textarea>')
        page.evaluate(_INJECT)
        page.locator("#area_id").focus()
        page.keyboard.press("Meta+c")
        # With no clipboard payload/beforeinput, paste must fall back to the
        # shortcut itself instead of disappearing from the capture.
        page.keyboard.press("Meta+v")
        page.keyboard.press("Meta+d")
        page.wait_for_timeout(100)
        browser.close()

    presses = [event for event in events if event["type"] == "press"]
    assert [(event["key"].lower(), event["modifiers"]) for event in presses] == [
        ("c", ["Meta"]), ("v", ["Meta"]), ("d", ["Meta"])]


def test_canvas_editor_paste_records_complete_semantic_text_once():
    """paste/beforeinput/input are three reports of one edit transaction.

    Canvas proxies often clear their textarea before a value snapshot can see
    it. The event payload must preserve the complete paste without also
    recording the Cmd+V key or duplicating the text at each event phase.
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        events: list[dict] = []
        page.expose_binding(
            "__showAndTellRecord", lambda _source, payload: events.append(payload))
        page.set_content(
            '<textarea id="proxy" style="position:absolute;left:-9999px;'
            'width:1px;height:1px"></textarea>')
        page.evaluate(_INJECT)
        page.locator("#proxy").focus()
        page.evaluate("""
          () => {
            const el = document.getElementById('proxy');
            const text = 'Senior Marketing Analyst';
            const transfer = new DataTransfer();
            transfer.setData('text/plain', text);
            el.dispatchEvent(new KeyboardEvent('keydown', {
              bubbles: true, key: 'v', metaKey: true
            }));
            el.dispatchEvent(new ClipboardEvent('paste', {
              bubbles: true, cancelable: true, clipboardData: transfer
            }));
            el.dispatchEvent(new InputEvent('beforeinput', {
              bubbles: true, cancelable: true, inputType: 'insertFromPaste', data: text
            }));
            el.dispatchEvent(new InputEvent('input', {
              bubbles: true, inputType: 'insertFromPaste', data: text
            }));
          }
        """)
        page.wait_for_timeout(700)
        browser.close()

    typed = [event for event in events if event["type"] == "type"]
    assert [event["text"] for event in typed] == ["Senior Marketing Analyst"]
    assert typed[0]["input_source"] == "beforeinput"
    assert typed[0]["input_types"] == ["insertFromPaste"]


def test_canvas_editor_paste_falls_back_when_beforeinput_is_omitted():
    """An event-scoped clipboard payload remains sufficient for odd editors."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        events: list[dict] = []
        page.expose_binding(
            "__showAndTellRecord", lambda _source, payload: events.append(payload))
        page.set_content(
            '<textarea id="proxy" style="position:absolute;left:-9999px;'
            'width:1px;height:1px"></textarea>')
        page.evaluate(_INJECT)
        page.evaluate("""
          () => {
            const el = document.getElementById('proxy');
            el.dispatchEvent(new KeyboardEvent('keydown', {
              bubbles: true, key: 'v', metaKey: true
            }));
            const transfer = new DataTransfer();
            transfer.setData('text/plain', 'NA');
            el.dispatchEvent(new ClipboardEvent('paste', {
              bubbles: true, cancelable: true, clipboardData: transfer
            }));
          }
        """)
        page.wait_for_timeout(700)
        browser.close()

    typed = [event for event in events if event["type"] == "type"]
    assert [(event["text"], event["input_source"]) for event in typed] == [
        ("NA", "paste")]
    assert [event for event in events if event["type"] == "press"] == []


def test_shortcut_fallback_yields_to_an_input_only_paste_report():
    """Editors that report a paste only through `input` still get one action.

    Command/Ctrl+V arms a shortcut fallback; whichever of paste, beforeinput
    or input delivers the semantic text must consume it, or replay would
    paste twice — once from the keystroke, once from the recorded text.
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        events: list[dict] = []
        page.expose_binding(
            "__showAndTellRecord", lambda _source, payload: events.append(payload))
        page.set_content(
            '<textarea id="proxy" style="position:absolute;left:-9999px;'
            'width:1px;height:1px"></textarea>')
        page.evaluate(_INJECT)
        page.evaluate("""
          () => {
            const el = document.getElementById('proxy');
            el.dispatchEvent(new KeyboardEvent('keydown', {
              bubbles: true, key: 'v', metaKey: true
            }));
            el.dispatchEvent(new InputEvent('input', {
              bubbles: true, inputType: 'insertFromPaste', data: 'Quarterly Forecast'
            }));
          }
        """)
        page.wait_for_timeout(700)
        browser.close()

    typed = [event for event in events if event["type"] == "type"]
    assert [(event["text"], event["input_source"]) for event in typed] == [
        ("Quarterly Forecast", "input-fallback")]
    assert [event for event in events if event["type"] == "press"] == []


def test_canvas_editor_composition_commits_final_text_once():
    """Intermediate IME candidates are state, not separate replay actions."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        events: list[dict] = []
        page.expose_binding(
            "__showAndTellRecord", lambda _source, payload: events.append(payload))
        page.set_content(
            '<textarea id="proxy" style="position:absolute;left:-9999px;'
            'width:1px;height:1px"></textarea>')
        page.evaluate(_INJECT)
        page.evaluate("""
          () => {
            const el = document.getElementById('proxy');
            el.dispatchEvent(new CompositionEvent('compositionstart', {bubbles: true}));
            for (const text of ['n', 'に']) {
              el.dispatchEvent(new CompositionEvent('compositionupdate', {
                bubbles: true, data: text
              }));
              el.dispatchEvent(new InputEvent('beforeinput', {
                bubbles: true, inputType: 'insertCompositionText',
                data: text, isComposing: true
              }));
              el.dispatchEvent(new InputEvent('input', {
                bubbles: true, inputType: 'insertCompositionText',
                data: text, isComposing: true
              }));
            }
            el.dispatchEvent(new CompositionEvent('compositionend', {
              bubbles: true, data: 'に'
            }));
          }
        """)
        page.wait_for_timeout(700)
        browser.close()

    typed = [event for event in events if event["type"] == "type"]
    assert [(event["text"], event["input_source"]) for event in typed] == [
        ("に", "composition")]


def test_cancelled_canvas_composition_records_no_text():
    """An empty compositionend cancels the candidate instead of committing it."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        events: list[dict] = []
        page.expose_binding(
            "__showAndTellRecord", lambda _source, payload: events.append(payload))
        page.set_content(
            '<textarea id="proxy" style="position:absolute;left:-9999px;'
            'width:1px;height:1px"></textarea>')
        page.evaluate(_INJECT)
        page.evaluate("""
          () => {
            const el = document.getElementById('proxy');
            el.dispatchEvent(new CompositionEvent('compositionstart', {bubbles: true}));
            el.dispatchEvent(new CompositionEvent('compositionupdate', {
              bubbles: true, data: 'draft'
            }));
            el.dispatchEvent(new CompositionEvent('compositionend', {
              bubbles: true, data: ''
            }));
          }
        """)
        page.wait_for_timeout(700)
        browser.close()

    assert not [event for event in events if event["type"] == "type"]


def test_repeated_canvas_characters_are_not_mistaken_for_event_echoes():
    """beforeinput/input dedupe must preserve a genuinely repeated key."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        events: list[dict] = []
        page.expose_binding(
            "__showAndTellRecord", lambda _source, payload: events.append(payload))
        page.set_content(
            '<textarea id="proxy" style="position:absolute;left:-9999px;'
            'width:1px;height:1px"></textarea>')
        page.evaluate(_INJECT)
        page.locator("#proxy").focus()
        page.keyboard.type("bookkeeper")
        page.keyboard.press("Enter")
        page.wait_for_timeout(100)
        browser.close()

    typed = [event["text"] for event in events if event["type"] == "type"]
    assert typed == ["bookkeeper"]


def test_pointer_drag_is_one_action_and_suppresses_release_click():
    """A press/move/release is a drag, not a click plus pointer noise."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        events: list[dict] = []
        page.expose_binding(
            "__showAndTellRecord", lambda _source, payload: events.append(payload))
        page.set_content(
            '<div id="source" style="width:160px;height:80px">Card</div>'
            '<div id="destination" style="margin-left:220px;width:160px;'
            'height:80px">Approved</div>')
        page.evaluate(_INJECT)
        source = page.locator("#source").bounding_box()
        destination = page.locator("#destination").bounding_box()
        page.mouse.move(source["x"] + 20, source["y"] + 20)
        page.mouse.down()
        page.mouse.move(
            destination["x"] + 30, destination["y"] + 30, steps=12)
        page.mouse.up()
        page.wait_for_timeout(400)
        browser.close()

    merged = merge_recorded_clicks(events)
    assert [event["type"] for event in merged] == ["drag"]
    drag = merged[0]
    assert drag["drag_mode"] == "pointer"
    assert "#source" in drag["target"]["selectors"]
    assert "#destination" in drag["destination"]["selectors"]
    assert len(drag["path"]) >= 2


def test_pointer_drag_preserves_subpixel_handle_geometry_and_hit_target():
    """Pointer capture retargets pointerup to the source, while rounding a
    near-edge press can put it outside a narrow resize handle."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        events: list[dict] = []
        page.expose_binding(
            "__showAndTellRecord", lambda _source, payload: events.append(payload))
        page.set_content(
            '<span id="handle" style="position:absolute;left:20px;top:20px;'
            'width:5px;height:30px;background:black"></span>'
            '<div id="destination" style="position:absolute;left:180px;top:20px;'
            'width:80px;height:40px;background:green"></div>'
            '<script>handle.addEventListener("pointerdown", event => '
            'handle.setPointerCapture(event.pointerId))</script>')
        page.evaluate(_INJECT)
        handle = page.locator("#handle").bounding_box()
        destination = page.locator("#destination").bounding_box()
        page.mouse.move(handle["x"] + 4.6, handle["y"] + 15)
        page.mouse.down()
        page.mouse.move(destination["x"] + 20, destination["y"] + 15,
                        steps=8)
        page.mouse.up()
        page.wait_for_timeout(100)
        browser.close()

    drag = next(event for event in events if event["type"] == "drag")
    assert drag["drag_mode"] == "pointer"
    assert 4 < drag["source_position"]["x"] < 5
    assert drag["path"][-1]["x"] > 5
    assert "#destination" in drag["destination"]["selectors"]


def test_cancelled_pointer_gesture_does_not_emit_a_drag():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        events: list[dict] = []
        page.expose_binding(
            "__showAndTellRecord", lambda _source, payload: events.append(payload))
        page.set_content('<div id="surface" style="width:200px;height:100px"></div>')
        page.evaluate(_INJECT)
        page.locator("#surface").dispatch_event(
            "pointerdown", {"pointerId": 7, "button": 0, "isPrimary": True,
                            "clientX": 20, "clientY": 20})
        page.locator("#surface").dispatch_event(
            "pointermove", {"pointerId": 7, "isPrimary": True,
                            "clientX": 80, "clientY": 20})
        page.locator("#surface").dispatch_event(
            "pointercancel", {"pointerId": 7, "isPrimary": True,
                              "clientX": 80, "clientY": 20})
        page.wait_for_timeout(50)
        browser.close()

    assert not any(event["type"] == "drag" for event in events)


def test_native_html_drag_survives_pointer_cancel():
    """Chromium cancels pointer events when native HTML drag-and-drop begins."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        events: list[dict] = []
        page.expose_binding(
            "__showAndTellRecord", lambda _source, payload: events.append(payload))
        page.set_content(
            '<div id="source" draggable="true" '
            'style="width:100px;height:60px">Card</div>'
            '<div id="destination" style="margin-left:220px;width:160px;'
            'height:80px">Approved</div>')
        page.evaluate(_INJECT)
        source = page.locator("#source").bounding_box()
        destination = page.locator("#destination").bounding_box()
        page.mouse.move(source["x"] + 20, source["y"] + 20)
        page.mouse.down()
        page.mouse.move(
            destination["x"] + 30, destination["y"] + 30, steps=12)
        page.mouse.up()
        page.wait_for_timeout(300)
        browser.close()

    drags = [event for event in events if event["type"] == "drag"]
    assert len(drags) == 1
    assert drags[0]["drag_mode"] == "native"
    assert "#destination" in drags[0]["destination"]["selectors"]


def test_captured_pointer_drag_roundtrips_after_scroll_and_handle_width_drift():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 800, "height": 500})
        events: list[dict] = []
        page.expose_binding(
            "__showAndTellRecord", lambda _source, payload: events.append(payload))
        page.set_content(
            '<div style="height:1400px"></div>'
            '<div id="panel" style="position:absolute;left:20px;top:20px;'
            'width:100px;height:60px;background:silver">'
            '<span id="handle" style="position:absolute;right:0;top:0;'
            'width:5px;height:60px;background:black"></span></div>'
            '<script>'
            'let startX, startWidth;'
            'handle.addEventListener("pointerdown", event => {'
            ' startX = event.clientX; startWidth = panel.getBoundingClientRect().width;'
            ' handle.setPointerCapture(event.pointerId);'
            '});'
            'handle.addEventListener("pointermove", event => {'
            ' if (startX !== undefined && event.buttons)'
            '   panel.style.width = `${startWidth + event.clientX - startX}px`;'
            '});'
            '</script>')
        # Move the panel below the fold after preserving its simple absolute
        # geometry; capture must scroll to it and replay must do so again.
        page.locator("#panel").evaluate("el => el.style.top = '1450px'")
        page.evaluate(_INJECT)
        handle = page.locator("#handle")
        handle.scroll_into_view_if_needed()
        box = handle.bounding_box()
        page.mouse.move(box["x"] + 4.6, box["y"] + 30)
        page.mouse.down()
        page.mouse.move(box["x"] + 54.6, box["y"] + 30, steps=8)
        page.mouse.up()

        event = next(event for event in events if event["type"] == "drag")
        assert page.locator("#panel").evaluate(
            "el => el.getBoundingClientRect().width") == 150

        page.locator("#panel").evaluate("el => el.style.width = '100px'")
        handle.evaluate("el => el.style.width = '4px'")
        page.evaluate("scrollTo(0, 0)")

        replay.drag(
            page, handle, None,
            event["source_position"], event["target_position"], event["path"],
            drag_mode=event["drag_mode"],
        )

        assert page.locator("#panel").evaluate(
            "el => el.getBoundingClientRect().width") == 150
        browser.close()


def test_native_drag_replay_dispatches_drop_in_real_browser():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            '<div id="source" draggable="true" '
            'style="width:80px;height:40px">Card</div>'
            '<div id="destination" style="margin-left:180px;'
            'width:120px;height:60px">Waiting</div>'
            '<script>'
            'source.addEventListener("dragstart", event => '
            ' event.dataTransfer.setData("text/plain", "Card"));'
            'destination.addEventListener("dragover", event => event.preventDefault());'
            'destination.addEventListener("drop", event => {'
            ' event.preventDefault(); destination.textContent = '
            ' event.dataTransfer.getData("text/plain");'
            '});'
            '</script>')

        replay.drag(
            page, page.locator("#source"), page.locator("#destination"),
            {"x": 20, "y": 20}, {"x": 30, "y": 30}, [],
            drag_mode="native",
        )

        assert page.locator("#destination").inner_text() == "Card"
        browser.close()


def test_visible_top_document_emits_passive_tab_switch():
    """A visible transition is recorded without requiring a later DOM action."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        events: list[dict] = []
        page.expose_binding(
            "__showAndTellRecord", lambda _source, payload: events.append(payload))
        page.set_content("<title>Orders</title><main>Queue</main>")
        page.evaluate(_INJECT)
        page.evaluate("""
          Object.defineProperty(document, 'visibilityState', {
            configurable: true, value: 'visible'
          });
          document.dispatchEvent(new Event('visibilitychange'));
        """)
        page.wait_for_timeout(50)
        browser.close()

    switches = [event for event in events if event["type"] == "tab_switch"]
    assert len(switches) == 1
    assert switches[0]["title"] == "Orders"
    assert isinstance(switches[0]["timestamp"], int)


def test_recorder_reaches_frames_that_predate_injection():
    """The OnlyOffice editor lives in an iframe that already exists when the
    recorder is installed; page.evaluate covers only the main frame, so the
    capture loop must walk page.frames — otherwise every canvas gesture in
    the embedded editor is silently unrecorded."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        events: list[dict] = []
        page.expose_binding(
            "__showAndTellRecord", lambda _source, payload: events.append(payload))
        page.set_content(
            '<iframe srcdoc="<button id=inner>In frame</button>"></iframe>')
        page.wait_for_load_state()
        for frame in page.frames:   # what ManagedCapture._run now does
            frame.evaluate(_INJECT)
        page.frame_locator("iframe").locator("#inner").click()
        browser.close()
    click = next(e for e in merge_recorded_clicks(events)
                 if e["type"] == "click")
    assert "#inner" in click["target"]["selectors"]


# --- Playwright-ideology selector ladder -----------------------------------
# Reference: playwright v1.61 selectorGenerator.ts. Test ids outrank role+name,
# which outranks text, which outranks CSS; GUID-like ids are rejected; text is
# trimmed at word boundaries and stripped of counter numbers.
LADDER_PAGE = """
<button id="x9f3ab77c2d14" data-testid="save-btn">Save changes</button>
<a href="/desk/item/test-01" onclick="event.preventDefault()">test</a>
<button id="menu-help">Help</button>
<nav><a href="/inbox" onclick="event.preventDefault()">Inbox 24</a></nav>
<div><span class="blurb">The quick brown fox jumps over the lazy dog and
keeps running far beyond the old fence line</span></div>
"""


@pytest.fixture(scope="module")
def ladder_events():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        events: list[dict] = []
        page.expose_binding(
            "__showAndTellRecord", lambda _source, payload: events.append(payload))
        page.set_content(LADDER_PAGE)
        page.evaluate(_INJECT)
        page.locator('[data-testid="save-btn"]').click()
        page.locator('a[href="/desk/item/test-01"]').click()
        page.locator("#menu-help").click()
        page.locator('a[href="/inbox"]').click()
        page.locator("span.blurb").click()
        browser.close()
        return [e for e in merge_recorded_clicks(events)
                if e["type"] == "click"]


def test_testid_outranks_role_and_everything_else(ladder_events):
    target = ladder_events[0]["target"]
    selectors = target["selectors"]
    assert selectors[0] == '[data-testid="save-btn"]'
    assert target["selector_kinds"][0] == "contract"


def test_guid_like_id_is_rejected(ladder_events):
    selectors = ladder_events[0]["target"]["selectors"]
    assert not any("x9f3ab77c2d14" in s for s in selectors), selectors


def test_anchor_records_role_link_with_name_first(ladder_events):
    selectors = ladder_events[1]["target"]["selectors"]
    assert selectors[0] == 'role=link[name*="test"]'


def test_stable_id_is_kept_but_ranked_below_role(ladder_events):
    selectors = ladder_events[2]["target"]["selectors"]
    assert selectors[0] == 'role=button[name*="Help"]'
    assert "#menu-help" in selectors
    assert selectors.index("#menu-help") > 0


def test_bootstrap_4_icon_button_records_tooltip_identity():
    events = _record_click_targets(
        '<span class="page-icon-group">'
        '<button class="text-muted btn btn-default prev-doc icon-btn" '
        'data-original-title="Previous Document"></button>'
        '<button class="text-muted btn btn-default next-doc icon-btn" '
        'data-original-title="Next Document"></button>'
        '</span>',
        ["button.next-doc"],
    )

    target = events[0]["target"]
    assert target["name"] == "Next Document"
    assert target["name_source"] == "tooltip"
    assert target["selectors"][0] == '[data-original-title="Next Document"]'
    # The framework's own semantic class distinguishes this button from its
    # sibling; the leading style tokens it shares with them do not.
    assert "button.next-doc" in target["selectors"]


def test_bootstrap_5_icon_button_records_data_title_identity():
    events = _record_click_targets(
        '<button data-bs-title="Next Document"><svg></svg></button>',
        ["button"],
    )

    target = events[0]["target"]
    assert target["name"] == "Next Document"
    assert target["name_source"] == "tooltip"
    assert target["selectors"][0] == '[data-bs-title="Next Document"]'


def test_native_title_remains_an_accessible_role_name():
    events = _record_click_targets(
        '<button title="Next Document"><svg></svg></button>',
        ["button"],
    )

    target = events[0]["target"]
    assert target["name"] == "Next Document"
    assert target["name_source"] == "accessible"
    assert target["selectors"][0] == 'role=button[name*="Next Document"]'
    assert '[title="Next Document"]' in target["selectors"]


def test_a_row_wide_data_name_is_qualified_by_the_target_s_tag():
    """Frameworks stamp one record's id on several of its own cells --
    frappe's list row puts dataset.name on the checkbox, the subject link
    and the like toggle -- so the bare attribute names the row, not the
    control, and is dropped for ambiguity. The target's tag separates
    them and is as durable as the attribute it qualifies; without it the
    click falls all the way to a positional path."""
    events = _record_click_targets(
        '<div class="list-row-container">'
        '<input class="list-row-checkbox" type="checkbox" data-name="SAL-ORD-0001">'
        '<a class="ellipsis" href="/desk/sales-order/SAL-ORD-0001" '
        'data-name="SAL-ORD-0001">Hilarion Food Supply</a>'
        '<span class="like-action" data-name="SAL-ORD-0001">heart</span>'
        '</div>',
        ["a.ellipsis"],
    )

    selectors = events[0]["target"]["selectors"]
    assert '[data-name="SAL-ORD-0001"]' not in selectors
    assert 'a[data-name="SAL-ORD-0001"]' in selectors


def test_tooltip_text_ranks_below_the_control_s_visible_text():
    """Tooltip prose is the weakest label channel, as playwright's own
    generator scores it (kTitleScore 200 vs kTextScore 180): it is verbose,
    translated, and in ERPNext routinely carries a modified-on timestamp.
    It earns a rung, not precedence over the words the operator read."""
    events = _record_click_targets(
        '<button title="Save the document">Save</button>', ["button"])

    selectors = events[0]["target"]["selectors"]
    assert '[title="Save the document"]' in selectors
    assert selectors.index('[title="Save the document"]') > selectors.index("text=Save")


def test_a_lone_distinguishing_class_beats_an_ambiguous_leading_pair():
    """Frameworks put their style tokens first and their semantic token
    last ("text-muted btn btn-default next-doc"), so a fixed two-class
    prefix matches every sibling and is dropped for ambiguity. Prefer the
    smallest class subset that actually resolves."""
    events = _record_click_targets(
        '<div>'
        '<div class="card card-body first-card" style="width:40px;height:20px"></div>'
        '<div class="card card-body second-card" style="width:40px;height:20px"></div>'
        '</div>',
        ["div.second-card"],
    )

    assert "div.second-card" in events[0]["target"]["selectors"]


def test_transient_state_class_is_not_recorded_as_identity():
    events = _record_click_targets(
        '<div><button class="text-muted btn btn-default active icon-btn">'
        '<svg></svg></button>'
        '<button class="text-muted btn btn-default icon-btn"><svg></svg></button></div>',
        ["button:nth-of-type(1)"],
    )

    selectors = events[0]["target"]["selectors"]
    assert "button.active" not in selectors
    assert any("nth-of-type" in selector for selector in selectors)


def test_a_counter_suffixed_id_is_not_treated_as_stable():
    """`popover976954` is minted per render, but the churn test only scores
    character-KIND transitions: one lower->digit transition over length 13
    fails `transitions >= length / 4`, so the id reads as stable. The class
    rung already rejects any run of four or more digits; the id path has to
    agree with it."""
    events = _record_click_targets(
        '<button id="popover976954">Filter</button>', ["button"])

    selectors = events[0]["target"]["selectors"]
    assert "#popover976954" not in selectors


def test_an_ember_counter_is_rejected_even_before_four_digits():
    """Ember assigns sequential element ids and reuses a short one for a
    different control after a rerender, so `ember365` is per-render churn that
    the generic four-digit rule is too coarse to see. Naming the framework
    rejects it without moving that boundary for everyone else."""
    events = _record_click_targets(
        '<button id="ember365">Actions</button>', ["button"])

    assert "#ember365" not in events[0]["target"]["selectors"]


def test_a_separator_delimited_counter_is_rejected_too():
    """The same counter reaches us punctuated as often as glued -- frappe
    writes `popover976954`, MUI writes `mui-12345`, and a dialog keyed on
    an epoch writes `dialog_1699887766`. A rule that only sees glued
    digits catches one framework and misses the rest, so the run itself is
    what disqualifies an id, wherever it sits."""
    events = _record_click_targets(
        '<button id="mui-12345">Filter</button>', ["button"])

    assert "#mui-12345" not in events[0]["target"]["selectors"]


def test_a_short_number_keeps_its_id():
    """The bound is what protects meaning: frappe's own
    `section_break_78` is a hand-written id with a number in it, and three
    digits or fewer stay pinnable."""
    events = _record_click_targets(
        '<button id="section_break_78">Details</button>', ["button"])

    assert "#section_break_78" in events[0]["target"]["selectors"]


def test_a_generated_wrapper_id_is_not_used_to_anchor_a_descendant():
    """The real ERPNext shape behind human-job-offer-follow-up's seven
    skipped actions: a filter popover whose only distinguishing ancestor is
    its generated id, wrapping a fieldname that also exists on the page
    behind it. The captured `#popoverNNNNNN [data-fieldname=...] select`
    resolves at capture time and is gone on the next render."""
    events = _record_click_targets(
        '<div data-fieldname="status">'
        '<select id="list-status"><option>Open</option></select></div>'
        '<div id="popover976954"><div data-fieldname="status">'
        '<select><option>Awaiting Response</option>'
        '<option>Accepted</option></select></div></div>',
        ["#popover976954 select"],
    )

    selectors = events[0]["target"]["selectors"]
    assert not any("popover976954" in selector for selector in selectors)


def test_trailing_counter_number_is_stripped_from_name(ladder_events):
    selectors = ladder_events[3]["target"]["selectors"]
    assert 'role=link[name*="Inbox"]' in selectors


def test_long_text_is_trimmed_at_a_word_boundary(ladder_events):
    selectors = ladder_events[4]["target"]["selectors"]
    texts = [s for s in selectors if s.startswith("text=")]
    assert texts, selectors
    payload = texts[0][len("text="):].strip('"')
    assert len(payload) <= 80
    assert not payload.endswith(" ")
    # trimmed at a word boundary: the original continues past the cut with
    # a word character, so a mid-word cut would end in a partial token
    assert payload.split()[-1] in (
        "jumps", "over", "beyond", "old", "the", "far", "running")


# --- target election: PW retarget + descendant resolution ------------------
# Frappe's list view delegates row clicks: the element under the pointer is a
# plain div, the navigation target is the row's item link. The recorder must
# resolve DOWN to it instead of recording the container (task-2 regression).
DELEGATED_LIST = """
<div class="result no-assign-to" style="width:600px; padding-bottom:60px">
  <div class="list-row" style="position:relative; height:40px">
    <a href="/desk/item/test-01" onclick="event.preventDefault()"
       style="position:absolute; left:8px; top:10px">test</a>
  </div>
</div>
<div id="dead-zone" style="width:300px; height:100px"></div>
<div role="tab" id="tab-billing" style="width:120px"><span
     class="tab-label">Billing</span></div>
"""


@pytest.fixture(scope="module")
def election_events():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        events: list[dict] = []
        page.expose_binding(
            "__showAndTellRecord", lambda _source, payload: events.append(payload))
        page.set_content(DELEGATED_LIST)
        page.evaluate(_INJECT)
        # 1: row whitespace far right of the link — same visual row
        page.locator("div.result").click(position={"x": 400, "y": 20})
        # 2: container with nothing interactive anywhere near
        page.locator("#dead-zone").click(position={"x": 250, "y": 80})
        # 3: label span inside a [role=tab] — no hoisting to bare [role]
        page.locator("span.tab-label").click()
        browser.close()
        return [e for e in merge_recorded_clicks(events)
                if e["type"] == "click"]


def test_delegated_row_click_resolves_to_the_row_link(election_events):
    click = election_events[0]
    assert click["target"]["tag"] == "a", click["target"]
    assert 'role=link[name*="test"]' in click["target"]["selectors"]
    assert "position" not in click


def test_container_without_interactive_descendants_records_position(election_events):
    click = election_events[1]
    assert click["target"]["tag"] == "div"
    assert "#dead-zone" in click["target"]["selectors"]
    assert click["position"] == {"x": 250, "y": 80}


def test_bare_role_ancestors_are_no_longer_hoisted(election_events):
    click = election_events[2]
    # playwright's retarget list has no bare [role]; the span itself is
    # recorded (small element: replay's center click reproduces the gesture,
    # and the tab still receives it by bubbling).
    assert click["target"]["tag"] == "span"
    assert "position" not in click
    assert any(s.startswith("text=") for s in click["target"]["selectors"])


# --- review hardening: engine grammar, cap crowd-out, row intent -----------
HARDENING_PAGE = """
<div><span class="path-text">/app/config.yml</span></div>
<div><span class="quoted-text">'Save'</span></div>
<div class="wide-list" style="width:700px; padding-bottom:60px">
  <div class="row" style="position:relative; height:40px">
    <a id="row200" href="/desk/report/consolidated"
       onclick="event.preventDefault()"
       style="position:absolute; left:8px; top:10px">Consolidated revenue
       report 2024</a>
    <a class="comment-badge" href="/desk/comments"
       onclick="event.preventDefault()"
       style="position:absolute; left:640px; top:10px">3</a>
  </div>
</div>
<label for="item-name-field">Item Name</label>
<input id="item-name-field" type="text">
"""


@pytest.fixture(scope="module")
def hardening_events():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        events: list[dict] = []
        page.expose_binding(
            "__showAndTellRecord", lambda _source, payload: events.append(payload))
        page.set_content(HARDENING_PAGE)
        page.evaluate(_INJECT)
        page.locator("span.path-text").click()
        page.locator("span.quoted-text").click()
        # row whitespace: geometrically NEARER the badge (dx~80) than the
        # subject link, but >24px from anything — intent is the row's action
        page.locator("div.wide-list div.row").click(position={"x": 560, "y": 20})
        # directly beside the badge: the pointer wins
        page.locator("div.wide-list div.row").click(position={"x": 630, "y": 20})
        page.locator("#item-name-field").click()
        browser.close()
        return [e for e in merge_recorded_clicks(events)
                if e["type"] == "click"]


def test_unquoted_text_selector_never_starts_with_regex_or_quote(hardening_events):
    """text=/x/ parses as a REGEX in playwright's engine (SyntaxError on bad
    flags, silent regex match otherwise) and text='x' re-quotes; both must
    stay quoted-exact or be skipped entirely."""
    for click in hardening_events[:2]:
        for sel in click["target"]["selectors"]:
            if sel.startswith("text=") and not sel.startswith('text="'):
                body = sel[len("text="):]
                assert not body.startswith(("/", "'", '"')), sel
                assert not body.endswith(("'", '"')), sel


def test_cap_never_crowds_out_every_css_selector(hardening_events):
    """role=/text= fan-out must not evict all CSS entries: real-engine
    accessible names can diverge from the recorder's approximation, and the
    CSS rung is the resilience floor."""
    click = hardening_events[2]
    selectors = click["target"]["selectors"]
    assert len(selectors) <= 6
    assert any(not s.startswith(("role=", "text=")) for s in selectors), selectors


def test_row_whitespace_click_adopts_the_primary_leftmost_action(hardening_events):
    click = hardening_events[2]
    assert click["target"]["name"].startswith("Consolidated"), click["target"]
    assert "position" not in click


def test_click_beside_a_control_still_adopts_that_control(hardening_events):
    click = hardening_events[3]
    assert click["target"]["name"] == "3", click["target"]


def test_label_association_feeds_the_accessible_name(hardening_events):
    click = hardening_events[4]
    assert 'role=textbox[name*="Item Name"]' in click["target"]["selectors"]


# --- settled click transactions ---------------------------------------------
# The recorder announces click_begin at dispatch and patches it one task
# later; these tests pin the settlement evidence, the navigation-intent
# outcome, the loss-safety branch, and label-forward collapsing.

ROUTED_PAGE = """<html><body>
  <button id="save">Save</button>
  <script>save.addEventListener("click", event => event.preventDefault())</script>
  <a id="spa" href="/list"
     onclick="event.preventDefault(); history.pushState({}, '', '/list')">Open list</a>
  <a id="hard" href="/next-page">Next page</a>
  <input id="variants" type="checkbox" aria-label="Has Variants">
  <label id="variants-label" for="variants">Has Variants</label>
</body></html>"""


def _routed_events(gestures) -> list[dict]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        events: list[dict] = []
        page.expose_binding(
            "__showAndTellRecord", lambda _source, payload: events.append(payload))
        # pushState and the Navigation API need a real origin; about:blank
        # pages have none. The route serves every URL without a server.
        page.route("**/*", lambda route: route.fulfill(
            content_type="text/html", body=ROUTED_PAGE))
        page.context.add_init_script(_INJECT)
        page.goto("http://app.test/edit")
        gestures(page)
        page.wait_for_timeout(100)
        browser.close()
    return events


def test_late_listener_prevent_default_reaches_the_settled_click():
    """The capture-phase listener runs before the application's handler; only
    the settled patch can see the final cancelled flag."""
    events = _routed_events(lambda page: page.locator("#save").click())
    click = next(e for e in merge_recorded_clicks(events)
                 if e["type"] == "click")
    assert click["settlement"]["status"] == "settled"
    assert click["settlement"]["default_prevented"] is True
    assert click["settlement"]["target_connected"] is True


def test_spa_push_state_click_carries_navigation_intent():
    events = _routed_events(lambda page: page.locator("#spa").click())
    click = next(e for e in merge_recorded_clicks(events)
                 if e["type"] == "click")
    assert click["settlement"]["default_prevented"] is True
    assert click["settlement"]["url_after"] == "http://app.test/list"
    intent = next(o for o in click["outcomes"]
                  if o["class"] == "navigation_intent")
    assert intent["navigation_type"] == "push"
    assert intent["same_document"] is True
    assert intent["destination"] == "http://app.test/list"


def test_cross_document_navigation_preserves_the_begin():
    """The settle timer races the navigation commit, and either side may win
    depending on network latency. The invariant is the begin: it is sent
    during dispatch, before anything can destroy the document, so the click
    survives no matter who wins."""
    events = _routed_events(lambda page: page.locator("#hard").click())
    begins = [e for e in events if e["type"] == "click_begin"]
    assert len(begins) == 1
    clicks = [e for e in merge_recorded_clicks(events)
              if e["type"] == "click"]
    assert len(clicks) == 1
    # The successor document announces itself; that announcement is how the
    # host finalizes a begin-only transaction as settlement "unavailable".
    tokens = [e["doc_token"] for e in events if e["type"] == "document_ready"]
    assert len(tokens) == 2 and tokens[0] != tokens[1]
    intent = next(e for e in events if e["type"] == "click_outcome")
    assert intent["evidence"]["same_document"] is False


def test_label_click_records_one_action_with_the_controls_state():
    """The browser forwards a label click as a second trusted click on its
    control; one gesture must persist once, carrying the control's state."""
    events = _routed_events(
        lambda page: page.locator("#variants-label").click())
    clicks = [e for e in merge_recorded_clicks(events)
              if e["type"] == "click"]
    assert len(clicks) == 1
    assert clicks[0]["checked_before"] is False
    assert clicks[0]["checked_after"] is True


# --- browser-confirmed multi-click markers ----------------------------------
# A real double-click closes with a trusted dblclick; the recorder pairs it
# with the two click transactions through element REFERENCE equality and
# sends a click_multi marker. Phase 1 records evidence only: both clicks
# still persist, and nothing is ever inferred from two fast clicks alone.


def _marker_events(markup: str, gestures) -> list[dict]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        events: list[dict] = []
        page.expose_binding(
            "__showAndTellRecord", lambda _source, payload: events.append(payload))
        page.set_content(markup)
        page.evaluate(_INJECT)
        gestures(page)
        page.wait_for_timeout(50)   # outlast the click_settled patches
        browser.close()
    return events


def test_double_click_sends_a_marker_referencing_both_transactions():
    events = _marker_events(
        '<button id="open">Open report</button>',
        lambda page: page.locator("#open").dblclick(),
    )

    begins = [e for e in events if e["type"] == "click_begin"]
    markers = [e for e in events if e["type"] == "click_multi"]
    assert [e["detail"] for e in begins] == [1, 2]
    assert len(markers) == 1
    assert markers[0]["action_ids"] == [e["action_id"] for e in begins]
    assert markers[0]["detail"] == 2


def test_merged_double_click_carries_evidence_on_the_second_click():
    events = _marker_events(
        '<button id="open">Open report</button>',
        lambda page: page.locator("#open").dblclick(),
    )

    first, second = [e for e in merge_recorded_clicks(events)
                     if e["type"] == "click"]
    assert "multi_click_evidence" not in first
    assert second["multi_click_evidence"] == {
        "marker": "dblclick", "first_action_id": first["action_id"]}


def test_two_separate_clicks_and_a_scripted_dblclick_send_no_marker():
    """Neither timing nor a synthetic dblclick may fabricate a multi-click:
    independent clicks carry detail 1 each, and a constructed dblclick is
    untrusted."""
    def gestures(page):
        page.locator("#open").click()
        page.locator("#open").click()
        page.evaluate("document.getElementById('open').dispatchEvent("
                      "new MouseEvent('dblclick', {bubbles: true, detail: 2}))")

    events = _marker_events('<button id="open">Open report</button>', gestures)

    assert len([e for e in events if e["type"] == "click_begin"]) == 2
    assert not [e for e in events if e["type"] == "click_multi"]


def test_target_replaced_between_the_clicks_sends_no_marker():
    """A rerendered control breaks the pair by element identity; both clicks
    must stay independent actions."""
    markup = (
        '<div id="wrap"><button id="swap">Load</button></div>'
        '<script>let swapped = false;'
        'wrap.addEventListener("click", () => {'
        ' if (swapped) return; swapped = true;'
        ' const old = document.getElementById("swap");'
        ' old.replaceWith(old.cloneNode(true));'
        '});</script>')

    events = _marker_events(markup, lambda page: page.locator("#swap").dblclick())

    assert len([e for e in events if e["type"] == "click_begin"]) == 2
    assert not [e for e in events if e["type"] == "click_multi"]


def test_click_begin_carries_pointer_diagnostics():
    events = _marker_events(
        '<button id="open">Open report</button>',
        lambda page: page.locator("#open").click(),
    )

    begin = next(e for e in events if e["type"] == "click_begin")
    assert begin["pointer_type"] == "mouse"
    assert isinstance(begin["pointer_id"], int)
    assert begin["buttons"] == 0    # released by the time click dispatches
    assert set(begin["client_position"]) == {"x", "y"}


def test_enter_activation_records_a_trusted_non_pointer_click():
    """Keyboard activation stays annotation evidence: trusted, zero detail,
    empty pointer type, spec pointer id -1 — and never a multi-click."""
    def gestures(page):
        page.locator("#open").focus()
        page.keyboard.press("Enter")
        page.keyboard.press("Enter")

    events = _marker_events('<button id="open">Open report</button>', gestures)

    begins = [e for e in events if e["type"] == "click_begin"]
    assert len(begins) == 2
    for begin in begins:
        assert begin["is_trusted"] is True
        assert begin["detail"] == 0
        assert begin["pointer_type"] == ""
        assert begin["pointer_id"] == -1
    assert not [e for e in events if e["type"] == "click_multi"]


def test_label_double_click_pairs_the_label_transactions():
    """Each physical click forwards a second trusted click to the control;
    the forwarded copies are suppressed and never enter the marker ring."""
    events = _routed_events(
        lambda page: page.locator("#variants-label").dblclick())

    begins = [e for e in events if e["type"] == "click_begin"]
    markers = [e for e in events if e["type"] == "click_multi"]
    assert len(begins) == 2
    assert len(markers) == 1
    assert markers[0]["action_ids"] == [e["action_id"] for e in begins]


def test_replayed_consolidated_double_click_regenerates_dblclick():
    """End to end: capture a real double-click, normalize, generate, replay
    on a fresh page. The application must observe click 1, click 2, and ONE
    dblclick again — not two independent single clicks."""
    markup = (
        '<button id="report">Open report</button>'
        '<script>window.seen = [];'
        'const el = document.getElementById("report");'
        'el.addEventListener("click", e => seen.push(["click", e.detail]));'
        'el.addEventListener("dblclick", e => seen.push(["dblclick", e.detail]));'
        '</script>')
    captured = _marker_events(
        markup, lambda page: page.locator("#report").dblclick())

    actions = []
    for event in merge_recorded_clicks(captured):
        if event["type"] != "click":
            continue
        selectors = event["target"]["selectors"]
        actions.append({**event, "page": "page", "at_ms": len(actions) * 120,
                        "frame_url": None, "selector": selectors[0],
                        "selectors": selectors, "url": "http://app.test/edit"})
    actions = _dedupe(actions)
    assert [event.get("click_count") for event in actions] == [2]

    namespace = {}
    exec(render_demonstrate(
        actions, [{"id": "app", "url": "http://app.test/edit"}]), namespace)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(markup)
        namespace["demonstrate"](page, "http://app.test", {},
                                 lambda *_args: None)
        seen = page.evaluate("window.seen")
        browser.close()

    assert seen == [["click", 1], ["click", 2], ["dblclick", 2]]
