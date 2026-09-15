"""Publication-safe research notes without changing evaluation evidence."""
from __future__ import annotations

import re

# Identifiers are operational provenance, not part of the public scoring record.
BATCH_REFERENCE = re.compile(r'\bbrackett-continuous-[a-z0-9-]+\b', re.I)
RUN_REFERENCE = re.compile(
    r'\bRun(?:\s+ID\s*:)?\s+(?:[a-f0-9]{20,}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})\b',
    re.I,
)
ARCHIVE_REFERENCE = re.compile(r'\b(?:Codex\s+)?root aggregate ' + r'archive\b', re.I)
INTERNAL_SOURCE = re.compile(
    r'\bapps/ui/(?:base-ui|web)/' + r'src/\S+'
    r'|\bbracketthq/' + r'brackett(?=[\]/\s"\x27]|$)', re.I)


def private_publication_reference(text: str) -> bool:
    return any(pattern.search(text) for pattern in (
        BATCH_REFERENCE, RUN_REFERENCE, ARCHIVE_REFERENCE, INTERNAL_SOURCE))


def publication_comment(value: str) -> str:
    """Remove internal IDs and archive labels, retaining all substantive caveats."""
    note = str(value or '')
    note = re.sub(BATCH_REFERENCE.pattern + r'\s*;\s*', '', note, flags=re.I)
    note = BATCH_REFERENCE.sub('', note)
    note = RUN_REFERENCE.sub('', note)
    note = re.sub(r'\bRun\s+ID\s*:\s*[\w-]+', '', note, flags=re.I)
    # Older short Run IDs also occur in excluded workbook rows.
    note = re.sub(r'\.\s*Run\s+[a-f0-9]{3,}\s*$', '', note, flags=re.I)
    note = re.sub(r'Video is in the (?:Codex\s+)?root aggregate ' + r'archive\.',
                  'Video is retained separately.', note, flags=re.I)
    note = re.sub(r'\s+([.;])', r'\1', note)
    note = re.sub(r'\.\s*\.', '.', note)
    return note.strip()
