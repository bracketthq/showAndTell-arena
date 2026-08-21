"""The shared teach/trial run pipeline.

Every live product adapter (Claude Teach, Brackett Show and Tell, Codex
Record & Replay) runs the same loop — launch, seed, record, demonstrate,
digest, quiz, save — differing only in which product it drives, and each
product teaches from two sources: a benchmark task's own demonstration
(``run_teach``) or a viewer-captured draft's recorded action stream
(``run_trial``). This module owns that loop once:

- The **plan**: each run is built up front as an ordered list of
  ``(label, action)`` steps, so progress numbering (``3/7 …``) is computed
  from the plan itself and cannot drift from the steps actually run.
- The **demonstration engine** (``perform_demo``): run the task's own
  demonstration on the recorded page, narrating each step aloud so the
  product captures spoken reasoning in sync with the actions.
- The **quiz + save** phase, shared verbatim across products and sources.

Products contribute a ``ProductAdapter`` subclass in ``showAndTell.students``
supplying the launch/arm/conclude
steps and the chat transport; everything product-agnostic stays here.
"""
from __future__ import annotations

import datetime
import inspect
import json
import os
import subprocess
import time
from contextlib import ExitStack
from pathlib import Path

from showAndTell.capture import screenrec
from showAndTell.core import chrome
from showAndTell.demonstration import narration
from showAndTell.player import trial as capture_trial
from showAndTell.quiz import comprehend
from showAndTell.students.base import ProductAdapter, Session
from showAndTell.students.registry import get_student
from showAndTell.tasks import load_task, load_task_logic

from showAndTell.core import audio


# TODO(arch): demo_plan/demo_script read the invoice-3way-match seed schema
# directly, so manual teach mode can only teleprompt that task family. The
# teleprompter lines should come from the task itself (its narration script
# already carries per-step text) rather than from the shared runner.
def demo_plan(task_dir: Path) -> list[dict]:
    """Derive the demonstration actions from the demo seed + task logic."""
    seed = json.loads((task_dir / "demo" / "seed.json").read_text())
    plan = []
    with load_task_logic(task_dir) as logic:
        for inv in seed["invoices"]:
            if inv["status"] != "pending":
                continue
            po = next((p for p in seed["purchase_orders"] if p["id"] == inv["po_id"]), None)
            grns = [g for g in seed["grns"] if g["po_id"] == inv["po_id"]]
            status, reason = logic.decide(inv, po, grns)
            plan.append({"invoice": inv["id"],
                         "decision": "approve" if status == "approved" else "hold",
                         "reason": reason})
    return plan


def demo_script(task_dir: Path) -> list[str]:
    """Teleprompter lines for --manual mode."""
    return [f"{step['invoice']}: open it -> Decision "
            f"{'Approve' if step['decision'] == 'approve' else 'Hold'} / reason {step['reason']}"
            " -> Save decision -> back to Pending invoices"
            for step in demo_plan(task_dir)]


def perform_demo(page, task_dir: Path, app_url: str, out, speak: bool = True,
                 creds: dict | None = None, *, before_start=None,
                 input_hooks: dict | None = None, ops_factory=None) -> None:
    """Run the task's own demonstration on the recorded page, narrating each
    step aloud so the product captures voice + clicks.

    Captured human tasks replay the original recording's embedded audio and
    install its clock at the generated driver's first paced action, after setup
    and login. Other tasks retain the per-step TTS path. ``input_hooks`` and
    ``ops_factory`` let Codex keep every gesture on its real OS-input plane.
    """
    from showAndTell.bundles import hub
    from showAndTell.core import tts
    from showAndTell.tasks import load_demonstrate_module

    # published bundles carry the __SHOWANDTELL_HOST__ placeholder in their
    # driver; map those origins onto the deployment's fixture host
    driver_path = Path(task_dir) / "demonstrate.py"
    driver_source = driver_path.read_text("utf-8") if driver_path.is_file() else ""
    module = load_demonstrate_module(
        Path(task_dir),
        url_replacements=hub.origin_replacements(driver_source) or None)
    demonstrate = module.demonstrate
    replay = getattr(module, "_REPLAY", None)
    parameters = inspect.signature(demonstrate).parameters

    if input_hooks is not None:
        required = {"locator_wrapper", "type_text"}
        if isinstance(replay, dict) and required.issubset(replay):
            replay.update(input_hooks)
        elif "ops" not in parameters:
            raise RuntimeError(
                "task demonstration supports neither captured nor legacy OS input")

    recording = narration.recorded_narration_path(task_dir) if speak else None
    narrate = None
    player = None

    def tts_narrator():
        voice = tts.voice_args()
        if voice is None:
            out("  (no TTS voice — demonstrating without narration)")
        return narration.make_narrator(Path(task_dir), voice, out)

    if speak and recording is None:
        narrate = tts_narrator()

    def start_replay_clock() -> None:
        nonlocal player, narrate
        if before_start is not None:
            before_start()
        if recording is not None:
            out(f"  playing original human narration from {recording.name}")
            player = audio.start_recorded_narration(recording, out)
            if player is None:
                narrate = tts_narrator()
        if isinstance(replay, dict):
            replay["start"] = time.monotonic()

    if recording is not None:
        if isinstance(replay, dict) and "on_start" in replay:
            replay["start"] = None
            replay["on_start"] = start_replay_clock
        else:
            start_replay_clock()
            out("  (this capture predates recorded pacing; actions may not align with audio)")
    elif before_start is not None:
        before_start()

    if creds is None:
        from showAndTell.applications.registry import default_registry
        task = load_task(task_dir)
        creds = dict(default_registry().manifest(task.primary_application).credentials)

    def on_step(key: str, current_page, description: str) -> None:
        if narrate is not None:
            narrate(key, current_page, description)
        else:
            out(f"  {description}")

    kwargs = {}
    if "ops" in parameters and ops_factory is not None:
        kwargs["ops"] = ops_factory()
    try:
        demonstrate(page, app_url, creds, on_step, **kwargs)
    finally:
        if player is not None and player.poll() is None:
            player.terminate()
            try:
                player.wait(timeout=5)
            except subprocess.TimeoutExpired:
                player.kill()


def _seed_steps(session: Session) -> list:
    if session.mode == "teach":
        def start_fixture() -> None:
            from showAndTell.task_runtime import start_task
            session.runtime = start_task(
                session.source_dir, task_loader=load_task,
            )
            session.stack.callback(session.runtime.close)
            session.app_url = session.runtime.app_url
            session.credentials = session.runtime.credentials
            session.runtime.write_manifest(session.run_dir)
        return [("starting the fixture with the demo data…", start_fixture)]

    def open_applications() -> None:
        session.page = capture_trial.open_trial_surfaces(
            session.ctx, session.capture, session.out)[0]

    steps = [("opening the selected applications…", open_applications)]
    if capture_trial.has_state_snapshot(session.capture):
        steps.append(("seeded state already restored from the draft snapshot",
                      lambda: None))
    else:
        steps.append(("rebuilding setup state…",
                      lambda: capture_trial.restore_generated_seed(
                          session.page, session.source_dir, session.capture,
                          session.out)))
    return steps


def _demonstrate(session: Session, adapter: ProductAdapter) -> None:
    if session.mode == "teach":
        if session.manual:
            adapter.manual_teach(session)
            return
        with adapter.demo_stage(session) as page:
            perform_demo(page, session.source_dir, session.app_url, session.out,
                         creds=session.credentials, **adapter.demo_kwargs(session))
        return

    with adapter.demo_stage(session) as page:
        capture_trial.replay_generated(
            page, session.source_dir, session.capture, session.out,
            **adapter.demo_kwargs(session))


def _quiz(session: Session, adapter: ProductAdapter) -> None:
    session.result = comprehend.run(
        session.task, lambda message: adapter.ask(session, message), session.out,
        intro=adapter.quiz_intro(session),
        response_artifact=session.run_dir / comprehend.RESPONSE_ARTIFACT_NAME,
    )
    _persist_result(session, adapter)


def _save_trial_result(result_dir: Path, product: str, result: dict, **metadata) -> None:
    """Persist the same comprehension payload used by ordinary benchmark runs.

    ``result.json`` remains the trial-completion manifest used by older drafts,
    but now also carries every standard result field.  ``comprehend.json`` is
    deliberately byte-for-schema compatible with regular run directories so
    consumers do not need a draft-only scoring model.  Replay completion and
    grading completion are separate states: an unavailable judge must not turn
    a successfully captured product replay into a failed trial.
    """
    (result_dir / "comprehend.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest = dict(result)
    manifest.update({
        "product": product,
        "status": "complete",
        "grading_status": result.get("status", "complete"),
    })
    manifest.update(metadata)
    (result_dir / "result.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _persist_result(session: Session, adapter: ProductAdapter) -> None:
    if session.mode == "teach":
        (session.run_dir / "comprehend.json").write_text(
            json.dumps(session.result, indent=2))
    else:
        _save_trial_result(session.run_dir, adapter.name, session.result,
                           **adapter.trial_metadata(session))


def _trial_dir(draft_dir: Path, product: str) -> Path:
    path = Path(draft_dir) / "trials" / f"{datetime.datetime.now():%Y%m%d-%H%M%S}-{product}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def _build_plan(session: Session, adapter: ProductAdapter) -> list:
    """The whole run as ordered (label, action) steps. Progress numbering is
    derived from this list, so a step count can never disagree with the steps
    actually announced (the codex pipeline once counted 7 steps in a /6 run)."""
    return [
        *adapter.launch_steps(session),
        *_seed_steps(session),
        *adapter.arm_steps(session),
        (adapter.demo_label(session), lambda: _demonstrate(session, adapter)),
        *adapter.conclude_steps(session),
        (adapter.quiz_label(session), lambda: _quiz(session, adapter)),
    ]


def _execute(session: Session, plan: list) -> None:
    total = len(plan)
    for number, (label, action) in enumerate(plan, 1):
        session.out(f"{number}/{total} {label}")
        action()
        # An adapter can produce a terminal measured result before the quiz
        # (for example, Claude generated an empty shortcut). Stop cleanly and
        # let the shared runner persist that result below.
        if session.result is not None:
            break


def _run(session: Session, adapter: ProductAdapter) -> Path:
    from playwright.sync_api import sync_playwright

    plan = _build_plan(session, adapter)
    with ExitStack() as stack:
        session.stack = stack
        # Narration routing outlives playwright on unwind: adapters that must
        # route audio before Chrome launches enter it here, and adapters that
        # stop recording mid-run close it early themselves.
        session.audio = stack.enter_context(ExitStack())
        automatic_narration = (
            (session.mode == "teach" and not session.manual)
            or session.mode == "trial"
        )
        if automatic_narration and not session.audio.enter_context(
                session.narration("virtual-cable")):
            raise RuntimeError(
                f"{adapter.display} narration routing is unavailable; refusing "
                "a replay whose voice would not be captured")
        session.pw = stack.enter_context(sync_playwright())
        _execute(session, plan)
        result_path = session.run_dir / "comprehend.json"
        if session.result is not None and not result_path.exists():
            _persist_result(session, adapter)
        adapter.save_artifacts(session)
    if session.result is not None:
        comprehend.print_results(session.task, session.result, adapter.name,
                                 session.out)
    return session.run_dir


def run_teach(product: str, task_dir: Path, *, cdp_port: int = 9223,
              codex_cdp_port: int = 9333, timeout_minutes: int = 20,
              manual: bool = False, wait_done=input, out=print,
              profile_root: Path = chrome.MANAGED_ROOT) -> Path:
    """Teach a product from a benchmark task's own demonstration, then quiz it."""
    adapter = get_student(product)
    task_dir = Path(task_dir).resolve()
    task = load_task(task_dir)
    run_dir = (Path("runs")
               / f"{datetime.datetime.now():%Y%m%d-%H%M%S}-{adapter.teach_label}-{task.name}")
    run_dir.mkdir(parents=True, exist_ok=True)
    session = Session(
        mode="teach", out=out, task=task, source_dir=task_dir, run_dir=run_dir,
        question_count=comprehend.question_count(task), subject_name=task.name,
        cdp_port=cdp_port, codex_cdp_port=codex_cdp_port,
        profile_root=Path(profile_root), timeout_minutes=timeout_minutes,
        manual=manual, wait_done=wait_done,
        manual_script=demo_script,
        narration_routes={"virtual-cable": audio.virtual_cable_narration},
    )
    adapter.preflight(session)
    return _run(session, adapter)


def run_trial(product: str, draft_dir: Path, *,
              cdp_port: int = 9223, codex_cdp_port: int = 9333,
              profile_root: Path | None = None, out=print) -> Path:
    """Teach a product from a viewer-captured draft's recording, then quiz it."""
    adapter = get_student(product)
    draft_dir = Path(draft_dir).resolve()
    capture = capture_trial.load_capture(draft_dir)
    if not capture_trial.generated_replay_available(draft_dir, capture):
        raise capture_trial.CaptureTrialError(
            "this draft has no generated replay (browser-mode capture); "
            "re-record it in managed mode to replay automatically")
    task = load_task(draft_dir)
    session = Session(
        mode="trial", out=out, task=task, source_dir=draft_dir,
        question_count=comprehend.question_count(task),
        subject_name=capture["name"], cdp_port=cdp_port,
        codex_cdp_port=codex_cdp_port,
        profile_root=Path(profile_root or chrome.MANAGED_ROOT),
        capture=capture,
        app_url=capture_trial.primary_origin(capture),
        credentials=dict(capture["surfaces"][0].get("credentials") or {}),
        narration_routes={"virtual-cable": audio.virtual_cable_narration},
    )
    adapter.preflight(session)
    session.run_dir = _trial_dir(draft_dir, product)
    return _run(session, adapter)


def run_capture_trial(draft_dir: Path, product: str, *,
                      cdp_port: int = 9223, codex_cdp_port: int = 9333,
                      profile_root: Path | None = None, out=print) -> Path:
    draft_dir = Path(draft_dir).resolve()
    get_student(product)  # refuse unknown products before any run artifacts exist

    trials_dir = draft_dir / "trials"
    trials_dir.mkdir(parents=True, exist_ok=True)
    existing = set(trials_dir.iterdir())
    stamp = f"{datetime.datetime.now():%Y%m%d-%H%M%S}"
    recorder = screenrec.start(
        trials_dir / f".recording-{stamp}-{product}-{os.getpid()}", out)
    result_dir = None
    try:
        result_dir = run_trial(
            product, draft_dir, cdp_port=cdp_port,
            codex_cdp_port=codex_cdp_port, profile_root=profile_root,
            out=out)
    except BaseException:
        # Product runners create their result directory before launching UI.
        # Preserve failed-run evidence there too; if failure happened even
        # earlier, create an explicit incomplete trial directory.
        candidates = [
            path for path in trials_dir.iterdir()
            if path not in existing and path.is_dir()
            and path.name.endswith(f"-{product}")
        ]
        result_dir = max(candidates, key=lambda path: path.stat().st_mtime,
                         default=None)
        if result_dir is None:
            result_dir = trials_dir / f"{stamp}-failed-{product}"
            result_dir.mkdir(exist_ok=True)
        screenrec.settle(recorder, Path(result_dir) / "screen", out)
        raise
    screenrec.settle(recorder, Path(result_dir) / "screen", out)
    return result_dir
