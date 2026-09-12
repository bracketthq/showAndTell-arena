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
    extra = dict(first, Run='3', Score='1', Comment='')
    # Run IDs are occurrence ordered within each case/system.
    index = max(i for i,r in enumerate(rows) if r['Usecase']==first['Usecase'] and r['Agent']=='Brackett')
    rows.insert(index+1, extra)
    source = tmp_path/'new.csv'
    with source.open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=rows[0].keys())
        writer.writeheader();writer.writerows(rows)
    subprocess.run([sys.executable,str(SCRIPT),'--site-dir',str(site),'--results',str(source),'--snapshot-date','2026-09-12'],check=True,capture_output=True,text=True)
    data=json.loads((site/'data/results.json').read_text())
    analysis=json.loads((site/'assets/analysis/analysis.json').read_text())
    html=(site/'index.html').read_text()
    assert len(data['runs'])==145
    assert len(analysis['task_inventory'])==42
    assert len(data['untested_cases'])==12
    assert analysis['outcomes']['Brackett']['Refusal']==1
    assert data['metadata']['source_sha256']==sha256(source.read_bytes()).hexdigest()
    assert 'Snapshot · 12 Sep 2026' in html
    assert '145 selected attempts across 30 tested cases' in html
    assert '61 selected attempts across 30 tested cases' in html
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
