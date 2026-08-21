# Live adapters — running a real product through a teach

`docs/DESIGN.md` covers *what* the benchmark measures (teach a product by
demonstration, then quiz it). This doc covers
the **live UI-driven adapters** — the ones that drive a real product's own
teach-by-demonstration flow in a real browser/app, narrate the demo aloud, and
then quiz the product on what it learned.

There are three:

| Adapter | Command | What it drives | Hands-free? |
|---|---|---|---|
| **claude-teach** | `showAndTell claude-teach` | Claude-in-Chrome side panel → **Teach Claude** recording | fully hands-free |
| **brackett-teach** | `showAndTell brackett-teach` | Brackett web app → **Show and Tell** | fully hands-free |
| **codex-record** | `showAndTell codex-record` | Codex desktop app → **Record & Replay** | fully hands-free |

All three live adapters are **task-agnostic**: they know nothing task-specific.
A task supplies its UI actions via `tasks/<name>/demonstrate.py` and its data via
its own `demo/seed.json`, so adding a task makes it work on every adapter and adding
an adapter makes it work on every task.

---

## Readiness gates

Prerequisites the harness cannot create — a signed-in account, an installed
extension, an enabled plugin — pause the run at a **readiness gate** instead
of failing mid-launch: the run states the problem and the exact remedy, then
waits. In a terminal, Enter re-checks and `q` cancels; a viewer-launched run
shows the instructions with **Continue**/**Cancel** buttons on its run panel.
Prerequisites the harness can observe directly (a sign-in completing in the
driven browser, an extension appearing in the profile) resolve the pause
automatically. A continue is never trusted: the gate re-checks before
proceeding.

Per product: **claude-teach** gates on the Claude extension being installed
and signed in; **brackett-teach** on the Brackett extension and workspace
sign-in; **codex-record** on the ChatGPT app being installed, signed in, and
the Record & Replay plugin installed and enabled.

## The shared shape

Every live adapter runs the same loop, differing only in which product it drives:

1. Launch the product (managed Chrome, or the Codex app) and start the task's
   fixture applications with its demo data.
2. Log into the fixture applications.
3. Start the product's *own* teach/record flow.
4. **Perform the demonstration** on the fixture applications — the task's `demonstrate.py` drives
   the clicks, narrating each step aloud so the product captures spoken reasoning
   in sync with the actions.
5. Stop/finish the recording and let the product process it.
6. **Quiz** — ask the product the task's fixed question set about the workflow it just learned
   and grade the answers (closed-form exact-match + rubric-judged free text).
   The headline score is the mean over all questions.

The quiz result prints to the terminal and is saved to the run directory.

In code, the loop lives once, in `src/showAndTell/teacher/run.py`: a run is built
up front as an ordered plan of numbered steps (launch → seed → record →
demonstrate → digest → quiz), and each product contributes a `ProductAdapter`
from its own module (`students/claude.py`, `students/brackett.py`,
`students/codex.py`) supplying the product-specific steps and chat transport. The
same loop also replays viewer-captured drafts (`run_trial` /
`run_capture_trial`), with the draft-side machinery in `player/trial.py`.

---

## One-time setup

```bash
uv sync && uv run playwright install chromium
```

### Rubric judge

Live adapters preflight the rubric judge before an uncached product run and
warn when it is unavailable; the run proceeds and its result is saved
ungraded — never masquerading as a zero score — to be graded later with
`showAndTell grade` or from the viewer's Results. The default backend is
`automatic`: the first available of `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
`GEMINI_API_KEY`, a `claude` CLI, or a `codex` CLI — only the Anthropic
pairing is canonical. To grade official results, use the Anthropic API:

```bash
export ANTHROPIC_API_KEY=your_key
showAndTell judge doctor
```

For local, unverified research, OpenAI and Gemini APIs are also supported:

```bash
export OPENAI_API_KEY=your_key
export SHOWANDTELL_JUDGE_BACKEND=openai-api
export SHOWANDTELL_JUDGE_MODEL=gpt-5.6-terra
showAndTell judge doctor

export GEMINI_API_KEY=your_key
export SHOWANDTELL_JUDGE_BACKEND=gemini-api
export SHOWANDTELL_JUDGE_MODEL=gemini-2.5-pro
showAndTell judge doctor
```

Authenticated Claude Code and Codex installations can be used without API
keys:

```bash
export SHOWANDTELL_JUDGE_BACKEND=claude-cli
export SHOWANDTELL_JUDGE_MODEL=claude-sonnet-4-6
showAndTell judge doctor

export SHOWANDTELL_JUDGE_BACKEND=codex-cli
export SHOWANDTELL_JUDGE_MODEL=gpt-5.6-terra
showAndTell judge doctor
```

The Codex CLI backend is experimental because it cannot preserve the API
backends' privileged system-message boundary.

The override, model identity, prompt/config fingerprints, usage, applicable
cost estimate, retries, and raw judgment are stored in `comprehend.json`.
Provider/model overrides report cost as unavailable rather than applying the
release judge's prices. Judge failures leave the result incomplete and
uncached. See the root README's “Run a benchmark” section for
standalone grading and re-grading commands.

### Managed Chrome profile (claude-teach, brackett-teach)

The browser adapters drive a **dedicated** Chrome profile at
`~/.showAndTell/chrome-profile`, never your daily one. Create it once:

```bash
showAndTell browser setup                 # clones your logged-in "Default" profile
#   or: showAndTell browser setup --fresh  # empty profile; you log into sites manually
```

Then make sure the profile is signed in to what each adapter needs:

- **claude-teach** needs the **Claude** Chrome extension installed and signed in
  in that profile (the cloned profile inherits it if your daily profile has it).
- **brackett-teach** needs to be signed in to the Brackett web app.

To sign in manually: `showAndTell browser launch --url <site>`, log in, close it.

### Voice narration (macOS)

Narration is spoken during the demo so the product records it. All products use
the same virtual cable:

```bash
brew install switchaudio-osx
brew install --cask vb-cable
```

`./showAndTell setup` installs both tools from the repository `Brewfile`. The
VB-CABLE package may ask once for an administrator password, but ShowAndTell
selects its input/output and restores the user's devices automatically. No
Audio MIDI setup is required. Automated adapters refuse to start when the
narration route is unavailable, so a silent run cannot be mistaken for a valid
benchmark result. In the viewer, **Hear narration** optionally mirrors the same
audio to the Mac speakers; it is silent by default. If speaker narration is on,
it can be turned on or off immediately during a run without interrupting the
virtual microphone feed.

### Brackett (brackett-teach)

```bash
export SHOWANDTELL_BRACKETT_URL=https://brackett.prod-002.app.brackett.ai/   # default
./showAndTell browser launch  # opens the Chrome Web Store when Brackett is missing
```

`SHOWANDTELL_BRACKETT_URL` points at whichever cluster you're testing.
Install Brackett from the Chrome Web Store in the managed Chrome profile. A
task run pauses at an installation gate and opens the store in its worker
profile when that worker does not have the extension yet.

### Codex (codex-record)

Install the OpenAI desktop app in `/Applications` — **ChatGPT.app** (recent
builds; formerly **Codex.app**, same app, bundle id `com.openai.codex`). The
adapter finds either and launches it with a CDP port, then signs into the
fixture (via CDP) before recording. It must be fully quit first — an
already-running instance ignores the debugging-port flag.

Codex needs the **Record & Replay plugin** installed and enabled for the
signed-in account (the app's Settings → Plugins; it records the demo and turns
it into a Skill). Right after launching the app the adapter verifies sign-in
and the plugin, pausing at a readiness gate when something is missing; if the
settings layout cannot be read it continues, and the recording-start wait
remains the backstop.

Codex drives real OS input, which needs macOS **Accessibility** permission.
The first run pops the system dialog and pre-registers the Python runtime in
System Settings → Privacy & Security → Accessibility; enable it and retry
(re-grant once after recreating `.venv` on a different Python version).

### Screen recording of runs

Every fresh teach run screen-records itself — whole screen, plus the default
audio input so the narration is audible — and files the video with the run
(`--no-record` to skip). Cached results never record.

- **macOS**: uses the built-in `screencapture`. The first run pops the
  one-time Screen Recording permission request and pre-registers your terminal
  (System Settings → Privacy & Security → Screen Recording); grant it, quit
  and reopen the terminal, and retry — the run refuses to record a silently
  useless video without it. Clicks are visualized in the video.
- **Linux**: needs `ffmpeg` (X11; `xdpyinfo` recommended for full-resolution
  capture) or `wf-recorder` (Wayland). Note codex-record itself is
  macOS-only, so on Linux this applies to the browser adapters.
- If no recorder is available the command currently proceeds after one warning;
  that run is invalid for benchmark collection and must be stopped/rejected.
- The default audio input is the **microphone**, not a narration-only feed —
  ambient room audio is recorded too, not just the demo narration.
- Orphan protection differs per backend. **macOS**: the recorder's life is
  tied to the run via its controlling pty — if the run dies without stopping
  it (even `kill -9`), the recorder is hung up within moments (that
  recording is lost; no duration cap — `screencapture -V` would break its
  graceful stop). **X11**: ffmpeg auto-stops after a 2-hour cap. **Wayland**:
  neither — a hard-killed run leaves `wf-recorder` running until killed
  manually. An interrupted run's file may be left at `runs/.recording-*`.

The **whole screen** is captured: don't leave sensitive windows visible while
a run is going.

---

## Running each adapter

```bash
showAndTell claude-teach   --task customer-returns-inbox-triage
showAndTell brackett-teach --task customer-returns-inbox-triage
showAndTell codex-record   --task customer-returns-inbox-triage
```

Application URLs and host-control ports come from application manifests and
the leased host assignment. `claude-teach` and `brackett-teach` share
`--cdp-port` (default 9223);
codex-record uses its own (default 9333). claude-teach also takes
`--timeout-minutes` and `--manual` (you perform the demo by hand instead of the
driver). Run unbuffered (`PYTHONUNBUFFERED=1`) if you're tailing the log — Python
block-buffers stdout to a file otherwise.

### Parallel Linux/VM runs (recording and voice are required)

Use one numbered worker and one X display per run. A worker isolates the Chrome
profile, browser-control ports, and PulseAudio narration route. `--no-cache`
forces a real demonstration instead of returning an older result.

Recording and voice are mandatory for benchmark runs:

- Never pass `--no-record`; recording is enabled by omitting that flag.
- Never accept the silent fallback. Linux requires both `ffmpeg` and `pactl`,
  and every worker log must show its isolated Pulse route before the demo.
- Run only tasks that have a non-empty `demo/narration_script.jsonl`.

Preflight the VM once:

```bash
command -v ffmpeg
command -v pactl
pactl info >/dev/null
test -s tasks/customer-returns-inbox-triage/demo/narration_script.jsonl
```

The `test -s` line applies to a local `tasks/` checkout; dataset-fetched
bundles live in the Hugging Face cache and always ship their narration script.

Example four-worker pool (replace these with cases that have not already
completed):

```bash
cd <your checkout>

PYTHONUNBUFFERED=1 nohup uv run showAndTell claude-teach \
  --task customer-returns-inbox-triage --worker 1 --display :21 --no-cache \
  >/tmp/showAndTell-worker-1.log 2>&1 &

PYTHONUNBUFFERED=1 nohup uv run showAndTell claude-teach \
  --task returns-inbox-sweep --worker 2 --display :22 --no-cache \
  >/tmp/showAndTell-worker-2.log 2>&1 &

PYTHONUNBUFFERED=1 nohup uv run showAndTell claude-teach \
  --task credit-release-queue --worker 3 --display :23 --no-cache \
  >/tmp/showAndTell-worker-3.log 2>&1 &

PYTHONUNBUFFERED=1 nohup uv run showAndTell claude-teach \
  --task rfq-quote-award --worker 4 --display :24 --no-cache \
  >/tmp/showAndTell-worker-4.log 2>&1 &
```

The same shape works for `brackett-teach`. Do not reuse a worker number or
display until its previous process exits.

Compatibility rules:

- Application replicas can run together because leases assign distinct host
  instances; worker numbers isolate browser-control ports and profiles.
- Do not concurrently run two mutating tasks against the same Docker/WebArena
  site (`gitlab`, `magento`, `magento_admin`, or `postmill`). They share the site's
  canonical container and can overwrite each other's state.
- Different Docker/WebArena sites may run together. Read-only tasks may share a
  site only when neither task resets or seeds it.
- Do not run tasks marked `status.state = "blocked"` merely to fill a slot.
- Check run history before scheduling. Rerun a completed task only to verify a
  specific fix; otherwise select a case that has never completed.

Verify voice and recording while the pool runs:

```bash
grep -H "worker .* pulse route\|narration uses isolated pulse route" \
  /tmp/showAndTell-worker-*.log
ps -axo pid,command | grep '[f]fmpeg.*\.recording-'
```

A successful run must finish with a score and a finalized
`runs/<timestamp>-<adapter>-<task>/screen.mkv`. A failed run must retain
`runs/<timestamp>-<adapter>-<task>-FAILED-screen.mkv`. A live run temporarily
uses `runs/.recording-*.mkv`; do not delete it or reuse that worker while its
process is active.

### claude-teach — how the panel opens itself

A **Teach Claude** recording needs the Claude-managed tab group a toolbar-icon
click creates (that group is what Teach records into). The adapter produces the
same state with no human (`open_claude_panel`): it dispatches the extension's
own `action.onClicked` listener in its service worker — the real click handler
runs and creates the group — then calls `chrome.sidePanel.open` from the
extension's options page, a page context where CDP's `userGesture` flag is
honored (the service worker refuses it). If any stage fails (say an extension
update changes the handler), the run fails immediately with an error naming the
stage — a run is fully unattended or it fails.

### brackett-teach / codex-record

Fully hands-free — the record controls are web buttons / in-app prompts the
adapter drives directly. codex-record auto-approves the app's "Allow Codex to
record" and file-write permission prompts as they appear.

---

## How capture differs (the load-bearing detail)

The three products capture a demonstration in fundamentally different ways, which
is why each adapter performs the demo differently:

| Product | Captures | Demo must be… |
|---|---|---|
| **Claude Teach** | browser **DOM events** in its tab group | driven in the page (synthetic CDP input works) |
| **Brackett Show-and-Tell** | browser **DOM events** via the extension | driven in the page (synthetic input works) |
| **Codex Record & Replay** | real **OS mouse/keyboard** via screen recording | driven with real OS input on the frontmost window |

Because Codex records the *screen and real input*, CDP/JS clicks are invisible to
it — the adapter resolves fixture-app elements to screen coordinates (Playwright
locators through an OS-page proxy) and issues real `CGEvent` clicks at those
coordinates, keeping the fixture-app window frontmost.

### Voice

**claude-teach**, **brackett-teach**, and **codex-record** all capture narration
from the **VB-CABLE** virtual microphone. The viewer's **Hear narration** toggle
mirrors that same audio to the physical speakers without changing the input
seen by the recording product. During a run it can be turned on or off at any
time.

---

## The quiz — how each product is asked

The quiz is shared (`comprehend.py`): each task's fixed question set, graded
closed-form + rubric. How the product *answers* differs:

- **brackett-teach / codex-record**: the recording is analyzed in the product's
  own chat/session, which retains the context — so the adapter just asks the
  questions in that chat and reads the reply.

- **claude-teach**: a Teach recording leaves **no memory in the chat thread** —
  the only artifact of what Claude learned is the **shortcut** it auto-generates,
  whose Prompt field is its written understanding of the demo. So the adapter:
  1. waits for Claude to finish generating the shortcut,
  2. **captures** that Prompt (Claude's understanding) from the Create-shortcut
     modal and saves the shortcut under a unique name (Claude silently refuses a
     duplicate name),
  3. quizzes Claude with that captured understanding supplied as context.

  Note: *invoking* the shortcut (`/name`) is **not** used for the quiz —
  invoking it makes Claude go and **do** the workflow ("Creating plan…", works
  the invoices) rather than answer questions.

---

## Output

Each run writes to `runs/<timestamp>-<adapter>-<task>/` (gitignored):

- `comprehend.json` — the graded quiz result (per-question and headline score).
- `teach_transcript.txt` / `handoff.txt` / `codex_chat.txt` — what the product
  produced.
- `shortcut_prompt.txt` (claude-teach) — Claude's captured understanding.
- a screenshot of the final product state.
- `screen.mov` / `screen.mkv` — a full screen recording of the run (video +
  narration audio) for after-the-fact review. A run that *failed* keeps its
  video too, as `runs/<ts>-<adapter>-<task>-FAILED-screen.<ext>`.

and prints, e.g.:

```
══════ COMPREHEND — invoice-3way-match (claude) ══════
  q2   ✓  The Goods Receipts section (Received qty)
  q3   ✓  Hold, reason awaiting_receipt
  ...
  ── closed: 7/9
```

The score varies run to run with how completely that particular teach captured
the workflow — that spread *is* the teach-quality signal the benchmark measures.

---

## Troubleshooting

- **claude-teach fails at "opening the Claude panel"** — the Claude extension
  isn't installed/signed-in in the managed profile, or an extension update
  changed its icon-click handler. Check the profile with `showAndTell browser
  launch`; see `open_claude_panel` in `students/claude.py`.
- **"managed Chrome did not come up" / CDP connect timeout** — a leftover Chrome
  on the profile. `pkill -f "user-data-dir=$HOME/.showAndTell/chrome-profile"` and
  retry.
- **No voice recorded** — VB-CABLE or SwitchAudioSource is unavailable. Run
  `./showAndTell setup` again and verify that `VB-CABLE` appears in macOS Sound.
- **Port busy** — a stray `showAndTell fixture-host` holds 8091; stop it.

---

## Changing an adapter?

The hands-free flows encode non-obvious, hard-won implementation constraints —
especially for Claude-in-Chrome: how the hands-free panel open works (and why
it once needed a real icon click), why Teach leaves no chat memory, the
shortcut save/capture/`/`-menu mechanics, the panel reply-timing guards, and
the CDP gotchas (arrow-function `evaluate` returning `{}`, buffered logs,
leftover Chrome). Read `students/claude.py` before changing that behavior —
its comments record the constraints.
