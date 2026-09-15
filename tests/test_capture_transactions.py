"""Ordered assembly of two-phase click transactions into persisted actions.

The recorder reports a click twice — an immediate ``click_begin`` that can
never be lost, and a best-effort ``click_settled`` patch one task later — and
the browser contributes confirmed outcomes independently. This module owns the
merge: one persisted ``type: "click"`` per gesture, emitted in authored order,
with settlement honestly marked ``unavailable`` when the document died first.
"""
from __future__ import annotations

from showAndTell.capture.transactions import (
    TransactionLog, merge_recorded_clicks, sanitize_url,
)


def _begin(action_id="doc1:1", frame_id="page:main", **extra):
    return {
        "type": "click_begin", "action_id": action_id, "frame_id": frame_id,
        "frame_sequence": extra.pop("frame_sequence", 1),
        "target": {"selectors": ["#save"]}, "timestamp": 1000,
        "url_before": "http://app.test/edit/7", **extra,
    }


def _settled(action_id="doc1:1", frame_id="page:main", **extra):
    return {
        "type": "click_settled", "action_id": action_id, "frame_id": frame_id,
        "default_prevented": False, "url_after": "http://app.test/edit/7",
        "target_connected": True, **extra,
    }


def _multi(action_ids=("doc1:1", "doc1:2"), frame_id="page:main", **extra):
    return {"type": "click_multi", "action_ids": list(action_ids),
            "frame_id": frame_id, "detail": 2, "timestamp": 1400, **extra}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _log(**kwargs):
    clock = Clock()
    log = TransactionLog(clock=clock, **kwargs)
    return log, clock


def test_begin_and_settled_merge_into_one_click():
    log, clock = _log()
    log.add("page", "frame", _begin(), recording=True)
    log.add("page", "frame", _settled(default_prevented=True,
                                      url_after="http://app.test/list"))
    clock.now = 1.0

    emitted = log.pump()

    assert len(emitted) == 1
    page, frame, event, recording = emitted[0]
    assert (page, frame, recording) == ("page", "frame", True)
    assert event["type"] == "click"
    assert event["action_id"] == "page:main:doc1:1"
    assert event["frame_sequence"] == 1
    assert event["url_before"] == "http://app.test/edit/7"
    assert event["settlement"] == {
        "status": "settled", "default_prevented": True,
        "url_after": "http://app.test/list", "target_connected": True,
    }


def test_checked_after_is_flattened_where_codegen_reads_it():
    log, clock = _log()
    log.add("page", "frame", _begin(checked_before=False))
    log.add("page", "frame", _settled(checked_after=True))
    clock.now = 1.0

    (_, _, event, _), = log.pump()

    assert event["checked_before"] is False
    assert event["checked_after"] is True


def test_later_action_cannot_pass_an_unresolved_earlier_click():
    log, clock = _log()
    log.add("page", "frame", _begin())
    log.add("page", "frame", {"type": "fill", "frame_id": "page:main",
                              "value": "Nos"})
    clock.now = 0.3  # past the paint delay, before the settle deadline

    assert log.pump() == []

    log.add("page", "frame", _settled())
    emitted = log.pump()
    assert [event["type"] for _, _, event, _ in emitted] == ["click", "fill"]


def test_paint_delay_holds_emission_without_serializing_a_burst():
    log, clock = _log()
    log.add("page", "frame", _begin(action_id="doc1:1"))
    log.add("page", "frame", _settled(action_id="doc1:1"))
    clock.now = 0.05
    log.add("page", "frame", _begin(action_id="doc1:2", frame_sequence=2))
    log.add("page", "frame", _settled(action_id="doc1:2"))

    assert log.pump() == []
    clock.now = 0.19  # first click's paint deadline passed, second's has not
    assert len(log.pump()) == 1
    clock.now = 0.24  # overlapping waits: 50 ms later, not 180 ms later
    assert len(log.pump()) == 1


def test_settle_deadline_finalizes_a_begin_only_click():
    log, clock = _log()
    log.add("page", "frame", _begin())
    clock.now = 0.6

    (_, _, event, _), = log.pump()

    assert event["settlement"] == {"status": "unavailable",
                                   "reason": "settle_deadline"}


def test_document_ready_from_a_new_document_finalizes_the_old_click():
    log, clock = _log()
    log.add("page", "frame", _begin(action_id="olddoc:3"))
    log.add("page", "frame", {"type": "document_ready", "doc_token": "newdoc",
                              "frame_id": "page:main"})
    clock.now = 0.2

    (_, _, event, _), = log.pump()

    assert event["settlement"] == {"status": "unavailable",
                                   "reason": "document_navigated"}


def test_document_ready_for_the_same_document_does_not_finalize():
    log, clock = _log()
    log.add("page", "frame", _begin(action_id="doc1:1"))
    log.add("page", "frame", {"type": "document_ready", "doc_token": "doc1",
                              "frame_id": "page:main"})
    clock.now = 0.2

    assert log.pump() == []  # still open, waiting for its settled patch


def test_page_close_and_frame_detach_finalize_open_clicks():
    log, clock = _log()
    log.add("pageA", "frame", _begin(action_id="doc1:1"))
    log.add("pageB", "frame2", _begin(action_id="doc2:1",
                                      frame_id="page2:frame-1"))
    log.page_closed("pageA")
    log.frame_detached("page2:frame-1")
    clock.now = 0.2

    events = [event for _, _, event, _ in log.pump()]

    assert events[0]["settlement"]["reason"] == "page_closed"
    assert events[1]["settlement"]["reason"] == "frame_detached"


def test_flush_finalizes_and_drains_immediately():
    log, _clock = _log()
    log.add("page", "frame", _begin())
    log.add("page", "frame", {"type": "press", "frame_id": "page:main",
                              "key": "Enter"})

    emitted = log.flush()

    assert [event["type"] for _, _, event, _ in emitted] == ["click", "press"]
    assert emitted[0][2]["settlement"] == {"status": "unavailable",
                                           "reason": "recording_stopped"}


def test_late_settled_patch_upgrades_an_already_emitted_click():
    log, clock = _log()
    log.add("page", "frame", _begin())
    clock.now = 0.6
    (_, _, event, _), = log.pump()
    assert event["settlement"]["status"] == "unavailable"

    log.add("page", "frame", _settled(default_prevented=True))

    assert event["settlement"]["status"] == "settled"
    assert event["settlement"]["default_prevented"] is True


def test_click_outcome_evidence_attaches_to_the_transaction():
    log, clock = _log()
    log.add("page", "frame", _begin())
    log.add("page", "frame", {
        "type": "click_outcome", "action_id": "doc1:1",
        "frame_id": "page:main",
        "evidence": {"class": "navigation_intent", "kind": "navigation",
                     "navigation_type": "push", "same_document": True,
                     "destination": "http://app.test/list"},
    })
    log.add("page", "frame", _settled())
    clock.now = 1.0

    (_, _, event, _), = log.pump()

    assert event["outcomes"] == [{
        "class": "navigation_intent", "kind": "navigation",
        "navigation_type": "push", "same_document": True,
        "destination": "http://app.test/list",
    }]


def test_frame_navigation_confirms_the_open_click_and_names_the_reason():
    log, clock = _log()
    log.add("page", "frame", _begin())
    log.frame_navigated("page", "page:main", "http://app.test/next?token=x#f")
    clock.now = 0.6

    (_, _, event, _), = log.pump()

    assert event["settlement"]["reason"] == "document_navigated"
    assert event["outcomes"] == [{
        "class": "browser_confirmed", "kind": "frame_navigation",
        "destination": "http://app.test/next",
    }]


def test_frame_navigation_confirms_a_recent_click_after_emission():
    log, clock = _log()
    log.add("page", "frame", _begin())
    log.add("page", "frame", _settled(url_after="http://app.test/list"))
    clock.now = 1.0
    (_, _, event, _), = log.pump()

    log.frame_navigated("page", "page:main", "http://app.test/list")

    assert event["outcomes"][-1]["kind"] == "frame_navigation"


def test_stale_or_superseded_clicks_never_absorb_a_navigation():
    log, clock = _log()
    log.add("page", "frame", _begin())
    log.add("page", "frame", _settled())
    clock.now = 10.0  # long past the confirmation window
    (_, _, stale, _), = log.pump()
    log.frame_navigated("page", "page:main", "http://app.test/late")
    assert "outcomes" not in stale

    # A later authored action on the frame also breaks attribution: the
    # navigation may belong to the Enter press, not the old click.
    log.add("page", "frame", _begin(action_id="doc1:9"))
    log.add("page", "frame", _settled(action_id="doc1:9"))
    log.add("page", "frame", {"type": "press", "frame_id": "page:main",
                              "key": "Enter"})
    log.frame_navigated("page", "page:main", "http://app.test/enter")
    clock.now = 11.0
    emitted = log.pump()
    click = next(e for _, _, e, _ in emitted if e["type"] == "click")
    assert "outcomes" not in click


def test_popup_and_download_attach_through_the_owning_page():
    log, clock = _log()
    log.add("page", "frame", _begin())
    log.add("page", "frame", _settled())
    log.popup_opened("page", "http://app.test/print?id=4")
    log.download_started("page", "http://app.test/export.csv?auth=t")
    log.popup_opened("otherpage", "http://app.test/unrelated")
    clock.now = 1.0

    (_, _, event, _), = log.pump()

    assert [o["kind"] for o in event["outcomes"]] == ["popup", "download"]
    assert event["outcomes"][0]["destination"] == "http://app.test/print"
    assert event["outcomes"][1]["destination"] == "http://app.test/export.csv"


def test_internal_messages_never_become_persisted_events():
    log, clock = _log()
    log.add("page", "frame", _begin())
    log.add("page", "frame", _settled())
    log.add("page", "frame", {"type": "document_ready", "doc_token": "doc1",
                              "frame_id": "page:main"})
    log.add("page", "frame", {
        "type": "click_outcome", "action_id": "doc1:1",
        "frame_id": "page:main", "evidence": {"class": "navigation_intent"},
    })
    log.add("page", "frame", _multi(action_ids=("doc1:1", "doc1:1")))
    clock.now = 1.0

    emitted = log.pump()

    assert [event["type"] for _, _, event, _ in emitted] == ["click"]


def test_sanitize_url_strips_query_and_fragment():
    assert sanitize_url("http://a.test/path?q=1#frag") == "http://a.test/path"
    assert sanitize_url("about:blank") == "about:blank"
    assert sanitize_url(None) == ""


def test_merge_recorded_clicks_reassembles_a_raw_recorder_stream():
    merged = merge_recorded_clicks([
        {"type": "document_ready", "doc_token": "doc1"},
        {"type": "click_begin", "action_id": "doc1:1",
         "target": {"selectors": ["#variants"]}, "checked_before": False,
         "timestamp": 5},
        {"type": "fill", "value": "Nos"},
        {"type": "click_settled", "action_id": "doc1:1",
         "default_prevented": False, "checked_after": True,
         "url_after": "about:blank", "target_connected": True},
        {"type": "click_outcome", "action_id": "doc1:1",
         "evidence": {"class": "navigation_intent", "kind": "navigation"}},
    ])

    assert [event["type"] for event in merged] == ["click", "fill"]
    click = merged[0]
    assert click["checked_before"] is False
    assert click["checked_after"] is True
    assert click["settlement"]["status"] == "settled"
    assert click["outcomes"][0]["class"] == "navigation_intent"
    # A raw recorder stream has no frame identity yet; the id stays local.
    assert click["action_id"] == "doc1:1"


# --- browser-confirmed multi-click markers -----------------------------------
# A trusted dblclick pairs the two click transactions it closes. Phase 1 is
# observability only: the evidence rides on the second click; nothing is
# consolidated, and a marker that cannot name both transactions is dropped.


def test_click_multi_attaches_dblclick_evidence_to_the_second_click():
    log, clock = _log()
    log.add("page", "frame", _begin(action_id="doc1:1", detail=1))
    log.add("page", "frame", _settled(action_id="doc1:1"))
    log.add("page", "frame", _begin(action_id="doc1:2", frame_sequence=2,
                                    detail=2))
    log.add("page", "frame", _settled(action_id="doc1:2"))
    log.add("page", "frame", _multi())
    clock.now = 1.0

    first, second = [event for _, _, event, _ in log.pump()]

    assert "multi_click_evidence" not in first
    assert second["multi_click_evidence"] == {
        "marker": "dblclick", "first_action_id": "page:main:doc1:1"}


def test_click_multi_attaches_after_the_clicks_were_already_emitted():
    # The marker can trail the pair by the double-click threshold (~500 ms),
    # long past the 180 ms paint delay that released the first click.
    log, clock = _log()
    log.add("page", "frame", _begin(action_id="doc1:1"))
    log.add("page", "frame", _settled(action_id="doc1:1"))
    log.add("page", "frame", _begin(action_id="doc1:2", frame_sequence=2))
    log.add("page", "frame", _settled(action_id="doc1:2"))
    clock.now = 1.0
    _, second = [event for _, _, event, _ in log.pump()]

    log.add("page", "frame", _multi())

    assert second["multi_click_evidence"]["first_action_id"] == "page:main:doc1:1"


def test_click_multi_missing_either_transaction_is_dropped():
    log, clock = _log()
    log.add("page", "frame", _begin(action_id="doc1:2"))
    log.add("page", "frame", _settled(action_id="doc1:2"))
    log.add("page", "frame", _multi())  # doc1:1 died with its document
    clock.now = 1.0

    (_, _, event, _), = log.pump()

    assert "multi_click_evidence" not in event


def test_click_multi_from_another_frame_never_reaches_the_pair():
    log, clock = _log()
    log.add("page", "frame", _begin(action_id="doc1:1"))
    log.add("page", "frame", _begin(action_id="doc1:2", frame_sequence=2))
    log.add("page", "frame", _multi(frame_id="page:frame-1"))
    log.add("page", "frame", _settled(action_id="doc1:1"))
    log.add("page", "frame", _settled(action_id="doc1:2"))
    clock.now = 1.0

    events = [event for _, _, event, _ in log.pump()]

    assert all("multi_click_evidence" not in event for event in events)


def test_merge_recorded_clicks_attaches_multi_click_evidence():
    merged = merge_recorded_clicks([
        {"type": "click_begin", "action_id": "doc1:1", "detail": 1,
         "target": {"selectors": ["#open"]}, "timestamp": 5},
        {"type": "click_settled", "action_id": "doc1:1",
         "default_prevented": False, "url_after": "about:blank",
         "target_connected": True},
        {"type": "click_begin", "action_id": "doc1:2", "detail": 2,
         "target": {"selectors": ["#open"]}, "timestamp": 160},
        {"type": "click_settled", "action_id": "doc1:2",
         "default_prevented": False, "url_after": "about:blank",
         "target_connected": True},
        {"type": "click_multi", "action_ids": ["doc1:1", "doc1:2"],
         "detail": 2, "timestamp": 165},
    ])

    first, second = merged
    assert "multi_click_evidence" not in first
    assert second["multi_click_evidence"] == {
        "marker": "dblclick", "first_action_id": "doc1:1"}


def test_merge_recorded_clicks_ignores_a_marker_naming_unknown_clicks():
    merged = merge_recorded_clicks([
        {"type": "click_begin", "action_id": "doc1:2", "detail": 2,
         "target": {"selectors": ["#open"]}, "timestamp": 160},
        {"type": "click_multi", "action_ids": ["doc1:1", "doc1:2"],
         "detail": 2, "timestamp": 165},
    ])

    assert [event["type"] for event in merged] == ["click"]
    assert "multi_click_evidence" not in merged[0]


def test_diagnostics_counts_activation_sources_and_marker_flow():
    log, _clock = _log()
    log.add("page", "frame", _begin(action_id="doc1:1", is_trusted=True,
                                    pointer_type="mouse"))
    log.add("page", "frame", _begin(action_id="doc1:2", frame_sequence=2,
                                    is_trusted=True, pointer_type=""))
    log.add("page", "frame", _begin(action_id="doc1:3", frame_sequence=3,
                                    is_trusted=False, pointer_type=""))
    log.add("page", "frame", _begin(action_id="doc1:4", frame_sequence=4))
    log.add("page", "frame", _multi(action_ids=("doc1:1", "doc1:2")))
    log.add("page", "frame", _multi(action_ids=("doc1:9", "doc1:2")))

    assert log.diagnostics() == {
        "clicks_pointer": 1,
        "clicks_non_pointer": 1,
        "clicks_programmatic": 1,
        "clicks_unknown": 1,
        "multi_click_markers": 2,
        "multi_click_evidence_attached": 1,
        "multi_click_markers_dropped": 1,
    }
