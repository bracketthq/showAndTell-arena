"""Exercise publication refreshes against changed source data, not just rendering helpers."""
import csv
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/refresh_research_figures.py'


def test_refresh_changed_results_updates_page_and_preserves_untested_catalog(tmp_path):
    pytest.importorskip('matplotlib')
    site = tmp_path/'site'
    shutil.copytree(ROOT/'site', site)
    with (site/'assets/analysis/attempts.csv').open() as stream:
        rows = list(csv.DictReader(stream))
    # A real data change: add another selected attempt to a known case and change
    # a scored answer to an incomplete run with a reported reason.
    first = next(r for r in rows if r['Agent']=='Brackett' and r['Score'])
    first['Score'], first['Comment'] = 'NA', 'refusal'
    # Run IDs are occurrence ordered within each case/system; append after the last existing attempt.
    same = [i for i,r in enumerate(rows) if r['Usecase']==first['Usecase'] and r['Agent']=='Brackett']
    extra = dict(first, Run=str(len(same)+1), Score='1', Comment='')
    rows.insert(same[-1]+1, extra)
    source = tmp_path/'new.csv'
    with source.open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=rows[0].keys())
        writer.writeheader();writer.writerows(rows)
    subprocess.run([sys.executable,str(SCRIPT),'--site-dir',str(site),'--results',str(source),'--snapshot-date','2026-09-12'],check=True,capture_output=True,text=True)
    data=json.loads((site/'data/results.json').read_text())
    analysis=json.loads((site/'assets/analysis/analysis.json').read_text())
    html=(site/'index.html').read_text()
    assert len(data['runs'])==192
    assert len(analysis['task_inventory'])==42
    assert len(data['untested_cases'])==3
    assert analysis['outcomes']['Brackett']['Refusal']==1
    assert data['metadata']['source_sha256']==sha256(source.read_bytes()).hexdigest()
    assert 'Snapshot · 12 Sep 2026' in html
    assert '192 selected attempts across 39 tested cases' in html
    assert '108 selected attempts across 39 tested cases' in html
    # Spreads use every selected attempt of a case, not only pairs.
    changed = next(r for r in analysis['repeat_gaps'] if r['agent']=='Brackett' and r['case']==first['Usecase'])
    assert changed['attempts']==len(same)+1 and changed['gap']==pytest.approx(1)
    assert analysis['attempts_per_case']['Brackett'][str(len(same)+1)]==1
    # The command ran the independent full-page integrity check before copying.
    assert (site/'assets/analysis/case-heatmap.png').read_bytes().startswith(b'\x89PNG')


def test_unknown_task_stops_before_changing_publication(tmp_path):
    site=tmp_path/'site'
    shutil.copytree(ROOT/'site',site)
    before={p.relative_to(site):sha256(p.read_bytes()).hexdigest() for p in site.rglob('*') if p.is_file()}
    source=tmp_path/'unknown.csv'
    source.write_text('Usecase,Agent,Run,Score\nUnreviewed new task,Brackett,1,1\nUnreviewed new task,Claude,1,1\nUnreviewed new task,Codex,1,1\n')
    run=subprocess.run([sys.executable,str(SCRIPT),'--site-dir',str(site),'--results',str(source),'--snapshot-date','2026-09-12'],capture_output=True,text=True)
    assert run.returncode!=0 and 'Assign a business-process group' in run.stderr
    after={p.relative_to(site):sha256(p.read_bytes()).hexdigest() for p in site.rglob('*') if p.is_file()}
    assert before==after


def test_workbook_rows_excluded_from_overview_are_kept_for_provenance_only(tmp_path):
    openpyxl = pytest.importorskip('openpyxl')
    from showAndTell.benchmark_analysis import load_attempts, analyze
    book = openpyxl.Workbook()
    overview = book.active; overview.title = 'Overview'
    overview.append(['Usecase ', 'Avg. Brackett Score', 'Run 1', 'Run 2', 'Run 3', 'Avg. Claude Score', 'Run 1', 'Avg. Codex Score', 'Run 1'])
    overview.append(['A', .8, .9, .7, .8, 'NA', 'NA', .5, .5])
    overview.append(['Final Score']); overview.append(['Brackett', .8])
    detail = book.create_sheet('Detailed Analysis')
    detail.append(['Run Number', 'Usecase ', 'Agent', 'Score', 'Model', 'Comments'])
    detail.append([1, 'A', 'Brackett', .9, 'm', 'manual grade'])
    detail.append(['Earlier 1', 'A', 'Brackett', .1, 'm', 'Third set: earlier attempt, excluded from overview. Run abc'])
    detail.append([3, 'A', 'Brackett', .8, 'm', 'imported grade'])
    detail.append([1, 'A', 'Claude', 'NA', 'm', 'refusal. Run ID: x'])
    path = tmp_path/'book.xlsx'; book.save(path)
    data, records, excluded = load_attempts(path)
    assert len(records)==5 and [e['label'] for e in excluded]==['Earlier 1']
    assert excluded[0]['score']==pytest.approx(.1) and excluded[0]['case']=='A'
    result = analyze(data, records, {'A': 'Test'}, excluded)
    assert result['agents']['Brackett']['average_score']==pytest.approx(.8)
    assert result['attempts_per_case']=={'Brackett': {'3': 1}, 'Claude': {'1': 1}, 'Codex': {'1': 1}}
    assert result['repeat_gaps'][0]['gap']==pytest.approx(.2) and result['excluded_attempts']==excluded
    assert result['outcomes']['Claude']=={'Refusal': 1}
    detail.append(['Later 9', 'A', 'Brackett', .2, 'm', 'no such convention']); book.save(path)
    with pytest.raises(ValueError, match='Unrecognised run label'):
        load_attempts(path)
