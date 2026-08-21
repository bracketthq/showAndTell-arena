from pathlib import Path

import pytest

from showAndTell.bundles.diff import diff_bundles, selector_tokens
from showAndTell.bundles.parse import AXNode, AXTree, Bundle, Step, Target, parse_bundle

MINI = Path(__file__).parent / "data" / "bundle_mini"
MINI_B = Path(__file__).parent / "data" / "bundle_mini_b"


@pytest.fixture
def a() -> Bundle:
    return parse_bundle(MINI)


@pytest.fixture
def b() -> Bundle:
    return parse_bundle(MINI_B)


def pair(diff, a_line: int):
    return next(p for p in diff.pairs if p.a_line == a_line)


# --- selector_tokens ---------------------------------------------------------

def test_selector_tokens_id_class_attr():
    assert selector_tokens(["button#open-part"]) == {"#open-part"}
    assert selector_tokens(["strong.DNoYtb"]) == {".DNoYtb"}
    assert selector_tokens(['input[title="Enter transaction code"]']) == {
        'title="Enter transaction code"'}
    assert selector_tokens(['[name="okcode"]']) == {'name="okcode"'}


def test_selector_tokens_compound_css_chain():
    assert selector_tokens(["div.card#main .row.active"]) == {
        ".card", "#main", ".row", ".active"}


def test_selector_tokens_xpath_id_extraction():
    assert selector_tokens(['//*[@id="ToolbarOkCode"]']) == {"#ToolbarOkCode"}
    # positional segments contribute nothing; the id predicate still does
    assert selector_tokens(['//div[3]/button[@id="open-part"]']) == {"#open-part"}
    assert selector_tokens(['//input[@title="Enter transaction code"]']) == {
        'title="Enter transaction code"'}


def test_selector_tokens_pure_positional_xpath_is_empty():
    assert selector_tokens(["//div[3]/div[1]/span[2]"]) == set()
    assert selector_tokens([""]) == set()


# --- self-diff ---------------------------------------------------------------

def test_self_diff_is_perfect(a):
    d = diff_bundles(a, a)
    assert d.unmatched_a == [] and d.unmatched_b == []
    assert [p.a_line for p in d.pairs] == [6, 8, 10, 15, 17]
    assert all(p.score == 1.0 for p in d.pairs)
    assert d.summary["density_flags"] == []
    assert d.summary["mean_score"] == 1.0


# --- divergent sibling: each planted divergence lands in the right metric ----

def test_alignment_and_unmatched(a, b):
    d = diff_bundles(a, b)
    assert [(p.a_line, p.b_line) for p in d.pairs] == [
        (6, 6), (8, 8), (10, 10), (15, 15)]
    assert d.unmatched_a == [17]      # A-only step
    assert d.unmatched_b == [19]      # B-only step
    assert d.summary["aligned"] == 4


def test_name_rework_flips_name_match(a, b):
    p = pair(diff_bundles(a, b), 8)
    assert p.role_match is True        # role unchanged
    assert p.name_match is False       # target name reworded
    assert p.selector_jaccard == 1.0   # selectors untouched on this step


def test_selector_id_changed_and_class_shared(a, b):
    d = diff_bundles(a, b)
    p10 = pair(d, 10)                  # id changed on the SAP step
    assert "#ToolbarOkCode" in p10.selector_only_a
    assert "#ToolbarCode2" in p10.selector_only_b
    assert p10.selector_jaccard < 1.0
    p15 = pair(d, 15)                  # class shared on the sheet step
    assert ".DNoYtb" in p15.selector_shared


def test_tree_divergence(a, b):
    p = pair(diff_bundles(a, b), 15)
    assert p.tree_jaccard is not None and p.tree_jaccard < 1.0
    assert "cell: region east" in p.tree_only_b   # planted extra node
    assert "cell: region west" in p.tree_only_a   # planted missing node


def test_density_flag(a, b):
    d = diff_bundles(a, b)
    p = pair(d, 15)
    assert p.node_count_ratio == pytest.approx(3.0)   # ~3x node_count
    flags = d.summary["density_flags"]
    assert flags == [{"a_line": 15, "b_line": 15, "ratio": pytest.approx(3.0)}]


def test_inline_textbox_excluded_from_tree_set(a, b):
    # bundle_mini line 15 carries an InlineTextBox "PN-TEST-0001"; it must not
    # leak into the tree divergence strings.
    p = pair(diff_bundles(a, b), 15)
    assert not any("InlineTextBox" in s for s in p.tree_only_a + p.tree_only_b)


# --- score None-axis handling ------------------------------------------------

def _pseudo(root: str, extra: list[tuple[str, str]]) -> Bundle:
    """A target-less (verb=None, target=None) pseudo-step with one tree whose
    root node is `root` plus `extra` (role, name) named nodes."""
    nodes = [AXNode(id="1", depth=0, role="RootWebArea", name=root)]
    nodes += [AXNode(id=str(i + 2), depth=1, role=r, name=n)
              for i, (r, n) in enumerate(extra)]
    tree = AXTree(frame_id="main", node_count=len(nodes), nodes=nodes)
    step = Step(line=1, verb=None, target=None, trees=[tree])
    return Bundle(path=Path("."), title="", steps=[step])


def test_targetless_steps_score_from_tree_alone():
    a = _pseudo("Same Page", [("generic", "Alpha"), ("generic", "Beta")])
    b = _pseudo("Same Page", [("generic", "Alpha"), ("generic", "Gamma")])
    d = diff_bundles(a, b)
    assert len(d.pairs) == 1 and not d.unmatched_a and not d.unmatched_b
    p = d.pairs[0]
    # target-less: role/name/selectors contribute nothing
    assert p.role_match is None and p.name_match is None
    assert p.selector_jaccard is None
    # {root, Alpha} shared over {root, Alpha, Beta, Gamma}
    assert p.tree_jaccard == pytest.approx(0.5)
    assert p.score == pytest.approx(0.5)   # score is the lone tree axis


def test_summary_counts_unmeasured_pairs():
    """A pair aligned by title with no measurable axis scores 1.0 but must be
    surfaced in summary['unmeasured'] so it cannot silently pad mean_score."""
    from showAndTell.bundles.parse import AXNode, AXTree, Bundle, Step

    def screen_only(line):
        tree = AXTree(frame_id="main", node_count=1,
                      nodes=[AXNode(id="1", depth=0, role="RootWebArea", name="Same Screen")])
        return Step(line=line, trees=[tree])

    a = Bundle(path=Path("a"), title="", steps=[screen_only(1)])
    b = Bundle(path=Path("b"), title="", steps=[screen_only(1)])
    d = diff_bundles(a, b)
    # tree axis IS measurable here (both have trees), so unmeasured == 0 ...
    assert d.summary["unmeasured"] == 0

    a2 = Bundle(path=Path("a"), title="", steps=[Step(line=1)])
    b2 = Bundle(path=Path("b"), title="", steps=[Step(line=1)])
    d2 = diff_bundles(a2, b2)
    # ... while treeless/targetless/selectorless pairs are counted.
    assert d2.summary["unmeasured"] == 1
    assert d2.pairs[0].score == 1.0


def test_tree_jaccard_on_empty_named_node_sets_is_perfect_and_measured():
    """Review-pinned: trees present but with no NAMED nodes (chrome-less pages)
    give tree_jaccard 1.0 — a measured axis, not an unmeasured pair."""
    from showAndTell.bundles.parse import AXNode, AXTree, Bundle, Step

    def bare(line):
        tree = AXTree(frame_id="main", node_count=1,
                      nodes=[AXNode(id="1", depth=0, role="RootWebArea", name="")])
        return Step(line=line, trees=[tree])

    d = diff_bundles(Bundle(path=Path("a"), title="", steps=[bare(1)]),
                     Bundle(path=Path("b"), title="", steps=[bare(1)]))
    [p] = d.pairs
    assert p.tree_jaccard == 1.0
    assert d.summary["unmeasured"] == 0


def test_one_sided_selectors_are_unmeasured_not_zero():
    """Review-pinned: a pseudo-bundle captures no selectors, so a real-vs-pseudo
    pair must show sel as unmeasured (None), not a score-dragging 0.0."""
    from showAndTell.bundles.parse import AXNode, AXTree, Bundle, Step

    tree = AXTree(frame_id="main", node_count=1,
                  nodes=[AXNode(id="1", depth=0, role="RootWebArea", name="Same")])
    real = Step(line=1, selectors=["#ToolbarOkCode"], trees=[tree])
    pseudo = Step(line=1, trees=[tree])
    d = diff_bundles(Bundle(path=Path("a"), title="", steps=[real]),
                     Bundle(path=Path("b"), title="", steps=[pseudo]))
    [p] = d.pairs
    assert p.selector_jaccard is None
    assert p.score == 1.0  # tree axis alone, undragged


def test_both_empty_selector_sets_stay_unmeasured():
    """Review-pinned DECISION: two selector-less steps (goto beats, pseudo
    bundles) keep sel unmeasured (None), NOT 1.0 — an empty selectors list
    means capture capability is unknown; absence of evidence is not agreement."""
    from showAndTell.bundles.parse import AXNode, AXTree, Bundle, Step

    tree = AXTree(frame_id="main", node_count=1,
                  nodes=[AXNode(id="1", depth=0, role="RootWebArea", name="S")])
    d = diff_bundles(Bundle(path=Path("a"), title="", steps=[Step(line=1, trees=[tree])]),
                     Bundle(path=Path("b"), title="", steps=[Step(line=1, trees=[tree])]))
    assert d.pairs[0].selector_jaccard is None


# --- _root_name screen identity ----------------------------------------------

def _step_with(nodes: list[tuple[str, str]]) -> Step:
    """A step with one tree of (role, name) nodes (depth ignored by _root_name)."""
    axn = [AXNode(id=str(i), depth=0 if i == 0 else 1, role=r, name=n)
           for i, (r, n) in enumerate(nodes)]
    return Step(line=1, trees=[AXTree(frame_id="", node_count=len(axn), nodes=axn)])


def test_root_name_rootwebarea_node0_wins():
    """Chromium-captured trees put the page title on the first root node."""
    from showAndTell.bundles.diff import _root_name
    s = _step_with([("RootWebArea", "Create Purchase Requisition"),
                    ("main", "Some Other Main")])
    assert _root_name(s) == "Create Purchase Requisition"
