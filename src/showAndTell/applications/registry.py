"""Discover applications beside this module in ``showAndTell.applications``.

There is no table of known applications anywhere in the codebase. A folder
holding an ``app.toml`` is an application; a folder that does not, is not.
Planes are loaded from their files rather than imported as a package, so the
application folders need no ``__init__.py`` and packaged data works without a
repository checkout.
"""
from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from types import ModuleType

from . import APPLICATIONS_ROOT
from .manifest import MANIFEST, Manifest, load_manifest

PLANES = ("driver", "state", "browser")


class UnknownApplication(KeyError):
    """No application by that name, or it has no such plane."""

    def __str__(self) -> str:  # KeyError quotes its argument; this reads better
        return self.args[0] if self.args else super().__str__()


class Registry:
    """One applications tree. Manifests are cached; planes are built per call."""

    def __init__(self, root: Path | str = APPLICATIONS_ROOT) -> None:
        self.root = Path(root)
        self._manifests: dict[str, Manifest] | None = None
        self._modules: dict[tuple[str, str], ModuleType] = {}
        # Reentrant: module() holds this while resolving a manifest, which
        # takes it again to populate the discovery cache.
        self._lock = threading.RLock()

    # -- discovery ---------------------------------------------------------
    def _discover(self) -> dict[str, Manifest]:
        found: dict[str, Manifest] = {}
        if not self.root.is_dir():
            return found
        for folder in sorted(self.root.iterdir()):
            if not (folder / MANIFEST).is_file():
                continue
            # A folder that declares itself an application but cannot be read
            # is a bug to fix, never a silent absence from the picker.
            found[folder.name] = load_manifest(folder)
        return found

    @property
    def manifests(self) -> dict[str, Manifest]:
        with self._lock:
            if self._manifests is None:
                self._manifests = self._discover()
            return self._manifests

    def names(self) -> tuple[str, ...]:
        return tuple(self.manifests)

    def manifest(self, name: str) -> Manifest:
        try:
            return self.manifests[name]
        except KeyError:
            raise UnknownApplication(
                f"unknown application {name!r}; available: "
                f"{sorted(self.manifests)}") from None

    def refresh(self) -> None:
        with self._lock:
            self._manifests = None

    # -- planes ------------------------------------------------------------
    def _plane_path(self, name: str, plane: str) -> Path:
        return self.manifest(name).root / f"{plane}.py"

    def _has(self, name: str, plane: str) -> bool:
        return self._plane_path(name, plane).is_file()

    def has_driver(self, name: str) -> bool:
        return self._has(name, "driver")

    def has_state(self, name: str) -> bool:
        return self._has(name, "state")

    def has_browser(self, name: str) -> bool:
        return self._has(name, "browser")

    def module(self, name: str, plane: str) -> ModuleType:
        """Load one plane, keyed so two applications never collide."""
        if plane not in PLANES:
            raise ValueError(f"unknown plane {plane!r}; expected one of {PLANES}")
        with self._lock:
            cached = self._modules.get((name, plane))
            if cached is not None:
                return cached
            path = self._plane_path(name, plane)
            if not path.is_file():
                raise UnknownApplication(
                    f"application {name!r} has no {plane} plane ({path.name})")
            # The key includes the tree so a test tree and the real one can
            # both be loaded in one process without shadowing each other.
            key = f"showAndTell_app_{abs(hash(str(self.root)))}_{name}_{plane}"
            spec = importlib.util.spec_from_file_location(key, path)
            if spec is None or spec.loader is None:
                raise UnknownApplication(f"cannot load {path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[key] = module
            spec.loader.exec_module(module)
            self._modules[(name, plane)] = module
            return module

    def driver(self, name: str, **kwargs):
        """Build this application's lifecycle plane. Runs beside its containers."""
        return self.module(name, "driver").Driver(self.manifest(name), **kwargs)

    def state(self, name: str, **kwargs):
        """Build this application's data plane. Runs beside the harness.

        A fresh instance every call: state planes remember the last seed they
        applied, and two sessions sharing one would reset each other's mail.
        """
        return self.module(name, "state").State(self.manifest(name), **kwargs)

    def browser(self, name: str) -> ModuleType:
        """This application's Playwright operations, as a module of functions."""
        return self.module(name, "browser")


_default: Registry | None = None
_default_lock = threading.Lock()


def default_registry() -> Registry:
    global _default
    with _default_lock:
        if _default is None:
            _default = Registry()
        return _default
