import shutil
import subprocess
import tempfile

import pytest

from showAndTell.core import tts
from showAndTell.core import audio

needs_say = pytest.mark.skipif(
    shutil.which("say") is None or shutil.which("afinfo") is None,
    reason="macOS say/afinfo not available")


@needs_say
def test_voice_args_yields_a_voice_that_actually_synthesizes():
    args = tts.voice_args()
    assert args is not None
    with tempfile.NamedTemporaryFile(suffix=".aiff") as f:
        subprocess.run(["say", *args, "-o", f.name, "testing one two three"],
                       check=True, timeout=30)
        assert tts.rendered_seconds(f.name) >= 0.3


def test_env_override_wins(monkeypatch):
    monkeypatch.setenv("SHOWANDTELL_SAY_VOICE", "Fred")
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/say")
    assert tts.voice_args() == ["-v", "Fred"]


def test_no_say_means_no_tts(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert tts.voice_args() is None


def test_voice_args_linux_requires_espeak(monkeypatch):
    monkeypatch.setattr(tts.sys, "platform", "linux")
    monkeypatch.delenv("SHOWANDTELL_SAY_VOICE", raising=False)
    monkeypatch.setattr(tts.shutil, "which", lambda n: None)
    assert tts.voice_args() is None
    monkeypatch.setattr(tts.shutil, "which",
                        lambda n: "/usr/bin/espeak-ng" if n == "espeak-ng" else None)
    assert tts.voice_args() == ["-v", "en-us+f3"]


def test_voice_args_env_override_on_linux(monkeypatch):
    monkeypatch.setattr(tts.sys, "platform", "linux")
    monkeypatch.setenv("SHOWANDTELL_SAY_VOICE", "en-us+f3")
    monkeypatch.setattr(tts.shutil, "which", lambda n: "/usr/bin/espeak-ng" if n == "espeak-ng" else None)
    assert tts.voice_args() == ["-v", "en-us+f3"]


def test_env_override_degrades_to_none_without_binary(monkeypatch):
    monkeypatch.setenv("SHOWANDTELL_SAY_VOICE", "Fred")
    monkeypatch.setattr(tts.shutil, "which", lambda n: None)
    monkeypatch.setattr(tts.sys, "platform", "darwin")
    assert tts.voice_args() is None
    monkeypatch.setattr(tts.sys, "platform", "linux")
    assert tts.voice_args() is None


def test_say_cmd_per_platform(monkeypatch):
    monkeypatch.delenv("SHOWANDTELL_SAY_RATE", raising=False)
    monkeypatch.setattr(tts.sys, "platform", "darwin")
    assert tts.say_cmd("hello there", ["-v", "Fred"]) == [
        "say", "-v", "Fred", "-r", "145", "hello there"]
    monkeypatch.setattr(tts.sys, "platform", "linux")
    assert tts.say_cmd("hello there", ["-v", "en-us+f3"]) == [
        "espeak-ng", "-v", "en-us+f3", "-s", "145", "hello there"]


def test_say_rate_env_override(monkeypatch):
    monkeypatch.setattr(tts.sys, "platform", "linux")
    monkeypatch.setenv("SHOWANDTELL_SAY_RATE", "125")
    assert tts.say_cmd("hello", []) == ["espeak-ng", "-s", "125", "hello"]


def test_mac_virtual_cable_renders_then_plays_tts(monkeypatch):
    commands = []

    class Result:
        returncode = 0

    class Player:
        def wait(self, timeout=None):
            commands.append(("wait", timeout))

    monkeypatch.setattr(tts.sys, "platform", "darwin")
    monkeypatch.setenv(audio.VIRTUAL_CABLE_ENV, "VB-CABLE")
    monkeypatch.setattr(tts.subprocess, "run",
                        lambda command, **kwargs: (commands.append(command), Result())[1])
    monkeypatch.setattr(audio, "start_narration_playback",
                        lambda path: (commands.append(("play", path)), Player())[1])

    tts.speak("hello", ["-v", "Fred"])

    render = commands[0]
    assert render[:5] == ["say", "-v", "Fred", "-r", "145"]
    assert "-o" in render and render[-1] == "hello"
    assert commands[-1] == ("wait", 120)


def test_neural_cmd_defaults_to_slow_female_voice(monkeypatch):
    monkeypatch.delenv("SHOWANDTELL_NEURAL_VOICE", raising=False)
    monkeypatch.delenv("SHOWANDTELL_NEURAL_RATE", raising=False)
    assert tts.neural_cmd("hello", "/tmp/voice.mp3") == [
        "edge-tts", "--voice", "en-US-AriaNeural", "--rate=-15%",
        "--text", "hello", "--write-media", "/tmp/voice.mp3"]


def test_neural_cmd_env_overrides(monkeypatch):
    monkeypatch.setenv("SHOWANDTELL_NEURAL_VOICE", "en-GB-SoniaNeural")
    monkeypatch.setenv("SHOWANDTELL_NEURAL_RATE", "-25%")
    assert tts.neural_cmd("hello", "voice.mp3")[1:5] == [
        "--voice", "en-GB-SoniaNeural", "--rate=-25%", "--text"]
