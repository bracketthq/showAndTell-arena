"""Semantic diff between two Show and Tell bundles.

Quantifies SEMANTIC ALIGNMENT between two bundles parsed by `snt_bundle` —
typically an original human recording vs. a fixture pseudo-bundle from
`bundle-snapshot` (or a second recording of the rebuilt fixture). At each
demonstrated step we ask: same acted-element role+name, comparably-shaped
selectors, and an accessibility tree exposing the same content at similar
density? Steps align by screen identity (see `_signature`); everything else is
measured as divergence on the aligned pairs.

Stdlib only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from .parse import Bundle, Step

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")

# attribute equalities: `@name="v"` (XPath) and `[name="v"]` (CSS); both quote styles
_ATTR_XPATH = re.compile(r"""@([\w:-]+)\s*=\s*(['"])(.*?)\2""")
_ATTR_CSS = re.compile(r"""\[\s*([\w:-]+)\s*=\s*(['"])(.*?)\2\s*\]""")
_ID = re.compile(r"#([\w-]+)")
_CLASS = re.compile(r"\.([\w-]+)")


def _norm(s: str) -> str:
    """casefold, strip punctuation to spaces, collapse whitespace."""
    s = _PUNCT.sub(" ", (s or "").casefold())
    return _WS.sub(" ", s).strip()


def _attr_token(name: str, val: str) -> str:
    if name == "id":
        return "#" + val
    if name == "class":
        return "." + val
    return f'{name}="{val}"'


def selector_tokens(selectors: list[str]) -> set[str]:
    """Shape-tokens from a step's selectors: ids as `#x`, classes as `.y`, other
    attribute equalities as `name="v"`. Pulls ids/classes/attrs from CSS chains
    (each id/class along the chain) AND from XPath `[@id="…"]`/`[@attr="…"]`
    predicates; pure positional XPath (`//div[3]/div[1]`) contributes nothing."""
    tokens: set[str] = set()
    for sel in selectors or []:
        if not sel:
            continue
        rem = sel
        for m in _ATTR_XPATH.finditer(rem):
            tokens.add(_attr_token(m.group(1), m.group(3)))
        rem = _ATTR_XPATH.sub(" ", rem)
        for m in _ATTR_CSS.finditer(rem):
            tokens.add(_attr_token(m.group(1), m.group(3)))
        rem = _ATTR_CSS.sub(" ", rem)  # drop attr values before scanning #/. tokens
        for m in _ID.finditer(rem):
            tokens.add("#" + m.group(1))
        for m in _CLASS.finditer(rem):
            tokens.add("." + m.group(1))
    return tokens


@dataclass
class StepDiff:
    a_line: int | None
    b_line: int | None
    role_match: bool | None
    name_match: bool | None
    selector_shared: list[str]
    selector_only_a: list[str]
    selector_only_b: list[str]
    selector_jaccard: float | None
    tree_jaccard: float | None
    tree_only_a: list[str]
    tree_only_b: list[str]
    node_count_ratio: float | None
    score: float


@dataclass
class BundleDiff:
    pairs: list[StepDiff]
    unmatched_a: list[int]
    unmatched_b: list[int]
    summary: dict


def _root_name(step: Step) -> str:
    """Screen identity from Chromium's first, root accessibility node."""
    if not (step.trees and step.trees[0].nodes):
        return ""
    return step.trees[0].nodes[0].name


def _signature(step: Step) -> tuple[str, ...]:
    """Alignment POLICY: steps align iff their screens do — the normalized root
    a11y name (page title) is the whole key. Deliberately NOT the acted-element
    verb/role/name: a pseudo-bundle's steps carry `verb=None`/`target=None` (so
    stricter keys would align nothing), and a reworded target must surface as a
    measured `name_match=False`, not as a failure to align. Edge: steps with no
    tree collapse to `("",)` and align positionally among themselves."""
    return (_norm(_root_name(step)),)


def _tree_node_set(step: Step) -> set[tuple[str, str]]:
    """(role, _norm(name)) for every NAMED node across the step's trees,
    excluding role `InlineTextBox`."""
    out: set[tuple[str, str]] = set()
    for tr in step.trees:
        for n in tr.nodes:
            if n.role == "InlineTextBox" or not n.name:
                continue
            out.add((n.role, _norm(n.name)))
    return out


def _max_node_count(step: Step) -> int | None:
    return max((tr.node_count for tr in step.trees), default=None)


def _step_diff(a: Step, b: Step) -> StepDiff:
    if a.target is None or b.target is None:
        role_match = name_match = None
    else:
        role_match = a.target.role == b.target.role
        name_match = _norm(a.target.name) == _norm(b.target.name)

    ta, tb = selector_tokens(a.selectors), selector_tokens(b.selectors)
    shared, only_a, only_b = ta & tb, ta - tb, tb - ta
    # One-sided absence is a capture-capability gap (pseudo-bundles carry no
    # selectors), not measured disagreement — the axis needs both sides.
    selector_jaccard = len(shared) / len(ta | tb) if (ta and tb) else None

    if not a.trees or not b.trees:
        tree_jaccard: float | None = None
        tree_only_a: list[str] = []
        tree_only_b: list[str] = []
    else:
        sa, sb = _tree_node_set(a), _tree_node_set(b)
        union = sa | sb
        tree_jaccard = len(sa & sb) / len(union) if union else 1.0
        tree_only_a = sorted(f"{r}: {n}" for r, n in sa - sb)[:10]
        tree_only_b = sorted(f"{r}: {n}" for r, n in sb - sa)[:10]

    anc, bnc = _max_node_count(a), _max_node_count(b)
    node_count_ratio = bnc / anc if (anc and bnc is not None) else None

    vals: list[float] = []
    for m in (role_match, name_match):
        if m is not None:
            vals.append(1.0 if m else 0.0)
    for j in (selector_jaccard, tree_jaccard):
        if j is not None:
            vals.append(j)
    score = sum(vals) / len(vals) if vals else 1.0

    return StepDiff(
        a_line=a.line, b_line=b.line,
        role_match=role_match, name_match=name_match,
        selector_shared=sorted(shared), selector_only_a=sorted(only_a),
        selector_only_b=sorted(only_b), selector_jaccard=selector_jaccard,
        tree_jaccard=tree_jaccard, tree_only_a=tree_only_a, tree_only_b=tree_only_b,
        node_count_ratio=node_count_ratio, score=score,
    )


def diff_bundles(a: Bundle, b: Bundle) -> BundleDiff:
    sa = [_signature(s) for s in a.steps]
    sb = [_signature(s) for s in b.steps]
    sm = SequenceMatcher(a=sa, b=sb, autojunk=False)

    pairs: list[StepDiff] = []
    matched_a: set[int] = set()
    matched_b: set[int] = set()
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            ia, ib = blk.a + k, blk.b + k
            pairs.append(_step_diff(a.steps[ia], b.steps[ib]))
            matched_a.add(ia)
            matched_b.add(ib)

    unmatched_a = [s.line for i, s in enumerate(a.steps) if i not in matched_a]
    unmatched_b = [s.line for i, s in enumerate(b.steps) if i not in matched_b]

    tjs = [p.tree_jaccard for p in pairs if p.tree_jaccard is not None]
    # A pair with no measurable axis scores 1.0 ("no measurable disagreement");
    # count such pairs so they can't silently pad mean_score.
    unmeasured = sum(
        1 for p in pairs
        if p.role_match is None and p.name_match is None
        and p.selector_jaccard is None and p.tree_jaccard is None)
    density_flags = [
        {"a_line": p.a_line, "b_line": p.b_line, "ratio": p.node_count_ratio}
        for p in pairs
        if p.node_count_ratio is not None and not 0.5 <= p.node_count_ratio <= 2.0
    ]
    summary = {
        "aligned": len(pairs),
        "unmatched_a": len(unmatched_a),
        "unmatched_b": len(unmatched_b),
        "mean_score": sum(p.score for p in pairs) / len(pairs) if pairs else 0.0,
        "mean_tree_jaccard": sum(tjs) / len(tjs) if tjs else None,
        "unmeasured": unmeasured,
        "density_flags": density_flags,
    }
    return BundleDiff(pairs=pairs, unmatched_a=unmatched_a,
                      unmatched_b=unmatched_b, summary=summary)


# --- report ------------------------------------------------------------------

def _flag(v: bool | None) -> str:
    return "✓" if v is True else "✗" if v is False else "-"


def _opt(v: float | None) -> str:
    return f"{v:.2f}" if v is not None else "-"


def print_report(d: BundleDiff, a: Bundle) -> None:
    """Print the compact human-readable diff (one row per pair + summary)."""
    verb_by_line = {s.line: s.verb for s in a.steps}
    for p in d.pairs:
        dens = f"{p.node_count_ratio:.1f}x" if p.node_count_ratio is not None else "-"
        print(f"{p.a_line}->{p.b_line}  {verb_by_line.get(p.a_line) or '-'}  "
              f"role{_flag(p.role_match)}  name{_flag(p.name_match)}  "
              f"sel={_opt(p.selector_jaccard)}  tree={_opt(p.tree_jaccard)}  "
              f"density={dens}")
    if d.unmatched_a:
        print(f"unmatched_a: {d.unmatched_a}")
    if d.unmatched_b:
        print(f"unmatched_b: {d.unmatched_b}")
    s = d.summary
    print(f"aligned={s['aligned']}  unmatched_a={s['unmatched_a']}  "
          f"unmatched_b={s['unmatched_b']}  mean_score={s['mean_score']:.2f}  "
          f"mean_tree_jaccard={_opt(s['mean_tree_jaccard'])}  "
          f"unmeasured={s['unmeasured']}")
    for f in s["density_flags"]:
        print(f"  density {f['a_line']}->{f['b_line']}  ratio={f['ratio']:.1f}x")
