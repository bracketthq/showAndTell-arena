"""Regression coverage for release privacy and explicit workspace configuration."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    'check_public_repo', Path(__file__).resolve().parents[1] / 'scripts/check_public_repo.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


@pytest.mark.parametrize('name', ['source.xlsx', '.env.local', 'backup.bundle',
                                  'runs/result.json', 'assets/run-a-task.gif'])
def test_private_artifacts_rejected(name):
    with pytest.raises(AssertionError):
        check.check_file(name, b'example')


def test_private_deployment_reference_rejected():
    host = 'https://' + 'workspace.prod-' + '123.app.example/'
    with pytest.raises(AssertionError, match='Private reference'):
        check.check_file('adapter.py', host.encode())


def test_public_attribution_allowed():
    check.check_file('README.md', b'https://github.com/bracketthq/showAndTell-arena')


def test_brackett_workspace_must_be_configured(monkeypatch):
    from showAndTell.students.brackett import brackett_url
    monkeypatch.delenv('SHOWANDTELL_BRACKETT_URL', raising=False)
    with pytest.raises(RuntimeError, match='Set SHOWANDTELL_BRACKETT_URL'):
        brackett_url()
    monkeypatch.setenv('SHOWANDTELL_BRACKETT_URL', '  https://workspace.example/  ')
    assert brackett_url() == 'https://workspace.example/'


@pytest.mark.parametrize('name', ['README.md', 'site/data/notes.csv', 'adapter.py'])
def test_internal_references_rejected(name):
    batch = 'brackett-' + 'continuous-third-set-test'
    run = 'Run ' + 'a' * 32
    path = 'apps/ui/base-ui/' + 'src/tokens/tokens.css'
    for text in [batch, run, path]:
        with pytest.raises(AssertionError, match='Private reference'):
            check.check_file(name, text.encode())


def test_note_sanitization_preserves_grading_and_failure_evidence():
    from showAndTell.publication import publication_comment
    batch = 'brackett-' + 'continuous-third-set-test'
    note = ('Earlier attempt, excluded from overview. ' + batch + '; '
            'graded by Claude Sonnet 4.6 (noncanonical); imported grade. '
            'Run ' + 'a' * 32 + '. Workbook corrupted; pre-fix score was 59.50%.')
    clean = publication_comment(note)
    assert clean == ('Earlier attempt, excluded from overview. '
                     'graded by Claude Sonnet 4.6 (noncanonical); imported grade. '
                     'Workbook corrupted; pre-fix score was 59.50%.')
    assert publication_comment(clean) == clean
    assert publication_comment('Refusal. Run ID: example') == 'Refusal.'


def test_archive_location_removed_without_removing_adverse_result():
    from showAndTell.publication import publication_comment
    note = 'Their verdict FAIL; workbook corrupted. Video is in the Codex root aggregate ' + 'archive.'
    assert publication_comment(note) == 'Their verdict FAIL; workbook corrupted. Video is retained separately.'
