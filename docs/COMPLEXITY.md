# Task Complexity Index (TCI)

Every task carries a complexity score so product results can be read against
task difficulty (does a product only pass easy tasks?). The score is the sum
of six dimensions; it shows up in the task viewer as a tier-banded pill (a
sortable column on the overview, a pill + per-dimension strip on each task
page). Scoring lives in `src/showAndTell/quiz/complexity.py`.

## The six questions

Each dimension answers one plain question about the task:

1. **Rule** — how complicated is the decision rule itself? "Assign every open
   unassigned issue to yourself" is one if-statement; "spam beats
   unanswered-question beats helpful-guide" is a decision tree with
   precedence.
2. **Evidence** — how much must be consulted before each decision? Most tasks
   decide from the page in front of them; invoice-3way-match joins the
   invoice, the PO, and the goods receipts; pcn-triage spans three
   applications.
3. **Plan** — once decided, how much must be done? One click, versus
   comment-*then*-close, versus three edits per issue — plus chained steps
   and state that carries across items (a running budget).
4. **Precision** — any arithmetic or exact cutoffs? Tolerances, thresholds,
   and inclusive/exclusive boundaries ("exactly $25 still counts").
5. **Inference** — must a "never do X" rule be inferred even though it is
   invisible in an action stream?
6. **Signal** — how much seeing-and-hearing must be fused? Screens to track,
   narrated words to absorb, and the binding of deictic speech ("nobody is on
   *this one*") to values that exist only on screen — or across screens.

## Parameters

**Pass 1 — auto-extracted** (counted from the task's files, no judgment):

| param | source | how |
|---|---|---|
| `branches` | `task_logic.py` | AST count of `if`/`elif`/`for`/`while`/bool-ops |
| `outcomes` | `task_logic.py` | distinct return payloads (min 1) |
| `arithmetic` | `task_logic.py` | any `+ − × ÷` or ordering comparison |
| `steps` | `demonstrate.py` | count of `on_step(` calls |
| `words` | `demo/narration_script.jsonl` | total words across `text` fields |

**Pass 2 — author-declared** in `task.toml`. Every task MUST carry this block
(a missing or invalid block is a hard error — the viewer build and the
complexity tests fail until the author declares the flags):

```toml
[complexity]
hops = 3          # lookups/records consulted per decision (1-3)
systems = 3       # distinct applications spanned (1-3)
plan = 2          # actions per processed item (1-3)
chained = true    # a later step depends on an earlier step's result
state = false     # mutable state carries across items (e.g. remaining budget)
precedence = 0    # precedence levels among competing rules (0-2)
optimize = false  # pick-best over a candidate set, with a tie-break
never_rules = true   # a "never do X" rule must be inferred from the demo
binding = 2       # 1 = decisive values live on screen only; 2 = matched ACROSS screens
```

## Formula

```
D1 rule       = log2(1+branches) + 0.5·(outcomes−1) + precedence
D2 evidence   = 1.5·(hops−1) + (systems−1)
D3 plan       = 1.5·(plan−1) + chained + state
D4 precision  = arithmetic + optimize
D5 inference  = never_rules
D6 signal     = 0.25·steps + words/100 + binding

TCI = D1 + D2 + D3 + D4 + D5 + D6
```

Weights encode three levels of impact: **1.5** for the strongest drivers
(each *extra* lookup per decision, each *extra* action per item — they
multiply work across every item processed), **1.0** for full structural
properties, **0.5** for incremental counts. `branches` goes through `log2` because 2→4 branches
changes a task's character far more than 10→15.

**Tiers** (edges 8.5 / 10.5 / 12.5 / 15.0):

| tier | TCI | character |
|---|---|---|
| 1 | < 8.5 | single rule, single page |
| 2 | 8.5–10.5 | keyword & threshold rules |
| 3 | 10.5–12.5 | precedence, multi-branch |
| 4 | 12.5–15 | multi-action plans |
| 5 | ≥ 15 | multi-source, multi-stage |

## Worked example — pcn-triage (TCI 19.0, hardest)

```
what the task has                               dimension score
─────────────────────────────────────────────   ─────────────────────────────
3 branches, 3 outcomes                          D1 = log2(4) + 0.5·2 + 0 = 3.0
consults PCN doc + sheet + SAP (3 systems)      D2 = 1.5·2 + 2           = 5.0
map in the sheet, then verify in SAP            D3 = 1.5·1 + 1 + 0       = 2.5
no arithmetic                                   D4 = 0                   = 0.0
"the sheet is only a lead, never act on it
  alone"                                        D5 = 1                   = 1.0
13 steps, 225 narrated words, part numbers
  carried across systems                        D6 = 3.25 + 2.25 + 2     = 7.5
                                                                   TCI   = 19.0
```

By rule alone pcn-triage is simple — "if the part numbers match, act" — but
its demo is the heaviest signal in the benchmark. That input-side burden is
exactly what D6 measures.

## Policies & provenance

- **Hard error for new tasks.** `score_task` raises `ComplexityError` when
  the `[complexity]` block is missing or malformed; `showAndTell.viewer.generate`
  fails the build with the task's name. Authors declare the flags when
  writing the task — they already know the answers.
- **Cache-safe.** `cache.fingerprint` strips the `[complexity]` block from
  `task.toml` before hashing: the flags describe how hard a task is, not what
  a cached teach result means, so declaring or tuning them never forces a
  re-teach.
- **Validation.** The formula was calibrated against the intended per-site
  task ladders (mean Spearman ≈ 0.85; every
  disagreement is an adjacent-rung swap between near-tied tasks). This is a
  design-time check, deliberately not a repo test — narration edits shift D6
  and would flake it.
- **Later.** Once enough real runs exist, the weights can be re-fit against
  observed product scores instead of the intended ladder order.
