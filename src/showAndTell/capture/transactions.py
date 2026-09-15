"""Ordered click-transaction assembly for managed capture.

The in-page recorder reports one click as internal protocol messages — an
immediate ``click_begin`` that reserves identity and order before navigation
can destroy the document, a best-effort ``click_settled`` patch on the next
task carrying final event and control state, and ``click_outcome`` evidence
from the Navigation API. The browser side contributes confirmed outcomes
(committed navigations, popups, downloads) through the host's Playwright
listeners. This module folds all of it into exactly one persisted
``type: "click"`` action per gesture, emitted in authored order.

Everything here is pure logic driven by an injected clock; capture.runtime
owns the side effects (screenshots, accessibility trees, event-list appends)
and performs them when :meth:`TransactionLog.pump` releases an event. Emitted
event dicts stay mutable on purpose: a navigation that commits seconds after
its click can still attach ``browser_confirmed`` evidence to the already
released action, because events.jsonl is only written when capture ends.
"""
from __future__ import annotations

import time
from collections import Counter, deque
from urllib.parse import urlsplit, urlunsplit

# Internal protocol message types. They travel over the recorder binding but
# must never become persisted events or enter capture_events.ACTION_TYPES.
INTERNAL_EVENT_TYPES = {
    "click_begin", "click_settled", "click_outcome", "click_multi",
    "document_ready",
}

# How long a released event's screenshot waits after the gesture, preserving
# the post-action visuals the old fixed 180 ms host sleep produced — but as a
# per-transaction deadline, so a burst of gestures overlaps its waits instead
# of accumulating one serialized sleep per queued message.
PAINT_DELAY_SECONDS = 0.18

# Settlement normally arrives one task (~0-4 ms) after begin; the deadline
# only backstops teardown races the navigation/close/detach triggers missed.
SETTLE_DEADLINE_SECONDS = 0.5

# A browser confirmation this long after the click's begin is no longer
# attributable: too much can have happened in between.
CONFIRM_WINDOW_SECONDS = 3.0


def sanitize_url(raw) -> str:
    """Strip query and fragment; outcome evidence keeps origin + path only."""
    text = str(raw or "")
    parts = urlsplit(text)
    if parts.query or parts.fragment:
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return text


def merged_click(begin: dict) -> dict:
    """Start the persisted click from its begin message.

    Every begin field is carried over — ``checked_before`` and ``url_before``
    stay flat because demonstrate_codegen and capture_events read the click's
    compatibility surface at the top level; only the new evidence nests.
    """
    event = {key: value for key, value in begin.items() if key != "type"}
    event["type"] = "click"
    raw_id = begin.get("action_id")
    frame_id = begin.get("frame_id")
    if raw_id:
        event["action_id"] = (f"{frame_id}:{raw_id}" if frame_id
                              else str(raw_id))
    return event


def apply_settled(event: dict, patch: dict) -> None:
    """Fold the settled patch into the click, replacing any earlier verdict."""
    settlement = {"status": "settled"}
    for field in ("default_prevented", "url_after", "target_connected",
                  "focus_after"):
        if field in patch:
            settlement[field] = patch[field]
    event["settlement"] = settlement
    # Flat, exactly where the set_checked codegen path reads it today.
    if isinstance(patch.get("checked_after"), bool):
        event["checked_after"] = patch["checked_after"]


def finalize_unavailable(event: dict, reason: str) -> None:
    """Missing settlement is not a failed click; record why it is missing."""
    if "settlement" not in event:
        event["settlement"] = {"status": "unavailable", "reason": reason}


def attach_outcome(event: dict, evidence: dict) -> None:
    event.setdefault("outcomes", []).append(dict(evidence))


def attach_multi_click(first: dict, second: dict) -> None:
    """Mark the second click of a browser-confirmed dblclick pair.

    Phase-1 observability: the evidence names the pair, nothing is
    consolidated, and both clicks persist as independent actions.
    """
    second["multi_click_evidence"] = {
        "marker": "dblclick",
        "first_action_id": first.get("action_id"),
    }


def _activation_source(begin: dict) -> str:
    """Derive click provenance from the event's own fields.

    Chromium's click is a PointerEvent: pointer_type is the device for a
    pointer gesture and empty for keyboard/assistive activation, while
    is_trusted separates script dispatch. No gesture correlation needed.
    """
    if "is_trusted" not in begin:
        return "unknown"
    if not begin["is_trusted"]:
        return "programmatic"
    return "pointer" if begin.get("pointer_type") else "non_pointer"


def merge_recorded_clicks(payloads: list[dict]) -> list[dict]:
    """Reassemble a raw recorder stream into persisted-style actions.

    Recorder-level tests observe binding payloads without a host; this gives
    them the merged view the host would persist, in authored order.
    """
    out: list[dict] = []
    by_id: dict[str, dict] = {}
    for payload in payloads:
        kind = payload.get("type")
        if kind == "click_begin":
            event = merged_click(payload)
            if payload.get("action_id"):
                by_id[payload["action_id"]] = event
            out.append(event)
        elif kind == "click_settled":
            event = by_id.get(payload.get("action_id"))
            if event is not None:
                apply_settled(event, payload)
        elif kind == "click_outcome":
            event = by_id.get(payload.get("action_id"))
            if event is not None and isinstance(payload.get("evidence"), dict):
                attach_outcome(event, payload["evidence"])
        elif kind == "click_multi":
            pair = [by_id.get(str(raw))
                    for raw in payload.get("action_ids") or []]
            if len(pair) == 2 and None not in pair:
                attach_multi_click(*pair)
        elif kind in INTERNAL_EVENT_TYPES:
            continue
        else:
            out.append(payload)
    return out


class _Entry:
    __slots__ = ("page", "frame", "event", "recording", "ready_at", "open",
                 "begin_at", "deadline", "navigated", "doc_token", "frame_id")

    def __init__(self, page, frame, event, recording, *, ready_at,
                 begin_at, frame_id, open=False, deadline=float("inf"),
                 doc_token=""):
        self.page = page
        self.frame = frame
        self.event = event
        self.recording = recording
        self.ready_at = ready_at
        self.open = open
        self.begin_at = begin_at
        self.deadline = deadline
        self.navigated = False
        self.doc_token = doc_token
        self.frame_id = frame_id


class TransactionLog:
    """One capture's click transactions and its ordered emission buffer."""

    def __init__(self, *, clock=time.monotonic,
                 paint_delay: float = PAINT_DELAY_SECONDS,
                 settle_deadline: float = SETTLE_DEADLINE_SECONDS,
                 confirm_window: float = CONFIRM_WINDOW_SECONDS) -> None:
        self._clock = clock
        self._paint_delay = paint_delay
        self._settle_deadline = settle_deadline
        self._confirm_window = confirm_window
        self._buffer: deque[_Entry] = deque()
        # Kept past emission so late patches and confirmations can still
        # reach the released (still mutable) event dictionaries.
        self._clicks: dict[tuple[str, str], _Entry] = {}
        self._last_click_by_frame: dict[str, _Entry] = {}
        self._last_entry_by_frame: dict[str, _Entry] = {}
        self._last_click_by_page: dict = {}
        self._counters: Counter[str] = Counter()

    # -- recorder messages -------------------------------------------------

    def add(self, page, frame, payload: dict, *, recording: bool = False) -> None:
        kind = payload.get("type")
        if kind == "click_settled":
            self._settle(payload)
            return
        if kind == "click_outcome":
            self._outcome(payload)
            return
        if kind == "click_multi":
            self._multi(payload)
            return
        if kind == "document_ready":
            self._document_changed(payload)
            return
        now = self._clock()
        frame_id = str(payload.get("frame_id") or "")
        if kind == "click_begin" and payload.get("action_id"):
            self._counters[f"clicks_{_activation_source(payload)}"] += 1
            raw_id = str(payload["action_id"])
            entry = _Entry(page, frame, merged_click(payload), recording,
                           ready_at=now + self._paint_delay, begin_at=now,
                           frame_id=frame_id, open=True,
                           deadline=now + self._settle_deadline,
                           doc_token=raw_id.split(":", 1)[0])
            self._clicks[(frame_id, raw_id)] = entry
            self._last_click_by_frame[frame_id] = entry
            self._last_click_by_page[page] = entry
        else:
            event = merged_click(payload) if kind == "click_begin" else payload
            entry = _Entry(page, frame, event, recording,
                           ready_at=now + self._paint_delay, begin_at=now,
                           frame_id=frame_id)
        self._buffer.append(entry)
        self._last_entry_by_frame[frame_id] = entry

    def _settle(self, payload: dict) -> None:
        entry = self._transaction(payload)
        if entry is None:
            return
        # A patch that lost the deadline race still improves the evidence:
        # the emitted dict is mutable until capture finalization.
        apply_settled(entry.event, payload)
        entry.open = False

    def _outcome(self, payload: dict) -> None:
        entry = self._transaction(payload)
        if entry is not None and isinstance(payload.get("evidence"), dict):
            attach_outcome(entry.event, payload["evidence"])

    def _multi(self, payload: dict) -> None:
        """Pair a trusted dblclick marker with both of its transactions.

        Emission timing does not matter — the first click is typically
        released before the marker arrives, and its dict stays mutable. A
        marker that cannot name both transactions in its own frame (the
        document died between click and dblclick) is dropped: the clicks
        then persist separately, which is the conservative outcome.
        """
        self._counters["multi_click_markers"] += 1
        frame_id = str(payload.get("frame_id") or "")
        pair = [self._clicks.get((frame_id, str(raw)))
                for raw in payload.get("action_ids") or []]
        if len(pair) != 2 or None in pair:
            self._counters["multi_click_markers_dropped"] += 1
            return
        attach_multi_click(pair[0].event, pair[1].event)
        self._counters["multi_click_evidence_attached"] += 1

    def diagnostics(self) -> dict[str, int]:
        """Phase-1 observability counters for the capture bundle."""
        return dict(self._counters)

    def _transaction(self, payload: dict) -> _Entry | None:
        key = (str(payload.get("frame_id") or ""),
               str(payload.get("action_id") or ""))
        return self._clicks.get(key)

    def _document_changed(self, payload: dict) -> None:
        """A new document appeared in the frame: its predecessor is gone.

        The recorder announces every document it is installed into. An open
        transaction from a different document token can never settle — its
        settle timer died with the old document — so it is finalized with the
        honest reason instead of waiting out the deadline.
        """
        frame_id = str(payload.get("frame_id") or "")
        token = str(payload.get("doc_token") or "")
        for entry in self._clicks.values():
            if entry.open and entry.frame_id == frame_id and entry.doc_token != token:
                finalize_unavailable(entry.event, "document_navigated")
                entry.open = False

    # -- browser lifecycle confirmations ------------------------------------

    def frame_navigated(self, page, frame_id: str, url) -> None:
        """Attach a committed navigation to the click that plausibly did it.

        Attribution is deliberately narrow: the open transaction on that
        frame, or the frame's most recent click when it is also the frame's
        most recent authored action and began inside the confirmation window.
        A navigation following a later press or fill is never pinned to an
        old click — proximity is not causality.
        """
        del page  # navigation evidence is frame-scoped
        now = self._clock()
        target = None
        for entry in self._clicks.values():
            if entry.open and entry.frame_id == frame_id:
                if target is None or entry.begin_at > target.begin_at:
                    target = entry
        if target is None:
            last = self._last_click_by_frame.get(frame_id)
            if (last is not None
                    and now - last.begin_at <= self._confirm_window
                    and self._last_entry_by_frame.get(frame_id) is last):
                target = last
        if target is None:
            return
        attach_outcome(target.event, {
            "class": "browser_confirmed", "kind": "frame_navigation",
            "destination": sanitize_url(url),
        })
        if target.open:
            target.navigated = True

    def frame_detached(self, frame_id: str) -> None:
        self._finalize_open("frame_detached",
                            lambda entry: entry.frame_id == frame_id)

    def page_closed(self, page) -> None:
        self._finalize_open("page_closed", lambda entry: entry.page is page)

    def popup_opened(self, opener_page, url) -> None:
        self._confirm_on_page(opener_page, "popup", url)

    def download_started(self, page, url) -> None:
        self._confirm_on_page(page, "download", url)

    def _confirm_on_page(self, page, kind: str, url) -> None:
        # Popups and downloads land on the opener/owning PAGE (Playwright's
        # strong relationship); the recent-click window bounds attribution.
        entry = self._last_click_by_page.get(page)
        if entry is None or self._clock() - entry.begin_at > self._confirm_window:
            return
        attach_outcome(entry.event, {
            "class": "browser_confirmed", "kind": kind,
            "destination": sanitize_url(url),
        })

    def _finalize_open(self, reason: str, matches) -> None:
        for entry in self._clicks.values():
            if entry.open and matches(entry):
                finalize_unavailable(entry.event, reason)
                entry.open = False

    # -- ordered emission ----------------------------------------------------

    def pump(self) -> list[tuple]:
        """Release every finalized event whose paint delay has passed.

        Ordered emission: an open transaction blocks everything behind it, so
        a deferred settlement can never reorder later actions.
        """
        now = self._clock()
        for entry in self._buffer:
            if entry.open and now >= entry.deadline:
                finalize_unavailable(
                    entry.event,
                    "document_navigated" if entry.navigated else "settle_deadline")
                entry.open = False
        out = []
        while self._buffer:
            front = self._buffer[0]
            if front.open or now < front.ready_at:
                break
            out.append(self._buffer.popleft())
        return [(e.page, e.frame, e.event, e.recording) for e in out]

    def flush(self) -> list[tuple]:
        """Recording stopped: finalize and release everything immediately."""
        out = []
        while self._buffer:
            entry = self._buffer.popleft()
            if entry.open:
                finalize_unavailable(entry.event, "recording_stopped")
                entry.open = False
            out.append(entry)
        return [(e.page, e.frame, e.event, e.recording) for e in out]
