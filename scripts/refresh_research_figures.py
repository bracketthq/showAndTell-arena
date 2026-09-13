"""Refresh research figures and page data together; validate in staging before replacing site/."""
from __future__ import annotations

import argparse
from datetime import date
from html import escape
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from showAndTell import benchmark_analysis, tci_charts
import check_site
import update_site_results


def replace_block(html, key, content):
    pattern = rf'(<!-- refresh:{key} -->).*?(<!-- /refresh:{key} -->)'
    html, count = re.subn(pattern, lambda m: m[1]+content+m[2], html, flags=re.S)
    if count != 1:
        raise ValueError(f'Expected one page refresh block: {key}; found {count}')
    return html


def sync_page(html, results, analysis, task_scope=None):
    stats = results['shared_agents']
    expected = {'Brackett', 'Claude', 'Codex'}
    if set(stats) != expected:
        raise ValueError('The page currently supports Brackett, Claude, and Codex. Update its table, JavaScript, colors and tests before changing systems.')
    if not results['shared_cases']:
        raise ValueError('No shared cases: redesign the shared-results view rather than publishing a zero score.')
    rows = []
    for agent in sorted(stats, key=lambda a: -stats[a]['average_score']):
        a = stats[agent]
        value = a['average_score']*100
        rows.append(f'<div class="bar-row" data-agent="{agent}"><div class="bar-label"><strong>{agent}</strong><span class="bar-detail">{a["completed_runs"]} / {a["attempted_runs"]} completed attempts</span></div><div class="bar-track"><div class="bar-fill {agent.lower()}" style="width:{value}%"></div></div><span class="bar-value">{value:.1f}<span>%</span></span></div>')
    shared = len(results['shared_cases'])
    tested = len(results['tested_cases'])
    untested = len(results['untested_cases'])
    leaders = analysis['shared_case_leaders']
    lead_text = ', '.join(f'{a}: {n}' for a, n in sorted(leaders.items(), key=lambda x: (-x[1], x[0])))
    snapshot = date.fromisoformat(results['metadata']['snapshot_date'])
    coverage = f'The snapshot includes {tested} cases with at least one attempted system and {untested} cases not yet tested by any system. '
    coverage += ' '.join(f'{escape(a)}: {s["attempted_runs"]} selected attempts across {s["tested_use_cases"]} tested cases.' for a, s in results['agents'].items())
    source_note = f'<cite>{escape(results["metadata"]["source"])}</cite>'
    sheet = results['metadata'].get('sheet')
    source_note += f', {escape(sheet)} sheet.' if sheet else '.'
    blocks = dict(source=source_note, bars='\n'+'\n'.join(rows)+'\n',
                  context=f'{shared} cases attempted by all three systems · equal weight per case',
                  date=f'Snapshot · {snapshot.strftime("%d %b %Y").lstrip("0")}',
                  tested=f'all {tested} tested cases',
                  leaders=f'Highest case means across {shared} shared cases — {escape(lead_text)}.',
                  coverage=coverage,
                  download=f'{len(results["runs"])} selected attempts across {tested} tested cases · CSV download')
    for key, value in blocks.items():
        html = replace_block(html, key, value)
    if task_scope is not None:
        html, n = re.subn(r'(<span data-refresh="tci-scope">).*?(</span>)', lambda m: m[1]+escape(task_scope)+m[2], html, flags=re.S)
        if n != 1: raise ValueError('Missing TCI scope marker')
    return html


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--results', type=Path, help='New XLSX or long-form CSV; omit to reproduce the committed snapshot')
    p.add_argument('--snapshot-date', type=date.fromisoformat, help='Required with --results')
    p.add_argument('--task-groups', type=Path, help='JSON object: exact workbook case name -> business-process group')
    p.add_argument('--tasks-root', type=Path, help='Recompute TCI from complete task definitions; otherwise rerender the frozen export')
    p.add_argument('--task-revision', help='Source revision for new task definitions')
    p.add_argument('--task-label', help='Short source label printed in the TCI figures')
    p.add_argument('--task-scope', help='Page caption identifying this task population and its relationship to result cases')
    p.add_argument('--site-dir', type=Path, default=ROOT/'site', help='Destination site (must already contain the publication)')
    args = p.parse_args(argv)
    if bool(args.results) != bool(args.snapshot_date):
        p.error('--results and --snapshot-date must be supplied together')
    if args.tasks_root and not all((args.task_revision, args.task_label, args.task_scope)):
        p.error('--tasks-root requires --task-revision, --task-label and --task-scope')
    if not args.tasks_root and any((args.task_revision, args.task_label, args.task_scope)):
        p.error('Task source metadata requires --tasks-root')
    site = args.site_dir.resolve()
    source = (args.results or site/'assets/analysis/attempts.csv').resolve()
    groups_path = (args.task_groups or site/'data/task-groups.json').resolve()
    groups = json.loads(groups_path.read_text())
    if not isinstance(groups, dict) or not all(isinstance(k,str) and isinstance(v,str) and v.strip() for k,v in groups.items()):
        raise ValueError('Task groups must map case names to nonempty group names')
    dataset, records, excluded = benchmark_analysis.load_attempts(source)
    analysis = benchmark_analysis.analyze(dataset, records, groups, excluded)
    unknown = [t['case'] for t in analysis['task_inventory'] if t['area']=='Unclassified']
    if unknown: raise ValueError(f'Assign a business-process group before publishing new cases: {unknown}')
    if set(dataset.agents) != {'Brackett','Claude','Codex'} or not analysis['shared_cases']:
        raise ValueError('Page requires all three supported systems and at least one shared case.')
    old = json.loads((site/'data/results.json').read_text())
    snapshot_date = args.snapshot_date or date.fromisoformat(old['metadata']['snapshot_date'])
    # All parsing, rendering and validation happens outside the publication directory.
    with tempfile.TemporaryDirectory(prefix='showtell-figures-') as tmp:
        stage = Path(tmp)/'site'
        shutil.copytree(site, stage)
        update_site_results.main([str(source),'--snapshot-date',snapshot_date.isoformat(),'--output-dir',str(stage/'data')])
        results = json.loads((stage/'data/results.json').read_text())
        if not args.results:
            results['metadata'] = old['metadata']
            (stage/'data/results.json').write_text(json.dumps(results,indent=2,ensure_ascii=False)+'\n')
        benchmark_analysis.main([str(source),'--output-dir',str(stage/'assets/analysis'),'--task-quiz',str(stage/'data/task-questions.json'),'--task-groups',str(groups_path)])
        if not args.results:
            report_path = stage/'assets/analysis/analysis.json'
            report = json.loads(report_path.read_text())
            previous = json.loads((site/'assets/analysis/analysis.json').read_text())
            # The attempt CSV carries neither the workbook identity nor the rows the workbook excluded.
            report['source'] = previous['source']
            report['excluded_attempts'] = previous.get('excluded_attempts', [])
            report_path.write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
        tci_args = ['--output-dir',str(stage/'assets/analysis')]
        if args.tasks_root:
            tci_args += ['--tasks-root',str(args.tasks_root.resolve()),'--source-revision',args.task_revision,'--label',args.task_label]
        else:
            tci_args += ['--metrics',str(site/'assets/analysis/tci.json')]
        tci_charts.main(tci_args)
        page = stage/'index.html'
        page.write_text(sync_page(page.read_text(),results,analysis,args.task_scope))
        (stage/'data/task-groups.json').write_text(json.dumps(groups,indent=2,ensure_ascii=False)+'\n')
        check_site.main(stage)
        for rel in ['data/results.json','data/results.csv','data/task-groups.json','index.html']:
            shutil.copy2(stage/rel, site/rel)
        shutil.copytree(stage/'assets/analysis',site/'assets/analysis',dirs_exist_ok=True)
    print('Refreshed and validated the research figures, page values and data. Nothing was committed or published.')


if __name__ == '__main__':
    main()
