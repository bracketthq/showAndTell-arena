"""Platform TTS: macOS ``say`` or neural Edge TTS on Linux.

On some Macs the unset/default voice is broken: `say` exits 0 but renders
~5ms of silence for any text. Never trust the default — probe it, and fall
back to a known-good built-in voice. Linux falls back to ``espeak-ng`` when
the online neural voice is unavailable. ``SHOWANDTELL_SAY_VOICE`` overrides the
local voice; ``SHOWANDTELL_NEURAL_VOICE`` overrides the neural voice.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FALLBACK_VOICES = ["Samantha", "Alex", "Fred"]
LINUX_DEFAULT_VOICE = "en-us+f3"
NEURAL_DEFAULT_VOICE = "en-US-AriaNeural"
NEURAL_DEFAULT_RATE = "-15%"
DEFAULT_RATE = 145
MIN_SECONDS = 0.3
_PROBE_TEXT = "voice probe"


def rendered_seconds(path: str) -> float:
    out = subprocess.run(["afinfo", path], capture_output=True, text=True,
                         timeout=30, check=True).stdout
    for line in out.splitlines():
        if "estimated duration" in line:
            return float(line.split()[-2])
    return 0.0


def _probe(args: list[str]) -> bool:
    with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as f:
        path = f.name
    try:
        subprocess.run(["say", *args, "-o", path, _PROBE_TEXT],
                       capture_output=True, timeout=30, check=True)
        return rendered_seconds(path) >= MIN_SECONDS
    except Exception:
        return False
    finally:
        if os.path.exists(path):
            os.unlink(path)


def voice_args() -> list[str] | None:
    """Extra TTS args for a working voice: [] (default is fine),
    ["-v", <voice>] (fallback/override), or None (no usable TTS here)."""
    env = os.environ.get("SHOWANDTELL_SAY_VOICE")
    if env:
        tts_bin = "say" if sys.platform == "darwin" else "espeak-ng"
        return ["-v", env] if shutil.which(tts_bin) else None
    if sys.platform != "darwin":
        return ["-v", LINUX_DEFAULT_VOICE] if shutil.which("espeak-ng") else None
    if shutil.which("say") is None or shutil.which("afinfo") is None:
        return None
    if _probe([]):
        return []
    for voice in FALLBACK_VOICES:
        if _probe(["-v", voice]):
            return ["-v", voice]
    return None


def say_cmd(text: str, voice_args: list[str]) -> list[str]:
    """The blocking speak-aloud command for this platform."""
    rate = os.environ.get("SHOWANDTELL_SAY_RATE", str(DEFAULT_RATE))
    if sys.platform == "darwin":
        return ["say", *voice_args, "-r", rate, text]
    return ["espeak-ng", *voice_args, "-s", rate, text]


def neural_cmd(text: str, media_path: str) -> list[str]:
    """Build the Edge neural-TTS synthesis command for one narration line."""
    voice = os.environ.get("SHOWANDTELL_NEURAL_VOICE", NEURAL_DEFAULT_VOICE)
    rate = os.environ.get("SHOWANDTELL_NEURAL_RATE", NEURAL_DEFAULT_RATE)
    return ["edge-tts", "--voice", voice, f"--rate={rate}", "--text", text,
            "--write-media", media_path]


def speak(text: str, voice_args: list[str]) -> None:
    """Speak one line, preferring a natural neural voice on Linux.

    Synthesis is intentionally blocking so demonstrations cannot outrun their
    narration. Any service/playback failure falls back to the local engine.
    """
    if sys.platform != "darwin" and shutil.which("edge-tts") and shutil.which("ffplay"):
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            media_path = f.name
        try:
            subprocess.run(neural_cmd(text, media_path), check=True, timeout=60,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["ffplay", "-nodisp", "-autoexit", "-loglevel", "error",
                            media_path], check=True, timeout=120)
            return
        except Exception:
            pass
        finally:
            if os.path.exists(media_path):
                os.unlink(media_path)
    if sys.platform == "darwin":
        from showAndTell.core import audio
        if os.environ.get(audio.VIRTUAL_CABLE_ENV):
            with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as f:
                media_path = f.name
            player = None
            try:
                rate = os.environ.get("SHOWANDTELL_SAY_RATE", str(DEFAULT_RATE))
                rendered = subprocess.run(
                    ["say", *voice_args, "-r", rate, "-o", media_path, text],
                    check=False,
                )
                if rendered.returncode == 0:
                    player = audio.start_narration_playback(Path(media_path))
                    if player is not None:
                        player.wait(timeout=120)
                        return
            except (OSError, subprocess.TimeoutExpired):
                if player is not None:
                    player.terminate()
                    try:
                        player.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        player.kill()
                pass
            finally:
                if os.path.exists(media_path):
                    os.unlink(media_path)
    subprocess.run(say_cmd(text, voice_args), check=False)
