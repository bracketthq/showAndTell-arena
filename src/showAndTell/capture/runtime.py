"""Managed-Chrome recorder for authoring replayable demonstrations.

The recorder observes real user gestures inside every captured page, derives a
ranked selector set for the acted element, and snapshots Chromium's full
accessibility tree plus a screenshot after each action.  It deliberately uses
CDP/Playwright rather than attempting to inspect cross-origin tabs from the
viewer page.

This module owns only the capture runtime: the ManagedCapture thread, browser
observation, and draft authoring. Portable event normalization and compilation
live in :mod:`showAndTell.demonstration`; application login and URL relocation
live in :mod:`showAndTell.applications.browser.runtime`.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from showAndTell.bundles.parse import format_metadata
from showAndTell.bundles.snapshot import _serialize_axtree
from showAndTell.applications.browser import runtime as browser_runtime
from showAndTell.core import chrome
from showAndTell.core.chrome import kill_managed_chrome, launch_managed_chrome
from showAndTell.demonstration.compiler import _js, _press_shortcut, compile_demonstration
from showAndTell.demonstration.events import (
    ACTION_TYPES,
    _dedupe,
    align_narration,
    is_action_event,
)
from showAndTell.demonstration.model import Demonstration

from . import screenrec
from .transactions import INTERNAL_EVENT_TYPES, TransactionLog


CAPTURE_CDP_PORT = 9444


def _available_cdp_port(preferred: int = CAPTURE_CDP_PORT) -> int:
    """Use the traditional capture port when free, else an ephemeral port.

    Chrome can bind only IPv6 when another process already owns the preferred
    IPv4 loopback port.  ``wait_for_cdp`` and Playwright intentionally connect
    to 127.0.0.1, so that split binding would attach the recorder to the other
    process instead of managed Chrome.  Resolve the collision before launch.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", preferred))
        except OSError:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])
    return preferred

# Settlement patches and document announcements are transport between the
# recorder and the transaction log, never authored actions: they consume no
# sequence and must never be persisted. click_begin is the click's authored
# moment, so it keeps normal sequence assignment.
_PATCH_EVENT_TYPES = INTERNAL_EVENT_TYPES - {"click_begin"}

_START_HINTS = {
    "erpnext": (
        "Start the unified ERPNext + Frappe HR stack from the repository root with: "
        "docker compose -p showandtell-erpnext "
        "-f src/showAndTell/applications/erpnext/compose.yaml up -d"
    ),
}


# A sibling .js file rather than an inline string so JavaScript tooling can
# highlight, lint, and test the recorder script. Read at import so a missing
# or mispackaged file fails fast, before a capture has signed into
# applications and launched managed Chrome.
_INJECT_SOURCE = Path(__file__).with_name("recorder.js").read_text(encoding="utf-8")


def _front_chrome_tab() -> tuple[str, str] | None:
    """Return the front Chrome window's active tab on macOS.

    Chromium keeps documents in separate top-level windows `visible` and
    `focused` from page JavaScript's perspective, so DOM lifecycle signals
    cannot observe a window-to-window switch. Chrome's scriptable front window
    supplies the missing browser-chrome state without Accessibility permission.
    Other platforms retain the injected visibility/focus observers.
    """
    if sys.platform != "darwin":
        return None
    script = (
        'tell application "Google Chrome"\n'
        'if (count of windows) is 0 then return ""\n'
        'set t to active tab of front window\n'
        'return (URL of t) & linefeed & (title of t)\n'
        'end tell'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True,
            timeout=1, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    url, _, title = result.stdout.rstrip("\n").partition("\n")
    return (url, title) if url else None


def _clock_start(recorder) -> int:
    """Epoch ms of the recording's own time zero.

    The recorder has been capturing since it was spawned — stamping "now"
    after screenrec.start() returns (which includes the startup grace sleep)
    shifts every at_ms behind the audio and replays the voice seconds after
    the gestures it describes.
    """
    return int(getattr(recorder, "start_epoch_ms", None) or time.time() * 1000)


def author_draft(directory: Path, metadata: dict, result: dict,
                 narration: list[dict], *, seed: dict | None = None,
                 replay_setup: bool = False) -> list[dict]:
    """Write the generated task scaffold and complete bundle index files."""
    directory = Path(directory)
    events = align_narration(_dedupe(result.get("events", [])), narration)
    setup_events = _dedupe(result.get("setup_events", []))
    surfaces = metadata["surfaces"]
    demonstration = Demonstration.from_values(
        events=events,
        surfaces=surfaces,
        setup_events=setup_events if replay_setup else (),
    )
    (directory / "demonstrate.py").write_text(
        compile_demonstration(demonstration), encoding="utf-8")
    (directory / "task_logic.py").write_text(
        '"""Decision logic must be extracted and reviewed from the captured workflow."""\n',
        encoding="utf-8")
    (directory / "demo").mkdir(exist_ok=True)
    (directory / "quiz").mkdir(exist_ok=True)
    seed_data = seed if seed is not None else {
        "_capture_note": "Populate deterministic application state before promotion."
    }
    (directory / "demo" / "seed.json").write_text(
        json.dumps(seed_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    (directory / "demo" / "seed_events.jsonl").write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n"
                for event in setup_events),
        encoding="utf-8")
    spoken = {
        f"voice-{index:03d}": row
        for index, row in enumerate(narration, 1)
    }
    narration_rows = []
    action_index = 0
    for event in events:
        if not is_action_event(event):
            continue
        action_index += 1
        keys = event.get("narration_keys") or [f"action-{action_index:03d}"]
        for key in keys:
            row = spoken.get(key, {})
            narration_rows.append({
                "key": key,
                "text": row.get("text") or event.get("description")
                or f"{event['type']} the selected control",
                "at_ms": row.get("at_ms", event.get("at_ms", 0)),
            })
    (directory / "demo" / "narration_script.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in narration_rows),
        encoding="utf-8")
    (directory / "quiz" / "questions.json").write_text(
        json.dumps({"questions": []}, indent=2) + "\n", encoding="utf-8")
    systems = max(1, min(3, len(metadata["applications"])))
    summary = metadata.get("summary") or f"Captured workflow for {metadata['title']}"
    toml = (
        "[task]\n"
        f"name = {_js(metadata['slug'])}\n"
        # No fixture family: applications is the whole declaration, and the
        # runtime composes a session from it.
        f"applications = {_js(metadata['applications'])}\n"
        f"primary_application = {_js(metadata['primary_application'])}\n"
        f"summary = {_js(summary)}\n\n"
        "[status]\nstate = \"draft\"\n"
        "reason = \"Generated from managed Chrome; review seed, logic, and quiz before promotion.\"\n\n"
        "[complexity]\n"
        f"hops = 1\nsystems = {systems}\nplan = 1\nchained = false\nstate = false\n"
        "precedence = 0\noptimize = false\nnever_rules = false\nbinding = 1\n"
    )
    (directory / "task.toml").write_text(toml, encoding="utf-8")

    bundle = directory / "capture_bundle"
    trace_lines = ["// Generated managed-Chrome action trace."]
    action_count = sum(is_action_event(event) for event in events)
    observations = [f"# {metadata['title']}", "", f"**Actions:** {action_count}", "", "## Steps", ""]
    page_vars = {event.get("page", "page") for event in events}
    for name in sorted(page_vars):
        if name != "page":
            trace_lines.append(f"const {name} = await context.newPage();")
    for index, event in enumerate(events, 1):
        kind, page = event["type"], event.get("page", "page")
        screenshot = event.get("screenshot")
        comment = f"  // Screenshot: {Path(screenshot).name}" if screenshot else ""
        if kind == "goto":
            code = f"await {page}.goto({_js(event.get('url', ''))});"
        elif kind == "tab_switch":
            code = f"await {page}.bringToFront();"
        elif kind == "click":
            code = f"await {page}.click({_js(event.get('selector', ''))});"
        elif kind == "drag":
            destination = event.get("destination") or {}
            destination_selectors = destination.get("selectors") or []
            destination_selector = destination_selectors[0] if destination_selectors else ""
            code = (
                f"await {page}.locator({_js(event.get('selector', ''))}).dragTo("
                f"{page}.locator({_js(destination_selector)}));"
            )
        elif kind == "fill":
            code = f"await {page}.fill({_js(event.get('selector', ''))}, {_js(event.get('value', ''))});"
        elif kind == "select":
            code = f"await {page}.selectOption({_js(event.get('selector', ''))}, {_js(event.get('value', ''))});"
        elif kind == "press":
            code = (
                f"await {page}.press({_js(event.get('selector', ''))}, "
                f"{_js(_press_shortcut(event))});"
            )
        elif kind == "type":
            code = f"await {page}.keyboard.type({_js(event.get('text', ''))});"
        else:
            continue
        trace_lines.append(code + comment)
        observations.append(f"{index}. {event.get('description', kind)}")
    bundle.mkdir(exist_ok=True)
    (bundle / "traces.js").write_text("\n".join(trace_lines) + "\n", encoding="utf-8")
    (bundle / "observation.md").write_text("\n".join(observations) + "\n", encoding="utf-8")
    return events


class ManagedCapture:
    """One background Playwright/CDP capture owned by the viewer server."""

    def __init__(self, directory: Path, surfaces: list[dict], out=print,
                 tools: list[dict] | None = None) -> None:
        self.directory = Path(directory)
        self.surfaces = surfaces
        # Author-facing pages opened beside the applications -- the seed
        # editor, today. They are never signed into, never written to the
        # draft, and never recorded. Filtering by origin rather than by page
        # object also covers a tool page the author navigates or reopens.
        self.tools = list(tools or [])
        self.tool_origins = {browser_runtime.origin(tool["url"]) for tool in self.tools}
        self.out = out
        self.ready = threading.Event()
        # ``prepare_freeze`` is a setup-phase barrier.  The viewer asks for it
        # before exporting application state so the capture thread can drain
        # the last browser events and report the pages the operator is actually
        # looking at.  Without this handshake a replica redirect can leave the
        # browser on one ONLYOFFICE instance while the data plane exports
        # another.
        self.freeze_requested = threading.Event()
        self.freeze_ready = threading.Event()
        self.live_surfaces: list[dict] = []
        self.recording_requested = threading.Event()
        self.recording_ready = threading.Event()
        self.stop_requested = threading.Event()
        self.finished = threading.Event()
        self.error: Exception | None = None
        self.login_results: list[dict] = []
        self.setup_events: list[dict] = []
        self.events: list[dict] = []
        self.recording: Path | None = None
        self.voice_recorded = False
        self.narration_lead_ms = 0
        self.setup_started_ms = int(time.time() * 1000)
        self.started_ms: int | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self, timeout: float = 45) -> None:
        self.thread.start()
        if not self.ready.wait(timeout):
            self.stop_requested.set()
            raise RuntimeError("managed Chrome capture did not become ready")
        if self.error:
            raise RuntimeError(f"managed Chrome capture failed: {self.error}")

    def _authenticate(self, page, surface: dict, page_name: str) -> dict:
        """Sign a surface in, and record that a replay must do the same.

        The step is recorded because the surface REQUIRES authentication, not
        because this capture's automatic attempt happened to succeed. Tying
        the two together meant a surface whose adapter failed here -- or that
        the operator then signed into by hand -- produced a draft with no
        login step at all, and every replay of it ran signed out.
        """
        credentials = surface.get("credentials") or {}
        needs_login = bool(credentials.get("email") or credentials.get("password"))
        result = {"id": surface.get("id"), "required": needs_login,
                  "status": "not-required" if not needs_login else "manual"}
        if not needs_login:
            return result
        login_event = {
            "type": "login", "page": page_name,
            "surface_id": surface.get("id"),
            "at_ms": int(time.time() * 1000) - self.setup_started_ms,
            "description": f"sign in to {surface.get('label', page.url)}",
        }
        if surface.get("login_replay") == "demonstrated":
            # Leave the login form visible for the recorded stage.  The
            # generated seed still uses this marker to authenticate while
            # rebuilding fixture state, but demonstrate() replays the human's
            # captured credential entry and submit instead.
            login_event["replay_mode"] = "demonstrated"
        self.setup_events.append(login_event)
        if login_event.get("replay_mode") == "demonstrated":
            return result
        try:
            if browser_runtime.auto_login(page, surface):
                result["status"] = "signed-in"
        except Exception as exc:                              # noqa: BLE001
            # A failed adapter is reported, not raised: the operator can still
            # sign in by hand, and the replay step is already recorded.
            result["status"] = "failed"
            result["error"] = str(exc)
        return result

    @staticmethod
    def _close_tool_pages(tool_pages, focus) -> None:
        """Shut the authoring tabs the moment setup is frozen.

        They belong to setup, so they should not sit open through the
        demonstration -- and closing them here also keeps their URLs out of
        the replay driver, which records every page still open at this
        instant as one to reopen.
        """
        closed = False
        for page in tool_pages:
            with contextlib.suppress(Exception):
                if not page.is_closed():
                    page.close()
                    closed = True
        if closed:
            # Closing the focused tab leaves the browser to pick a successor;
            # the demonstration should open on the application, not on
            # whatever it chose.
            with contextlib.suppress(Exception):
                focus.bring_to_front()

    def begin_recording(self, timeout: float = 30) -> dict:
        if self.finished.is_set():
            raise RuntimeError("managed Chrome capture has already ended")
        if self.recording_ready.is_set():
            raise RuntimeError("managed Chrome recording has already started")
        self.recording_requested.set()
        if not self.recording_ready.wait(timeout):
            raise RuntimeError("managed Chrome recording did not start")
        if self.error:
            raise RuntimeError(f"managed Chrome capture failed: {self.error}")
        return {"voice_recorded": self.voice_recorded}

    def prepare_freeze(self, timeout: float = 30) -> list[dict]:
        """Drain setup gestures and describe the live application pages.

        This deliberately does not start the recording.  The caller first
        validates these URLs against its leased application instances and
        flushes their state; only then does ``begin_recording`` advance the
        browser into the demonstration phase.
        """
        if self.finished.is_set():
            raise RuntimeError("managed Chrome capture has already ended")
        self.freeze_requested.set()
        if not self.freeze_ready.wait(timeout):
            raise RuntimeError("managed Chrome setup did not become ready to freeze")
        if self.error:
            raise RuntimeError(f"managed Chrome capture failed: {self.error}")
        return [dict(row) for row in self.live_surfaces]

    def stop(self, timeout: float = 30) -> dict:
        self.stop_requested.set()
        if not self.finished.wait(timeout):
            raise RuntimeError("managed Chrome capture did not stop cleanly")
        if self.error:
            raise RuntimeError(f"managed Chrome capture failed: {self.error}")
        return {
            "events": _dedupe(self.events),
            "setup_events": _dedupe(self.setup_events),
            "recording": self.recording.name if self.recording else None,
            "voice_recorded": self.voice_recorded,
            "started_ms": self.started_ms,
            # How far the recording clock precedes the moment begin_recording
            # returned — the browser's narration timestamps start there and
            # must be shifted onto the recording clock before alignment.
            "narration_lead_ms": self.narration_lead_ms,
        }

    def _clock_base(self, recording: bool) -> int:
        """Milliseconds that count as t=0 for an emitted event: the recording
        start once recording has begun, else the setup start. This is the
        audio/action clock zero every `at_ms` in a capture is measured from."""
        if recording and self.started_ms is not None:
            return self.started_ms
        return self.setup_started_ms

    def _run(self) -> None:
        recorder = screenrec.NullRecorder()
        try:
            from playwright.sync_api import sync_playwright

            bundle = self.directory / "capture_bundle"
            (bundle / "metadata").mkdir(parents=True, exist_ok=True)
            kill_managed_chrome(chrome.MANAGED_ROOT)
            cdp_port = _available_cdp_port(CAPTURE_CDP_PORT)
            launch_managed_chrome(cdp_port, profile_root=chrome.MANAGED_ROOT)
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{cdp_port}")
                ctx = browser.contexts[0]
                for existing in list(ctx.pages):
                    existing.close()
                pending: list[tuple[object, object, dict]] = []
                page_names: dict[object, str] = {}
                sessions: dict[object, object] = {}
                frame_ids: dict[object, str] = {}
                frame_sequences: dict[str, int] = {}
                next_frame_number: dict[object, int] = {}
                arrival_sequence = 0

                def page_name(page) -> str:
                    if page not in page_names:
                        page_names[page] = "page" if not page_names else f"page{len(page_names) + 1}"
                    return page_names[page]

                def frame_identity(page, frame) -> str:
                    if frame == page.main_frame:
                        return f"{page_name(page)}:main"
                    if frame not in frame_ids:
                        number = next_frame_number.get(page, 0) + 1
                        next_frame_number[page] = number
                        frame_ids[frame] = f"{page_name(page)}:frame-{number}"
                    return frame_ids[frame]

                def binding(source, payload) -> None:
                    nonlocal arrival_sequence
                    if isinstance(payload, dict):
                        page, frame = source["page"], source["frame"]
                        frame_id = frame_identity(page, frame)
                        enriched = dict(payload)
                        enriched["frame_id"] = frame_id
                        if payload.get("type") not in _PATCH_EVENT_TYPES:
                            frame_sequence = frame_sequences.get(frame_id, 0) + 1
                            frame_sequences[frame_id] = frame_sequence
                            arrival_sequence += 1
                            enriched["frame_sequence"] = frame_sequence
                            enriched["arrival_sequence"] = arrival_sequence
                        if enriched.get("edit_sequence") is not None:
                            enriched["edit_transaction_id"] = (
                                f"{frame_id}:{enriched['edit_sequence']}")
                        pending.append((page, frame, enriched))

                def snapshot(page, frame, payload: dict, *, recording: bool) -> None:
                    n = len(self.events) + 1
                    target = payload.get("target") or {}
                    selectors = target.get("selectors") or []
                    try:
                        frame_url = frame.url
                    except Exception:
                        frame_url = page.url
                    if browser_runtime.origin(page.url) in self.tool_origins:
                        # The author filling in starting data is setup for the
                        # setup, and replaying it would seed twice.
                        return
                    is_main_frame = frame == page.main_frame
                    kind = payload.get("type")
                    description = payload.get("description")
                    if not description and kind == "tab_switch":
                        description = f"switch to {payload.get('title') or page.url}"
                    if not description:
                        description = (
                            f"{kind} {target.get('name') or target.get('role') or target.get('tag', '')}"
                            .strip()
                        )
                    # Enriched in place: the transaction log keeps a reference
                    # to this same dict, so a navigation that commits after
                    # emission can still attach browser_confirmed evidence to
                    # the persisted action (events.jsonl is written at stop).
                    event = payload
                    event.update({
                        "page": page_name(page),
                        "selector": selectors[0] if selectors else None,
                        "selectors": selectors,
                        "frame_url": None if is_main_frame else frame_url,
                        # Post-action page URL — for a navigating click this
                        # is the destination, which the synthetic search-clear
                        # dedupe in capture_events depends on.
                        "url": page.url,
                        "at_ms": max(0, payload.get("timestamp", int(time.time() * 1000))
                                     - self._clock_base(recording)),
                        "description": description,
                    })
                    event.setdefault("frame_id", frame_identity(page, frame))
                    if recording:
                        try:
                            page.screenshot(
                                path=str(bundle / f"screenshot_line{n}.png"),
                                full_page=True)
                            if page not in sessions:
                                sessions[page] = ctx.new_cdp_session(page)
                            session = sessions[page]
                            ax = session.send("Accessibility.getFullAXTree")
                            tree, count = _serialize_axtree(ax.get("nodes", []))
                            meta = {
                                "frameUrl": frame_url,
                                "selectors": selectors,
                                "targetElement": {
                                    "role": target.get("role", ""),
                                    "name": target.get("name", ""),
                                    "nodeId": None,
                                },
                                "trees": [{
                                    "frameId": "main", "nodeCount": count,
                                    "timestamp": int(time.time() * 1000),
                                    "tree": tree,
                                }],
                            }
                            (bundle / "metadata" / f"line{n}.md").write_text(
                                format_metadata(n, meta), encoding="utf-8")
                            event["screenshot"] = (
                                f"capture_bundle/screenshot_line{n}.png")
                        except Exception as exc:
                            event["snapshot_error"] = str(exc)
                    (self.events if recording else self.setup_events).append(event)

                pages = []
                for surface in self.surfaces:
                    page = ctx.new_page()
                    page_name(page)
                    try:
                        page.goto(surface["url"], wait_until="domcontentloaded")
                    except Exception as exc:
                        detail = str(exc)
                        if "ERR_CONNECTION_REFUSED" in detail:
                            hint = _START_HINTS.get(
                                surface.get("application"),
                                "Start the selected local application, then retry.",
                            )
                            raise RuntimeError(
                                f"{surface.get('label', 'Selected application')} is not "
                                f"running at {surface['url']}. {hint}"
                            ) from exc
                        raise
                    self.login_results.append(
                        self._authenticate(page, surface, page_name(page)))
                    pages.append(page)

                # Install the observer only after automatic authentication so
                # credentials never appear as user-authored setup gestures.
                ctx.expose_binding("__showAndTellRecord", binding)
                ctx.add_init_script(_INJECT_SOURCE)
                for page in pages:
                    # Frames that already exist never run the init script —
                    # page.evaluate covers only the main frame, and an
                    # embedded editor iframe (OnlyOffice) would otherwise
                    # record nothing at all.
                    for frame in page.frames:
                        try:
                            frame.evaluate(_INJECT_SOURCE)
                        except Exception:
                            pass
                for surface, page in zip(self.surfaces, pages):
                    self.setup_events.append({
                        "type": "goto", "page": page_name(page), "url": page.url,
                        "at_ms": int(time.time() * 1000) - self.setup_started_ms,
                        "description": f"open {surface.get('label', page.url)}",
                    })
                tool_pages = []
                for tool in self.tools:
                    try:
                        tool_page = ctx.new_page()
                        page_name(tool_page)
                        tool_page.goto(tool["url"], wait_until="domcontentloaded")
                        tool_pages.append(tool_page)
                    except Exception as exc:                  # noqa: BLE001
                        # An authoring convenience must never fail the capture.
                        self.out(f"could not open {tool.get('label', tool['url'])}: {exc}")

                discovered_pages: list[object] = []
                captured_new_pages: set[object] = set()
                transactions = TransactionLog()
                # Browser lifecycle notifications queued by event handlers.
                # Handlers only append; every Playwright call happens in the
                # capture loop, never inside an event callback.
                notes: list[tuple] = []

                def watch_page(page) -> None:
                    page.on("framenavigated", lambda frame, page=page:
                            notes.append(("frame_navigated", page, frame, frame.url)))
                    page.on("framedetached", lambda frame, page=page:
                            notes.append(("frame_detached", page, frame)))
                    page.on("close", lambda page=page:
                            notes.append(("page_closed", page)))
                    page.on("download", lambda download, page=page:
                            notes.append(("download", page, download.url)))

                for watched in pages + tool_pages:
                    watch_page(watched)

                def discovered(page) -> None:
                    page_name(page)
                    watch_page(page)
                    discovered_pages.append(page)
                    notes.append(("popup", page))

                def drain_notes() -> None:
                    requeued = []
                    while notes:
                        note = notes.pop(0)
                        kind = note[0]
                        if kind == "frame_navigated":
                            _, page, frame, url = note
                            transactions.frame_navigated(
                                page, frame_identity(page, frame), url)
                        elif kind == "frame_detached":
                            frame_id = frame_ids.get(note[2])
                            if frame_id:
                                transactions.frame_detached(frame_id)
                        elif kind == "page_closed":
                            transactions.page_closed(note[1])
                        elif kind == "download":
                            transactions.download_started(note[1], note[2])
                        elif kind == "popup":
                            page = note[1]
                            if not page.is_closed() and page.url == "about:blank":
                                # The popup exists before it has an address;
                                # attribution waits for the URL to commit.
                                requeued.append(note)
                                continue
                            opener = None
                            with contextlib.suppress(Exception):
                                opener = page.opener()
                            if opener is not None:
                                transactions.popup_opened(opener, page.url)
                    notes.extend(requeued)

                last_active_page = None
                last_front_poll = 0.0

                def detected_front_page():
                    front = _front_chrome_tab()
                    if front is None:
                        return None
                    url, title = front
                    candidates = [
                        current for current in ctx.pages
                        if not current.is_closed() and current.url == url
                    ]
                    if len(candidates) == 1:
                        return candidates[0]
                    if len(candidates) > 1 and title:
                        for current in candidates:
                            with contextlib.suppress(Exception):
                                if current.title() == title:
                                    return current
                    return None

                def record_tab_switch(current, *, timestamp: int,
                                      recording: bool, initial: bool = False) -> None:
                    nonlocal last_active_page
                    if current is None or current.is_closed():
                        return
                    if current is last_active_page and not initial:
                        return
                    try:
                        title = current.title()
                    except Exception:
                        title = ""
                    payload = {
                        "type": "tab_switch", "title": title,
                        "timestamp": timestamp,
                        "description": (("start on " if initial else "switch to ")
                                        + (title or current.url)),
                    }
                    if initial:
                        payload["initial"] = True
                    snapshot(current, current.main_frame, payload,
                             recording=recording)
                    last_active_page = current

                def emit(page, frame, event, recording) -> None:
                    """Ordered emission: snapshot one finalized event.

                    Tab-switch inference runs here, at emission, so a
                    synthetic switch can never land between a click's begin
                    and its settlement or ahead of a still-open transaction.
                    """
                    nonlocal last_active_page
                    kind = event.get("type")
                    if kind == "tab_switch":
                        if page is last_active_page:
                            return
                        last_active_page = page
                    elif kind in ACTION_TYPES and page is not last_active_page:
                        # A fast switch followed immediately by a gesture can
                        # beat the browser-chrome poll. Preserve the switch
                        # immediately before the first action on that page.
                        record_tab_switch(
                            page,
                            timestamp=max(0, int(event.get("timestamp")
                                                 or time.time() * 1000) - 1),
                            recording=recording,
                        )
                    snapshot(page, frame, event, recording=recording)

                ctx.on("page", discovered)
                pages[0].bring_to_front()
                self.ready.set()
                recording_active = False
                while not self.stop_requested.is_set():
                    live = next((page for page in ctx.pages if not page.is_closed()), None)
                    if live is None:
                        raise RuntimeError("all captured application pages were closed")
                    live.wait_for_timeout(100)
                    now = time.monotonic()
                    if now - last_front_poll >= 0.2:
                        last_front_poll = now
                        front = detected_front_page()
                        if front is not None and front is not last_active_page:
                            record_tab_switch(
                                front, timestamp=int(time.time() * 1000),
                                recording=recording_active)
                    drain_notes()
                    while pending:
                        page, frame, payload = pending.pop(0)
                        transactions.add(page, frame, payload,
                                         recording=recording_active)
                    # Ordered emission with a per-transaction paint delay:
                    # bursts overlap their waits instead of accumulating one
                    # serialized sleep per queued message, and an unsettled
                    # click holds everything behind it in authored order.
                    for page, frame, event, was_recording in transactions.pump():
                        emit(page, frame, event, was_recording)
                    if self.freeze_requested.is_set() and not self.freeze_ready.is_set():
                        # Finish the setup tail before taking the live-page
                        # snapshot. A final navigation or cell commit often
                        # arrives in the transaction queue just before the
                        # author presses Start recording.
                        for page, frame, event, was_recording in transactions.flush():
                            emit(page, frame, event, was_recording)
                        snapshots: list[dict] = []
                        for surface, surface_page in zip(self.surfaces, pages):
                            name = page_name(surface_page)
                            try:
                                title = surface_page.title()
                            except Exception:                 # noqa: BLE001
                                title = ""
                            snapshots.append({
                                "id": surface.get("id"),
                                "application": surface.get("application"),
                                "page": name,
                                "url": surface_page.url,
                                "title": title,
                            })
                        self.live_surfaces = snapshots
                        self.freeze_ready.set()
                    if self.recording_requested.is_set() and not recording_active:
                        # The setup phase ends here; releasing its buffered
                        # tail now keeps the recording timeline's initial-tab
                        # bookkeeping from interleaving with late setup events.
                        for page, frame, event, was_recording in transactions.flush():
                            emit(page, frame, event, was_recording)
                        self._close_tool_pages(tool_pages, pages[0])
                        tool_pages = []
                        recorder = screenrec.start(
                            self.directory / ".managed-recording", self.out)
                        self.voice_recorded = bool(getattr(recorder, "has_audio", False))
                        self.started_ms = _clock_start(recorder)
                        self.narration_lead_ms = max(
                            0, int(time.time() * 1000) - self.started_ms)
                        for current in ctx.pages:
                            if current.is_closed() or current.url == "about:blank":
                                continue
                            self.events.append({
                                "type": "goto", "page": page_name(current),
                                "url": current.url, "at_ms": 0,
                                "description": "open seeded recording state",
                            })
                        # The initial foreground tab is part of the observable
                        # timeline. Without it, a recording containing only a
                        # passive switch has no starting point to replay from.
                        initial = (detected_front_page()
                                   or last_active_page or pages[0])
                        # A setup-phase activation of this same page is distinct
                        # from the recording timeline's initial context marker.
                        last_active_page = None
                        record_tab_switch(
                            initial, timestamp=self.started_ms,
                            recording=True, initial=True)
                        recording_active = True
                        self.recording_ready.set()
                    for opened in list(discovered_pages):
                        if opened in captured_new_pages or opened.is_closed() or opened.url == "about:blank":
                            continue
                        captured_new_pages.add(opened)
                        target = self.events if recording_active else self.setup_events
                        target.append({
                            "type": "goto", "page": page_name(opened), "url": opened.url,
                            "at_ms": (int(time.time() * 1000)
                                      - self._clock_base(recording_active)),
                            "description": "open captured popup/tab",
                        })
                front = detected_front_page()
                if front is not None and front is not last_active_page:
                    record_tab_switch(
                        front, timestamp=int(time.time() * 1000),
                        recording=recording_active)
                drain_notes()
                while pending:
                    page, frame, payload = pending.pop(0)
                    transactions.add(page, frame, payload,
                                     recording=recording_active)
                # Recording stopped: an open transaction finalizes as
                # settlement "unavailable" rather than blocking the drain.
                for page, frame, event, was_recording in transactions.flush():
                    emit(page, frame, event, was_recording)
                # Phase-1 multi-click observability: how clicks classified
                # and how many dblclick markers paired, for auditing against
                # the recording video. Bundle evidence, never a replay input.
                diagnostics = transactions.diagnostics()
                if diagnostics:
                    (bundle / "click_diagnostics.json").write_text(
                        json.dumps(diagnostics, ensure_ascii=False,
                                   sort_keys=True) + "\n", encoding="utf-8")

            # settle() degrades a failed move to a warning: a capture must
            # never fail (or lose its events) because filing its video did.
            self.recording = screenrec.settle(
                recorder, self.directory / "recording", self.out,
                label="managed Chrome recording")
            recorder = screenrec.NullRecorder()
            with (self.directory / "events.jsonl").open("w", encoding="utf-8") as handle:
                for event in _dedupe(self.events):
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            with (self.directory / "setup_events.jsonl").open("w", encoding="utf-8") as handle:
                for event in _dedupe(self.setup_events):
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception as exc:
            self.error = exc
            self.ready.set()
            self.freeze_ready.set()
            self.recording_ready.set()
        finally:
            if not isinstance(recorder, screenrec.NullRecorder):
                recorder.stop()
            try:
                kill_managed_chrome(chrome.MANAGED_ROOT)
            except Exception as exc:
                self.out(f"  (managed Chrome cleanup failed: {exc})")
            self.finished.set()
