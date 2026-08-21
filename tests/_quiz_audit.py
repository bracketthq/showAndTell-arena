"""Shared demonstration-dependent quiz standard for enrolled benchmark tasks.

A quiz question earns its place only if seeing the demonstration is the cheapest
way to answer it. The arbiter is an agentic blind-guess audit whose measured
per-question rates are committed alongside the quiz; this module is the
mechanical half of that standard, asserting the shape a quiz and its audit must
hold before either can be trusted.

The standard has two halves, one per exported assertion:

* :func:`assert_quiz_meets_demonstration_standard` - the quiz and its audit are
  shaped so that a score means what it claims to mean.
* :func:`assert_quiz_evidence_is_closed` - every claim a question makes about
  the demonstration resolves to a committed, hash-addressed artifact, and no
  captured artifact dangles unreferenced.

Enforcement lives under ``tests`` on purpose. ``pyproject.toml`` pins
``testpaths = ["tests"]``, so a validation module placed beside a task is never
collected by the default gate and promotes nothing.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any

# The runtime grader itself, used below as the oracle for "can this alias ever
# match itself?". Restating its normalisation here would let the two drift.
from showAndTell.quiz.judge import grade_closed

# The blind-guess ceiling is pinned here rather than read from the per-task
# audit file, so no task can widen its own gate by editing its own data. A
# closed question over three visible tags sits near 0.33 for an uninformed
# guesser, so a ceiling at or above that admits pure guesswork. A task may
# declare a stricter ceiling of its own; the stricter of the two applies.
MAXIMUM_ALLOWED_RATE = 0.20

AUDIT_VERSION = 1
AUDIT_RUNS = 20

# The audit is the only empirical part of this standard, and it is committed
# data rather than something the gate can re-run. Matching score ids against
# question ids therefore proves only that a row exists per question - never that
# the row measured *that* question. Each score binds to a digest of exactly what
# the blind agents were put in front of, so rewriting a question into something
# guessable after the fact refuses rather than inherits the clean rate.
#
# The digest covers, and covers only, what changes the odds of an uninformed
# guess:
#
# * ``question`` - the text the guessers read, verbatim. These quizzes state the
#   answer format inside that text ("Answer as two numbers separated by a
#   comma"), and the format is a large part of the odds, so hashing the string
#   as written keeps the format inside the measurement without inventing a field
#   for it.
# * ``answer_aliases`` - what a guess is graded against. Widening them after the
#   measurement grades answers the audit never admitted.
# * ``id`` - nothing makes question text unique, so without the id two questions
#   worded alike hash alike and a guessable one can adopt its twin's rate.
#
# Deliberately outside it: ``scope``, which a blind agent never sees and grading
# never consults, and which the live quiz is separately held to (A7/A8) - a
# re-labelling measures nothing new, so demanding a fresh audit for one would be
# ceremony, not assurance. ``type`` likewise, being pinned to ``closed`` for
# every admissible question and so unable to vary. ``evidence`` likewise never
# reaches a guesser and is held by the closure half.
AUDIT_DIGEST_FIELDS = ("id", "question", "answer_aliases")

# The printed-options ban constrains the question; nothing constrained the
# answers themselves. A question graded correct on a bare "yes" hands a blind
# guesser roughly even odds no matter what ceiling the audit pins.
#
# The empirical audit would catch this too — a true 50% rate measures far above
# 0.20 over 20 runs — but the structural guard still earns its place: it holds
# even if a future author never reruns the audit honestly. The audit is
# committed data an author produces; this is a property of the quiz file that
# no committed measurement can talk its way past.
TRIVIAL_ANSWERS = frozenset(
    {"yes", "no", "true", "false", "ok", "pass", "fail"}
)
# Surrounding whitespace and punctuation only: the comparison below is against
# a *whole* alias, never a substring of one. The shipped Paperless answer
# "no, Ready, Kilo invoice sharing another number" begins with a trivial word
# and is not itself one, so it must keep passing.
_ALIAS_EDGES = re.compile(r"^[^a-z0-9]+|[^a-z0-9]+$")

QUESTION_SCOPES = frozenset({"workflow_specific", "generic_rule"})

# ``generic_rule`` means applying a taught rule to a case the demonstration did
# not walk. Below the floor a quiz never checks transfer; above the ceiling the
# score stops measuring what this demonstration actually did.
MINIMUM_GENERIC_RULE_SHARE = 0.10
MAXIMUM_GENERIC_RULE_SHARE = 0.20

# The method statement cannot be silently weakened into a softer audit.
REQUIRED_METHOD_TOKENS = ("independent", "option-free", "best guess")

# A task enrols in the whole standard by adding its directory name here; both
# exported assertions are driven from this one table so a task can never hold
# one half of the standard and skip the other.
ENROLLED = ()  # add task names here to enroll their quizzes in the audit

# Evidence answers "where in the demonstration is this visible?". A narration
# beat quotes the spoken line and a screenshot pins a captured frame by hash.
EVIDENCE_TYPES = frozenset({"narration", "screenshot"})

# A capture is addressed by content and by beat, never by wall clock: a
# timestamp on an evidence entry is a replay that cannot be reproduced.
FORBIDDEN_EVIDENCE_KEYS = ("timestamp",)

CAPTURE_DIR = "evidence/demo-screenshots"
# Metadata is swept as a directory the way frames are swept by extension, so
# every row's metadata lives here and an unlisted file beside them is visible.
METADATA_DIR = "metadata"
MANIFEST_VERSION = 1


def question_digest(question: dict) -> str:
    """Return the digest binding an audited score to the question it measured.

    Canonical JSON over :data:`AUDIT_DIGEST_FIELDS`: sorted keys and explicit
    UTF-8 so the same question hashes the same way whatever wrote the file.
    Aliases are hashed in their committed order rather than as a set - a
    reordering is a cheap edit to avoid, and normalising it away would make the
    digest a summary of the quiz rather than a copy of it.
    """

    payload = {field: question[field] for field in AUDIT_DIGEST_FIELDS}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require(condition: object, message: str) -> None:
    """Fail unless ``condition`` holds.

    A bare ``assert`` compiles away under ``python -O``, which would turn this
    whole validator into a gate that passes everything. Every check below goes
    through this raise so the standard holds whatever flags the interpreter ran
    with.
    """

    if not condition:
        raise AssertionError(message)


def _load_json(path: Path, label: str) -> Any:
    _require(path.is_file(), f"{label}: {path.name} is missing")
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as error:  # pragma: no cover - message only
        raise AssertionError(f"{label}: {path.name} does not parse: {error}") from error


def assert_quiz_meets_demonstration_standard(task_dir: Path) -> None:
    """Assert one task's quiz and blind-guess audit meet the shared standard."""

    task = task_dir.name
    questions = _assert_quiz_shape(task, task_dir)
    _assert_audit_matches_quiz(task, task_dir, questions)


def assert_quiz_evidence_is_closed(task_dir: Path) -> None:
    """Assert one task's quiz evidence resolves to committed capture artifacts.

    Closed means both directions. Every reference a question makes - a narration
    beat or a captured frame - resolves to something committed and unchanged;
    every frame the capture manifest lists is a real file whose bytes still hash
    to the recorded digest with nothing unlisted beside it. A quiz whose evidence
    is not closed cites a demonstration that may no longer exist.
    """

    task = task_dir.name
    questions = _assert_question_list(task, task_dir)
    narration = _load_narration(task, task_dir)
    manifest = _assert_capture_manifest_resolves(task, task_dir)

    for question in questions:
        where = f"{task} {question['id']}"
        # E1 - a question with no evidence claims the demonstration taught it
        # without ever saying where.
        evidence = question.get("evidence")
        _require(isinstance(evidence, list) and evidence, f"{where}: no evidence")

        for entry in evidence:
            kind = entry.get("type")
            # E2 - an unrecognised kind is evidence nothing knows how to check.
            _require(
                kind in EVIDENCE_TYPES,
                f"{where}: evidence type must be one of {sorted(EVIDENCE_TYPES)}, "
                f"got {kind!r}",
            )
            # E3 - evidence is addressed by content and beat, never by clock.
            for forbidden in FORBIDDEN_EVIDENCE_KEYS:
                _require(
                    forbidden not in entry,
                    f"{where}: evidence carries a {forbidden!r} key",
                )
            if kind == "narration":
                _assert_narration_entry(where, entry, narration)
            else:
                _assert_screenshot_entry(where, entry, task_dir, manifest)


def _assert_question_list(task: str, task_dir: Path) -> list[dict]:
    payload = _load_json(task_dir / "quiz/questions.json", task)
    questions = payload.get("questions")
    _require(
        isinstance(questions, list) and questions, f"{task}: quiz has no questions"
    )
    return questions


def _load_narration(task: str, task_dir: Path) -> dict[str, str]:
    path = task_dir / "demo/narration_script.jsonl"
    _require(path.is_file(), f"{task}: {path.name} is missing")
    rows = [
        json.loads(line) for line in path.read_text().splitlines() if line.strip()
    ]
    keys = [row["key"] for row in rows]
    # E5 - a duplicate beat key makes "which line was this?" unanswerable.
    _require(len(set(keys)) == len(keys), f"{task}: narration keys are not unique")
    return {row["key"]: row["text"] for row in rows}


def _assert_narration_entry(
    where: str, entry: dict, narration: dict[str, str]
) -> None:
    key = entry.get("key")
    # E6 - a quoted beat that no longer exists is a quote from nowhere.
    _require(key in narration, f"{where}: narration key {key!r} is not in the script")
    # E7 - quoting the script inexactly lets the taught wording drift away from
    # the wording the question was written against.
    _require(
        narration[key] == entry.get("text"),
        f"{where}: narration {key!r} text does not match the script",
    )


def _assert_screenshot_entry(
    where: str, entry: dict, task_dir: Path, manifest: dict[str, dict]
) -> None:
    root = task_dir.resolve().parents[1]
    step = entry.get("step")
    # E8 - a frame is cited by the beat it was captured at, so the beat must be
    # one the capture manifest actually recorded.
    _require(step in manifest, f"{where}: step {step!r} is not in the capture manifest")
    recorded = manifest[step]

    path = entry.get("path")
    _require(isinstance(path, str) and path, f"{where}: screenshot has no path")
    image = root / path
    # E9 - a quiz may only cite frames captured for its own task.
    _require(
        image.is_file() and image.resolve().is_relative_to(task_dir.resolve()),
        f"{where}: screenshot {path!r} is not a file inside {task_dir.name}",
    )
    # E10 - the cited path and the manifest row must be the same frame, or the
    # quiz and the capture record disagree about what was shown.
    _require(
        image.resolve() == recorded["image"],
        f"{where}: screenshot {path!r} is not the frame the manifest records "
        f"for step {step!r}",
    )
    # E11 - the digest travels with the citation so an edited frame breaks the
    # question that leans on it. Comparing against the bytes rather than the
    # manifest row is deliberate: the manifest is verified against the same
    # bytes, so one comparison pins the citation, the row and the file together.
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    _require(
        entry.get("sha256") == digest,
        f"{where}: screenshot sha256 does not match {path!r}",
    )
    if "line" in entry:
        # E12 - an optional line number must agree with the captured beat.
        _require(
            entry["line"] == recorded["line"],
            f"{where}: line {entry['line']!r} disagrees with the manifest line "
            f"{recorded['line']!r} for step {step!r}",
        )
    # E13 - a frame nobody described is a frame nobody can grade against.
    description = entry.get("description")
    _require(
        isinstance(description, str) and description.strip(),
        f"{where}: screenshot has no description",
    )


def _assert_capture_manifest_resolves(task: str, task_dir: Path) -> dict[str, dict]:
    """Return the manifest keyed by step, once every row resolves and hashes."""

    capture_dir = task_dir / CAPTURE_DIR
    manifest = _load_json(capture_dir / "manifest.json", task)

    _require(
        manifest.get("version") == MANIFEST_VERSION,
        f"{task}: unexpected capture manifest version",
    )
    # E14 - a manifest naming another task is a capture record pasted in.
    _require(
        manifest.get("task") == task, f"{task}: capture manifest names another task"
    )
    method = manifest.get("capture_method")
    _require(
        isinstance(method, str) and method.strip(),
        f"{task}: capture manifest has no capture_method",
    )

    steps = manifest.get("steps")
    _require(isinstance(steps, list) and steps, f"{task}: capture manifest has no steps")
    names = [step["step"] for step in steps]
    # E15 - two rows for one beat make the citation ambiguous.
    _require(len(set(names)) == len(names), f"{task}: capture steps are not unique")
    # E16 - lines are the replay order, so they run 1..n with no hole.
    _require(
        [step["line"] for step in steps] == list(range(1, len(steps) + 1)),
        f"{task}: capture lines are not 1..{len(steps)} in order",
    )

    recorded: dict[str, dict] = {}
    images: set[Path] = set()
    metadata_files: set[Path] = set()
    metadata_root = (capture_dir / METADATA_DIR).resolve()
    for step in steps:
        where = f"{task} {step['step']}"
        image = capture_dir / step["screenshot"]
        metadata = capture_dir / step["metadata"]
        # E17 - a manifest row pointing at a missing file records nothing.
        _require(image.is_file(), f"{where}: screenshot {step['screenshot']} is missing")
        _require(metadata.is_file(), f"{where}: metadata {step['metadata']} is missing")
        # E17a - metadata is swept as a directory below, so a row may not point
        # outside the one being swept.
        _require(
            metadata.resolve().parent == metadata_root,
            f"{where}: metadata {step['metadata']} is not inside "
            f"{METADATA_DIR}/",
        )
        # E18 - the recorded digest is what makes the capture tamper-evident.
        _require(
            hashlib.sha256(image.read_bytes()).hexdigest() == step["sha256"],
            f"{where}: screenshot sha256 does not match {step['screenshot']}",
        )
        # E18a - and the metadata is held to its bytes exactly as the frame is.
        # The frame is pixels; this file carries the accessibility tree and the
        # rendered text a graded agent actually reads, so a hand-written tree is
        # enough to invent the answer to a question citing the frame beside it.
        metadata_digest = step.get("metadata_sha256")
        _require(
            isinstance(metadata_digest, str) and metadata_digest,
            f"{where}: metadata {step['metadata']} has no sha256",
        )
        _require(
            hashlib.sha256(metadata.read_bytes()).hexdigest() == metadata_digest,
            f"{where}: metadata sha256 does not match {step['metadata']}",
        )
        images.add(image.resolve())
        metadata_files.add(metadata.resolve())
        recorded[step["step"]] = {
            "image": image.resolve(),
            "sha256": step["sha256"],
            "line": step["line"],
        }

    # E19 - an unlisted frame beside the listed ones is a capture nothing
    # accounts for, and the obvious place for a stale or hand-edited image.
    unlisted = {path.resolve() for path in capture_dir.glob("*.png")} - images
    _require(
        not unlisted,
        f"{task}: capture directory holds frames the manifest does not list: "
        f"{sorted(path.name for path in unlisted)}",
    )
    # E19a - the same sweep for metadata, for the same reason.
    stray = {
        path.resolve() for path in metadata_root.rglob("*") if path.is_file()
    } - metadata_files
    _require(
        not stray,
        f"{task}: metadata directory holds files the manifest does not list: "
        f"{sorted(path.name for path in stray)}",
    )
    return recorded


def _assert_quiz_shape(task: str, task_dir: Path) -> list[dict]:
    # A1 - the question list itself.
    questions = _assert_question_list(task, task_dir)
    ids = [question["id"] for question in questions]
    _require(len(set(ids)) == len(ids), f"{task}: quiz question ids are not unique")

    for question in questions:
        where = f"{task} {question['id']}"
        # A2 - every question is exact-graded, never judged prose.
        _require(question["type"] == "closed", f"{where}: type must be closed")
        # A2a - the audit digest binds the text a guesser was shown, so a
        # question with no text is a measurement of nothing.
        prompt = question.get("question")
        _require(
            isinstance(prompt, str) and prompt.strip(), f"{where}: no question text"
        )
        # A3 - closed grading needs at least one alias to match.
        aliases = question.get("answer_aliases")
        _require(isinstance(aliases, list) and aliases, f"{where}: no answer_aliases")
        # A4 - options let a guesser pick from a printed answer space.
        _require("options" not in question, f"{where}: carries options")
        _require("correct_option" not in question, f"{where}: carries correct_option")
        for alias in aliases:
            # A5 - closed grading accepts any alias, so a single binary or
            # status answer is enough to make the whole question a coin flip.
            _require(
                _ALIAS_EDGES.sub("", alias.casefold()) not in TRIVIAL_ANSWERS,
                f"{where}: alias {alias!r} is a trivially guessable answer; "
                f"a closed question may not accept {sorted(TRIVIAL_ANSWERS)} "
                "on its own",
            )
            # A5a - the grader normalises an alias before comparing it, so
            # an alias that normalises away - empty, whitespace, punctuation -
            # can never match any answer. Ask the real grader rather than
            # restating its normalization: an alias no answer can match is
            # exactly one the grader cannot match against itself.
            _require(
                grade_closed(alias, [alias]),
                f"{where}: alias {alias!r} normalises to nothing, so the grader "
                "can never match it against any answer",
            )
        # A7 - scope declares what the question actually measures.
        scope = question.get("scope")
        _require(
            scope in QUESTION_SCOPES,
            f"{where}: scope must be one of {sorted(QUESTION_SCOPES)}, got {scope!r}",
        )

    # A8 - generic_rule questions stay a deliberate minority.
    generic = sum(question["scope"] == "generic_rule" for question in questions)
    share = generic / len(questions)
    _require(
        MINIMUM_GENERIC_RULE_SHARE <= share <= MAXIMUM_GENERIC_RULE_SHARE,
        f"{task}: generic_rule share {share:.3f} is outside "
        f"{MINIMUM_GENERIC_RULE_SHARE}-{MAXIMUM_GENERIC_RULE_SHARE}",
    )
    return questions


def _assert_audit_matches_quiz(
    task: str, task_dir: Path, questions: list[dict]
) -> None:
    # A9 - a question is never admitted without an audited rate.
    audit = _load_json(task_dir / "quiz/blind-guess-audit.json", task)

    # A10/A11 - the audited protocol is the one this standard describes.
    _require(audit["version"] == AUDIT_VERSION, f"{task}: unexpected audit version")
    _require(
        audit["runs"] == AUDIT_RUNS,
        f"{task}: audit must report {AUDIT_RUNS} runs, got {audit['runs']!r}",
    )

    # A12 - the declared ceiling is informational; the shared pin below governs.
    declared = audit["maximum_allowed_rate"]
    _require(
        isinstance(declared, (int, float)) and not isinstance(declared, bool),
        f"{task}: maximum_allowed_rate must be a number",
    )
    _require(
        0 < declared <= 1, f"{task}: maximum_allowed_rate {declared!r} is not a rate"
    )
    ceiling = min(float(declared), MAXIMUM_ALLOWED_RATE)

    # A13 - the method statement records how the rates were obtained.
    method = audit.get("method")
    _require(isinstance(method, str) and method.strip(), f"{task}: audit has no method")
    folded = method.casefold()
    for token in REQUIRED_METHOD_TOKENS:
        _require(token in folded, f"{task}: audit method omits {token!r}")

    # A14 - no unaudited question, no stale row for a question that is gone.
    scores = audit["scores"]
    _require(
        {row["id"] for row in scores} == {question["id"] for question in questions},
        f"{task}: audit scores do not cover exactly the quiz questions",
    )

    by_id = {question["id"]: question for question in questions}
    for row in scores:
        where = f"{task} {row['id']}"
        # A14a - the row binds to the question it measured, not merely to an id.
        # Without this the audit is forgeable by omission: rewrite a question
        # into something a guesser answers from ordinary sense, leave the id
        # alone, and the rate measured against text nobody ships still admits it.
        recorded = row.get("question_digest")
        _require(
            isinstance(recorded, str) and recorded,
            f"{where}: audit row records no question_digest, so nothing ties "
            f"the measured rate to the question that was measured",
        )
        _require(
            recorded == question_digest(by_id[row["id"]]),
            f"{where}: the audit row measured a different question - its "
            f"question_digest does not cover the question text and "
            f"answer_aliases the quiz now commits. Re-run the blind-guess audit "
            f"against the current question rather than editing the digest to "
            f"match.",
        )
        # A15 - the committed count and rate describe the same measurement.
        _require(
            0 <= row["correct"] <= AUDIT_RUNS,
            f"{where}: correct {row['correct']!r} is outside 0-{AUDIT_RUNS}",
        )
        _require(
            row["correct"] == round(row["rate"] * AUDIT_RUNS),
            f"{where}: correct {row['correct']!r} disagrees with rate {row['rate']!r}",
        )
        # A16 - a question guessable without the demonstration is not a question.
        # The ceiling is inclusive: a rate measured exactly at it is admitted.
        _require(
            row["rate"] <= ceiling,
            f"{where}: blind-guess rate {row['rate']!r} exceeds the {ceiling} ceiling",
        )
