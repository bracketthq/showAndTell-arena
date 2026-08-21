# Contributing to ShowAndTell-Bench

ShowAndTell-Bench welcomes focused fixes, new benchmark tasks, application integrations, product adapters, tests, and documentation improvements.

The governing standard is reproducibility. A contribution should make the benchmark easier to run, harder to mis-score, or broader without changing what an existing result means silently.

## Before you start

For a large task, new application, scoring change, or protocol change, open a [GitHub issue](https://github.com/bracketthq/showAndTell-arena/issues) first. Describe the use case, the proposed evaluation signal, and any effect on existing results. Small fixes can go directly to a pull request.

Never include real credentials, customer data, or private recordings. ShowAndTell runs can capture the entire screen.

## Development setup

```bash
git clone https://github.com/bracketthq/showAndTell-arena.git
cd showAndTell-arena
./showAndTell setup
source .venv/bin/activate
pytest -q tests/test_architecture.py tests/test_tasks.py tests/test_viewer.py
```

Docker with Compose is required for fixture-backed task work. Browser tests are marked slow:

```bash
pytest -q -m slow
```

During development, run the narrowest relevant test first and the full unit suite before opening a pull request. If the base branch already has unrelated failures, identify them explicitly; do not add new ones.

## Make a focused change

Create a branch, keep unrelated changes out of it, and add a regression test when behavior changes.

```bash
git switch -c <short-description>
```

Keep modules inside the boundaries documented in [the source architecture guide](../../docs/SRC_ARCHITECTURE.md). `tests/test_architecture.py` enforces the most important import rules.

### Add or revise a benchmark task

A task is acceptable when its lesson, initial state, questions, and evidence agree.

- Declare the applications, primary application, status, and complexity in `task.toml`.
- Make `demo/seed.json` deterministic and safe to reset.
- Keep workflow orchestration in `demonstrate.py`; move reusable UI operations into the relevant application browser plane.
- Align narration to visible actions and explain decision rules that cannot be inferred from clicks alone.
- Give every quiz answer or rubric direct evidence from the demonstration bundle.
- Avoid names, values, or answer patterns that make questions guessable without the lesson.

Validate the task and inspect it in the viewer:

```bash
./showAndTell task-validate --task tasks/<task>
./showAndTell viewer
```

Review the lifecycle rules in [the task runtime guide](../../docs/TASK_RUNTIME.md), the capture workflow in [the viewer guide](viewer/README.md), and the scoring dimensions in [the complexity guide](../../docs/COMPLEXITY.md).

### Add an application

Create `src/showAndTell/applications/<name>/` with an `app.toml`. Add only the planes the application needs:

| Plane | Responsibility |
|---|---|
| `driver.py` | Start, stop, reset, and expose the application |
| `state.py` | Seed, export, and capture deterministic task data |
| `browser.py` | Log in and perform semantic UI operations |

The registry discovers folders containing `app.toml`; do not add a second application list. Test reset behavior against the real application twice in succession—state leakage between runs invalidates results. See [the application guide](../../docs/APPLICATIONS.md).

### Add a product adapter

Implement `ProductAdapter` in `src/showAndTell/students/`, register it in `students/registry.py`, and keep product-specific UI logic out of tasks and applications. An adapter must support the same ordered contract as existing products: launch, arm recording, receive the demonstration, finish processing, answer the quiz, and save auditable artifacts.

Add focused tests for preflight failures, UI state transitions, quiz transport, and cleanup.

### Change grading or benchmark semantics

Treat scoring changes as protocol changes. Document:

- which previous results are no longer comparable;
- whether task or cache fingerprints must change;
- the judge model and prompt assumptions;
- failure, timeout, retry, and aggregation behavior;
- tests that pin the new rule at boundaries and invalid inputs.

Never improve a published score by silently dropping failed questions or runs.

## Pull request checklist

Before requesting review, confirm that:

- the change has one clear purpose;
- relevant tests pass and the change adds no full-suite failures;
- new task data is deterministic and resettable;
- graded claims have inspectable evidence;
- commands, paths, and links in documentation work from the repository root;
- no secrets, personal data, caches, or local run artifacts are included;
- user-facing or result-compatibility changes are explained in the pull request;
- third-party code or data includes its source and license notice.

In the pull request description, state what changed, why it is needed, how you verified it, and any result-compatibility impact. Screenshots or short recordings are useful for viewer and workflow changes.

## Review principles

Maintainers review for correctness, evidence quality, reproducibility, scope, and operational safety. A smaller contribution with a complete proof is preferred to a broad contribution that cannot be independently replayed.

## License

By submitting a contribution, you agree that it may be distributed under the [MIT License](LICENSE). You must have the right to contribute all included code, data, images, and recordings.
