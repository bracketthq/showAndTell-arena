from pathlib import Path
import threading

from showAndTell.core import audio


def test_virtual_cable_no_op_when_absent(monkeypatch):
    monkeypatch.setattr(audio.sys, "platform", "darwin")
    monkeypatch.setattr(audio, "_virtual_cable", lambda: None)
    msgs = []
    with audio.virtual_cable_narration(out=msgs.append) as routed:
        assert routed is False
    assert any("not installed" in m for m in msgs)


def test_real_device_skips_virtual(monkeypatch):
    monkeypatch.setattr(audio, "_devices",
                        lambda kind: ["VB-CABLE", "ZoomAudioDevice", "MacBook Pro Speakers"])
    assert audio._real_device("output") == "MacBook Pro Speakers"


def test_virtual_cable_must_exist_as_input_and_output(monkeypatch):
    monkeypatch.setattr(audio, "_has_switch", lambda: True)
    monkeypatch.setattr(
        audio, "_devices",
        lambda kind: ["VB-CABLE"] if kind == "output" else ["MacBook Microphone"])
    assert audio._virtual_cable() is None
    monkeypatch.setattr(audio, "_devices", lambda _kind: ["VB-Cable"])
    assert audio._virtual_cable() == "VB-Cable"


def test_virtual_cable_sets_and_restores(monkeypatch):
    monkeypatch.setattr(audio.sys, "platform", "darwin")
    calls = []
    monkeypatch.setattr(audio, "_virtual_cable", lambda: "VB-CABLE")
    monkeypatch.setattr(audio, "_current", lambda kind: f"prev-{kind}")
    monkeypatch.setattr(audio, "_real_device", lambda kind: f"real-{kind}")
    monkeypatch.setattr(audio, "_set_device", lambda kind, dev: calls.append((kind, dev)))

    with audio.virtual_cable_narration(out=lambda _: None) as routed:
        assert routed is True
        assert ("output", "VB-CABLE") in calls and ("input", "VB-CABLE") in calls
        assert audio.os.environ[audio.VIRTUAL_CABLE_ENV] == "VB-CABLE"
    assert calls[-2:] == [("output", "prev-output"), ("input", "prev-input")]
    assert audio.VIRTUAL_CABLE_ENV not in audio.os.environ


def test_virtual_cable_restores_on_exception(monkeypatch):
    monkeypatch.setattr(audio.sys, "platform", "darwin")
    calls = []
    monkeypatch.setattr(audio, "_virtual_cable", lambda: "VB-CABLE")
    monkeypatch.setattr(audio, "_current", lambda kind: f"prev-{kind}")
    monkeypatch.setattr(audio, "_set_device", lambda kind, dev: calls.append((kind, dev)))
    try:
        with audio.virtual_cable_narration(out=lambda _: None):
            raise ValueError("boom")
    except ValueError:
        pass
    assert ("output", "prev-output") in calls and ("input", "prev-input") in calls


def test_pulse_narration_noop_without_pactl(monkeypatch):
    monkeypatch.setattr(audio.shutil, "which", lambda n: None)
    with audio.pulse_narration(out=lambda *a: None) as ok:
        assert ok is False


def test_pulse_narration_routes_and_restores(monkeypatch):
    calls = []

    def fake_run(cmd, capture_output=True, text=True):
        calls.append(cmd)
        class R:
            stdout = ""
        if cmd[1] == "get-default-sink":
            R.stdout = "prev_sink\n"
        elif cmd[1] == "get-default-source":
            R.stdout = "prev_src\n"
        elif cmd[1] == "load-module":
            R.stdout = "42\n" if "module-null-sink" in cmd else "43\n"
        return R()

    monkeypatch.setattr(audio.shutil, "which", lambda n: "/usr/bin/pactl")
    monkeypatch.setattr(audio.subprocess, "run", fake_run)
    with audio.pulse_narration(out=lambda *a: None) as ok:
        assert ok is True
        assert ["pactl", "set-default-sink", audio.PULSE_SINK] in calls
        # The default mic must be the REMAP source, never the raw monitor:
        # Chromium's pulse backend filters monitor sources out of mic
        # enumeration, so a bare .monitor default leaves getUserMedia with
        # NotFoundError (measured on the webarena VM).
        assert any(c[1] == "load-module" and "module-remap-source" in c
                   and f"master={audio.PULSE_SINK}.monitor" in " ".join(c)
                   for c in calls)
        assert ["pactl", "set-default-source", audio.PULSE_MIC] in calls
        assert not any(c[:2] == ["pactl", "set-default-source"]
                       and c[-1] == f"{audio.PULSE_SINK}.monitor" for c in calls)
    assert ["pactl", "set-default-sink", "prev_sink"] in calls
    assert ["pactl", "set-default-source", "prev_src"] in calls
    # Both modules unloaded, the remap (43) before its master sink (42).
    joined = [" ".join(c) for c in calls]
    assert joined.index("pactl unload-module 43") < joined.index("pactl unload-module 42")


def test_pulse_hear_narration_mirrors_to_previous_speaker(monkeypatch):
    calls = []
    module_ids = iter(("42", "43", "44"))

    def fake_pactl(*args):
        calls.append(args)
        if args[0] == "get-default-sink":
            return "real-speaker"
        if args[0] == "get-default-source":
            return "real-mic"
        if args[0] == "load-module":
            return next(module_ids)
        return ""

    monkeypatch.setenv(audio.HEAR_NARRATION_ENV, "1")
    monkeypatch.setattr(audio.shutil, "which", lambda name: "/usr/bin/pactl")
    monkeypatch.setattr(audio, "_pactl", fake_pactl)

    with audio.pulse_narration(out=lambda _message: None) as routed:
        assert routed is True

    assert ("load-module", "module-loopback",
            f"source={audio.PULSE_SINK}.monitor", "sink=real-speaker",
            "latency_msec=20") in calls
    assert calls.index(("unload-module", "44")) < calls.index(
        ("unload-module", "43"))


def test_virtual_cable_delegates_to_pulse_on_linux(monkeypatch):
    calls = []

    def fake_run(cmd, capture_output=True, text=True):
        calls.append(cmd)
        class R:
            stdout = {"get-default-sink": "prev\n",
                      "get-default-source": "prev\n",
                      "load-module": "7\n"}.get(cmd[1], "")
        return R()

    monkeypatch.setattr(audio.sys, "platform", "linux")
    monkeypatch.setattr(audio.shutil, "which",
                        lambda n: "/usr/bin/pactl" if n == "pactl" else None)
    monkeypatch.setattr(audio.subprocess, "run", fake_run)
    with audio.virtual_cable_narration(out=lambda *a: None) as ok:
        assert ok is True
    assert ["pactl", "set-default-sink", audio.PULSE_SINK] in calls
    assert ["pactl", "unload-module", "7"] in calls


def test_virtual_cable_never_restores_a_stuck_virtual_default(monkeypatch):
    monkeypatch.setattr(audio.sys, "platform", "darwin")
    calls = []
    monkeypatch.setattr(audio, "_virtual_cable", lambda: "VB-CABLE")
    monkeypatch.setattr(audio, "_current", lambda kind: "VB-CABLE")
    monkeypatch.setattr(audio, "_real_device", lambda kind: f"real-{kind}")
    monkeypatch.setattr(audio, "_set_device",
                        lambda kind, dev: calls.append((kind, dev)))

    with audio.virtual_cable_narration(out=lambda _: None):
        calls.clear()

    assert ("output", "real-output") in calls
    assert ("input", "real-input") in calls
    assert (("output", "VB-CABLE") not in calls
            and ("input", "VB-CABLE") not in calls)


def test_ensure_real_input_heals_a_stuck_virtual_mic(monkeypatch):
    calls = []
    monkeypatch.setattr(audio, "_has_switch", lambda: True)
    monkeypatch.setattr(audio, "_current", lambda kind: "VB-CABLE")
    monkeypatch.setattr(audio, "_real_device", lambda kind: f"real-{kind}")
    monkeypatch.setattr(audio, "_set_device",
                        lambda kind, dev: calls.append((kind, dev)))

    msgs = []
    audio.ensure_real_devices(out=msgs.append)

    assert ("input", "real-input") in calls
    assert ("output", "real-output") in calls
    assert any("VB-CABLE" in m for m in msgs)


def test_ensure_real_devices_leaves_healthy_defaults_alone(monkeypatch):
    calls = []
    monkeypatch.setattr(audio, "_has_switch", lambda: True)
    monkeypatch.setattr(audio, "_current", lambda kind: f"MacBook Pro {kind}")
    monkeypatch.setattr(audio, "_set_device",
                        lambda kind, dev: calls.append((kind, dev)))

    audio.ensure_real_devices(out=lambda _: None)

    assert calls == []


def test_recorded_narration_plays_into_virtual_cable(monkeypatch):
    cmds = []

    class Proc:
        pass

    monkeypatch.setenv(audio.VIRTUAL_CABLE_ENV, "VB-CABLE")
    monkeypatch.delenv(audio.HEAR_NARRATION_ENV, raising=False)
    monkeypatch.setattr(audio.shutil, "which",
                        lambda name: "/opt/x/ffplay" if name == "ffplay" else None)
    monkeypatch.setattr(audio.subprocess, "Popen",
                        lambda cmd, **kw: (cmds.append(cmd), Proc())[1])

    assert audio.start_recorded_narration(Path("n.mov"), out=lambda _m: None)
    joined = " ".join(cmds[0])
    assert "volume=" not in joined


def test_recorded_narration_plays_at_full_level_through_speakers(monkeypatch):
    """Brackett's speaker route plays into the real mic across the room —
    attenuation there would starve the physical capture."""
    cmds = []

    class Proc:
        pass

    monkeypatch.delenv(audio.VIRTUAL_CABLE_ENV, raising=False)
    monkeypatch.delenv(audio.HEAR_NARRATION_ENV, raising=False)
    monkeypatch.setattr(audio.shutil, "which",
                        lambda name: "/opt/x/ffplay" if name == "ffplay" else None)
    monkeypatch.setattr(audio.subprocess, "Popen",
                        lambda cmd, **kw: (cmds.append(cmd), Proc())[1])

    assert audio.start_recorded_narration(Path("n.mov"), out=lambda _m: None)
    assert "volume=" not in " ".join(cmds[0])


def test_hear_narration_mirrors_to_physical_speaker(monkeypatch):
    commands = []

    class Proc:
        def poll(self):
            return None

    monkeypatch.setenv(audio.HEAR_NARRATION_ENV, "1")
    monkeypatch.setattr(audio, "_real_device", lambda kind: "MacBook Pro Speakers")
    monkeypatch.setattr(audio, "_audiotoolbox_device_index", lambda name: 3)
    monkeypatch.setattr(audio.shutil, "which",
                        lambda name: f"/opt/x/{name}" if name in ("ffplay", "ffmpeg") else None)
    monkeypatch.setattr(audio.subprocess, "Popen",
                        lambda cmd, **kw: (commands.append(cmd), Proc())[1])

    playback = audio.start_narration_playback(Path("n.aiff"))

    assert playback is not None
    assert len(commands) == 2
    assert commands[0][0] == "ffplay"
    assert "audiotoolbox" in commands[1]
    assert commands[1][commands[1].index("-audio_device_index") + 1] == "3"


def test_live_control_can_start_and_stop_only_the_speaker_monitor(
        tmp_path, monkeypatch):
    control = tmp_path / "hear"
    control.write_text("0")
    started = threading.Event()
    stopped = threading.Event()

    class PrimaryProcess:
        returncode = None

        def poll(self):
            return self.returncode

    class SpeakerProcess:
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15
            stopped.set()

    monkeypatch.setenv(audio.HEAR_NARRATION_CONTROL_ENV, str(control))
    monkeypatch.setattr(
        audio, "_speaker_monitor_command",
        lambda path, gain=None, start_at=0: ["speaker", str(start_at)])
    monkeypatch.setattr(
        audio.subprocess, "Popen",
        lambda *args, **kwargs: (started.set(), SpeakerProcess())[1])
    primary = PrimaryProcess()
    playback = audio._MirroredPlayback(primary)
    audio._manage_speaker_monitor(playback, Path("n.mov"), None, None)

    control.write_text("1")
    assert started.wait(2)
    control.write_text("0")
    assert stopped.wait(2)
    assert audio.hear_narration_enabled() is False
    assert primary.poll() is None
    primary.returncode = 0
