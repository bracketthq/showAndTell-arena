import csv
import math

import pytest

from showAndTell.benchmark_charts import load_dataset, main, parse_rows, score_value, summarize


def test_na_is_failed_attempt_but_blank_is_untested_and_numeric_zero_is_completed():
    assert score_value("N/A") == (0, False)
    assert score_value(" na ") == (0, False)
    assert score_value(0) == (0, True)
    assert score_value("") is None
    assert score_value(None) is None
    assert score_value("86.3%") == pytest.approx((.863, True))
    for value in [True, -1, 86, math.nan, math.inf, "failed"]:
        with pytest.raises(ValueError):
            score_value(value)


def test_partial_completion_and_equal_case_weighting():
    data = parse_rows([
        ["Usecase", "Agent", "Score"],
        ["A", "Brackett", 1], ["A", "Brackett", "NA"],
        ["B", "Brackett", .8],
        ["A", "Claude", 0], ["B", "Claude", ""],
        ["Unused", "Claude", ""],
    ])
    report = summarize(data)
    assert report["agents"]["Brackett"]["average_score"] == pytest.approx(.65)
    assert report["agents"]["Brackett"]["completion_rate"] == pytest.approx(2/3)
    assert report["agents"]["Claude"]["completion_rate"] == 1
    assert report["shared_cases"] == ["A"]
    assert report["agents"]["Brackett"]["shared_score"] == .5
    assert report["untested_cases"] == ["Unused"]


def test_wide_csv_and_excel_match_and_ignore_cached_averages(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    table = [
        ["Usecase ", "Avg. Brackett Score", "Run 1", "Run 2", "Avg. Claude Score", "Run 1", "Run 2"],
        ["A", .99, 1, 1, .8, .8, "NA"],
        ["B", .99, .5, .5, "NA", "NA", "NA"],
        ["Untested", None, None, None, None, None, None],
        ["Final Score"], ["Brackett", .99],
    ]
    csv_path = tmp_path / "wide.csv"
    with csv_path.open("w", newline="") as stream:
        csv.writer(stream).writerows(table)
    book = openpyxl.Workbook()
    book.active.title = "Overview"
    for row in table:
        book.active.append(row)
    xlsx_path = tmp_path / "data.xlsx"
    book.save(xlsx_path)
    assert load_dataset(csv_path) == load_dataset(xlsx_path)
    report = summarize(load_dataset(xlsx_path))
    assert report["agents"]["Claude"]["average_score"] == .2
    assert report["agents"]["Claude"]["completion_rate"] == .25
    with pytest.raises(ValueError, match="Sheet"):
        load_dataset(xlsx_path, "Missing")


def test_duplicate_runs_and_invalid_scores_rejected():
    with pytest.raises(ValueError, match="duplicate run"):
        parse_rows([["Usecase", "Agent", "Score", "Run Number"],
                    ["A", "Claude", .5, 1], ["A", "Claude", .6, 1]])
    with pytest.raises(ValueError, match="Row 2, Score"):
        parse_rows([["Usecase", "Agent", "Score"], ["A", "Claude", 75]])
    with pytest.raises(ValueError, match="run columns are required"):
        parse_rows([["Usecase", "Avg. Claude Score"], ["A", .5]])


def test_no_shared_cases_is_unavailable_not_zero():
    report = summarize(parse_rows([["Usecase", "Agent", "Score"],
                                  ["A", "Brackett", 1], ["B", "Claude", "NA"]]))
    assert report["shared_cases"] == []
    assert all(a["shared_score"] is None for a in report["agents"].values())


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_cli_renders_csv_without_shared_cases(tmp_path, theme):
    pytest.importorskip("matplotlib")
    source = tmp_path / "input.csv"
    source.write_text("Usecase,Agent,Score\nA,Brackett,1\nB,Claude,NA\n")
    output = tmp_path / theme
    assert main([str(source), "--output-dir", str(output), "--theme", theme]) == 0
    assert (output / "benchmark-overview.png").read_bytes().startswith(b"\x89PNG")
    assert "<svg" in (output / "benchmark-overview.svg").read_text()
    assert (output / "use-case-scores-1.png").is_file()
    assert (output / "summary.json").is_file()
