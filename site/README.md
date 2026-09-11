# AEI research website

This portable static publication makes AEI the main subject, with Show & Tell as an initial benchmark application. It deliberately distinguishes comprehension-after-Teach results from the full B/O/L index.

## Preview and deploy

Serve `site/` with any static web server. All local URLs are relative, so the site works at both a generated private Pages root and the eventual public `/showAndTell-arena/` path.

The `AEI research site` workflow uploads **only `site/`**. It runs on changes to `main` or the initial `aei-research-site` preview branch. The GitHub Pages site is configured private. Repository visibility alone does not protect a Pages site; keep the Pages `public` setting false until launch is explicitly approved. Preview also uses a noindex meta tag and robots exclusion, which are not access controls.

For a public launch, explicitly change Pages visibility, remove the noindex meta tag and robots exclusion, and update the corresponding preview-only check. After this branch is merged, remove the preview-branch trigger and deployment branch allowance.

## Sources

- The header links “By Brackett” to `https://brackett.ai`. `downloads/ShowTellArena_Paper_Draft.pdf` is an unchanged copy of the older manuscript at `~/repos/showtellarena/main.pdf`, labeled draft with arXiv forthcoming; it is not the authority for the current results. Replace its link with the verified arXiv abstract URL once published.

- `downloads/AEI_Methodology_and_Build_Specification_v0.2.docx` is an unchanged copy of the latest user-approved teaching-profiles specification. It is the source of the B/O/L definitions, weights, learning panel, and confidence-interval description.
- `data/results.json` and `data/results.csv` freeze the provided workbook using the repository's existing `benchmark_charts.py` semantics. JSON includes the source filename, SHA-256, snapshot date, attempt records, and grading limitations. No full AEI score is reported.
- `data/example-source.json` preserves the three selected questions and evidence references from Hugging Face revision `f4aa4251d0bbefb80d10ea9074a073c2b036c2af`. This revised public quiz is a design example, not asserted to be the precise quiz version behind the workbook results.
- The three-panel case figure embeds original demonstration frames 47, 90, and 144 from the same pinned dataset revision. Captions describe the human demonstrator’s prepared decisions, not agent outcomes. Each image links to its full-resolution original.
- Recording and poster load directly from that pinned Hugging Face revision. They are fetched by the visitor's browser; the recording is not preloaded. The public narration is available through the task source link. The video does not ship with verified captions.
- `assets/run-a-task.gif` and `assets/create-a-task.gif` come unchanged from `src/showAndTell/assets/`. `assets/brackett-mark.svg` is the Brackett UI mark supplied from the local Brackett repository. Companion chart hues match the existing distinct chart palette.

The page uses DM Sans / IBM Plex Mono from Google Fonts with system fallbacks. There are no analytics, cookies, credentials, or client-side API keys.

## Refreshing the workbook snapshot

**Preferred:** follow [the coordinated refresh guide](../docs/RESEARCH_FIGURES.md) and run `python scripts/refresh_research_figures.py`. The commands below are lower-level utilities.

From the repository root, use:

```sh
python scripts/update_site_results.py path/to/results.xlsx --snapshot-date YYYY-MM-DD
```

The generated CSV can also be used as input. The script reuses the benchmark chart loader and summary logic. It never edits the source workbook. Review grading and coverage notes when refreshing the data. Update the saved shared-case chart and dated/count prose in `index.html` to match; this preserves an accurate initial render even without JavaScript. `scripts/check_site.py` rejects a stale saved chart. Interactive views always read the frozen JSON.

Before committing:

```sh
python scripts/check_site.py
node --check site/app.js
```

The data check independently recomputes averages from the CSV, reconciles shared-case coverage and completed attempts, verifies the static chart against the data, checks local links and IDs, and compares the displayed questions with the frozen dataset source. No browser automation is part of this check.

## Additional research figures

Install the existing `charts` optional dependencies. The reusable Python generators use Matplotlib and the Brackett publication palette, exporting SVG, PNG, and machine-readable data:

```sh
PYTHONPATH=src python -m showAndTell.benchmark_analysis path/to/results.xlsx --output-dir site/assets/analysis --task-quiz site/data/task-questions.json
PYTHONPATH=src python -m showAndTell.tci_charts --metrics site/assets/analysis/tci.json --output-dir site/assets/analysis
```

The installed entry points are `showAndTell-analysis` and `showAndTell-tci-charts`. `benchmark_analysis` also accepts its exported `attempts.csv`; blank-score rows preserve untested cases. The original workbook is never modified. Refresh the overview snapshot separately with `update_site_results.py`, then reconcile counts and captions in the page.

- Task inventory: editorial grouping of all 42 workbook case names; 30 tested and 12 untested. Named variants remain separate. The grouping is not a difficulty measurement.
- Heatmap: equal-weight case means, identical color scale for every system. Numeric zero is scored; NA is incomplete and contributes zero; blank is untested and stays absent.
- Attempt outcomes: 144 selected attempts. Failure labels come from the workbook's Detailed Analysis notes, aligned and checked against Overview. They are observations from selected runs, not independently verified failure rates.
- Repeat spread: the three largest absolute two-attempt gaps per repeated system. Selection is intentional and descriptive; it is neither a learning measure nor an uncertainty interval.
- Evidence matrix: all 11 questions from the pinned public returns quiz (`data/task-questions.json`). A question can cite several evidence types. This revised quiz is separate from the older scored runs.
- TCI: an optional heuristic for choosing a spread of arena tasks across expected difficulty levels. The 49 task definitions are from the retained Show & Tell source archive at revision `6e48f4d70b870d2a05c9f1ff15d07db7a678d2ee` (26 July 2026), including variants. They are **not matched to the 42 workbook cases**. No performance-versus-TCI relationship is claimed. The export records per-file source hashes, scorer hash, inputs, components, and tier assignments. Re-render from the committed export; to recompute from definitions, use `--tasks-root path/to/tasks --source-revision REV --label LABEL`. The generator reads/parses source files; it does not execute task logic.

The site check also reconciles the analysis with the overview snapshot, attempt outcomes, question evidence, and every TCI value and bucket from its exported inputs. Chart SVGs retain selectable text and PNGs are available for reuse.
