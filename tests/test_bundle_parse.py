from pathlib import Path

import pytest

from showAndTell.bundles.parse import Bundle, format_metadata, parse_bundle

BUNDLE = Path(__file__).parent / "data" / "bundle_mini"


@pytest.fixture
def bundle() -> Bundle:
    return parse_bundle(BUNDLE)


def step(bundle: Bundle, line: int):
    return next(item for item in bundle.steps if item.line == line)


def test_current_bundle_title_steps_and_ordering(bundle):
    assert bundle.title == "Example Part Triage Demo"
    assert [item.line for item in bundle.steps] == [6, 8, 10, 15, 17]


def test_current_double_quoted_trace_calls_are_extracted(bundle):
    goto = step(bundle, 6)
    assert (goto.verb, goto.page, goto.selector) == ("goto", "page", None)
    assert goto.value == "https://example-drive.local/home"
    assert goto.screenshot == "screenshot_line6.png"

    click = step(bundle, 8)
    assert (click.verb, click.page, click.selector, click.value) == (
        "click", "page", "button#open-part", None,
    )

    fill = step(bundle, 10)
    assert (fill.verb, fill.page, fill.selector, fill.value) == (
        "fill", "page2", 'input[title="Enter transaction code"]', "/nm",
    )


def test_current_metadata_reads_json_selectors_target_and_tree(bundle):
    current = step(bundle, 8)
    assert current.selectors == [
        "button#open-part", '//div[3]/button[@id="open-part"]',
    ]
    assert current.frame_url == "https://example-part.local/app"
    assert (current.target.role, current.target.name, current.target.node_id) == (
        "textbox", "Mfr Part Number", "10300",
    )
    tree = current.trees[0]
    assert (tree.frame_id, tree.node_count, len(tree.nodes)) == ("main", 512, 4)


def test_target_null_is_none(bundle):
    assert step(bundle, 10).target is None


def test_tree_node_parse_preserves_depth_colons_empty_names_and_negative_ids(bundle):
    root, colon, empty, inline = step(bundle, 15).trees[0].nodes
    assert (root.depth, root.role, root.name) == (0, "RootWebArea", "Example Sheet")
    assert (colon.depth, colon.role, colon.name) == (1, "cell", "Region: West")
    assert (empty.depth, empty.role, empty.name) == (1, "cell", "")
    assert (inline.depth, inline.role, inline.id) == (
        2, "InlineTextBox", "-1000000002",
    )


def test_current_recorder_action_shapes(tmp_path):
    (tmp_path / "traces.js").write_text(
        '\n'.join([
            'await page.goto("https://example.test/a");  // Screenshot: screenshot_line1.png',
            'await page.click("button\\\"quoted");  // Screenshot: screenshot_line2.png',
            'await page.fill("#name", "Ada");  // Screenshot: screenshot_line3.png',
            'await page.selectOption("#tier", "gold");  // Screenshot: screenshot_line4.png',
            'await page.press("#name", "Enter");  // Screenshot: screenshot_line5.png',
            'await page.keyboard.type("hello");  // Screenshot: screenshot_line6.png',
            'await page2.bringToFront();  // Screenshot: screenshot_line7.png',
            'await page.locator("#from").dragTo(page.locator("#to"));  // Screenshot: screenshot_line8.png',
            '// beat: visible | Screenshot: screenshot_line9.png',
        ]) + '\n'
    )

    parsed = parse_bundle(tmp_path)
    actions = {item.line: item for item in parsed.steps}
    assert (actions[1].verb, actions[1].value) == ("goto", "https://example.test/a")
    assert (actions[2].verb, actions[2].selector) == ("click", 'button"quoted')
    assert (actions[3].verb, actions[3].selector, actions[3].value) == (
        "fill", "#name", "Ada",
    )
    assert (actions[4].verb, actions[4].selector, actions[4].value) == (
        "select", "#tier", "gold",
    )
    assert (actions[5].verb, actions[5].selector, actions[5].value) == (
        "press", "#name", "Enter",
    )
    assert (actions[6].verb, actions[6].value) == ("type", "hello")
    assert (actions[7].verb, actions[7].page) == ("tab_switch", "page2")
    assert (actions[8].verb, actions[8].selector, actions[8].value) == (
        "drag", "#from", "#to",
    )
    assert actions[9].verb is None


def test_writer_reader_roundtrip_uses_current_json_contract(tmp_path):
    from showAndTell.bundles.parse import serialize_tree

    entries = [
        ("1", 0, "RootWebArea", "Title: with colon"),
        ("2", 1, "button", ""),
        ("-1000000002", 1, "InlineTextBox", "leaf text"),
    ]
    metadata = {
        "frameUrl": "http://fixture.local/x",
        "selectors": ["#save", 'role=button[name="Save"]'],
        "targetElement": {"role": "button", "name": "Save", "nodeId": None},
        "trees": [{
            "frameId": "main",
            "nodeCount": len(entries),
            "timestamp": 0,
            "tree": serialize_tree(entries),
        }],
    }
    (tmp_path / "metadata").mkdir()
    (tmp_path / "metadata/line1.md").write_text(format_metadata(1, metadata))

    [current] = parse_bundle(tmp_path).steps
    assert current.screenshot == "screenshot_line1.png"
    assert current.selectors == metadata["selectors"]
    assert (current.target.role, current.target.name) == ("button", "Save")
    assert [(node.id, node.depth, node.role, node.name)
            for node in current.trees[0].nodes] == entries


def test_missing_node_count_uses_parsed_tree_size(tmp_path):
    metadata = {"trees": [{"tree": "[1] RootWebArea: Sheet\n  [2] button: Save"}]}
    (tmp_path / "metadata").mkdir()
    (tmp_path / "metadata/line1.md").write_text(format_metadata(1, metadata))
    [current] = parse_bundle(tmp_path).steps
    assert current.trees[0].node_count == 2


def test_metadata_only_partial_step_is_tolerated(tmp_path):
    (tmp_path / "metadata").mkdir()
    (tmp_path / "metadata/line3.md").write_text("# Line 3\n\n## Unknown\nignored\n")
    [current] = parse_bundle(tmp_path).steps
    assert current.line == 3
    assert current.screenshot is None and current.trees == []


def test_missing_bundle_directory_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="bundle directory"):
        parse_bundle(tmp_path / "nope")


def test_cli_module_is_runnable():
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "showAndTell.cli", "bundle-diff", "x", "y"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert result.returncode != 0
    assert "bundle directory" in (result.stderr + result.stdout)
