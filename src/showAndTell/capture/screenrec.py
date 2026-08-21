"""OS-level screen recording of teach runs (video + default-input audio), so
a human can review afterwards what actually happened on screen — the Codex
app, window ordering, OS dialogs, real clicks. Recording is evidence, not a
dependency: every failure path degrades to a no-op with one warning; a teach
run must never fail because its recording did."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

# A backend that exits within this many seconds never really started (denied
# screen-recording permission, missing audio device, bad flag).
STARTUP_GRACE_S = 3.0

# ffmpeg-level cap so a recorder orphaned by a hard-killed run (stop() never
# reached) cannot capture indefinitely; generous vs the longest teach run.
# screencapture must NOT get a cap: -V<seconds> disables its SIGINT finalize
# entirely (the stop signal then kills it and the movie is lost) — its orphan
# protection is the controlling pty instead (see _spawn).
MAX_RECORD_S = 7200

# Every container any recording backend can leave behind: screencapture
# writes .mov, ffmpeg/wf-recorder write .mkv, and demo_record's Playwright
# page recorder writes .webm. prune_alternates sweeps exactly these, so a
# backend that adopts a new format must be added here or its predecessor's
# stale recording survives beside the new one.
VIDEO_SUFFIXES = (".mov", ".mkv", ".webm")


class NullRecorder:
    """Stand-in when nothing records; stop() is a harmless no-op."""
    path: Path | None = None
    has_audio = False
    start_epoch_ms: int | None = None

    def stop(self) -> Path | None:
        return None


def settle(rec, dest_base: Path | None, out, *, label: str = "screen recording",
           prune_alternates: bool = False) -> Path | None:
    """Stop ``rec`` and move its file to ``<dest_base><ext>`` (the extension
    comes from the backend). Best-effort: a failed move leaves the file where
    it is and says so, and never raises past the caller's run result.

    ``prune_alternates`` removes same-stem files in the other video formats,
    for destinations that must hold exactly one canonical recording — only
    after the replacement above has succeeded.
    """
    video = rec.stop()
    if video is None or dest_base is None:
        return None
    dest_base.parent.mkdir(parents=True, exist_ok=True)
    dest = dest_base.parent / f"{dest_base.name}{video.suffix}"
    try:
        os.replace(video, dest)
    except OSError as exc:
        out(f"  ({label} left at {video}: {exc})")
        return video
    if prune_alternates:
        for suffix in VIDEO_SUFFIXES:
            alternate = dest_base.parent / f"{dest_base.name}{suffix}"
            if alternate != dest:
                alternate.unlink(missing_ok=True)
    try:
        video.parent.rmdir()
    except OSError:
        pass
    out(f"  {label}: {dest}")
    return dest


def _x11_size() -> str | None:
    """The X display size ("1920x1080") via xdpyinfo, or None — ffmpeg's
    x11grab default frame is tiny, so probe when we can. Odd dimensions round
    DOWN to even (CRD's resize-to-fit yields sizes like 1512x827, and
    libx264+yuv420p refuses odd frames — the recorder would die unstarted,
    losing the run's recording for a 1px sliver)."""
    try:
        info = subprocess.run(["xdpyinfo"], capture_output=True, text=True,
                              timeout=5).stdout
        for line in info.splitlines():
            if "dimensions:" in line:
                w, h = line.split()[1].split("x")
                return f"{int(w) & ~1}x{int(h) & ~1}"
    except Exception:
        pass
    return None


def _x11_cmd(base: Path, audio: bool) -> tuple[list[str], Path]:
    path = base.with_suffix(".mkv")   # mkv stays playable if the proc dies mid-write
    cmd = ["ffmpeg", "-y", "-f", "x11grab", "-framerate", "15"]
    size = _x11_size()
    if size:
        cmd += ["-video_size", size]
    cmd += ["-i", os.environ.get("DISPLAY", ":0")]
    if audio:
        cmd += ["-f", "pulse", "-i", "default"]
    return cmd + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                  "-pix_fmt", "yuv420p", "-t", str(MAX_RECORD_S), str(path)], path


def _attempts(base: Path) -> list[tuple[list[str], Path]]:
    """Ordered (command, output_path) backends to try on this platform; [] when
    none apply. -g/-a capture the default audio input so narration is audible."""
    if sys.platform == "darwin":
        path = base.with_suffix(".mov")
        return [(["screencapture", "-v", "-g", "-k", "-x", str(path)], path)]
    if sys.platform.startswith("linux"):
        if os.environ.get("DISPLAY") and shutil.which("ffmpeg"):
            # pulse capture is the common startup failure -> retry video-only
            return [_x11_cmd(base, audio=True), _x11_cmd(base, audio=False)]
        if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wf-recorder"):
            path = base.with_suffix(".mkv")
            return [(["wf-recorder", "-a", "-f", str(path)], path)]
    return []


def _spawn(cmd: list[str]) -> tuple[subprocess.Popen, int | None]:
    """Spawn a backend, returning (proc, pty_master_fd_or_None). screencapture
    installs its finalize-on-SIGINT handler only when stdin is a terminal —
    spawned on DEVNULL, the stop signal default-kills it and the movie (written
    only at finalize) is lost — so it gets a pty on stdin, made its CONTROLLING
    terminal (setsid + TIOCSCTTY). That also ties the recorder's life to the
    harness: if the run dies without stop() (even kill -9), the master fd
    closes and the kernel SIGHUPs the recorder, so an orphan can never keep
    capturing. The other backends handle SIGINT regardless."""
    if Path(cmd[0]).name == "screencapture":
        import fcntl
        import pty
        import termios
        master, slave = pty.openpty()

        def _make_controlling_tty():   # runs in the child; no threads live yet
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        try:
            proc = subprocess.Popen(cmd, stdin=slave, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    preexec_fn=_make_controlling_tty)
        except Exception:
            os.close(master)
            raise
        finally:
            os.close(slave)
        return proc, master
    return subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL), None


def _close_fd(fd: int | None) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


class Recorder:
    """A live backend process writing to self.path; stop() finalizes the file."""

    def __init__(self, proc: subprocess.Popen, path: Path, out=print,
                 master_fd: int | None = None, has_audio: bool = False,
                 start_epoch_ms: int | None = None):
        self._proc, self.path, self._out = proc, path, out
        self._master_fd = master_fd   # pty keeping screencapture's SIGINT handler alive
        self.has_audio = has_audio
        # When the backend was spawned — the recording's own timeline begins
        # here, not when start() returns after the grace sleep. Consumers that
        # timestamp events against the recording must use this as time zero.
        self.start_epoch_ms = start_epoch_ms

    def stop(self) -> Path | None:
        """Finalize and return the recording, or None if nothing usable was
        written. Never raises. Every backend finalizes its output on SIGINT
        (screencapture only via the pty _spawn gave it). On the kill
        fallback path (this stop() never runs, MAX_RECORD_S or a hard kill
        ends the process another way) a macOS .mov can be left truncated and
        unplayable (missing moov atom) yet still filed; Linux's .mkv
        tolerates this by design."""
        try:
            if self._proc.poll() is None:
                self._proc.send_signal(signal.SIGINT)
                try:
                    self._proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=5)
        except Exception as e:
            self._out(f"  (screen recorder stop failed: {e})")
        _close_fd(self._master_fd)
        self._master_fd = None
        try:
            if self.path.exists() and self.path.stat().st_size > 0:
                return self.path
        except OSError as e:
            self._out(f"  (screen recorder stop failed: {e})")
        return None


def start(base: Path, out=print) -> Recorder | NullRecorder:
    """Begin recording the whole screen (plus default-input audio) to
    <base>.<mov|mkv>. Never raises for recorder failures (the run proceeds
    unrecorded after one warning). On macOS the native ``screencapture``
    command owns permission handling; an unbundled Python preflight would
    ask for access under the wrong application identity."""
    try:
        attempts = _attempts(base)
        if not attempts:
            out("  (screen recording unavailable on this platform)")
            return NullRecorder()
        base.parent.mkdir(parents=True, exist_ok=True)
        for cmd, path in attempts:
            spawn_ms = int(time.time() * 1000)
            proc, master = _spawn(cmd)
            time.sleep(STARTUP_GRACE_S)   # instant exit = failed start (see STARTUP_GRACE_S)
            if proc.poll() is None:
                out(f"  screen recording -> {path}")
                name = Path(cmd[0]).name
                has_audio = ((name == "screencapture" and "-g" in cmd)
                             or (name == "ffmpeg" and "pulse" in cmd)
                             or (name == "wf-recorder" and "-a" in cmd))
                return Recorder(proc, path, out, master_fd=master,
                                has_audio=has_audio, start_epoch_ms=spawn_ms)
            _close_fd(master)
            path.unlink(missing_ok=True)
        out("  (screen recorder failed to start — run proceeds unrecorded)")
    except Exception as e:
        out(f"  (screen recording unavailable: {e} — run proceeds unrecorded)")
    return NullRecorder()
