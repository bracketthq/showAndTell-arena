"""Brackett Show and Tell: drive the Brackett web UI (new agent → Show and
Tell → demo → Stop) while the extension records, then capture the produced
agent/workflow. BrackettAdapter plugs this machinery into the shared run loop
in ``teacher.run``; task-agnostic via the same fixture registry + demonstrate
driver as the other products."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from showAndTell.applications.browser.runtime import origin
from showAndTell.core import chrome
from showAndTell.core.chrome import cdp_eval, kill_managed_chrome, window_bounds
from showAndTell.core.llm import reply_has_answer

from .base import ProductAdapter

BRACKETT_REPLY_TIMEOUT_SECONDS = 600


ADOPT_APPLICATION_TABS_JS = """
(async () => {
  const wantedOrigins = %s;
  const originOf = url => {
    try { return new URL(url || '').origin; }
    catch (_) { return ''; }
  };
  const groups = await chrome.tabGroups.query({});
  const named = groups.filter(group => group.title === 'Brackett');
  const group = named.length === 1 ? named[0]
    : (named.length === 0 && groups.length === 1 ? groups[0] : null);
  if (!group)
    return {error: `expected one Brackett recording group; found ${named.length}`};

  const adopt = async tab => {
    if (!tab || tab.id === undefined || tab.windowId !== group.windowId) return false;
    if (!wantedOrigins.includes(originOf(tab.url || tab.pendingUrl))) return false;
    if (tab.groupId !== group.id) {
      let lastError = null;
      for (const delay of [0, 150, 300, 600, 1000]) {
        if (delay) await new Promise(resolve => setTimeout(resolve, delay));
        try {
          await chrome.tabs.group({groupId: group.id, tabIds: [tab.id]});
          lastError = null;
          break;
        } catch (error) {
          lastError = String(error);
        }
      }
      if (lastError) throw new Error(lastError);
    }
    return true;
  };

  globalThis.__showAndTellBrackettOrigins = wantedOrigins;
  globalThis.__showAndTellBrackettGroupId = group.id;
  globalThis.__showAndTellBrackettAdopt = adopt;
  if (!globalThis.__showAndTellBrackettTabAdopter) {
    globalThis.__showAndTellBrackettTabAdopter = true;
    chrome.tabs.onCreated.addListener(tab => { void globalThis.__showAndTellBrackettAdopt(tab); });
    chrome.tabs.onUpdated.addListener((id, info, tab) => {
      if (info.url) void globalThis.__showAndTellBrackettAdopt(tab);
    });
  }

  const tabs = await chrome.tabs.query({windowId: group.windowId});
  const matching = tabs.filter(tab => wantedOrigins.includes(
    originOf(tab.url || tab.pendingUrl)));
  const added = matching.filter(tab => tab.groupId !== group.id).length;
  for (const tab of matching) await adopt(tab);

  const grouped = await chrome.tabs.query({groupId: group.id});
  const groupedOrigins = new Set(grouped.map(tab => originOf(tab.url || tab.pendingUrl)));
  const missing = wantedOrigins.filter(origin => !groupedOrigins.has(origin));
  return {groupId: group.id, matched: matching.length, added, missing};
})()
"""


def brackett_url() -> str:
    url = os.environ.get("SHOWANDTELL_BRACKETT_URL", "").strip()
    if not url:
        raise RuntimeError(
            "Set SHOWANDTELL_BRACKETT_URL to your Brackett workspace URL "
            "before launching the Brackett adapter.")
    return url


def _visible(locator) -> bool:
    """Whether a locator resolves to at least one visible element, in one
    Playwright round trip: ``first`` answers False for zero matches and for
    the first of many, and a detached handle or closed page raises — which
    every caller here means as "not visible"."""
    try:
        return locator.first.is_visible()
    except Exception:
        return False


# The current product contract keeps the agent shortcut and teach-entry id
# stable. The picker id is legacy-only: current builds start directly, but
# retaining it lets older deployed builds continue to work.
_TEACH_ENTRY_TESTID = "snt-trigger"
_THIS_BROWSER_TESTID = "snt-picker-this-browser"


def _teach_entry_locators(page):
    """Ordered candidates for the teach entry point: the stable test id the
    product guarantees, then the label/role shapes of builds deployed before
    the contract existed."""
    return (
        page.get_by_test_id(_TEACH_ENTRY_TESTID),
        page.get_by_role("button", name="Show me how you do it", exact=False),
        page.get_by_role("button", name="Show and Tell"),
    )


def _authenticated_home_locators(page):
    """Positive markers that Brackett's signed-in home has finished loading.

    The absence of a Sign in button is not sufficient: that button appears
    only after client hydration and disappears while the identity redirect is
    in flight.  These controls belong to the authenticated workspace shell.
    """
    return (
        page.get_by_role("tab", name="Your team", exact=False),
        page.get_by_role("button", name="New agent", exact=True),
        page.get_by_text("New Agent", exact=False),
    )


def _authenticated_brackett_page(page):
    """Return the signed-in Brackett page, including a login-created tab.

    The production identity flow can finish by opening the workspace in a new
    tab while leaving the original Auth0 tab open.  Playwright keeps ``page``
    attached to that original tab, so checking only it leaves the run at the
    sign-in gate even though authentication succeeded visibly.
    """
    try:
        siblings = list(page.context.pages)
    except Exception:
        siblings = []
    candidates = [page, *(candidate for candidate in siblings
                           if candidate is not page)]
    wanted_host = urlsplit(brackett_url()).netloc
    for candidate in candidates:
        try:
            if urlsplit(str(candidate.url)).netloc != wanted_host:
                continue
        except Exception:
            continue
        if any(_visible(locator)
               for locator in _authenticated_home_locators(candidate)):
            return candidate
    return None


def _sign_in_targets(page):
    """Every (label, locator) pair that could be Brackett's sign-in control."""
    for label in ("Sign in", "Log in"):
        yield label, page.get_by_role("button", name=label, exact=True)
        yield label, page.get_by_text(label, exact=True)


def _brackett_sign_in_required(page) -> bool:
    """Whether the current Brackett page is visibly asking for authentication."""
    if "/login" in str(page.url).casefold():
        return True
    return any(_visible(locator) for _label, locator in _sign_in_targets(page))


def _click_brackett_sign_in(page, out) -> bool:
    """Open Brackett's login flow, allowing a saved session to complete it."""
    for label, locator in _sign_in_targets(page):
        if not _visible(locator):
            continue
        try:
            locator.first.click(timeout=5000)
        except Exception:
            continue
        out(f"  clicked '{label}' automatically; checking for a saved login…")
        return True
    return False


# Seconds an authenticated page may sit without the protected marker before
# the gate re-points it at the marker's own route. Long enough for normal
# hydration, short enough that an onboarding or landing redirect that swallowed
# the marker costs seconds, not the whole budget.
_REVISIT_AFTER_S = 20.0


from showAndTell.students import readiness


def _wait_for_brackett_sign_in(
        page, out, timeout_s: int = 300, ready=None, revisit_url=None) -> None:
    """Wait for authentication or an explicit protected-page ready marker.

    When ``ready`` is supplied, an initially absent Sign in button is not proof
    of authentication: Brackett hydrates that button several seconds after
    navigation. The run proceeds only when the protected UI itself is ready.

    When ``revisit_url`` is also supplied, an authenticated page that still
    lacks the marker is periodically re-pointed at that route: a first login
    can land on onboarding or a workspace redirect where the marker never
    appears. Only same-origin pages are steered — a user mid-login on an
    external identity provider must never have the page yanked away.
    """
    def ready_now() -> bool:
        if ready is None:
            return False
        try:
            return bool(ready())
        except Exception:
            return False

    if ready_now():
        return
    if ready is None and not _brackett_sign_in_required(page):
        return

    started = time.time()
    deadline = started + timeout_s
    clicked = False
    action_prompted = False
    authenticated_reads = 0
    last_revisit = started
    # published lazily: a healthy profile must never flash a sign-in gate
    # while the first ready check is still settling
    publication = readiness.GatePublication(
        "Brackett is not signed in",
        "Complete login in the managed Chrome window; the run continues "
        "automatically once you are signed in.",
        open_url=revisit_url or brackett_url(),
        open_label="Open Brackett login")
    published = False
    try:
      while time.time() < deadline:
        if published and publication.cancelled():
            raise readiness.ReadinessCancelled(
                "cancelled at readiness gate: Brackett is not signed in")
        continued = published and publication.continued()
        if ready_now():
            out("  ✓ Brackett authentication complete — continuing the run")
            return

        sign_in_required = _brackett_sign_in_required(page)
        if continued:
            out("  Continue received — checking the Brackett sign-in now…")
            if sign_in_required:
                # The previous login attempt may have returned to the same
                # button. A deliberate Continue is also permission to retry it.
                clicked = _click_brackett_sign_in(page, out) or clicked
            elif (revisit_url and ready is not None
                  and urlsplit(str(page.url)).netloc
                  == urlsplit(revisit_url).netloc):
                # Login commonly lands on a workspace/onboarding route. Do
                # the protected-route check immediately instead of making the
                # Continue button appear inert for _REVISIT_AFTER_S seconds.
                out(f"  revisiting {revisit_url} to verify the signed-in workspace")
                try:
                    page.goto(revisit_url)
                except Exception:
                    pass
                last_revisit = time.time()
            elif (ready is None
                  and urlsplit(str(page.url)).netloc
                  == urlsplit(brackett_url()).netloc):
                out("  ✓ Brackett sign-in confirmed — continuing the run")
                return
        if sign_in_required and not clicked:
            clicked = _click_brackett_sign_in(page, out)

        if (revisit_url and ready is not None and not sign_in_required
                and time.time() - last_revisit >= _REVISIT_AFTER_S
                and urlsplit(str(page.url)).netloc == urlsplit(revisit_url).netloc):
            out(f"  authenticated, but the expected Brackett page hasn't appeared — "
                f"revisiting {revisit_url}")
            try:
                page.goto(revisit_url)
            except Exception:
                pass
            last_revisit = time.time()

        # Callers without a protected-page marker retain the lightweight check
        # used by browser setup: two stable reads after the login gate closes.
        if ready is None and clicked:
            authenticated_reads = 0 if sign_in_required else authenticated_reads + 1
            if authenticated_reads >= 2:
                out("  ✓ saved Brackett sign-in restored — continuing the run")
                return

        if sign_in_required and not published:
            publication.__enter__()
            published = True
        prompt_after = 5 if clicked else 10
        if not action_prompted and time.time() - started >= prompt_after:
            out("  ACTION REQUIRED: Brackett is installed but not signed in.")
            out("  Complete login in the managed Chrome window;")
            out(f"  this run will continue automatically (waiting up to {timeout_s // 60} minutes).")
            action_prompted = True
        time.sleep(0.5)
    finally:
        if published:
            publication.__exit__(None, None, None)

    raise RuntimeError(
        f"Brackett sign-in or protected page readiness did not complete at "
        f"{page.url}. Run `./showAndTell browser launch`, sign in, close the "
        "managed browser, and retry.")


def _launch_with_extension(
        cdp_port: int, profile_root: Path = chrome.MANAGED_ROOT) -> None:
    kill_managed_chrome(profile_root)
    if not chrome.brackett_extension_installed(profile_root):
        raise RuntimeError(
            "Brackett is not installed in this managed Chrome profile")
    chrome.launch(user_data_dir=profile_root, port=cdp_port,
                  extra_args=["--use-fake-ui-for-media-stream"])
    if not chrome.wait_for_cdp(cdp_port):
        raise RuntimeError("managed Chrome did not come up")


def _brackett_service_worker(cdp_port: int, timeout_s: float = 10.0) -> str:
    """Find Brackett's extension worker without assuming a Web Store ID."""
    deadline = time.time() + timeout_s
    while True:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{cdp_port}/json/list", timeout=2) as response:
                targets = json.load(response)
        except Exception:
            targets = []
        for target in targets:
            if (target.get("type") != "service_worker"
                    or not str(target.get("url", "")).startswith("chrome-extension://")):
                continue
            try:
                name = cdp_eval(
                    target["webSocketDebuggerUrl"],
                    "chrome.runtime.getManifest().name", timeout_s=3)
            except Exception:
                continue
            if str(name).casefold() == "brackett":
                return target["webSocketDebuggerUrl"]
        if time.time() >= deadline:
            raise RuntimeError(
                "Brackett extension service worker not found after recording started")
        time.sleep(0.5)


def _adopt_application_tabs(cdp_port: int, urls: list[str], out=print, *,
                            require_all: bool = True) -> None:
    """Put every demonstrated application tab in Brackett's recording group.

    Brackett automatically groups tabs opened by a grouped tab. Playwright/CDP
    tabs have no opener, so multi-application replays must adopt them explicitly
    or Brackett records only its fresh blank tab.
    """
    origins = sorted({
        origin(parsed) for url in urls
        if (parsed := urlsplit(url)).scheme and parsed.netloc
    })
    if not origins:
        raise RuntimeError("cannot identify application origins for Brackett grouping")
    worker = _brackett_service_worker(cdp_port)
    result = cdp_eval(
        worker, ADOPT_APPLICATION_TABS_JS % json.dumps(origins), timeout_s=30) or {}
    if result.get("error"):
        raise RuntimeError(
            f"cannot adopt application tabs into Brackett's recording group: "
            f"{result['error']}")
    if require_all and result.get("missing"):
        raise RuntimeError(
            "Brackett recording group is missing application origin(s): "
            + ", ".join(result["missing"]))
    suffix = "" if require_all else " (late tabs will be adopted on open)"
    out(f"  ✓ adopted {result.get('matched', 0)} application tab(s) into "
        f"the Brackett recording group{suffix}")


def _create_agent(page, name: str, out):
    agents_url = brackett_url().rstrip("/") + "/agents"
    page.goto(agents_url)
    authenticated_page = None

    def authenticated_agents_ready() -> bool:
        nonlocal authenticated_page
        authenticated_page = _authenticated_brackett_page(page)
        return authenticated_page is not None

    # The cluster root may redirect to a dashboard that has no agent controls.
    # Use the agent workspace as the protected readiness route, then leave
    # creation itself to the stable /new-agent shortcut below.
    _wait_for_brackett_sign_in(
        page, out, timeout_s=900, ready=authenticated_agents_ready,
        revisit_url=agents_url)
    page = authenticated_page or page

    # Brackett's stable automation shortcut: /new-agent?name= always creates
    # a fresh agent and lands in its chat, whatever the product's interactive
    # creation flow looks like this week. Everything downstream of the URL may
    # churn; the shortcut absorbs it.
    shortcut_url = (brackett_url().rstrip("/") + "/new-agent?name="
                    + quote(f"ShowAndTell {name}"))
    page.goto(shortcut_url)

    def agent_chat_ready() -> bool:
        return any(_visible(loc) for loc in _teach_entry_locators(page))

    page.wait_for_url("**/agents/**", timeout=30000)
    deadline = time.time() + 30
    while not agent_chat_ready():
        if time.time() > deadline:
            raise RuntimeError(
                f"the /new-agent shortcut landed at {page.url}, but no "
                f"Show-and-Tell entry point appeared")
        time.sleep(0.5)
    out("  Brackett created the agent via the /new-agent shortcut")
    time.sleep(2)
    return page


def _handle_show_and_tell_onboarding(page, out) -> str | None:
    """Dismiss Brackett's intermittent first-session teaching modal.

    The modal appears after starting Show and Tell and prevents the normal
    in-progress marker from rendering. Prefer its persistent skip action so
    the managed profile does not see it again; use Start Session when that
    action is unavailable in a UI variant. Some deployed variants render the
    action before (or without) the guide copy, so the controls are also used
    as positive modal markers.
    """
    marker = page.get_by_text(
        "Drag a tab into the Brackett group", exact=False)
    skip_candidates = [
        page.get_by_text("Skip & don't show again", exact=True),
        page.get_by_text("Skip & don’t show again", exact=True),
    ]
    start_candidates = [
        page.get_by_role("button", name="Start Session"),
        page.get_by_text("Start Session", exact=True),
    ]
    marker_visible = _visible(marker)
    visible_control = any(
        _visible(control) for control in (*skip_candidates, *start_candidates))
    if not marker_visible and not visible_control:
        return None

    out("  Brackett first-session guide appeared — dismissing it…")
    click_failed = False
    for skip in skip_candidates:
        if not _visible(skip):
            continue
        try:
            skip.first.click(timeout=5000)
        except Exception:
            click_failed = True
            continue
        out("  skipped Brackett's first-session guide permanently")
        time.sleep(1)
        return "skipped"

    for start in start_candidates:
        if not _visible(start):
            continue
        try:
            start.first.click(timeout=5000)
        except Exception:
            click_failed = True
            continue
        out("  started the Brackett session from its first-session guide")
        time.sleep(1)
        return "started"

    if click_failed:
        # React can replace the modal between is_visible() and click(). The
        # outer recording-start loop will resolve fresh locators and retry.
        out("  Brackett's first-session guide is still settling — retrying…")
        return "pending"

    raise RuntimeError(
        "Brackett's first-session guide is blocking Show and Tell, but its "
        "Skip and Start Session controls were not found")


def _this_browser_locators(page):
    """Legacy source-picker candidates, retained for older Brackett builds."""
    return (
        page.get_by_test_id(_THIS_BROWSER_TESTID),
        page.get_by_role("radio", name=re.compile("this browser", re.I)),
        page.get_by_role("button", name=re.compile("this browser", re.I)),
        page.locator('[role="radio"]', has_text="This browser"),
        page.get_by_text("This browser", exact=False),
    )


def _legacy_this_browser_picker_visible(page) -> bool:
    try:
        return any(_visible(locator) for locator in _this_browser_locators(page))
    except Exception:
        return False


def _choose_this_browser(page) -> None:
    """Click an older build's 'This browser' choice, preferring its test id.

    Some picker UIs render the phrase twice — a muted typography label that
    is never enabled, and the radio card that actually starts capture — so
    bare text can resolve to the label and time out forever. Prefer the card
    by role; fall back to raw text only when no interactive match exists.
    """
    last_error = None
    for locator in _this_browser_locators(page):
        first = locator.first
        if not _visible(first):
            continue
        try:
            first.click(timeout=5000)
            return
        except Exception as exc:
            last_error = exc
    raise last_error or RuntimeError("no clickable 'This browser' choice found")


def _dump_this_browser_candidates(page, out) -> None:
    """Name every 'This browser' match so picker-UI drift reads as evidence,
    not a bare timeout."""
    try:
        rows = page.evaluate(
            """() => [...document.querySelectorAll('*')]
                .filter(e => e.children.length === 0
                    && (e.innerText || '').includes('This browser'))
                .map(e => {
                    const i = e.closest('[role],button,label,a') || e;
                    return {tag: i.tagName, role: i.getAttribute('role'),
                            ariaDisabled: i.getAttribute('aria-disabled'),
                            cls: (i.className || '').slice(0, 60)};
                })""")
        out(f"  'This browser' matches at failure: {rows}")
    except Exception:
        pass


def _wait_for_recording_start(page, out, timeout_s: float = 60) -> None:
    """Wait for Brackett's positive recording acknowledgement.

    Current Brackett builds start the browser session directly after Show and
    Tell is clicked. Older builds first render a ``This browser`` picker; keep
    that as an optional compatibility path, never as a prerequisite. A
    transient extension/API failure leaves a ``Try again`` control; drive that
    explicit retry while still requiring the positive in-progress marker
    before the demonstrated application is touched.
    """
    progress = page.get_by_text("Show and Tell in progress", exact=False).first
    retry = page.get_by_role("button", name="Try Again", exact=True)
    deadline = time.time() + timeout_s
    retry_count = 0
    next_retry_at = 0.0

    while time.time() < deadline:
        if _visible(progress):
            return

        onboarding = _handle_show_and_tell_onboarding(page, out)
        if onboarding == "skipped":
            # Skip has two deployed meanings: continue directly, or dismiss
            # back to the legacy picker. Only reselect when it actually exists.
            if _legacy_this_browser_picker_visible(page):
                out("  first-session guide dismissed — choosing This browser again…")
                _choose_this_browser(page)
        if onboarding:
            time.sleep(0.5)
            continue

        if _legacy_this_browser_picker_visible(page):
            out("  legacy source picker appeared — choosing This browser…")
            try:
                _choose_this_browser(page)
            except Exception:
                # Streaming can replace the card between visibility and click;
                # resolve fresh locators on the next loop iteration.
                time.sleep(0.5)
            continue

        now = time.time()
        if _visible(retry) and now >= next_retry_at:
            if retry_count >= 3:
                break
            retry_count += 1
            out(f"  Brackett did not start recording — retrying "
                f"the extension handshake ({retry_count}/3)…")
            retry.first.click(timeout=5000)
            # The retry is async. Do not double-submit while React is moving
            # through isStarting and the extension is answering.
            next_retry_at = now + 8
        time.sleep(0.5)

    _dump_this_browser_candidates(page, out)
    try:
        body = " ".join(page.locator("body").inner_text().split())
        out(f"  Brackett state at recording-start failure: {body[-800:]}")
    except Exception:
        pass
    raise RuntimeError(
        "Brackett never acknowledged that Show and Tell recording started")


def _start_show_and_tell(page, out) -> None:
    for entry in _teach_entry_locators(page):
        if _visible(entry):
            entry.first.click(timeout=5000)
            break
    else:
        raise RuntimeError("no visible Show-and-Tell entry point")
    # Recording is live only when the in-progress indicator shows. Do NOT key
    # off a generic "Stop" button — "Stop generating" is present while thinking.
    _wait_for_recording_start(page, out)
    out("  recording started")


def _stop_show_and_tell(page, out) -> None:
    stop = page.get_by_role("button", name="Stop", exact=True)
    progress = page.get_by_text("Show and Tell in progress", exact=False)

    if _visible(stop):
        try:
            # Brackett can finish the recording and replace this control while
            # replay cleanup is still running, so do not inherit Playwright's
            # 30-second default timeout here.
            stop.first.click(timeout=5000)
            time.sleep(2)
            return
        except Exception as exc:
            if not _visible(progress):
                out("  Show-and-Tell had already stopped; continuing with analysis")
                return
            raise RuntimeError(
                "Show-and-Tell is still in progress, but its Stop button could not be clicked"
            ) from exc

    if not _visible(progress):
        out("  Show-and-Tell had already stopped; continuing with analysis")
        return
    raise RuntimeError(
        "Show-and-Tell is still in progress, but Brackett has no visible Stop button"
    )


def _brackett_ask(page, msg: str, out) -> str:
    """Send the quiz to the Brackett agent and return the page once its answer has
    landed. After Show and Tell, Brackett sits in a workflow-sketch UI; a generic
    'Thinking gone + page stable' check can return on that UI *before* the chat
    answer streams in — capturing no 'A1:' answer (the intermittent all-blank
    quiz). So poll until Brackett's reply actually carries the answer marker and
    has settled; fall back to the current page only on timeout."""
    # The composer refuses the send while the agent is still generating (seen
    # live: the quiz prompt sits unsent while the reply streams) — wait for
    # idle BEFORE sending, then verify the message actually left the box.
    idle_deadline = time.time() + 150
    last_body, settled = "", 0
    while time.time() < idle_deadline:
        body = page.locator("body").inner_text()
        thinking = page.get_by_text("Thinking", exact=False).count() > 0
        if not thinking and body == last_body:
            settled += 1
            if settled >= 2:
                break
        else:
            settled = 0
        last_body = body
        time.sleep(2)

    box = None
    for loc in (page.get_by_placeholder("Send a message", exact=False),
                page.get_by_role("textbox")):
        try:
            if loc.first.count():
                box = loc.first
                break
        except Exception:
            pass
    box = box or page.get_by_role("textbox").first
    tail = msg[-45:]  # comprehend echoes the prompt; the reply is the text after it
    answer_markers = re.findall(r"\bA\d+:\s*<ans>", msg, flags=re.I)
    final_marker = (answer_markers[-1].split("<", 1)[0].strip()
                    if answer_markers else "A1:")

    def _composer_text() -> str:
        # Sent = the composer emptied. (Checking the page body is a trap: the
        # UNSENT draft is part of the body text, so it false-verifies.)
        for probe in ("input_value", "inner_text"):
            try:
                return (getattr(box, probe)() or "").strip()
            except Exception:
                continue
        return ""

    for _ in range(4):
        box.click()
        box.fill(msg)
        page.keyboard.press("Enter")
        time.sleep(2)
        if not _composer_text():
            break
        # Enter didn't send (this composer treats it as newline / ignores it
        # mid-generation) — click the send control instead.
        for btn in (page.get_by_role("button", name="Send"),
                    page.locator("[aria-label*='Send' i]"),
                    page.locator("form button").last):
            try:
                if btn.count():
                    btn.first.click(timeout=2000)
                    break
            except Exception:
                continue
        time.sleep(2)
        if not _composer_text():
            break
        out("  (send did not take — agent may be busy; retrying)")
        time.sleep(4)

    # Complex recorded workflows can spend several minutes re-reading the
    # trace before emitting their answer. Keep the chat alive long enough for
    # that delayed response and gate on the final answer marker, not on stale
    # or hidden "Thinking" nodes left in the page.
    deadline = time.time() + BRACKETT_REPLY_TIMEOUT_SECONDS
    last, stable = "", 0
    while time.time() < deadline:
        body = page.locator("body").inner_text()
        if reply_has_answer(body, tail, final_marker) and body == last:
            stable += 1
            if stable >= 2:
                out("  reply captured")
                return body
        else:
            stable = 0
        last = body
        time.sleep(4)
    out("  (reply not captured within timeout — returning current page)")
    return page.locator("body").inner_text()


def _skip_pending_questions(page, out, rounds: int = 3) -> None:
    """The current Brackett asks post-SNT clarifying questions ('A couple
    details to lock in') and leaves the workflow un-finalized until they are
    answered or skipped — quizzed in that state it refuses ('I can't answer
    those — no cutoff was ever established'). The bench's contract is that
    ONLY the demonstration + narration teach, so Skip rather than answer:
    what the demo left unsaid stays unsaid."""
    for _ in range(rounds):
        # The questions live in a side panel that may be closed — the chat
        # shows an 'Answer the questions' pill that opens it.
        opener = page.get_by_text("Answer the questions", exact=False)
        if opener.count():
            try:
                opener.first.click(timeout=2000)
                page.wait_for_timeout(1500)
            except Exception:
                pass
        skip = page.get_by_role("button", name="Skip")
        if skip.count() == 0:
            skip = page.get_by_text("Skip", exact=True)
        if skip.count() == 0:
            out("  (no clarifying-questions panel to skip)")
            return
        try:
            skip.first.click(timeout=3000)
            out("  skipped Brackett's clarifying questions (demo-only teaching)")
            page.wait_for_timeout(2500)
        except Exception as exc:
            out(f"  (skip click failed: {exc})")
            return


def _await_analysis(page, out, timeout_s: int = 300) -> None:
    """After Stop, Brackett processes the recording ('analyzing your actions')
    into a proposal/workflow. Wait until that processing text is gone and the
    chat has settled, so we capture the result rather than the spinner."""
    deadline = time.time() + timeout_s
    stable, last = 0, ""
    while time.time() < deadline:
        text = page.locator("body").inner_text()
        processing = ("Processing Show and Tell" in text
                      or "analyzing your actions" in text
                      or page.get_by_text("Thinking", exact=False).count() > 0)
        if not processing and text == last and text:
            stable += 1
            if stable >= 2:
                out("  analysis complete")
                return
        else:
            stable = 0
        last = text
        time.sleep(5)
    out("  (analysis still running at timeout — capturing current state)")


def _manual_demonstration(ctx, task_dir: Path, app_url: str, creds: dict,
                          out, wait_done=input, script_lines=()) -> None:
    """Hand the demonstration to the human: open a tab on the app, print the
    teleprompter, and block until they confirm they are finished."""
    demo_tab = ctx.new_page()
    demo_tab.goto(f"{app_url}/login")
    out("\n=== YOUR TURN — demonstrate in the new tab (narrate aloud) ===")
    out(f"  log in as {creds['email']} / {creds['password']}, then:")
    for line in script_lines:
        out(f"  - {line}")
    wait_done("Press Enter here when the demonstration is finished… ")
    demo_tab.close()


class BrackettAdapter(ProductAdapter):
    """Brackett Show and Tell: sign in, create an agent, record the
    demonstration in the extension's tab group, let Brackett analyze it."""

    name = "brackett"
    display = "Brackett"
    teach_label = "brackett-teach"

    def __init__(self) -> None:
        self.product_page = None

    def preflight(self, session) -> None:
        def extension_state():
            if chrome.brackett_extension_installed(session.profile_root):
                return None
            return ("the Brackett extension is not installed in the "
                    "managed Chrome profile")

        readiness.ensure(
            extension_state,
            "In the ShowAndTell managed Chrome profile — not your "
            "existing Chrome profile — click 'Add to Chrome' on the Brackett "
            "install page, sign in, and close that Chrome window. Then click "
            "'Continue' here. If you navigated away, click 'Install Brackett "
            "again' below.",
            session.out, live=True, open_url=chrome.BRACKETT_WEBSTORE_URL,
            open_label="Install Brackett")

    def launch_steps(self, session):
        return [("launching managed Chrome with the Brackett extension…",
                 lambda: self._launch(session)),
                ("verifying Brackett sign-in and creating a new agent…",
                 lambda: self._create_agent(session))]

    def _launch(self, session) -> None:
        # The shared runner selects VB-CABLE before Chrome launches. This must
        # happen before the extension enumerates microphones.
        _launch_with_extension(session.cdp_port, session.profile_root)
        if session.mode == "teach":
            # Chrome is a detached child process; without explicit cleanup its
            # CDP port remains occupied after a failed run and prevents retrying
            # with the managed profile.
            session.stack.callback(kill_managed_chrome, session.profile_root)
        session.connect_chrome()
        if session.mode == "trial":
            for existing in list(session.ctx.pages):
                existing.close()

    def _create_agent(self, session) -> None:
        self.product_page = session.ctx.new_page()
        if session.mode == "teach":
            cdp = session.ctx.new_cdp_session(self.product_page)
            window_id = cdp.send("Browser.getWindowForTarget")["windowId"]
            cdp.send("Browser.setWindowBounds",
                     {"windowId": window_id, "bounds": window_bounds()})
        self.product_page = _create_agent(
            self.product_page, session.subject_name, session.out)

    def arm_steps(self, session):
        if session.mode == "teach":
            return [("starting Show and Tell…",
                     lambda: _start_show_and_tell(self.product_page, session.out))]
        return [("starting Show and Tell and adopting the application tabs…",
                 lambda: self._arm_trial(session))]

    def _arm_trial(self, session) -> None:
        self.product_page.bring_to_front()
        _start_show_and_tell(self.product_page, session.out)
        application_urls = [surface["url"] for surface in session.capture["surfaces"]]
        # An automatic trial opens only the primary surface up front; supporting
        # pages open during generated setup and are adopted later by
        # verify_live_application_tabs.
        _adopt_application_tabs(session.cdp_port, application_urls, session.out,
                                require_all=False)
        session.page.bring_to_front()

    def demo_label(self, session) -> str:
        if session.mode == "teach":
            return ("handing the demonstration to you…" if session.manual
                    else "performing the demonstration…")
        return super().demo_label(session)

    @contextmanager
    def demo_stage(self, session):
        if session.mode == "teach":
            # Narration routing was entered before Chrome launched, so the
            # recording stream opens against the already-present virtual mic.
            demo_tab = session.ctx.new_page()
            try:
                yield demo_tab
            finally:
                demo_tab.close()
            return
        yield session.page

    def demo_kwargs(self, session) -> dict:
        if session.mode == "teach":
            return {}

        def verify_live_application_tabs() -> None:
            # Supporting surfaces can redirect across co-hosted ports
            # (ONLYOFFICE's connector on 8086 opens its editor on 8081).
            # Verify the pages that actually exist after the generated setup,
            # not the pre-redirect testcase URLs.
            live_urls = [
                page.url for page in session.ctx.pages
                if page != self.product_page
                and page.url.startswith(("http://", "https://"))
            ]
            _adopt_application_tabs(session.cdp_port, live_urls, session.out)
        return {"before_start": verify_live_application_tabs}

    def manual_teach(self, session) -> None:
        _manual_demonstration(
            session.ctx, session.source_dir, session.app_url,
            session.credentials, session.out, session.wait_done,
            script_lines=session.demo_script(),
        )

    def conclude_steps(self, session):
        return [("stopping Show and Tell and waiting for Brackett to analyze…",
                 lambda: self._conclude(session))]

    def _conclude(self, session) -> None:
        if session.mode == "trial":
            for page in list(session.ctx.pages):
                if page != self.product_page:
                    page.close()
        self.product_page.bring_to_front()
        _stop_show_and_tell(self.product_page, session.out)
        _await_analysis(self.product_page, session.out)
        _skip_pending_questions(self.product_page, session.out)
        (session.run_dir / "agent_url.txt").write_text(
            self.product_page.url, encoding="utf-8")
        (session.run_dir / "handoff.txt").write_text(
            self.product_page.locator("body").inner_text()[:16000], encoding="utf-8")
        self.product_page.screenshot(
            path=str(session.run_dir / "brackett.png"), full_page=True)

    def ask(self, session, message: str) -> str:
        return _brackett_ask(self.product_page, message, session.out)

    def trial_metadata(self, session) -> dict:
        return {"agent_url": self.product_page.url}
