"""Check tracked release files for private artifacts and deployment references."""
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from showAndTell.publication import private_publication_reference
PRIVATE_SUFFIXES = {'.xlsx', '.xls', '.xlsm', '.xlsb', '.pem', '.key', '.p12', '.pfx', '.bundle'}
PRIVATE_REFERENCES = re.compile(
    r'https?://[^\s/"\x27]*\.prod-\d+\.app\.[^\s/"\x27]+'
    r'|https?://[^\s/"\x27]+\.slack\.com/'
    r'|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', re.I)


def check_file(name: str, content: bytes) -> None:
    path = Path(name)
    assert path.suffix.lower() not in PRIVATE_SUFFIXES, f'Private artifact: {name}'
    assert not (path.name == '.env' or path.name.startswith('.env.')
                and path.name != '.env.example'), f'Environment credentials: {name}'
    assert not any(p in {'runs', '.venv', '.git'} for p in path.parts), f'Local data: {name}'
    assert path.name != 'run-a-task.gif', f'Unreviewed account recording: {name}'
    if b'\0' not in content:
        text = unquote(content.decode('utf-8', errors='replace'))
        assert not (PRIVATE_REFERENCES.search(text) or private_publication_reference(text)), (
            f'Private reference: {name}')


def main():
    names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    checked = 0
    for name in filter(None, names):
        path = ROOT / name
        if path.is_file():
            check_file(name, path.read_bytes())
            checked += 1
    print(f'Public repository checks passed ({checked} tracked files).')


if __name__ == '__main__':
    main()
