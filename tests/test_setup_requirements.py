from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_brewfile_declares_recorded_narration_runtime():
    brewfile = (ROOT / "Brewfile").read_text(encoding="utf-8")
    assert 'brew "git-lfs"' in brewfile
    assert 'brew "ffmpeg"' in brewfile
    assert 'brew "switchaudio-osx"' in brewfile
    assert 'cask "vb-cable"' in brewfile


def test_setup_installs_native_runtime_and_hydrates_recordings():
    launcher = (ROOT / "showAndTell").read_text(encoding="utf-8")
    setup = launcher.split("setup() {", 1)[1].split("command_name=", 1)[0]
    assert "install_native_runtime_dependencies" in setup
    assert "prepare_lfs_recordings" in setup
    assert "check_audio_runtime" in setup
    assert "git lfs pull --include='tasks/*/demo/recording.*'" in launcher


def test_setup_does_not_open_chrome_for_brackett():
    launcher = (ROOT / "showAndTell").read_text(encoding="utf-8")
    setup = launcher.split("setup() {", 1)[1].split("command_name=", 1)[0]
    assert "prepare_brackett_extension" not in setup
    assert "browser launch" not in setup
