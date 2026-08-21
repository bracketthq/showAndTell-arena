"""Lazy registry for in-repository student adapters."""
from __future__ import annotations

from importlib import import_module

from .base import ProductAdapter

_STUDENTS = {
    "claude": "showAndTell.students.claude:ClaudeAdapter",
    "brackett": "showAndTell.students.brackett:BrackettAdapter",
    "codex": "showAndTell.students.codex:CodexAdapter",
}


def list_students() -> tuple[str, ...]:
    """Return registry keys in their stable CLI display order."""
    return tuple(_STUDENTS)


def get_student(name: str) -> ProductAdapter:
    """Import and instantiate one adapter, with useful unknown-name errors."""
    try:
        target = _STUDENTS[name]
    except KeyError:
        valid = ", ".join(repr(item) for item in list_students())
        raise ValueError(f"unknown student {name!r}; available: {valid}") from None
    module_name, class_name = target.split(":", 1)
    adapter_type = getattr(import_module(module_name), class_name)
    adapter = adapter_type()
    if not isinstance(adapter, ProductAdapter):
        raise TypeError(f"registered student {name!r} is not a ProductAdapter")
    return adapter
