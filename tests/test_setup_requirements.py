from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_brewfile_declares_recorded_narration_runtime():
    brewfile = (ROOT / "Brewfile").read_text(encoding="utf-8")
    assert 'brew "ffmpeg"' in brewfile
    assert 'brew "switchaudio-osx"' in brewfile
    assert 'cask "vb-cable"' in brewfile
    assert "git-lfs" not in brewfile


def test_setup_installs_native_runtime_without_git_lfs():
    launcher = (ROOT / "showAndTell").read_text(encoding="utf-8")
    setup = launcher.split("setup() {", 1)[1].split("command_name=", 1)[0]
    assert "install_native_runtime_dependencies" in setup
    assert "check_audio_runtime" in setup
    assert "git-lfs" not in launcher
    assert "git lfs" not in launcher


def test_setup_previews_every_install_and_requires_confirmation_first():
    launcher = (ROOT / "showAndTell").read_text(encoding="utf-8")
    setup = launcher.split("setup() {", 1)[1].split("command_name=", 1)[0]

    preview = setup.index("print_setup_plan")
    confirmation = setup.index("confirm_setup_plan")
    create_venv = setup.index('"$setup_python" -m venv')
    native_install = setup.index("install_native_runtime_dependencies")
    python_install = setup.index('"$venv_python" -m pip install')
    browser_install = setup.index('"$venv_python" -m playwright install chromium')

    assert preview < confirmation < create_venv
    assert confirmation < native_install
    assert confirmation < python_install
    assert confirmation < browser_install
    assert "No changes have been made yet." in launcher
    assert "Continue with setup? [y/N]" in launcher
    assert "Setup cancelled; nothing was installed." in launcher
    assert "Version/source" in launcher
    assert "Approx. size" in launcher
    assert "Why needed" in launcher
    assert "python_packages_size" in launcher
    assert "python_packages_status" in launcher
    assert "playwright_status" in launcher
    assert "dataset_size" in launcher
    assert "dataset_status" in launcher
    assert '"?blobs=true"' in launcher
    assert "Already installed" in launcher
    assert "Will install" in launcher
    assert "Plays and processes recorded narration audio" in launcher
    assert "Selects and restores macOS audio devices" in launcher
    assert "Routes narration through a virtual audio device" in launcher


def test_setup_does_not_open_chrome_for_brackett():
    launcher = (ROOT / "showAndTell").read_text(encoding="utf-8")
    setup = launcher.split("setup() {", 1)[1].split("command_name=", 1)[0]
    assert "prepare_brackett_extension" not in setup
    assert "browser launch" not in setup
