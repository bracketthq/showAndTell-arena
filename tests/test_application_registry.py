"""The registry is the directory listing: adding an app is adding a folder."""
import pytest

from showAndTell.applications.registry import Registry, UnknownApplication

MANIFEST = """
[app]
name = "{name}"
label = "{label}"

[compose]
file = "compose.yaml"
service = "{name}"

[ports]
app = {{ container = 80, host = {port} }}

[health]
http = "/"

[credentials]
username = "someone@showAndTell.test"
password = "secret"
"""

DRIVER = '''
class Driver:
    name = "{name}"

    def __init__(self, manifest, **kwargs):
        self.manifest = manifest
        self.kwargs = kwargs

    def status(self):
        return {{"state": "healthy", "ports": {{"app": 1}}}}
'''

STATE = '''
class State:
    application = "{name}"

    def __init__(self, manifest):
        self.manifest = manifest
'''


def _app(root, name, *, port=8000, label=None, driver=True, state=True):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "app.toml").write_text(
        MANIFEST.format(name=name, label=label or name.title(), port=port))
    (folder / "compose.yaml").write_text("services: {}\n")
    if driver:
        (folder / "driver.py").write_text(DRIVER.format(name=name))
    if state:
        (folder / "state.py").write_text(STATE.format(name=name))
    return folder


def test_discovery_finds_every_folder_holding_a_manifest(tmp_path):
    _app(tmp_path, "roundcube", port=8082)
    _app(tmp_path, "erpnext", port=8080)
    assert Registry(tmp_path).names() == ("erpnext", "roundcube")


def test_a_folder_without_a_manifest_is_not_an_application(tmp_path):
    _app(tmp_path, "roundcube")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "README.md").write_text("not an app")
    assert Registry(tmp_path).names() == ("roundcube",)


def test_an_empty_applications_root_is_not_an_error(tmp_path):
    assert Registry(tmp_path).names() == ()


def test_a_manifest_is_reachable_by_name(tmp_path):
    _app(tmp_path, "roundcube", label="Roundcube")
    assert Registry(tmp_path).manifest("roundcube").label == "Roundcube"


def test_an_unknown_application_names_what_is_available(tmp_path):
    _app(tmp_path, "roundcube")
    with pytest.raises(UnknownApplication, match="roundcube"):
        Registry(tmp_path).manifest("nope")


def test_a_broken_manifest_fails_loudly_at_discovery(tmp_path):
    """A folder that cannot be read is a bug to fix, never a silent absence."""
    folder = tmp_path / "broken"
    folder.mkdir()
    (folder / "app.toml").write_text("[app]\nname = 'mismatch'\n")
    with pytest.raises(ValueError, match="broken"):
        Registry(tmp_path).names()


# -- planes ---------------------------------------------------------------

def test_the_driver_plane_is_loaded_from_the_folder(tmp_path):
    _app(tmp_path, "roundcube")
    driver = Registry(tmp_path).driver("roundcube")
    assert driver.name == "roundcube"
    assert driver.manifest.name == "roundcube"


def test_driver_construction_passes_through_keyword_arguments(tmp_path):
    """The agent injects a fake command runner in tests, a real one in production."""
    _app(tmp_path, "roundcube")
    driver = Registry(tmp_path).driver("roundcube", runner="fake")
    assert driver.kwargs == {"runner": "fake"}


def test_the_state_plane_is_loaded_from_the_folder(tmp_path):
    _app(tmp_path, "roundcube")
    assert Registry(tmp_path).state("roundcube").application == "roundcube"


def test_each_call_builds_a_fresh_state_plane(tmp_path):
    """State planes remember the last seed; two sessions must not share one."""
    _app(tmp_path, "roundcube")
    registry = Registry(tmp_path)
    assert registry.state("roundcube") is not registry.state("roundcube")


def test_an_application_without_a_state_plane_says_so_by_name(tmp_path):
    _app(tmp_path, "kiwix", state=False)
    registry = Registry(tmp_path)
    assert registry.has_state("kiwix") is False
    with pytest.raises(UnknownApplication, match="state"):
        registry.state("kiwix")


def test_an_application_without_a_driver_plane_says_so_by_name(tmp_path):
    _app(tmp_path, "kiwix", driver=False)
    assert Registry(tmp_path).has_driver("kiwix") is False


def test_two_applications_load_independent_modules(tmp_path):
    """Path-loaded modules must not collide on a shared module name."""
    _app(tmp_path, "roundcube", port=8082)
    _app(tmp_path, "erpnext", port=8080)
    registry = Registry(tmp_path)
    assert registry.driver("roundcube").name == "roundcube"
    assert registry.driver("erpnext").name == "erpnext"


# -- the real applications folder -----------------------------------------

def test_the_repository_ships_the_agent_backed_applications():
    from showAndTell.applications.registry import default_registry

    assert set(default_registry().names()) >= {"erpnext", "onlyoffice", "roundcube"}


def test_every_shipped_application_has_a_driver_and_a_manifest():
    from showAndTell.applications.registry import default_registry

    registry = default_registry()
    for name in registry.names():
        assert registry.has_driver(name), f"{name} has no driver.py"
        assert registry.manifest(name).app_port.host > 0


def test_every_relative_bind_mount_resolves_to_something_that_exists():
    """A bind whose source is missing does not fail — Docker invents it.

    It silently creates an empty directory at that path, so a mounted config
    file becomes a directory, is never loaded, and the application fails at
    runtime with an error that names none of this. Roundcube's TLS config was
    mounted from a stale relative path after the move to applications/ and
    login died with "Connection to storage server failed".
    """
    import re

    from showAndTell.applications.registry import default_registry

    registry = default_registry()
    broken = []
    for name in registry.names():
        compose = registry.manifest(name).compose_file
        if compose is None:
            continue
        for line in compose.read_text().splitlines():
            match = re.match(r"\s*-\s+(\.{1,2}/[^:]+):", line)
            if not match:
                continue
            source = compose.parent / match.group(1)
            if not source.exists():
                broken.append(f"{name}: {match.group(1)} does not exist")
            elif source.suffix and not source.is_file():
                # The invented directory Docker leaves behind after a miss —
                # it makes a re-run of this check pass while the mount is
                # still broken, so a file-shaped source must be a file.
                broken.append(f"{name}: {match.group(1)} is not a file")
    assert broken == []
