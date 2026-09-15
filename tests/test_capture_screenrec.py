"""Screen recording of teach runs: platform backend selection, the
never-raise start/stop lifecycle, and the no-op degradation. No test in the
default suite records a real screen."""
from __future__ import annotations

import time
from pathlib import Path

from showAndTell.capture import screenrec


# --- settle: stop-and-file the recording -----------------------------------

def _stopped(video):
    class Rec:
        def stop(self):
            return video
    return Rec()


def test_settle_moves_the_recording_to_the_destination(tmp_path):
    video = tmp_path / "work" / ".rec.mov"
    video.parent.mkdir()
    video.write_bytes(b"movie")

    dest = screenrec.settle(
        _stopped(video), tmp_path / "out" / "screen", lambda _line: None)

    assert dest == tmp_path / "out" / "screen.mov"
    assert dest.read_bytes() == b"movie"
    assert not video.exists()


def test_settle_failed_move_leaves_the_file_and_never_raises(
        tmp_path, monkeypatch):
    video = tmp_path / ".rec.mov"
    video.write_bytes(b"movie")

    def refuse(src, dst):
        raise OSError("cross-device link")

    monkeypatch.setattr(screenrec.os, "replace", refuse)
    lines = []
    result = screenrec.settle(_stopped(video), tmp_path / "screen", lines.append)

    assert result == video and video.exists()
    assert any("left at" in line for line in lines)


def test_settle_prunes_every_alternate_recording_format(tmp_path):
    for suffix in screenrec.VIDEO_SUFFIXES:
        (tmp_path / f"recording{suffix}").write_bytes(b"stale")
    video = tmp_path / "work" / ".rec.webm"
    video.parent.mkdir()
    video.write_bytes(b"fresh")

    dest = screenrec.settle(
        _stopped(video), tmp_path / "recording", lambda _line: None,
        prune_alternates=True)

    assert dest == tmp_path / "recording.webm"
    assert dest.read_bytes() == b"fresh"
    survivors = sorted(path.name for path in tmp_path.glob("recording.*"))
    assert survivors == ["recording.webm"]


# --- backend selection -----------------------------------------------------

def test_macos_uses_screencapture(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.platform", "darwin")
    (cmd, path), = screenrec._attempts(tmp_path / "rec")
    assert cmd[0] == "screencapture"
    assert {"-v", "-g", "-k", "-x"} <= set(cmd)   # video, audio, clicks, silent
    # -V<seconds> must NOT be present: it disables screencapture's SIGINT
    # finalize, so the stop signal would kill it and lose the movie.
    assert not any(a.startswith("-V") for a in cmd)
    assert path == tmp_path / "rec.mov" and str(path) == cmd[-1]


def test_linux_x11_tries_audio_then_silent(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(screenrec.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(screenrec, "_x11_size", lambda: "1920x1080")
    attempts = screenrec._attempts(tmp_path / "rec")
    assert len(attempts) == 2
    (with_audio, p1), (silent, p2) = attempts
    assert with_audio[0] == "ffmpeg" and "pulse" in with_audio
    assert "pulse" not in silent
    assert "1920x1080" in with_audio
    assert "-t" in with_audio
    assert p1 == p2 == tmp_path / "rec.mkv"


def test_linux_wayland_uses_wf_recorder(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(screenrec.shutil, "which",
                        lambda name: "/usr/bin/wf-recorder" if name == "wf-recorder" else None)
    (cmd, path), = screenrec._attempts(tmp_path / "rec")
    assert cmd[0] == "wf-recorder" and "-a" in cmd
    assert path == tmp_path / "rec.mkv"


def test_no_backend_on_other_platforms(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.platform", "win32")
    assert screenrec._attempts(tmp_path / "rec") == []


def test_linux_without_tools_has_no_backend(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(screenrec.shutil, "which", lambda name: None)
    assert screenrec._attempts(tmp_path / "rec") == []


def test_null_recorder_is_a_no_op():
    rec = screenrec.NullRecorder()
    assert rec.path is None and rec.stop() is None


# --- start/stop lifecycle --------------------------------------------------

def test_start_records_with_a_live_backend(monkeypatch, tmp_path):
    """A backend that stays up past the grace period yields a live Recorder."""
    out_file = tmp_path / "rec.mov"
    monkeypatch.setattr(screenrec, "STARTUP_GRACE_S", 0.01)
    monkeypatch.setattr(screenrec, "_attempts",
                        lambda base: [(["sleep", "30"], out_file)])
    rec = screenrec.start(tmp_path / "rec", out=lambda _: None)
    assert isinstance(rec, screenrec.Recorder) and rec.path == out_file
    out_file.write_bytes(b"fake video data")   # what the tool would have written
    assert rec.stop() == out_file
    assert rec._proc.poll() is not None        # backend really terminated


def test_start_stamps_when_capture_actually_began(monkeypatch, tmp_path):
    """start() returns only after the startup grace sleep, but the backend has
    been capturing since it was spawned. The recorder must expose that spawn
    moment so the action clock can be aligned with the audio timeline —
    stamping "now" after start() returns bakes a multi-second voice lag into
    every replay."""
    out_file = tmp_path / "rec.mov"
    monkeypatch.setattr(screenrec, "STARTUP_GRACE_S", 0.4)
    monkeypatch.setattr(screenrec, "_attempts",
                        lambda base: [(["sleep", "30"], out_file)])
    before = int(time.time() * 1000)
    rec = screenrec.start(tmp_path / "rec", out=lambda _: None)
    after_grace = int(time.time() * 1000)
    try:
        assert isinstance(rec, screenrec.Recorder)
        # Stamped at spawn: at or after `before`, but clearly before the
        # grace sleep finished.
        assert before <= rec.start_epoch_ms <= after_grace - 300
    finally:
        rec._proc.kill()
    assert screenrec.NullRecorder().start_epoch_ms is None


def test_start_falls_through_dead_backends(monkeypatch, tmp_path):
    """First backend dies instantly (like ffmpeg with a bad audio device);
    the second records."""
    out_file = tmp_path / "rec.mkv"
    monkeypatch.setattr(screenrec, "STARTUP_GRACE_S", 0.05)
    monkeypatch.setattr(screenrec, "_attempts", lambda base: [
        (["false"], out_file), (["sleep", "30"], out_file)])
    rec = screenrec.start(tmp_path / "rec", out=lambda _: None)
    assert isinstance(rec, screenrec.Recorder)
    rec._proc.kill()


def test_start_null_when_every_backend_dies(monkeypatch, tmp_path):
    monkeypatch.setattr(screenrec, "STARTUP_GRACE_S", 0.05)
    monkeypatch.setattr(screenrec, "_attempts",
                        lambda base: [(["false"], tmp_path / "rec.mov")])
    msgs = []
    rec = screenrec.start(tmp_path / "rec", out=msgs.append)
    assert isinstance(rec, screenrec.NullRecorder)
    assert any("unrecorded" in m for m in msgs)


def test_start_null_when_no_backend(monkeypatch, tmp_path):
    monkeypatch.setattr(screenrec, "_attempts", lambda base: [])
    msgs = []
    rec = screenrec.start(tmp_path / "rec", out=msgs.append)
    assert isinstance(rec, screenrec.NullRecorder)
    assert any("unavailable" in m for m in msgs)


def test_start_never_raises(monkeypatch, tmp_path):
    """Even a missing executable (FileNotFoundError from Popen) degrades to a
    warning + NullRecorder."""
    monkeypatch.setattr(screenrec, "STARTUP_GRACE_S", 0.01)
    monkeypatch.setattr(screenrec, "_attempts", lambda base: [
        (["definitely-not-a-real-command-xyz"], tmp_path / "rec.mov")])
    msgs = []
    rec = screenrec.start(tmp_path / "rec", out=msgs.append)
    assert isinstance(rec, screenrec.NullRecorder)
    assert msgs   # warned, didn't raise


def test_stop_none_when_file_empty(monkeypatch, tmp_path):
    import subprocess
    proc = subprocess.Popen(["true"])
    proc.wait()
    rec = screenrec.Recorder(proc, tmp_path / "rec.mov", out=lambda _: None)
    assert rec.stop() is None                  # dead proc + no file -> None


def test_stop_tolerates_stop_errors(monkeypatch, tmp_path):
    class ExplodingProc:
        def poll(self):
            raise RuntimeError("boom")
    msgs = []
    rec = screenrec.Recorder(ExplodingProc(), tmp_path / "rec.mov", out=msgs.append)
    assert rec.stop() is None                  # warned, didn't raise
    assert any("stop failed" in m for m in msgs)


def test_stop_tolerates_file_check_errors(monkeypatch, tmp_path):
    """Permission errors (or other OSError) from Path.exists() or Path.stat()
    don't escape stop() — they're caught, logged, and degraded to None."""
    import subprocess
    proc = subprocess.Popen(["true"])
    proc.wait()

    class FakePath:
        def exists(self):
            raise PermissionError("denied")

    msgs = []
    rec = screenrec.Recorder(proc, FakePath(), out=msgs.append)
    assert rec.stop() is None                  # warned, didn't raise
    assert any("stop failed" in m for m in msgs)


def test_start_gives_screencapture_a_tty_stdin(monkeypatch, tmp_path):
    """macOS screencapture only installs its finalize-on-SIGINT handler when
    stdin is a terminal; spawned on DEVNULL it is default-killed by the stop
    signal and the movie (written only at finalize) is lost. Locked in with a
    fake screencapture that writes its output on SIGINT only when stdin is a
    tty, exactly like the real one."""
    fake = tmp_path / "screencapture"
    dest = tmp_path / "rec.mov"
    ready = tmp_path / "ready"
    fake.write_text(
        "#!/bin/sh\n"
        "for a; do dest=$a; done\n"
        "[ -t 0 ] && tty=1 || tty=0\n"
        "trap '[ \"$tty\" = 1 ] && echo data > \"$dest\"; exit 0' INT\n"
        f"touch {ready}\n"
        "while :; do sleep 0.2; done\n")
    fake.chmod(0o755)
    monkeypatch.setattr(screenrec, "STARTUP_GRACE_S", 0.05)
    monkeypatch.setattr(screenrec, "_attempts",
                        lambda base: [([str(fake), str(dest)], dest)])
    rec = screenrec.start(tmp_path / "rec", out=lambda _: None)
    assert isinstance(rec, screenrec.Recorder)
    for _ in range(100):           # wait for the trap to be installed, or the
        if ready.exists():         # stop's SIGINT can outrun sh's startup
            break
        time.sleep(0.05)
    assert ready.exists()
    assert rec.stop() == dest      # finalized on SIGINT because stdin was a tty


# --- CLI wrapper integration ----------------------------------------------

import pytest

from showAndTell import cli


def _stub_screenrec(monkeypatch, starts):
    """Replace cli.screenrec.start with a stub whose recorder 'wrote' a .mov
    file; records each start call in starts."""
    class StubRec:
        def __init__(self, path):
            self.path = path

        def stop(self):
            return self.path if self.path.exists() else None

    def start(base, out=print):
        path = base.parent / (base.name + ".mov")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")
        starts.append(path)
        return StubRec(path)

    monkeypatch.setattr(cli.screenrec, "start", start)


def test_wrapper_files_recording_with_the_run(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    starts = []
    _stub_screenrec(monkeypatch, starts)

    def run_fn():
        rd = Path("runs") / "20260101-000000-claude-teach-t"
        rd.mkdir(parents=True)
        return rd

    cli._run_with_cache("claude-teach", tmp_path / "t", no_cache=True,
                        run_fn=run_fn, out=lambda *_: None, record=True)
    assert len(starts) == 1
    assert (Path("runs") / "20260101-000000-claude-teach-t" / "screen.mov").read_bytes() == b"video"


def test_wrapper_keeps_recording_of_a_failed_run(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _stub_screenrec(monkeypatch, [])

    def run_fn():
        raise RuntimeError("teach exploded")

    with pytest.raises(RuntimeError):
        cli._run_with_cache("codex-record", tmp_path / "t", no_cache=True,
                            run_fn=run_fn, out=lambda *_: None, record=True)
    kept = list((Path("runs")).glob("*-codex-record-t-FAILED-screen.mov"))
    assert len(kept) == 1 and kept[0].read_bytes() == b"video"


def test_wrapper_record_false_never_starts(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.screenrec, "start",
                        lambda *a, **k: pytest.fail("must not record"))

    def run_fn():
        rd = Path("runs") / "r"
        rd.mkdir(parents=True, exist_ok=True)
        return rd

    cli._run_with_cache("claude-teach", tmp_path / "t", no_cache=True,
                        run_fn=run_fn, out=lambda *_: None, record=False)
    cli._run_with_cache("claude-teach", tmp_path / "t", no_cache=True,
                        run_fn=run_fn, out=lambda *_: None)   # default is off too


def test_no_record_flag_wired_on_all_three_commands(monkeypatch):
    """main() binds each subcommand's fn at parser-build time, so patching the
    _cmd_* handlers first makes dispatch harmless and hands us the parsed args."""
    captured = []
    for name in ("_cmd_claude_teach", "_cmd_brackett_teach", "_cmd_codex_record"):
        monkeypatch.setattr(cli, name, lambda args: captured.append(args))
    for cmd in ("claude-teach", "brackett-teach", "codex-record"):
        monkeypatch.setattr("sys.argv", ["showAndTell", cmd, "--task", "tasks/x", "--no-record"])
        cli.main()
    assert len(captured) == 3 and all(ns.no_record is True for ns in captured)
    monkeypatch.setattr("sys.argv", ["showAndTell", "claude-teach", "--task", "tasks/x"])
    cli.main()
    assert captured[-1].no_record is False   # flag defaults to recording on


# --- real capture (slow, macOS only) --------------------------------------

import os
import sys as _sys
import time as _time


@pytest.mark.slow
@pytest.mark.skipif(_sys.platform != "darwin", reason="real capture test is macOS-only")
@pytest.mark.skipif(not os.environ.get("SHOWANDTELL_REAL_CAPTURE"),
                    reason="set SHOWANDTELL_REAL_CAPTURE=1 to run the real screen-capture test")
def test_real_recording_produces_a_movie(tmp_path):
    rec = screenrec.start(tmp_path / "rec", out=lambda _: None)
    if isinstance(rec, screenrec.NullRecorder):
        pytest.skip("screen recording unavailable (no Screen Recording permission?)")
    _time.sleep(2)
    path = rec.stop()
    assert path is not None and path.suffix == ".mov"
    assert path.stat().st_size > 0


def test_x11_size_rounds_odd_dimensions_to_even(monkeypatch):
    # CRD's resize-to-fit produces odd sizes (e.g. 1512x827); libx264+yuv420p
    # refuses odd dimensions, so an odd probe must round down or the recorder
    # dies at startup and the run silently goes unrecorded.
    import types

    def fake_run(*a, **k):
        return types.SimpleNamespace(
            stdout="  dimensions:    1512x827 pixels (400x218 millimeters)\n")

    monkeypatch.setattr(screenrec.subprocess, "run", fake_run)
    assert screenrec._x11_size() == "1512x826"


def test_x11_size_passes_even_dimensions_through(monkeypatch):
    import types

    def fake_run(*a, **k):
        return types.SimpleNamespace(
            stdout="  dimensions:    1920x1080 pixels (508x285 millimeters)\n")

    monkeypatch.setattr(screenrec.subprocess, "run", fake_run)
    assert screenrec._x11_size() == "1920x1080"


def test_macos_capture_start_failure_never_blocks_the_run(tmp_path, monkeypatch):
    """A denied native recorder exits early; teach continues unrecorded and
    never exposes the Python runtime as a permission target."""
    dest = tmp_path / "video.mov"

    class DeniedRecorder:
        def poll(self):
            return 1

    monkeypatch.setattr(screenrec.sys, "platform", "darwin")
    monkeypatch.setattr(screenrec, "STARTUP_GRACE_S", 0.01)
    monkeypatch.setattr(
        screenrec, "_attempts",
        lambda _base: [(["screencapture", "-v", str(dest)], dest)])
    monkeypatch.setattr(
        screenrec, "_spawn", lambda _command: (DeniedRecorder(), None))
    messages = []

    rec = screenrec.start(tmp_path / "video", out=messages.append)

    assert isinstance(rec, screenrec.NullRecorder)
    assert any("run proceeds unrecorded" in message for message in messages)
    assert all("Python.app" not in message for message in messages)
