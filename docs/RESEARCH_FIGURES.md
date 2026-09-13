# Refresh the AEI / Show & Tell research figures

This is the working guide for Claude, Codex, and anyone updating the research page. Keep AEI central, Show & Tell as the concrete Teach → Comprehend benchmark, and the page visual. Use the existing light Brackett palette. Keep source data and numerical claims traceable.

## Start here

Run from this repository's root, on the branch containing `site/` (currently `aei-research-site`). Read `site/README.md` for source provenance and hosting context. Preserve unrelated changes.

```sh
python3 -m venv .venv-charts
.venv-charts/bin/python -m pip install 'matplotlib>=3.8,<4' 'openpyxl>=3.1,<4' pytest
.venv-charts/bin/python -m pip install -e . --no-deps
```

Use Python 3.12 or newer. The utilities use Matplotlib and openpyxl; no browser or running benchmark is required. They read source files without executing demonstrations or task logic.

## The normal refresh command

Reproduce the figures from the committed snapshots, without the original Excel file or task archive:

```sh
.venv-charts/bin/python scripts/refresh_research_figures.py
```

For new results:

```sh
.venv-charts/bin/python scripts/refresh_research_figures.py \
  --results /path/to/results.xlsx \
  --snapshot-date YYYY-MM-DD
```

The utility stages the publication in a temporary directory, regenerates figures and frozen data, updates the saved HTML bars and numeric captions, and runs the integrity checks. It copies the outputs back only after validation succeeds. It does **not** commit, push, change visibility, or publish. An interrupted copy is not an atomic directory swap; inspect the diff and rerun if interrupted during the final copy.

It refreshes:

| Output | What it shows |
|---|---|
| `site/data/results.json` and `results.csv` | Overview values and selected attempts used by the interactive page |
| `assets/analysis/task-inventory.{svg,png}` | Cases grouped by business process |
| `assets/analysis/case-heatmap.{svg,png}` | Case means by system, using one common color scale |
| `assets/analysis/attempt-outcomes.{svg,png}` | Scored answers and incomplete attempts, with reasons from source notes |
| `assets/analysis/repeat-spread.{svg,png}` | Largest observed ranges between selected attempts of one case |
| `assets/analysis/task-evidence.{svg,png}` | Evidence cited by the committed sample quiz |
| `assets/analysis/tci-*.{svg,png}` and `tci.json` | Optional task buckets and component examples |
| `assets/analysis/analysis.json` and `attempts.csv` | Analysis values, classifications, provenance, and reusable attempt export |
| `site/index.html` | Saved bars, date, coverage, leaders, and download counts |

Asset paths in the table are relative to `site/`. The utility preserves the original source provenance and snapshot date when rerendering existing data. Passing new results records a new source filename and SHA-256. No current date is silently substituted for the evaluation snapshot date.

## Results input contract

Prefer the original XLSX with `Overview` and `Detailed Analysis`. Run scores, rather than cached average cells, are authoritative. Detailed Analysis notes are joined by exact system, case, and integer run number; mismatched scores are rejected (a difference within floating-point noise is tolerated). Detailed rows whose run label is `Earlier N` must say “excluded from overview” in their note; they are exported to `analysis.json` under `excluded_attempts` for provenance and never enter a score. Any other non-integer run label stops the refresh.

The supported long-form CSV export has these columns:

```csv
Usecase,Agent,Run,Score,Outcome,Comment
Example process,Brackett,1,0.85,Scored,
Example process,Claude,1,NA,Empty shortcut,empty shortcut no quiz
Example process,Codex,1,0,Scored,
Untested process,Brackett,1,,Untested,
```

Use `Run` numbers beginning at 1 in occurrence order for each case/system. `Comment` is optional; preserve it when available. Outcome labels are recomputed from completion and comments, not trusted from the `Outcome` column. Missing notes produce `Other incomplete`, not an invented failure reason. The narrow `site/data/results.csv` excludes wholly untested cases and notes; use **`assets/analysis/attempts.csv`** for full refreshes without the workbook.

- Numeric zero is a scored answer. NA is an incomplete attempt worth zero. Blank means untested and remains absent from the mean.
- Average attempts within each case, then weight cases equally. Shared means use only cases attempted by every system, including incomplete attempts.
- Preserve selected-attempt and manual-grading qualifications until a new source supports changing them. Comprehension results are not operational success or a full AEI score.
- Repetitions are not before/after learning tests. Largest ranges (max − min across a case's selected attempts) are intentionally selected diagnostics, not confidence intervals or representative variability estimates. Attempts per case may differ by system and case; `analysis.json` records the distribution under `attempts_per_case`.
- Grading provenance comes from the workbook notes. The current snapshot mixes manual grades with imported model-assisted grades (Claude Sonnet 4.6) for the third Brackett attempt set and the Brackett-only cases; the page states this outside the generated blocks, so review that paragraph when the grading mix changes.
- The page currently supports Brackett, Claude, and Codex and requires a nonempty shared cohort. A different system set requires updating `site/app.js`, table headings, palette, saved-bar generation, and checks. The wrapper stops rather than silently omitting a new system.

## New tasks and business-process groups

`site/data/task-groups.json` maps exact workbook case names to editorial business-process groups. When adding result cases, add their assignments there or pass a replacement with `--task-groups /path/to/groups.json`. An unknown case is rejected if neither the explicit map nor the generator's existing mappings classify it. Do not infer a workflow's rules or difficulty from its name.

Named variants count as separate case entries in the inventory. They are not automatically independent workflow families. The result inventory and TCI task population can be different; identify each population explicitly.

## Using TCI to choose arena tasks

TCI is a useful heuristic for bucketing tasks before they enter the benchmark arena. It helps us spread the suite across expected difficulty levels and look for tasks we expect to be harder. It uses rule structure, evidence, planning, precision, implicit constraints, and demonstration burden. Those weights and buckets are design choices; observed results may challenge the expected difficulty.

Keep it an optional task-selection view. Do not imply that the buckets establish a validated difficulty scale or that a larger TCI guarantees worse agent performance. Do not change the scorer or thresholds as part of a figure refresh.

To recompute from a new task snapshot:

```sh
.venv-charts/bin/python scripts/refresh_research_figures.py \
  --tasks-root /path/to/tasks \
  --task-revision FULL_SOURCE_COMMIT_OR_DATASET_REVISION \
  --task-label 'Arena candidate suite · YYYY-MM-DD' \
  --task-scope 'N task definitions from this source snapshot; describe how these relate to the result cases.'
```

Combine these flags with `--results` and `--snapshot-date` when both populations change. Replace all placeholders with the actual source metadata and counts.

Each task folder must contain `task.toml` with complete `[complexity]` flags, substantive `task_logic.py`, `demonstrate.py`, and `demo/narration_script.jsonl`. Read the actual task before including it: a valid-looking placeholder or incomplete capture can produce a misleading bucket even if the parser accepts it. The utility stops on missing or invalid inputs; it never silently drops them. Source hashes, the scorer hash, component inputs, scores, and tiers are exported to `tci.json`.

The currently committed reference contains 49 historical definitions at revision `6e48f4d70b870d2a05c9f1ff15d07db7a678d2ee` (26 July 2026), separate from the 42 workbook entries. Do not join those populations by approximate names. A future score-versus-TCI figure needs an explicit task/version/result mapping and declared treatment of related variants.

## Changing the illustrated task or quiz

The wrapper regenerates the evidence matrix from `site/data/task-questions.json`. That file currently contains the full pinned returns quiz. It does not automatically replace the three selected questions, expected answers, screenshots, timestamps, or recording on the page.

For a new quiz revision or task, update `data/task-questions.json`, `data/example-source.json`, and the illustrated page panels together. Pin the dataset revision in every source link. Check the exact evidence frames and question text. The current matrix uses narration, screenshots, and source files; update its labels/type handling if the new quiz uses other modalities. The generator's friendly question labels and “Published returns task” caption are specific to this example. Update them for a different task. Do not describe the latest illustrative quiz as the one used by earlier runs without checking provenance.

## Review before committing

```sh
.venv-charts/bin/python scripts/check_site.py
node --check site/app.js
.venv-charts/bin/python -m pytest tests/test_research_refresh.py tests/test_benchmark_charts.py --noconftest -q
git diff --check
```

`--noconftest` keeps these standalone chart tests independent of the benchmark web-server fixtures. Inspect the generated PNGs for clipping and the page at a narrow and wide width for legibility. Check the source date, data scope, all numerical captions and alt text, and any editorial claims outside the generated blocks. The wrapper updates only marked numeric regions; it does not rewrite the narrative or methodology. If the sample quiz changed, review its captions and answer panels manually as described above.

The publication defaults to light backgrounds. Preserve SVG text and its browser font fallback; PNGs are available for slides and documents. Keep untested heatmap cells visually distinct from zero.

A push to `aei-research-site` touching `site/**` deploys the private Pages preview. Follow the user's publishing instructions, keep the same branch unless directed otherwise, and verify the workflow finishes. Keep public launch and repository visibility changes separate from refreshing figures.

## Paper and top-line links

The header links to Brackett, GitHub, Hugging Face, and the current manuscript draft. `site/downloads/ShowTellArena_Paper_Draft.pdf` is copied unchanged from `~/repos/showtellarena/main.pdf` (currently paper commit `102653b`, built from the same workbook snapshot). Refreshing figures does not rebuild it; copy a newly built PDF deliberately and note the paper commit here. No arXiv identifier was found in the paper repository. When the paper is published, replace the draft link with its verified `https://arxiv.org/abs/...` URL and change the label to “Paper on arXiv”. Do not invent an ID or link to arXiv's homepage as if it were the paper.

## Smaller utilities

- `python -m showAndTell.benchmark_charts INPUT --output-dir OUT`: original overview and paginated comparison figures; supports the existing palette/theme options.
- `python -m showAndTell.benchmark_analysis INPUT --output-dir OUT --task-quiz QUESTIONS --task-groups GROUPS`: standalone task/results analysis.
- `python -m showAndTell.tci_charts --metrics EXPORT --output-dir OUT`: rerender frozen TCI inputs.
- `python scripts/update_site_results.py INPUT --snapshot-date YYYY-MM-DD`: lower-level result export only. Prefer the wrapper for a publication refresh because it synchronizes the other assets and numeric page text.

### Suggested instruction to Claude

> Read docs/RESEARCH_FIGURES.md. Refresh the research page from [exact results path and snapshot date], using [task snapshot path and revision, if changed]. Preserve the declared scoring rules and Brackett palette. Use the refresh utility, inspect the figures, and check the captions against the source. Keep TCI as an optional heuristic for selecting a spread of tasks. Show the changed populations and any unmapped cases before making claims about task difficulty. Follow my instructions about committing and publishing.
