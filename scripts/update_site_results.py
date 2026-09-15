"""Freeze a website result snapshot using the benchmark's existing chart semantics.

Run from the repository root:
  python scripts/update_site_results.py path/to/results.xlsx --snapshot-date YYYY-MM-DD
The generated CSV also works as input, so the snapshot can be reproduced without Excel.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from showAndTell import benchmark_charts as charts, benchmark_analysis as analysis


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--snapshot-date", type=date.fromisoformat, required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "site/data")
    args = parser.parse_args(argv)
    data, records, _ = analysis.load_attempts(args.source)
    summary = charts.summarize(data)
    summary["shared_agents"] = charts.summarize(charts.Dataset(
        [r for r in data.runs if r.case in summary["shared_cases"]],
        summary["shared_cases"], data.agents
    ))["agents"]
    summary["runs"] = [{k: r[k] for k in ('case', 'agent', 'run', 'score', 'completed', 'model')} for r in records]
    summary['model_labels'] = analysis.model_labels(records, data.agents)
    summary["metadata"] = {
        "snapshot_date": args.snapshot_date.isoformat(),
        "source": args.source.name,
        "source_sha256": sha256(args.source.read_bytes()).hexdigest(),
        "sheet": "Overview" if args.source.suffix.lower() == ".xlsx" else None,
        "score_source": "Overview run cells, cross-checked against matching Detailed Analysis rows; workbook average formulas are not used." if args.source.suffix.lower() == '.xlsx' else "CSV Score column; blank scores excluded and NA scored zero.",
        "status": "Preliminary; selected attempts; non-canonical grading (manual grades and imported model-assisted grades, as recorded in the workbook notes).",
        "methodology": "NA = attempted and incomplete, scored zero. Blank = untested, excluded. Average attempts within each use case, then weight use cases equally. Shared cases require an attempt from every system.",
        "completion_definition": "An attempt has a numeric benchmark score; not a measure of operational task success.",
        "system_configuration": analysis.model_note(summary['model_labels']),
        "model_source": "Brackett: author-specified Fused/multiple models system description; component model labels are not published. Other systems: " + ("Detailed Analysis, Model column; missing labels remain Not recorded." if args.source.suffix.lower() == '.xlsx' else "CSV Model column; missing labels remain Not recorded."),
        "uncertainty": "No confidence interval is reported for this selected-attempt snapshot.",
        "colors": {"Brackett": "#f5b840", "Claude": "#a89acd", "Codex": "#65aeb5"},
    }
    dest = args.output_dir
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "results.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    with (dest / "results.csv").open("w", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["Usecase", "Agent", "Run", "Score", "Model"])
        for run in records:
            writer.writerow([run['case'], run['agent'], run['run'], run['score'] if run['completed'] else "NA", run['model']])
    print(json.dumps({"shared_cases": len(summary["shared_cases"]), "shared_agents": summary["shared_agents"], "tested_cases": len(summary["tested_cases"]), "attempts": len(data.runs)}, indent=2))


if __name__ == "__main__":
    main()
