"""Enforce the hard import boundaries from the package architecture spec."""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[1] / "src" / "showAndTell"
RETIRED_IMPORTS = {
    "showAndTell.managed_capture",
    "showAndTell.replay",
    "showAndTell.teacher.codegen",
    "showAndTell.capture.events",
    "applications",
    "showAndTell.applications.browser_context",
    "showAndTell.applications.browser_runtime",
    "showAndTell.applications.compose_driver",
    "showAndTell.applications.docker_image",
    "showAndTell.applications.fixtures",
    "showAndTell.applications.hostagent",
    "showAndTell.applications.public_url",
    "showAndTell.applications.seedform",
    "showAndTell.applications.erpnext.migration",
}


def _module(path: Path) -> tuple[str, str]:
    relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
    parts = ("showAndTell", *relative.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    module = ".".join(parts)
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    return module, package


def _imports(path: Path) -> set[str]:
    _name, package = _module(path)
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), path)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            target = node.module or ""
            if node.level:
                target = importlib.util.resolve_name(
                    "." * node.level + target, package)
            if target:
                found.add(target)
            found.update(
                f"{target}.{alias.name}" for alias in node.names
                if target and alias.name != "*"
            )
    return found


def test_hard_package_import_boundaries():
    failures: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        module, _package = _module(path)
        imports = _imports(path)
        if module.startswith("showAndTell.core"):
            failures += [f"{module} imports {name}"
                         for name in imports
                         if name.startswith("showAndTell.")
                         and not name.startswith("showAndTell.core")]
        if module.startswith("showAndTell.quiz"):
            failures += [f"{module} imports {name}"
                         for name in imports if name.startswith("showAndTell.students")]
        if module.startswith("showAndTell.students"):
            failures += [f"{module} imports {name}"
                         for name in imports if name.startswith("showAndTell.teacher")]
        if module.startswith("showAndTell.demonstration"):
            forbidden = (
                "showAndTell.capture", "showAndTell.player", "showAndTell.teacher",
                "showAndTell.students", "showAndTell.quiz", "showAndTell.bundles",
            )
            failures += [f"{module} imports {name}"
                         for name in imports
                         if any(name == prefix or name.startswith(prefix + ".")
                                for prefix in forbidden)]
        if module.startswith("showAndTell.capture"):
            failures += [f"{module} imports {name}"
                         for name in imports
                         if name.startswith(("showAndTell.player", "showAndTell.teacher"))]
        if module.startswith("showAndTell.player"):
            failures += [f"{module} imports {name}"
                         for name in imports
                         if name.startswith(("showAndTell.capture", "showAndTell.teacher"))]
        if module.startswith("showAndTell.applications"):
            forbidden = (
                "showAndTell.students", "showAndTell.teacher", "showAndTell.player",
                "showAndTell.capture", "showAndTell.quiz", "showAndTell.bundles",
                "showAndTell.tasks",
            )
            failures += [f"{module} imports {name}"
                         for name in imports
                         if any(name == prefix or name.startswith(prefix + ".")
                                for prefix in forbidden)]
        failures += [f"{module} imports CLI module {name}"
                     for name in imports if name.startswith("showAndTell.cli")]
        failures += [f"{module} imports retired path {name}"
                     for name in imports
                     if any(name == retired or name.startswith(retired + ".")
                            for retired in RETIRED_IMPORTS)]
    assert failures == []
