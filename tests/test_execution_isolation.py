import pytest


def test_codex_record_has_no_worker_flag(capsys, monkeypatch):
    import sys

    from showAndTell.cli import main

    monkeypatch.setattr(sys, "argv", ["showAndTell", "codex-record", "--help"])
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 0
    help_text = capsys.readouterr().out
    assert "--worker" not in help_text
    assert "--display" not in help_text
    assert "--app-port" not in help_text
    assert "--fixture-port" not in help_text
