"""The demonstration-dependent quiz standard, over every enrolled task."""
from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from showAndTell.quiz.judge import grade_closed
from tests._quiz_audit import (
    AUDIT_RUNS,
    ENROLLED,
    MAXIMUM_ALLOWED_RATE,
    assert_quiz_meets_demonstration_standard,
    question_digest,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("task", ENROLLED)
def test_quiz_is_demonstration_dependent_and_blind_guess_audited(task: str) -> None:
    assert_quiz_meets_demonstration_standard(ROOT / "tasks" / task)


def _collected_roots() -> tuple[Path, ...]:
    testpaths = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"][
        "pytest"
    ]["ini_options"]["testpaths"]
    return tuple(ROOT / entry for entry in testpaths)


def test_shared_enforcement_sits_where_the_default_gate_collects_it() -> None:
    """The promotion is cosmetic unless the default gate actually runs it.

    A validation module placed beside a task passes when it is named
    explicitly and never runs in the laptop gate, because ``testpaths``
    excludes it. Pin the location so the shared standard cannot drift back.
    """

    collected = _collected_roots()

    for module in (ROOT / "tests/_quiz_audit.py", Path(__file__)):
        assert any(module.is_relative_to(root) for root in collected), module

    for task in ENROLLED:
        task_dir = ROOT / "tasks" / task
        assert not any(task_dir.is_relative_to(root) for root in collected), task


def test_no_task_ships_a_test_module_the_default_gate_never_collects() -> None:
    """A test file beside a task asserts nothing and reads like a quality gate.

    ``tasks/pcn-triage/test_question_evidence.py`` was exactly that: it looked
    like the task's gate, had never executed, and had drifted into stating a
    weaker blind-guess rule than the shared standard that does run. Task
    validation belongs under ``tests``; this refuses the shape that hid it.
    """

    collected = _collected_roots()
    stranded = [
        path.relative_to(ROOT).as_posix()
        for path in sorted((ROOT / "tasks").rglob("test_*.py"))
        if "__pycache__" not in path.parts
        and not any(path.is_relative_to(root) for root in collected)
    ]
    assert stranded == []


def test_the_shared_ceiling_leaves_no_room_for_an_uninformed_guesser() -> None:
    """A closed question over three visible tags sits near 0.33 when guessed.

    The pin is the committed value; the inequality is the reason for it. A
    ceiling at or above one third would admit questions a guesser answers by
    picking blindly from the smallest answer space a quiz question can have.
    """

    assert MAXIMUM_ALLOWED_RATE == 0.20
    assert 0 < MAXIMUM_ALLOWED_RATE < 1 / 3


def _quiz(count: int = 10, generic: int = 1) -> dict:
    questions = [
        {
            "id": f"q{index}",
            "type": "closed",
            "scope": "generic_rule" if index <= generic else "workflow_specific",
            "question": f"Question {index}?",
            "answer_aliases": [f"answer {index}"],
        }
        for index in range(1, count + 1)
    ]
    return {"questions": questions}


def _audit(quiz: dict) -> dict:
    return {
        "version": 1,
        "method": (
            "Twenty independent subagents saw only the option-free question text "
            "and were forced to provide a specific best guess."
        ),
        "runs": AUDIT_RUNS,
        "maximum_allowed_rate": MAXIMUM_ALLOWED_RATE,
        "scores": [
            {
                "id": question["id"],
                "correct": 0,
                "rate": 0.00,
                "question_digest": question_digest(question),
            }
            for question in quiz["questions"]
        ],
    }


def _task(
    tmp_path: Path, quiz: dict | None = None, audit: Any = _audit
) -> Path:
    quiz = _quiz() if quiz is None else quiz
    task_dir = tmp_path / "sample-task"
    (task_dir / "quiz").mkdir(parents=True)
    (task_dir / "quiz/questions.json").write_text(json.dumps(quiz))
    payload = audit(quiz) if callable(audit) else audit
    if payload is not None:
        (task_dir / "quiz/blind-guess-audit.json").write_text(json.dumps(payload))
    return task_dir


def _rejects(task_dir: Path, message: str) -> None:
    with pytest.raises(AssertionError, match=message):
        assert_quiz_meets_demonstration_standard(task_dir)


def test_a_well_formed_audited_quiz_passes_the_standard(tmp_path) -> None:
    assert_quiz_meets_demonstration_standard(_task(tmp_path))


def test_missing_quiz_file_is_rejected(tmp_path) -> None:
    task_dir = _task(tmp_path)
    (task_dir / "quiz/questions.json").unlink()
    _rejects(task_dir, "questions.json is missing")


def test_quiz_without_questions_is_rejected(tmp_path) -> None:
    _rejects(_task(tmp_path, quiz={"questions": []}), "quiz has no questions")


def test_repeated_question_ids_are_rejected(tmp_path) -> None:
    quiz = _quiz()
    quiz["questions"][1]["id"] = quiz["questions"][0]["id"]
    _rejects(_task(tmp_path, quiz=quiz), "ids are not unique")


def test_judged_question_type_is_rejected(tmp_path) -> None:
    quiz = _quiz()
    quiz["questions"][3]["type"] = "rubric"
    _rejects(_task(tmp_path, quiz=quiz), "q4: type must be closed")


def test_question_without_answer_aliases_is_rejected(tmp_path) -> None:
    quiz = _quiz()
    quiz["questions"][2]["answer_aliases"] = []
    _rejects(_task(tmp_path, quiz=quiz), "q3: no answer_aliases")


def test_question_offering_a_printed_answer_space_is_rejected(tmp_path) -> None:
    quiz = _quiz()
    quiz["questions"][0]["options"] = [{"id": "a", "text": "first"}]
    _rejects(_task(tmp_path, quiz=quiz), "q1: carries options")


def test_question_naming_a_correct_option_is_rejected(tmp_path) -> None:
    quiz = _quiz()
    quiz["questions"][0]["correct_option"] = "a"
    _rejects(_task(tmp_path, quiz=quiz), "q1: carries correct_option")


@pytest.mark.parametrize(
    "aliases",
    [
        ["yes", "no"],
        ["no"],
        ["True", "False"],
        ["  OK  "],
        ["No."],
        ["answer 7", "fail"],
    ],
)
def test_question_whose_answer_space_is_a_coin_flip_is_rejected(
    tmp_path, aliases
) -> None:
    """Grading matches any alias, so one trivial alias is a free guess."""

    quiz = _quiz()
    quiz["questions"][6]["answer_aliases"] = aliases
    _rejects(_task(tmp_path, quiz=quiz), "q7: alias .* is a trivially guessable")


def test_an_answer_that_merely_begins_with_a_trivial_word_is_admitted(
    tmp_path,
) -> None:
    """The shipped Paperless answer starts with "no" and must keep passing.

    The guard is an exact match on a whole alias; a substring test would refuse
    every answer whose first word happens to be a yes/no.
    """

    quiz = _quiz()
    quiz["questions"][6]["answer_aliases"] = [
        "no, Ready, Kilo invoice sharing another number",
        "no Ready Kilo invoice sharing another number",
        "no. Ready. Kilo invoice sharing another number",
        "not duplicates, Ready, Kilo invoice sharing another number",
        "passport office",
        "failure to route",
    ]

    assert_quiz_meets_demonstration_standard(_task(tmp_path, quiz=quiz))


def test_question_without_a_declared_scope_is_rejected(tmp_path) -> None:
    quiz = _quiz()
    del quiz["questions"][4]["scope"]
    _rejects(_task(tmp_path, quiz=quiz), "q5: scope must be one of")


def test_unrecognised_scope_value_is_rejected(tmp_path) -> None:
    quiz = _quiz()
    quiz["questions"][4]["scope"] = "domain_knowledge"
    _rejects(_task(tmp_path, quiz=quiz), "q5: scope must be one of")


def test_quiz_without_enough_generic_rule_questions_is_rejected(tmp_path) -> None:
    _rejects(
        _task(tmp_path, quiz=_quiz(generic=0)),
        "generic_rule share 0.000 is outside",
    )


def test_quiz_with_too_many_generic_rule_questions_is_rejected(tmp_path) -> None:
    _rejects(
        _task(tmp_path, quiz=_quiz(generic=3)),
        "generic_rule share 0.300 is outside",
    )


def test_quiz_without_a_blind_guess_audit_is_rejected(tmp_path) -> None:
    _rejects(_task(tmp_path, audit=None), "blind-guess-audit.json is missing")


def test_audit_of_an_unknown_version_is_rejected(tmp_path) -> None:
    def audit(quiz: dict) -> dict:
        return {**_audit(quiz), "version": 2}

    _rejects(_task(tmp_path, audit=audit), "unexpected audit version")


def test_audit_with_too_few_runs_is_rejected(tmp_path) -> None:
    def audit(quiz: dict) -> dict:
        return {**_audit(quiz), "runs": 5}

    _rejects(_task(tmp_path, audit=audit), "audit must report 20 runs")


def test_audit_without_a_numeric_ceiling_is_rejected(tmp_path) -> None:
    def audit(quiz: dict) -> dict:
        return {**_audit(quiz), "maximum_allowed_rate": "0.20"}

    _rejects(_task(tmp_path, audit=audit), "maximum_allowed_rate must be a number")


def test_audit_with_a_ceiling_outside_the_unit_interval_is_rejected(tmp_path) -> None:
    def audit(quiz: dict) -> dict:
        return {**_audit(quiz), "maximum_allowed_rate": 0.0}

    _rejects(_task(tmp_path, audit=audit), "is not a rate")


def test_audit_method_dropping_a_required_token_is_rejected(tmp_path) -> None:
    def audit(quiz: dict) -> dict:
        return {
            **_audit(quiz),
            "method": (
                "Twenty independent subagents saw the questions and made a "
                "best guess."
            ),
        }

    _rejects(_task(tmp_path, audit=audit), "audit method omits 'option-free'")


def test_audit_without_a_method_statement_is_rejected(tmp_path) -> None:
    def audit(quiz: dict) -> dict:
        return {**_audit(quiz), "method": "   "}

    _rejects(_task(tmp_path, audit=audit), "audit has no method")


def test_audit_missing_a_question_row_is_rejected(tmp_path) -> None:
    def audit(quiz: dict) -> dict:
        payload = _audit(quiz)
        payload["scores"].pop()
        return payload

    _rejects(_task(tmp_path, audit=audit), "do not cover exactly the quiz questions")


def test_audit_keeping_a_stale_question_row_is_rejected(tmp_path) -> None:
    def audit(quiz: dict) -> dict:
        payload = _audit(quiz)
        payload["scores"].append({"id": "q_retired", "correct": 0, "rate": 0.00})
        return payload

    _rejects(_task(tmp_path, audit=audit), "do not cover exactly the quiz questions")


def test_audit_count_disagreeing_with_its_rate_is_rejected(tmp_path) -> None:
    def audit(quiz: dict) -> dict:
        payload = _audit(quiz)
        payload["scores"][0].update(correct=1, rate=0.00)
        return payload

    _rejects(_task(tmp_path, audit=audit), "q1: correct 1 disagrees with rate 0.0")


def test_audit_count_outside_the_run_range_is_rejected(tmp_path) -> None:
    def audit(quiz: dict) -> dict:
        payload = _audit(quiz)
        payload["scores"][0].update(correct=21, rate=1.05)
        return payload

    _rejects(_task(tmp_path, audit=audit), r"q1: correct 21 is outside 0-20")


def test_a_question_audited_exactly_at_the_ceiling_is_admitted(tmp_path) -> None:
    """The ceiling is inclusive, and the next measurable rate above it is not.

    Over twenty runs the step above 0.20 is 0.25, which the sibling refutation
    rejects; a question measured at exactly the ceiling stays enrolled.
    """

    def audit(quiz: dict) -> dict:
        payload = _audit(quiz)
        payload["scores"][0].update(
            correct=round(MAXIMUM_ALLOWED_RATE * AUDIT_RUNS),
            rate=MAXIMUM_ALLOWED_RATE,
        )
        return payload

    assert_quiz_meets_demonstration_standard(_task(tmp_path, audit=audit))


def test_the_standard_still_rejects_under_optimized_bytecode(tmp_path) -> None:
    """``python -O`` deletes ``assert``; it must not delete this standard.

    Compiling the shared validator with ``optimize=2`` reproduces that run mode.
    A quiz the validator rejects normally must still be rejected once bare
    asserts are gone, or the whole gate passes everything in optimized runs.
    """

    source = (ROOT / "tests/_quiz_audit.py").read_text()
    namespace: dict[str, Any] = {"__name__": "quiz_audit_optimized"}
    exec(compile(source, "tests/_quiz_audit.py", "exec", optimize=2), namespace)

    quiz = _quiz()
    quiz["questions"][3]["type"] = "rubric"
    with pytest.raises(AssertionError, match="q4: type must be closed"):
        namespace["assert_quiz_meets_demonstration_standard"](
            _task(tmp_path, quiz=quiz)
        )


def test_guessable_question_above_the_shared_ceiling_is_rejected(tmp_path) -> None:
    def audit(quiz: dict) -> dict:
        payload = _audit(quiz)
        payload["scores"][0].update(correct=5, rate=0.25)
        return payload

    _rejects(_task(tmp_path, audit=audit), r"q1: blind-guess rate 0.25 exceeds")


def test_a_task_cannot_widen_the_shared_ceiling_from_its_own_audit(tmp_path) -> None:
    """Declaring a looser ceiling must not admit a question the pin rejects."""

    def audit(quiz: dict) -> dict:
        payload = _audit(quiz)
        payload["maximum_allowed_rate"] = 0.50
        payload["scores"][0].update(correct=6, rate=0.30)
        return payload

    _rejects(_task(tmp_path, audit=audit), r"q1: blind-guess rate 0.3 exceeds the 0.2")


def test_a_task_declaring_a_stricter_ceiling_is_held_to_it(tmp_path) -> None:
    """The stricter of the declared ceiling and the shared pin applies."""

    def audit(quiz: dict) -> dict:
        payload = _audit(quiz)
        payload["maximum_allowed_rate"] = 0.05
        payload["scores"][0].update(correct=2, rate=0.10)
        return payload

    _rejects(_task(tmp_path, audit=audit), r"q1: blind-guess rate 0.1 exceeds the 0.05")


# --- the audit binds to the question it measured -----------------------------
#
# Matching score ids against question ids says only that a row exists per
# question. It says nothing about *which* question the row measured, so the
# whole empirical half of the standard can be satisfied by an audit taken
# against text that no longer exists. Each refutation below rewrites the quiz
# after the measurement was taken and requires the stale audit to be refused.


def test_audit_row_measuring_different_question_text_is_rejected(tmp_path) -> None:
    """Rewriting a question after the audit is the forge this standard invites.

    The blind agents saw the old wording; the committed rate describes the old
    wording; the quiz now ships wording a guesser answers from ordinary sense.
    Nothing about the id set changes, so id matching alone lets it through.
    """

    quiz = _quiz()
    measured = _audit(quiz)
    quiz["questions"][0]["question"] = "Is the batch larger than a single document?"

    _rejects(_task(tmp_path, quiz=quiz, audit=measured), "q1: the audit row")


def test_audit_row_measuring_different_answer_aliases_is_rejected(tmp_path) -> None:
    """Aliases are what a guess is graded against, so widening them after the
    measurement inflates the answer space the committed rate was measured over."""

    quiz = _quiz()
    measured = _audit(quiz)
    quiz["questions"][2]["answer_aliases"] = ["answer 3", "3", "three", "iii"]

    _rejects(_task(tmp_path, quiz=quiz, audit=measured), "q3: the audit row")


def test_audit_row_measuring_a_different_answer_format_is_rejected(tmp_path) -> None:
    """The answer-format statement is part of the question, and part of the odds.

    Telling a blind agent the answer is two comma-separated numbers collapses
    the space it has to guess over. These quizzes carry that instruction inside
    the question text rather than in a field of its own, so hashing the question
    verbatim is what keeps the format inside the measurement.
    """

    quiz = _quiz()
    quiz["questions"][4]["question"] = (
        "How many documents does the batch hold, and how many change?"
    )
    measured = _audit(quiz)
    quiz["questions"][4]["question"] += " Answer as two numbers separated by a comma."

    _rejects(_task(tmp_path, quiz=quiz, audit=measured), "q5: the audit row")


def test_audit_row_without_a_question_digest_is_rejected(tmp_path) -> None:
    """Omission is the cheapest forgery, so a row with no binding is no row."""

    def audit(quiz: dict) -> dict:
        payload = _audit(quiz)
        for row in payload["scores"]:
            row.pop("question_digest", None)
        return payload

    _rejects(_task(tmp_path, audit=audit), "q1: audit row records no question_digest")


def test_a_stale_digest_tells_the_author_to_re_run_rather_than_to_rehash(
    tmp_path,
) -> None:
    """A binding whose failure reads as "update the hash" binds nothing.

    The only honest repair for a mismatch is another measurement, so the message
    has to say so; an author who recomputes the digest by hand has produced a
    quiz with an audit of a question nobody ever put in front of a guesser.
    """

    quiz = _quiz()
    measured = _audit(quiz)
    quiz["questions"][0]["question"] = "Is the batch larger than a single document?"

    with pytest.raises(AssertionError) as raised:
        assert_quiz_meets_demonstration_standard(
            _task(tmp_path, quiz=quiz, audit=measured)
        )

    message = str(raised.value)
    assert "Re-run the blind-guess audit" in message
    assert "rather than editing" in message


def test_two_questions_with_identical_wording_cannot_swap_measured_rates(
    tmp_path,
) -> None:
    """Nothing makes question text unique, so the digest carries the id too.

    Without it, two questions worded alike hash alike, and a guessable one can
    quietly adopt the clean rate measured for its twin.
    """

    quiz = _quiz()
    twin, other = quiz["questions"][8], quiz["questions"][9]
    twin["question"] = other["question"]
    twin["answer_aliases"] = list(other["answer_aliases"])
    measured = _audit(quiz)
    rows = {row["id"]: row for row in measured["scores"]}
    rows["q9"]["id"], rows["q10"]["id"] = "q10", "q9"

    _rejects(_task(tmp_path, quiz=quiz, audit=measured), "the audit row")


def test_reclassifying_a_question_scope_does_not_invalidate_its_measurement(
    tmp_path,
) -> None:
    """Scope is deliberately outside the digest, and this pins that choice.

    A blind agent never sees ``scope`` and grading never consults it, so moving
    a question between ``workflow_specific`` and ``generic_rule`` cannot change
    how guessable it is. The live quiz is still held to the transfer-share
    window, so nothing is laundered by leaving it out; putting it in would only
    demand a fresh measurement for a re-labelling that measured nothing new.
    """

    quiz = _quiz()
    measured = _audit(quiz)
    quiz["questions"][1]["scope"] = "generic_rule"

    assert_quiz_meets_demonstration_standard(
        _task(tmp_path, quiz=quiz, audit=measured)
    )


def test_question_without_prompt_text_is_rejected(tmp_path) -> None:
    """The digest binds the text a guesser saw, so the text has to be there."""

    quiz = _quiz()
    quiz["questions"][3]["question"] = "   "
    _rejects(_task(tmp_path, quiz=quiz), "q4: no question text")


# --- an alias the grader can never match -------------------------------------


@pytest.mark.parametrize("alias", ["", "   ", "---", "\t\n"])
def test_alias_the_grader_can_never_match_is_rejected(tmp_path, alias) -> None:
    """An alias that normalises away is dead weight at best and a hole at worst.

    ``grade_closed`` normalises an alias before matching, so one that normalises
    to nothing can never grade an answer correct - the same defect the capture
    ceiling refuses at the other end of the length scale. It is refused here as
    a property of the quiz file, so it cannot come to depend on the grader's
    guard against an empty target staying where it is.
    """

    quiz = _quiz()
    quiz["questions"][6]["answer_aliases"] = ["answer 7", alias]
    _rejects(_task(tmp_path, quiz=quiz), "q7: alias .* can never match")


def test_an_alias_that_normalises_away_matches_nothing_not_everything() -> None:
    """The counterexample to "an empty alias grades every answer correct".

    ``grade_closed`` refuses an empty normalized answer, so an empty alias
    matches nothing at all. Exercising the real grader keeps that the reason
    the quiz standard refuses such an alias: it is a dead alias.
    """

    for alias in ("", "   ", "---"):
        assert not grade_closed("anything at all", [alias]), alias
        assert not grade_closed("", [alias]), alias
    assert grade_closed("Ready", ["", "Ready"])
