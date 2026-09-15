"""Keep private source files and references out of the Pages artifact."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('check_site', ROOT / 'scripts/check_site.py')
check_site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_site)


@pytest.mark.parametrize('extension', ['xlsx', 'xls', 'xlsm', 'xlsb'])
def test_bundled_workbook_stops_publication(tmp_path, extension):
    (tmp_path / f'accidental-copy.{extension}').write_bytes(b'private input')
    with pytest.raises(AssertionError, match='workbook must not be bundled'):
        check_site.check_private_sources(tmp_path)


@pytest.mark.parametrize('reference', [
    '../showtellarena/docs/source.xlsx',
    '../showtellarena',
    '~/repos/showtellarena/main.pdf',
    '/Users/example/repos/showtellarena/docs/source.xlsx',
    'https://github.com/bracketthq/showtellarena/blob/main/main.tex',
    'https://github.com/bracketthq/showtellarena.git',
    '..%2Fshowtellarena%2Fdocs%2Fsource.xlsx',
])
def test_private_reference_stops_publication(tmp_path, reference):
    (tmp_path / 'README.md').write_text(reference, encoding='utf-8')
    with pytest.raises(AssertionError, match='Private source reference'):
        check_site.check_private_sources(tmp_path)


def test_source_name_and_public_dataset_are_allowed(tmp_path):
    (tmp_path / 'README.md').write_text(
        'Brackett ShowTell Benchmark Results.xlsx\n'
        'https://huggingface.co/datasets/brackettai/showtellarena/tree/main\n'
        'https://github.com/bracketthq/showAndTell-arena\n'
        '~/repos/showtellarena-bench/site\n', encoding='utf-8')
    check_site.check_private_sources(tmp_path)


@pytest.mark.parametrize('suffix', ['csv', 'json', 'html'])
def test_internal_run_reference_stops_site_upload(tmp_path, suffix):
    (tmp_path / f'notes.{suffix}').write_text('Run ' + 'a' * 32, encoding='utf-8')
    with pytest.raises(AssertionError, match='Private source reference'):
        check_site.check_private_sources(tmp_path)


def test_internal_specification_stops_site_upload(tmp_path):
    (tmp_path / 'Internal_Build_Specification.docx').write_bytes(b'internal document')
    with pytest.raises(AssertionError, match='Internal build specification'):
        check_site.check_private_sources(tmp_path)
