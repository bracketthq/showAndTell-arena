"""The quiz-evidence closure standard, over every enrolled task.

A question that cites the demonstration is only as good as the citation. This
module runs the shared closure assertion over the enrolled tasks and then
refutes it one rule at a time on synthetic tasks, so every rule is known to be
load-bearing rather than decorative.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests._quiz_audit import (
    CAPTURE_DIR,
    ENROLLED,
    assert_quiz_evidence_is_closed,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("task", ENROLLED)
def test_quiz_evidence_resolves_to_committed_capture_artifacts(task: str) -> None:
    assert_quiz_evidence_is_closed(ROOT / "tasks" / task)


TASK_NAME = "sample-task"
FRAMES = ("screenshot_line1.png", "screenshot_line2.png")


def _frame_bytes(name: str) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + name.encode()


def _metadata_text(step: str) -> str:
    """The capture metadata a beat records beside its frame.

    Stands in for the accessibility tree and rendered text a graded agent
    actually reads: the frame is pixels, this is the words.
    """

    return f"# {step}\nStaticText: captured at {step}\n"


def _world() -> dict[str, Any]:
    """A synthetic task whose evidence is closed in both directions."""

    return {
        "narration": [
            {"key": "open_inbox", "text": "I open the inbox."},
            {"key": "decide", "text": "I record the decision."},
        ],
        "manifest": {
            "version": 1,
            "task": TASK_NAME,
            "capture_method": "Full-page screenshot at every on_step beat.",
            "steps": [
                {
                    "line": index,
                    "step": step,
                    "description": f"Visible {step}.",
                    "screenshot": FRAMES[index - 1],
                    "metadata": f"metadata/line{index}.md",
                }
                for index, step in enumerate(("open_inbox", "decide"), start=1)
            ],
        },
        "frames": dict.fromkeys(FRAMES),
        "extra_metadata": {},
        "questions": [
            {
                "id": "q1",
                "evidence": [
                    {
                        "type": "narration",
                        "key": "open_inbox",
                        "text": "I open the inbox.",
                    },
                    {
                        "type": "screenshot",
                        "step": "open_inbox",
                        "line": 1,
                        "path": f"tasks/{TASK_NAME}/{CAPTURE_DIR}/{FRAMES[0]}",
                        "description": "Visible inbox before any decision.",
                    },
                ],
            },
            {
                "id": "q2",
                "evidence": [
                    {
                        "type": "screenshot",
                        "step": "decide",
                        "path": f"tasks/{TASK_NAME}/{CAPTURE_DIR}/{FRAMES[1]}",
                        "description": "Visible recorded decision.",
                    },
                ],
            },
        ],
    }


def _task(tmp_path: Path, mutate: Callable[[dict], None] | None = None) -> Path:
    """Materialise a synthetic task, applying ``mutate`` to it beforehand."""

    world = _world()
    if mutate is not None:
        mutate(world)

    task_dir = tmp_path / "tasks" / TASK_NAME
    capture_dir = task_dir / CAPTURE_DIR
    (capture_dir / "metadata").mkdir(parents=True)
    (task_dir / "quiz").mkdir(parents=True)
    (task_dir / "demo").mkdir(parents=True)

    digests: dict[str, str] = {}
    for name, payload in world["frames"].items():
        payload = _frame_bytes(name) if payload is None else payload
        (capture_dir / name).write_bytes(payload)
        digests[name] = hashlib.sha256(payload).hexdigest()

    manifest = world["manifest"] or {}
    for step in manifest.get("steps", []):
        step.setdefault("sha256", digests.get(step["screenshot"], ""))
        text = step.pop("_metadata_text", None) or _metadata_text(step["step"])
        if not step.pop("_drop_metadata_digest", False):
            step.setdefault(
                "metadata_sha256", hashlib.sha256(text.encode()).hexdigest()
            )
        metadata = capture_dir / step["metadata"]
        if step.pop("_drop_metadata", False):
            continue
        metadata.parent.mkdir(parents=True, exist_ok=True)
        metadata.write_text(text)
    for name, text in world["extra_metadata"].items():
        (capture_dir / "metadata" / name).write_text(text)
    if world["manifest"] is not None:
        (capture_dir / "manifest.json").write_text(json.dumps(manifest))

    by_step = {step["step"]: step for step in manifest.get("steps", [])}
    for question in world["questions"]:
        for entry in question.get("evidence", []):
            if entry.get("type") != "screenshot":
                continue
            recorded = by_step.get(entry.get("step"), {})
            entry.setdefault("sha256", recorded.get("sha256", ""))

    (task_dir / "quiz/questions.json").write_text(
        json.dumps({"questions": world["questions"]})
    )
    narration = world["narration"]
    if narration is not None:
        (task_dir / "demo/narration_script.jsonl").write_text(
            "\n".join(json.dumps(row) for row in narration)
        )
    return task_dir


def _rejects(task_dir: Path, message: str) -> None:
    with pytest.raises(AssertionError, match=message):
        assert_quiz_evidence_is_closed(task_dir)


def test_a_task_whose_evidence_is_closed_passes(tmp_path) -> None:
    assert_quiz_evidence_is_closed(_task(tmp_path))


def test_quiz_without_questions_is_rejected(tmp_path) -> None:
    _rejects(
        _task(tmp_path, lambda world: world.update(questions=[])),
        "quiz has no questions",
    )


def test_question_citing_no_evidence_is_rejected(tmp_path) -> None:
    def mutate(world: dict) -> None:
        world["questions"][1]["evidence"] = []

    _rejects(_task(tmp_path, mutate), "q2: no evidence")


def test_unrecognised_evidence_type_is_rejected(tmp_path) -> None:
    def mutate(world: dict) -> None:
        world["questions"][0]["evidence"][0]["type"] = "hunch"

    _rejects(_task(tmp_path, mutate), "q1: evidence type must be one of")


def test_evidence_addressed_by_wall_clock_is_rejected(tmp_path) -> None:
    """A timestamped citation points at a replay nobody can reproduce."""

    def mutate(world: dict) -> None:
        world["questions"][0]["evidence"][1]["timestamp"] = "2026-07-27T10:00:00Z"

    _rejects(_task(tmp_path, mutate), "q1: evidence carries a 'timestamp' key")


def test_missing_narration_script_is_rejected(tmp_path) -> None:
    _rejects(
        _task(tmp_path, lambda world: world.update(narration=None)),
        "narration_script.jsonl is missing",
    )


def test_repeated_narration_keys_are_rejected(tmp_path) -> None:
    def mutate(world: dict) -> None:
        world["narration"][1]["key"] = "open_inbox"

    _rejects(_task(tmp_path, mutate), "narration keys are not unique")


def test_quoting_a_beat_the_script_does_not_have_is_rejected(tmp_path) -> None:
    def mutate(world: dict) -> None:
        world["questions"][0]["evidence"][0]["key"] = "open_outbox"

    _rejects(_task(tmp_path, mutate), "narration key 'open_outbox' is not in the script")


def test_quoting_a_beat_inexactly_is_rejected(tmp_path) -> None:
    """Paraphrasing the script lets the taught wording drift out from under the
    question that was written against it."""

    def mutate(world: dict) -> None:
        world["questions"][0]["evidence"][0]["text"] = "I open the inbox"

    _rejects(_task(tmp_path, mutate), "narration 'open_inbox' text does not match")


def test_citing_a_beat_the_manifest_never_captured_is_rejected(tmp_path) -> None:
    def mutate(world: dict) -> None:
        world["questions"][0]["evidence"][1]["step"] = "open_sap"

    _rejects(_task(tmp_path, mutate), "step 'open_sap' is not in the capture manifest")


def test_screenshot_without_a_path_is_rejected(tmp_path) -> None:
    def mutate(world: dict) -> None:
        del world["questions"][0]["evidence"][1]["path"]

    _rejects(_task(tmp_path, mutate), "q1: screenshot has no path")


def test_screenshot_path_outside_the_task_is_rejected(tmp_path) -> None:
    """A quiz may only cite frames captured for its own demonstration."""

    def mutate(world: dict) -> None:
        world["questions"][0]["evidence"][1]["path"] = "tasks/other-task/frame.png"

    _rejects(_task(tmp_path, mutate), "is not a file inside sample-task")


def test_screenshot_citing_a_frame_other_than_the_recorded_one_is_rejected(
    tmp_path,
) -> None:
    def mutate(world: dict) -> None:
        world["questions"][0]["evidence"][1]["path"] = (
            f"tasks/{TASK_NAME}/{CAPTURE_DIR}/{FRAMES[1]}"
        )

    _rejects(_task(tmp_path, mutate), "is not the frame the manifest records")


def test_screenshot_digest_that_does_not_match_the_frame_is_rejected(tmp_path) -> None:
    def mutate(world: dict) -> None:
        world["questions"][0]["evidence"][1]["sha256"] = "0" * 64

    _rejects(_task(tmp_path, mutate), "q1: screenshot sha256 does not match")


def test_screenshot_line_disagreeing_with_the_manifest_is_rejected(tmp_path) -> None:
    def mutate(world: dict) -> None:
        world["questions"][0]["evidence"][1]["line"] = 2

    _rejects(_task(tmp_path, mutate), "line 2 disagrees with the manifest line 1")


def test_undescribed_screenshot_is_rejected(tmp_path) -> None:
    def mutate(world: dict) -> None:
        world["questions"][0]["evidence"][1]["description"] = "   "

    _rejects(_task(tmp_path, mutate), "q1: screenshot has no description")


def test_missing_capture_manifest_is_rejected(tmp_path) -> None:
    _rejects(
        _task(tmp_path, lambda world: world.update(manifest=None)),
        "manifest.json is missing",
    )


def test_capture_manifest_without_a_capture_method_is_rejected(tmp_path) -> None:
    def mutate(world: dict) -> None:
        del world["manifest"]["capture_method"]

    _rejects(_task(tmp_path, mutate), "capture manifest has no capture_method")


def test_capture_manifest_with_no_steps_is_rejected(tmp_path) -> None:
    def mutate(world: dict) -> None:
        world["manifest"]["steps"] = []
        world["frames"] = {}

    _rejects(_task(tmp_path, mutate), "capture manifest has no steps")


def test_capture_manifest_of_an_unknown_version_is_rejected(tmp_path) -> None:
    def mutate(world: dict) -> None:
        world["manifest"]["version"] = 2

    _rejects(_task(tmp_path, mutate), "unexpected capture manifest version")


def test_capture_manifest_naming_another_task_is_rejected(tmp_path) -> None:
    def mutate(world: dict) -> None:
        world["manifest"]["task"] = "another-task"

    _rejects(_task(tmp_path, mutate), "capture manifest names another task")


def test_repeated_capture_steps_are_rejected(tmp_path) -> None:
    def mutate(world: dict) -> None:
        world["manifest"]["steps"][1]["step"] = "open_inbox"

    _rejects(_task(tmp_path, mutate), "capture steps are not unique")


def test_capture_lines_with_a_hole_are_rejected(tmp_path) -> None:
    """Lines are the replay order, so a gap means a beat went missing."""

    def mutate(world: dict) -> None:
        world["manifest"]["steps"][1]["line"] = 3

    _rejects(_task(tmp_path, mutate), r"capture lines are not 1\.\.2 in order")


def test_capture_row_pointing_at_a_missing_frame_is_rejected(tmp_path) -> None:
    def mutate(world: dict) -> None:
        world["frames"].pop(FRAMES[1])

    _rejects(_task(tmp_path, mutate), "screenshot screenshot_line2.png is missing")


def test_capture_row_pointing_at_missing_metadata_is_rejected(tmp_path) -> None:
    def mutate(world: dict) -> None:
        world["manifest"]["steps"][0]["_drop_metadata"] = True

    _rejects(_task(tmp_path, mutate), "metadata metadata/line1.md is missing")


def test_edited_metadata_is_rejected_by_its_recorded_digest(tmp_path) -> None:
    """The frame is pixels; the metadata is the words the agent actually reads.

    Checking only that the file exists leaves the accessibility tree and the
    rendered text free to be written by hand, which is enough to make up the
    answer to a question that cites the frame beside it. The frame stays
    byte-identical throughout, so every screenshot rule still passes.
    """

    def mutate(world: dict) -> None:
        step = world["manifest"]["steps"][0]
        step["metadata_sha256"] = hashlib.sha256(
            _metadata_text(step["step"]).encode()
        ).hexdigest()
        step["_metadata_text"] = "# open_inbox\nStaticText: Total 999,999.00\n"

    _rejects(_task(tmp_path, mutate), "metadata sha256 does not match")


def test_capture_row_without_a_metadata_digest_is_rejected(tmp_path) -> None:
    """An unhashed metadata row is a row nothing holds to its bytes."""

    def mutate(world: dict) -> None:
        world["manifest"]["steps"][1]["_drop_metadata_digest"] = True

    _rejects(_task(tmp_path, mutate), "decide: metadata metadata/line2.md has no sha256")


def test_metadata_the_manifest_does_not_list_is_rejected(tmp_path) -> None:
    """An unaccounted metadata file is where a hand-written tree hides.

    The frames get this sweep already; without its counterpart, a fabricated
    tree can sit beside the captured ones waiting for a manifest row to point
    at it.
    """

    def mutate(world: dict) -> None:
        world["extra_metadata"]["line9.md"] = "# invented\n"

    _rejects(_task(tmp_path, mutate), r"does not list: \['line9.md'\]")


def test_capture_row_whose_metadata_escapes_the_metadata_directory_is_rejected(
    tmp_path,
) -> None:
    """Metadata is swept as a directory, so a row may not point outside it."""

    def mutate(world: dict) -> None:
        world["manifest"]["steps"][0]["metadata"] = "../../task.toml"

    _rejects(_task(tmp_path, mutate), "is not inside metadata/")


def test_edited_frame_is_rejected_by_its_recorded_digest(tmp_path) -> None:
    def mutate(world: dict) -> None:
        world["frames"][FRAMES[0]] = b"\x89PNG\r\n\x1a\nedited"
        world["manifest"]["steps"][0]["sha256"] = hashlib.sha256(
            _frame_bytes(FRAMES[0])
        ).hexdigest()

    _rejects(_task(tmp_path, mutate), "screenshot sha256 does not match")


def test_frame_the_manifest_does_not_list_is_rejected(tmp_path) -> None:
    """An unaccounted frame beside the captured ones is where a stale or
    hand-made image hides."""

    def mutate(world: dict) -> None:
        world["frames"]["screenshot_line9.png"] = None

    _rejects(_task(tmp_path, mutate), "does not list: \\['screenshot_line9.png'\\]")


def test_closure_still_rejects_under_optimized_bytecode(tmp_path) -> None:
    """``python -O`` deletes ``assert``; it must not delete this standard.

    The closure half goes through the same raise as the audit half, so
    compiling the shared validator with ``optimize=2`` must not turn a task
    with a broken citation into a passing one.
    """

    source = (ROOT / "tests/_quiz_audit.py").read_text()
    namespace: dict[str, Any] = {"__name__": "quiz_audit_optimized"}
    exec(compile(source, "tests/_quiz_audit.py", "exec", optimize=2), namespace)

    def mutate(world: dict) -> None:
        world["questions"][0]["evidence"][1]["sha256"] = "0" * 64

    with pytest.raises(AssertionError, match="q1: screenshot sha256 does not match"):
        namespace["assert_quiz_evidence_is_closed"](_task(tmp_path, mutate))
