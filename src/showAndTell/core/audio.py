"""Route narration into every recording product's microphone.

macOS uses the easily installed VB-CABLE device for both default output and
input. Narration is silent by default; when ``SHOWANDTELL_HEAR_NARRATION=1`` it is
also mirrored to the physical speakers. Linux creates the equivalent route
with a PulseAudio null sink and remapped source.

Routes restore the prior devices on exit, including error paths.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

VIRTUAL_CABLE_NAMES = ("VB-CABLE", "VB-Cable", "VB-Audio Virtual Cable")
VIRTUAL_HINTS = (
    "vb-cable", "vb cable", "vb-audio", "teams", "zoom", "aggregate",
    "multi-output", "virtual",
)
VIRTUAL_CABLE_ENV = "SHOWANDTELL_VIRTUAL_CABLE"
HEAR_NARRATION_ENV = "SHOWANDTELL_HEAR_NARRATION"
HEAR_NARRATION_CONTROL_ENV = "SHOWANDTELL_HEAR_NARRATION_CONTROL"
PULSE_SINK = "showAndTell_narration"
PULSE_MIC = "showAndTell_mic"
PULSE_PREPARED = "SHOWANDTELL_PULSE_PREPARED"


def hear_narration_enabled() -> bool:
    """Current speaker-monitor choice, optionally controlled by the viewer."""
    control = os.environ.get(HEAR_NARRATION_CONTROL_ENV)
    if control:
        try:
            return Path(control).read_text(encoding="utf-8").strip() == "1"
        except OSError:
            pass
    return os.environ.get(HEAR_NARRATION_ENV) == "1"


def _pactl(*args: str) -> str:
    return subprocess.run(["pactl", *args], capture_output=True, text=True).stdout.strip()


@contextmanager
def pulse_narration(out=print):
    """Linux narration loopback: null sink as default out, and a REMAP of its
    monitor as the default mic. The remap is load-bearing: Chromium's pulse
    backend filters monitor sources out of microphone enumeration, so with a
    bare .monitor as default, getUserMedia sees zero devices and raises
    NotFoundError (measured on the webarena VM — Chrome listed no mics until
    the remap existed, then captured espeak at full scale). Restores prior
    defaults and unloads both modules on exit."""
    if os.environ.get(PULSE_PREPARED) == "1":
        out(f"  narration uses isolated pulse route {os.environ.get('PULSE_SINK')!r} -> "
            f"{os.environ.get('PULSE_SOURCE')!r}")
        yield True
        return
    if shutil.which("pactl") is None:
        out("  (pactl unavailable — narration won't be captured)")
        yield False
        return
    prev_sink, prev_src = _pactl("get-default-sink"), _pactl("get-default-source")
    sink_module = _pactl("load-module", "module-null-sink", f"sink_name={PULSE_SINK}",
                         "sink_properties=device.description=ShowAndTellNarration")
    mic_module = _pactl("load-module", "module-remap-source",
                        f"master={PULSE_SINK}.monitor", f"source_name={PULSE_MIC}",
                        "source_properties=device.description=ShowAndTellMic")
    monitor_module = ""
    try:
        _pactl("set-default-sink", PULSE_SINK)
        _pactl("set-default-source", PULSE_MIC)
        if hear_narration_enabled() and prev_sink:
            monitor_module = _pactl(
                "load-module", "module-loopback",
                f"source={PULSE_SINK}.monitor", f"sink={prev_sink}",
                "latency_msec=20")
        out(f"  narration routed through pulse null sink '{PULSE_SINK}' -> mic "
            f"'{PULSE_MIC}' (restores {prev_sink!r}/{prev_src!r} after)")
        yield True
    finally:
        if prev_sink:
            _pactl("set-default-sink", prev_sink)
        if prev_src:
            _pactl("set-default-source", prev_src)
        if monitor_module.isdigit():
            _pactl("unload-module", monitor_module)
        # The remap depends on the sink's monitor — unload it first.
        if mic_module.isdigit():
            _pactl("unload-module", mic_module)
        if sink_module.isdigit():
            _pactl("unload-module", sink_module)


def _has_switch() -> bool:
    return shutil.which("SwitchAudioSource") is not None


def _devices(kind: str) -> list[str]:
    out = subprocess.run(["SwitchAudioSource", "-a", "-t", kind],
                         capture_output=True, text=True).stdout
    return [d.strip() for d in out.splitlines() if d.strip()]


def _current(kind: str) -> str | None:
    r = subprocess.run(["SwitchAudioSource", "-c", "-t", kind], capture_output=True, text=True)
    return r.stdout.strip() or None


def _set_device(kind: str, name: str) -> None:
    subprocess.run(["SwitchAudioSource", "-t", kind, "-s", name], capture_output=True)


def _virtual_cable() -> str | None:
    """The same supported VB-CABLE device on both sides of the route."""
    if not _has_switch():
        return None
    inputs = {device.casefold(): device for device in _devices("input")}
    outputs = {device.casefold(): device for device in _devices("output")}
    for candidate in VIRTUAL_CABLE_NAMES:
        key = candidate.casefold()
        if key in inputs and key in outputs:
            return outputs[key]
    return None


def _is_virtual_device(name: str | None) -> bool:
    return bool(name and any(hint in name.casefold() for hint in VIRTUAL_HINTS))


def _real_device(kind: str) -> str | None:
    """A physical device — prefer built-in, skip virtual/conferencing devices."""
    devs = _devices(kind)
    real = [d for d in devs if not any(h in d.lower() for h in VIRTUAL_HINTS)]
    for d in real:
        if "macbook" in d.lower() or "built-in" in d.lower():
            return d
    return real[0] if real else None


def _restore_device(kind: str, previous: str | None) -> None:
    """Restore a saved default, but never a stale virtual route."""
    if not previous:
        return
    if _is_virtual_device(previous):
        previous = _real_device(kind) or previous
    _set_device(kind, previous)


def ensure_real_devices(out=print) -> None:
    """Self-heal defaults left on a virtual cable by a hard-killed run."""
    if not _has_switch():
        return
    for kind in ("input", "output"):
        current = _current(kind)
        if _is_virtual_device(current):
            real = _real_device(kind)
            if real:
                out(f"  default {kind} was stuck on {current} — "
                    f"switching to {real!r}")
                _set_device(kind, real)


@contextmanager
def virtual_cable_narration(out=print):
    """Route narration through VB-CABLE for Claude, Brackett, and Codex."""
    if sys.platform.startswith("linux"):
        with pulse_narration(out=out) as ok:
            yield ok
        return
    cable = _virtual_cable()
    if cable is None:
        out("  (VB-CABLE is not installed — narration won't be captured; "
            "run: brew install --cask vb-cable)")
        yield False
        return
    prev_out, prev_in = _current("output"), _current("input")
    prev_env = os.environ.get(VIRTUAL_CABLE_ENV)
    try:
        _set_device("output", cable)
        _set_device("input", cable)
        os.environ[VIRTUAL_CABLE_ENV] = cable
        audible = hear_narration_enabled()
        out(f"  narration routed through {cable}"
            f" ({'speaker monitor on' if audible else 'silent'}; restores "
            f"{prev_out!r}/{prev_in!r} after)")
        yield True
    finally:
        if prev_env is None:
            os.environ.pop(VIRTUAL_CABLE_ENV, None)
        else:
            os.environ[VIRTUAL_CABLE_ENV] = prev_env
        _restore_device("output", prev_out)
        _restore_device("input", prev_in)


def _audiotoolbox_device_index(name: str) -> int | None:
    """Resolve an FFmpeg AudioToolbox output index by its CoreAudio name."""
    if shutil.which("ffmpeg") is None:
        return None
    command = [
        "ffmpeg", "-hide_banner", "-f", "lavfi", "-i",
        "anullsrc=r=48000:cl=stereo", "-t", "0.001", "-f", "audiotoolbox",
        "-list_devices", "true", "-",
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in result.stderr.splitlines():
        marker = line.rsplit("] [", 1)
        if len(marker) != 2 or "]" not in marker[1]:
            continue
        index_text, description = marker[1].split("]", 1)
        device_name = description.rsplit(",", 1)[0].strip()
        if index_text.isdigit() and device_name == name:
            return int(index_text)
    return None


def _speaker_monitor_command(path: Path, gain: float | None = None,
                             start_at: float = 0.0) -> list[str] | None:
    if sys.platform != "darwin":
        return None
    speaker = _real_device("output")
    index = _audiotoolbox_device_index(speaker) if speaker else None
    if index is None:
        return None
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    if start_at > 0.05:
        command.extend(["-ss", f"{start_at:.3f}"])
    command.extend(["-i", str(path)])
    if gain is not None:
        command.extend(["-af", f"volume={gain}"])
    command.extend(["-f", "audiotoolbox", "-audio_device_index", str(index),
                    "showAndTell-speaker-monitor"])
    return command


class _MirroredPlayback:
    """Popen-compatible handle for virtual-cable plus optional speaker audio."""

    def __init__(self, primary: subprocess.Popen) -> None:
        self.primary = primary
        self.processes = [primary]
        self._lock = threading.Lock()

    def add(self, process: subprocess.Popen) -> None:
        with self._lock:
            self.processes.append(process)

    def _snapshot(self) -> list[subprocess.Popen]:
        with self._lock:
            return list(self.processes)

    def poll(self):
        return self.primary.poll()

    def terminate(self) -> None:
        for process in self._snapshot():
            if process.poll() is None:
                process.terminate()

    def kill(self) -> None:
        for process in self._snapshot():
            if process.poll() is None:
                process.kill()

    def wait(self, timeout=None):
        import time
        deadline = None if timeout is None else time.monotonic() + timeout
        code = 0
        for process in self._snapshot():
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            code = process.wait(timeout=remaining)
        return code


def _manage_speaker_monitor(playback: _MirroredPlayback, path: Path,
                            gain: float | None,
                            initial: subprocess.Popen | None) -> None:
    """Follow the viewer's live speaker switch without touching virtual mic."""
    if not os.environ.get(HEAR_NARRATION_CONTROL_ENV):
        return

    def watch() -> None:
        import time
        started = time.monotonic()
        speaker = initial
        while playback.primary.poll() is None:
            enabled = hear_narration_enabled()
            if speaker is not None and speaker.poll() is not None:
                speaker = None
            if enabled and speaker is None:
                command = _speaker_monitor_command(
                    path, gain, start_at=time.monotonic() - started)
                if command is not None:
                    try:
                        speaker = subprocess.Popen(
                            command, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
                        playback.add(speaker)
                    except OSError:
                        speaker = None
            elif not enabled and speaker is not None:
                speaker.terminate()
                speaker = None
            threading.Event().wait(0.15)
        if speaker is not None and speaker.poll() is None:
            speaker.terminate()

    threading.Thread(
        target=watch, name="showAndTell-speaker-monitor", daemon=True).start()


def start_narration_playback(path: Path, *, gain: float | None = None,
                             out=print):
    """Play narration into the selected virtual cable and optional speakers."""
    if shutil.which("ffplay"):
        command = ["ffplay", "-nodisp", "-autoexit", "-vn",
                   "-loglevel", "error"]
        if gain is not None:
            command += ["-af", f"volume={gain}"]
        command.append(str(path))
    elif shutil.which("afplay"):
        command = ["afplay"]
        if gain is not None:
            command += ["-v", str(gain)]
        command.append(str(path))
    else:
        out("  (no media player available; falling back to synthesized narration)")
        return None
    processes = []
    try:
        primary = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(primary)
        playback = _MirroredPlayback(primary)
        speaker_process = None
        monitor = (_speaker_monitor_command(path, gain)
                   if hear_narration_enabled() else None)
        if monitor is not None:
            speaker_process = subprocess.Popen(
                monitor, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            processes.append(speaker_process)
            playback.add(speaker_process)
        _manage_speaker_monitor(playback, path, gain, speaker_process)
        return playback
    except OSError as exc:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        out(f"  (could not play narration: {exc})")
        return None


def start_recorded_narration(path: Path, out=print):
    """Play a capture's own recorded narration track (audio only).

    The audio runs straight through while the replay's pacing holds each
    action to its recorded moment, so one clock drives both and the voice
    stays on the action it is describing. Playback goes to the default
    output, which the teach session has already routed into the product's
    microphone. Returns the player process, or None to fall back to TTS.
    """
    return start_narration_playback(path, out=out)
