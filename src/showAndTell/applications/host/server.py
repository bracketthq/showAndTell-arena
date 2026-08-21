"""Process entry for the fixture host agent."""
from __future__ import annotations

import inspect
import os
import secrets
from collections.abc import Mapping
from pathlib import Path

import uvicorn

from .api import build_agent_api
from .protocol import AppDriver

DEFAULT_ROOT = Path.home() / ".showAndTell" / "fixture-host"
DEFAULT_TOKEN_PATH = Path.home() / ".showAndTell" / "fixture-host-token"


def ensure_token(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text().strip():
        return path.read_text().strip()
    token = secrets.token_hex(32)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(token)
    return token


def _replica_count(name: str, environ: Mapping[str, str]) -> int:
    key = f"SHOWANDTELL_{name.upper()}_REPLICAS"
    raw = environ.get(key, "1")
    try:
        count = int(raw)
    except ValueError:
        count = 0
    if count < 1:
        raise ValueError(f"{key} must be a positive integer, got {raw!r}")
    return count


def _assert_unique_ports(published: Mapping[str, Mapping[str, int]]) -> None:
    owners: dict[int, str] = {}
    for instance, ports in published.items():
        for port in ports.values():
            owner = owners.setdefault(port, instance)
            if owner != instance:
                raise ValueError(
                    f"host port {port} is published by both {owner} "
                    f"and {instance}")


def build_pools(
    root: Path, registry=None, environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, AppDriver], dict[str, tuple[str, ...]]]:
    """Every application instance this agent serves, plus its replica pools.

    SHOWANDTELL_<APP>_REPLICAS=N (default 1) expands an application into N
    instances: the base keeps its bare name and exact current behavior;
    replica i runs compose project showandtell-<app>-<i> on ports shifted
    +100·(i−1). Only drivers whose constructors accept the port kwargs can
    be replicated — anything else fails loudly rather than folding replicas
    onto one stack.
    """
    from showAndTell.applications import manifest as manifests
    from showAndTell.applications.registry import default_registry

    registry = registry or default_registry()
    environ = os.environ if environ is None else environ
    drivers: dict[str, AppDriver] = {}
    families: dict[str, tuple[str, ...]] = {}
    published: dict[str, dict[str, int]] = {}
    for name in registry.names():
        if not registry.has_driver(name):
            continue
        module = registry.module(name, "driver")
        accepted = inspect.signature(module.Driver.__init__).parameters
        instances: list[str] = []
        for index in range(1, _replica_count(name, environ) + 1):
            spec = manifests.replica(registry.manifest(name), index)
            kwargs: dict = {"root": root}
            if index > 1:
                kwargs["project"] = f"showandtell-{name}-{index}"
                for key, port in spec.ports.items():
                    kwargs["port" if key == "app" else f"{key}_port"] = port.host
                missing = sorted(set(kwargs) - set(accepted))
                if missing:
                    raise ValueError(
                        f"{name} driver cannot be replicated: its constructor "
                        f"does not accept {missing}")
            drivers[spec.name] = module.Driver(spec, **kwargs)
            published[spec.name] = {key: port.host
                                    for key, port in spec.ports.items()}
            instances.append(spec.name)
        families[name] = tuple(instances)
    _assert_unique_ports(published)
    return drivers, families


def default_drivers(root: Path, registry=None) -> dict[str, AppDriver]:
    """Every application folder that ships a driver plane.

    There is no list of known applications here: the agent serves whatever
    ``showAndTell.applications`` contains, so a new folder is a new endpoint with no
    change to this file.
    """
    return build_pools(root, registry)[0]


def run_agent(host: str, port: int, root: Path, token_path: Path) -> None:
    token = ensure_token(token_path)
    drivers, families = build_pools(root)
    api = build_agent_api(drivers, token, families=families)
    uvicorn.run(api, host=host, port=port, log_level="warning")
