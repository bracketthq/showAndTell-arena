"""Normalize raw capture events into portable demonstration events.

Capture observers report raw, noisy events: duplicated cross-frame echoes,
debounced input artifacts, tab restores that are not switches.  The helpers
here collapse that stream into the authored actions a draft stores and a
replay performs, and align spoken narration onto those actions.
"""
from __future__ import annotations

import re


# Event types a host/iframe observer pair can both report for one physical
# gesture, mapped to the field an echo must agree on (None: no payload — a
# click's two copies carry different coordinate spaces by definition).
_ECHO_PAYLOAD_FIELDS = {"click": None, "press": "key", "type": "text",
                        "fill": "value", "select": "value"}
_CLICK_ECHO_WINDOW_MS = 2
# Browser delivery measurements across the recorded human tasks show exact
# payload echoes arriving up to 4 ms apart.  Payload equality is strong enough
# identity evidence to tolerate twice that observed jitter.  Clicks have no
# comparable payload (their coordinate spaces legitimately differ), so they
# retain the tighter window above.
_PAYLOAD_ECHO_WINDOW_MS = 8
ACTION_TYPES = {"click", "drag", "fill", "select", "press", "type",
                "tab_switch"}


def is_action_event(event: dict) -> bool:
    """Whether an event represents one authored user action."""
    return event.get("type") in ACTION_TYPES and not event.get("initial")


_ONLYOFFICE_CELL_NAME = re.compile(r"^Cell ([A-Z]{1,3}[1-9][0-9]*)$")


def _onlyoffice_cell_ref(event: dict) -> str | None:
    """Return the stable cell identity carried by the host-page echo."""
    target = event.get("target") or {}
    match = _ONLYOFFICE_CELL_NAME.fullmatch(str(target.get("name") or "").strip())
    return match.group(1) if match else None


def _frame_identity(event: dict) -> str:
    """Return a stable document identity, including for legacy recordings."""
    if event.get("frame_id"):
        return str(event["frame_id"])
    if event.get("frame_url"):
        return f"legacy-frame:{event['frame_url']}"
    return "legacy-main"


def _is_main_document(event: dict) -> bool:
    frame_id = event.get("frame_id")
    if frame_id:
        return str(frame_id).endswith(":main")
    return not bool(event.get("frame_url"))


def _echo_payload(payload: str | None, prior: dict,
                  event: dict) -> tuple[bool, object | None]:
    """Return whether two cross-frame reports are one semantic action.

    Exact payloads are ordinary echoes.  Text proxies can additionally report
    a leading/trailing fragment while their host accessibility mirror reports
    the committed text.  That relation is accepted only between a main frame
    and child frame; unrelated sibling-frame text remains independent.
    """
    if payload is None:
        return True, None
    first, second = prior.get(payload), event.get(payload)
    if (payload == "key" and first == second
            and list(prior.get("modifiers") or [])
            != list(event.get("modifiers") or [])):
        return False, None
    if first == second:
        return True, first
    if payload != "text" or not isinstance(first, str) or not isinstance(second, str):
        return False, None
    if _is_main_document(prior) == _is_main_document(event):
        return False, None
    shorter, longer = sorted((first, second), key=len)
    if not shorter or not (longer.startswith(shorter) or longer.endswith(shorter)):
        return False, None
    return True, longer


def _dedupe(events: list[dict]) -> list[dict]:
    """Collapse noisy input/change duplicates without losing user intent."""
    out: list[dict] = []
    active_page: str | None = None
    for event in events:
        kind = event.get("type")
        page = event.get("page", "page")
        if kind == "tab_switch":
            # Restoring a minimized browser can report the already-visible tab
            # as visible again. It is not another switch. Capture normally
            # removes this before narration alignment; the key merge also makes
            # repeated normalization of stored drafts lossless.
            if page == active_page:
                keys = list(event.get("narration_keys") or [])
                if keys and out:
                    prior = dict(out[-1])
                    prior["narration_keys"] = list(dict.fromkeys(
                        list(prior.get("narration_keys") or []) + keys))
                    out[-1] = prior
                continue
            active_page = page
        elif kind in ACTION_TYPES:
            # A real gesture proves which tab is active even if a browser or
            # platform failed to deliver the preceding visibility transition.
            active_page = page
        # Some SPA search dialogs clear their input as the selected result
        # navigates. The debounced input observer can report that synthetic
        # empty value just before the click observer, after asynchronous
        # evidence collection has already attached the click's destination URL
        # to both events. Replaying the clear is not a user gesture: it leaves
        # the live page behind while the recorded screenshot is already at the
        # destination. The complete signature is intentionally narrow so a
        # real clear-and-click sequence is preserved.
        if event.get("type") == "click" and len(out) >= 2:
            cleared, before = out[-1], out[-2]
            delay = event.get("at_ms", 0) - cleared.get("at_ms", 0)
            if (cleared.get("type") == "fill"
                    and cleared.get("value") == ""
                    and cleared.get("page") == event.get("page")
                    and 0 <= delay <= 150
                    and cleared.get("url")
                    and cleared.get("url") == event.get("url")
                    and before.get("url") != cleared.get("url")):
                out.pop()
                # Stored drafts are re-deduped at render after narration was
                # aligned, so the dropped clear may carry spoken segments; the
                # click inherits them. Copied because the caller's event dicts
                # must not change behind its back.
                keys = list(dict.fromkeys(
                    list(cleared.get("narration_keys") or [])
                    + list(event.get("narration_keys") or [])))
                if keys:
                    event = dict(event)
                    event["narration_keys"] = keys
        # Embedded editors can expose one physical gesture through both the
        # real editor document and a host-page accessibility mirror. Search the
        # whole tiny burst, not only the preceding event: a commit key can sit
        # between the child-frame edit and its delayed host echo. ONLYOFFICE's
        # canvas is one instance: its child copy owns replayable coordinates,
        # while the host copy can carry a stable cell identity.
        # An echo reports the SAME gesture, so its payload must match. Text is
        # the one exception: an editor proxy may expose only a boundary fragment
        # while its main-frame accessibility mirror exposes the committed text.
        # Click copies legitimately disagree on coordinates.
        # Timing alone is not identity: a debounced fill can flush beside
        # another document's unrelated action. Payload-bearing echoes permit a
        # slightly wider delivery-jitter window because their values must also
        # match; payload-less clicks retain the tight coordinate-only window.
        echo_index = None
        canonical_payload = None
        payload = _ECHO_PAYLOAD_FIELDS.get(event.get("type"), False)
        if payload is not False and not event.get("echo_collapsed"):
            echo_window_ms = (_CLICK_ECHO_WINDOW_MS if payload is None
                              else _PAYLOAD_ECHO_WINDOW_MS)
            for index in range(len(out) - 1, -1, -1):
                prior = out[index]
                if (abs(event.get("at_ms", 0) - prior.get("at_ms", 0))
                        > echo_window_ms):
                    break
                if (prior.get("type") == event.get("type")
                        and prior.get("page") == event.get("page")
                        and _frame_identity(prior) != _frame_identity(event)
                        and not prior.get("echo_collapsed")):
                    payload_matches, merged_payload = _echo_payload(
                        payload, prior, event)
                    if not payload_matches:
                        continue
                    echo_index = index
                    canonical_payload = merged_payload
                    break
        if echo_index is not None:
            prior = out[echo_index]
            if _is_main_document(prior) != _is_main_document(event):
                keep, echo = ((event, prior) if _is_main_document(prior)
                              else (prior, event))
            else:
                keep, echo = prior, event
            # Copied because stored drafts are re-deduped at render time and
            # the caller's event dicts must not change behind its back.
            keep = dict(keep)
            if payload is not None and canonical_payload is not None:
                keep[payload] = canonical_payload
            # The real iframe copy owns the actionable editor frame, but a
            # canvas coordinate is only meaningful at the captured worksheet
            # zoom and scroll. The host accessibility mirror owns the stable
            # cell reference. Preserve that semantic identity on the iframe
            # action so replay can navigate with ONLYOFFICE's Name Box instead
            # of clicking a stale pixel. This also upgrades stored drafts when
            # they are deduped again during render.
            cell_ref = (_onlyoffice_cell_ref(echo)
                        or _onlyoffice_cell_ref(keep))
            if event.get("type") == "click" and cell_ref:
                keep["cell_ref"] = cell_ref
                keep["description"] = (echo.get("description")
                                       or f"click Cell {cell_ref}")
            keys = list(dict.fromkeys(
                list(keep.get("narration_keys") or [])
                + list(echo.get("narration_keys") or [])))
            if keys:
                keep["narration_keys"] = keys
            # This pass runs at capture finalization, draft authoring, AND
            # render, over the same persisted stream. Mark the merged event so
            # a later pass cannot absorb a neighbouring real action into it.
            keep["echo_collapsed"] = True
            out[echo_index] = keep
            continue
        if event["type"] in {"fill", "select"} and out:
            prior = out[-1]
            # Selector-less fills are NOT the same target: None == None would
            # collapse consecutive fills on different anonymous dialog fields.
            if (prior["type"] == event["type"] and prior.get("page") == event.get("page")
                    and prior.get("selector") is not None
                    and prior.get("selector") == event.get("selector")):
                out[-1] = event
                continue
        # Pressing Enter commonly emits a synthetic click on the same target;
        # preserve both because either may be the actual navigation trigger.
        out.append(event)
    # Echo collapse first, so a double-click's host-mirror copies are already
    # folded into their iframe clicks before the pair itself is merged.
    return _consolidate_multi_clicks(out)


def _consolidate_multi_clicks(events: list[dict]) -> list[dict]:
    """Merge browser-confirmed double-clicks into one semantic action.

    The recorder pairs a trusted ``dblclick`` with its two click transactions
    by element identity; capture attaches ``multi_click_evidence`` to the
    second click, naming the first by its globally unique ``action_id``. Only
    that proof merges — timing, targets, and screenshots never do, and
    evidence naming a click that is not in the stream leaves both actions.

    The merged action keeps the first click's identity (position in the
    stream, target, selectors, screenshot, ``*_before`` state) and the second
    click's post-gesture state (``detail``, settlement, ``checked_after``,
    post-action ``url``); outcomes and narration keys concatenate in order.
    Consuming the evidence into ``click_count`` makes the pass idempotent.
    """
    out: list[dict] = []
    index_by_action: dict[tuple, int] = {}
    for event in events:
        evidence = (event.get("multi_click_evidence")
                    if event.get("type") == "click" else None)
        first_index = index_by_action.get(
            (event.get("page"), (evidence or {}).get("first_action_id")))
        if not isinstance(evidence, dict) or first_index is None:
            if event.get("type") == "click" and event.get("action_id"):
                index_by_action[(event.get("page"), event["action_id"])] = len(out)
            out.append(event)
            continue
        # Copied because stored drafts are re-deduped at render time and the
        # caller's event dicts must not change behind its back.
        merged = dict(out[first_index])
        merged["click_count"] = 2
        merged["constituent_action_ids"] = [merged.get("action_id"),
                                            event.get("action_id")]
        for field in ("detail", "settlement", "url"):
            if event.get(field) is not None:
                merged[field] = event[field]
        merged.pop("checked_after", None)
        if isinstance(event.get("checked_after"), bool):
            merged["checked_after"] = event["checked_after"]
        outcomes = (list(merged.get("outcomes") or [])
                    + list(event.get("outcomes") or []))
        if outcomes:
            merged["outcomes"] = outcomes
        keys = list(dict.fromkeys(list(merged.get("narration_keys") or [])
                                  + list(event.get("narration_keys") or [])))
        if keys:
            merged["narration_keys"] = keys
        out[first_index] = merged
    return out


def align_narration(events: list[dict], narration: list[dict]) -> list[dict]:
    """Assign each spoken segment to its nearest recorded action."""
    events = [dict(event) for event in events]
    actions = [event for event in events if is_action_event(event)]
    if not actions:
        return events
    for index, spoken in enumerate(narration, 1):
        at_ms = spoken.get("at_ms", 0)
        target = min(actions, key=lambda event: abs(event.get("at_ms", 0) - at_ms))
        key = target.setdefault("narration_keys", [])
        key.append(f"voice-{index:03d}")
    return events
