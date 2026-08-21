# Task Inspector

A self-contained web UI for browsing the ShowAndTell-Bench tasks — the published
dataset (read-only, from the Hugging Face cache) merged with any local
`tasks/` checkout, local winning on a name collision — each task's demonstration + narration, quiz questions, and
per-product results — plus an overview matrix of how each product scored on each task.

## Use it

    ./showAndTell setup              # first run only (and after dependency changes)
    ./showAndTell viewer             # live viewer at http://localhost:8000
    ./showAndTell dataset-pull       # refresh to the latest published dataset

The live viewer is rebuilt from `tasks/` + `runs/.cache/` on every refresh,
with no regenerate step (bound to localhost; opens your browser automatically):

    ./showAndTell                    # also starts the viewer on port 8000
    ./showAndTell viewer 9001        # custom port

Run `./showAndTell setup` once first. All commands above are run from the repository
root. Direct `python -m showAndTell.viewer.serve` or
`uv run python -m showAndTell.viewer.serve` commands remain available for
developers using an activated or managed environment.
Browsing tasks does not require Docker. The **Run** and **New task** controls do;
`./showAndTell setup` checks for the Docker CLI, Compose v2, and a running daemon
and prints platform-specific installation guidance without installing Docker.
`./showAndTell setup` creates one empty managed Chrome profile named `showAndTell`
without copying any personal browser data. A **Run in** control also creates it
automatically if it
is missing. Interactive setup opens Brackett's official Chrome Web Store listing
in that profile; Chrome requires the user to approve **Add to Chrome** once.
`./showAndTell browser launch` and **Run with Brackett** also open that listing when
the extension is absent. Once installed, the browser command opens Brackett for
sign-in. A task run that reaches the sign-in screen pauses for up to five minutes
after clicking **Sign in** automatically. A saved session continues without user
input; otherwise the run resumes when the user completes authentication.

To generate a shareable static snapshot instead of serving live:

    .venv/bin/python -m showAndTell.viewer.generate

Then open `src/showAndTell/viewer/index.html` in a browser (double-click, or
`open src/showAndTell/viewer/index.html`). The data is embedded in the file, so it
needs no server. Re-run the generator whenever tasks change or new teach runs land. Malformed
task files are skipped with a warning on stderr rather than aborting the build —
with one exception: a task missing its `[complexity]` block in `task.toml`
fails the build by design (see `docs/COMPLEXITY.md`).

Use `showAndTell.viewer.generate` when you want a shareable snapshot file; use
`showAndTell.viewer.serve`
while iterating on tasks or watching new runs land. In local serve mode the
Quiz tab also has an editor that validates and writes
`tasks/<task>/quiz/questions.json` or a captured draft's
`task-drafts/<task>/quiz/questions.json`; saves preserve top-level metadata and
use a file hash to refuse overwriting concurrent changes. A newly completed
capture links directly to this editor. Tasks without questions show a warning
and require confirmation before a product run starts. The write API is
available only on the loopback-bound development server, not snapshots or
`showAndTell.viewer.serve_public`.

The live sidebar also includes **Settings** for fixture-host and judge setup.
Explicit environment variables remain highest priority and appear locked in
the page. Local preferences, fixture tokens, and judge API keys are saved to
the repository's gitignored `.env` with owner-only permissions. Secrets remain
masked and are never embedded in the generated page or returned by the
settings API. **Test current host** checks agent health and application discovery;
**Test current grader** runs the same authenticated model smoke check as
`showAndTell judge doctor`.

On a browser's first visit to the local live viewer, a five-step **Getting
started** tour points to the task library, a task's run controls, **New task**,
application selection, and **Start clean apps** in turn. It explains that the
selected apps are clean, isolated copies used by the recorded workflow. The
tour can be skipped, stays dismissed in that browser, and can be replayed from
the header's help button. Finishing, skipping, or closing the tour returns to
the Overview. Static snapshots and the public read-only viewer do not show the
guide.

Each task header in local serve mode also has **Run with Brackett**, **Run with
Codex**, and **Run with Claude** buttons. A button starts the corresponding
adapter as a fresh recorded run, shows its live status and recent output, then
refreshes the viewer when the result is ready. The task must have
`demo/seed.json`; one run is allowed at a time because the adapters share the
local browser and recording devices. These controls are intentionally absent
from generated snapshots and the public viewer.

Local serve mode also enables **New task** in the header. It starts the selected
applications in a clean isolated/reset fixture and opens them in managed Chrome.
Applications with accounts are signed in automatically before setup begins.
The setup screen also shows local-only username/password cards with copy buttons
for manual fallback; public applications are explicitly marked as requiring no
login. Successful sign-ins are included in generated replay so clean trials start
with the same authenticated state.
Selecting **ONLYOFFICE spreadsheet** creates a blank connector-owned `.xlsx`
workbook and opens the real editor—not the Document Server installation page.
Users can add worksheets and seed cells normally; clean trials recreate the
blank workbook before replaying those saved setup actions.
The first stage is deliberately **not recorded**: the operator can create any
starting data the workflow needs. Clicking **Start recording** exports that
state into the draft's `demo/seed.json`, then begins action, screen, default-
microphone, and live speech-to-text capture together. Setup gestures are also
saved to `demo/seed_events.jsonl` and compiled into replay as a fallback for
applications whose fixture cannot export structured state.

During the recording stage every click, fill, selection, and Enter/Escape press
carries ranked unique selectors, its page/frame URL, target role/name, a post-
action screenshot, and Chromium's full accessibility tree. **Discard recording**
abandons the take after confirmation — the applications are released and no
draft is written; the dialog's close control does the same rather than being
inert while a capture is live. Stopping instead saves
`task-drafts/<name>/` with `demonstrate.py`, a task scaffold, `events.jsonl`, a
bundle-shaped `capture_bundle/`, narration, recording, and structured
`testcase.json`. Password values are never written into the action trace;
generated replay reads them from `creds`.

The generated `demonstrate.py` is immediately replayable and tries captured
selectors in stability order. The completion screen and captured-task page both
offer **Add to tasks**, which moves the complete capture into `tasks/<name>/` as
an ordinary runnable test. The viewer owns application startup and cleanup; required
Docker images/application dependencies must be installed. The managed Chrome
profile is created automatically; product extensions and sign-ins still need
to be initialized once with `showAndTell browser launch`.

A replay plays the operator's **own recorded narration** rather than a
synthesized voice, and runs at the demonstration's own speed: the recording's
audio track plays straight through while each recorded action waits for its
captured `at_ms`, so one clock drives both and the voice stays on the action it
describes. Drafts with no recording fall back to reading the narration script
with TTS; drafts generated before pacing existed are re-rendered in memory from
their captured events.

The completion screen links to the captured task. Its **Run with Brackett**,
**Run with Codex**, and **Run with Claude** controls use the same run endpoint, status
panel, readiness gates, narration toggle, replacement flow, and cancellation
as ordinary tasks. The runner launches managed Chrome, starts the applications,
automatically restores the saved seed/setup stage, then starts the product's
recorder and executes only the recorded task actions with synchronized
narration. Claude's shortcut, Brackett's agent handoff, or Codex's Record
& Replay result is retained under `task-drafts/<name>/trials/`. Every completed
run then uses the same COMPREHEND quiz as an ordinary benchmark run, and its
score, answers, history, and saved artifacts appear in the draft's **Results**
tab. The full run is also screen-recorded to `screen.mov` (macOS) or
`screen.mkv` (Linux) and can be played from its result card. Older
artifact-only trials remain visible there as **not graded**, but cannot gain a
trial video retroactively.
Browser-only legacy captures have no generated
replay, so a run is refused with a note to re-record them in managed
mode. The generated driver can subsequently run through the
normal adapters after the draft has deterministic fixture seed data and is
promoted into `tasks/`.

## What it shows

- **Overview** — stat tiles (tasks, questions, per-product mean
  score) and a **task × product results matrix** (score + meter, banded
  ≥ 0.80 / 0.50–0.79 / < 0.50; · = not run). Click a product column header to
  sort by that product's score (desc → asc → grouped); the dot beside a score
  marks the best product on that task. Click a row to open the task.
  A **TCI column** (Task Complexity Index, tier-banded 1–5, sortable; hover
  for the six-dimension breakdown) puts each score next to how hard the task
  is — see `docs/COMPLEXITY.md` for the parameters and formula. Each task
  page repeats the TCI as a pill in its header with a per-dimension strip.
- **Task detail**, three tabs:
  - **Demonstration** — the task's `demo/recording` video (`.webm`, `.mp4`, `.mov`, or
    `.mkv`) when it has one, followed by narration
    steps, with `demonstrate.py` and `task_logic.py` as collapsible source.
  - **Quiz** — each question with its accepted answers (closed) or rubric.
    Local serve mode can add, remove,
    reorder, and edit multiple-choice, closed, and rubric/LLM-judged questions,
    including their evidence arrays.
  - **Results** — per-product score cards, then a per-question comparison
    table with one verdict column per product; click a row to see the
    expected answer and every product's actual answer side by side.
    A `▶ recording` button on a product's score card plays the screen
    recording of the run that produced that result (inline `<video>`;
    snapshots reference the file under `runs/` rather than embedding it, so
    the player appears only where that file exists).

## Getting around

- `/` focuses the sidebar filter · `j`/`k` next/previous task · `1–3` switch
  tabs · `⌘K`/`Ctrl+K` fuzzy quick-open palette · `Esc` closes/blurs.
- Light/dark follows the OS; the header button overrides it (persisted).
- Deep links: `#/task/<name>/<tab>`, `#/executions`, and `#/settings`.

## Files

- `serve.py` — live mode: a stdlib HTTP server that runs the same scan +
  assembly on every request.
- `settings.py` — layered local preferences, secret redaction, and host/judge
  connection checks for live mode.
- `task_runs.py` — validates and serializes local Claude, Brackett, and Codex
  subprocesses launched from task pages; active runs can be cancelled directly
  from their task status panel.
- `generate.py` — scans the repo and assembles `index.html` from `src/`:
  stylesheets and scripts are inlined in `CSS_MANIFEST`/`JS_MANIFEST` order,
  and the data is embedded as a JSON `<script>` island.
- `src/` — the viewer's source: `skeleton.html` (page shell),
  `styles/*.css` (tokens → base → components → views), and plain-JS modules
  (`html.js` auto-escaping templates, `state.js`, `router.js`,
  `components.js`, `views/*.js`, `palette.js`, `keyboard.js`, `app.js`).
  Files share one `<script type="module">` scope — no import/export; each
  file's header comment names what it defines and uses.
- `index.html` — generated output (gitignored; it embeds a `runs/` snapshot).

Tests: `tests/test_viewer.py` (fast — scanning, warnings, assembly) plus the
other `tests/test_viewer_*.py` suites.
