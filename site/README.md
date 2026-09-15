# AEI research website

This portable static publication makes AEI the main subject, with Show & Tell as an initial benchmark application. It deliberately distinguishes comprehension-after-Teach results from the full B/O/L index.

## Preview and deploy

Serve `site/` with any static web server. All local URLs are relative, so the site works at both a generated private Pages root and the eventual public `/showAndTell-arena/` path.

The `AEI research site` workflow uploads **only `site/`**. It runs on publication changes to `main`. The GitHub Pages site is configured private. Repository visibility alone does not protect a Pages site; keep the Pages `public` setting false until launch is explicitly approved. Preview also uses a noindex meta tag and robots exclusion, which are not access controls.

For a public launch, explicitly change Pages visibility, remove the noindex meta tag and robots exclusion, and update the corresponding preview-only check.

## Sources

- The header links “By Brackett” to `https://brackett.ai`. `downloads/ShowTellArena_Paper_Draft.pdf` is the manuscript draft built on 14 September 2026 with model-provenance updates. Its SHA-256 is `e4427458ce51fa23d4e44bfea871429187c812c7201753ca1548c3a3902c6792`. It retains the same 12 September evaluation snapshot as `data/results.json` and is labeled draft with arXiv forthcoming. The frozen JSON, not the PDF, remains the authority for the page's numbers. Replace its link with the verified arXiv abstract URL once published.

- `data/results.json` and `data/results.csv` freeze the provided workbook using the repository's existing `benchmark_charts.py` semantics. JSON includes the source filename, SHA-256, snapshot date, attempt records, and grading limitations. The original workbook is private and is not distributed; published references use its filename and hash only. Keep private source paths and repository links out of this site. No full AEI score is reported. Grading is non-canonical and mixed: the workbook notes record manual grades for the first two Brackett attempts and the Claude attempts, and imported grades produced by Claude Sonnet 4.6 for the third Brackett attempt set and the nine cases tested only by Brackett. Detailed Analysis rows labelled “Earlier N” are excluded from the overview by the workbook; `assets/analysis/analysis.json` lists them under `excluded_attempts` and they never enter a score.
- `data/example-source.json` preserves the three selected questions and evidence references from Hugging Face revision `f4aa4251d0bbefb80d10ea9074a073c2b036c2af`. This revised public quiz is a design example, not asserted to be the precise quiz version behind the workbook results.
- The three-panel case figure embeds original demonstration frames 47, 90, and 144 from the same pinned dataset revision. Captions describe the human demonstrator’s prepared decisions, not agent outcomes. Each image links to its full-resolution original.
- `assets/returns-demo.mp4` is a publication crop of the recording from that pinned dataset revision. It removes 260 pixels from the top and 80 pixels from the bottom of the 4096 × 2648 source to exclude browser and desktop chrome, including address and link-hover bars, then scales to 1920 pixels wide. The first two seconds hold the clean frame at 2 seconds; from 510 seconds to the end, the last clean application frame is held. Audio in those boundary intervals is muted to omit desktop transitions. The intervening demonstration and evidence timestamps are preserved. The poster remains pinned to the dataset. The recording is not preloaded and does not ship with verified captions. Its SHA-256 is `e4427458ce51fa23d4e44bfea871429187c812c7201753ca1548c3a3902c6792`.
- `assets/create-a-task.gif` comes unchanged from `src/showAndTell/assets/`. `assets/brackett-mark.svg` is the Brackett UI mark supplied from the local Brackett repository. Companion chart hues match the existing distinct chart palette.

The page uses DM Sans / IBM Plex Mono from Google Fonts with system fallbacks. There are no analytics, cookies, credentials, or client-side API keys.

## Refreshing the workbook snapshot

The model-metadata refresh on 14 September 2026 preserves all scores and the 12 September evaluation snapshot date. Brackett uses the author-specified public description `Fused/multiple models` for all 107 selected attempts; component model names from the raw source are not published. The workbook's second sheet, `Detailed Analysis`, labels 60 Claude attempts `Sonnet 5` and 24 Codex attempts `Not recorded`. The latter labels are workbook-reported, not verified model IDs. Claude Sonnet 4.6 in grading notes is the grader. Publication model labels now survive both CSV exports and appear per attempt and by selected count in the JSON; excluded attempts retain separate provenance with the same public labeling. The page shows these labels beside the chart without changing the product-level aggregates.

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

The site check also rejects internal build specifications, internal run identifiers, bundled spreadsheet workbooks, and private paper-source references in the site's text assets before deployment.

## Additional research figures

Install the existing `charts` optional dependencies. The reusable Python generators use Matplotlib and the Brackett publication palette, exporting SVG, PNG, and machine-readable data:

```sh
PYTHONPATH=src python -m showAndTell.benchmark_analysis path/to/results.xlsx --output-dir site/assets/analysis --task-quiz site/data/task-questions.json
PYTHONPATH=src python -m showAndTell.tci_charts --metrics site/assets/analysis/tci.json --output-dir site/assets/analysis
```

The installed entry points are `showAndTell-analysis` and `showAndTell-tci-charts`. `benchmark_analysis` also accepts its exported `attempts.csv`; blank-score rows preserve untested cases. The original workbook is never modified. Refresh the overview snapshot separately with `update_site_results.py`, then reconcile counts and captions in the page.

- Task inventory: editorial grouping of all 42 workbook case names; 39 tested and 3 untested. Named variants remain separate. The grouping is not a difficulty measurement.
- Heatmap: equal-weight case means, identical color scale for every system. Numeric zero is scored; NA is incomplete and contributes zero; blank is untested and stays absent.
- Attempt outcomes: 191 selected attempts. Failure labels come from the workbook's Detailed Analysis notes, aligned and checked against Overview. They are observations from selected runs, not independently verified failure rates.
- Repeat spread: the three largest ranges (max − min) between selected attempts of one case, per repeated system. Brackett cases have two or three selected attempts and Claude cases two. Selection is intentional and descriptive; it is neither a learning measure nor an uncertainty interval.
- Evidence matrix: all 11 questions from the pinned public returns quiz (`data/task-questions.json`). A question can cite several evidence types. This revised quiz is separate from the older scored runs.
- TCI: an optional heuristic for choosing a spread of arena tasks across expected difficulty levels. The 49 task definitions are from the retained Show & Tell source archive at revision `6e48f4d70b870d2a05c9f1ff15d07db7a678d2ee` (26 July 2026), including variants. They are **not matched to the 42 workbook cases**. No performance-versus-TCI relationship is claimed. The export records per-file source hashes, scorer hash, inputs, components, and tier assignments. Re-render from the committed export; to recompute from definitions, use `--tasks-root path/to/tasks --source-revision REV --label LABEL`. The generator reads/parses source files; it does not execute task logic.

The site check also reconciles the analysis with the overview snapshot, attempt outcomes, question evidence, and every TCI value and bucket from its exported inputs. Chart SVGs retain selectable text and PNGs are available for reuse.
