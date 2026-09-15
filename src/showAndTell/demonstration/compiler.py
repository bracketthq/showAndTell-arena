"""Compile a portable demonstration into a generated replay driver.

render_demonstrate turns the captured surfaces, setup markers, and gesture
stream into demonstrate.py source: a silent fail-fast seed stage plus a
narrated, recovering demonstrate stage bound onto showAndTell.player.replay's helpers.
The output is under a byte-equality contract with every stored human capture
(tests/test_human_task_capture_events.py), so a regenerated driver proves the
stored stream still compiles to exactly the code that was reviewed.
"""
from __future__ import annotations

import json
from urllib.parse import urlsplit

from showAndTell.applications.browser.runtime import browser_plane, origin as _origin

from .events import _dedupe
from .model import Demonstration, DemonstrationEvent, Surface


def _js(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _py(value) -> str:
    return repr(value)


# Mirrors BRIDGE_HOST_PAGE_PATH in applications/onlyoffice/connector.py; the
# connector owns the route, but importing it would drag its web stack in here.
_BRIDGE_EDITOR_PATH = "/web-apps/brackett-host/editor.html"


def _replayable_url(url: str, surfaces: list[dict]) -> str:
    """Swap a recorded address that cannot be navigated to for one that can.

    The spreadsheet's bridge host page takes its signed editor config from the
    URL fragment and strips it as soon as it has read it, so a recorder only
    ever observes the stripped address.  Replaying that shows "missing or
    invalid editor configuration"; the connector's editor route issues a fresh
    signed redirect, so the surface entry point is what a replay must use.
    """
    if _BRIDGE_EDITOR_PATH not in url:
        return url
    for surface in surfaces:
        if surface.get("application") == "onlyoffice" and surface.get("url"):
            return str(surface["url"])
    return url


def _hostname(url: str) -> str | None:
    return urlsplit(url).hostname


def _normalized(application: str | None, url: str) -> str:
    """Apply an application's replay-URL normalization while compiling.

    The same hook runs again inside runtime_application_url, but applying it
    here strips per-session state out of every captured address the driver
    embeds — including the primary application's, which replays as a plain
    ``app_url`` suffix and never passes through the runtime resolver.
    """
    hook = getattr(browser_plane(application), "normalize_replay_url", None)
    return hook(url) if callable(hook) else url


def _strip_selectors(meta: dict) -> dict:
    """The selector list rides positionally into `_locator`; the emitted target
    metadata keeps only the keys the replay ladder actually reads."""
    return {key: value for key, value in meta.items() if key != "selectors"}


def _replay_completion(
    application: str | None,
    kind: str,
    target: dict,
) -> str | None:
    """Ask an application's browser plane whether this action needs a gate."""
    hook = getattr(browser_plane(application), "replay_completion", None)
    if not callable(hook):
        return None
    completion = hook(kind, target)
    if completion is None:
        return None
    if not isinstance(completion, str) or not completion:
        raise ValueError(
            f"{application} returned an invalid replay completion: {completion!r}"
        )
    return completion


def _press_shortcut(event: dict) -> str:
    """Return a Playwright-style shortcut without discarding modifiers.

    Old captures only stored ``key``. Audited legacy repairs may supply
    ``replay_modifiers`` after screenshots prove that a modified key owned the
    recorded outcome; new captures retain the modifiers directly.
    """
    key = str(event.get("replay_key") or event.get("key") or "Enter")
    if "+" in key:
        return key
    modifiers = (event.get("replay_modifiers")
                 if "replay_modifiers" in event else event.get("modifiers"))
    return "+".join([*(str(value) for value in (modifiers or [])), key])


def compile_demonstration(demonstration: Demonstration) -> str:
    """Compile one typed demonstration artifact into Python driver source."""
    return render_demonstrate(
        list(demonstration.events),
        list(demonstration.surfaces),
        list(demonstration.setup_events),
    )


def render_demonstrate(
    events: list[dict] | list[DemonstrationEvent],
    surfaces: list[dict] | list[Surface],
    setup_events: list[dict] | list[DemonstrationEvent] | None = None,
) -> str:
    """Compile setup and recorded gestures into separate replay stages."""
    artifact = Demonstration.from_values(
        surfaces=surfaces,
        events=events,
        setup_events=setup_events or (),
    )
    surfaces = artifact.surface_dicts()
    events = artifact.event_dicts()
    setup_events = artifact.setup_event_dicts()
    # Stored drafts can predate the latest capture-time normalization.  Apply
    # it again while rendering so regenerating an old driver's source upgrades
    # its event stream without rewriting the evidence files on disk.
    events = _dedupe(events)
    setup_events = _dedupe(list(setup_events or []))
    primary = _origin(surfaces[0]["url"])
    page_surfaces = {
        ("page" if index == 0 else f"page{index + 1}"): surface
        for index, surface in enumerate(surfaces)
    }
    page_credentials = {
        page_name: surface.get("credentials", {})
        for page_name, surface in page_surfaces.items()
    }
    captured_surfaces = {surface["id"]: surface for surface in surfaces}
    # Every surface dict here came through Surface.from_mapping above, so
    # re-wrapping is loss-free and keeps Surface.application the one authority
    # for the application-ownership rule.
    surface_by_application = {
        Surface(surface).application: surface for surface in surfaces
    }
    primary_application = (
        Surface(surfaces[0]).application if surfaces else None
    )
    # Which application a replayed address belongs to, so a navigation can wait
    # for that application's own readiness gate.
    application_by_origin = {
        _origin(surface["url"]): Surface(surface).application
        for surface in surfaces if surface.get("url")
    }

    def landed_application(url: str, page_surface: dict) -> str | None:
        """Which application owns a captured address, or None for no claim.

        Managed runs relocate an application by port, never by host, so a
        same-host address outside the manifest is this page's own application
        answering on its live port. A foreign host or a non-web address
        (about:blank, a download) belongs to no runtime application: claiming
        one would re-point the address onto a surface that never served it.
        """
        origin = _origin(url)
        known = application_by_origin.get(origin)
        if known:
            return known
        if not url.startswith(origin):
            return None
        host = _hostname(url)
        if host and host == _hostname(str(page_surface.get("url") or "")):
            return Surface(page_surface).application
        return None

    has_login_events = any(
        event.get("type") == "login" for event in [*setup_events, *events])
    has_type_events = any(
        event.get("type") == "type" for event in [*setup_events, *events])
    has_paste_events = any(
        event.get("type") == "type" and event.get("input_source") == "paste"
        for event in [*setup_events, *events])
    has_cell_events = any(
        event.get("type") == "click" and event.get("cell_ref")
        for event in [*setup_events, *events])
    lines = [
        '"""Generated from a managed-Chrome action/accessibility capture.\n\n'
        "Review selectors marked by the capture bundle before promoting this draft.\n"
        '"""',
        "from __future__ import annotations",
        "",
        *(["from showAndTell.applications.onlyoffice.browser import select_cell as _select_onlyoffice_cell"]
          if has_cell_events else []),
        "from showAndTell.player import replay as _replay",
        "from showAndTell.applications.browser.runtime import (",
        *(["    auto_login as _auto_login,"] if has_login_events else []),
        "    runtime_application_url as _runtime_application_url,",
        ")",
        "",
        f"_CAPTURED_CREDENTIALS = {_py(page_credentials)}",
        f"_CAPTURED_SURFACES = {_py(captured_surfaces)}",
        "",
        "# Per-driver replay state, plus bindings onto showAndTell.player.replay's helpers.",
        "_REPLAY = _replay.new_replay_state()",
        "",
        "",
        *(["def _login_credentials(creds, page_name):",
           "    return _replay.login_credentials(_CAPTURED_CREDENTIALS, creds, page_name)",
           "",
           "",
           ] if has_login_events else []),
        "def _pace(at_ms):",
        "    _replay.pace(_REPLAY, at_ms)",
        "",
        "",
        "def _action_target(target):",
        "    return _replay.action_target(_REPLAY, target)",
        "",
        "",
        *(["def _type_text(page, text):",
           "    _replay.type_text(_REPLAY, page, text)",
           "",
           "",
           ] if has_type_events else []),
        *(["def _paste_text(page, text):",
           "    _replay.paste_text(_REPLAY, page, text)",
           "",
           "",
           ] if has_paste_events else []),
        "def _locator(root, selectors, target=None, allow_focus=False, timeout=None):",
        "    return _replay.locator(root, selectors, target=target,",
        "                           allow_focus=allow_focus, timeout=timeout,",
        "                           resolve_timeout=_REPLAY.get('resolve_timeout', 8.0))",
    ]

    def render_function(name: str, rows: list[dict], *, narrate: bool) -> int:
        lines.extend(["", "", f"def {name}(page, app_url, creds, on_step):",
                      "    pages = {'page': page}",
                      "    _skipped = []"])
        known_pages = {"page"}
        action_index = 0
        # The page whose tab is currently visible. Tab switches are not DOM
        # gestures, so they are never recorded; without explicit activation a
        # multi-app replay drives background tabs while the human (and the
        # product recording the screen) watches the wrong application.
        active_page = None
        # A field's own last legitimately-typed value, and — while a picker
        # episode on that same field is in progress — the value a failed
        # widget click can retype instead. See the tracking block in `emit`.
        last_named_fill: dict[str, tuple] = {}
        date_recovery_active: dict[str, tuple] = {}
        # The capture records the page URL after every gesture. Recovery may
        # navigate only when that gesture actually changed the captured URL;
        # otherwise an unsupported in-place key (for example Tab inside
        # ONLYOFFICE) would reopen the connector route and reload the workbook.
        last_url_by_page: dict[str, str] = {}

        def ensure_page(page_name: str) -> None:
            """Open a tab the replay has not used yet. Every surface that
            addresses `pages[...]` goes through here, so the open and the
            bookkeeping that keeps it to one `new_page()` cannot drift apart."""
            if page_name not in known_pages:
                lines.append(
                    f"    pages[{_py(page_name)}] = page.context.new_page()")
                known_pages.add(page_name)

        def emit(event: dict) -> None:
            nonlocal action_index, active_page
            kind = event["type"]
            page_name = event.get("page", "page")
            if kind == "login":
                surface_id = event.get("surface_id")
                if surface_id not in captured_surfaces:
                    return
                # Most recordings treat authentication as setup and repeat it
                # automatically before the visible demonstration.  A surface
                # can opt into replaying the login the operator actually
                # demonstrated instead.  seed() still authenticates when it
                # has to rebuild fixture state; only demonstrate() suppresses
                # the otherwise duplicate automatic login.
                if (narrate and (
                        event.get("replay_mode") == "demonstrated"
                        or captured_surfaces[surface_id].get(
                            "login_replay") == "demonstrated")):
                    return
                ensure_page(page_name)
                current = f"pages[{_py(page_name)}]"
                # Identical for every surface: auto_login picks each
                # application's own live address out of creds, so a supporting
                # application is no longer signed into at whatever address it
                # happened to have when this was captured.
                lines.append(
                    f"    _auto_login({current}, _CAPTURED_SURFACES[{_py(surface_id)}], "
                    f"app_url={'app_url' if page_name == 'page' else 'None'}, "
                    f"credentials=_login_credentials(creds, {_py(page_name)}))")
                return
            if kind == "goto":
                replay_url = event.get("replay_url")
                url = _replayable_url(
                    replay_url if isinstance(replay_url, str) and replay_url
                    else event.get("url", ""), surfaces)
                application = landed_application(
                    url, page_surfaces.get(page_name, {}))
                url = _normalized(application, url)
                last_url_by_page[page_name] = url
                ensure_page(page_name)
                captured_origin = _origin(url)
                if not url or (url.startswith(captured_origin)
                               and (page_name == "page"
                                    or captured_origin == primary)):
                    suffix = url[len(captured_origin):] or "/"
                    lines.append(
                        f"    pages[{_py(page_name)}].goto(app_url.rstrip('/') + "
                        f"{_py(suffix)}, wait_until='domcontentloaded')")
                elif application in surface_by_application:
                    surface_id = surface_by_application[application]["id"]
                    lines.append(
                        f"    pages[{_py(page_name)}].goto("
                        f"_runtime_application_url(creds, {_py(application)}, "
                        f"{_py(url)}, _CAPTURED_SURFACES[{_py(surface_id)}]['url']), "
                        "wait_until='domcontentloaded')")
                else:
                    lines.append(
                        f"    pages[{_py(page_name)}].goto({_py(url)}, "
                        "wait_until='domcontentloaded')")
                # A navigation returns when the document loaded, which for a
                # canvas editor is long before it can be driven.
                if application:
                    lines.append(
                        f"    _replay.wait_ready(pages[{_py(page_name)}], {_py(application)})")
                return
            if kind == "tab_switch":
                ensure_page(page_name)
                initial = bool(event.get("initial"))
                if narrate and not initial:
                    action_index += 1
                    lines.append(f"    _pace({int(event.get('at_ms') or 0)})")
                if page_name != active_page:
                    lines.append(f"    pages[{_py(page_name)}].bring_to_front()")
                    active_page = page_name
                    # Foregrounding a tab is a navigation-shaped moment: a
                    # background tab's timers are throttled, so the view the
                    # operator saw can still be building itself when the tab
                    # comes forward. Gate it through the same per-application
                    # readiness hook every goto runs. The initial switch only
                    # names which already-gated tab starts in front.
                    application = landed_application(
                        event.get("url", ""), page_surfaces.get(page_name, {}))
                    if application and not initial:
                        lines.append(
                            f"    _replay.wait_ready(pages[{_py(page_name)}], "
                            f"{_py(application)})")
                lines.append(f"    current = pages[{_py(page_name)}]")
                if narrate and not initial:
                    desc = (event.get("description")
                            or f"switch to {event.get('title') or page_name}")
                    keys = (event.get("narration_keys")
                            or [f"action-{action_index:03d}"])
                    for key in keys:
                        lines.append(f"    on_step({_py(key)}, current, {_py(desc)})")
                return
            if kind not in {"click", "drag", "fill", "select", "press", "type"}:
                return
            if narrate:
                action_index += 1
            selectors = event.get("selectors") or (
                [event["selector"]] if event.get("selector") else [])
            target_meta = event.get("target") or {}
            if (not selectors and kind == "click"
                    and target_meta.get("role") in {"div", "generic"}
                    and target_meta.get("tag") in {"div", "span"}
                    and not event.get("replay_noop_reason")):
                # Recorder noise from layout/tab-panel bubbling is not a user
                # data mutation and cannot be targeted uniquely on replay.
                return
            # A field's own real value survives being cleared for a picker
            # demo: remember the last one typed into each named field, and
            # while that SAME field sits cleared, keep the recovery armed
            # through the picker's own controls (empty or bare-digit names —
            # a nav arrow, a calendar day) until something with a real,
            # different name shows this field's episode is over.
            field_name = str(target_meta.get("name") or "").strip()
            if kind == "fill":
                raw_value = event.get("value", "")
                source = event.get("value_source")
                if (field_name and not raw_value
                        and last_named_fill.get(page_name, (None,))[0] == field_name):
                    date_recovery_active[page_name] = last_named_fill[page_name]
                else:
                    if field_name and raw_value and source not in {"password", "email"}:
                        last_named_fill[page_name] = (
                            field_name, list(selectors),
                            _strip_selectors(target_meta), raw_value)
                    date_recovery_active.pop(page_name, None)
            elif kind in {"click", "press"} and (not field_name or field_name.isdigit()):
                pass  # still inside the picker episode; leave armed state as is
            else:
                date_recovery_active.pop(page_name, None)
            allow_focus = kind == "fill" or target_meta.get("tag") in {"input", "textarea", "select"}
            if narrate:
                # Recorded actions carry the demonstration's real timing; the
                # setup stage is a silent prelude and should not be slowed.
                lines.append(f"    _pace({int(event.get('at_ms') or 0)})")

            body = []
            if page_name != active_page:
                body.append(f"pages[{_py(page_name)}].bring_to_front()")
                active_page = page_name
            body.append(f"current = pages[{_py(page_name)}]")
            audited_noop = event.get("replay_noop_reason")
            replay_landed_url = event.get("replay_landed_url")
            if audited_noop and replay_landed_url:
                raise ValueError(
                    "replay_noop_reason cannot be combined with "
                    "replay_landed_url; replay the navigation gesture instead"
                )
            if audited_noop:
                body.append("pass  # audited no-op")
            elif kind == "type":
                # IME-recorded keystrokes go through the page keyboard into
                # whatever the preceding canvas click focused; filling the
                # hidden textarea instead is garbage to the editor.
                type_helper = (
                    "_paste_text"
                    if event.get("input_source") == "paste"
                    else "_type_text"
                )
                body.append(
                    f"{type_helper}(current, {_py(event.get('text', ''))})"
                )
            elif kind == "drag":
                destination_meta = event.get("destination") or target_meta
                destination_selectors = destination_meta.get("selectors") or []
                drag_mode = event.get("drag_mode")
                body.append(
                    f"target = _action_target(_locator(_replay.root(current, {_py(event.get('frame_url'))}), "
                    f"{_py(selectors)}, {_py(_strip_selectors(target_meta))}, allow_focus=False))")
                if drag_mode == "pointer":
                    body.append("destination = None")
                else:
                    body.append(
                        f"destination = _action_target(_locator(_replay.root(current, {_py(event.get('frame_url'))}), "
                        f"{_py(destination_selectors)}, {_py(_strip_selectors(destination_meta))}, allow_focus=False))")
                drag_call = (
                    f"_replay.drag(current, target, destination, "
                    f"{_py(event.get('source_position'))}, "
                    f"{_py(event.get('target_position'))}, "
                    f"{_py(event.get('path') or [])}")
                if drag_mode is not None:
                    drag_call += f", drag_mode={_py(drag_mode)}"
                body.append(drag_call + ")")
            elif kind == "click" and event.get("commit_only"):
                # The operator clicked blank form space only to commit the
                # focused editor. Replaying its coordinate can activate a
                # floating control that was absent during capture.
                body.append(
                    f"_replay.commit_active_field(_replay.root(current, "
                    f"{_py(event.get('frame_url'))}))")
            elif kind == "click" and event.get("cell_ref"):
                # A spreadsheet's canvas coordinates drift with zoom, scroll,
                # and the worksheet's saved selection. The paired host event
                # recorded the exact A1 reference, so select it semantically.
                body.append(
                    f"_select_onlyoffice_cell(current, {_py(event['cell_ref'])})")
            else:
                body.append(
                    f"target = _action_target(_locator(_replay.root(current, {_py(event.get('frame_url'))}), "
                    f"{_py(selectors)}, {_py(_strip_selectors(target_meta))}, allow_focus={_py(allow_focus)}))")
                if kind == "click":
                    # A consolidated multi-click replays as ONE semantic call:
                    # Playwright dispatches the constituent clicks with the
                    # browser's own click count, regenerating the dblclick the
                    # application reacted to. Two separately captured clicks
                    # still compile to two calls — only browser-confirmed
                    # evidence ever sets click_count.
                    multi = event.get("click_count")
                    count = (f", click_count={multi}"
                             if isinstance(multi, int) and multi > 1 else "")
                    wheel_delta_y = event.get("replay_wheel_delta_y")
                    if (isinstance(wheel_delta_y, (int, float))
                            and wheel_delta_y != 0):
                        hover_position = event.get("position")
                        if hover_position:
                            body.append(
                                f"target.hover(position={_py(hover_position)})")
                        else:
                            body.append("target.hover()")
                        wheel_steps = event.get("replay_wheel_steps")
                        if (isinstance(wheel_steps, int)
                                and not isinstance(wheel_steps, bool)
                                and wheel_steps > 1):
                            body.append(f"for _ in range({wheel_steps}):")
                            body.append(
                                f"    current.mouse.wheel(0, {int(wheel_delta_y)})")
                            body.append("    current.wait_for_timeout(40)")
                        else:
                            body.append(
                                f"current.mouse.wheel(0, {int(wheel_delta_y)})")
                        body.append("current.wait_for_timeout(300)")
                    if event.get("position"):
                        # A replay's cursor sits still, so hover tooltips linger
                        # over the canvas and would intercept every later click.
                        # position clicks target the app's own drawing surface.
                        body.append(
                            f"target.click(position={_py(event['position'])}, force=True{count})")
                    elif (isinstance(event.get("checked_after"), bool)
                          and (target_meta.get("input_type") in {"checkbox", "radio"}
                               or target_meta.get("role") in {"checkbox", "radio"})):
                        # Reproduce the recorded Boolean state, not a relative
                        # toggle — the consolidated final state already nets a
                        # double-toggle out, so the count is ignored. `force`
                        # retains the reactive-overlay resilience that ordinary
                        # captured fields require; set_checked is idempotent
                        # and verifies the outcome.
                        body.append(
                            f"_replay.set_checked(_REPLAY, target, "
                            f"{_py(event['checked_after'])})")
                    elif target_meta.get("tag") in {"input", "textarea", "select"}:
                        # _locator already proved this is the unique visible
                        # captured field. Reactive forms can replace or cover a
                        # field for a moment while committing the preceding
                        # value (ERPNext grids do this on blur); Playwright's
                        # actionability wait then times out on the correct
                        # control even though dispatching its click is safe.
                        body.append(f"target.click(force=True{count})")
                    else:
                        body.append(f"target.click({count.removeprefix(', ')})")
                    login_response_path = event.get(
                        "replay_wait_for_response_path")
                    if (isinstance(login_response_path, str)
                            and login_response_path.startswith("/")):
                        click_line = body.pop()
                        body.append(
                            "with current.expect_response("
                            "lambda response: response.request.method == 'POST' "
                            f"and response.url.endswith({_py(login_response_path)}), "
                            "timeout=30000) as _login_response_info:")
                        body.append(f"    {click_line}")
                        body.append(
                            "if not _login_response_info.value.ok:")
                        body.append(
                            "    raise RuntimeError('demonstrated login request failed: ' "
                            "+ str(_login_response_info.value.status))")
                    post_login_path = event.get("replay_post_login_path")
                    if (isinstance(post_login_path, str)
                            and post_login_path.startswith("/")
                            and page_name == "page"):
                        # The recorded Frappe redirect may carry the capture
                        # machine's absolute origin (and can therefore lose a
                        # clean runtime's port).  The click remains visible, but
                        # its authenticated landing always uses the live task
                        # origin.  If the click itself raises on the bad
                        # redirect, the recovery URL below uses this same path.
                        body.append(
                            f"current.goto(app_url.rstrip('/') + "
                            f"{_py(post_login_path)}, wait_until='domcontentloaded')")
                        if primary_application:
                            body.append(
                                f"_replay.wait_after_login(current, "
                                f"{_py(primary_application)})")
                    page_surface = page_surfaces.get(page_name)
                    application = (
                        Surface(page_surface).application
                        if page_surface is not None else None
                    )
                    completion = _replay_completion(
                        application, kind, target_meta)
                    if completion:
                        body.append(
                            "_replay.wait_for_application_completion("
                            f"current, {_py(application)}, {_py(completion)})"
                        )
                elif kind == "fill":
                    source = event.get("value_source")
                    fallback = page_credentials.get(page_name, {})
                    if source == "password":
                        if page_name == "page":
                            value = f"creds.get('password') or {_py(fallback.get('password', ''))}"
                        else:
                            value = f"_CAPTURED_CREDENTIALS.get({_py(page_name)}, {{}}).get('password', '')"
                    elif source == "email":
                        if page_name == "page":
                            value = f"creds.get('email') or {_py(fallback.get('email', event.get('value', '')))}"
                        else:
                            value = (f"_CAPTURED_CREDENTIALS.get({_py(page_name)}, {{}}).get"
                                     f"('email', {_py(event.get('value', ''))})")
                    else:
                        value = _py(event.get("value", ""))
                    body.append(f"target.fill({value})")
                elif kind == "select":
                    body.append(f"target.select_option({_py(event.get('value', ''))})")
                else:
                    body.append(f"target.press({_py(_press_shortcut(event))})")

            if not narrate:
                # The seed stage rebuilds state, where a silent miss corrupts
                # the whole trial — it stays fail-fast.
                lines.extend(f"    {line}" for line in body)
                return

            desc = (event.get("description")
                    or f"{kind} {event.get('text') or target_meta.get('name', '')}".strip())
            if audited_noop:
                desc = f"audited no-op ({audited_noop}): {desc}"
            keys = event.get("narration_keys") or [f"action-{action_index:03d}"]
            # Where the page stood once this gesture had been performed, so a
            # gesture that cannot be performed here can still be recovered.
            # Only a click or a keypress can take a page somewhere; typing into
            # a field that is not there cannot be repaired by going anywhere,
            # and pretending otherwise would navigate away from the form the
            # demonstration was in the middle of filling.
            post_login_path = event.get("replay_post_login_path")
            if isinstance(replay_landed_url, str) and replay_landed_url:
                post_url = _replayable_url(replay_landed_url, surfaces)
            elif (kind == "click" and isinstance(post_login_path, str)
                    and post_login_path.startswith("/") and page_name == "page"):
                post_url = primary.rstrip("/") + post_login_path
            else:
                post_url = _replayable_url(event.get("url", ""), surfaces)
            post_application = landed_application(
                post_url, page_surfaces.get(page_name, {})) if post_url else None
            post_url = _normalized(post_application, post_url)
            previous_url = last_url_by_page.get(page_name)
            moved_page = bool(post_url) and (
                previous_url is None or post_url != previous_url)
            landed = (
                post_url
                if kind in {"click", "press"}
                and (moved_page or bool(replay_landed_url))
                else ""
            )
            if post_url:
                last_url_by_page[page_name] = post_url
            landed_app = post_application if landed else None
            if landed and landed_app == primary_application:
                captured_origin = _origin(landed)
                landed_expr = (f"app_url.rstrip('/') + "
                               f"{_py(landed[len(captured_origin):] or '/')}")
            elif landed and landed_app in surface_by_application:
                surface_id = surface_by_application[landed_app]["id"]
                landed_expr = (
                    f"_runtime_application_url(creds, "
                    f"{_py(landed_app)}, {_py(landed)}, "
                    f"_CAPTURED_SURFACES[{_py(surface_id)}]['url'])"
                )
            else:
                landed_expr = _py(landed)
            if replay_landed_url:
                body.append(
                    f"_replay.require_place(current, {landed_expr})")
            # A widget click standing in for a field's own value: the field's
            # last real typed value, to retype if this click cannot be driven.
            if page_name in date_recovery_active:
                refill_expr = _py(date_recovery_active[page_name][1:])
            else:
                refill_expr = "None"
            # A momentarily stuck control must not kill a long teach near the
            # end; the miss is voiced through on_step and counted, and the
            # replay fails only when a large share of the demo was skipped.
            lines.append("    try:")
            lines.extend(f"        {line}" for line in body)
            lines.append("    except Exception as exc:")
            if replay_landed_url:
                lines.append(
                    f"        raise RuntimeError({_py('audited navigation failed: ' + desc)}) "
                    "from exc")
            else:
                recovery_keys = keys if len(keys) > 1 else keys[0]
                lines.append(
                    f"        _replay.recover(pages[{_py(page_name)}], {landed_expr}, "
                    f"{_py(landed_app)}, "
                    f"{_py(desc)}, {_py(recovery_keys)}, on_step, _skipped, exc, "
                    f"refill={refill_expr})")
            lines.append("    else:")
            for key in keys:
                lines.append(f"        on_step({_py(key)}, current, {_py(desc)})")

        # seed() may be skipped when application state was restored from a
        # snapshot, and demonstrate() always creates fresh supporting tabs of
        # its own. Authentication therefore belongs in both stages: replay
        # only the setup login markers here, never its data-changing gestures.
        if narrate:
            for event in setup_events:
                if event.get("type") == "login":
                    emit(event)
        for event in rows:
            emit(event)
        if not rows:
            lines.append("    return None")
        elif narrate and action_index == 0:
            lines.append("    on_step('capture-empty', page, 'No replayable actions were captured')")
        elif narrate:
            lines.append(f"    if len(_skipped) * 3 > {action_index}:")
            lines.append(
                "        raise RuntimeError('replay skipped %d of "
                f"{action_index} actions: ' % len(_skipped) + ', '.join(_skipped[:5]))")
        return action_index

    render_function("seed", setup_events, narrate=False)
    render_function("demonstrate", list(events), narrate=True)
    lines.append("")
    return "\n".join(lines)
