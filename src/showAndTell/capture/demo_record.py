"""Record a task's canonical demonstration from its managed Chrome page only.

This runner deliberately owns only the task fixture, ShowAndTell's managed Chrome
profile, and a Playwright CDP connection. It records the browser context itself,
not the macOS desktop, so Codex and every other application remain outside the
video and macOS Screen Recording permission is not required. The resulting
video is the source material a reviewer can watch when authoring questions.
"""
from __future__ import annotations

import datetime
import os
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from showAndTell.task_runtime import start_task
from showAndTell.capture import screenrec
from showAndTell.core import chrome
from showAndTell.core.chrome import kill_managed_chrome, launch_managed_chrome
from showAndTell.tasks import load_demonstrate, load_task


VIDEO_SIZE = {"width": 1440, "height": 900}
BEAT_HOLD_MS = 900


class BrowserVideoRecorder:
    """Finalize Playwright's page-only video when the recording context closes."""

    def __init__(self, context, video, out=print) -> None:
        self._context = context
        self._video = video
        self._out = out
        self._stopped = False

    def stop(self) -> Path | None:
        if self._stopped:
            return None
        self._stopped = True
        try:
            # Playwright writes the final WebM trailer only when its context is
            # closed. This context contains only the fixture page.
            self._context.close()
            path = Path(self._video.path())
            if path.is_file() and path.stat().st_size > 0:
                return path
        except Exception as exc:
            self._out(f"  (Chrome video finalization failed: {exc})")
        return None


def record_demo(
    task_dir: Path,
    *,
    cdp_port: int = chrome.DEFAULT_CDP_PORT,
    output_base: Path | None = None,
    profile_root: Path = chrome.MANAGED_ROOT,
    runs_root: Path = Path("runs"),
    speak: bool = True,
    out=print,
) -> Path | None:
    """Seed and replay one task while recording only its managed Chrome page.

    A successful replay replaces ``<task>/demo/recording.<webm>`` by
    default.  A failed replay never touches that canonical video; if recording
    had started, its partial evidence is retained under ``runs_root`` and the
    original exception is re-raised.
    """
    task_dir = Path(task_dir).resolve()
    task = load_task(task_dir)
    canonical_base = (Path(output_base) if output_base is not None
                      else task_dir / "demo" / "recording")
    if not canonical_base.is_absolute():
        canonical_base = Path.cwd() / canonical_base

    timestamp = f"{datetime.datetime.now():%Y%m%d-%H%M%S}"
    temp_base = task_dir / "demo" / f".recording-{os.getpid()}"
    video_dir = temp_base.parent / f"{temp_base.name}-video"
    failed_base = Path(runs_root) / f"{timestamp}-demo-record-{task.name}-FAILED-screen"

    rec = None
    fixture_owned = False
    chrome_owned = False
    replay_succeeded = False
    settled: Path | None = None

    out("1/5 starting and seeding the task applications…")
    try:
        runtime = start_task(task_dir, task_loader=load_task)
        fixture_owned = True
        creds = runtime.credentials
        app_url = runtime.app_url

        out("2/5 launching managed Chrome…")
        # This targets only ShowAndTell's dedicated profile, never the user's
        # regular Chrome and never the Codex desktop application.
        kill_managed_chrome(profile_root)
        chrome_owned = True
        launch_managed_chrome(cdp_port, profile_root=profile_root)

        out("3/5 opening the application…")
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
            video_dir.mkdir(parents=True, exist_ok=True)
            # A dedicated incognito context makes Playwright's video recorder
            # available while keeping the persistent ShowAndTell profile and every
            # non-fixture tab out of the recording.
            context = browser.new_context(
                record_video_dir=str(video_dir),
                record_video_size=VIDEO_SIZE,
                viewport=VIDEO_SIZE,
            )
            page = context.new_page()
            rec = BrowserVideoRecorder(context, page.video, out)
            # This is only a visual preview before demonstrate.py performs the
            # task's real login/navigation. Frappe v16 can abort its bare-root
            # request while rewriting /app to /desk; do not fail a recording
            # before the stable task-specific route gets a chance to open.
            try:
                page.goto(app_url, wait_until="commit")
            except PlaywrightError as exc:
                if "ERR_ABORTED" not in str(exc):
                    raise
                out("  (application preview redirected; continuing to task login)")
            page.bring_to_front()

            out("4/5 recording Chrome and replaying demonstrate.py…")
            if speak:
                out("  (Chrome-only video is visual-only; narration remains in the reviewer UI)")

            def on_step(key: str, _page, description: str) -> None:
                out(f"    [{key}] {description}")
                # Hold each completed beat long enough for a human reviewer to
                # see it; page-only capture intentionally contains no TTS audio.
                page.wait_for_timeout(BEAT_HOLD_MS)

            try:
                load_demonstrate(task_dir)(page, app_url, creds, on_step)
                replay_succeeded = True
            finally:
                # Video.path() talks to Playwright, so the recording context
                # must close before sync_playwright() tears down its event loop.
                settled = screenrec.settle(
                    rec,
                    canonical_base if replay_succeeded else failed_base,
                    out, label="Chrome recording", prune_alternates=True,
                )
                rec = None
    finally:
        # A recorder normally settles inside sync_playwright(). This fallback
        # only covers an exception between recorder creation and the inner
        # replay guard.
        if rec is not None:
            settled = screenrec.settle(
                rec, canonical_base if replay_succeeded else failed_base, out,
                label="Chrome recording", prune_alternates=True,
            )
        if chrome_owned:
            try:
                kill_managed_chrome(profile_root)
            except Exception as exc:
                out(f"  (managed Chrome cleanup failed: {exc})")
        if fixture_owned:
            try:
                runtime.close()
            except Exception as exc:
                out(f"  (fixture cleanup failed: {exc})")

    out("5/5 browser-only demonstration recorded.")
    return settled
