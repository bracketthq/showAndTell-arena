"""Codex Record & Replay: drive the Codex desktop app (Chromium/CDP) to
screen-record a demonstration, perform the demo on screen in the managed
Chrome, then tell Codex "done recording" and capture what it inspected.
CodexAdapter plugs this machinery into the shared run loop in ``teacher.run``.

Unlike Claude/Brackett (which capture browser events), Codex screen-records,
so the demo must be the frontmost window while it runs. Task-agnostic via the
same fixture registry + demonstrate driver."""
from __future__ import annotations

import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

from showAndTell.core import chrome
from showAndTell.students import readiness
from showAndTell.core.chrome import kill_managed_chrome, launch_managed_chrome

from .base import ProductAdapter

# OpenAI's desktop app shipped as "Codex.app"; recent versions ship as
# "ChatGPT.app" (same app — bundle id com.openai.codex, "Codex Framework"
# internals, ~/Library/Application Support/Codex). Accept either so the rename
# doesn't break the adapter; newest first.
CODEX_APP_PATHS = ("/Applications/ChatGPT.app", "/Applications/Codex.app")
DEFAULT_CODEX_CDP = 9333
# Seconds to wait after Codex reports "recording is live" before demonstrating —
# its Record & Replay capture engine lags the chat confirmation, and gestures
# issued too early are dropped from the recording.
RECORDING_ENGAGE_SECS = 5
# Codex confirms the recording within ~2 minutes; poll a bit past that, approving
# any permission dialog along the way.
RECORDING_START_TIMEOUT = 150
START_PROMPT = "Use Record & Replay to start recording my screen. Just start it; I'll demonstrate a task, then tell you \"done recording\"."
DONE_PROMPT = "done recording"
_COMPOSER_SELECTOR = (
    '[data-codex-composer-root]:visible, '
    '[data-codex-composer="true"]:visible, '
    '[contenteditable="true"][role="textbox"]:visible, '
    'textarea:visible'
)
_NEW_CHAT_HOME_SELECTOR = '[class*="home-main-content"]:visible'
_CHATGPT_MODE_BUTTON = "Switch mode, current mode: ChatGPT"
_CODEX_MODE_BUTTON = "Switch mode, current mode: Codex"
_PROJECTLESS_COMPOSER_BUTTON = "Choose project"
# Newer ChatGPT builds put a Chat/Work toggle on the new-chat home and default
# to Chat, whose composer has no project/plugin controls.  Record & Replay is
# a Work-mode (agentic) feature, and only the Work composer exposes the
# "Choose project" button that proves the chat is projectless.
_WORK_MODE_TOGGLE = "Work"
_ACTIVE_PROJECT_SELECTOR = '[aria-label^="Project: "]:visible'
_CHANGE_PROJECT_SELECTOR = '[aria-label^="Change project: "]:visible'

# Two shapes: with is/has any live-word is safe ("recording is on"); bare forms
# accept only unambiguous past-state words with a word boundary — bare "on"
# would match pre-start text ("start recording on your screen", "once", "only").
_LIVE_RE = re.compile(
    r"recording (is|has) (on|now running|running|started|live|active|in progress|begun)\b"
    r"|recording (now running|started|live|active|in progress|begun)\b")


def _recording_live(body: str) -> bool:
    """True once the app confirms recording is live. Its wording varies run to run
    ('Recording is active', 'Recording has started', 'Recording is now running'…),
    so match is/has + any live word — and, robustly, fall back to the demo
    instruction the app shows only once recording is running ('demonstrate your
    task … done recording', or the recording-time cap), which no pre-start state
    shows."""
    low = body.lower()
    if _LIVE_RE.search(low):
        return True
    return ((("demonstrate your task" in low or "perform your task" in low)
             and "done recording" in low)
            or "maximum recording time" in low)


# Affirmative approval controls Codex renders, matched by the BUTTON label — a
# stable UI affordance — rather than the prose of the request, which varies per
# permission (record screen / edit skill file / run command) and stalled the run
# whenever a new wording appeared. exact=True so "Allow" can't match "Don't allow".
_APPROVE_BUTTONS = ("Allow once", "Allow always", "Allow anyway", "Allow", "Approve")


def _approve(page) -> bool:
    """Grant whatever permission Codex is prompting for, decided by the presence of
    an affirmative control — not the wording of the request. Returns True if it
    clicked something; a poll with no pending approval is a no-op returning False."""
    # radio-style: pick "Yes, allow for this turn", then Submit (Enter).
    try:
        radio = page.get_by_text("Yes, allow for this turn", exact=False)
        if radio.count():
            radio.first.click(timeout=2000)
            page.keyboard.press("Enter")
            return True
    except Exception:
        pass
    # button-style: click the first affirmative approval button that is present.
    for name in _APPROVE_BUTTONS:
        try:
            btn = page.get_by_role("button", name=name, exact=True)
            if btn.count():
                btn.first.click(timeout=2000)
                return True
        except Exception:
            continue
    return False


def _codex_app_installed() -> bool:
    return any(Path(p).exists() for p in CODEX_APP_PATHS)


def _codex_app_path() -> str:
    for p in CODEX_APP_PATHS:
        if Path(p).exists():
            return p
    raise SystemExit(
        "OpenAI desktop app not found — install ChatGPT.app (formerly Codex.app) "
        "in /Applications")


def _main_app_pids(app_path: str) -> list[int]:
    """Return PIDs for the bundle's main executable, excluding its helpers.

    macOS exposes a short, truncated process name to ``pgrep -x`` for this
    Chromium bundle (for example ``/Applications/Ch`` instead of ``ChatGPT``),
    so matching the display name reports that the app is not running.  Match
    the full command path from ``ps`` instead; helper processes live elsewhere
    under ``Contents/Frameworks`` and therefore cannot be mistaken for the
    main executable.
    """
    executable = str(Path(app_path) / "Contents" / "MacOS" / Path(app_path).stem)
    output = subprocess.run(
        ["ps", "-Axo", "pid=,command="], capture_output=True, text=True,
    ).stdout
    pids: list[int] = []
    for line in output.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2:
            continue
        pid, command = fields
        if command == executable or command.startswith(executable + " "):
            pids.append(int(pid))
    return pids


def _quit_codex(app_path: str) -> None:
    """Quit a running instance so the relaunch can expose the debugging port —
    an already-running app ignores --remote-debugging-port. Graceful quit first,
    force-kill if it lingers."""
    name = Path(app_path).stem
    subprocess.run(["osascript", "-e", f'quit app "{name}"'], capture_output=True)
    for _ in range(20):
        if not _main_app_pids(app_path):
            return
        time.sleep(0.5)
    pids = _main_app_pids(app_path)
    if pids:
        subprocess.run(["kill", "-TERM", *map(str, pids)], check=False)
    time.sleep(1.5)


def launch_codex(cdp_port: int = DEFAULT_CODEX_CDP, out=print) -> None:
    app_path = _codex_app_path()
    name = Path(app_path).stem  # 'ChatGPT' (or 'Codex' on older installs)
    if chrome.wait_for_cdp(cdp_port, timeout_s=0):  # already up with CDP
        return
    # A running instance ignores the debugging-port flag, so a fresh `open`
    # won't expose CDP. Quit it first, then relaunch with the port.
    if _main_app_pids(app_path):
        out(f"  {name} is running without a debugging port — quitting it to relaunch with CDP…")
        _quit_codex(app_path)
    subprocess.run(["open", "-na", app_path, "--args", f"--remote-debugging-port={cdp_port}"],
                   check=True)
    if not chrome.wait_for_cdp(cdp_port, timeout_s=30, poll_s=1):
        raise RuntimeError(
            f"{name} did not expose CDP on {cdp_port} even after a clean relaunch — "
            "this build may not allow remote debugging via --remote-debugging-port")


def _page_targets(cdp_port: int) -> list[str]:
    """Page-target URLs the app reports over plain /json (empty on errors)."""
    import json as _json
    import urllib.request

    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{cdp_port}/json", timeout=3) as reply:
            targets = _json.loads(reply.read().decode())
    except Exception:
        return []
    return [t.get("url", "") for t in targets if t.get("type") == "page"]


def _codex_page(pw, cdp_port: int):
    """Return the desktop app's main page, not one of its overlay surfaces.

    Newer Codex builds expose several auxiliary ``Codex Pet Composition
    Surface`` pages over CDP.  Those pages can precede the real app page, so
    selecting ``pages[0]`` intermittently hands callers a surface with no chat
    composer.  Prefer the canonical main-window URL, with a composer-bearing
    Codex page as a compatibility fallback for older/newer shells.
    """
    # Connect only once the app reports a page target over plain /json: a
    # playwright connection opened during app startup never learns about
    # this framework's later-created targets and polls an empty browser
    # forever, while /json already lists the window. A relaunch cold start
    # can take well past 30s to create that first page target.
    deadline = time.time() + 120
    targets = []
    while time.time() < deadline:
        targets = _page_targets(cdp_port)
        if targets:
            break
        time.sleep(0.5)
    else:
        raise RuntimeError(
            "Codex main window did not appear on its debugging port; "
            f"/json listed no page targets (last: {targets})")

    browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
    seen: list[str] = []
    while time.time() < deadline:
        pages = [page for context in browser.contexts for page in context.pages]
        seen = [page.url for page in pages]
        for page in pages:
            if page.url == "app://-/index.html":
                return page
        for page in pages:
            try:
                if (page.title() == "Codex"
                        and page.locator('[data-codex-composer="true"], '
                                         '[contenteditable="true"][role="textbox"], '
                                         'textarea').count()):
                    return page
            except Exception:
                continue
        time.sleep(0.5)
    raise RuntimeError(
        "Codex main window did not appear on its debugging port; "
        f"CDP pages were: {seen}")


def _send(page, text: str) -> None:
    # A Record & Replay permission dialog ("Allow ChatGPT to record…") can cover
    # the composer; dismiss it first, then wait for the composer to be visible
    # rather than hanging on the locator's default 30s timeout.
    _approve(page)
    # data-codex-composer is the app-owned stable hook. Keep role-based and
    # textarea fallbacks for app versions on either side of its introduction.
    box = page.locator(
        '[data-codex-composer="true"]:visible, '
        '[contenteditable="true"][role="textbox"]:visible, '
        'textarea:visible').first
    box.wait_for(state="visible", timeout=20000)
    box.click()
    box.type(text)
    time.sleep(0.3)
    page.keyboard.press("Enter")


def _select_work_mode(page) -> None:
    """Press the home-screen ``Work`` toggle on builds that have one.

    Older builds have no toggle and always show the Work-style composer, so
    a missing toggle is not an error; the projectless verification that
    follows is the arbiter either way.
    """
    work = page.get_by_role("button", name=_WORK_MODE_TOGGLE, exact=True)
    if not work.count():
        return
    # Only the home-screen mode toggle carries aria-pressed; anything else
    # named "Work" (a sidebar row, say) is not the control we want.
    pressed = work.first.get_attribute("aria-pressed")
    if pressed is None or pressed == "true":
        return
    work.first.click(timeout=5000)
    page.wait_for_timeout(500)


def _new_task(page) -> None:
    """Switch to ChatGPT and open a verified top-level, projectless chat.

    A Codex-mode ``New chat`` inherits the active local project.  That leaks
    repository context into Record & Replay and is exactly what the benchmark
    must avoid.  Switch modes through the app's accessible mode menu, click the
    global ChatGPT ``New chat`` button, and require the projectless composer
    state before sending anything.

    Reusing an old task is unsafe too: stale "Recording is live" text can make
    the adapter perform the demonstration before a new capture has started.
    """
    try:
        chatgpt_mode = page.get_by_role(
            "button", name=_CHATGPT_MODE_BUTTON, exact=True)
        if not chatgpt_mode.count():
            page.get_by_role(
                "button", name=_CODEX_MODE_BUTTON, exact=True,
            ).click(timeout=5000)
            page.get_by_role("menuitem").filter(
                has_text="ChatGPT",
            ).click(timeout=5000)
            chatgpt_mode.wait_for(state="visible", timeout=10000)

        # The top icon is the global ChatGPT action.  The separate sidebar
        # text button can inherit the currently open project, and builds
        # relabel these controls — try the most specific control first and
        # fall back; the projectless verification below is the arbiter no
        # matter which control opened the chat.
        for open_new_chat in (
                lambda: page.get_by_label("New chat", exact=True).click(
                    timeout=5000, force=True),
                lambda: page.get_by_role(
                    "button", name="New chat", exact=True,
                ).first.click(timeout=5000, force=True),
                lambda: page.keyboard.press("Meta+n"),
        ):
            try:
                open_new_chat()
                break
            except Exception:
                continue
        page.locator(_NEW_CHAT_HOME_SELECTOR).first.wait_for(
            state="visible", timeout=10000)
        page.locator(_COMPOSER_SELECTOR).first.wait_for(
            state="visible", timeout=10000)
        _select_work_mode(page)
        page.get_by_role(
            "button", name=_PROJECTLESS_COMPOSER_BUTTON, exact=True,
        ).wait_for(state="visible", timeout=10000)
        if (page.locator(_ACTIVE_PROJECT_SELECTOR).count()
                or page.locator(_CHANGE_PROJECT_SELECTOR).count()):
            raise RuntimeError("an active project is still attached")
    except Exception as exc:
        try:
            seen = page.evaluate(
                "()=>document.body.innerText.slice(0,200)").replace("\n", " ")
        except Exception:
            seen = "(page unreadable)"
        raise RuntimeError(
            "ChatGPT did not open a verified top-level projectless chat; "
            "the mode, new-chat home, composer, or project state was wrong "
            f"(page shows: …{seen})"
        ) from exc
    page.wait_for_timeout(1200)


def _wait_idle(page, timeout_s: int, out=lambda _: None) -> None:
    """Wait until Codex stops Thinking/Working and the transcript settles,
    auto-approving any permission prompts it raises along the way."""
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        if _approve(page):
            out("  approved a Codex prompt")
            time.sleep(3)
            continue
        body = page.evaluate("()=>document.body.innerText")
        busy = "Thinking" in body or "Working for" in body
        if not busy and body == last:
            return
        last = body
        time.sleep(6)


_OPEN_PLUGIN_MANAGEMENT_JS = """()=>{
  const heads=[...document.querySelectorAll('*')].filter(e=>e.children.length===0&&e.textContent.trim()==='Installed');
  for (const h of heads){
    const btn=(h.parentElement&&h.parentElement.querySelector('button'))
      ||(h.closest('div')&&h.closest('div').querySelector('button'));
    if(btn){btn.click();return true;}
  }
  return false;
}"""

_SETTINGS_RENDERED_JS = """()=>document.body.innerText.includes('Manage plugins')"""

_RECORD_REPLAY_TOGGLE_JS = """()=>{
  const el=[...document.querySelectorAll('*')].find(e=>e.children.length===0&&/^record\\s*(&|and)\\s*replay$/i.test(e.textContent.trim()));
  if(!el) return null;
  let n=el;
  for(let i=0;i<6;i++){ n=n.parentElement; if(n&&n.querySelector('[role=\"switch\"]')) break; }
  const sw=n&&n.querySelector('[role=\"switch\"]');
  return sw?sw.getAttribute('aria-checked'):null;
}"""

_SIGNED_OUT_MARKERS = ("Log in", "Sign up", "Welcome back")


def _poll(page, probe, tries=10, interval_ms=500):
    """Page renders settle at different speeds per machine; bounded polling
    instead of one fixed sleep."""
    for _ in range(tries):
        result = probe()
        if result:
            return result
        page.wait_for_timeout(interval_ms)
    return None


def _restore_home(page) -> None:
    """Best-effort: leave the app on the new-chat home."""
    try:
        back = page.get_by_text("Back to app")
        if back.count():
            back.first.click()
            page.wait_for_timeout(400)
        page.get_by_role("button", name="New chat").first.click()
        page.wait_for_timeout(400)
    except Exception:
        pass


def _record_replay_state(page) -> str | None:
    """Read the Record & Replay plugin from the app's settings UI.

    Returns "enabled", "disabled", "missing" (settings rendered, no such
    plugin row — it is not installed), or None when the settings surface
    could not be navigated (app layout drift) — callers fail open on None.
    """
    state = None
    try:
        page.get_by_role("button", name="Plugins").first.click()
        if _poll(page, lambda: page.evaluate(_OPEN_PLUGIN_MANAGEMENT_JS)):
            toggle = _poll(page, lambda: page.evaluate(_RECORD_REPLAY_TOGGLE_JS))
            if toggle in ("true", "false"):
                state = "enabled" if toggle == "true" else "disabled"
            elif page.evaluate(_SETTINGS_RENDERED_JS):
                state = "missing"
    except Exception:
        state = None
    finally:
        _restore_home(page)
    return state


def _verify_codex_ready(page, out=print) -> None:
    """Gate on the account prerequisites the harness cannot create.

    Record & Replay is a per-account plugin (the app's Settings -> Plugins:
    "Record what I'm doing on my Mac and turn it into a Skill"). Without it
    the model refuses the start prompt only after the fixture, Chrome, and
    the screen recording have been spent. Sign-in is checked first — a
    signed-out app has no plugins at all. Settings that cannot be read (app
    layout drift) warn and continue; the recording-start wait stays the
    backstop.
    """
    def signed_in():
        body = page.evaluate("()=>document.body.innerText") or ""
        if any(marker in body for marker in _SIGNED_OUT_MARKERS):
            return "the ChatGPT desktop app is not signed in"
        return None

    readiness.ensure(
        signed_in,
        "Sign in to the ChatGPT app window that just opened.",
        out, live=True)

    unreadable = []

    def plugin():
        state = _record_replay_state(page)
        if state == "disabled":
            return "the Record & Replay plugin is disabled for this account"
        if state == "missing":
            return "the Record & Replay plugin is not installed for this account"
        if state is None and not unreadable:
            unreadable.append(True)
            out("  (could not verify Record & Replay availability — continuing)")
        return None

    readiness.ensure(
        plugin,
        "In the ChatGPT app: Settings → Plugins → install and enable "
        "“Record & Replay”, then continue.",
        out)


# How the desktop sidebar marks the chat that is currently open.  Newer builds
# set ``data-app-action-sidebar-thread-active="true"`` plus ``aria-current=
# "page"`` on that row; there ``…-thread-selected`` is only a sticky "last row
# clicked/focused" flag — a chat opened via "New chat" and a first message
# stays ``selected="false"`` for its whole life, and a previously clicked row
# keeps ``selected="true"`` even from the new-chat home — so waiting on that
# attribute alone never returns (or names the wrong chat).  Older builds only
# had ``selected``, so it is still honoured, but not on a row that the app
# explicitly reports as not active.
_CURRENT_THREAD_MARKERS = (
    '[data-app-action-sidebar-thread-active="true"]',
    '[aria-current="page"]',
    '[data-app-action-sidebar-thread-selected="true"]'
    ':not([data-app-action-sidebar-thread-active="false"])',
)


def _current_thread_selector(thread_id: str | None = None,
                             visible: bool = False) -> str:
    """CSS for the sidebar row of the open chat (optionally a specific one)."""
    ident = "[data-app-action-sidebar-thread-id"
    ident += f'="{thread_id}"]' if thread_id else "]"
    suffix = ":visible" if visible else ""
    return ", ".join(f"{ident}{marker}{suffix}"
                     for marker in _CURRENT_THREAD_MARKERS)


def _selected_thread_id(page) -> str:
    """Return the desktop sidebar identity for the currently open chat.

    Identification only reads an attribute, so it must not depend on the
    row being visible: a collapsed sidebar section hides the row without
    changing which chat is open.
    """
    from showAndTell.core.pwerrors import PWTimeout

    row = page.locator(_current_thread_selector()).first
    try:
        row.wait_for(state="attached", timeout=10000)
    except PWTimeout:
        rows = page.locator("[data-app-action-sidebar-thread-id]").count()
        raise RuntimeError(
            "the ChatGPT desktop app never marked the new chat as current "
            "in its sidebar (no row carries thread-active/aria-current/"
            f"thread-selected; {rows} thread rows present; page {page.url}); "
            "the -FAILED-screen recording under runs/ shows what the app "
            "displayed") from None
    thread_id = row.get_attribute("data-app-action-sidebar-thread-id")
    if not thread_id:
        raise RuntimeError("the open ChatGPT chat has no stable thread identity")
    return thread_id


def _restore_thread(page, thread_id: str) -> None:
    """Return to an in-progress recording chat if the user navigated away.

    Being open is app state, not visibility: a chat hidden by a collapsed
    sidebar section is still the current chat and needs no restoring.
    """
    if page.locator(_current_thread_selector(thread_id)).count():
        return
    row = page.locator(
        f'[data-app-action-sidebar-thread-id="{thread_id}"]:visible'
    ).first
    row.wait_for(state="visible", timeout=10000)
    row.click(force=True)
    page.locator(
        _current_thread_selector(thread_id, visible=True)
    ).first.wait_for(state="visible", timeout=10000)


def _start_recording(page, out=print) -> str:
    """Start a fresh Record & Replay capture and wait for it to be live."""
    _new_task(page)
    _send(page, START_PROMPT)
    thread_id = _selected_thread_id(page)
    deadline = time.time() + RECORDING_START_TIMEOUT
    body = ""
    while time.time() < deadline:
        _restore_thread(page, thread_id)
        body = page.evaluate("()=>document.body.innerText")
        if _recording_live(body):
            out("  recording is live")
            return thread_id
        if _approve(page):
            out("  approved the recording permission")
        time.sleep(3)
    tail = body[-400:].replace("\n", " ")
    raise RuntimeError(
        f"Codex did not confirm the recording started; last chat: …{tail}")


def _finish_recording(page, out=print) -> None:
    """Stop Record & Replay and wait for Codex to inspect the capture."""
    _send(page, DONE_PROMPT)
    time.sleep(5)
    _wait_idle(page, 600, out)


def _activate_managed_chrome(page, pid: int) -> None:
    """Raise the exact managed Chrome page that will receive OS gestures.

    Activating a Chrome process alone is insufficient when several managed
    profiles are running: macOS may foreground a different Chrome window from
    the same application bundle.  Select the Playwright-owned target tab after
    native application activation so its window owns the coordinates used by
    the CGEvent actuator.
    """
    from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication

    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is None:
        raise RuntimeError(f"managed Chrome process {pid} is no longer running")
    app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
    page.bring_to_front()
    time.sleep(0.5)


def _osinput_driver(task_dir: Path, activate):
    """Wrap the browser module already selected by a task demonstration."""
    from showAndTell.player.osactuator import OSActuator
    from showAndTell.player.osops import OSOps
    from showAndTell.tasks import load_demonstrate

    demonstrate = load_demonstrate(Path(task_dir))
    candidates = [
        value for value in demonstrate.__globals__.values()
        if isinstance(value, ModuleType) and callable(getattr(value, "login", None))
    ]
    if len(candidates) != 1:
        names = sorted(getattr(value, "__name__", repr(value)) for value in candidates)
        raise RuntimeError(
            f"task browser plane is ambiguous; expected one login module, found {names}")
    return OSOps(candidates[0], OSActuator(activate))


class CodexAdapter(ProductAdapter):
    """Codex Record & Replay: screen-record the demonstration performed with
    real OS input in the frontmost managed Chrome, then quiz over its chat."""

    name = "codex"
    display = "Codex"
    teach_label = "codex-record"

    def __init__(self) -> None:
        self.codex_page = None
        self.codex_thread_id = None
        self.activate = None
        self.actuator = None

    def preflight(self, session) -> None:
        from showAndTell.core import osinput

        # CGEventPost fails silently without Accessibility trust; refuse
        # before relaunching Codex, seeding the fixture, and starting the
        # screen recording rather than at the Chrome step
        osinput.assert_trusted()

    def launch_steps(self, session):
        def start() -> None:
            readiness.ensure(
                lambda: (None if _codex_app_installed()
                         else "the OpenAI desktop app is not installed"),
                "Install ChatGPT.app from https://openai.com/chatgpt/download/ "
                "into /Applications, then continue.",
                session.out, live=True)
            launch_codex(session.codex_cdp_port, session.out)

        codex = ("launching Codex app…", start)
        ready = ("verifying ChatGPT sign-in and Record & Replay…",
                 lambda: _verify_codex_ready(
                     _codex_page(session.pw, session.codex_cdp_port),
                     session.out))
        if session.mode == "teach":
            # The teach fixture boots between Codex and Chrome (Chrome joins
            # in arm_steps); a trial's surfaces open inside Chrome, so it must
            # be up before the seed phase.
            return [codex, ready]
        return [codex, ready, self._chrome_step(session)]

    def _chrome_step(self, session):
        return ("launching managed Chrome…",
                lambda: self._launch_chrome(session))

    def _launch_chrome(self, session) -> None:
        from showAndTell.core import osinput
        from showAndTell.player.osactuator import OSActuator
        from .codex_input import main_chrome_pid

        # CGEventPost fails silently when the posting Python process lacks
        # Accessibility trust.  Detect that before starting Chrome or Record &
        # Replay instead of producing a nine-minute capture with every action
        # skipped.
        osinput.assert_trusted()
        kill_managed_chrome(session.profile_root)
        launch_managed_chrome(session.cdp_port, profile_root=session.profile_root)
        session.connect_chrome()
        if session.mode == "teach":
            page = session.ctx.new_page()
            cdp = session.ctx.new_cdp_session(page)
            window_id = cdp.send("Browser.getWindowForTarget")["windowId"]
            cdp.send("Browser.setWindowBounds", {"windowId": window_id,
                     "bounds": {"left": 0, "top": 0, "width": 1600, "height": 1000}})
            session.page = page
        else:
            for existing in list(session.ctx.pages):
                existing.close()
        pid = main_chrome_pid(str(session.profile_root))

        def activate() -> None:
            _activate_managed_chrome(session.page, pid)

        self.activate = activate
        self.actuator = OSActuator(activate)
        if session.mode == "teach":
            activate()
            time.sleep(1)

    def arm_steps(self, session):
        label = ("asking Codex to start screen recording…" if session.mode == "teach"
                 else "asking Codex to start Record & Replay…")

        def arm() -> None:
            self.codex_page = _codex_page(session.pw, session.codex_cdp_port)
            self.codex_thread_id = _start_recording(
                self.codex_page, session.out)

        steps = [(label, arm)]
        if session.mode == "teach":
            steps.insert(0, self._chrome_step(session))
        return steps

    def demo_label(self, session) -> str:
        if session.mode == "teach":
            return "performing the demonstration with real OS input…"
        return "replaying only the recorded task actions as OS input…"

    @contextmanager
    def demo_stage(self, session):
        # Codex's chat says "recording is live" before Record & Replay is
        # actually capturing; bring the managed Chrome frontmost and give the
        # capture engine a few seconds to engage, or the first gestures land
        # outside the recording (Codex then sees "a page with no actions").
        self.activate()
        time.sleep(RECORDING_ENGAGE_SECS)
        yield session.page
        if session.mode == "teach":
            time.sleep(1)

    def demo_kwargs(self, session) -> dict:
        from showAndTell.player.osops import OSLocatorProxy

        kwargs = {
            "before_start": self.activate,
            "input_hooks": {
                "locator_wrapper": lambda locator: OSLocatorProxy(
                    locator, locator.page, self.actuator),
                "type_text": self.actuator.type_text,
                "paste_text": self.actuator.paste_text,
            },
        }
        if session.mode == "teach":
            # Hand-authored tasks use their established generic OS plane;
            # generated human captures use the hooks above instead.
            kwargs["ops_factory"] = lambda: _osinput_driver(
                session.source_dir, self.activate)
        return kwargs

    def conclude_steps(self, session):
        label = ("telling Codex 'done recording' and waiting for it to inspect…"
                 if session.mode == "teach" else "ending Record & Replay…")
        def finish() -> None:
            _restore_thread(self.codex_page, self.codex_thread_id)
            _finish_recording(self.codex_page, session.out)

        return [(label, finish)]

    def ask(self, session, message: str) -> str:
        _restore_thread(self.codex_page, self.codex_thread_id)
        _send(self.codex_page, message)
        time.sleep(5)
        _wait_idle(self.codex_page, 420, session.out)
        return self.codex_page.evaluate("()=>document.body.innerText")

    def save_artifacts(self, session) -> None:
        # The graded result is already on disk — the Codex-window screenshot
        # can hang, and the score must survive that.
        transcript = self.codex_page.evaluate("()=>document.body.innerText")[:16000]
        (session.run_dir / "codex_chat.txt").write_text(transcript, encoding="utf-8")
        try:
            self.codex_page.screenshot(
                path=str(session.run_dir / "codex.png"), timeout=8000)
        except Exception as exc:
            session.out(f"  (codex screenshot skipped: {exc})")
