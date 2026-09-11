"""Plot the repository's Task Complexity Index from real task definitions or a frozen export."""
from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import tomllib

from .quiz.complexity import score_task


def collect(root: Path, revision: str, label: str):
    tasks=[]
    for manifest in sorted(root.glob('*/task.toml')):
        folder=manifest.parent
        result=score_task(folder)  # Fail loudly on missing/invalid source inputs.
        config=tomllib.loads(manifest.read_text())
        hashes={name:sha256((folder/name).read_bytes()).hexdigest() for name in
                ['task.toml','task_logic.py','demonstrate.py','demo/narration_script.jsonl']}
        tasks.append(dict(id=folder.name,summary=config.get('task',{}).get('summary',''),
                          **result,input_sha256=hashes))
    if not tasks:raise ValueError('No task definitions found')
    return dict(source_revision=revision,label=label,scorer_sha256=sha256(Path(__file__).with_name('quiz').joinpath('complexity.py').read_bytes()).hexdigest(),
                scope='Task-definition snapshot, including named variants. Not joined to benchmark result rows.',
                tasks=tasks,tier_counts=dict(Counter(t['tier'] for t in tasks)))


def render(snapshot, output: Path):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.patches import Patch
    output.mkdir(parents=True,exist_ok=True)
    ink='#0d1824';muted='#5c7389';line='#d8dde2'
    dims=['rule','evidence','plan','precision','inference','signal']
    colors=['#f5b840','#65aeb5','#a89acd','#cc8e7c','#91ad88','#596f87']
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':12,'text.color':ink,
                         'xtick.color':muted,'ytick.color':ink,'svg.fonttype':'none','svg.hashsalt':'showtell-tci'})
    def save(fig,name):
        for ext in ['svg','png']:fig.savefig(output/f'{name}.{ext}',dpi=170,facecolor='white',metadata={'Date':None} if ext=='svg' else {})
        # Keep SVG text selectable, with a browser-safe fallback on machines without DejaVu.
        svg_path = output / f'{name}.svg'
        svg_path.write_text(svg_path.read_text().replace("font-family: 'DejaVu Sans'", "font-family: 'DejaVu Sans', Arial, sans-serif"))
        plt.close(fig)
    def style(ax):
        for spine in ax.spines.values():spine.set_visible(False)
        ax.tick_params(length=0,pad=10);ax.set_axisbelow(True)
    tasks=snapshot['tasks'];counts=Counter(t['tier'] for t in tasks)
    fig,ax=plt.subplots(figsize=(11,5.4));fig.subplots_adjust(left=.09,right=.96,top=.73,bottom=.23);style(ax)
    labels=['Tier 1\n< 8.5','Tier 2\n8.5–<10.5','Tier 3\n10.5–<12.5','Tier 4\n12.5–<15','Tier 5\n≥ 15']
    bars=ax.bar(range(5),[counts[t] for t in range(1,6)],width=.55,color=['#fff0cf','#f9d383','#f5b840','#c38a23','#835407'])
    ax.set_xticks(range(5),labels);ax.set_ylim(0,max(counts.values())*1.18)
    from matplotlib.ticker import MaxNLocator
    ax.yaxis.set_major_locator(MaxNLocator(integer=True));ax.grid(axis='y',color=line);ax.set_ylabel('Task definitions',color=muted)
    for bar in bars:ax.text(bar.get_x()+bar.get_width()/2,bar.get_height()+.35,str(int(bar.get_height())),ha='center',fontsize=19)
    fig.text(.04,.95,'One way to bucket the tasks',fontsize=22,va='top')
    fig.text(.04,.895,f'{len(tasks)} task definitions · {snapshot["label"]}',fontsize=12,color=muted,va='top')
    fig.text(.04,.055,'TCI helps spread arena tasks across expected difficulty levels. Named variants count separately.',fontsize=11,color=muted)
    save(fig,'tci-distribution')

    # Real examples spaced across the observed TCI range, without hand-picking winners.
    ordered=sorted(tasks,key=lambda t:(t['tci'],t['id']))
    selected=[ordered[i] for i in sorted(set(round(i) for i in np.linspace(0,len(ordered)-1,min(8,len(ordered)))))]
    fig,ax=plt.subplots(figsize=(12,7.3));fig.subplots_adjust(left=.32,right=.93,top=.77,bottom=.2);style(ax)
    left=np.zeros(len(selected))
    for dim,color in zip(dims,colors):
        values=[t['dims'][dim] for t in selected]
        ax.barh(range(len(selected)),values,left=left,color=color,height=.55);left+=values
    ax.set_yticks(range(len(selected)),[t['id'] for t in selected]);ax.invert_yaxis();ax.set_xlim(0,max(left)*1.16)
    for y,t in enumerate(selected):ax.text(left[y]+.3,y,f'{t["tci"]:.2f}',va='center',fontsize=12)
    ax.grid(axis='x',color=line);ax.set_xlabel('TCI points',color=muted)
    fig.text(.04,.95,'What goes into the buckets?',fontsize=22,va='top')
    fig.text(.04,.905,'Six additive components · eight examples spaced across the observed TCI range',fontsize=12,color=muted,va='top')
    fig.legend(handles=[Patch(color=c,label=d.title()) for c,d in zip(colors,dims)],loc='lower center',bbox_to_anchor=(.5,.075),ncol=6,frameon=False,fontsize=11)
    fig.text(.04,.025,f'{snapshot["label"]}. Source components are rounded to two decimals; displayed totals use the scorer output.',fontsize=10,color=muted)
    save(fig,'tci-components')


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    group=p.add_mutually_exclusive_group(required=True)
    group.add_argument('--tasks-root',type=Path)
    group.add_argument('--metrics',type=Path,help='Rerender a previously exported tci.json')
    p.add_argument('--source-revision',default='unversioned')
    p.add_argument('--label',default='Task-definition snapshot')
    p.add_argument('--output-dir',type=Path,default=Path('runs/tci-charts'))
    args=p.parse_args(argv)
    snapshot=json.loads(args.metrics.read_text()) if args.metrics else collect(args.tasks_root,args.source_revision,args.label)
    render(snapshot,args.output_dir)
    (args.output_dir/'tci.json').write_text(json.dumps(snapshot,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'tasks':len(snapshot['tasks']),'tier_counts':snapshot['tier_counts']}))
    return 0


if __name__=='__main__':raise SystemExit(main())
