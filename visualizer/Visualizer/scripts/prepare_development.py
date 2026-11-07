"""Build a hash allowlist from one existing development-only fit record.

No circuit files, label tables, audit manifests, or corpus directories are opened.
"""
import argparse
import hashlib
import json
from pathlib import Path


def prepare(record_path: Path, output_path: Path):
    payload = json.loads(record_path.read_text(encoding='utf-8'))
    if payload.get('operation') != 'fit_development':
        raise ValueError('Expected an existing fit_development input record')
    rows = payload.get('rows', [])
    if not rows:
        raise ValueError('The development record contains no rows')
    entries = {}
    for row in rows:
        if type(row.get('fold')) is not int or row['fold'] not in range(5):
            raise ValueError('Every row must belong to a development fold 0–4; audit rows are forbidden')
        group = row.get('group', '')
        if len(group) != 64 or any(c not in '0123456789abcdef' for c in group):
            raise ValueError('Expected decompressed-content SHA-256 hashes in group')
        entries[group] = {'origin': 'development', 'name': Path(row['filename']).name}
    result = {'scope': 'development_only', 'source': str(record_path.resolve()),
              'record_sha256': hashlib.sha256(record_path.read_bytes()).hexdigest(), 'circuits': entries}
    output_path.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    return len(entries)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--record', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(f'Approved {prepare(args.record, args.output)} development content hashes. No circuit corpus or audit labels were opened.')
