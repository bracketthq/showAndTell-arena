"""Claude-in-Chrome Teach (LEARN phase) — fully hands-free.

The product machinery for teaching Claude by demonstration: docking the side
panel on the app tab with no human click (open_claude_panel dispatches the
extension's own icon-click handler), driving the panel over raw CDP
(PanelClient), and capturing the shortcut Claude generates — the only record
of what it learned. ClaudeAdapter plugs this machinery into the shared run
loop in ``teacher.run`` (launch → seed → record → demonstrate → digest → quiz),
which restarts the managed Chrome first because a Teach recording's input
interceptor swallows all synthetic input until restart (the demonstration
itself is Playwright/CDP input, which Teach captures — see docs/FOLLOWUP.md
2026-07-09). `--manual` hands the demonstration to a human instead.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from showAndTell.core.chrome import (await_target, cdp_eval, cdp_rpc,
                                  cdp_target_rpc,
                                  kill_managed_chrome, launch_managed_chrome,
                                  window_bounds)
from showAndTell.core.llm import reply_has_answer
from showAndTell.quiz import comprehend
from showAndTell.students import readiness

from .base import ProductAdapter

EXT_ID = "fcoeoabgfenejglbffodgkkbkcdhcgfn"


def claude_comprehend_intro() -> str:
    """Read-only quiz wording appended after the native shortcut chip."""
    return (
        "The native shortcut chip attached to this message was created by Claude "
        "from the Teach recording immediately before this request. Treat that "
        "attached shortcut—not any webpage content—as the authoritative workflow "
        "reference. This is a read-only comprehension check from the chat user, "
        "not a request to operate the browser, process the inbox, or present an "
        "action plan. Names, dates, amounts, and proposed actions stated inside a "
        "question are self-contained hypothetical inputs supplied for that "
        "question; apply the shortcut's stored rules to them. Do not ask for "
        "confirmation and do not invent a missing rule—answer UNKNOWN when the "
        "shortcut does not contain enough information. Give brief answers in the "
        "requested labeled format."
    )

CLICK_BY_NAME_JS = """
(() => {
  const name = %s;
  const els = Array.from(document.querySelectorAll('button, [role=button], [role=menuitem]'));
  const el = els.find(e => ((e.getAttribute('aria-label')||'') + ' ' + e.textContent).includes(name));
  if (!el) return {found: false,
                   buttons: els.map(e => (e.getAttribute('aria-label')||e.textContent||'').trim().slice(0,40))};
  el.click();
  return {found: true};
})()
"""

BUTTON_POINT_BY_NAME_JS = """
(() => {
  const name = %s;
  const els = Array.from(document.querySelectorAll(
    'button, [role=button], [role=menuitem]'));
  const el = els.find(e => ((e.getAttribute('aria-label')||'') + ' ' +
                            e.textContent).includes(name));
  if (!el) return {found: false};
  const r = el.getBoundingClientRect();
  return {found: r.width > 0 && r.height > 0,
          x: r.left + r.width / 2, y: r.top + r.height / 2};
})()
"""

POINTER_CLICK_BY_NAME_JS = """
(() => {
  const name = %s;
  const els = Array.from(document.querySelectorAll(
    'button, [role=button], [role=menuitem]'));
  const el = els.find(e => ((e.getAttribute('aria-label')||'') + ' ' +
                            e.textContent).includes(name));
  if (!el) return false;
  el.dispatchEvent(new PointerEvent('pointerdown',
    {bubbles: true, button: 0, pointerType: 'mouse'}));
  el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0}));
  el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, button: 0}));
  el.dispatchEvent(new MouseEvent('click', {bubbles: true, button: 0}));
  return true;
})()
"""

# Click the LAST button matching a name. The 'Create shortcut' modal repeats the
# label as both its heading and its footer submit button — the footer one (which
# actually saves) is last in the DOM.
CLICK_LAST_JS = """
(() => {
  const name = %s;
  const els = Array.from(document.querySelectorAll('button, [role=button]'));
  const m = els.filter(e => ((e.getAttribute('aria-label')||'') + ' ' + e.textContent).includes(name));
  if (!m.length) return {found: false};
  m[m.length - 1].click();
  return {found: true, count: m.length};
})()
"""

# Read the Create-shortcut modal's fields + submit-button state. The Prompt field
# holds Claude's generated understanding of the demonstration — the only artifact
# of what it learned (the chat thread keeps no memory of a Teach recording).
MODAL_CAPTURE_JS = """
(() => {
  let dlg = document.querySelector('[role="dialog"],[aria-modal="true"]');
  if (!dlg) {
    const heading = Array.from(document.querySelectorAll('h1,h2,h3,h4'))
      .find(h => (h.textContent || '').trim() === 'Create shortcut');
    let candidate = heading && heading.parentElement;
    while (candidate && candidate !== document.body) {
      const buttons = Array.from(candidate.querySelectorAll('button'))
        .map(b => (b.textContent || '').trim());
      if (buttons.includes('Cancel') && buttons.includes('Create shortcut')) {
        dlg = candidate;
        break;
      }
      candidate = candidate.parentElement;
    }
  }
  const scope = dlg || document.body;
  const areas = Array.from(scope.querySelectorAll('textarea,[contenteditable="true"]'))
    .map(a => (a.value || a.textContent || '').trim()).filter(Boolean);
  const inputs = Array.from(scope.querySelectorAll('input')).map(i => (i.value || '').trim());
  const nameInput = scope.querySelector('input[placeholder*="summarize"]');
  const submit = Array.from(scope.querySelectorAll('button'))
    .filter(b => /create shortcut|save/i.test(b.textContent || '')).pop();
  return {real: !!dlg, modalText: dlg ? (dlg.innerText || '') : '',
          name: nameInput ? (nameInput.value || '').trim() : '',
          prompt: areas.sort((a, b) => b.length - a.length)[0] || '',
          names: inputs.filter(Boolean), submitDisabled: submit ? !!submit.disabled : null};
})()
"""

# Select all content of the chat composer so a following Input.insertText replaces
# it — clears any stuck draft (e.g. a half-typed '/command') that would otherwise
# prepend to the message and block the send.
SELECT_ALL_COMPOSER_JS = """
(() => {
  const eds = Array.from(document.querySelectorAll('[contenteditable="true"]'));
  const b = eds.find(e => !e.closest('[role="dialog"],[aria-modal="true"]')) || eds[0];
  if (!b) return false;
  b.focus();
  const r = document.createRange(); r.selectNodeContents(b);
  const s = window.getSelection(); s.removeAllRanges(); s.addRange(r);
  return true;
})()
"""


class PanelClient:
    """Drives the docked Claude side panel over raw CDP (it is not a tab, so
    Playwright/agent-browser cannot see it). JS clicks bypass the recorder's
    input interception; synthetic CDP input does not."""

    def __init__(self, cdp_base: str) -> None:
        self.cdp_base = cdp_base

    def _ws_url(self) -> str:
        # Chrome briefly removes the side-panel CDP target while the extension
        # replaces Teach with the generated-shortcut modal. Treat that as a
        # target transition, not an immediately fatal undock; a permanently
        # closed panel still fails with the same actionable error after 15s.
        deadline = time.time() + 15
        while True:
            try:
                targets = json.load(urllib.request.urlopen(f"{self.cdp_base}/json/list"))
            except Exception:
                targets = []
            for t in targets:
                if (t["type"] == "page"
                        and f"{EXT_ID}/sidepanel.html" in t.get("url", "")):
                    return t["webSocketDebuggerUrl"]
            if time.time() >= deadline:
                raise RuntimeError("Claude side panel not found — is it docked?")
            time.sleep(0.5)

    def _rpc(self, method: str, params: dict, timeout_s: float = 90) -> dict:
        # The panel target is re-resolved on every call — Chrome briefly
        # replaces the side-panel document while Teach initializes (_ws_url).
        return cdp_rpc(self._ws_url(), method, params, timeout_s)

    def _browser_ws_url(self) -> str:
        version = json.load(urllib.request.urlopen(f"{self.cdp_base}/json/version"))
        return version["webSocketDebuggerUrl"]

    def _iframe_target_ids(self) -> list[str]:
        """Claude's new panel is a cross-origin claude.ai iframe.

        Chrome does not reliably include out-of-process iframe targets in
        /json/list, so discover them from the browser target. Limit candidates
        to Claude's dedicated Chrome-panel route: an unrelated claude.ai tab
        must never receive panel automation.
        """
        try:
            # Target.getTargets' default filter excludes iframe targets in
            # current Chrome. Request them explicitly or the visible Claude
            # document remains undiscoverable even though it is an OOPIF.
            result = cdp_rpc(
                self._browser_ws_url(), "Target.getTargets",
                {"filter": [{"type": "iframe", "exclude": False}]},
            )
        except Exception:
            return []
        found = []
        for target in result.get("targetInfos", []):
            url = target.get("url", "")
            if target.get("type") != "iframe":
                continue
            try:
                parsed = urllib.parse.urlparse(url)
            except ValueError:
                continue
            is_claude = parsed.hostname == "claude.ai" and parsed.path.startswith("/cic/")
            is_preview = parsed.hostname == "preview.claude.ai"
            if is_claude or is_preview:
                found.append(target["targetId"])
        return found

    @staticmethod
    def _evaluation_value(result: dict):
        if result.get("exceptionDetails"):
            raise RuntimeError(str(result["exceptionDetails"])[:300])
        return result.get("result", {}).get("value")

    def _evaluate_on_main(self, expr: str):
        result = self._rpc("Runtime.evaluate",
                           {"expression": expr, "returnByValue": True,
                            "awaitPromise": True, "userGesture": True})
        return self._evaluation_value(result)

    def _evaluate_on_iframe(self, target_id: str, expr: str):
        result = cdp_target_rpc(
            self._browser_ws_url(), target_id, "Runtime.evaluate",
            {"expression": expr, "returnByValue": True,
             "awaitPromise": True, "userGesture": True},
            timeout_s=90,
        )
        return self._evaluation_value(result)

    def _ui_context(self) -> str | None:
        """Return the document that currently renders the visible panel UI.

        Classic Claude renders in the extension page. The newer experience
        renders in a cross-origin claude.ai iframe, including its first-run
        cookie dialog. Choose the document with the most visible text; hidden
        or still-loading documents report an empty string.
        """
        text_expr = "document.body?.innerText || ''"
        candidates: list[tuple[str | None, str]] = [
            (None, self._evaluate_on_main(text_expr) or "")
        ]
        for target_id in self._iframe_target_ids():
            try:
                candidates.append(
                    (target_id, self._evaluate_on_iframe(target_id, text_expr) or "")
                )
            except Exception:
                # The extension replaces iframe targets during initialization;
                # a disappeared candidate is equivalent to a loading document.
                continue
        return max(candidates, key=lambda item: len(item[1].strip()))[0]

    def evaluate(self, expr: str):
        target_id = self._ui_context()
        if target_id is None:
            return self._evaluate_on_main(expr)
        try:
            return self._evaluate_on_iframe(target_id, expr)
        except Exception:
            # A navigation can replace the chosen iframe between the probe and
            # the actual command. Re-resolve once, like _rpc does for the main
            # side-panel target.
            target_id = self._ui_context()
            return (self._evaluate_on_main(expr) if target_id is None
                    else self._evaluate_on_iframe(target_id, expr))

    def _button_result(self, name: str) -> dict:
        """Find and click a panel control in either rendering document.

        The new Claude experience lives in the iframe, while its "Switch back
        to classic" control lives in the extension shell around that iframe.
        Choosing only the document with the most text therefore makes the
        mode switch unreachable. Search the shell first, then every live
        Claude iframe, and merge the observed button labels for diagnostics.
        """
        expr = CLICK_BY_NAME_JS % json.dumps(name)
        seen = []
        try:
            result = self._evaluate_on_main(expr) or {}
            if result.get("found"):
                return result
            seen.extend(result.get("buttons") or [])
        except Exception:
            pass
        for target_id in self._iframe_target_ids():
            try:
                result = self._evaluate_on_iframe(target_id, expr) or {}
            except Exception:
                continue
            if result.get("found"):
                return result
            seen.extend(result.get("buttons") or [])
        return {"found": False, "buttons": seen}

    def click_button(self, name: str) -> None:
        result = self._button_result(name)
        if not result.get("found"):
            raise RuntimeError(f"button {name!r} not in panel; saw: {result.get('buttons')}")

    def try_click_button(self, name: str) -> bool:
        return bool(self._button_result(name).get("found"))

    def trusted_click_button(self, name: str) -> bool:
        """Click a visible Radix control in the shell or embedded experience.

        Radix dropdown triggers and items ignore ``element.click()``. The shell
        accepts real CDP pointer input; an out-of-process iframe needs the full
        pointer/mouse event sequence dispatched in its own document. This is
        used only before Teach recording starts.
        """
        point = self._evaluate_on_main(
            BUTTON_POINT_BY_NAME_JS % json.dumps(name)) or {}
        if point.get("found"):
            common = {"x": point["x"], "y": point["y"],
                      "button": "left", "clickCount": 1}
            self._rpc("Input.dispatchMouseEvent",
                      {"type": "mousePressed", **common})
            self._rpc("Input.dispatchMouseEvent",
                      {"type": "mouseReleased", **common})
            return True
        expr = POINTER_CLICK_BY_NAME_JS % json.dumps(name)
        for target_id in self._iframe_target_ids():
            try:
                if self._evaluate_on_iframe(target_id, expr) is True:
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def recording_active_in(t: str) -> bool:
        """True once Teach is recording: the start screens are gone and the
        stop control ('Done') / step list / 'Listening' is showing."""
        past_start = "Start recording" not in t and "How can I help" not in t
        return past_start and ("Done" in t or "record each step" in t or "Listening" in t)

    def recording_active(self) -> bool:
        return self.recording_active_in(self.text())

    def text(self) -> str:
        # The extension briefly replaces the side-panel document while Teach
        # initializes. A worker can observe that target between documents,
        # where document.body is null; treat it as an empty/loading panel and
        # let the caller's retry loop continue.
        return self.evaluate("document.body?.innerText || ''") or ""

    @staticmethod
    def busy_in(t: str) -> bool:
        """True while Claude is still generating — a Stop control is showing
        instead of Send. Sending a quiz into a busy panel queues the message and
        never gets a proper reply (the cause of blank 0/9 quizzes)."""
        return ("Stop message" in t or "Stop generating" in t
                or "Generating shortcut" in t)

    def busy(self) -> bool:
        return self.busy_in(self.text())

    def wait_idle(self, out, timeout_s: int = 180) -> None:
        """Block until Claude stops generating, so the next message isn't sent
        into a busy panel."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if not self.busy():
                return
            time.sleep(3)
        out("  (Claude still generating at idle-timeout — proceeding)")

    def shortcut_modal_open(self) -> bool:
        """True when Claude's post-Teach 'Create shortcut' modal is up. It
        blocks the chat composer (and steals Enter → submits the shortcut), so
        it must be dismissed before quizzing."""
        return bool(self.evaluate(
            "(() => { const t = document.body.innerText || '';"
            " return /Create shortcut/.test(t) && /Cancel/.test(t)"
            "        && (/Prompt/.test(t) || /summarize/.test(t)); })()"))

    def save_shortcut(self, out, name: str) -> str:
        """After a Teach recording Claude auto-generates a shortcut and opens a
        'Create shortcut' modal (Name + Prompt*). The Prompt is Claude's written
        understanding of the demo — the ONLY record of what it learned, since the
        chat thread keeps no memory of a Teach recording. Wait for it to fill,
        CAPTURE it (returned, for the quiz), name + save the shortcut (persisting
        it, as the user asked), and dismiss the modal. Returns the captured Prompt
        text ('' if no modal appeared)."""
        out("  waiting for Claude to generate the shortcut (up to ~3 min) — do NOT click "
            "'Create shortcut'; I'll save it…")
        empty_polls = 0
        for _ in range(90):  # wait out generation and for Prompt to fill. Bare
            # 'Generating' also covers the modal's auto-named Name slot — typing
            # the name while that is still in flight gets wiped when generation
            # lands, so Create silently doesn't take. Scope the check to the
            # dialog element itself when one exists: chat text can legitimately
            # contain the word 'Generating' forever (a workflow about
            # 'Generating reports'), which must not stall the save.
            cap = self.evaluate(MODAL_CAPTURE_JS) or {}
            busy_text = cap.get("modalText") if cap.get("real") else self.text()
            generating = "Generating" in (busy_text or "")
            if cap.get("prompt") and not generating:
                break
            visibly_empty = (
                cap.get("real") and not cap.get("name")
                and not cap.get("prompt") and not generating
            )
            empty_polls = empty_polls + 1 if visibly_empty else 0
            if empty_polls >= 5:
                out("  Claude returned an empty shortcut; closing the form.")
                self.try_click_button("Cancel")
                time.sleep(1)
                return ""
            time.sleep(2)
        if not self.shortcut_modal_open():
            return ""
        # Claude can remove the visible Generating label before it finishes
        # appending to Prompt. Observe the actual field and require a full minute
        # (12 polls) with identical content. The generation loop above already
        # waited for a real, non-Generating prompt; imposing another fixed
        # multi-minute delay here lets Chrome retire the side-panel target before
        # the shortcut can be saved.
        out("  shortcut looks ready; watching Prompt until its text stabilizes…")
        cap = self.evaluate(MODAL_CAPTURE_JS) or {}
        last_prompt = cap.get("prompt", "")
        stable_polls = 0
        for _ in range(120):
            time.sleep(5)
            if not self.shortcut_modal_open():
                return ""
            cap = self.evaluate(MODAL_CAPTURE_JS) or {}
            current_prompt = cap.get("prompt", "")
            stable_polls = stable_polls + 1 if current_prompt == last_prompt else 0
            last_prompt = current_prompt
            if stable_polls >= 12:
                break
        out(f"  shortcut Prompt stabilized at {len(last_prompt)} chars.")
        prompt = cap.get("prompt", "")
        out(f"  Claude's shortcut understanding: {len(prompt)} chars; "
            f"submit disabled={cap.get('submitDisabled')}")
        out(f"  naming the shortcut {name!r} and saving…")
        # Fill Name via REAL CDP input (focus the field, then insertText). The
        # synthetic value-setter doesn't register with React's controlled input,
        # so Save stays invalid and the modal won't close — typing does register.
        if self.evaluate(
                "(() => { const i = document.querySelector('input[placeholder*=\"summarize\"]')"
                " || document.querySelector('[role=\"dialog\"] input,[aria-modal=\"true\"] input');"
                " if (i) { i.focus(); i.select && i.select(); return true; } return false; })()") is True:
            self._rpc("Input.insertText", {"text": name})
            time.sleep(0.6)
        self.evaluate(CLICK_LAST_JS % json.dumps("Create shortcut"))
        for _ in range(15):
            time.sleep(1)
            if not self.shortcut_modal_open():
                out("  shortcut saved.")
                return prompt
        # Save didn't take — the modal must be dismissed or it swallows every
        # quiz message (empty answers graded and cached as a bogus 0.0).
        # Cancel first, Escape as fallback, and fail loudly if neither lands.
        self.try_click_button("Cancel")
        time.sleep(2)
        if self.shortcut_modal_open():
            for kind in ("keyDown", "keyUp"):
                self._rpc("Input.dispatchKeyEvent", {"type": kind, "key": "Escape",
                          "code": "Escape", "windowsVirtualKeyCode": 27})
            time.sleep(2)
        if self.shortcut_modal_open():
            # Fail loudly only when an actual dialog element is present —
            # shortcut_modal_open() is a text heuristic that chat prose quoting
            # the Teach UI can satisfy, and prose never dismisses.
            still = self.evaluate(MODAL_CAPTURE_JS) or {}
            if still.get("real"):
                raise RuntimeError(
                    "could not dismiss the 'Create shortcut' modal; a quiz "
                    "against a blocked composer would read back only empty "
                    "answers")
            out("  (modal-like text in the transcript, but no dialog element "
                "— continuing)")
            return prompt
        out("  (couldn't save the shortcut — cancelled the dialog to proceed)")
        return prompt

    def _focus_composer(self) -> None:
        """Focus the chat composer — the contenteditable NOT inside a modal
        dialog — so input can't be captured by a shortcut's Prompt field."""
        if self.evaluate(
                "(() => { const eds = Array.from(document.querySelectorAll('[contenteditable=\"true\"]'));"
                " const b = eds.find(e => !e.closest('[role=\"dialog\"],[aria-modal=\"true\"]')) || eds[0];"
                " if (b) { b.focus(); return true; } return false; })()") is not True:
            raise RuntimeError("Claude panel composer not found")

    def _approve_plan_if_present(self, out) -> bool:
        """Approve the action plan produced by an invoked shortcut, if shown."""
        if not self.try_click_button("Approve plan"):
            return False
        out("  approving Claude's shortcut plan…")
        return True

    def ask(self, msg: str, out, timeout_s: int = 240,
            shortcut_name: str | None = None) -> str:
        """Send a chat message into the panel composer and return Claude's
        settled reply. Uses CDP Input.insertText — real input the rich editor
        registers (the panel isn't the recorded surface, so no interceptor).
        Clears the composer first (select-all) so a stray draft can't prepend a
        stuck '/command' that blocks the send."""
        if self.shortcut_modal_open():  # a stray modal would swallow the message
            self.save_shortcut(out, name="quiz")
        self.wait_idle(out)  # don't send into a still-generating panel
        self.evaluate(SELECT_ALL_COMPOSER_JS)  # select any existing draft…
        self._rpc("Input.insertText", {"text": ""})  # …and delete it
        self._focus_composer()
        if shortcut_name:
            # A literal '/name' in submitted text does NOT invoke a shortcut.
            # Type it into the rich composer, then accept the autocomplete
            # result so React replaces it with a node-shortcutChip token.
            self._rpc("Input.insertText", {"text": "Based on "})
            self._rpc("Input.insertText", {"text": f"/{shortcut_name}"})
            time.sleep(1.0)  # allow the shortcut autocomplete to render
            for kind in ("keyDown", "keyUp"):
                self._rpc("Input.dispatchKeyEvent", {"type": kind, "key": "Enter",
                          "code": "Enter", "windowsVirtualKeyCode": 13})
            selected = self.evaluate(
                "(() => { const chip=document.querySelector('.node-shortcutChip');"
                " return chip ? (chip.textContent || '').trim() : ''; })()")
            if selected != shortcut_name:
                raise RuntimeError(
                    f"shortcut autocomplete did not select {shortcut_name!r}; got {selected!r}")
            self._rpc("Input.insertText", {"text": f", {msg}"})
        else:
            self._rpc("Input.insertText", {"text": msg})
        time.sleep(1.0)
        if not self.try_click_button("Send message"):
            for kind in ("keyDown", "keyUp"):  # fallback: submit on Enter
                self._rpc("Input.dispatchKeyEvent", {"type": kind, "key": "Enter",
                          "code": "Enter", "windowsVirtualKeyCode": 13})
        time.sleep(2)
        # Phase 1: wait for Claude to START replying. Between send and the first
        # streamed token there's a 'thinking' gap where busy() is false and the
        # text is stable — an idle+stable check alone returns that empty gap as
        # the answer. Wait until it goes busy OR the thread grows past the posted
        # message before deciding it's done.
        baseline = len(self.text())
        start_deadline = time.time() + 60
        while time.time() < start_deadline:
            t = self.text()
            if self.busy_in(t) or len(t) > baseline + 20:
                break
            time.sleep(2)
        # Phase 2: wait for generation to settle (idle AND stable) AND for the
        # answer to have actually rendered. Idle+stable alone can settle on the
        # 'thinking' gap or a mid-stream pause before the 'A1:' answer appears —
        # captured as an all-blank quiz. Require the answer text present so a
        # reply Claude did give is never dropped. (Fall back on timeout so a
        # differently-formatted reply still returns something to grade.)
        tail = msg[-45:]  # comprehend echoes the prompt; the answer follows it
        deadline = time.time() + timeout_s
        stable, last = 0, ""
        while time.time() < deadline:
            if self._approve_plan_if_present(out):
                # Shortcut execution starts only after approval. Give the
                # resulting browser work and answer a fresh full timeout.
                deadline = time.time() + timeout_s
                stable, last = 0, ""
                time.sleep(2)
                continue
            t = self.text()
            if t == last and t and not self.busy_in(t) and reply_has_answer(t, tail):
                stable += 1
                if stable >= 3:  # ~9s idle+stable+answered => generation finished
                    break
            else:
                stable = 0
            last = t
            time.sleep(3)
        return self.text()

    def screenshot(self, path: Path) -> None:
        data = self._rpc("Page.captureScreenshot", {}).get("data", "")
        path.write_bytes(base64.b64decode(data))


# Simulate the toolbar-icon click inside the extension: dispatch its own
# action.onClicked listener on the app tab — the real handler runs and wraps
# the tab in the Claude-managed group Teach records into (sidePanel.open alone
# never creates it; that was the empty-transcript bug). The handler's own
# sidePanel.open still fails here (the SW context refuses CDP's userGesture
# flag), so options.html is opened as a helper *page* context — pages honor
# the flag — for the OPEN_SIDEPANEL_JS step.
DISPATCH_ICON_CLICK_JS = """
(async () => {
  const [tab] = await chrome.tabs.query({url: '%s/*'});
  if (!tab) return {error: 'app tab not found'};
  chrome.action.onClicked.dispatch(tab);
  for (let i = 0; i < 20 && !(await chrome.tabGroups.query({})).length; i++)
    await new Promise(r => setTimeout(r, 500));
  const groups = (await chrome.tabGroups.query({})).length;
  const opts = await chrome.tabs.create({url: chrome.runtime.getURL('options.html'),
                                         active: false});
  return {groups, tabId: tab.id, optionsTabId: opts.id};
})()
"""

OPEN_SIDEPANEL_JS = """
(async () => {
  try { await chrome.sidePanel.open({tabId: %d}); return {ok: true}; }
  catch (e) { return {error: String(e)}; }
})()
"""

# Teach records only tabs inside the Claude-managed group, and the icon
# handler wraps just the tab it was dispatched on. Every other application
# tab must be adopted into the group explicitly: a surface tab opened before
# the panel stays ungrouped, and a tab a demonstration opens mid-recording
# over CDP (Target.createTarget has no opener tab) never inherits the group,
# so a two-tab workflow would record only its first tab. Sweeps stray tabs
# into the group now, and leaves onCreated/onUpdated listeners in the service
# worker so tabs opened or first navigated during the demonstration join too
# (the listeners live as long as the SW instance, which recording keeps busy).
# Chrome can throw a transient 'No current window' from tabs.query while
# focus moves between windows; the sweep retries in place before reporting.
ADOPT_OPEN_TABS_JS = """
(async () => {
  const ignored = url => /^(chrome|chrome-extension|devtools):/.test(url);
  const adopt = async tab => {
    try {
      if (ignored(tab.url || tab.pendingUrl || '')) return;
      const [group] = await chrome.tabGroups.query({windowId: tab.windowId});
      if (!group || tab.groupId === group.id) return;
      await chrome.tabs.group({groupId: group.id, tabIds: [tab.id]});
    } catch (e) { /* tab closed mid-adoption */ }
  };
  if (!globalThis.__showAndTellTabAdopter) {
    globalThis.__showAndTellTabAdopter = true;
    chrome.tabs.onCreated.addListener(adopt);
    chrome.tabs.onUpdated.addListener((id, info, tab) => { if (info.url) adopt(tab); });
  }
  for (let attempt = 0; ; attempt++) {
    try {
      const [group] = await chrome.tabGroups.query({});
      if (!group) return {error: 'no recording tab group'};
      const tabs = await chrome.tabs.query({windowId: group.windowId});
      const stray = tabs.filter(t => t.groupId !== group.id
                                     && !ignored(t.url || t.pendingUrl || ''));
      if (stray.length)
        await chrome.tabs.group({groupId: group.id, tabIds: stray.map(t => t.id)});
      return {added: stray.length};
    } catch (e) {
      if (attempt >= 2) return {error: String(e)};
      await new Promise(r => setTimeout(r, 500));
    }
  }
})()
"""


def open_claude_panel(cdp_port: int, app_url: str, out=print) -> None:
    """Produce the two effects of a human toolbar-icon click — the Claude-
    managed tab group (what Teach records into) and the docked side panel on
    the app tab — with no human, then adopt every other open application tab
    (and, via service-worker listeners, tabs opened mid-demonstration) into
    that group so a multi-tab workflow is recorded whole. Hard-fails naming
    the failed stage: a run is fully unattended or it fails; there is no
    guided-click fallback.

    `app_url` is the app tab's full base (e.g. http://127.0.0.1:8090, or a remote
    WebArena host when SHOWANDTELL_<NAME>_HOST is set) — the extension queries for
    exactly that tab, so it must not assume localhost."""
    sw = await_target(cdp_port, lambda t: t["type"] == "service_worker"
                       and f"{EXT_ID}/" in t.get("url", ""))
    if sw is None:
        raise RuntimeError(
            "Claude extension service worker not found — is the extension "
            "installed in the managed profile? (showAndTell browser setup)")
    r = cdp_eval(sw, DISPATCH_ICON_CLICK_JS % app_url) or {}
    if r.get("error"):
        raise RuntimeError(f"cannot open the Claude panel: {r['error']} "
                           f"(expected the app at {app_url})")
    if not r.get("groups"):
        raise RuntimeError(
            "dispatching the Claude icon handler did not create a recording "
            "tab group — an extension update may have changed the handler; "
            "Teach would record nothing, so failing the run")
    out("  ✓ recording tab group created (icon handler dispatched in the extension)")
    adopted = cdp_eval(sw, ADOPT_OPEN_TABS_JS) or {}
    if adopted.get("error"):
        raise RuntimeError(
            "cannot adopt the open application tabs into the recording tab "
            f"group: {adopted['error']} — Teach records only tabs inside the "
            "group, so any other tab would go uncaptured")
    if adopted.get("added"):
        out(f"  ✓ adopted {adopted['added']} more open tab(s) into the recording group")
    options = await_target(cdp_port, lambda t: t["type"] == "page"
                            and f"{EXT_ID}/options.html" in t.get("url", ""))
    if options is None:
        raise RuntimeError("the extension options page did not open (needed as "
                           "a page context for sidePanel.open)")
    opened = cdp_eval(options, OPEN_SIDEPANEL_JS % r["tabId"]) or {}
    if not opened.get("ok"):
        raise RuntimeError("chrome.sidePanel.open failed from the options page: "
                           f"{opened.get('error', 'no result')}")
    cdp_eval(sw, f"chrome.tabs.remove({r['optionsTabId']})")
    panel = await_target(cdp_port, lambda t: t["type"] == "page"
                          and f"{EXT_ID}/sidepanel.html" in t.get("url", ""))
    if panel is None:
        raise RuntimeError("the Claude side panel did not open after sidePanel.open")
    out("  ✓ Claude side panel docked on the app tab.")


def _ensure_recording(panel: "PanelClient") -> None:
    """Walk the panel from wherever it is to the recording state: open Teach if
    the composer is showing, click Start recording if the intro is showing.
    Claude's new iframe experience has no Teach recorder, so switch its shell
    back to Classic before looking for Teach."""
    for _ in range(30):
        t = panel.text()
        if panel.recording_active_in(t):
            return
        if (("Sign in" in t or "Log in" in t)
                and "recording" not in t.lower()):
            readiness.ensure(
                lambda: ("the Claude extension is not signed in"
                         if any(m in panel.text()
                                for m in ("Sign in", "Log in")) else None),
                "Sign in to Claude in the managed Chrome window's side "
                "panel; the run continues once you are signed in.",
                live=True, open_label="Open Claude sign-in")
        elif "Cookie settings" in t:
            # First panel open after an extension login lands under a cookie
            # consent overlay that swallows every other click.
            panel.try_click_button("Accept")
        elif "Start recording" in t:
            panel.try_click_button("Start recording")
        elif "Type / for skills" in t or "Opus 5 High" in t:
            # The new experience hides this action in its overflow menu. The
            # Radix menu item does not exist until Menu is opened, and both
            # trigger and item require trusted pointer input rather than
            # element.click(). Recording has not started yet, so CDP input is
            # safe here.
            if not panel.trusted_click_button("Switch back to classic"):
                if panel.trusted_click_button("Menu"):
                    time.sleep(0.3)
                    panel.trusted_click_button("Switch back to classic")
        else:
            panel.try_click_button("Teach Claude")
        time.sleep(1.2)
    raise RuntimeError(f"recorder did not start; panel says: {panel.text()[:200]}")


def _claude_extension_state(profile_root):
    """A readiness check: the Claude extension is present in the profile."""
    def check():
        root = Path(profile_root) / "Default" / "Extensions" / EXT_ID
        if root.is_dir() and any(root.glob("*/manifest.json")):
            return None
        return ("the Claude extension is not installed in the managed "
                "Chrome profile")
    return check


def _await_manual_done(is_recording, timeout_s: float, poll_s: float = 2) -> bool:
    """Block while the human demonstrates: the recorder shows one of several
    states ('record each step' hint, 'Done' control, 'Listening'), so completion
    is 'the recording UI has been gone for two consecutive reads' — never the
    absence of a single marker string. Returns False on timeout."""
    deadline = time.time() + timeout_s
    idle_reads = 0
    while time.time() < deadline:
        idle_reads = idle_reads + 1 if not is_recording() else 0
        if idle_reads >= 2:
            return True
        time.sleep(poll_s)
    return False


class ClaudeAdapter(ProductAdapter):
    """Claude-in-Chrome Teach: dock the side panel on the app tab, record the
    demonstration, save the generated shortcut, quiz against it."""

    name = "claude"
    display = "Claude"
    teach_label = "claude-teach"

    def __init__(self) -> None:
        self.panel: PanelClient | None = None
        self.shortcut_name = ""
        self.learned = ""

    def launch_steps(self, session):
        def launch() -> None:
            readiness.ensure(
                _claude_extension_state(session.profile_root),
                "In the ShowAndTell managed Chrome profile — not "
                "your existing Chrome profile — click 'Add to Chrome' on the "
                "Claude install page, sign in to Claude, and close that Chrome "
                "window. Then click 'Continue' here. If you navigated away, "
                "click 'Install Claude again' below.",
                session.out, live=True,
                open_url=("https://chromewebstore.google.com/detail/" + EXT_ID),
                open_label="Install Claude")
            kill_managed_chrome(session.profile_root)
            launch_managed_chrome(session.cdp_port, profile_root=session.profile_root)
            if session.mode == "trial":
                for page in list(session.connect_chrome().pages):
                    page.close()
        return [("preparing managed Chrome…", launch)]

    def arm_steps(self, session):
        if session.mode == "teach":
            return [("opening the app…", lambda: self._open_app(session)),
                    ("opening the Claude panel (no human click needed)…",
                     lambda: self._open_panel(session)),
                    ("starting Teach recording…",
                     lambda: self._start_recording(session))]
        return [("opening Claude and starting Teach…",
                 lambda: self._arm_trial(session))]

    def _open_app(self, session) -> None:
        session.connect_chrome()
        page = session.ctx.new_page()
        cdp = session.ctx.new_cdp_session(page)
        window_id = cdp.send("Browser.getWindowForTarget")["windowId"]
        cdp.send("Browser.setWindowBounds",
                 {"windowId": window_id, "bounds": window_bounds()})
        # Show the app so the panel can dock; the demonstration logs in
        # during recording (its demonstrate driver handles login per
        # fixture) — same as brackett-teach, so no opsfix-specific login here.
        page.goto(f"{session.app_url}/")
        for other in list(session.ctx.pages):
            if other is not page:
                other.close()
        page.bring_to_front()
        session.page = page

    def _open_panel(self, session) -> None:
        open_claude_panel(session.cdp_port, session.app_url, session.out)
        if not session.manual:
            session.out("  (hands-off from here — I start/stop the recording and save the "
                        "shortcut Claude generates; don't click anything in the panel)")

    def _start_recording(self, session) -> None:
        self.panel = PanelClient(f"http://127.0.0.1:{session.cdp_port}")
        if session.manual:
            _ensure_recording(self.panel)
            return
        # The shared runner selected VB-CABLE before Chrome launched, so Teach
        # opens its microphone stream on the virtual input rather than the
        # physical mic.
        _ensure_recording(self.panel)
        # Teach's input interceptor swallows synthetic input for a
        # beat while it arms (and re-arms after every navigation) —
        # a demo whose FIRST action is goto+fill loses its
        # keystrokes into that window (seen live: gitlab login
        # submitted empty fields). Let it settle before driving.
        time.sleep(3.0)

    def _arm_trial(self, session) -> None:
        open_claude_panel(session.cdp_port, session.app_url, session.out)
        self.panel = PanelClient(f"http://127.0.0.1:{session.cdp_port}")
        _ensure_recording(self.panel)
        session.page.bring_to_front()

    def demo_label(self, session) -> str:
        if session.mode == "teach" and session.manual:
            return "YOUR TURN — do the demo in the managed Chrome window"
        return super().demo_label(session)

    @contextmanager
    def demo_stage(self, session):
        if session.mode == "teach":
            session.page.bring_to_front()
        yield session.page

    def manual_teach(self, session) -> None:
        for line in session.demo_script():
            session.out(f"  - {line}")
        session.out("When the pending queue is empty, click **Done** in the Claude panel.")
        if not _await_manual_done(self.panel.recording_active,
                                  session.timeout_minutes * 60):
            raise RuntimeError("timed out waiting for the demonstration to finish")

    def conclude_steps(self, session):
        if session.mode == "teach":
            return [("waiting for Claude to digest the demonstration…",
                     lambda: self._digest(session)),
                    ("saving the shortcut Claude generated + capturing what it learned…",
                     lambda: self._save_teach_shortcut(session))]
        return [("ending Teach and saving Claude’s shortcut…",
                 lambda: self._conclude_trial(session))]

    def _digest(self, session) -> None:
        if not session.manual:
            session.out("  demonstration complete — clicking Done…")
            self.panel.click_button("Done")
            session.audio.close()
        # Clicking Done makes Claude generate the 'Create shortcut' modal;
        # its appearance is the real 'digested' signal, so proceed the moment
        # it shows. Fall back to idle+stable only if no modal appears, rather
        # than always waiting for 4 stable reads (which burned time while the
        # modal was still generating).
        last, stable = "", 0
        capture_deadline = time.time() + 300
        while time.time() < capture_deadline:
            if self.panel.shortcut_modal_open():
                break
            text = self.panel.text()
            stable = stable + 1 if (
                text == last and text and not self.panel.busy_in(text)) else 0
            if stable >= 2:
                break
            last = text
            time.sleep(3)
        (session.run_dir / "teach_transcript.txt").write_text(self.panel.text())
        self.panel.screenshot(session.run_dir / "panel.png")

    def _save_shortcut(self, session, artifact: str, error: str) -> None:
        """Save the generated shortcut and capture its Prompt — the only
        record of what Claude learned. Unique name per run: Claude silently
        refuses to save a shortcut whose name already exists (leaving the
        modal stuck), and each run re-teaches. An empty Prompt means Teach
        learned nothing. Treat that as a measured completed zero, skip the
        quiz, and keep the ordinary result artifacts."""
        self.shortcut_name = f"{session.subject_name}-{int(time.time()) % 1000000}"
        self.learned = self.panel.save_shortcut(session.out, name=self.shortcut_name)
        (session.run_dir / artifact).write_text(self.learned, encoding="utf-8")
        if not self.learned:
            reason = "claude_empty_shortcut"
            (session.run_dir / comprehend.RESPONSE_ARTIFACT_NAME).write_text(
                "", encoding="utf-8")
            session.result = comprehend.zero_result(session.task, reason)
            session.out(f"  {error}")
            session.out(
                "  Claude returned an empty shortcut — completed with score 0.")

    def _save_teach_shortcut(self, session) -> None:
        self._save_shortcut(
            session, "shortcut_prompt.txt",
            "Teach produced no shortcut prompt — the panel never "
            f"digested the recording (see {session.run_dir}/panel.png)")

    def _conclude_trial(self, session) -> None:
        self.panel.click_button("Done")
        deadline = time.time() + 300
        while time.time() < deadline and not self.panel.shortcut_modal_open():
            time.sleep(2)
        (session.run_dir / "panel.txt").write_text(self.panel.text(), encoding="utf-8")
        self.panel.screenshot(session.run_dir / "panel.png")
        self._save_shortcut(
            session, "learned.txt",
            "Teach produced no shortcut prompt, so the draft trial cannot "
            "be graded")

    def ask(self, session, message: str) -> str:
        return self.panel.ask(
            message, session.out, shortcut_name=self.shortcut_name)

    def quiz_label(self, session) -> str:
        return (f"quizzing Claude ({session.question_count} questions) "
                "against its saved shortcut to judge understanding…")

    def quiz_intro(self, session) -> str | None:
        return claude_comprehend_intro()

    def trial_metadata(self, session) -> dict:
        return {"shortcut_name": self.shortcut_name, "learned": self.learned}
