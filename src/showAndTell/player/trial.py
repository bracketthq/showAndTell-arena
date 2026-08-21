"""Viewer-captured task drafts: loading, origin remapping, and replay.

Managed captures include a generated Playwright demonstration, so trials can
teach the exact recorded action stream automatically (browser-only captures
have no generated replay and cannot be trialed; re-record them in managed
mode).  This module owns the draft-side machinery — validating a captured
testcase, remapping capture-time origins onto fresh trial ports, upgrading
older generated drivers in memory, and replaying the recorded actions at their
recorded pace. The run loop that teaches a product from a draft lives in
``teacher.run``.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from showAndTell.applications.browser.runtime import auto_login, origin
from showAndTell.core import audio
from showAndTell.demonstration.compiler import compile_demonstration
from showAndTell.demonstration.model import Demonstration
from showAndTell.demonstration import narration


class CaptureTrialError(ValueError):
    pass


def load_capture(draft_dir: Path) -> dict:
    draft_dir = Path(draft_dir).resolve()
    path = draft_dir / "testcase.json"
    try:
        capture = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaptureTrialError(f"cannot read captured testcase {path}: {exc}") from exc
    if not isinstance(capture, dict) or capture.get("status") != "captured-draft":
        raise CaptureTrialError("testcase.json is not a captured draft")
    surfaces = capture.get("surfaces")
    if (not isinstance(surfaces, list) or not surfaces
            or any(not isinstance(row, dict) or not isinstance(row.get("url"), str)
                   or not row["url"].startswith(("http://", "https://"))
                   for row in surfaces)):
        raise CaptureTrialError("captured draft has no valid application surfaces")
    return capture


def _sign_in(page, surface: dict, out=print) -> None:
    """Authenticate a surface so the replay starts from a usable page.

    Generated ``seed()`` opens with the same call, but it goes on to re-create
    the task's data — which a restored state snapshot already contains — so
    authentication cannot be bundled with it.  The adapters return early when
    a session already exists, so calling this as well as seed() is harmless.
    """
    # Some recordings intentionally include the operator's login as part of
    # the visible workflow.  Signing those surfaces in here would redirect the
    # opening /login page before the recorded email/password gestures run.
    if surface.get("login_replay") == "demonstrated":
        return
    credentials = surface.get("credentials") or {}
    if not (credentials.get("email") or credentials.get("password")):
        return
    base = origin(surface["url"])
    label = surface.get("label", base)
    try:
        if auto_login(page, surface, app_url=base, credentials=credentials):
            out(f"  signed in to {label}")
    except Exception as exc:
        # Continuing would fail many lines later inside a recorded selector,
        # where the real cause is invisible.
        raise CaptureTrialError(
            f"automatic sign-in to {label} failed, so the replay cannot "
            f"start from a signed-in page: {exc}") from exc


def open_trial_surfaces(ctx, capture: dict, out=print):
    surface = capture["surfaces"][0]
    page = ctx.new_page()
    out(f"  opening {surface.get('label', surface['url'])}: {surface['url']}")
    page.goto(surface["url"], wait_until="commit")
    _sign_in(page, surface, out)
    page.bring_to_front()
    return [page]


def generated_replay_available(draft_dir: Path, capture: dict) -> bool:
    replay = capture.get("replay") or {}
    return bool(replay.get("generated") and (Path(draft_dir) / "demonstrate.py").is_file())


def primary_origin(capture: dict) -> str:
    return origin(capture["surfaces"][0]["url"])


def _surface_origins(draft_dir: Path, capture: dict) -> dict[str, str]:
    """Map capture-time origins in demonstrate.py onto fresh trial origins."""
    source = (Path(draft_dir) / "demonstrate.py").read_text(encoding="utf-8")
    captured = _captured_surfaces(source)
    live = {row.get("id"): row for row in capture.get("surfaces", [])}
    replacements: dict[str, str] = {}
    relocations: dict[str, set[tuple[str, str, int]]] = {}
    for surface_id, old in captured.items():
        current = live.get(surface_id)
        if not isinstance(old, dict) or not isinstance(current, dict):
            continue
        old_url, live_url = old.get("url"), current.get("url")
        if not isinstance(old_url, str) or not isinstance(live_url, str):
            continue
        old_parts, live_parts = urlsplit(old_url), urlsplit(live_url)
        old_origin, live_origin = origin(old_parts), origin(live_parts)
        if old_origin != live_origin:
            replacements[old_origin] = live_origin
        if (old_parts.hostname and live_parts.hostname
                and old_parts.port is not None and live_parts.port is not None):
            relocations.setdefault(old_parts.hostname, set()).add((
                live_parts.scheme,
                live_parts.hostname,
                live_parts.port - old_parts.port,
            ))
    replacements.update(_cohosted_origins(source, replacements, relocations))
    return replacements


def _cohosted_origins(source: str, replacements: dict[str, str],
                      relocations: dict[str, set[tuple[str, str, int]]]
                      ) -> dict[str, str]:
    """Follow a surface relocation for the services hosted beside it.

    A surface is one service of an application that may run several: the
    ONLYOFFICE editor the operator sees is the connector's page, but the frames
    it hosts are served by Document Server on its own port. Only surfaces are
    recorded, so when a draft replays on a different machine than it was
    captured on those neighbouring origins keep pointing at the capture
    instance, and every action inside such a frame is skipped for a frame that
    "is not present". Replica fixtures move every published port by one common
    stride, so apply that same offset to neighbouring services. Only the host
    and port stride are part of the relocation: nothing recorded says a
    neighbour's scheme followed the surface's, so it keeps its own. Explicit
    ports are required on both sides — every fixture publishes its manifest's
    port, so a portless surface records no relocation and a portless neighbour
    is left alone. When surfaces on one capture host imply different
    relocations, leave the neighbour alone instead of guessing which
    application owns it.
    """
    followed = {}
    for captured in set(re.findall(r"https?://[A-Za-z0-9._-]+(?::\d+)?", source)):
        if captured in replacements:
            continue
        parts = urlsplit(captured)
        candidates = relocations.get(parts.hostname or "", set())
        if len(candidates) != 1 or parts.port is None:
            continue
        _, live_host, port_offset = next(iter(candidates))
        live_port = parts.port + port_offset
        if not 0 < live_port <= 65535:
            continue
        followed[captured] = f"{parts.scheme}://{live_host}:{live_port}"
    return followed


def _captured_surfaces(source: str) -> dict:
    captured: dict = {}
    try:
        tree = ast.parse(source)
        for node in tree.body:
            if (isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name)
                            and target.id == "_CAPTURED_SURFACES"
                            for target in node.targets)):
                value = ast.literal_eval(node.value)
                if isinstance(value, dict):
                    captured = value
                break
    except (SyntaxError, ValueError):
        return {}
    return captured


def _read_events(path: Path) -> list[dict]:
    rows = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _driver_source(draft_dir: Path) -> str | None:
    """Upgrade an older generated driver in memory.

    Older drafts predate the seed/record split, older ones the recorded
    pacing that keeps a replay in step with its narration audio, older ones
    the tab activation that keeps the acted-on page visible in a multi-app
    replay, older ones still the frame match that survives replaying on a
    different install of an embedded editor than the capture used, and the
    oldest the readiness gate that stops the first gesture landing in an
    application that is still starting. All are recovered by re-rendering the
    captured events rather than rewriting the file on disk.
    """
    source = (draft_dir / "demonstrate.py").read_text(encoding="utf-8")
    # A current driver binds its replay state through showAndTell.player.replay, where
    # the selector ladder and recovery logic now live — so that one line is
    # the whole contract, and byte-equality tests keep committed drivers in
    # step with the renderer. Anything older is upgraded from its events.
    if "_REPLAY = _replay.new_replay_state()" in source and "def seed(" in source:
        return None
    setup = _read_events(draft_dir / "demo" / "seed_events.jsonl")
    recorded = _read_events(draft_dir / "events.jsonl")
    surfaces = list(_captured_surfaces(source).values())
    if not setup or not recorded or not surfaces:
        return None
    return compile_demonstration(Demonstration.from_values(
        events=recorded, surfaces=surfaces, setup_events=setup))


def _load_replay_module(draft_dir: Path, capture: dict):
    from showAndTell.tasks import load_demonstrate_module
    return load_demonstrate_module(
        draft_dir,
        url_replacements=_surface_origins(draft_dir, capture),
        source_override=_driver_source(draft_dir),
    )


def _recording_path(draft_dir: Path, capture: dict) -> Path | None:
    relative = (capture.get("capture") or {}).get("recording")
    if not relative:
        return None
    path = Path(draft_dir) / relative
    return path if path.is_file() else None


def replay_generated(page, draft_dir: Path, capture: dict, out=print, *,
                     before_start=None, input_hooks=None) -> None:
    """Run demonstrate.py at the demonstration's own pace, with its own voice."""
    module = _load_replay_module(Path(draft_dir), capture)
    replay = getattr(module, "_REPLAY", None)
    if input_hooks is not None:
        required = {"locator_wrapper", "type_text"}
        if not isinstance(replay, dict) or not required.issubset(replay):
            raise CaptureTrialError(
                "this draft predates OS-input replay support; recapture it "
                "before replaying with Codex")
        replay.update(input_hooks)
    recording = _recording_path(Path(draft_dir), capture)
    narrate = None
    if recording is None:
        # No captured audio: fall back to reading the narration script aloud.
        from showAndTell.core import tts
        voice = tts.voice_args()
        if voice is None:
            out("  (no recording and no TTS voice; replaying without narration)")
        narrate = narration.make_narrator(Path(draft_dir), voice, out)

    def on_step(key: str, current_page, description: str) -> None:
        if narrate is not None:
            narrate(key, current_page, description)
        else:
            out(f"  {description}")

    surface_creds = capture["surfaces"][0].get("credentials") or {}
    replacements = _surface_origins(Path(draft_dir), capture)
    if replacements:
        out(f"  remapping {len(replacements)} captured application endpoint(s) to clean trial ports")

    player = None
    startup_checked = False

    def start_replay_clock() -> None:
        """Begin audio only after generated setup reaches its first action."""
        nonlocal player, startup_checked
        if not startup_checked and before_start is not None:
            before_start()
            startup_checked = True
        if recording is not None:
            out(f"  playing the recorded narration from {recording.name}")
            player = audio.start_recorded_narration(recording, out)
        replay["start"] = time.monotonic()

    try:
        # New generated drivers invoke on_start from their first _pace() call,
        # after login, supporting-page navigation, and readiness checks. Keep a
        # compatibility path for hand-authored/older drivers without the hook.
        if isinstance(replay, dict) and "on_start" in replay:
            replay["start"] = None
            replay["on_start"] = start_replay_clock
        elif isinstance(replay, dict):
            start_replay_clock()
        elif recording is not None:
            out(f"  playing the recorded narration from {recording.name}")
            player = audio.start_recorded_narration(recording, out)
            out("  (this draft predates recorded pacing; actions will not be timed)")
        module.demonstrate(page, primary_origin(capture), surface_creds, on_step)
    finally:
        if player is not None and player.poll() is None:
            player.terminate()
            try:
                player.wait(timeout=5)
            except subprocess.TimeoutExpired:
                player.kill()


def has_state_snapshot(capture: dict) -> bool:
    """Whether the draft carries an exact application-state snapshot.

    The viewer restores that snapshot before launching the trial, so the state
    is already in place and replaying setup gestures on top of it would apply
    the same work twice.
    """
    return bool((capture.get("seed") or {}).get("state_snapshots"))


def restore_generated_seed(page, draft_dir: Path, capture: dict, out=print) -> None:
    """Rebuild setup by replaying its gestures, for drafts with no snapshot.

    This is the fallback for fixtures that cannot snapshot their own state.
    It is inherently fragile — the recorded selectors assume the page looks
    exactly as it did during capture — so it is skipped whenever a real state
    snapshot exists.
    """
    module = _load_replay_module(Path(draft_dir), capture)
    seed = getattr(module, "seed", None)
    if not callable(seed):
        return
    out("  replaying setup gestures (no state snapshot in this draft)…")
    surface_creds = capture["surfaces"][0].get("credentials") or {}
    seed(page, primary_origin(capture), surface_creds,
         lambda _key, _page, _description: None)
