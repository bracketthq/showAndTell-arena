"""``app.toml`` — the declarative identity of one application.

This is the only file both the fixture host agent and the harness read. The
agent needs it to know what it can run; the harness needs it to know what to
open and who to log in as. Neither imports the application's Python to find
out, which is what keeps the two sides from disagreeing about a port or a
password the way the old three-copy arrangement did.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
import re
import tomllib

MANIFEST = "app.toml"
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class Port:
    """One published port. ``service`` is the container that publishes it."""

    name: str
    container: int
    host: int
    service: str

    def __post_init__(self) -> None:
        for label, value in (("container", self.container), ("host", self.host)):
            if not 1 <= value <= 65535:
                raise ValueError(
                    f"port {self.name!r} {label} must be between 1 and 65535")


@dataclass(frozen=True, slots=True)
class Manifest:
    """Everything about an application that is not code."""

    name: str
    label: str
    familiar_label: str
    root: Path
    compose_file: Path | None
    compose_service: str
    external: bool
    ports: dict[str, Port]
    health_http: str | None
    health_tcp: tuple[str, ...]
    credentials: dict[str, str]
    surface_id: str
    surface_entry: str
    snapshots: bool
    captures: bool
    fresh_install_bytes: int
    env: dict[str, str] = field(default_factory=dict)

    @property
    def app_port(self) -> Port:
        return self.ports["app"]

    def driver_module(self) -> str:
        return f"showAndTell.applications.{self.name}.driver"

    def state_module(self) -> str:
        return f"showAndTell.applications.{self.name}.state"

    def browser_module(self) -> str:
        return f"showAndTell.applications.{self.name}.browser"


def _table(data: dict, key: str) -> dict:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"[{key}] must be a table")
    return value


def load_manifest(root: Path | str) -> Manifest:
    """Read one application folder's manifest, validating it against the folder."""
    root = Path(root).resolve()
    path = root / MANIFEST
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"{root} has no {MANIFEST}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path} is not valid TOML: {exc}") from exc

    app = _table(data, "app")
    name = str(app.get("name", ""))
    if not _NAME.fullmatch(name):
        raise ValueError(f"{path}: invalid application name {name!r}")
    # The folder is the registry key, so a manifest that disagrees with it
    # would make every lookup answer for a different application.
    if name != root.name:
        raise ValueError(
            f"{path}: application name {name!r} does not match its folder "
            f"{root.name!r}")

    compose = _table(data, "compose")
    external = bool(compose.get("external", False))
    driver_managed = bool(compose.get("driver", False))
    compose_file: Path | None = None
    compose_service = str(compose.get("service", name))
    if not external and not driver_managed:
        compose_file = root / str(compose.get("file", "compose.yaml"))
        if not compose_file.is_file():
            raise ValueError(f"{path}: compose file {compose_file.name} does not exist")

    ports: dict[str, Port] = {}
    for key, value in _table(data, "ports").items():
        if not isinstance(value, dict):
            raise ValueError(f"{path}: port {key!r} must be a table")
        ports[key] = Port(
            name=key,
            container=int(value["container"]),
            host=int(value["host"]),
            service=str(value.get("service", compose_service)),
        )
    if "app" not in ports:
        raise ValueError(f"{path}: an 'app' port is required")

    health = _table(data, "health")
    health_http = health.get("http", "/")
    if health_http is not None:
        health_http = str(health_http)
        if not health_http.startswith("/"):
            raise ValueError(f"{path}: health.http must start with /")
    health_tcp = tuple(str(item) for item in health.get("tcp", ()))
    unknown = [item for item in health_tcp if item not in ports]
    if unknown:
        raise ValueError(f"{path}: health.tcp names undeclared ports {unknown}")

    credentials = _table(data, "credentials")
    capabilities = _table(data, "capabilities")
    storage = _table(data, "storage")
    fresh_install_bytes = int(storage.get("fresh_install_bytes", 0))
    if fresh_install_bytes < 0:
        raise ValueError(f"{path}: storage.fresh_install_bytes cannot be negative")
    surface = _table(data, "surface")
    entry = str(surface.get("entry", "/"))
    if not entry.startswith("/"):
        raise ValueError(f"{path}: surface.entry must start with /")

    return Manifest(
        name=name,
        label=str(app.get("label", name)),
        familiar_label=str(app.get("familiar_label", app.get("label", name))),
        root=root,
        compose_file=compose_file,
        compose_service=compose_service,
        external=external,
        ports=ports,
        health_http=health_http,
        health_tcp=health_tcp,
        # "email" rather than "username": the legacy credential contract calls
        # the first value email even for products that authenticate by name.
        credentials={"email": str(credentials.get("username", "")),
                     "password": str(credentials.get("password", ""))},
        surface_id=str(surface.get("id", name)),
        surface_entry=entry,
        snapshots=bool(capabilities.get("snapshots", False)),
        captures=bool(capabilities.get("capture", False)),
        fresh_install_bytes=fresh_install_bytes,
        env={str(k): str(v) for k, v in _table(data, "env").items()},
    )


# Host-port distance between successive replicas of one application. Shared
# by the agent (which registers the instances) and the ops tooling (which
# derives the firewall list), so it lives beside the manifest they both read.
PORT_STRIDE = 100


def replica(manifest: Manifest, index: int) -> Manifest:
    """The index-th instance of an application; 1 is the manifest itself.

    Replicas differ from their base only in name and published host ports,
    which is exactly what lets N of them share one compose file.
    """
    if index < 1:
        raise ValueError("replica index is 1-based")
    if index == 1:
        return manifest
    ports = {
        key: dataclasses.replace(port, host=port.host + PORT_STRIDE * (index - 1))
        for key, port in manifest.ports.items()
    }
    return dataclasses.replace(
        manifest, name=f"{manifest.name}_{index}", ports=ports)
