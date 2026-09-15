"""Validate this static publication without browser automation or third-party packages."""
from collections import Counter, defaultdict
import csv
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re
import sys
from statistics import mean
from urllib.parse import urlsplit, unquote

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
sys.path.insert(0, str(ROOT / "src"))
from showAndTell.publication import private_publication_reference


def check_private_sources(site):
    private_reference = re.compile(
        r'(?:\.\.?/showtellarena(?:[/\s`]|$)'
        r'|(?:~|/Users/[^/\s]+|/home/[^/\s]+)/repos/showtellarena(?:[/\s`]|$)'
        r'|github\.com/bracketthq/showtellarena(?:\.git)?(?:[/?#\s`]|$))',
        re.IGNORECASE,
    )
    text_extensions = {'.html', '.md', '.json', '.csv', '.txt', '.js', '.css', '.svg', '.xml'}
    for path in site.rglob('*'):
        if not path.is_file():
            continue
        assert path.suffix.lower() not in {'.xlsx', '.xls', '.xlsm', '.xlsb'}, (
            f'Private workbook must not be bundled: {path.relative_to(site)}'
        )
        assert not (path.suffix.lower() == '.docx' and 'build_specification' in path.name.lower()), (
            f'Internal build specification must not be bundled: {path.relative_to(site)}'
        )
        if path.suffix.lower() in text_extensions:
            text = unquote(path.read_text(encoding='utf-8'))
            assert not (private_reference.search(text) or private_publication_reference(text)), (
                f'Private source reference in publication: {path.relative_to(site)}'
            )


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids, self.links, self.text, self.headings = [], [], [], []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag in ("a", "link", "img", "script", "source", "video"):
            for key in ("href", "src", "poster"):
                if attrs.get(key): self.links.append(attrs[key])
        if tag == "img": assert "alt" in attrs, "Image needs alt text"
        if re.fullmatch("h[1-6]", tag): self.headings.append(tag)
    def handle_data(self, data):
        self.text.append(data)


def main(site=None):
    global SITE
    if site is not None:
        SITE = Path(site).resolve()
    check_private_sources(SITE)
    html = (SITE / "index.html").read_text()
    page = Page()
    page.feed(html)
    assert len(page.ids) == len(set(page.ids)), "Duplicate HTML IDs"
    assert page.headings.count("h1") == 1, "Use one page heading"
    assert 'name="color-scheme" content="light"' in html
    public_url = "https://bracketthq.github.io/showAndTell-arena/"
    assert 'noindex' not in html.lower(), "Public research site must allow indexing"
    assert f'<link rel="canonical" href="{public_url}">' in html
    assert f'<meta property="og:url" content="{public_url}">' in html
    robots = (SITE / "robots.txt").read_text()
    assert not re.search(r'^Disallow:\s*/\s*$', robots, re.MULTILINE), "Public site must allow crawling"
    assert f'Sitemap: {public_url}sitemap.xml' in robots
    from xml.etree import ElementTree
    sitemap = ElementTree.parse(SITE / "sitemap.xml")
    assert [e.text for e in sitemap.findall('.//{http://www.sitemaps.org/schemas/sitemap/0.9}loc')] == [public_url]
    assert not re.search(r'https?://[^/\s"<>]+\.pages\.github\.io', html), "Do not link the retired private Pages host"
    for link in page.links:
        parsed = urlsplit(link)
        if parsed.scheme or parsed.netloc: continue
        if parsed.path:
            path = (SITE / unquote(parsed.path)).resolve()
            assert path.is_relative_to(SITE), f"Path escapes site: {link}"
            assert path.is_file(), f"Missing file: {link}"
        elif parsed.fragment:
            assert parsed.fragment in page.ids, f"Missing anchor: {link}"

    data = json.loads((SITE / "data/results.json").read_text())
    scores, counts = defaultdict(list), defaultdict(lambda: [0, 0])
    models, run_models = defaultdict(Counter), {}
    with (SITE / "data/results.csv").open() as f:
        for row in csv.DictReader(f):
            complete = row["Score"] != "NA"
            score = float(row["Score"]) if complete else 0.0
            assert 0 <= score <= 1
            scores[row["Agent"], row["Usecase"]].append(score)
            counts[row["Agent"]][0] += int(complete)
            counts[row["Agent"]][1] += 1
            model = row['Model'].strip() or 'Not recorded'
            models[row['Agent']][model] += 1
            key = (row['Agent'], row['Usecase'], int(row['Run']))
            assert key not in run_models, f'Duplicate attempt: {key}'
            run_models[key] = model
    means = {key: mean(values) for key, values in scores.items()}
    shared = set.intersection(*(set(c for a, c in means if a == agent) for agent in data["agents"]))
    assert shared == set(data["shared_cases"])
    assert len(data["runs"]) == sum(v[1] for v in counts.values())
    assert {a: dict(v) for a, v in models.items()} == data['model_labels']
    for run in data['runs']:
        assert run['model'] == run_models[(run['agent'], run['case'], run['run'])]
        if run['agent'] == 'Brackett':
            assert run['model'] == 'Fused/multiple models', 'Brackett must use its public system description'
    for agent, stats in data["agents"].items():
        assert counts[agent] == [stats["completed_runs"], stats["attempted_runs"]]
        assert math.isclose(mean(v for (a, _), v in means.items() if a == agent), stats["average_score"])
        for case, score in data["case_scores"][agent].items():
            assert math.isclose(means[agent, case], score)
        shared_score = mean(means[agent, case] for case in shared)
        assert math.isclose(shared_score, stats["shared_score"])
        assert math.isclose(shared_score, data["shared_agents"][agent]["average_score"])
        match = re.search(r'data-agent="' + agent + r'".*?class="bar-value">([\d.]+)', html, re.S)
        assert match and match[1] == f"{shared_score * 100:.1f}", f"Stale saved chart: {agent}"
        expected = data["shared_agents"][agent]
        assert f'{expected["completed_runs"]} / {expected["attempted_runs"]} completed attempts' in html

    source = json.loads((SITE / "data/example-source.json").read_text())
    plain = " ".join(" ".join(page.text).split())
    for question in source["questions"]:
        assert " ".join(question["question"].split()) in plain, f"Question changed: {question['id']}"
    assert source["revision"] in html, "Example links must use the frozen revision"
    for weight in ("0.40", "0.20"):
        assert f"<sup>{weight}</sup>" in html
    assert 'They do not represent a full AEI score.' in html
    analysis = json.loads((SITE / "assets/analysis/analysis.json").read_text())
    for key in ("shared_cases", "case_scores", "agents", "model_labels"):
        assert analysis[key] == data[key], f"Analysis disagrees with results: {key}"
    assert all(r['model'] == 'Fused/multiple models' for r in analysis['excluded_attempts'] if r['agent'] == 'Brackett')
    with (SITE / "assets/analysis/attempts.csv").open() as f:
        attempts = list(csv.DictReader(f))
    for agent in data["agents"]:
        observed = Counter(r["Outcome"] for r in attempts if r["Agent"] == agent and r["Score"])
        assert dict(observed) == analysis["outcomes"][agent]
    for attempt in attempts:
        if attempt['Score']:
            assert attempt['Model'] == run_models[(attempt['Agent'], attempt['Usecase'], int(attempt['Run']))]
    for agent, labels in data['model_labels'].items():
        expected = agent + ': ' + ', '.join(f'{label} ({count} selected attempts)' for label, count in labels.items()) + '.'
        assert expected in plain, f'Stale model labels: {agent}'
    assert len({r["Usecase"] for r in attempts}) == len(analysis["task_inventory"])
    assert sum(r["Score"] != "" for r in attempts) == len(data["runs"])
    questions = json.loads((SITE / "data/task-questions.json").read_text())["questions"]
    evidence = analysis["question_evidence"]
    assert evidence["ids"] == [q["id"] for q in questions]
    assert evidence["matrix"] == [[int(any(e["type"] == t for e in q.get("evidence", [])))
                                  for t in evidence["types"]] for q in questions]
    tci = json.loads((SITE / "assets/analysis/tci.json").read_text())
    for task in tci["tasks"]:
        p = task["params"]
        dims = dict(rule=math.log2(1+p["branches"])+.5*(p["outcomes"]-1)+p["precedence"],
                    evidence=1.5*(p["hops"]-1)+(p["systems"]-1),
                    plan=1.5*(p["plan"]-1)+p["chained"]+p["state"],
                    precision=p["arithmetic"]+p["optimize"], inference=p["never_rules"],
                    signal=.25*p["steps"]+p["words"]/100+p["binding"])
        total = sum(dims.values())
        assert task["dims"] == {k: round(v, 2) for k, v in dims.items()}
        assert task["tci"] == round(total, 2)
        assert task["tier"] == 1 + sum(total >= edge for edge in (8.5, 10.5, 12.5, 15))
    assert tci["tier_counts"] == {str(k): v for k, v in Counter(t["tier"] for t in tci["tasks"]).items()}
    print(f"Verified: {len(page.ids)} unique IDs, local links and assets, exact source questions, {sum(v[1] for v in counts.values())} attempts, {len(shared)} shared cases, chart scores, and AEI scope.")


if __name__ == "__main__":
    main()
