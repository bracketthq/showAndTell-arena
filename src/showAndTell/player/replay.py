"""Runtime library for generated demonstrate.py replay drivers.

A managed capture compiles into a driver module (see
``showAndTell.demonstration.compiler.render_demonstrate``) whose gestures resolve targets and
recover from replay drift through the helpers here. Each generated module
keeps its own ``_REPLAY`` state dict — created by :func:`new_replay_state` —
so trial runners can install pacing and input hooks per driver; the stateful
helpers take that dict explicitly and everything else is pure.
"""
from __future__ import annotations

import re
import sys
import time
from urllib.parse import parse_qsl, urlsplit

from showAndTell.applications.browser.context import CONTEXT_KEY
from showAndTell.applications.browser.runtime import (
    browser_plane,
    wait_after_login,
    wait_ready,
)
from showAndTell.core.pwerrors import PlaywrightError


_CHECKED_SETTLE_TIMEOUT = 1.0
_CHECKED_POLL_INTERVAL = 0.025


def new_replay_state() -> dict:
    """Fresh per-driver replay state, set by a trial runner that plays the
    original recording alongside the replay."""
    return {'start': None, 'on_start': None, 'speed': 1.0,
            'resolve_timeout': 8.0, 'locator_wrapper': None,
            'type_text': None, 'paste_text': None}


def login_credentials(captured_credentials, creds, page_name):
    """Use live multi-app credentials, or this page's captured fallback."""
    context = creds.get(CONTEXT_KEY) if isinstance(creds, dict) else None
    if isinstance(context, dict):
        return creds
    return captured_credentials.get(page_name, {})


def pace(replay, at_ms):
    """Hold until this action's moment in the original demonstration.

    One clock drives both the replay and the recorded narration playing
    beside it, so the operator's voice stays on the action it describes.
    Running unpaced also races ahead of a human-legible speed.
    """
    start = replay.get('start')
    if start is None:
        # A trial installs this hook before demonstrate(). Setup login,
        # supporting-tab navigation, and readiness waits all run before
        # the first paced action. Starting narration here keeps that
        # setup silent instead of speaking over pages that are loading.
        on_start = replay.get('on_start')
        if on_start is not None:
            replay['on_start'] = None
            on_start()
            start = replay.get('start')
        if start is None:
            return
    speed = replay.get('speed') or 1.0
    delay = (start + (at_ms / 1000.0) / speed) - time.monotonic()
    if delay > 0:
        # The deadline is absolute, so an action that ran long already has a
        # non-positive delay and proceeds immediately.  Never cap a positive
        # delay: recorded narration cannot skip the same interval, and doing
        # so moves every following action ahead of the voice until the clock
        # happens to catch up again.
        time.sleep(delay)


def action_target(replay, target):
    wrapper = replay.get('locator_wrapper')
    return wrapper(target) if wrapper is not None else target


def type_text(replay, page, text):
    routed = replay.get('type_text')
    if routed is not None:
        routed(page, text)
    else:
        page.keyboard.type(text, delay=40)


def paste_text(replay, page, text):
    """Replay a captured paste through the active input plane.

    A routed hook performs a genuine OS paste so product recorders can observe
    it. Browser replay writes the captured text to Chromium's clipboard and
    sends the platform paste shortcut; unlike ``keyboard.insert_text``, this
    fires a real ``paste`` event and preserves tab/newline spreadsheet paste
    semantics.
    """
    routed = replay.get('paste_text')
    if routed is None and replay.get('type_text') is not None:
        # Older external input planes only expose routed typing. Keep those
        # gestures observable rather than silently bypassing them through CDP.
        routed = replay['type_text']
    if routed is not None:
        routed(page, text)
    else:
        parts = urlsplit(page.url)
        if parts.scheme not in {'http', 'https'} or not parts.netloc:
            raise RuntimeError(
                f"cannot grant clipboard access for replay page {page.url!r}")
        origin = f"{parts.scheme}://{parts.netloc}"
        page.context.grant_permissions(
            ['clipboard-read', 'clipboard-write'], origin=origin)
        page.evaluate(
            "value => navigator.clipboard.writeText(value)", text)
        shortcut = 'Meta+V' if sys.platform == 'darwin' else 'Control+V'
        page.keyboard.press(shortcut)
    # Controlled editors may debounce the browser input before committing it.
    page.wait_for_timeout(1500)


def commit_active_field(root):
    """Commit an edit without replaying a background click by coordinates.

    Operators commonly click blank form space to blur a grid input. The point
    they happened to click is incidental, and replaying it is not merely
    imprecise: a click on a page-sized container resolves to that container's
    centre, and Playwright's hit-target check accepts the target OR ANY
    DESCENDANT, so the call silently activates whichever control now sits
    there and still reports success.

    Blurring the focused editor reproduces what the gesture was for. The
    browser fires change, blur and focusout, and focus lands on <body> --
    where the operator's own click left it. `fill` alone does not commit:
    it fires input and keeps the focus, so a field typed into and never
    blurred never reaches the application's model.

    What this deliberately does NOT reproduce is the pointer events a real
    click dispatches, so a widget that dismisses itself on a document-level
    mousedown never sees one. That is the cost of being unable to hit the
    wrong thing; Frappe controls commit on change and close on blur, which
    is the case this serves.

    One evaluate rather than root.locator(':focus').blur(): the caller needs
    to know whether the focus was editable at all, and reading the focus and
    blurring it in a single turn leaves no window for it to move in between.
    """
    return root.evaluate("""() => {
      const active = document.activeElement;
      if (!active || !(active.matches('input,textarea,select')
          || active.isContentEditable)) return false;
      active.blur();
      return true;
    }""")


def _pointer_drag_geometry(box, source_position, path):
    """Translate a captured pointer path onto the source's current raw box.

    Captured points are offsets from the source box at pointerdown.  Clamp the
    current press inside today's box, then translate every point by the same
    adjustment so the demonstrated movement deltas remain unchanged.
    """
    points = list(path or [])
    recorded_start = (points[0] if points else None) or source_position or {
        'x': box['width'] / 2, 'y': box['height'] / 2}
    requested_start = source_position or recorded_start

    def inside(value, extent):
        extent = max(0.0, float(extent))
        # A thousandth of a CSS pixel can round onto the neighbouring element
        # when Playwright/Quartz converts to device coordinates. Stay half a
        # CSS pixel inside normal controls, or use the centre when thinner.
        inset = min(0.5, extent / 2)
        return min(max(float(value), inset), max(inset, extent - inset))

    start = {
        'x': inside(requested_start['x'], box['width']),
        'y': inside(requested_start['y'], box['height']),
    }
    dx = start['x'] - float(recorded_start['x'])
    dy = start['y'] - float(recorded_start['y'])
    translated = [
        {'x': float(point['x']) + dx, 'y': float(point['y']) + dy}
        for point in points
    ]
    if translated:
        translated[0] = start
    return start, translated


def drag(page, source, destination, source_position, target_position, path,
         drag_mode=None):
    """Replay pointer motion faithfully; keep legacy/native drag compatibility."""
    routed = getattr(source, 'drag_path_to', None)
    if routed is not None:
        routed(destination, source_position=source_position,
               target_position=target_position, path=path,
               drag_mode=drag_mode)
        return
    if drag_mode == 'pointer':
        points = list(path or [])
        if len(points) < 2:
            raise RuntimeError("captured pointer drag has no movement path")
        # Measure only after scrolling. Hover provides Playwright's normal
        # actionability check, then a second measurement absorbs any layout
        # change caused by scrolling or hover styling before pointerdown.
        source.scroll_into_view_if_needed()
        box = source.bounding_box()
        if box is None:
            raise RuntimeError("drag source has no visible bounding box")
        start, _ = _pointer_drag_geometry(box, source_position, points)
        source.hover(position=start)
        box = source.bounding_box()
        if box is None:
            raise RuntimeError("drag source disappeared while preparing drag")
        start, translated = _pointer_drag_geometry(
            box, source_position, points)
        page.mouse.move(box['x'] + start['x'], box['y'] + start['y'])
        page.mouse.down()
        try:
            # Every point is relative to the source box at pointerdown. The
            # final captured coordinate is the release point; no DOM
            # destination is required for a resizer, slider, canvas, or map.
            for point in translated[1:]:
                page.mouse.move(box['x'] + point['x'], box['y'] + point['y'])
        finally:
            page.mouse.up()
        return
    if drag_mode == 'native':
        source_box = source.bounding_box()
        destination_box = destination.bounding_box()
        if source_box is None or destination_box is None:
            raise RuntimeError("native drag source or destination is not visible")
        safe_source, _ = _pointer_drag_geometry(
            source_box, source_position, [])
        safe_target, _ = _pointer_drag_geometry(
            destination_box, target_position, [])
        source.drag_to(destination, source_position=safe_source,
                       target_position=safe_target)
        return
    if len(path or []) < 2:
        source.drag_to(destination, source_position=source_position,
                       target_position=target_position)
        return
    box = source.bounding_box()
    if box is None:
        source.drag_to(destination, source_position=source_position,
                       target_position=target_position)
        return
    source.hover(position=source_position)
    page.mouse.down()
    try:
        for point in path[1:-1]:
            page.mouse.move(box['x'] + point['x'], box['y'] + point['y'])
        destination.hover(position=target_position)
    finally:
        page.mouse.up()


def single_visible(candidate):
    """The unique VISIBLE match — never an invisible sole match.

    An SPA keeps hidden copies of dialog controls in the DOM (a hidden
    form page has the same fields as the quick-entry dialog above it);
    the visible twin is the one the operator acted on. A structural
    CSS path (nth-of-type chains) is captured only when nothing more
    stable was available, so on a page whose DOM shifted even slightly
    it just as often lands on some unrelated, unrendered node — a
    dangling tooltip left over from an earlier hover, in one observed
    case. A captured click was necessarily on something the operator
    could see, so a 'unique' invisible match is the wrong match, not
    the right one holding still; treating it as absent lets the caller
    fall through to a better rung, or keep waiting for this one to
    become visible.
    """
    count = candidate.count()
    if count == 1:
        return candidate if candidate.is_visible() else None
    if count > 1:
        shown = [i for i in range(count) if candidate.nth(i).is_visible()]
        if len(shown) == 1:
            return candidate.nth(shown[0])
    return None


def reveal_parent(root, selectors):
    """Expand a captured hierarchical menu when its parent is closed."""
    for selector in selectors:
        match = re.match(
            r'^\[data-id=(?:"([^"]+)"|\'([^\']+)\')\]\s+\[data-id=',
            selector,
        )
        if match is None:
            continue
        label = match.group(1) or match.group(2)
        try:
            parent = single_visible(root.get_by_text(label, exact=True))
            if parent is None:
                continue
            parent.click()
            return True
        except Exception:
            continue
    return False


def accessible_name(target):
    """The captured name, but only where it is a real accessible name.

    A recorded name reaches us through one of three channels, and only
    one of them is the string playwright's own name computation would
    arrive at. Tooltip prose lives in a framework data attribute the AX
    tree never reads, and a `name=` attribute is machine identity that no
    user agent surfaces at all — asking get_by_role or get_by_text for
    either one cannot succeed, and a stray same-text match is worse than
    the miss. Events captured before the channel was recorded carry no
    marker and keep their historical benefit of the doubt.
    """
    target = target or {}
    if target.get('name_source') in {None, 'accessible'}:
        return target.get('name') or ''
    return ''


def label_identity(target):
    """The captured name where it is text a person could read on screen.

    Wider than the accessible name: a Bootstrap tooltip is not an
    accessible name, yet it is exactly what the operator hovered to read,
    so it can still confirm or refute a candidate. A `name=` attribute
    is the one channel that is not a label in any sense.
    """
    target = target or {}
    if target.get('name_source') == 'attribute':
        return ''
    return target.get('name') or ''


_ARIA_ROLES = {
    'alert', 'alertdialog', 'application', 'article', 'banner', 'blockquote',
    'button', 'caption', 'cell', 'checkbox', 'code', 'columnheader',
    'combobox', 'complementary', 'contentinfo', 'definition', 'deletion',
    'dialog', 'directory', 'document', 'emphasis', 'feed', 'figure', 'form',
    'grid', 'gridcell', 'group', 'heading', 'img', 'insertion', 'link', 'list',
    'listbox', 'listitem', 'log', 'main', 'marquee', 'math', 'menu', 'menubar',
    'menuitem', 'menuitemcheckbox', 'menuitemradio', 'meter', 'navigation',
    'note', 'option', 'paragraph', 'progressbar', 'radio', 'radiogroup',
    'region', 'row', 'rowgroup', 'rowheader', 'scrollbar', 'search',
    'searchbox', 'separator', 'slider', 'spinbutton', 'status', 'strong',
    'subscript', 'superscript', 'switch', 'tab', 'table', 'tablist',
    'tabpanel', 'term', 'textbox', 'time', 'timer', 'toolbar', 'tooltip',
    'tree', 'treegrid', 'treeitem',
}


def semantic_role(target):
    """The actual ARIA role, never a DOM tag stored in the legacy role slot."""
    target = target or {}
    captured = target.get('aria_role')
    if captured is not None:
        return captured if captured in _ARIA_ROLES else ''
    legacy = target.get('role')
    return legacy if legacy in _ARIA_ROLES else ''


def selector_kind(selector, target=None, index=None):
    """Semantic/contract selectors are trusted; CSS fallbacks need proof.

    New captures carry the recorder's score-derived classification. The string
    fallback keeps old events safe without rewriting their evidence files.
    """
    kinds = (target or {}).get('selector_kinds') or []
    if isinstance(index, int) and index < len(kinds):
        kind = kinds[index]
        if kind in {'semantic', 'contract', 'weak', 'structural'}:
            return kind
    selector = str(selector or '')
    if selector.startswith(('role=', 'text=')):
        return 'semantic'
    if selector.startswith(('xpath=', '//')):
        return 'structural'
    if (':nth-' in selector or ' > ' in selector):
        return 'structural'
    if selector.startswith(('#', '[')):
        return 'contract'
    if re.match(r'^[a-zA-Z][\w-]*(?:\.[\w-]+)+$', selector):
        return 'weak'
    return 'weak'


def _normalize_identity(value):
    return ' '.join(str(value or '').split()).casefold()


def named_control_matches(candidate, target):
    """Compatibility identity check for minimal/custom locator objects.

    Real Playwright locators are corroborated with its semantic engines below.
    This exact-channel fallback exists for unit doubles and integrations that
    expose only attributes/text. It deliberately never treats a text input's
    current ``value`` as its name.
    """
    target = target or {}
    expected_label = label_identity(target)
    if not expected_label:
        return True
    expected = _normalize_identity(expected_label)
    identity_kind = target.get('identity_kind') or ''
    if identity_kind == 'name-attribute':
        return True  # machine identity, not a human-visible label
    attributes = {
        'aria-label': ('aria-label',),
        'placeholder': ('placeholder',),
        'title': ('title',),
        'tooltip': ('data-original-title', 'data-bs-title'),
    }.get(identity_kind)
    observed = []
    for attribute in (attributes or (
            'aria-label', 'data-label', 'title', 'data-original-title',
            'data-bs-title', 'placeholder')):
        try:
            value = candidate.get_attribute(attribute)
        except Exception:
            value = None
        if value and value.strip():
            observed.append(value.strip())
    if identity_kind in {'', 'text', 'label', 'aria-labelledby'}:
        try:
            text = candidate.inner_text().strip()
        except Exception:
            text = ''
        if text:
            observed.append(text)
    if target.get('tag') == 'button':
        try:
            value = candidate.get_attribute('value')
        except Exception:
            value = None
        if value and value.strip():
            observed.append(value.strip())
    if not observed:
        return True  # minimal/custom locators may not expose labels
    return any(expected == _normalize_identity(value) for value in observed)


def _identity_locator(root, target):
    """Build Playwright's own semantic view of the captured identity."""
    target = target or {}
    name = accessible_name(target)
    if not name or not name.strip():
        return None
    role = semantic_role(target)
    try:
        if role:
            return root.get_by_role(role, name=name, exact=True)
        kind = target.get('identity_kind')
        if kind == 'placeholder':
            return root.get_by_placeholder(name, exact=True)
        if kind == 'label':
            return root.get_by_label(name, exact=True)
        if kind == 'title':
            return root.get_by_title(name, exact=True)
        if kind in {None, '', 'text'} and target.get('tag') not in {
                'input', 'textarea', 'select'}:
            return root.get_by_text(name, exact=True)
    except Exception:
        return None
    return None


def corroborated_candidate(root, candidate, target):
    """Require positive semantic proof before accepting a weak CSS match."""
    identity = _identity_locator(root, target)
    if identity is not None:
        try:
            return candidate if single_visible(candidate.and_(identity)) is not None else None
        except Exception:
            pass  # compatibility with minimal/custom locator implementations
    return candidate if named_control_matches(candidate, target) else None


def resolve(root, selectors, target, allow_focus):
    for index, selector in enumerate(selectors):
        kind = selector_kind(selector, target, index)
        try:
            raw = root.locator(selector)
            candidate = None
            semantically_narrowed = False
            if kind in {'weak', 'structural'}:
                identity = _identity_locator(root, target)
                if identity is not None:
                    try:
                        candidate = single_visible(raw.and_(identity))
                        semantically_narrowed = candidate is not None
                    except Exception:
                        candidate = None
            if candidate is None:
                candidate = single_visible(raw)
            candidate = coerce_target(candidate, target)
        except Exception:
            continue  # engine-grammar edge case; the next rung still works
        if candidate is not None:
            if (kind in {'weak', 'structural'}
                    and not semantically_narrowed):
                candidate = corroborated_candidate(root, candidate, target)
            if candidate is not None:
                return candidate
    target = target or {}
    role, name = semantic_role(target), accessible_name(target)
    if role and name:
        candidate = single_visible(root.get_by_role(role, name=name, exact=True))
        if candidate is not None:
            return candidate
    if (name and name.strip()
            and target.get('tag') not in {'input', 'textarea', 'select'}):
        # The captured 'role' is the element's TAG, not a resolved ARIA
        # role, so it names no real role for the plain <p>/<div> nodes an
        # autocomplete or dropdown draws its options from — the role
        # rung above never fires for them, and only a fragile structural
        # selector was ever captured. Its own visible text is what the
        # operator actually read before clicking, and survives a DOM
        # that reshuffled around it. A form field is the one target this
        # rung must not serve: its captured name is an aria-label or
        # placeholder — text that also sits visibly on the LABEL beside
        # it — so matching by text hands the action that label, while
        # the focus rung below finds the field itself.
        try:
            candidate = single_visible(root.get_by_text(name, exact=True))
        except Exception:
            candidate = None
        if candidate is not None:
            return candidate
    if allow_focus:
        candidate = single_visible(root.locator(':focus'))
        candidate = coerce_target(candidate, target)
        if candidate is not None:
            return candidate
    tag = target.get('tag')
    if tag and tag not in {'div', 'span'}:
        candidate = single_visible(root.locator(tag))
        if candidate is not None:
            candidate = corroborated_candidate(root, candidate, target)
            if candidate is not None:
                return candidate
    return None


def coerce_target(candidate, target):
    """Turn captured wrappers/inner graphics into actionable controls."""
    if candidate is None:
        return None
    tag = (target or {}).get('tag')
    try:
        actual = candidate.evaluate("el => el.tagName.toLowerCase()")
    except Exception:
        return candidate  # compatibility with minimal/custom locators
    if tag in {'path', 'use'} and actual == tag:
        # Browsers hit-test an SVG graphic through its container. A
        # recorded inner <path> is visible but Playwright refuses to
        # click it when the parent <svg> intercepts pointer events.
        try:
            parent = single_visible(candidate.locator('xpath=..'))
            return parent or candidate
        except Exception:
            return candidate
    try:
        token_source = candidate.get_attribute('data-recipient-input')
    except Exception:
        token_source = None
    if token_source == 'true':
        # Roundcube keeps the captured textarea off-screen and puts
        # the actual caret in the adjacent recipient-token input.
        # Filling the source briefly paints text but the widget clears
        # it before the following Enter can create a recipient chip.
        try:
            proxy = single_visible(candidate.locator(
                'xpath=following-sibling::ul[contains(@class, "recipient-input")]//input'))
            if proxy is not None:
                return proxy
        except Exception:
            pass
    if tag not in {'input', 'textarea', 'select'}:
        return candidate
    if actual == tag:
        return candidate
    try:
        return single_visible(candidate.locator(tag))
    except Exception:
        return None


def locator(root, selectors, target=None, allow_focus=False, timeout=None,
            resolve_timeout=8.0):
    """The captured target, waiting for the page to produce it.

    Counting matches is a snapshot, so a target that is one element a
    moment later is 'not unique' now — a table mid-render has no filter
    row, and a re-render swaps the node out from under the count. An
    operator never met that race because they only acted on what they
    could already see; a replay arrives while the page is still building
    itself, so the whole ladder is retried until the deadline.
    """
    if timeout is None:
        timeout = resolve_timeout
        # Save-to-Submit and similar named toolbar transitions can take
        # longer while a product recorder is observing the page. A broad
        # CSS fallback is deliberately rejected when it still names the
        # old button, so give the requested semantic control the same
        # actionability window Playwright gives the eventual click.
        if ((target or {}).get('role') in {'button', 'link'}
                and (target or {}).get('name')):
            timeout = max(timeout, 30.0)
    deadline = time.monotonic() + timeout
    revealed_parent = False
    while True:
        candidate = resolve(root, selectors, target, allow_focus)
        if candidate is not None:
            return candidate
        if not revealed_parent:
            revealed_parent = reveal_parent(root, selectors)
            if revealed_parent:
                continue
        if time.monotonic() >= deadline:
            raise RuntimeError(f'captured target did not resolve uniquely: selectors={selectors}, target={target}')
        time.sleep(0.25)


def same_place(here, there):
    """Whether two URLs name the same view of the same application.

    A document the operator had not saved is routed by a name the desk
    mints per session (new-material-request-<random>), so the capture's
    URL and the replay's URL for the SAME form never match textually.
    Treating that as a different place is destructive: 'going back' to
    the captured name does not return anywhere, it opens yet another
    empty draft and discards the form being filled.
    """
    def parts(url):
        return url.split('?', 1)[0].rstrip('/').split('/')
    here_parts, there_parts = parts(here), parts(there)
    if len(here_parts) != len(there_parts):
        return False
    for mine, theirs in zip(here_parts, there_parts):
        if mine == theirs:
            continue
        if mine.startswith('new-') and theirs.startswith('new-'):
            continue  # the same unsaved document, named per session
        return False
    return True


def same_audited_place(here, there):
    """Whether two URLs have the same path and audited query/fragment state.

    Ordinary recovery deliberately ignores query strings because applications
    add transient session parameters. An explicit ``replay_landed_url`` is a
    stronger assertion: filters and mailbox views often differ only by query,
    so those components are part of the reviewed outcome.
    """
    if not same_place(here, there):
        return False
    mine, theirs = urlsplit(here), urlsplit(there)
    mine_query = sorted(parse_qsl(mine.query, keep_blank_values=True))
    their_query = sorted(parse_qsl(theirs.query, keep_blank_values=True))
    return mine_query == their_query and mine.fragment == theirs.fragment


def require_place(page, url, timeout=8.0):
    """Require a recorded navigation to reach its exact audited URL state."""
    deadline = time.monotonic() + timeout
    while not same_audited_place(page.url, url):
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f'recorded navigation did not reach {url}; remained at {page.url}'
            )
        page.wait_for_timeout(100)


def _wait_for_checked(target, checked, timeout=None):
    """Wait for a Boolean control's live state across reactive replacements."""
    if timeout is None:
        timeout = _CHECKED_SETTLE_TIMEOUT
    deadline = time.monotonic() + timeout
    while True:
        try:
            if target.is_checked() == checked:
                return True
        except PlaywrightError:
            # A locator re-resolves after a framework replaces its element. A
            # read can still land in the small interval where neither node is
            # usable, so keep polling until the same settlement deadline.
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_CHECKED_POLL_INTERVAL, remaining))


def set_checked(replay, target, checked):
    """Reach and verify a recorded Boolean state through one UI gesture.

    ``set_checked`` and an OS-routed click can both deliver their gesture before
    a reactive control publishes its replacement node or ARIA state. Waiting
    for that state is safe; guessing that the first gesture failed and toggling
    again is not. Browser-side mutation is deliberately excluded because it
    cannot prove that the application's controlled model accepted the value.
    """
    if _wait_for_checked(target, checked, timeout=0):
        return

    # OS-input replay wraps the locator so the gesture remains visible to the
    # recorder. Issue it exactly once, then allow the application to settle.
    if replay.get('locator_wrapper') is not None:
        target.click(force=True)
        if not _wait_for_checked(target, checked):
            raise RuntimeError(
                f'checkbox did not reach recorded state {checked} via routed input'
            )
        return

    try:
        target.set_checked(checked, force=True)
    except PlaywrightError as exc:
        # Playwright can report that its post-click verification lost a node
        # even though the application accepted the gesture. Preserve that one
        # gesture and give its reactive replacement time to publish the state.
        if _wait_for_checked(target, checked):
            return
        raise RuntimeError(
            f'checkbox did not reach recorded state {checked}'
        ) from exc
    if not _wait_for_checked(target, checked):
        raise RuntimeError(f'checkbox did not reach recorded state {checked}')


def wait_for_application_completion(page, application, completion):
    """Let an application's browser plane verify one replayed outcome."""
    hook = getattr(
        browser_plane(application), 'wait_for_replay_completion', None)
    if not callable(hook):
        raise RuntimeError(
            f'{application} does not support replay completion {completion!r}'
        )
    hook(page, completion)


def landed(page, application):
    """Whether the application actually has something to show here.

    A captured URL can name a document that a step earlier in THIS
    replay was meant to create — a Save the demonstration relied on to
    turn an unsaved draft into 'MAT-MR-2026-00001'. If that step did not
    truly succeed here (a validation error, a mis-clicked control), no
    such document exists locally and the application renders its generic
    'not found' page instead of silently failing — going there does not
    reproduce the recorded outcome, it lands on a dead page that nothing
    after it can act on. Each application phrases that page its own way,
    through a DEAD_PAGE_MARKERS tuple on its own browser plane — the
    same plane that answers wait_ready. One with no known phrasing is
    taken at its word.
    """
    markers = getattr(browser_plane(application), 'DEAD_PAGE_MARKERS', ())
    if not markers:
        return True
    try:
        body = page.locator('body').inner_text()
    except Exception:
        return True  # a page whose body cannot even be read is not this
    return not any(marker in body for marker in markers)


def retype(page, refill):
    """Retype a field's own last known value in place of a broken widget.

    A date picker is driven by clicks a replay cannot always reproduce
    (a month-forward arrow with no name or role at all) even though the
    field it fills is an ordinary text input the operator had already
    typed a real value into earlier, before clearing it to demonstrate
    the picker. Retyping that same value is not a guess: it is the last
    value this exact field is known to have legitimately held, and it
    reproduces the field's OUTCOME without depending on calendar UI a
    stray icon selector cannot drive on another machine.
    """
    if refill is None:
        return False
    selectors, target, value = refill
    try:
        field = locator(page, selectors, target, timeout=3.0)
        field.fill(value)
        return True
    except Exception:
        return False


def recover(page, url, application, desc, key, on_step, skipped, exc, refill=None):
    """Reproduce a failed gesture's outcome when the capture recorded one.

    A gesture can be unperformable in a browser other than the one that
    recorded it: a desk sidebar section the recording machine had left
    expanded is collapsed here, so the captured link is in the page but
    never visible, and the click waits out its timeout. The capture also
    stored where the page stood once the gesture had been performed, so
    when the replay stands somewhere else, going there reproduces this
    step's outcome — and every later action that assumed it keeps working
    instead of acting on whichever page the replay was stranded on.

    Only a gesture that MOVED the page can be recovered this way; one
    that failed in place is still a miss, counted and voiced as before —
    unless `refill` names a field whose own value this gesture was only
    ever an indirect way of setting (see `retype`).
    Arriving is not enough either — a captured document permalink is
    only ever as real as whatever earlier step was supposed to create
    it, so landing on the desk's own 'not found' page is treated as no
    recovery at all, not a quiet success over a page with nothing on it.
    """
    keys = (key,) if isinstance(key, str) else tuple(key)
    if not keys:
        raise ValueError('recovery requires at least one callback key')

    def emit(message):
        for callback_key in keys:
            on_step(callback_key, page, message)

    recovered = bool(url) and not same_place(page.url, url)
    if recovered:
        try:
            page.goto(url, wait_until='domcontentloaded')
            if application:
                wait_ready(page, application)
            # The gesture this replaces cost its own timeout, so the
            # actions after it are already overdue and pacing will fire
            # them back to back. Let the page finish arriving first: a
            # desk report fetches its rows and draws its table well
            # after the document is loaded.
            try:
                page.wait_for_load_state('networkidle', timeout=30000)
            except Exception:
                pass  # a page that keeps polling is still usable
            recovered = landed(page, application)
        except Exception:
            recovered = False
    if not recovered and retype(page, refill):
        emit(desc + ' (recovered by retyping its known value)')
        return
    if not recovered:
        # A grouped narration still represents one failed gesture, so count
        # one skip while updating every narration segment attached to it.
        skipped.append(keys[0])
        emit('skipped ' + desc + ': ' + str(exc)[:160])
        return
    emit(desc + ' (recovered by opening ' + url + ')')


def frame_key(url):
    """What identifies a frame across installs of its application.

    A document server serves its editor from a build-stamped asset path
    (/9.4.0-<build hash>/web-apps/...) that differs between two installs
    of the very same version. Its host also follows the task runtime, so
    neither the captured origin nor the build stamp identifies the frame.
    Everything after the stamp is the frame's real identity.
    """
    base = url.split('#', 1)[0].split('?', 1)[0]
    # Frame hosts are runtime-specific; compare only normalized paths.
    base = re.sub(r'^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]+', '', base)
    return re.sub(r'/\d+(?:\.\d+)*-[0-9a-f]{8,}/', '/', base)


def root(page, frame_url):
    if not frame_url or frame_url == page.url:
        return page
    # Playwright includes the top-level document in ``page.frames``.  Do not
    # let a relaxed child-frame match fall back to that document: applications
    # such as Roundcube serve both the inbox and its message preview from ``/``,
    # distinguished only by query parameters that legitimately change between
    # fixture restores.
    frames = [frame for frame in page.frames if frame != page.main_frame]
    for frame in frames:
        if frame.url == frame_url:
            return frame
    # Embedded editors mint per-session tokens into their iframe URL;
    # the same frame is recognizable by its query-less form.
    base = frame_url.split('#', 1)[0].split('?', 1)[0]
    for frame in frames:
        if frame.url.split('#', 1)[0].split('?', 1)[0] == base:
            return frame
    # ...and a build stamp into its asset path, which moves with the
    # install rather than with the workflow.
    key = frame_key(frame_url)
    for frame in frames:
        if frame_key(frame.url) == key:
            return frame
    raise RuntimeError(f'captured frame is not present: {frame_url}')
