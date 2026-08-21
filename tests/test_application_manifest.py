"""app.toml is the only file the agent and the harness both read."""
import pytest

from showAndTell.applications.manifest import Manifest, load_manifest

MINIMAL = """
[app]
name = "roundcube"
label = "Roundcube"

[compose]
file = "compose.yaml"
service = "roundcube"

[ports]
app = { container = 80, host = 8082 }

[health]
http = "/"

[credentials]
username = "agent@showAndTell.test"
password = "showAndTell-mail"
"""


def _write(tmp_path, text, *, name="roundcube", compose=True):
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.toml").write_text(text)
    if compose:
        (root / "compose.yaml").write_text("services: {}\n")
    return root


def test_a_manifest_names_its_own_folder(tmp_path):
    manifest = load_manifest(_write(tmp_path, MINIMAL))
    assert manifest.name == "roundcube"
    assert manifest.label == "Roundcube"
    assert manifest.familiar_label == "Roundcube"
    assert manifest.fresh_install_bytes == 0
    assert manifest.root.name == "roundcube"


def test_a_manifest_can_declare_a_familiar_label(tmp_path):
    text = MINIMAL.replace(
        'label = "Roundcube"',
        'label = "Roundcube"\nfamiliar_label = "Email"',
    )

    manifest = load_manifest(_write(tmp_path, text))

    assert manifest.label == "Roundcube"
    assert manifest.familiar_label == "Email"


def test_a_manifest_can_declare_fresh_install_space(tmp_path):
    manifest = load_manifest(_write(
        tmp_path,
        MINIMAL + "\n[storage]\nfresh_install_bytes = 123456\n",
    ))

    assert manifest.fresh_install_bytes == 123456


def test_fresh_install_space_cannot_be_negative(tmp_path):
    with pytest.raises(ValueError, match="cannot be negative"):
        load_manifest(_write(
            tmp_path,
            MINIMAL + "\n[storage]\nfresh_install_bytes = -1\n",
        ))


def test_the_compose_file_resolves_inside_the_application_folder(tmp_path):
    root = _write(tmp_path, MINIMAL)
    assert load_manifest(root).compose_file == root / "compose.yaml"


def test_a_manifest_whose_name_disagrees_with_its_folder_is_refused(tmp_path):
    """The folder is the registry key; a mismatch makes lookups lie."""
    root = _write(tmp_path, MINIMAL.replace('name = "roundcube"', 'name = "mail"'))
    with pytest.raises(ValueError, match="folder"):
        load_manifest(root)


def test_a_missing_compose_file_is_refused(tmp_path):
    root = _write(tmp_path, MINIMAL, compose=False)
    with pytest.raises(ValueError, match="compose.yaml"):
        load_manifest(root)


def test_ports_carry_container_and_host_numbers(tmp_path):
    manifest = load_manifest(_write(tmp_path, MINIMAL))
    assert manifest.ports["app"].container == 80
    assert manifest.ports["app"].host == 8082


def test_a_port_defaults_to_the_applications_own_compose_service(tmp_path):
    manifest = load_manifest(_write(tmp_path, MINIMAL))
    assert manifest.ports["app"].service == "roundcube"


def test_a_secondary_port_may_name_a_different_service(tmp_path):
    """Roundcube's mail store is Dovecot: one app, two containers, two ports."""
    text = MINIMAL.replace(
        "app = { container = 80, host = 8082 }",
        'app = { container = 80, host = 8082 }\n'
        'imap = { container = 31143, host = 1143, service = "dovecot" }')
    manifest = load_manifest(_write(tmp_path, text))
    assert manifest.ports["imap"].service == "dovecot"
    assert manifest.ports["imap"].host == 1143


def test_an_app_port_is_required(tmp_path):
    text = MINIMAL.replace("app = { container = 80, host = 8082 }", "")
    with pytest.raises(ValueError, match="app"):
        load_manifest(_write(tmp_path, text))


def test_health_defaults_to_an_http_probe_on_the_app_port(tmp_path):
    manifest = load_manifest(_write(tmp_path, MINIMAL))
    assert manifest.health_http == "/"
    assert manifest.health_tcp == ()


def test_health_may_also_require_a_tcp_banner(tmp_path):
    text = MINIMAL.replace(
        "app = { container = 80, host = 8082 }",
        "app = { container = 80, host = 8082 }\n"
        "imap = { container = 31143, host = 1143 }",
    ).replace('http = "/"', 'http = "/"\ntcp = ["imap"]')
    assert load_manifest(_write(tmp_path, text)).health_tcp == ("imap",)


def test_a_tcp_health_probe_must_name_a_declared_port(tmp_path):
    text = MINIMAL.replace('http = "/"', 'http = "/"\ntcp = ["nope"]')
    with pytest.raises(ValueError, match="nope"):
        load_manifest(_write(tmp_path, text))


def test_credentials_travel_with_the_application(tmp_path):
    """One source of truth: the surface and the container read the same value."""
    manifest = load_manifest(_write(tmp_path, MINIMAL))
    assert manifest.credentials == {"email": "agent@showAndTell.test",
                                    "password": "showAndTell-mail"}


def test_capabilities_default_to_the_conservative_answer(tmp_path):
    manifest = load_manifest(_write(tmp_path, MINIMAL))
    assert manifest.snapshots is False
    assert manifest.captures is False


def test_capabilities_are_declared_not_inferred(tmp_path):
    text = MINIMAL + "\n[capabilities]\nsnapshots = true\ncapture = true\n"
    manifest = load_manifest(_write(tmp_path, text))
    assert manifest.snapshots is True
    assert manifest.captures is True


def test_the_surface_entry_defaults_to_the_application_root(tmp_path):
    assert load_manifest(_write(tmp_path, MINIMAL)).surface_entry == "/"


def test_a_surface_entry_may_be_a_login_path(tmp_path):
    text = MINIMAL + '\n[surface]\nentry = "/login"\n'
    assert load_manifest(_write(tmp_path, text)).surface_entry == "/login"


def test_an_external_application_needs_no_compose_file(tmp_path):
    """Escape hatch: something already running that this repo does not deploy."""
    text = MINIMAL.replace(
        '[compose]\nfile = "compose.yaml"\nservice = "roundcube"',
        "[compose]\nexternal = true")
    manifest = load_manifest(_write(tmp_path, text, compose=False))
    assert manifest.external is True
    assert manifest.compose_file is None


def test_a_manifest_is_immutable(tmp_path):
    manifest = load_manifest(_write(tmp_path, MINIMAL))
    with pytest.raises(Exception):
        manifest.name = "other"


def test_manifest_is_what_load_returns(tmp_path):
    assert isinstance(load_manifest(_write(tmp_path, MINIMAL)), Manifest)


def test_replica_one_is_the_manifest_itself():
    from showAndTell.applications.manifest import replica
    from showAndTell.applications.registry import default_registry

    manifest = default_registry().manifest("roundcube")
    assert replica(manifest, 1) is manifest


def test_replica_shifts_every_published_port_and_renames():
    from showAndTell.applications.manifest import PORT_STRIDE, replica
    from showAndTell.applications.registry import default_registry

    base = default_registry().manifest("roundcube")
    third = replica(base, 3)
    assert third.name == "roundcube_3"
    assert third.ports["app"].host == base.ports["app"].host + 2 * PORT_STRIDE
    assert third.ports["imap"].host == base.ports["imap"].host + 2 * PORT_STRIDE
    # Everything that is not a port or the name is untouched.
    assert third.credentials == base.credentials
    assert third.compose_file == base.compose_file
    # The original is not mutated.
    assert base.name == "roundcube"


def test_replica_index_must_be_positive():
    import pytest

    from showAndTell.applications.manifest import replica
    from showAndTell.applications.registry import default_registry

    with pytest.raises(ValueError, match="1-based"):
        replica(default_registry().manifest("roundcube"), 0)
