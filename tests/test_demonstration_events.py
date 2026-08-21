"""Collapsing a raw captured event stream into demonstration actions.

Every test here drives showAndTell.demonstration.events._dedupe directly: the pass is
pure over event dicts, so no browser, capture thread, or replay stack is
involved.
"""
from __future__ import annotations

from showAndTell.demonstration.events import _dedupe


def test_dedupe_drops_restore_of_already_active_tab():
    first = {"type": "tab_switch", "page": "page", "at_ms": 0}
    click = {"type": "click", "page": "page", "at_ms": 100,
             "selector": "#open"}
    restored = {"type": "tab_switch", "page": "page", "at_ms": 200}
    switched = {"type": "tab_switch", "page": "page2", "at_ms": 300}

    assert _dedupe([first, click, restored, switched]) == [
        first, click, switched]


def test_dedupe_never_collapses_fills_without_a_selector():
    """Two selector-less fills on different dialog fields must both survive:
    None == None made the item-code fill vanish into the item-name fill, so
    replay never typed the code at all."""

    events = _dedupe([
        {"type": "fill", "page": "page", "selector": None, "value": "PC-003"},
        {"type": "fill", "page": "page", "selector": None, "value": "Pencil HB"},
        {"type": "fill", "page": "page", "selector": "#code", "value": "a"},
        {"type": "fill", "page": "page", "selector": "#code", "value": "ab"},
    ])
    assert [e["value"] for e in events] == ["PC-003", "Pencil HB", "ab"]


def test_dedupe_drops_spa_search_clear_emitted_by_result_navigation():
    """A search dialog can clear itself while its result click navigates.

    Evidence collection may attach the destination URL to the debounced empty
    fill just before the click. That clear is lifecycle noise, not a gesture.
    """

    opened = {
        "type": "click", "page": "page", "at_ms": 100,
        "url": "http://fixture.test/desk/item", "selector": "#search",
    }
    synthetic_clear = {
        "type": "fill", "page": "page", "at_ms": 200,
        "url": "http://fixture.test/desk/sales-order",
        "selector": "#navbar-search", "value": "",
    }
    result_click = {
        "type": "click", "page": "page", "at_ms": 231,
        "url": "http://fixture.test/desk/sales-order",
        "selector": "a[href='/desk/sales-order']",
    }

    assert _dedupe([opened, synthetic_clear, result_click]) == [
        opened, result_click,
    ]

    # A slower, deliberate clear remains replayable.
    deliberate = {**result_click, "at_ms": 500}
    assert _dedupe([opened, synthetic_clear, deliberate]) == [
        opened, synthetic_clear, deliberate,
    ]


def test_dedupe_moves_narration_from_dropped_search_clear_to_click():
    """Dropping the synthetic clear must not silence narration aligned to it.

    Stored drafts are re-deduped at render after align_narration has already
    attached voice segments; the surviving click inherits the clear's keys.
    """

    opened = {
        "type": "click", "page": "page", "at_ms": 100,
        "url": "http://fixture.test/desk/item", "selector": "#search",
    }
    synthetic_clear = {
        "type": "fill", "page": "page", "at_ms": 200,
        "url": "http://fixture.test/desk/sales-order",
        "selector": "#navbar-search", "value": "",
        "narration_keys": ["voice-002"],
    }
    result_click = {
        "type": "click", "page": "page", "at_ms": 231,
        "url": "http://fixture.test/desk/sales-order",
        "selector": "a[href='/desk/sales-order']",
        "narration_keys": ["voice-003"],
    }

    assert _dedupe([opened, synthetic_clear, result_click]) == [
        opened,
        {**result_click, "narration_keys": ["voice-002", "voice-003"]},
    ]
    # Stored drafts are re-deduped from these same dicts; never mutate them.
    assert result_click["narration_keys"] == ["voice-003"]


def test_dedupe_keeps_clear_preceded_by_a_same_page_focus_click():
    """A deliberate clear follows a focus click on the same URL.

    The synthetic-clear signature requires the URL to have changed at the
    clear; a stable URL means a real user gesture that must replay.
    """

    focus = {
        "type": "click", "page": "page", "at_ms": 100,
        "url": "http://fixture.test/desk/sales-order",
        "selector": "#navbar-search",
    }
    clear = {
        "type": "fill", "page": "page", "at_ms": 200,
        "url": "http://fixture.test/desk/sales-order",
        "selector": "#navbar-search", "value": "",
    }
    click = {
        "type": "click", "page": "page", "at_ms": 231,
        "url": "http://fixture.test/desk/sales-order",
        "selector": "a[href='/desk/sales-order']",
    }

    assert _dedupe([focus, clear, click]) == [focus, clear, click]


def test_dedupe_collapses_onlyoffice_host_echo_to_real_frame_action():
    """ONLYOFFICE reports one physical cell click from both the editor iframe
    and its host-page accessibility canvas.  The iframe copy has replayable
    local coordinates; the host echo must not click the cell a second time."""

    frame = {
        "type": "click", "page": "page2", "at_ms": 25669,
        "frame_url": "http://document-server/editor",
        "selector": "#ws-canvas-graphic-overlay",
        "position": {"x": 552, "y": 409},
        "target": {"tag": "canvas", "name": ""},
    }
    host = {
        "type": "click", "page": "page2", "at_ms": 25670,
        "frame_url": None, "selector": '[aria-label="Cell F22"]',
        "position": {"x": 10592, "y": 585},
        "target": {"tag": "canvas", "name": "Cell F22"},
        "narration_keys": ["voice-001"],
    }

    events = _dedupe([frame, host])

    assert events == [{
        **frame,
        "cell_ref": "F22",
        "description": "click Cell F22",
        "narration_keys": ["voice-001"],
        "echo_collapsed": True,
    }]


def test_dedupe_preserves_distinct_payloads_within_an_echo_burst():
    """A host event near an iframe event is only an echo when it
    reports the same gesture.  A different key or value is a second real
    action — a debounced fill flushing next to another document's fill, or
    Enter's synthetic click landing beside a Tab — and must survive."""

    frame_url = "http://document-server/editor"
    presses = _dedupe([
        {"type": "press", "page": "page2", "at_ms": 100,
         "frame_url": frame_url, "key": "Enter"},
        {"type": "press", "page": "page2", "at_ms": 101,
         "frame_url": None, "key": "Tab"},
    ])
    assert [event["key"] for event in presses] == ["Enter", "Tab"]

    fills = _dedupe([
        {"type": "fill", "page": "page", "at_ms": 200,
         "frame_url": frame_url, "selector": "#composebody",
         "value": "compose body"},
        {"type": "fill", "page": "page", "at_ms": 201,
         "frame_url": None, "selector": "#compose-subject",
         "value": "Quarterly totals"},
    ])
    assert [event["value"] for event in fills] == [
        "compose body", "Quarterly totals"]


def test_dedupe_tolerates_measured_payload_echo_delivery_jitter():
    """Exact host/iframe payload echoes observed in recorded tasks can arrive
    four milliseconds apart, including when a commit key is interleaved."""

    frame_url = "http://document-server/editor"
    events = _dedupe([
        {"type": "type", "page": "page2", "at_ms": 100,
         "frame_url": frame_url, "text": "Ship"},
        {"type": "press", "page": "page2", "at_ms": 102,
         "frame_url": frame_url, "key": "Enter"},
        {"type": "type", "page": "page2", "at_ms": 104,
         "frame_url": None, "text": "Ship"},
    ])

    assert [(event["type"], event.get("text"), event.get("key"))
            for event in events] == [
                ("type", "Ship", None),
                ("press", None, "Enter"),
            ]
    assert events[0]["echo_collapsed"] is True


def test_dedupe_keeps_payloadless_clicks_outside_tight_echo_window():
    """Clicks lack a matching semantic payload, so four-millisecond timing
    proximity across frames is not enough to claim that they are one action."""

    events = _dedupe([
        {"type": "click", "page": "page2", "at_ms": 100,
         "frame_url": "http://document-server/editor", "selector": "#cell"},
        {"type": "click", "page": "page2", "at_ms": 104,
         "frame_url": None, "selector": "#save"},
    ])

    assert [event["selector"] for event in events] == ["#cell", "#save"]


def test_dedupe_does_not_merge_plain_and_modified_enter():
    events = _dedupe([
        {"type": "press", "page": "page", "at_ms": 100,
         "frame_url": "http://editor", "key": "Enter", "modifiers": []},
        {"type": "press", "page": "page", "at_ms": 101,
         "frame_url": None, "key": "Enter", "modifiers": ["Meta"]},
    ])

    assert [event.get("modifiers") for event in events] == [[], ["Meta"]]


def test_dedupe_is_idempotent_across_pipeline_stages():
    """_dedupe runs at capture finalization, again while authoring the draft,
    and again at render time.  A pass over an already-collapsed stream must
    not absorb a neighbouring real action into the merged pair."""

    events = [
        # Enter's synthetic click on the host document...
        {"type": "click", "page": "page", "at_ms": 100, "frame_url": None,
         "selector": "#send"},
        # ...followed by a cell click reported by both observers.
        {"type": "click", "page": "page", "at_ms": 101, "frame_url": None,
         "selector": '[aria-label="Cell A1"]'},
        {"type": "click", "page": "page", "at_ms": 102,
         "frame_url": "http://document-server/editor",
         "selector": "#ws-canvas-graphic-overlay"},
    ]

    once = _dedupe(events)
    twice = _dedupe([dict(event) for event in once])

    assert [event["selector"] for event in once] == [
        "#send", "#ws-canvas-graphic-overlay"]
    assert twice == once


def test_dedupe_leaves_the_stored_events_unmutated():
    """render_demonstrate re-dedupes stored draft events; the caller's dicts
    must not gain merged narration keys or markers behind its back."""

    frame = {"type": "click", "page": "page", "at_ms": 100,
             "frame_url": "http://document-server/editor", "selector": "#c"}
    host = {"type": "click", "page": "page", "at_ms": 101, "frame_url": None,
            "selector": '[aria-label="Cell A1"]',
            "narration_keys": ["voice-001"]}

    _dedupe([frame, host])

    assert "narration_keys" not in frame
    assert "echo_collapsed" not in frame


def test_dedupe_matches_interleaved_onlyoffice_text_and_key_echoes():
    """The host's text echo can arrive after the iframe's following Tab, so
    duplicate matching must cover the complete same-millisecond burst."""

    frame_url = "http://document-server/editor"
    events = _dedupe([
        {"type": "type", "page": "page2", "at_ms": 100,
         "frame_url": frame_url, "text": "roved"},
        {"type": "press", "page": "page2", "at_ms": 101,
         "frame_url": frame_url, "key": "Tab"},
        {"type": "type", "page": "page2", "at_ms": 101,
         "frame_url": None, "text": "roved"},
        {"type": "press", "page": "page2", "at_ms": 101,
         "frame_url": None, "key": "Tab"},
    ])

    assert [(event["type"], event.get("text"), event.get("key"))
            for event in events] == [
                ("type", "roved", None),
                ("press", None, "Tab"),
            ]


def test_dedupe_upgrades_partial_cross_frame_text_before_commit_key():
    """A child editor fragment and complete host mirror are one transaction.

    The host echo may arrive after the commit key. Keeping the child event in
    its original slot, but upgrading its text, produces the replayable order
    `type NA`, then `press Enter`.
    """

    events = _dedupe([
        {"type": "type", "page": "page2", "at_ms": 100,
         "frame_id": "page2:frame-1", "frame_url": "http://editor",
         "frame_sequence": 8, "text": "A"},
        {"type": "press", "page": "page2", "at_ms": 101,
         "frame_id": "page2:frame-1", "frame_url": "http://editor",
         "frame_sequence": 9, "key": "Enter"},
        {"type": "type", "page": "page2", "at_ms": 101,
         "frame_id": "page2:main", "frame_url": None,
         "frame_sequence": 14, "text": "NA"},
    ])

    assert [(event["type"], event.get("text"), event.get("key"))
            for event in events] == [
                ("type", "NA", None),
                ("press", None, "Enter"),
            ]
    assert events[0]["frame_id"] == "page2:frame-1"
    assert events[0]["echo_collapsed"] is True


def test_dedupe_uses_stable_frame_ids_not_url_presence():
    """Two explicitly identified documents can echo even with similar URLs."""

    events = _dedupe([
        {"type": "type", "page": "page", "at_ms": 20,
         "frame_id": "page:frame-1", "frame_url": "http://same.test/app",
         "text": "Approved"},
        {"type": "type", "page": "page", "at_ms": 21,
         "frame_id": "page:main", "frame_url": "http://same.test/app",
         "text": "Approved"},
    ])

    assert len(events) == 1
    assert events[0]["frame_id"] == "page:frame-1"


def test_dedupe_preserves_unrelated_cross_frame_text_in_same_burst():
    """Timing and different frames alone never establish echo identity."""

    events = _dedupe([
        {"type": "type", "page": "page", "at_ms": 20,
         "frame_id": "page:frame-1", "text": "A"},
        {"type": "type", "page": "page", "at_ms": 21,
         "frame_id": "page:main", "text": "B"},
    ])

    assert [event["text"] for event in events] == ["A", "B"]


def _double_click_pair(**second_extra):
    """A browser-confirmed double-click as Phase-1 capture persists it."""
    first = {
        "type": "click", "page": "page", "at_ms": 100, "frame_url": None,
        "selector": "#open", "selectors": ["#open"],
        "action_id": "page:main:doc1:1", "detail": 1,
        "url": "http://app.test/edit", "url_before": "http://app.test/edit",
        "settlement": {"status": "settled", "default_prevented": False},
        "outcomes": [{"class": "navigation_intent", "kind": "navigation"}],
        "narration_keys": ["voice-001"],
        "screenshot": "capture_bundle/screenshot_line1.png",
        "target": {"tag": "button", "role": "button", "name": "Open"},
    }
    second = {
        "type": "click", "page": "page", "at_ms": 260, "frame_url": None,
        "selector": "#open", "selectors": ["#open"],
        "action_id": "page:main:doc1:2", "detail": 2,
        "url": "http://app.test/edit#opened",
        "url_before": "http://app.test/edit",
        "settlement": {"status": "settled", "default_prevented": True},
        "outcomes": [{"class": "browser_confirmed", "kind": "popup"}],
        "narration_keys": ["voice-002"],
        "screenshot": "capture_bundle/screenshot_line2.png",
        "target": {"tag": "button", "role": "button", "name": "Open"},
        "multi_click_evidence": {"marker": "dblclick",
                                 "first_action_id": "page:main:doc1:1"},
        **second_extra,
    }
    return first, second


def test_dedupe_consolidates_a_browser_confirmed_double_click():
    """One semantic action: identity from the first click, post-gesture state
    from the second, evidence from both — nothing invented, nothing lost."""

    first, second = _double_click_pair()

    (merged,) = _dedupe([first, second])

    assert merged["click_count"] == 2
    assert merged["constituent_action_ids"] == [
        "page:main:doc1:1", "page:main:doc1:2"]
    # identity: the first click's
    assert merged["action_id"] == "page:main:doc1:1"
    assert merged["at_ms"] == 100
    assert merged["screenshot"] == "capture_bundle/screenshot_line1.png"
    assert merged["url_before"] == "http://app.test/edit"
    # post-gesture state: the second click's
    assert merged["detail"] == 2
    assert merged["settlement"]["default_prevented"] is True
    assert merged["url"] == "http://app.test/edit#opened"
    # evidence union, order preserved; the consumed marker never survives
    assert [o["class"] for o in merged["outcomes"]] == [
        "navigation_intent", "browser_confirmed"]
    assert merged["narration_keys"] == ["voice-001", "voice-002"]
    assert "multi_click_evidence" not in merged


def test_double_clicked_checkbox_merges_to_its_final_state():
    """Two toggles net to the SECOND click's settled state; taking the first
    click's checked_after would make set_checked flip the box wrongly."""

    first, second = _double_click_pair(checked_after=False)
    first["checked_before"] = False
    first["checked_after"] = True

    (merged,) = _dedupe([first, second])

    assert merged["checked_before"] is False
    assert merged["checked_after"] is False


def test_evidence_naming_an_unknown_click_leaves_both_actions():
    first, second = _double_click_pair()
    second["multi_click_evidence"] = {
        "marker": "dblclick", "first_action_id": "page:main:gone:9"}

    events = _dedupe([first, second])

    assert [e["action_id"] for e in events] == [
        "page:main:doc1:1", "page:main:doc1:2"]
    assert all("click_count" not in e for e in events)


def test_two_fast_clicks_without_evidence_stay_two_actions():
    """Timing never becomes identity: no marker, no consolidation."""

    first, second = _double_click_pair()
    del second["multi_click_evidence"]

    events = _dedupe([first, second])

    assert len(events) == 2
    assert all("click_count" not in e for e in events)


def test_multi_click_consolidation_is_idempotent():
    first, second = _double_click_pair()

    once = _dedupe([first, second])
    twice = _dedupe([dict(event) for event in once])

    assert twice == once


def test_multi_click_consolidation_leaves_caller_dicts_unmutated():
    first, second = _double_click_pair()

    _dedupe([first, second])

    assert "click_count" not in first
    assert first["narration_keys"] == ["voice-001"]
    assert "multi_click_evidence" in second


def test_multi_click_composes_with_the_onlyoffice_host_echo():
    """Echo collapse resolves each click's two frame copies first; the
    multi-click pair then merges the retained iframe copies, keeping the
    stable cell identity the host mirror contributed."""

    frame_url = "http://document-server/editor"

    def frame_click(at_ms, action_id, detail, **extra):
        return {"type": "click", "page": "page2", "at_ms": at_ms,
                "frame_id": "page2:frame-1", "frame_url": frame_url,
                "selector": "#ws-canvas-graphic-overlay",
                "action_id": action_id, "detail": detail,
                "position": {"x": 552, "y": 409},
                "target": {"tag": "canvas", "name": ""}, **extra}

    def host_echo(at_ms):
        return {"type": "click", "page": "page2", "at_ms": at_ms,
                "frame_id": "page2:main", "frame_url": None,
                "selector": '[aria-label="Cell F22"]',
                "target": {"tag": "canvas", "name": "Cell F22"}}

    events = _dedupe([
        frame_click(100, "page2:frame-1:doc1:1", 1),
        host_echo(101),
        frame_click(260, "page2:frame-1:doc1:2", 2, multi_click_evidence={
            "marker": "dblclick", "first_action_id": "page2:frame-1:doc1:1"}),
        host_echo(261),
    ])

    (merged,) = events
    assert merged["click_count"] == 2
    assert merged["cell_ref"] == "F22"
    assert merged["frame_id"] == "page2:frame-1"
    assert merged["position"] == {"x": 552, "y": 409}


def test_synthetic_clear_dedupe_still_fires_on_settled_clicks():
    """The rule matches the click by its destination URL; the two-phase merge
    must keep the persisted url post-action or this silently stops working."""
    opened = {
        "type": "click", "page": "page", "at_ms": 100,
        "url": "http://app.test/desk/item", "selector": "#search",
    }
    synthetic_clear = {
        "type": "fill", "page": "page", "at_ms": 200,
        "url": "http://app.test/desk/sales-order",
        "selector": "#navbar-search", "value": "",
    }
    settled_click = {
        "type": "click", "page": "page", "at_ms": 231,
        "url": "http://app.test/desk/sales-order",
        "selector": "a[href='/desk/sales-order']",
        "action_id": "page:main:doc1:2",
        "url_before": "http://app.test/desk/item",
        "settlement": {"status": "settled", "default_prevented": True,
                       "url_after": "http://app.test/desk/sales-order"},
        "outcomes": [{"class": "navigation_intent", "kind": "navigation"}],
    }

    assert _dedupe([opened, synthetic_clear, settled_click]) == [
        opened, settled_click,
    ]
