"""Generic OS-input `ops` for the Codex record-and-replay adapter.

Codex captures real OS input (not CDP), so its demonstrations must be genuine
CGEvent gestures. Rather than hand-write a parallel OS driver per fixture, this
module turns ANY fixture's Playwright `*_ui.py` module into an OS-input ops:

    OSOps(gitlab_ui, OSActuator(activate))

`OSOps` runs each primitive with an `OSPageProxy` in place of the real page.
The proxy is deny-by-default: reads / waits / navigation pass straight through
to the real Playwright page, the actuation verbs (click / fill / press /
select_option) are rerouted to the actuator as CGEvent input, and anything
that would actuate over CDP without our knowing — an ElementHandle click,
`mouse.*` — raises `UnsupportedOverCGEvent` loudly rather than silently
running as a non-OS gesture. That strictness is the whole point: a silent CDP
click would make an "OS demo" quietly not one.

Retry is two-layered. Each mutating primitive is expected to carry its own
entry-guard (skip if already applied) and exit-verify (assert the effect) — see
gitlab_ui.assign_self / opsfix_ui._select_radio — which makes re-running it
safe. On top of that, `OSOps` re-runs a whole primitive when its exit-verify
times out (`TimeoutError` from a `wait_for_*`), since a CGEvent click can miss
when the window isn't frontmost. The entry-guard is what keeps that outer
re-run from double-applying a non-idempotent gesture.
"""
from __future__ import annotations

import time

from showAndTell.core.pwerrors import PlaywrightError, PWTimeout

# Conditions worth re-running a whole (entry-guarded) primitive for: a verify
# miss (TimeoutError), a navigation interrupted by a concurrent redirect —
# e.g. the post-login redirect racing the next goto — or a read landing right
# as a navigation destroys the page's JS context. All are transient.
_TRANSIENT_SUBSTRINGS = ("interrupted by another navigation",
                         "Execution context was destroyed")


def _is_transient(exc) -> bool:
    if isinstance(exc, PWTimeout):
        return True
    if isinstance(exc, PlaywrightError):
        return any(s in str(exc) for s in _TRANSIENT_SUBSTRINGS)
    return False


class UnsupportedOverCGEvent(RuntimeError):
    """Raised when code asks the proxy to actuate in a way that cannot be
    reproduced as real OS input (so it would silently fall back to CDP)."""


def _select_text(loc, value=None, label=None, **kw):
    """Resolve what the actuator should type into the native select popup —
    the popup's type-ahead matches VISIBLE text, so a bare value is resolved
    to its option's label via the real locator. Forms with no faithful
    CGEvent mapping (index=/element=, multi-select lists) fail loudly."""
    kw.pop("timeout", None)  # harmless Playwright kwarg
    if kw or isinstance(value, (list, tuple)) or isinstance(label, (list, tuple)):
        raise UnsupportedOverCGEvent(
            "select_option over OS input supports a single value= or label= "
            f"only (got value={value!r}, label={label!r}, extra={sorted(kw)})")
    if label is not None:
        return label
    if value is None:
        raise UnsupportedOverCGEvent("select_option needs a value or a label")
    resolved = loc.evaluate(
        "(el, v) => { const o = Array.from(el.options || [])"
        ".find(o => o.value === v); return o ? o.label : null; }", value)
    if not resolved:
        raise UnsupportedOverCGEvent(
            f"select_option: no option with value {value!r} to resolve a "
            "visible label from")
    return resolved


# Verbs that mutate the page. Routed ones become CGEvent input; denied ones
# have no faithful CGEvent equivalent yet and must fail loudly, never pass
# through to CDP.
_ROUTED = frozenset({"click", "fill", "press", "select_option"})
_PAGE_DENIED = frozenset({
    "set_input_files", "check", "uncheck", "set_checked", "tap", "dblclick",
    "drag_and_drop", "hover", "type", "mouse", "keyboard", "touchscreen",
})
_LOCATOR_DENIED = frozenset({
    "set_input_files", "check", "uncheck", "set_checked",
    "tap", "dblclick", "drag_to", "hover", "type",
})
_HANDLE_DENIED = frozenset({
    "click", "fill", "type", "press", "select_option", "set_input_files",
    "check", "uncheck", "set_checked", "tap", "dblclick", "hover",
})
# Methods that return a Locator (or list of them) and so must stay proxied.
_LOCATOR_FACTORIES = frozenset({
    "locator", "get_by_role", "get_by_text", "get_by_label", "get_by_test_id",
    "get_by_placeholder", "get_by_alt_text", "get_by_title", "filter", "nth",
})


class _GuardedHandle:
    """Wraps an ElementHandle so reads pass through but any actuation raises —
    ui modules use query_selector for reads, and a stray handle.click() would
    otherwise run over CDP unnoticed."""

    __slots__ = ("_h",)

    def __init__(self, handle):
        object.__setattr__(self, "_h", handle)

    def query_selector(self, *a, **k):
        h = self._h.query_selector(*a, **k)
        return _GuardedHandle(h) if h is not None else None

    def query_selector_all(self, *a, **k):
        return [_GuardedHandle(h) for h in self._h.query_selector_all(*a, **k)]

    def __getattr__(self, name):
        if name in _HANDLE_DENIED:
            raise UnsupportedOverCGEvent(
                f"ElementHandle.{name}() cannot be issued as OS input; locate "
                f"with page.locator(...) so the actuator can click by coordinate")
        return getattr(self._h, name)


class OSLocatorProxy:
    """Wraps a Locator: actuation routes to the actuator, locator-returning
    methods stay proxied, reads pass through."""

    __slots__ = ("_loc", "_page", "_act")

    def __init__(self, locator, page, actuator):
        self._loc = locator
        self._page = page
        self._act = actuator

    def __repr__(self):
        return f"OSLocatorProxy({self._loc!r})"

    def click(self, **kw):
        # Bringing Chrome frontmost and issuing a CGEvent are separate macOS
        # operations. The first click can therefore move the pointer correctly
        # yet be swallowed by activation or an overlay. Arm a capture-phase
        # listener on the intended element, then accept the gesture only when
        # the trusted OS click actually reached it. This is intentionally
        # application-agnostic: tabs, buttons, links, and fields all use the
        # same delivery contract.
        for _ in range(4):
            token = f"showAndTell-{time.monotonic_ns()}"
            if not self._arm_click_probe(token):
                # Instrumentation is a verification aid, never a reason to
                # replace a genuine OS gesture with browser-side actuation.
                self._act.click(
                    self._page, self._loc, position=kw.get("position"))
                return
            try:
                self._act.click(
                    self._page, self._loc, position=kw.get("position"))
            except Exception:
                self._finish_click_probe(token)
                raise
            try:
                if self._finish_click_probe(token):
                    return
            except PlaywrightError:
                # A delivered click can synchronously navigate or replace its
                # target, making the old locator unreadable. That transition is
                # itself sufficient evidence that the gesture went through.
                return
        raise RuntimeError(
            "OS click did not reach its target after four attempts")

    def _arm_click_probe(self, token: str) -> bool:
        try:
            return bool(self._loc.evaluate(
                "(el, token) => {"
                "  const doc = el.ownerDocument;"
                "  const probes = doc.__showAndTellClickProbes ||= {};"
                "  const record = {seen: false};"
                "  const handler = event => {"
                "    if (event.isTrusted && event.composedPath().includes(el))"
                "      record.seen = true;"
                "  };"
                "  doc.addEventListener('click', handler, true);"
                "  probes[token] = {record, handler};"
                "  return true;"
                "}", token))
        except Exception:
            return False

    def _finish_click_probe(self, token: str) -> bool:
        # The recorder's input interceptor can delay trusted-click delivery
        # by a beat; reading the probe exactly once right after the gesture
        # then re-clicking posts a duplicate click that Teach records as its
        # own step. Poll for late delivery before giving up and cleaning up.
        for _ in range(8):
            if bool(self._loc.evaluate(
                    "(el, token) => {"
                    "  const probes = el.ownerDocument.__showAndTellClickProbes"
                    "    || {};"
                    "  const probe = probes[token];"
                    "  return !!probe && probe.record.seen;"
                    "}", token)):
                self._cleanup_click_probe(token)
                return True
            time.sleep(0.15)
        self._cleanup_click_probe(token)
        return False

    def _cleanup_click_probe(self, token: str) -> None:
        try:
            self._loc.evaluate(
                "(el, token) => {"
                "  const doc = el.ownerDocument;"
                "  const probes = doc.__showAndTellClickProbes || {};"
                "  const probe = probes[token];"
                "  if (!probe) return;"
                "  doc.removeEventListener('click', probe.handler, true);"
                "  delete probes[token];"
                "}", token)
        except Exception:
            pass

    def fill(self, text, **kw):
        self._act.fill(self._page, self._loc, text)

    def press(self, key, **kw):
        self._act.press(self._page, key)

    def select_option(self, value=None, *, label=None, **kw):
        self._act.select(self._page, self._loc,
                         _select_text(self._loc, value, label, **kw))

    def drag_path_to(self, destination, *, source_position=None,
                     target_position=None, path=None, drag_mode=None):
        if drag_mode != "pointer" and not isinstance(destination, OSLocatorProxy):
            raise UnsupportedOverCGEvent(
                "drag destination must remain an OS-input locator")
        self._act.drag(
            self._page, self._loc,
            destination._loc if isinstance(destination, OSLocatorProxy) else None,
            source_position=source_position, target_position=target_position,
            path=path or [], drag_mode=drag_mode,
        )

    @property
    def first(self):
        return OSLocatorProxy(self._loc.first, self._page, self._act)

    @property
    def last(self):
        return OSLocatorProxy(self._loc.last, self._page, self._act)

    def __getattr__(self, name):
        if name in _LOCATOR_DENIED:
            raise UnsupportedOverCGEvent(
                f"Locator.{name}() has no faithful CGEvent form; it would run "
                f"over CDP")
        attr = getattr(self._loc, name)
        if name in _LOCATOR_FACTORIES and callable(attr):
            def wrapped(*a, **k):
                return OSLocatorProxy(attr(*a, **k), self._page, self._act)
            return wrapped
        return attr


class OSPageProxy:
    """Wraps a Playwright Page. Deny-by-default: actuation routes to the
    actuator or raises; everything else (goto, waits, query_selector, evaluate,
    reads) passes through to the real page."""

    __slots__ = ("_page", "_act")

    def __init__(self, page, actuator):
        self._page = page
        self._act = actuator

    # -- routed actuation ----------------------------------------------------
    def click(self, selector, **kw):
        self._act.click(
            self._page, self._page.locator(selector).first,
            position=kw.get("position"))

    def fill(self, selector, text, **kw):
        self._act.fill(self._page, self._page.locator(selector).first, text)

    def press(self, selector, key, **kw):
        self._act.click(self._page, self._page.locator(selector).first)
        self._act.press(self._page, key)

    def select_option(self, selector, value=None, *, label=None, **kw):
        loc = self._page.locator(selector).first
        self._act.select(self._page, loc, _select_text(loc, value, label, **kw))

    # -- locator factories stay proxied --------------------------------------
    def locator(self, *a, **k):
        return OSLocatorProxy(self._page.locator(*a, **k), self._page, self._act)

    def get_by_role(self, *a, **k):
        return OSLocatorProxy(self._page.get_by_role(*a, **k), self._page, self._act)

    def get_by_text(self, *a, **k):
        return OSLocatorProxy(self._page.get_by_text(*a, **k), self._page, self._act)

    def get_by_label(self, *a, **k):
        return OSLocatorProxy(self._page.get_by_label(*a, **k), self._page, self._act)

    def get_by_test_id(self, *a, **k):
        return OSLocatorProxy(self._page.get_by_test_id(*a, **k), self._page, self._act)

    def get_by_placeholder(self, *a, **k):
        return OSLocatorProxy(self._page.get_by_placeholder(*a, **k), self._page, self._act)

    # -- handles are read-only wrappers --------------------------------------
    def query_selector(self, *a, **k):
        h = self._page.query_selector(*a, **k)
        return _GuardedHandle(h) if h is not None else None

    def query_selector_all(self, *a, **k):
        return [_GuardedHandle(h) for h in self._page.query_selector_all(*a, **k)]

    # -- deny-by-default: reads/nav pass through, actuation raises -----------
    def __getattr__(self, name):
        if name in _PAGE_DENIED:
            raise UnsupportedOverCGEvent(
                f"page.{name} cannot be issued as OS input; it would run over "
                f"CDP. Add a routed actuator or a per-fixture override.")
        return getattr(self._page, name)


class OSOps:
    """Generic OS-input ops over a Playwright `*_ui.py` module.

    `OSOps(gitlab_ui, actuator).add_label(page, "bug")` runs
    `gitlab_ui.add_label` with an OSPageProxy, retrying the whole primitive if
    its exit-verify times out. The primitive's own entry-guard keeps that
    re-run safe.
    """

    def __init__(self, ui, actuator, *, tries: int = 3, settle_ms: int = 800):
        self._ui = ui
        self._act = actuator
        self._tries = tries
        self._settle_ms = settle_ms

    def __getattr__(self, name):
        target = getattr(self._ui, name)
        if not callable(target):
            return target

        def wrapped(page, *args, **kwargs):
            proxy = OSPageProxy(page, self._act)
            last = None
            for _ in range(self._tries):
                try:
                    return target(proxy, *args, **kwargs)
                except PlaywrightError as exc:
                    if not _is_transient(exc):
                        raise
                    last = exc
                    self._act.reactivate(page)
                    page.wait_for_timeout(self._settle_ms)
            raise last

        wrapped.__name__ = name
        return wrapped
