"""Internal leaderboard: aggregate run outputs (runs/*/comprehend.json) into a
product x task score board, rendered to the terminal, Markdown, HTML and JSON.

A run directory is named `<YYYYMMDD-HHMMSS>-<adapter>-<task>` and holds a
`comprehend.json` with the headline `score` and multiple-choice tallies.
This module reads only those facts (dir name + comprehend.json), so it works
for any run produced by the live adapters without extra bookkeeping.

The Markdown/HTML it emits are the same tables we would publish to a Kaggle
Dataset + Notebook (or a HuggingFace Space) when the board goes public; for now
they are the internal tracking view.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

# adapter directory-slug -> human product label for the board.
PRODUCTS = {
    "claude-teach": "Claude-in-Chrome (Teach)",
    "brackett-teach": "Brackett (Show and Tell)",
    "codex-record": "Codex (Record & Replay)",
}
_RUNDIR = re.compile(r"^(?P<ts>\d{8}-\d{6})-(?P<adapter>%s)-(?P<task>.+)$"
                     % "|".join(map(re.escape, PRODUCTS)))


@dataclass
class Run:
    ts: str
    adapter: str
    task: str
    score: float
    multiple_choice_correct: int
    multiple_choice_total: int
    path: Path
    judge_fingerprint: str | None
    official: bool


def load_runs(runs_dir: Path) -> list[Run]:
    out: list[Run] = []
    if not runs_dir.is_dir():
        return out
    for d in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
        m = _RUNDIR.match(d.name)
        cj = d / "comprehend.json"
        if not m or not cj.exists():
            continue
        try:
            r = json.loads(cj.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        judge = r.get("judge") or {}
        if r.get("status", "complete") != "complete" or r.get("score") is None:
            continue
        out.append(Run(
            ts=m["ts"], adapter=m["adapter"], task=m["task"],
            score=float(r.get("score", 0.0)),
            multiple_choice_correct=int(r.get("multiple_choice_correct", r.get("closed_correct", 0))),
            multiple_choice_total=int(r.get("multiple_choice_total", r.get("closed_total", 0))),
            path=d,
            judge_fingerprint=(judge.get("prompt_fingerprint")
                               or judge.get("config_fingerprint")),
            official=bool(judge.get("verified")),
        ))
    return out


def _pick(runs: list[Run], agg: str) -> Run:
    """Collapse repeated (adapter, task) runs into one per the chosen policy."""
    if agg == "best":
        return max(runs, key=lambda r: r.score)
    if agg == "mean":
        n = len(runs)
        base = max(runs, key=lambda r: r.ts)
        return Run(
            ts=base.ts, adapter=base.adapter, task=base.task,
            score=sum(r.score for r in runs) / n,
            multiple_choice_correct=round(sum(r.multiple_choice_correct for r in runs) / n),
            multiple_choice_total=base.multiple_choice_total,
            path=base.path,
            judge_fingerprint=base.judge_fingerprint,
            official=all(r.official for r in runs),
        )
    return max(runs, key=lambda r: r.ts)  # "latest" (default)


def aggregate(runs: list[Run], agg: str = "latest") -> dict:
    """Return {cells: {(adapter,task): Run}, tasks, products, standings, n_runs}."""
    by_cell: dict[tuple[str, str], list[Run]] = {}
    for r in runs:
        by_cell.setdefault((r.adapter, r.task), []).append(r)
    cells = {k: _pick(v, agg) for k, v in by_cell.items()}
    judge_fingerprints = {r.judge_fingerprint for r in cells.values()}
    if len(judge_fingerprints) > 1:
        raise ValueError(
            "cannot combine results from different judge protocols; re-grade "
            "them to one judge configuration first")

    tasks = sorted({t for _, t in cells})
    products = [a for a in PRODUCTS if any(pa == a for pa, _ in cells)]

    standings = []
    for a in products:
        rows = [c for (pa, _), c in cells.items() if pa == a]
        n = len(rows)
        standings.append({
            "adapter": a, "product": PRODUCTS[a], "tasks": n,
            "score": sum(c.score for c in rows) / n if n else 0.0,
        })
    standings.sort(key=lambda s: s["score"], reverse=True)
    return {"cells": cells, "tasks": tasks, "products": products,
            "standings": standings, "n_runs": len(runs),
            "judge_fingerprint": next(iter(judge_fingerprints), None),
            "official": bool(cells) and all(r.official for r in cells.values())}


# ── renderers ────────────────────────────────────────────────────────────────

def render_terminal(a: dict, agg: str, out=print) -> None:
    out("")
    out(f"══════ ShowAndTell-Bench leaderboard  ({a['n_runs']} runs · agg={agg}) ══════")
    if not a["standings"]:
        out("  (no scored runs found)")
        return
    out("")
    out(f"  {'#':<3}{'product':<28}{'tasks':>6}{'score':>8}")
    for i, s in enumerate(a["standings"], 1):
        out(f"  {i:<3}{s['product']:<28}{s['tasks']:>6}{s['score']:>8.2f}")
    out("")
    out("  per task (score):")
    w = max((len(t) for t in a["tasks"]), default=4)
    head = "  " + " " * (w + 2) + "".join(f"{PRODUCTS[p].split(' ')[0]:>12}" for p in a["products"])
    out(head)
    for t in a["tasks"]:
        row = f"  {t:<{w + 2}}"
        for p in a["products"]:
            c = a["cells"].get((p, t))
            row += f"{(f'{c.score:.2f}' if c else '·'):>12}"
        out(row)


def render_markdown(a: dict, agg: str) -> str:
    L = [f"# ShowAndTell-Bench leaderboard",
         "",
         f"_{a['n_runs']} runs · aggregation: **{agg}** · "
         f"status: **{'official' if a['official'] else 'local/unverified'}** "
         "(one score per product×task)._",
         "", "## Standings", "",
         "| # | Product | Tasks | Score |",
         "|---|---------|------:|------:|"]
    for i, s in enumerate(a["standings"], 1):
        L.append(f"| {i} | {s['product']} | {s['tasks']} | {s['score']:.2f} |")
    L += ["", "## Per-task score", "",
          "| Task | " + " | ".join(PRODUCTS[p] for p in a["products"]) + " |",
          "|------|" + "|".join(["---:"] * len(a["products"])) + "|"]
    for t in a["tasks"]:
        cells = " | ".join(
            (f"{a['cells'][(p, t)].score:.2f}" if (p, t) in a["cells"] else "·")
            for p in a["products"])
        L.append(f"| `{t}` | {cells} |")
    return "\n".join(L) + "\n"


def render_html(a: dict, agg: str) -> str:
    def th(x): return f"<th>{x}</th>"
    def td(x, cls=""): return f'<td class="{cls}">{x}</td>'
    stand = "".join(
        "<tr>" + td(i) + td(s["product"], "l") + td(s["tasks"])
        + td(f"{s['score']:.2f}", "s") + "</tr>"
        for i, s in enumerate(a["standings"], 1))
    matrix_head = "<tr>" + th("Task") + "".join(th(PRODUCTS[p]) for p in a["products"]) + "</tr>"
    matrix_rows = ""
    for t in a["tasks"]:
        row = td(f"<code>{t}</code>", "l")
        for p in a["products"]:
            c = a["cells"].get((p, t))
            row += (td(f"{c.score:.2f}", "s") if c else td("·", "muted"))
        matrix_rows += "<tr>" + row + "</tr>"
    return f"""<!doctype html><meta charset=utf-8>
<title>ShowAndTell-Bench leaderboard</title>
<style>
 body{{font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;max-width:820px;margin:2rem auto;padding:0 1rem;color:#1a1a1a}}
 h1{{font-size:1.5rem;margin:0 0 .25rem}} .sub{{color:#666;margin:0 0 1.5rem}}
 table{{border-collapse:collapse;width:100%;margin:0 0 2rem}}
 th,td{{padding:.5rem .75rem;border-bottom:1px solid #eee;text-align:right}}
 th{{font-size:.8rem;text-transform:uppercase;letter-spacing:.03em;color:#888;border-bottom:2px solid #ddd}}
 td.l,th:first-child{{text-align:left}} td.s{{font-weight:600}} td.muted{{color:#ccc}}
 tr:first-child td{{font-weight:600}}
 @media(prefers-color-scheme:dark){{body{{background:#111;color:#eee}}th,td{{border-color:#333}}td.muted{{color:#555}}}}
</style>
<h1>ShowAndTell-Bench leaderboard</h1>
<p class=sub>{a['n_runs']} runs · aggregation: <b>{agg}</b> · status:
<b>{'official' if a['official'] else 'local/unverified'}</b> · one score per product×task</p>
<h2>Standings</h2>
<table><tr>{th('#')}{th('Product')}{th('Tasks')}{th('Score')}</tr>{stand}</table>
<h2>Per-task score</h2>
<table>{matrix_head}{matrix_rows}</table>
"""


def build(runs_dir: Path, agg: str = "latest", out_dir: Path | None = None,
          out=print) -> dict:
    runs = load_runs(runs_dir)
    a = aggregate(runs, agg)
    render_terminal(a, agg, out)
    out_dir = out_dir or runs_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "leaderboard.md").write_text(render_markdown(a, agg))
    (out_dir / "leaderboard.html").write_text(render_html(a, agg))
    (out_dir / "leaderboard.json").write_text(json.dumps({
        "aggregation": agg, "n_runs": a["n_runs"],
        "judge_fingerprint": a["judge_fingerprint"],
        "official": a["official"],
        "standings": a["standings"],
        "cells": [{"product": p, "task": t, "score": c.score,
                   "multiple_choice": [c.multiple_choice_correct, c.multiple_choice_total],
                   "run": c.path.name}
                  for (p, t), c in sorted(a["cells"].items())],
    }, indent=2))
    out("")
    out(f"  wrote {out_dir}/leaderboard.{{md,html,json}}")
    return a
