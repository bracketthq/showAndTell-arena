import asyncio
import io
import json
import threading
import time
from pathlib import Path

import pytest

from showAndTell.students import claude as claude_chrome
from showAndTell.quiz import comprehend
from showAndTell.students.claude import (_await_manual_done, PanelClient,
                                    open_claude_panel)


class _FakeCDP:
    """Stub for the open_claude_panel CDP seams: await_target returns queued
    ws urls in call order; cdp_eval returns queued values and records the
    (ws_url, expression) of every call for sequence assertions."""

    def __init__(self, targets: list, evals: list) -> None:
        self.targets = list(targets)
        self.evals = list(evals)
        self.eval_calls: list[tuple[str, str]] = []

    def await_target(self, cdp_port, match, timeout_s=10.0):
        return self.targets.pop(0) if self.targets else None

    def cdp_eval(self, ws_url, expr, timeout_s=30):
        self.eval_calls.append((ws_url, expr))
        return self.evals.pop(0)


def _patch_cdp(monkeypatch, fake: _FakeCDP) -> None:
    monkeypatch.setattr(claude_chrome, "await_target", fake.await_target)
    monkeypatch.setattr(claude_chrome, "cdp_eval", fake.cdp_eval)


def test_install_gate_uses_install_then_continue_workflow(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        claude_chrome.readiness, "ensure",
        lambda check, instructions, *_args, **kwargs:
            calls.append((check, instructions, kwargs)))
    monkeypatch.setattr(claude_chrome, "kill_managed_chrome", lambda *_args: None)
    monkeypatch.setattr(claude_chrome, "launch_managed_chrome", lambda *_args, **_kwargs: None)
    session = type("Session", (), {
        "profile_root": tmp_path / "showAndTell-profile",
        "cdp_port": 9223,
        "mode": "teach",
        "out": print,
    })()

    _label, launch = claude_chrome.ClaudeAdapter().launch_steps(session)[0]
    launch()

    _check, instructions, kwargs = calls[0]
    assert "Add to Chrome" in instructions
    assert "Continue" in instructions
    assert "Install Claude again" in instructions
    assert "ShowAndTell managed Chrome profile" in instructions
    assert "not your existing Chrome profile" in instructions
    assert "showAndTell browser launch" not in instructions
    assert "https://" not in instructions
    assert kwargs["open_label"] == "Install Claude"
    assert kwargs["open_url"].endswith(claude_chrome.EXT_ID)


def test_open_claude_panel_happy_path(monkeypatch):
    """The icon-click stand-in: dispatch action.onClicked in the SW (creates
    the recording group), sidePanel.open from the options page (page contexts
    honor userGesture), then remove the helper tab."""
    fake = _FakeCDP(
        targets=["ws://sw", "ws://options", "ws://panel"],
        evals=[{"groups": 1, "tabId": 42, "optionsTabId": 77},  # SW dispatch
               {"added": 0},                                    # tab adoption
               {"ok": True},                                    # sidePanel.open
               None])                                           # tabs.remove
    _patch_cdp(monkeypatch, fake)
    open_claude_panel(9223, "http://127.0.0.1:8090", out=lambda _: None)
    assert [ws for ws, _ in fake.eval_calls] == [
        "ws://sw", "ws://sw", "ws://options", "ws://sw"]
    dispatch, _adopt, open_panel, remove = (expr for _, expr in fake.eval_calls)
    assert "http://127.0.0.1:8090/*" in dispatch and "onClicked.dispatch" in dispatch
    assert "sidePanel.open({tabId: 42})" in open_panel
    assert "77" in remove


def test_open_claude_panel_targets_a_remote_app_url(monkeypatch):
    """With a remote WebArena host (SHOWANDTELL_<NAME>_HOST), the app tab is NOT on
    127.0.0.1 — the extension must query for the real host, or it never finds the
    tab ('app tab not found')."""
    fake = _FakeCDP(
        targets=["ws://sw", "ws://options", "ws://panel"],
        evals=[{"groups": 1, "tabId": 42, "optionsTabId": 77}, {"added": 0},
               {"ok": True}, None])
    _patch_cdp(monkeypatch, fake)
    open_claude_panel(9223, "http://203.0.113.10:7770", out=lambda _: None)
    dispatch = fake.eval_calls[0][1]
    assert "http://203.0.113.10:7770/*" in dispatch
    assert "127.0.0.1" not in dispatch  # no hardcoded localhost


def test_open_claude_panel_adopts_every_open_tab(monkeypatch):
    """Teach records only tabs inside the Claude-managed group, and the icon
    handler wraps only the dispatched app tab. The panel opener must sweep the
    other open tabs into the group and install the service-worker adopter for
    tabs a demonstration opens mid-recording (a CDP-created tab has no opener,
    so it never inherits the group) — otherwise a two-tab demo records one tab."""
    fake = _FakeCDP(
        targets=["ws://sw", "ws://options", "ws://panel"],
        evals=[{"groups": 1, "tabId": 42, "optionsTabId": 77},  # SW dispatch
               {"added": 1},                                    # tab adoption
               {"ok": True},                                    # sidePanel.open
               None])                                           # tabs.remove
    _patch_cdp(monkeypatch, fake)
    lines = []
    open_claude_panel(9223, "http://127.0.0.1:8090", out=lines.append)
    assert [ws for ws, _ in fake.eval_calls] == [
        "ws://sw", "ws://sw", "ws://options", "ws://sw"]
    adopt = fake.eval_calls[1][1]
    assert "tabs.group" in adopt          # sweeps already-open stray tabs
    assert "onCreated" in adopt and "onUpdated" in adopt  # adopts mid-demo tabs
    assert any("open tab" in line for line in lines)


def test_open_claude_panel_fails_when_open_tabs_cannot_join_group(monkeypatch):
    """A second application tab outside the group silently truncates the
    recording — hard-fail naming the stage, like every other stage."""
    fake = _FakeCDP(targets=["ws://sw"],
                    evals=[{"groups": 1, "tabId": 42, "optionsTabId": 77},
                           {"error": "No current window"}])
    _patch_cdp(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="uncaptured"):
        open_claude_panel(9223, "http://127.0.0.1:8090", out=lambda _: None)


def test_open_claude_panel_fails_without_service_worker(monkeypatch):
    _patch_cdp(monkeypatch, _FakeCDP(targets=[None], evals=[]))
    with pytest.raises(RuntimeError, match="service worker"):
        open_claude_panel(9223, "http://127.0.0.1:8090", out=lambda _: None)


def test_open_claude_panel_fails_when_app_tab_missing(monkeypatch):
    fake = _FakeCDP(targets=["ws://sw"], evals=[{"error": "app tab not found"}])
    _patch_cdp(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="app tab"):
        open_claude_panel(9223, "http://127.0.0.1:8090", out=lambda _: None)


def test_open_claude_panel_fails_when_no_group_appears(monkeypatch):
    """Group creation is what makes Teach record anything (a panel without the
    group was the empty-transcript bug) — a dispatch that doesn't produce the
    group must hard-fail, not proceed to a recording that captures nothing."""
    fake = _FakeCDP(targets=["ws://sw"],
                    evals=[{"groups": 0, "tabId": 42, "optionsTabId": 77}])
    _patch_cdp(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="tab group"):
        open_claude_panel(9223, "http://127.0.0.1:8090", out=lambda _: None)


def test_open_claude_panel_fails_when_sidepanel_open_rejected(monkeypatch):
    fake = _FakeCDP(targets=["ws://sw", "ws://options"],
                    evals=[{"groups": 1, "tabId": 42, "optionsTabId": 77},
                           {"added": 0},
                           {"error": "Error: user gesture required"}])
    _patch_cdp(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="sidePanel.open"):
        open_claude_panel(9223, "http://127.0.0.1:8090", out=lambda _: None)


def test_open_claude_panel_fails_when_panel_never_shows(monkeypatch):
    fake = _FakeCDP(targets=["ws://sw", "ws://options", None],
                    evals=[{"groups": 1, "tabId": 42, "optionsTabId": 77},
                           {"added": 0}, {"ok": True}, None])
    _patch_cdp(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="side panel did not open"):
        open_claude_panel(9223, "http://127.0.0.1:8090", out=lambda _: None)


def test_panel_client_waits_through_transient_target_replacement(monkeypatch):
    responses = iter([
        [],
        [{"type": "page",
          "url": f"chrome-extension://{claude_chrome.EXT_ID}/sidepanel.html",
          "webSocketDebuggerUrl": "ws://panel"}],
    ])
    monkeypatch.setattr(
        claude_chrome.urllib.request, "urlopen",
        lambda _url: io.StringIO(json.dumps(next(responses))),
    )
    monkeypatch.setattr(claude_chrome.time, "sleep", lambda _seconds: None)

    assert PanelClient("http://127.0.0.1:9223")._ws_url() == "ws://panel"


def test_panel_client_drives_cookie_dialog_inside_claude_iframe():
    """The first-run cookie dialog lives in a cross-origin claude.ai iframe.
    The extension document is blank, so panel automation must select the
    iframe for both text reads and button clicks."""
    class FramedPanel(PanelClient):
        def __init__(self):
            self.accepted = False

        def _iframe_target_ids(self):
            return ["claude-cic-frame"]

        def _evaluate_on_main(self, expr):
            return "" if "innerText" in expr else {"found": False, "buttons": []}

        def _evaluate_on_iframe(self, target_id, expr):
            assert target_id == "claude-cic-frame"
            if "innerText" in expr:
                return "Cookie settings\nCustomize\nReject\nAccept"
            if "const name" in expr and "Accept" in expr:
                self.accepted = True
                return {"found": True}
            return {"found": False, "buttons": []}

    panel = FramedPanel()

    assert "Cookie settings" in panel.text()
    assert panel.try_click_button("Accept") is True
    assert panel.accepted is True


def test_panel_client_can_click_classic_switch_outside_selected_iframe():
    """The iframe contains more text and is selected for reads, but the mode
    switch is a sibling control in the extension shell."""
    class FramedPanel(PanelClient):
        def _iframe_target_ids(self):
            return ["new-experience"]

        def _evaluate_on_main(self, expr):
            if "Switch back to classic" in expr:
                return {"found": True}
            return "shell" if "innerText" in expr else {"found": False}

        def _evaluate_on_iframe(self, target_id, expr):
            assert target_id == "new-experience"
            return ("How can I help you today?\nType / for skills\nOpus 5 High"
                    if "innerText" in expr else {"found": False})

    panel = FramedPanel("http://127.0.0.1:9223")

    assert "Type / for skills" in panel.text()  # iframe is the read context
    assert panel.try_click_button("Switch back to classic") is True


def test_button_lookup_includes_overflow_menu_items():
    assert "[role=menuitem]" in claude_chrome.CLICK_BY_NAME_JS


def test_trusted_click_dispatches_pointer_to_shell_control():
    class Panel(PanelClient):
        calls = []

        def _evaluate_on_main(self, expr):
            assert "role=menuitem" in expr
            return {"found": True, "x": 12.5, "y": 24.5}

        def _rpc(self, method, params, timeout_s=90):
            self.calls.append((method, params))
            return {}

    panel = Panel("http://127.0.0.1:9223")

    assert panel.trusted_click_button("Menu") is True
    assert [params["type"] for _, params in panel.calls] == [
        "mousePressed", "mouseReleased"
    ]
    assert all(params["x"] == 12.5 and params["y"] == 24.5
               for _, params in panel.calls)


def test_trusted_click_dispatches_pointer_sequence_inside_iframe():
    class Panel(PanelClient):
        def _evaluate_on_main(self, expr):
            return {"found": False}

        def _iframe_target_ids(self):
            return ["new-experience"]

        def _evaluate_on_iframe(self, target_id, expr):
            assert target_id == "new-experience"
            assert "PointerEvent('pointerdown'" in expr
            assert "Switch back to classic" in expr
            return True

    panel = Panel("http://127.0.0.1:9223")

    assert panel.trusted_click_button("Switch back to classic") is True


def test_panel_client_explicitly_discovers_only_claude_cic_iframes(monkeypatch):
    calls = []

    def rpc(ws_url, method, params):
        calls.append((ws_url, method, params))
        return {"targetInfos": [
            {"type": "iframe", "url": "https://claude.ai/cic/new?surface=cic_sidepanel",
             "targetId": "panel-frame"},
            {"type": "iframe", "url": "https://example.com/embedded",
             "targetId": "unrelated-frame"},
            {"type": "page", "url": "https://claude.ai/cic/new",
             "targetId": "claude-tab"},
        ]}

    monkeypatch.setattr(claude_chrome, "cdp_rpc", rpc)
    panel = PanelClient("http://127.0.0.1:9223")
    monkeypatch.setattr(panel, "_browser_ws_url", lambda: "ws://browser")

    assert panel._iframe_target_ids() == ["panel-frame"]
    assert calls == [("ws://browser", "Target.getTargets",
                      {"filter": [{"type": "iframe", "exclude": False}]})]


def test_rpc_timeout_bounds_hung_panel():
    """A panel that accepts the websocket but never answers the RPC must not
    hang _rpc past its timeout (regression: TimeoutError used to unwind into a
    blocking ThreadPoolExecutor shutdown that waited on the hung recv forever)."""
    import websockets.sync.server as ws_server

    def silent(conn):  # accept, read, never reply
        try:
            for _ in conn:
                pass
        except Exception:
            pass

    server = ws_server.serve(silent, "127.0.0.1", 0)
    port = server.socket.getsockname()[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = PanelClient("http://127.0.0.1:0")
        client._ws_url = lambda: f"ws://127.0.0.1:{port}"
        returned = threading.Event()

        def call():
            with pytest.raises((TimeoutError, asyncio.TimeoutError)):
                client._rpc("Runtime.evaluate", {}, timeout_s=1)
            returned.set()

        threading.Thread(target=call, daemon=True).start()
        assert returned.wait(15), "_rpc hung past its timeout"
    finally:
        server.shutdown()


def test_reply_has_answer_gates_on_the_landed_answer():
    # panel.ask must not settle before Claude's answer renders. The quiz prompt
    # (echoed in the panel) itself contains 'A1: <ans>' before its tail, so only
    # text AFTER the prompt counts as the answer.
    prompt = ("Reply on ONE line, exactly this format: A1: <ans> ;; A2: <ans> . "
              "Questions: (1) Which account? (2) a distinctive final question to anchor the tail")
    tail = prompt[-45:]
    assert not comprehend.reply_has_answer(f"panel {prompt}  Thinking…", tail)
    assert comprehend.reply_has_answer(f"panel {prompt}  A1: byteblaze ;; A2: open issues", tail)


def test_await_manual_done_waits_out_recording_states():
    """The human demo wait must not end while the recorder is active in ANY of
    its states ('Listening'/'Done' show without 'record each step'); it ends
    only once the recording UI has been gone for two consecutive reads."""
    reads = iter([True, True, True, False, False])
    assert _await_manual_done(lambda: next(reads), timeout_s=5, poll_s=0)


def test_await_manual_done_times_out():
    assert not _await_manual_done(lambda: True, timeout_s=0.2, poll_s=0.02)


class _FakeModalPanel(PanelClient):
    """save_shortcut's surface, scripted: the modal's Name slot keeps showing
    'Generating...' for the first `generating_polls` text() reads; Create only
    lands if nothing was typed while generating; Cancel/Escape close (or not)
    per flags."""

    def __init__(self, generating_polls=0, cancel_closes=True, escape_closes=True,
                 real_dialog=True):
        self.generating_polls = generating_polls
        self.cancel_closes = cancel_closes
        self.escape_closes = escape_closes
        self.real_dialog = real_dialog
        self.reads = 0
        self.modal_open = True
        self.typed_while_generating = False
        self.saved = False

    def _generating(self) -> bool:
        return self.reads <= self.generating_polls

    def text(self) -> str:
        self.reads += 1
        base = "Create shortcut Cancel Prompt"
        return f"{base} Generating..." if self._generating() else base

    def evaluate(self, expr):
        if "Create shortcut/.test" in expr:      # shortcut_modal_open probe
            return self.modal_open
        if "prompt" in expr and "submitDisabled" in expr:  # MODAL_CAPTURE_JS
            return {"real": self.modal_open and self.real_dialog,
                    "modalText": self.text() if self.real_dialog else "",
                    "prompt": "learned workflow", "submitDisabled": False}
        if "summarize" in expr:                  # focus the Name input
            return True
        if "Create shortcut" in expr:            # CLICK_LAST_JS submit
            if not self.typed_while_generating:
                self.saved = True
                self.modal_open = False
            return True
        return None

    def _rpc(self, method, params, timeout_s=90):
        if method == "Input.insertText":
            if self._generating():
                self.typed_while_generating = True  # generation wipes the name
        elif method == "Input.dispatchKeyEvent" and params.get("key") == "Escape":
            if self.escape_closes:
                self.modal_open = False
        return {}

    def try_click_button(self, name):
        if name == "Cancel" and self.cancel_closes:
            self.modal_open = False
        return True


@pytest.fixture()
def _no_sleep(monkeypatch):
    monkeypatch.setattr(claude_chrome.time, "sleep", lambda s: None)


def test_save_shortcut_waits_out_name_generation(_no_sleep):
    # Typing while the modal's auto-name is still 'Generating...' gets wiped
    # and the save silently fails — save_shortcut must wait it out.
    panel = _FakeModalPanel(generating_polls=3)

    prompt = panel.save_shortcut(out=lambda *_: None, name="my-shortcut")

    assert prompt == "learned workflow"
    assert panel.saved is True
    assert panel.typed_while_generating is False


def test_save_shortcut_waits_for_prompt_content_to_stabilize(monkeypatch):
    panel = _FakeModalPanel()
    sleeps = []
    monkeypatch.setattr(claude_chrome.time, "sleep", sleeps.append)

    prompt = panel.save_shortcut(out=lambda *_: None, name="my-shortcut")

    assert prompt == "learned workflow"
    assert sleeps.count(5) == 12  # one full stable minute after generation
    assert 120 not in sleeps
    assert panel.saved is True


def test_save_shortcut_recognizes_a_stably_empty_form(_no_sleep):
    class EmptyPanel(_FakeModalPanel):
        def evaluate(self, expr):
            if "Create shortcut/.test" in expr:
                return self.modal_open
            if "prompt" in expr and "submitDisabled" in expr:
                self.reads += 1
                return {"real": self.modal_open, "modalText": "Name Prompt",
                        "name": "", "prompt": "", "submitDisabled": True}
            return super().evaluate(expr)

    panel = EmptyPanel()
    output = []

    prompt = panel.save_shortcut(out=output.append, name="unused")

    assert prompt == ""
    assert panel.modal_open is False
    assert panel.saved is False
    assert any("empty shortcut" in line for line in output)


def test_empty_shortcut_sets_a_completed_zero_result(tmp_path):
    quiz_dir = tmp_path / "quiz"
    quiz_dir.mkdir()
    (quiz_dir / "questions.json").write_text(json.dumps({"questions": [
        {"id": "q1", "type": "closed", "question": "Which queue?",
         "answer_aliases": ["returns"]},
        {"id": "q2", "type": "rubric", "question": "Explain the rule",
         "rubric": "Mentions the refund window."},
    ]}))
    task = type("Task", (), {"dir": tmp_path, "name": "empty-shortcut"})()
    output = []
    session = type("Session", (), {
        "subject_name": "empty-shortcut", "run_dir": tmp_path,
        "task": task, "out": output.append, "result": None,
    })()
    adapter = claude_chrome.ClaudeAdapter()
    adapter.panel = type("Panel", (), {
        "save_shortcut": lambda _self, _out, name: "",
    })()

    adapter._save_shortcut(session, "shortcut_prompt.txt", "No shortcut prompt.")

    assert session.result["status"] == "complete"
    assert session.result["score"] == 0.0
    assert session.result["completion_reason"] == "claude_empty_shortcut"
    assert all(not item["ok"] for item in session.result["per_question"])
    assert (tmp_path / "shortcut_prompt.txt").read_text() == ""
    assert (tmp_path / comprehend.RESPONSE_ARTIFACT_NAME).read_text() == ""
    assert any("completed with score 0" in line for line in output)


def test_save_shortcut_escape_fallback_dismisses_stuck_modal(_no_sleep):
    panel = _FakeModalPanel(generating_polls=10 ** 6, cancel_closes=False)

    prompt = panel.save_shortcut(out=lambda *_: None, name="x")

    assert prompt == "learned workflow"
    assert panel.modal_open is False  # Escape closed it; composer usable


def test_save_shortcut_raises_when_modal_cannot_be_dismissed(_no_sleep):
    # A modal that survives Cancel AND Escape would swallow every quiz message
    # (empty answers cached as a bogus 0.0) — fail loudly instead.
    panel = _FakeModalPanel(generating_polls=10 ** 6,
                            cancel_closes=False, escape_closes=False)

    with pytest.raises(RuntimeError, match="dismiss"):
        panel.save_shortcut(out=lambda *_: None, name="x")


def test_save_shortcut_tolerates_modal_like_prose(_no_sleep):
    # shortcut_modal_open() is a text heuristic: chat prose quoting the Teach
    # UI ('Create shortcut… Cancel… Prompt') satisfies it with no dialog
    # element, and prose never dismisses — that must NOT raise mid-quiz.
    panel = _FakeModalPanel(generating_polls=10 ** 6, cancel_closes=False,
                            escape_closes=False, real_dialog=False)

    prompt = panel.save_shortcut(out=lambda *_: None, name="x")  # no raise

    assert prompt == "learned workflow"


def test_save_shortcut_ignores_generating_in_chat_transcript(_no_sleep):
    # 'Generating' in the CHAT (e.g. a workflow about 'Generating reports')
    # must not stall the save — only the dialog's own text gates typing.
    class ChattyPanel(_FakeModalPanel):
        def text(self) -> str:
            self.reads += 1
            return "chat says: Generating reports workflow Create shortcut Cancel Prompt"

        def evaluate(self, expr):
            if "Create shortcut/.test" in expr:
                return self.modal_open
            if "prompt" in expr and "submitDisabled" in expr:
                self.reads += 1
                return {"real": self.modal_open, "modalText": "Name Prompt",
                        "prompt": "learned workflow", "submitDisabled": False}
            if "summarize" in expr:
                return True
            if "Create shortcut" in expr:
                self.saved = True
                self.modal_open = False
                return True
            return None

    chatty = ChattyPanel()
    prompt = chatty.save_shortcut(out=lambda *_: None, name="x")

    assert prompt == "learned workflow"
    assert chatty.saved is True
    # It broke out of the initial 90-poll generation wait immediately; the
    # remaining reads are the intentional Prompt-stability observations.
    assert chatty.reads < 60


def test_comprehend_intro_frames_attached_shortcut_as_read_only():
    intro = claude_chrome.claude_comprehend_intro()
    assert "read-only comprehension check" in intro
    assert "native shortcut chip attached" in intro
    assert "created by Claude" in intro
    assert "not any webpage content" in intro
    assert "hypothetical inputs" in intro
    assert "UNKNOWN" in intro
    assert "brief answers" in intro


def test_adapter_quiz_invokes_saved_shortcut_without_inlining_prompt():
    class Panel:
        call = None

        def ask(self, message, out, shortcut_name=None):
            self.call = (message, shortcut_name)
            return "A1: £50"

    class Session:
        out = lambda *_: None

    adapter = claude_chrome.ClaudeAdapter()
    adapter.panel = Panel()
    adapter.shortcut_name = "returns-123"
    adapter.learned = "Direct approval limit: £50"

    assert adapter.ask(Session(), "Questions: (1) What is the limit?") == "A1: £50"
    sent, shortcut_name = adapter.panel.call
    assert sent == "Questions: (1) What is the limit?"
    assert "Direct approval limit: £50" not in sent
    assert shortcut_name == "returns-123"


def test_shortcut_plan_is_approved_when_present():
    class Panel(PanelClient):
        def __init__(self):
            self.clicked = []

        def try_click_button(self, name):
            self.clicked.append(name)
            return True

    output = []
    panel = Panel()
    assert panel._approve_plan_if_present(output.append)
    assert panel.clicked == ["Approve plan"]
    assert output == ["  approving Claude's shortcut plan…"]



def test_ensure_recording_dismisses_first_run_consent_dialogs(monkeypatch):
    """After a fresh extension login the panel opens under a Cookie settings
    consent overlay; the walker must accept it to reach Teach instead of
    burning all its attempts clicking buttons the overlay swallows."""
    monkeypatch.setattr(claude_chrome.time, "sleep", lambda _s: None)

    class Panel:
        state = "cookies"
        clicks = []

        def text(self):
            return {
                "cookies": "Cookie settings\nWe use cookies…\n"
                           "Customize\nReject\nAccept",
                "composer": "Type / for commands\nAsk before acting",
                "teach": "Start recording",
                "recording": "Listening",
            }[self.state]

        def recording_active_in(self, t):
            return "Listening" in t

        def try_click_button(self, name):
            self.clicks.append(name)
            self.state = {"cookies": "composer", "composer": "teach",
                          "teach": "recording"}.get(self.state, self.state) \
                if name in {"Accept", "Teach Claude", "Start recording"} \
                else self.state

    panel = Panel()
    claude_chrome._ensure_recording(panel)

    assert panel.clicks[0] == "Accept"
    assert panel.state == "recording"


def test_ensure_recording_switches_new_experience_to_classic(monkeypatch):
    monkeypatch.setattr(claude_chrome.time, "sleep", lambda _s: None)

    class Panel:
        state = "new"
        clicks = []

        def text(self):
            return {
                "new": "How can I help you today?\nType / for skills\nOpus 5 High",
                "classic": "How can I help you today?\nType / for commands",
                "teach": "Start recording",
                "recording": "Listening",
            }[self.state]

        def recording_active_in(self, text):
            return "Listening" in text

        def try_click_button(self, name):
            self.clicks.append(name)
            transitions = {
                ("new", "Switch back to classic"): "classic",
                ("classic", "Teach Claude"): "teach",
                ("teach", "Start recording"): "recording",
            }
            new_state = transitions.get((self.state, name))
            if new_state:
                self.state = new_state
                return True
            return False

        trusted_click_button = try_click_button

    panel = Panel()
    claude_chrome._ensure_recording(panel)

    assert panel.clicks[:3] == [
        "Switch back to classic", "Teach Claude", "Start recording"
    ]
    assert panel.state == "recording"


def test_ensure_recording_opens_menu_for_hidden_classic_switch(monkeypatch):
    monkeypatch.setattr(claude_chrome.time, "sleep", lambda _s: None)

    class Panel:
        state = "new"
        menu_open = False
        clicks = []

        def text(self):
            return {
                "new": "Type / for skills\nHow can I help you today?\nOpus 5 High",
                "classic": "Type / for commands\nHow can I help you today?",
                "teach": "Start recording",
                "recording": "Listening",
            }[self.state]

        def recording_active_in(self, text):
            return "Listening" in text

        def try_click_button(self, name):
            self.clicks.append(name)
            if self.state == "new" and name == "Menu":
                self.menu_open = True
                return True
            if (self.state == "new" and self.menu_open
                    and name == "Switch back to classic"):
                self.state = "classic"
                self.menu_open = False
                return True
            if self.state == "classic" and name == "Teach Claude":
                self.state = "teach"
                return True
            if self.state == "teach" and name == "Start recording":
                self.state = "recording"
                return True
            return False

        trusted_click_button = try_click_button

    panel = Panel()
    claude_chrome._ensure_recording(panel)

    assert panel.clicks[:3] == ["Switch back to classic", "Menu",
                                "Switch back to classic"]
    assert panel.state == "recording"
