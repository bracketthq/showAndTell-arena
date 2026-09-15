import sys

import pytest

from showAndTell.students import ProductAdapter, Session, get_student, list_students


def test_public_student_surface_and_stable_listing():
    assert issubclass(ProductAdapter, object)
    assert Session.__name__ == "Session"
    assert list_students() == ("claude", "brackett", "codex")


def test_registry_imports_adapter_lazily():
    sys.modules.pop("showAndTell.students.codex", None)
    assert "showAndTell.students.codex" not in sys.modules
    assert get_student("codex").name == "codex"
    assert "showAndTell.students.codex" in sys.modules


def test_unknown_student_lists_valid_names():
    with pytest.raises(ValueError, match="claude.*brackett.*codex"):
        get_student("unknown")
