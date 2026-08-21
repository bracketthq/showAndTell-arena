import json
from pathlib import Path


TASKS = Path(__file__).parent.parent / "tasks"


def test_every_quiz_uses_only_supported_question_types():
    for path in TASKS.glob("*/quiz/questions.json"):
        questions = json.loads(path.read_text())["questions"]
        assert questions, path
        for q in questions:
            # The grader (comprehend.grade) dispatches exactly these kinds:
            # multiple_choice (options + correct_option), closed (free-text vs
            # answer_aliases), and the LLM-judged rubric kinds. Task generations
            # differ; all four spellings are live.
            assert q["type"] in {"multiple_choice", "llm_judge",
                                 "closed", "rubric"}, (path, q["id"])
            if q["type"] == "multiple_choice":
                ids = {option["id"] for option in q["options"]}
                assert len(ids) == len(q["options"]) >= 2
                assert q["correct_option"] in ids
                assert any(option["text"] == "I’m not sure" for option in q["options"])
                assert "rubric" not in q
            elif q["type"] == "closed":
                assert q["answer_aliases"], (path, q["id"])
                assert "rubric" not in q and "options" not in q
            else:
                assert q["rubric"].strip()
                assert "correct_option" not in q
                assert "options" not in q


# gap<->quiz cross-referencing is asserted bidirectionally, per task, by
# tests/test_task_contract.py::test_quiz_and_gaps_cross_reference_no_orphans.
