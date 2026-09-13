"""Freeze a website result snapshot using the benchmark's existing chart semantics.

Run from the repository root:
  python scripts/update_site_results.py path/to/results.xlsx --snapshot-date YYYY-MM-DD
The generated CSV also works as input, so the snapshot can be reproduced without Excel.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
from datetime import date
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("benchmark_charts", ROOT / "src/showAndTell/benchmark_charts.py")
charts = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = charts
spec.loader.exec_module(charts)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--snapshot-date", type=date.fromisoformat, required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "site/data")
    args = parser.parse_args(argv)
    data = charts.load_dataset(args.source)
    summary = charts.summarize(data)
    summary["shared_agents"] = charts.summarize(charts.Dataset(
        [r for r in data.runs if r.case in summary["shared_cases"]],
        summary["shared_cases"], data.agents
    ))["agents"]
    summary["runs"] = [asdict(r) for r in data.runs]
    summary["metadata"] = {
        "snapshot_date": args.snapshot_date.isoformat(),
        "source": args.source.name,
        "source_sha256": sha256(args.source.read_bytes()).hexdigest(),
        "sheet": "Overview" if args.source.suffix.lower() == ".xlsx" else None,
        "status": "Preliminary; selected attempts; non-canonical grading (manual grades and imported model-assisted grades, as recorded in the workbook notes).",
        "methodology": "NA = attempted and incomplete, scored zero. Blank = untested, excluded. Average attempts within each use case, then weight use cases equally. Shared cases require an attempt from every system.",
        "completion_definition": "An attempt has a numeric benchmark score; not a measure of operational task success.",
        "system_configuration": "Workbook labels identify products, not fully pinned model and adapter configurations. This snapshot is not a verified canonical leaderboard.",
        "uncertainty": "No confidence interval is reported for this selected-attempt snapshot.",
        "colors": {"Brackett": "#f5b840", "Claude": "#a89acd", "Codex": "#65aeb5"},
    }
    dest = args.output_dir
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "results.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    with (dest / "results.csv").open("w", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["Usecase", "Agent", "Run", "Score"])
        counts = {}
        for run in data.runs:
            key = (run.case, run.agent)
            counts[key] = counts.get(key, 0) + 1
            writer.writerow([run.case, run.agent, counts[key], run.score if run.completed else "NA"])
    print(json.dumps({"shared_cases": len(summary["shared_cases"]), "shared_agents": summary["shared_agents"], "tested_cases": len(summary["tested_cases"]), "attempts": len(data.runs)}, indent=2))


if __name__ == "__main__":
    main()
