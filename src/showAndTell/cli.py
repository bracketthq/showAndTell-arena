"""ShowAndTell-Bench CLI."""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .task_runtime import load_task_seed, start_task
from .capture import screenrec
from .core import cache
from .quiz import comprehend
from .students import list_students
from .tasks import load_task


def _task_dir(spec: str) -> Path:
    """Resolve a task argument: local directory, or a bare published name.

    A name with no local directory is fetched from the Hugging Face dataset
    repo, so a public install can run tasks without cloning the data.
    """
    from .bundles import hub
    return hub.resolve_task_dir(spec)


def _cmd_task_serve(args) -> None:
    """Run the exact isolated application lifecycle used by recordings."""
    runtime = start_task(_task_dir(args.task))
    print(f"task:        {runtime.task.name}")
    print(f"run id:      {runtime.run_id}")
    print(f"applications: {', '.join(runtime.task.applications)}")
    print(f"app:         {runtime.app_url}")
    print(f"seed sha256: {runtime.seed.sha256}")
    print("Press Ctrl+C to destroy this run's writable state.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        runtime.close()


def _cmd_dataset_pull(args) -> None:
    from .bundles import hub
    root = hub.fetch_dataset(revision=args.revision)
    names = sorted(p.name for p in root.iterdir() if p.is_dir())
    print(f"✓ {hub.dataset_repo()} @ {root.parent.name[:12]}: "
          f"{len(names)} task(s) cached — {', '.join(names)}")


def _cmd_task_validate(args) -> None:
    task_dir = _task_dir(args.task)
    task = load_task(task_dir)
    seed = load_task_seed(task)
    print(f"✓ {task.name}: applications={list(task.applications)}; "
          f"seed {seed.sha256[:12]}")


def _cmd_build_task(args) -> None:
    subprocess.run([sys.executable, str(Path(args.task) / "build_task.py")], check=True)


def _cmd_demo_record(args) -> None:
    from .capture.demo_record import record_demo
    video = record_demo(
        _task_dir(args.task),
        cdp_port=args.cdp_port,
        output_base=Path(args.out) if args.out else None,
        speak=not args.no_narration,
    )
    if video is None:
        print("Demonstration completed, but Chrome recording was unavailable.")


def _cmd_bundle_snapshot(args) -> None:
    from .bundles.snapshot import snapshot_task
    n = snapshot_task(Path(args.task), Path(args.out))
    print(f"captured {n} beats -> {args.out}")


def _cmd_bundle_diff(args) -> None:
    from dataclasses import asdict
    from .bundles.diff import diff_bundles, print_report
    from .bundles.parse import parse_bundle
    a = parse_bundle(Path(args.bundle_a))
    b = parse_bundle(Path(args.bundle_b))
    d = diff_bundles(a, b)
    print_report(d, a)
    if args.json:
        Path(args.json).write_text(json.dumps(asdict(d), indent=2))
        print(f"wrote {args.json}")


def _cmd_browser_setup(args) -> None:
    from .core import chrome
    if args.fresh:
        import shutil
        shutil.rmtree(chrome.MANAGED_ROOT, ignore_errors=True)
        chrome.ensure_managed_profile()
        print(f"fresh managed profile '{chrome.PROFILE_NAME}' at "
              f"{chrome.MANAGED_ROOT} — launch it and log in once")
        return
    dst = chrome.clone_profile(chrome.system_chrome_root(), chrome.MANAGED_ROOT,
                               profile=args.clone_from)
    print(f"cloned Chrome profile '{args.clone_from}' as "
          f"'{chrome.PROFILE_NAME}' -> {dst}")
    print("extensions and logins came along; if a site logged you out, sign in once in the")
    print("managed browser (showAndTell browser launch) and it persists.")


def _select_cdp_port(requested: int | None, preferred: int, *,
                     unavailable=()) -> int:
    from .core import chrome
    desired = requested if requested is not None else preferred
    selected = chrome.available_cdp_port(desired, unavailable=unavailable)
    if selected != desired:
        print(f"CDP port {desired} is busy; using {selected}")
    return selected


def _cmd_browser_launch(args) -> None:
    from .core import chrome
    port = _select_cdp_port(args.port, chrome.DEFAULT_CDP_PORT)
    url = args.url
    opening_brackett_install = (
        url is None
        and not chrome.brackett_extension_installed()
    )
    if opening_brackett_install:
        url = chrome.BRACKETT_WEBSTORE_URL
    elif url is None:
        from .students.brackett import brackett_url
        url = brackett_url()
    chrome.launch(port=port, url=url)
    print(f"managed Chrome launched (profile {chrome.MANAGED_ROOT}, CDP port {port})")
    if opening_brackett_install:
        print("Brackett is not installed yet. Click 'Add to Chrome' once, then")
        print("open Brackett and complete 'Sign in'. Close the managed browser")
        print("before starting a task run.")
    elif args.url is None:
        print("If Brackett shows 'Sign in', complete it now. Task runs will also")
        print("pause here and continue automatically after authentication.")


def _warn_if_blocked(task_dir: Path) -> None:
    """Surface a task's 'blocked' status before a run so nobody expects a clean
    result from a task the organic (no-seed) sites can't feed."""
    cfg = load_task(task_dir)
    if cfg.status == "blocked":
        print(f"WARNING: task '{cfg.name}' is blocked and will not demonstrate "
              f"cleanly.\n  {cfg.status_reason}")


def _requires_ready_judge(task_dir: Path) -> bool:
    """Keep release runs strict without blocking draft-status iteration.

    Captured drafts normally use ``capture-trial``, whose replay and grading
    states are already separate.  A reviewed capture can also live under
    ``tasks/`` while its task status is still ``draft``; the viewer launches a
    normal teach command for that location.  Treat that explicit status the
    same way so a missing judge does not prevent the product replay itself.
    """
    return load_task(task_dir).status != "draft"


def _run_with_cache(adapter: str, task_dir: Path, no_cache: bool, run_fn,
                    out=print, record: bool = False,
                    judge_fingerprint: str | None = None,
                    require_judge: bool = False):
    """Reuse a cached result for (adapter, task) when the task is unchanged, else
    run the teach flow and cache its result. A teach flow that leaves no
    comprehend.json (it failed) is never cached. --no-cache forces a fresh run
    and then refreshes the cache. With record=True the whole fresh run is
    screen-recorded (screenrec); the video lands in the run dir, or under a
    -FAILED- name in runs/ when the run raises. Cached results never record."""
    if not no_cache:
        hit = cache.load(adapter, task_dir, judge_fingerprint)
        if hit is not None:
            out(f"✓ cached result for {adapter} / {hit['task']} "
                f"(task unchanged since {hit['cached_at']})")
            if hit.get("run_dir"):
                out(f"  source: {hit['run_dir']}")
            out("  re-run with --no-cache to force a fresh teach.")
            comprehend.print_results(load_task(task_dir), hit["result"], "cached", out)
            return hit
    if require_judge:
        from .quiz import judge
        try:
            judge.require_ready(live=False)
        except Exception as exc:
            out(f"! grader unavailable: {exc}")
            out("  running anyway — the result will be saved ungraded; grade "
                "it later with `showAndTell grade` or from the viewer's Results.")
    ts = f"{datetime.datetime.now():%Y%m%d-%H%M%S}"
    rec = (screenrec.start(Path("runs") / f".recording-{ts}-{os.getpid()}", out)
           if record else screenrec.NullRecorder())
    try:
        run_dir = run_fn()
    except BaseException:
        screenrec.settle(
            rec, Path("runs") / f"{ts}-{adapter}-{Path(task_dir).name}-FAILED-screen", out)
        raise
    screenrec.settle(rec, Path(run_dir) / "screen" if run_dir else None, out)
    result_path = Path(run_dir) / "comprehend.json" if run_dir else None
    if result_path and result_path.exists():
        result = json.loads(result_path.read_text())
        if result.get("status", "complete") != "complete":
            out("! result is ungraded because the judge failed; it was not cached.")
            return None
        cache.save(
            adapter, task_dir, result, run_dir,
            judge_fingerprint=judge_fingerprint)
        out("✓ result cached and run history retained; a repeat run reuses the "
            "latest result unless the task changes or you pass --no-cache.")
    return None


def _cmd_claude_teach(args) -> None:
    task_dir = _task_dir(args.task)
    _warn_if_blocked(task_dir)
    from .core import chrome
    from .teacher.run import run_teach
    from .quiz import judge
    cdp_port = _select_cdp_port(args.cdp_port, chrome.DEFAULT_CDP_PORT)
    _run_with_cache("claude-teach", task_dir, args.no_cache, lambda: run_teach(
        "claude", task_dir,
        cdp_port=cdp_port,
        timeout_minutes=args.timeout_minutes, manual=args.manual,
        profile_root=chrome.MANAGED_ROOT), record=not args.no_record,
        judge_fingerprint=judge.protocol_fingerprint(),
        require_judge=_requires_ready_judge(task_dir))


def _cmd_brackett_teach(args) -> None:
    task_dir = _task_dir(args.task)
    _warn_if_blocked(task_dir)
    from .core import chrome
    from .teacher.run import run_teach
    from .quiz import judge
    # a missing Brackett extension is the run's readiness gate's job now
    cdp_port = _select_cdp_port(args.cdp_port, chrome.DEFAULT_CDP_PORT)
    _run_with_cache("brackett-teach", task_dir, args.no_cache,
                    lambda: run_teach(
                        "brackett", task_dir,
                        cdp_port=cdp_port,
                        manual=args.manual,
                        profile_root=chrome.MANAGED_ROOT), record=not args.no_record,
                    judge_fingerprint=judge.protocol_fingerprint(),
                    require_judge=_requires_ready_judge(task_dir))


def _cmd_leaderboard(args) -> None:
    from .quiz import leaderboard
    leaderboard.build(Path(args.runs), agg=args.agg,
                      out_dir=Path(args.out) if args.out else None)


def _write_or_print(payload: dict, output: str | None) -> None:
    rendered = json.dumps(payload, indent=2) + "\n"
    if output:
        Path(output).write_text(rendered, encoding="utf-8")
        print(f"wrote {output}")
    else:
        print(rendered, end="")


def _grade_saved_response(task_path: str, response_path: str) -> dict:
    from .quiz import judge
    judge.require_ready(live=False)
    task = load_task(Path(task_path))
    quiz = json.loads(
        (task.dir / "quiz" / "questions.json").read_text())['questions']
    response = Path(response_path).read_text(encoding="utf-8")
    return comprehend.grade(task, quiz, response)


def _cmd_grade(args) -> None:
    result = _grade_saved_response(args.task, args.response)
    _write_or_print(result, args.out)
    if result.get("status") != "complete":
        raise SystemExit(2)


def _cmd_regrade(args) -> None:
    run_dir = Path(args.run)
    response = run_dir / comprehend.RESPONSE_ARTIFACT_NAME
    if not response.exists():
        raise SystemExit(f"saved response not found: {response}")
    result = _grade_saved_response(args.task, str(response))
    output = run_dir / ("comprehend.json" if args.replace
                        else "comprehend.regraded.json")
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {output}")
    if result.get("status") != "complete":
        raise SystemExit(2)
    if args.replace:
        adapter = cache.adapter_from_run_dir(run_dir)
        if adapter is None:
            print("! regraded result was not cached: the run directory name "
                  "does not identify a teach adapter")
        else:
            from .quiz import judge
            cached = cache.save(
                adapter, Path(args.task), result, run_dir,
                judge_fingerprint=judge.protocol_fingerprint(),
            )
            print(f"cached regraded result in {cached}")


def _cmd_judge_doctor(args) -> None:
    from .quiz import judge
    report = {
        "judge": judge.release_manifest(),
        "preflight": judge.require_ready(live=not args.no_call),
    }
    _write_or_print(report, args.out)


def _cmd_judge_eval(args) -> None:
    from .quiz import judge_eval
    cases = judge_eval.load_cases(
        Path(args.cases) if args.cases else judge_eval.DEFAULT_CASES)
    result = judge_eval.evaluate(cases)
    _write_or_print(result, args.out)
    if result["status"] != "pass":
        raise SystemExit(1)


def _cmd_codex_record(args) -> None:
    task_dir = _task_dir(args.task)
    _warn_if_blocked(task_dir)
    from .core import chrome
    from .teacher.run import run_teach
    from .quiz import judge
    chrome_cdp_port = _select_cdp_port(
        args.chrome_cdp_port, chrome.DEFAULT_CDP_PORT)
    codex_cdp_port = _select_cdp_port(
        args.cdp_port, 9333, unavailable={chrome_cdp_port})
    _run_with_cache("codex-record", task_dir, args.no_cache,
                    lambda: run_teach(
                        "codex", task_dir,
                        cdp_port=chrome_cdp_port,
                        codex_cdp_port=codex_cdp_port,
                        profile_root=chrome.MANAGED_ROOT),
                    record=not args.no_record,
                    judge_fingerprint=judge.protocol_fingerprint(),
                    require_judge=_requires_ready_judge(task_dir))


def _cmd_capture_trial(args) -> None:
    from .core import chrome
    from .teacher.run import run_capture_trial
    cdp_port = _select_cdp_port(args.cdp_port, chrome.DEFAULT_CDP_PORT)
    codex_cdp_port = _select_cdp_port(
        args.codex_cdp_port, 9333, unavailable={cdp_port})
    result = run_capture_trial(
        Path(args.draft), args.product,
        cdp_port=cdp_port, codex_cdp_port=codex_cdp_port,
        profile_root=chrome.MANAGED_ROOT)
    print(f"capture trial saved to {result}")


def _cmd_fixture_host(args) -> None:
    from showAndTell.applications.host import server

    server.run_agent(
        args.host, args.port,
        Path(args.root) if args.root else server.DEFAULT_ROOT,
        Path(args.token_file) if args.token_file else server.DEFAULT_TOKEN_PATH,
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="showAndTell")
    sub = parser.add_subparsers(required=True)

    p = sub.add_parser(
        "fixture-host",
        help="run the fixture host agent (container lifecycle over HTTP)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8091)
    p.add_argument("--root", default=None,
                   help="state root (default ~/.showAndTell/fixture-host)")
    p.add_argument("--token-file", default=None,
                   help="bearer token path (default ~/.showAndTell/fixture-host-token)")
    p.set_defaults(fn=_cmd_fixture_host)

    p = sub.add_parser(
        "task-serve",
        help="start one task in an isolated seeded application stack",
    )
    p.add_argument("--task", required=True)
    p.set_defaults(fn=_cmd_task_serve)

    p = sub.add_parser(
        "dataset-pull",
        help="download the latest published task dataset into the local "
             "cache (each revision keeps its own snapshot folder)")
    p.add_argument("--revision", default=None,
                   help="pin a tag or commit, e.g. v0.1 (default: latest)")
    p.set_defaults(fn=_cmd_dataset_pull)

    p = sub.add_parser("task-validate", help="validate a task and its owned seed")
    p.add_argument("--task", required=True)
    p.set_defaults(fn=_cmd_task_validate)

    p = sub.add_parser("build-task", help="regenerate a task's demo seed")
    p.add_argument("--task", required=True)
    p.set_defaults(fn=_cmd_build_task)

    p = sub.add_parser(
        "demo-record",
        help="record a task's browser demonstration for question reviewers",
    )
    p.add_argument("--task", required=True)
    p.add_argument("--cdp-port", type=int, default=9223)
    p.add_argument(
        "--out",
        help="output base path without an extension (default: <task>/demo/recording)",
    )
    p.add_argument(
        "--no-narration",
        action="store_true",
        help="compatibility flag; Chrome-only recordings are visual-only and narration is shown in the reviewer UI",
    )
    p.set_defaults(fn=_cmd_demo_record)

    p = sub.add_parser("browser", help="manage the Chrome profile used by product adapters")
    bsub = p.add_subparsers(required=True)
    bp = bsub.add_parser("setup", help="clone your daily profile (default) or create a fresh one")
    bp.add_argument("--clone-from", default="Default", help="source Chrome profile directory name")
    bp.add_argument("--fresh", action="store_true", help="empty profile; log in manually once")
    bp.set_defaults(fn=_cmd_browser_setup)
    bp = bsub.add_parser("launch", help="start Chrome on the managed profile with a CDP port")
    bp.add_argument("--port", type=int, default=9223)
    bp.add_argument("--url")
    bp.set_defaults(fn=_cmd_browser_launch)

    p = sub.add_parser("claude-teach",
                       help="teach Claude-in-Chrome: dock panel, start recording, you demo, capture result")
    p.add_argument("--task", required=True)
    p.add_argument("--cdp-port", type=int)
    p.add_argument("--timeout-minutes", type=int, default=20)
    p.add_argument("--manual", action="store_true",
                   help="a human performs the demonstration instead of OS-level automation")
    p.add_argument("--no-cache", action="store_true",
                   help="ignore any cached result and force a fresh teach (then refresh the cache)")
    p.add_argument("--no-record", action="store_true",
                   help="do not screen-record this run (recordings are on by default)")
    p.set_defaults(fn=_cmd_claude_teach)

    p = sub.add_parser("brackett-teach",
                       help="teach Brackett Show-and-Tell: new agent, record, demo, stop, capture")
    p.add_argument("--task", required=True)
    p.add_argument("--cdp-port", type=int)
    p.add_argument("--manual", action="store_true")
    p.add_argument("--no-cache", action="store_true",
                   help="ignore any cached result and force a fresh teach (then refresh the cache)")
    p.add_argument("--no-record", action="store_true",
                   help="do not screen-record this run (recordings are on by default)")
    p.set_defaults(fn=_cmd_brackett_teach)

    p = sub.add_parser("codex-record",
                       help="teach Codex record-and-replay: start screen recording, demo on screen, done")
    p.add_argument("--task", required=True)
    p.add_argument("--chrome-cdp-port", type=int)
    p.add_argument("--cdp-port", type=int)
    p.add_argument("--no-cache", action="store_true",
                   help="ignore any cached result and force a fresh teach (then refresh the cache)")
    p.add_argument("--no-record", action="store_true",
                   help="do not screen-record this run (recordings are on by default)")
    p.set_defaults(fn=_cmd_codex_record)

    p = sub.add_parser(
        "capture-trial",
        help="replay a captured viewer draft into Claude, Brackett, or Codex",
    )
    p.add_argument("--draft", required=True)
    p.add_argument(
        "--product", required=True, choices=list_students())
    p.add_argument("--cdp-port", type=int)
    p.add_argument("--codex-cdp-port", type=int)
    p.set_defaults(fn=_cmd_capture_trial)

    p = sub.add_parser("leaderboard",
                       help="aggregate runs/*/comprehend.json into a product×task board")
    p.add_argument("--runs", default="runs", help="directory of run outputs")
    p.add_argument("--agg", choices=["latest", "best", "mean"], default="latest",
                   help="collapse repeated product×task runs by (default: latest)")
    p.add_argument("--out", default=None, help="where to write leaderboard.{md,html,json} (default: --runs dir)")
    p.set_defaults(fn=_cmd_leaderboard)

    p = sub.add_parser(
        "grade", help="grade a saved quiz response without rerunning a product")
    p.add_argument("--task", required=True)
    p.add_argument("--response", required=True)
    p.add_argument("--out")
    p.set_defaults(fn=_cmd_grade)

    p = sub.add_parser(
        "regrade", help="re-grade a run's saved response with the current judge")
    p.add_argument("--task", required=True)
    p.add_argument("--run", required=True)
    p.add_argument(
        "--replace", action="store_true",
        help="replace comprehend.json instead of writing comprehend.regraded.json")
    p.set_defaults(fn=_cmd_regrade)

    p = sub.add_parser("judge", help="inspect and evaluate the configured judge")
    jsub = p.add_subparsers(required=True)
    jp = jsub.add_parser("doctor", help="verify judge config, auth, and model access")
    jp.add_argument("--no-call", action="store_true",
                    help="check configuration/auth without a model smoke call")
    jp.add_argument("--out")
    jp.set_defaults(fn=_cmd_judge_doctor)
    jp = jsub.add_parser("eval", help="run the human-labeled judge regression suite")
    jp.add_argument("--cases", help="JSONL cases (default: packaged release suite)")
    jp.add_argument("--out")
    jp.set_defaults(fn=_cmd_judge_eval)

    p = sub.add_parser("bundle-snapshot",
                       help="capture a Show-and-Tell-shaped pseudo-bundle from a task's demonstration")
    p.add_argument("--task", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(fn=_cmd_bundle_snapshot)

    p = sub.add_parser("bundle-diff",
                       help="semantic diff between two Show-and-Tell bundles")
    p.add_argument("bundle_a")
    p.add_argument("bundle_b")
    p.add_argument("--json", help="also write the full diff as JSON to this path")
    p.set_defaults(fn=_cmd_bundle_diff)

    args = parser.parse_args()
    try:
        args.fn(args)
    except Exception as exc:
        from .core.llm import JudgeUnavailable
        from .quiz.protocol import JudgeConfigError
        if isinstance(exc, (JudgeUnavailable, JudgeConfigError)):
            raise SystemExit(f"judge unavailable: {exc}") from None
        raise


if __name__ == "__main__":  # `python -m showAndTell.cli` must behave like `showAndTell`
    main()
