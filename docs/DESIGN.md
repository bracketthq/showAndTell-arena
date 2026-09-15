# ShowAndTell-Bench — design

*2026-07-06 · pilot (v0) design · simplified 2026-07-14 to teach + quiz*

## 1. What this is

A public benchmark for **teach-by-demonstration agent systems**: a human
demonstrates an operations workflow once (narrated screen recording) through a
product's own teach flow; the product is then quizzed on the work it just
learned. How well it answers is the benchmark result.

Launch systems: **Brackett (Show and Tell)**, **Claude-in-Chrome (Teach)**,
**Codex (Record & Replay)**.

Positioning: Terminal-Bench measures "can an agent do a task described in
text." ShowAndTell-Bench measures "can an agent learn a *job* from being shown"
— comprehension from demonstration. The quiz probes whether the product
grasped the underlying rules, not just the clicks it saw.

## 2. The flow

For each product, one run:

1. Start the task's fixture applications, seeded with its demo data.
2. Start the product's own teach/record flow and log into the fixture applications.
3. **Perform the demonstration** — the task's `demonstrate.py` drives the
   clicks while narrating each step aloud, so the product captures spoken
   reasoning in sync with the actions.
4. Finish the recording and let the product process it.
5. **Quiz** — ask the product a fixed set of questions about the workflow and
   grade the answers.

## 3. Task anatomy

```
tasks/<task-name>/
  task.toml            # name, applications, primary application, summary
  task_logic.py        # the workflow's decision rules (source of truth)
  demonstrate.py       # drives the demonstration in the fixture UI
  demo/
    seed.json          # fixture state the demo runs against
    narration_script.jsonl  # spoken narration, step-aligned
  quiz/questions.json  # the question set: multiple_choice | closed | llm_judge
                       # (never shown to the product)
```

`seed.json` is exported from the viewer's capture stage (tasks that carry a
`build_task.py` generate it from `task_logic.py` instead), so demo data and
workflow rules stay in step.

## 4. Fixture app contract

Fixture apps are self-hosted, dockerizable web apps the demonstration runs
against (the v0 pilot used **opsfix**, a small ops system; today each task
declares real applications such as ERPNext or Roundcube).

The product-facing and harness-facing surfaces are hard-separated:

- **App surface** (product-facing): login + normal UI. No trace of harness APIs.
- **Harness surface**: seeding and reset go through each application's own
  API/IMAP/REST state plane, and containers are managed by the fixture host
  agent. Neither is ever exposed to the product's browser.

## 5. Quiz protocol

The quiz is shared across products (`comprehend.py`): the harness asks the
questions in the product's own chat/session surface (each live adapter supplies
an `ask(message) -> response_text`), then grades the reply. No fixture access
during the quiz.

## 6. Scoring

- **multiple-choice** questions: exact match on the selected option identifier.
- **closed** questions: exact match against the accepted aliases, with an LLM
  semantic-equivalence check for near-miss phrasings; scored 1/0.
- **LLM-judge** questions: an LLM judge scores semantic correctness from 0..1
  against the published rubric. `judge.toml` pins the release model and
  generation settings; prompt and config fingerprints plus raw judgments are
  stored with the result.
- **headline score**: mean over all questions (multiple choice and closed →
  1/0, LLM judge → the judge score).

## 7. Fairness & anti-gaming

- Identical demo, narration, and fixture state per product; adapters are open
  source in this repo and vendors may PR fixes to their own adapter.
- `quiz/` answers are never shown to the product; the fixture port is
  unreachable from its browser.
- Published judge prompts, full run logs, per-decision provenance, and a
  human-labeled adversarial judge suite make results auditable. Candidate
  answers are explicitly untrusted data, never judge instructions.
- A judge infrastructure failure leaves the run ungraded (`score: null`) rather
  than changing the benchmark score. Product responses can be re-graded without
  replaying the lesson.

## 8. Pilot scope (v0)

- 1 fixture app (opsfix), one task **invoice-3way-match**.
- Live adapters: `claude-teach`, `brackett-teach`, `codex-record`.

## 9. Deferred (v1+)

- Hosted submission service.
- More tasks, a second fixture app, human-recorded demos + a human baseline.
- Contamination canary rotation.
