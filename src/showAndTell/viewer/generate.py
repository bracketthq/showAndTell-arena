"""Generate the ShowAndTell-Bench viewer: a self-contained index.html that browses
every task's demonstration, quiz, and per-product results.

Scans tasks/ for task definitions, runs/.cache/ for graded results, and runs/
for completed replays whose grading is unavailable, then assembles this
package's src/ — skeleton.html plus the
stylesheets and scripts named by CSS_MANIFEST/JS_MANIFEST, concatenated in
order — around the data, embedded as a JSON <script> island. Open
src/showAndTell/viewer/index.html directly in a browser (no server needed).

    python -m showAndTell.viewer.generate
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import shlex
import sys
import tomllib
from pathlib import Path

from showAndTell.core import cache as cache_module
from showAndTell.bundles import hub
from showAndTell.quiz import complexity

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
TASKS_DIR = ROOT / "tasks"
APPLICATIONS_DIR = HERE.parent / "applications"
CACHE_DIR = ROOT / "runs" / ".cache"
SRC = HERE / "src"

# Inlined into index.html in this order. The JS files share one
# <script type="module"> scope: no import/export, later files may call
# anything defined by earlier ones.
JS_MANIFEST = [
    "html.js",
    "api.js",
    "state.js",
    "router.js",
    "components.js",
    "views/overview.js",
    "views/demo.js",
    "views/quiz.js",
    "views/results.js",
    "task-runs.js",
    "executions.js",
    "views/settings.js",
    "views/task.js",
    "palette.js",
    "keyboard.js",
    "capture.js",
    "intro.js",
    "app.js",
]
CSS_MANIFEST = [
    "styles/tokens.css",
    "styles/base.css",
    "styles/components.css",
    "styles/views.css",
]

# The teach products, in display order — one row per product: the draft/trial
# id, the CLI adapter that runs it, and the display label. The one place a
# product is added; serve.py, task_runs.py, and capture.py all derive from it.
PRODUCT_ROWS = (
    ("claude", "claude-teach", "Claude"),
    ("brackett", "brackett-teach", "Brackett"),
    ("codex", "codex-record", "Codex"),
)
PRODUCTS = [(adapter, label) for _, adapter, label in PRODUCT_ROWS]
DRAFT_PRODUCT_ADAPTERS = {product: adapter for product, adapter, _ in PRODUCT_ROWS}
DRAFT_PRODUCT_LABELS = {product: label for product, _, label in PRODUCT_ROWS}

# Recording and trial-artifact filenames, shared with serve.py's allowlists so
# a URL built here is always servable there.
DEMO_RECORDING_NAMES = ("recording.webm", "recording.mp4", "recording.mov",
                        "recording.mkv")
SCREEN_RECORDING_NAMES = ("screen.mov", "screen.mkv")
TRIAL_ARTIFACTS = {  # filename -> (viewer label, content type)
    "comprehend.json": ("grading result", "application/json; charset=utf-8"),
    "comprehend-response.txt": ("complete quiz response", "text/plain; charset=utf-8"),
    "result.json": ("trial manifest", "application/json; charset=utf-8"),
    "learned.txt": ("learned prompt", "text/plain; charset=utf-8"),
    "panel.png": ("panel screenshot", "image/png"),
    "handoff.txt": ("agent handoff", "text/plain; charset=utf-8"),
    "brackett.png": ("Brackett screenshot", "image/png"),
    "codex_chat.txt": ("Codex transcript", "text/plain; charset=utf-8"),
    "codex.png": ("Codex screenshot", "image/png"),
}
# Applications are discovered from the same manifests used by the runtime.
APPLICATION_ORDER = sorted(
    path.name for path in APPLICATIONS_DIR.iterdir()
    if (path / "app.toml").is_file()
)


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:  # temp dirs in tests live outside the repo
        return str(path)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _read_json(path: Path, default, warnings: list[str]):
    """Missing file -> default (a legitimate empty state); bad JSON -> warn."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return default
    except json.JSONDecodeError as e:
        warnings.append(f"{_rel(path)}: bad JSON ({e})")
        return default


def _read_json_list(path: Path, key: str, warnings: list[str]) -> list:
    """Read a list nested under ``key``. A non-object warns and reads as empty."""
    obj = _read_json(path, {}, warnings)
    if not isinstance(obj, dict):
        warnings.append(f"{_rel(path)}: expected an object")
        return []
    return obj.get(key, [])


def _file_sha256(path: Path) -> str | None:
    """Hash the exact bytes used by the serve-mode optimistic write guard."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _task_title(task: dict, testcase: object, name: str) -> str:
    """Return a human-facing title while keeping ``name`` as stable identity."""
    captured_title = testcase.get("title") if isinstance(testcase, dict) else None
    for value in (task.get("title"), captured_title):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return name.replace("_", "-").replace("-", " ").title()


def _read_jsonl(path: Path, warnings: list[str]) -> list[dict]:
    out = []
    for i, line in enumerate(_read(path).splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            warnings.append(f"{_rel(path)}:{i}: bad JSONL line")
    return out


def _recording(root: Path, run_dir) -> str | None:
    """Viewer-relative URL (../../../runs/…/screen.mov|mkv) of the run's screen
    recording, or None. Relative to the generated index.html so it works both opened
    as a file and served by serve.py. A missing file is normal, not a warning
    (a shared cache without run dirs). Absolute run_dirs get None — no valid
    relative URL exists for them."""
    if not run_dir or not isinstance(run_dir, str) or Path(run_dir).is_absolute():
        return None
    for name in SCREEN_RECORDING_NAMES:
        if (root / run_dir / name).is_file():
            return f"../../../{run_dir}/{name}"
    return None


def _run_artifacts(root: Path, run_dir) -> list[dict]:
    """Allowlisted artifacts for a repository-local promoted run."""
    if not run_dir or not isinstance(run_dir, str) or Path(run_dir).is_absolute():
        return []
    directory = root / run_dir
    return [
        {"label": label, "url": f"../../../{run_dir}/{filename}"}
        for filename, (label, _ctype) in TRIAL_ARTIFACTS.items()
        if (directory / filename).is_file()
    ]


def _demo_recording(task_dir: Path, asset_root: str = "tasks") -> str | None:
    """Canonical reviewer video committed beside a task's demonstration."""
    for filename in DEMO_RECORDING_NAMES:
        if (task_dir / "demo" / filename).is_file():
            return f"{asset_root}/{task_dir.name}/demo/{filename}"
    return None


def load_results(cache_dir: Path, warnings: list[str],
                 task_fingerprints: dict[str, str] | None = None,
                 task_dirs: dict[str, Path] | None = None) -> dict[str, dict[str, dict]]:
    """runs/.cache/<adapter>__<task>__<hash>.json -> {task: {adapter: payload}}.
    Keeps every graded payload per (task, adapter), plus completed promoted
    replays whose judge failed. Ungraded runs are visible history, never cache
    hits. The freshest valid grade remains the product's score summary."""
    root = cache_dir.parent.parent   # runs/.cache -> repo root
    by_task: dict[str, dict[str, dict]] = {}
    cached_run_dirs: set[Path] = set()
    for f in sorted(cache_dir.glob("*.json")) if cache_dir.exists() else []:
        p = _read_json(f, None, warnings)
        if p is None:
            continue
        if not isinstance(p, dict) or not {"task", "adapter", "result"} <= p.keys():
            warnings.append(f"{_rel(f)}: missing task/adapter/result keys")
            continue
        expected = (task_fingerprints or {}).get(p["task"])
        # Cache entries created before fingerprints existed remain readable.
        # Once an entry carries a fingerprint, however, never combine it with
        # results for a different version of the task.
        stored_fingerprint = p.get("fingerprint")
        if (expected and isinstance(stored_fingerprint, str)
                and len(stored_fingerprint) == 64
                and stored_fingerprint != expected):
            continue
        r = p["result"]
        if not isinstance(r, dict):
            warnings.append(f"{_rel(f)}: result is not an object")
            continue
        run = {
            "score": r.get("score"),
            "multiple_choice_correct": r.get("multiple_choice_correct", r.get("closed_correct")),
            "multiple_choice_total": r.get("multiple_choice_total", r.get("closed_total")),
            "per_question": r.get("per_question", []),
            "cached_at": p.get("cached_at"),
            "run_dir": p.get("run_dir"),
            "recording": _recording(root, p.get("run_dir")),
            "artifacts": _run_artifacts(root, p.get("run_dir")),
            "deletion": {"kind": "cache", "id": f.name},
        }
        if isinstance(p.get("run_dir"), str):
            candidate = Path(p["run_dir"])
            cached_run_dirs.add(
                (candidate if candidate.is_absolute() else root / candidate).resolve())
        slot = by_task.setdefault(p["task"], {})
        product = slot.setdefault(p["adapter"], {"runs": []})
        product["runs"].append(run)

    # A judge outage deliberately prevents cache.save(), but the replay and
    # its artifacts are still valid history. Recover those runs directly from
    # their durable directories so Results never misreports "not run".
    runs_dir = cache_dir.parent
    for run_dir in sorted(
            (path for path in runs_dir.iterdir()
             if path.is_dir() and not path.name.startswith(".")),
            reverse=True) if runs_dir.is_dir() else []:
        if run_dir.resolve() in cached_run_dirs:
            continue
        adapter = cache_module.adapter_from_run_dir(run_dir)
        if adapter is None:
            continue
        result = _read_json(run_dir / "comprehend.json", None, warnings)
        if result is None:
            continue
        if not isinstance(result, dict):
            warnings.append(f"{_rel(run_dir / 'comprehend.json')}: expected an object")
            continue
        # Completed, uncached grades may belong to an old task/judge version.
        # They remain outside Results until cache.save establishes provenance.
        if result.get("status", "complete") == "complete":
            continue
        runtime = _read_json(run_dir / "task-runtime.json", {}, warnings)
        if not isinstance(runtime, dict) or not isinstance(runtime.get("task"), str):
            warnings.append(
                f"{_rel(run_dir / 'task-runtime.json')}: missing task identity")
            continue
        task = runtime["task"]
        if task_fingerprints is not None and task not in task_fingerprints:
            continue
        task_dir = (task_dirs or {}).get(task, root / "tasks" / task)
        relative_run_dir = str(run_dir.relative_to(root))
        run = {
            "score": None,
            "multiple_choice_correct": result.get(
                "multiple_choice_correct", result.get("closed_correct")),
            "multiple_choice_total": result.get(
                "multiple_choice_total", result.get("closed_total")),
            "per_question": result.get("per_question", []),
            "cached_at": _trial_timestamp(run_dir.name),
            "run_dir": relative_run_dir,
            "recording": _recording(root, relative_run_dir),
            "status": "complete",
            "grading": _grading_failure(result, task_dir, run_dir),
            "artifacts": _run_artifacts(root, relative_run_dir),
            "deletion": {"kind": "run", "id": run_dir.name},
        }
        slot = by_task.setdefault(task, {})
        slot.setdefault(adapter, {"runs": []})["runs"].append(run)
    for products in by_task.values():
        for product in products.values():
            _summarize_product(product, pick_summary=lambda runs: next(
                (run for run in runs if isinstance(run.get("score"), (int, float))),
                runs[0]))
            product["grading"] = product["runs"][0].get("grading")
    return by_task


def _summarize_product(product: dict, *, pick_summary=None) -> None:
    """Fold a product's runs (freshest first) into the flat summary fields.

    ``pick_summary`` chooses which run becomes the summary; the default is the
    freshest one.
    """
    product["runs"].sort(key=lambda run: run.get("cached_at") or "", reverse=True)
    runs = product["runs"]
    product.update(pick_summary(runs) if pick_summary else runs[0])
    scores = [run["score"] for run in runs
              if isinstance(run.get("score"), (int, float))]
    product["score"] = sum(scores) / len(scores) if scores else None
    product["graded_run_count"] = len(scores)
    product["run_count"] = len(runs)


def _trial_timestamp(dirname: str) -> str | None:
    """Turn YYYYMMDD-HHMMSS-product into the viewer's ISO timestamp."""
    try:
        stamp = dirname.split("-", 2)[:2]
        return _dt.datetime.strptime("-".join(stamp), "%Y%m%d-%H%M%S").isoformat()
    except (ValueError, IndexError):
        return None


def _trial_artifacts(task_dir: Path, trial_dir: Path, manifest: dict) -> list[dict]:
    base = f"/task-drafts/{task_dir.name}/trials/{trial_dir.name}"
    artifacts = [
        {"label": label, "url": f"{base}/{filename}"}
        for filename, (label, _ctype) in TRIAL_ARTIFACTS.items()
        if (trial_dir / filename).is_file()
    ]
    agent_url = manifest.get("agent_url")
    if isinstance(agent_url, str) and agent_url.startswith(("http://", "https://")):
        artifacts.append({"label": "open agent", "url": agent_url})
    return artifacts


def _draft_trial_recording(task_dir: Path, trial_dir: Path) -> str | None:
    base = f"/task-drafts/{task_dir.name}/trials/{trial_dir.name}"
    for name in SCREEN_RECORDING_NAMES:
        if (trial_dir / name).is_file():
            return f"{base}/{name}"
    return None


_JUDGE_API_KEYS = {
    "anthropic-api": "ANTHROPIC_API_KEY",
    "openai-api": "OPENAI_API_KEY",
    "gemini-api": "GEMINI_API_KEY",
}


def _grading_failure(result: dict, task_dir: Path, trial_dir: Path) -> dict | None:
    """Return safe, actionable viewer data for an incomplete judge pass."""
    if result.get("status") != "incomplete" or not result.get("judge_failures"):
        return None
    judge = result.get("judge") if isinstance(result.get("judge"), dict) else {}
    backend = judge.get("backend")
    error = None
    questions = result.get("per_question")
    for question in questions if isinstance(questions, list) else []:
        if not isinstance(question, dict):
            continue
        details = question.get("judge")
        if not isinstance(details, dict):
            continue
        failures = details.get("errors")
        for failure in failures if isinstance(failures, list) else []:
            if isinstance(failure, dict) and isinstance(failure.get("message"), str):
                error = failure["message"].strip()[:500]
                if error:
                    break
        if error:
            break

    commands = []
    key_name = _JUDGE_API_KEYS.get(backend)
    if key_name:
        commands.append(f"export {key_name}='your-key'")
    elif backend == "claude-cli":
        commands.append("claude auth login")
    elif backend == "codex-cli":
        commands.append("codex login")
    commands.extend([
        "./showAndTell judge doctor",
        "./showAndTell regrade "
        f"--task {shlex.quote(_rel(task_dir))} "
        f"--run {shlex.quote(_rel(trial_dir))} --replace",
    ])
    return {
        "status": "incomplete",
        "judge_failures": result.get("judge_failures"),
        "backend": backend,
        "model": judge.get("model"),
        "error": error or "The configured judge could not grade this replay.",
        "credential": key_name,
        "task": _rel(task_dir),
        "run": _rel(trial_dir),
        "recovery_command": "\n".join(commands),
    }


def load_draft_results(task_dir: Path, warnings: list[str]) -> dict[str, dict]:
    """Load draft trials into the same product/run shape as cached results.

    Current trials all write the ordinary ``comprehend.json`` schema.  Older
    trials only have a completion manifest; retain those as visible, ungraded
    runs rather than pretending they never happened.
    """
    trials_dir = task_dir / "trials"
    by_product: dict[str, dict] = {}
    if not trials_dir.is_dir():
        return by_product
    for trial_dir in sorted(
            (path for path in trials_dir.iterdir() if path.is_dir()), reverse=True):
        product = next(
            (name for name in DRAFT_PRODUCT_ADAPTERS
             if trial_dir.name.endswith(f"-{name}")),
            None,
        )
        if product is None:
            continue
        manifest = _read_json(trial_dir / "result.json", {}, warnings)
        if not isinstance(manifest, dict):
            warnings.append(f"{_rel(trial_dir / 'result.json')}: expected an object")
            manifest = {}
        graded = _read_json(trial_dir / "comprehend.json", None, warnings)
        if graded is not None and not isinstance(graded, dict):
            warnings.append(f"{_rel(trial_dir / 'comprehend.json')}: expected an object")
            graded = None
        # New result manifests duplicate the standard fields so either file is
        # independently useful.  This also reads transitional Brackett trials
        # that stored only ``comprehend_score`` in their manifest.
        result = graded or (manifest if "score" in manifest else {})
        score = result.get("score", manifest.get("comprehend_score"))
        run = {
            "score": score if isinstance(score, (int, float)) else None,
            "multiple_choice_correct": result.get(
                "multiple_choice_correct", result.get("closed_correct")),
            "multiple_choice_total": result.get(
                "multiple_choice_total", result.get("closed_total")),
            "per_question": result.get("per_question", []),
            "cached_at": _trial_timestamp(trial_dir.name),
            "run_dir": str(trial_dir),
            "recording": _draft_trial_recording(task_dir, trial_dir),
            "status": manifest.get("status", "incomplete"),
            "grading": _grading_failure(result, task_dir, trial_dir),
            "artifacts": _trial_artifacts(task_dir, trial_dir, manifest),
            "deletion": {
                "kind": "draft", "task": task_dir.name, "id": trial_dir.name,
            },
        }
        adapter = DRAFT_PRODUCT_ADAPTERS[product]
        by_product.setdefault(adapter, {"runs": []})["runs"].append(run)
    for product in by_product.values():
        # The summary is the freshest *scored* trial: an ungraded legacy trial
        # must not hide a graded one.
        _summarize_product(product, pick_summary=lambda runs: next(
            (run for run in runs if isinstance(run.get("score"), (int, float))),
            runs[0]))
        # Grading recovery always describes the newest replay, even when the
        # headline score intentionally remains an older valid measurement.
        product["grading"] = product["runs"][0].get("grading")
    return by_product


def load_task(task_dir: Path, results: dict, warnings: list[str], *,
              draft: bool = False, dataset: bool = False) -> dict | None:
    toml_path = task_dir / "task.toml"
    if not toml_path.exists():
        return None
    try:
        cfg = tomllib.loads(_read(toml_path))
    except tomllib.TOMLDecodeError as e:
        warnings.append(f"{_rel(toml_path)}: bad TOML ({e}) — task skipped")
        return None
    task = cfg.get("task", {})
    if not isinstance(task, dict):
        warnings.append(f"{_rel(toml_path)}: [task] is not a table — task skipped")
        return None
    name = task.get("name", task_dir.name)
    questions_path = task_dir / "quiz" / "questions.json"
    seed = _read_json(task_dir / "demo" / "seed.json", {}, warnings)
    raw_seed_events = _read_jsonl(
        task_dir / "demo" / "seed_events.jsonl", warnings)
    seed_events = [{
        key: event.get(key)
        for key in ("type", "page", "description", "value", "key")
        if event.get(key) not in (None, "")
    } for event in raw_seed_events]
    testcase = _read_json(task_dir / "testcase.json", {}, warnings)
    return {
        "name": name,
        "title": _task_title(task, testcase, name),
        "draft": draft,
        "dataset": dataset,
        "questionTask": task_dir.name,
        "questionSource": "draft" if draft else "dataset" if dataset else "task",
        "questionsHash": _file_sha256(questions_path),
        "applications": list(task.get("applications", ())),
        "primaryApplication": task.get("primary_application", ""),
        "summary": task.get("summary", ""),
        "budgets": cfg.get("budgets", {}),
        "phases": cfg.get("phases", {}),
        "status": cfg.get("status", {}),
        "narration": _read_jsonl(task_dir / "demo" / "narration_script.jsonl", warnings),
        "seed": seed if isinstance(seed, dict) else {},
        "seedEvents": seed_events,
        "seedInfo": testcase.get("seed", {}) if isinstance(testcase, dict) else {},
        "demoRecording": _demo_recording(
            task_dir, "task-drafts" if draft
            else ("dataset-tasks" if dataset else "tasks")),
        "demonstrate": _read(task_dir / "demonstrate.py"),
        "taskLogic": _read(task_dir / "task_logic.py"),
        "questions": _read_json_list(questions_path, "questions", warnings),
        "complexity": complexity.score_task(task_dir),
        "results": results.get(name, {}),
    }


def build(tasks_dir: Path | None = None,
          cache_dir: Path | None = None, *,
          include_drafts: bool = False) -> tuple[dict, list[str]]:
    """Scan the repo -> (viewer data, human-readable warnings)."""
    tasks_dir = tasks_dir or TASKS_DIR
    cache_dir = cache_dir or CACHE_DIR
    warnings: list[str] = []
    # a checkout without tasks/ (e.g. one that fetches bundles from the
    # dataset) gets an empty viewer, not a crash
    task_entries = sorted(tasks_dir.iterdir()) if tasks_dir.is_dir() else []
    task_fingerprints = {
        d.name: cache_module.fingerprint(d)
        for d in task_entries
        if d.is_dir() and (d / "task.toml").exists()
    }
    task_dirs = {
        d.name: d
        for d in task_entries
        if d.is_dir() and (d / "task.toml").exists()
    }
    # published bundles already pulled into the hub cache join the scan,
    # read-only; a local tasks/<name> checkout wins on a name collision
    dataset_info = hub.cached_dataset_info()
    dataset_dirs = [
        d for d in (dataset_info or {}).get("task_dirs", ())
        if d.name not in task_dirs and (d / "task.toml").exists()
    ]
    task_fingerprints |= {
        d.name: cache_module.fingerprint(d) for d in dataset_dirs}
    task_dirs |= {d.name: d for d in dataset_dirs}
    results = load_results(
        cache_dir, warnings, task_fingerprints, task_dirs)
    tasks = []
    for d in sorted(p for p in task_entries if p.is_dir()):
        t = load_task(d, results, warnings)
        if t:
            tasks.append(t)
    for d in dataset_dirs:
        t = load_task(d, results, warnings, dataset=True)
        if t:
            tasks.append(t)
    drafts = []
    if include_drafts:
        drafts_dir = tasks_dir.resolve().parent / "task-drafts"
        task_names = {task["name"] for task in tasks}
        if drafts_dir.is_dir():
            for d in sorted(p for p in drafts_dir.iterdir() if p.is_dir()):
                t = load_task(d, {}, warnings, draft=True)
                if t and t["name"] not in task_names:
                    t["results"] = load_draft_results(d, warnings)
                    drafts.append(t)
    data = {
        "generatedAt": _dt.datetime.now().isoformat(timespec="seconds"),
        "products": [{"id": pid, "label": lbl} for pid, lbl in PRODUCTS],
        "applicationOrder": APPLICATION_ORDER,
        "tasks": tasks,
        "drafts": drafts,
        "dataset": ({"repo": dataset_info["repo"],
                     "revision": dataset_info["revision"],
                     "count": len(dataset_dirs)}
                    if dataset_info else None),
    }
    return data, warnings


def assemble(data: dict) -> str:
    """Inline styles, scripts, and the JSON data island into src/skeleton.html."""
    css = "\n".join(f"/* ==== {p} ==== */\n{(SRC / p).read_text(encoding='utf-8')}" for p in CSS_MANIFEST)
    js = "\n".join(f"// ==== {p} ====\n{(SRC / p).read_text(encoding='utf-8')}" for p in JS_MANIFEST)
    # A literal "<" inside the island could open "</script>" or "<!--" parser
    # states; the JSON escape "\u003c" is the same string to JSON.parse but
    # inert to the HTML parser.
    blob = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    page = (SRC / "skeleton.html").read_text(encoding="utf-8")
    replacements = [
        ("<!-- @styles -->", f"<style>\n{css}\n</style>"),
        ("<!-- @scripts -->", f'<script type="module">\n{js}\n</script>'),
        # data goes last so no marker scan runs over user-authored text
        ("<!-- @data -->", f'<script type="application/json" id="showAndTell-data">{blob}</script>'),
    ]
    for marker, replacement in replacements:
        if page.count(marker) != 1:
            raise SystemExit(f"src/skeleton.html: expected exactly one {marker!r}")
        page = page.replace(marker, replacement)
    return page


def main() -> None:
    try:
        data, warnings = build()
    except complexity.ComplexityError as e:
        raise SystemExit(f"error: {e}")
    out = HERE / "index.html"
    out.write_text(assemble(data), encoding="utf-8")
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    n_q = sum(len(t["questions"]) for t in data["tasks"])
    n_res = sum(len(t["results"]) for t in data["tasks"])
    tail = f", {len(warnings)} warning(s)" if warnings else ""
    print(f"wrote {out.relative_to(ROOT)} — {len(data['tasks'])} tasks, "
          f"{n_q} questions, {n_res} product results{tail}")
    if not data["tasks"]:
        raise SystemExit("no tasks found under tasks/ — nothing to view")


if __name__ == "__main__":
    main()
