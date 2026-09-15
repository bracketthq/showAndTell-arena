"""Portable demonstration model, compiler, and narration contract."""
from __future__ import annotations

import pytest

from showAndTell.demonstration.compiler import compile_demonstration, render_demonstrate
from showAndTell.demonstration.model import Demonstration
from showAndTell.demonstration.narration import load_narration, make_narrator


SURFACE = {
    "id": "example",
    "application": "example",
    "url": "http://fixture.test/",
    "credentials": {},
    "extension": {"preserved": True},
}
EVENT = {
    "type": "goto",
    "page": "page",
    "url": "http://fixture.test/items",
}


def test_model_is_typed_and_preserves_extension_fields_losslessly():
    artifact = Demonstration.from_values(surfaces=[SURFACE], events=[EVENT])

    assert artifact.surfaces[0].id == "example"
    assert artifact.surfaces[0].application == "example"
    assert artifact.events[0].type == "goto"
    assert artifact.surface_dicts() == [SURFACE]

    # Callers cannot mutate the artifact through their original dictionaries.
    source = {**SURFACE}
    copy = Demonstration.from_values(surfaces=[source], events=[])
    source["url"] = "http://changed.test/"
    assert copy.surfaces[0].url == "http://fixture.test/"


def test_model_rejects_an_unroutable_surface_and_untyped_event():
    with pytest.raises(ValueError, match="surface requires a non-empty id"):
        Demonstration.from_values(surfaces=[{"url": "http://fixture.test"}], events=[])
    with pytest.raises(ValueError, match="event requires a non-empty type"):
        Demonstration.from_values(surfaces=[SURFACE], events=[{}])


def test_typed_compiler_preserves_the_existing_driver_contract():
    artifact = Demonstration.from_values(surfaces=[SURFACE], events=[EVENT])

    assert compile_demonstration(artifact) == render_demonstrate([EVENT], [SURFACE])


def test_narrator_loads_ordered_beats_and_speaks_each_key_once(tmp_path):
    demo = tmp_path / "demo"
    demo.mkdir()
    (demo / "narration_script.jsonl").write_text(
        '{"key":"action-001","text":"Open the queue"}\n'
        '{"key":"action-002","text":"Save the result"}\n',
        encoding="utf-8",
    )
    spoken = []
    output = []
    beats = load_narration(tmp_path)
    narrate = make_narrator(
        tmp_path,
        ["test-voice"],
        output.append,
        beats=beats,
        speaker=lambda text, voice: spoken.append((text, voice)),
    )

    narrate("action-001", object(), "click Queue")
    narrate("action-001", object(), "click Queue again")
    narrate("unknown", object(), "inspect")

    assert spoken == [("Open the queue", ["test-voice"])]
    assert output == ["  click Queue", "  click Queue again", "  inspect"]
