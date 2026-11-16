"""Validate retained evidence files and their references from rubric judgments.

These checks verify availability and attribution, not the truth of observations.
"""
from pathlib import Path, PurePosixPath

from .data import fingerprint, read_json, sha256
from .scoring import normalize_judgments
from .storage import write_json

EXCLUDED = {"node_modules", ".git", ".venv", "__pycache__", "bbs-runtime", "bbs-artifacts"}
INDEX = 'evaluation/evidence.json'
PROOF = 'trace/evidence-integrity.json'




def regular_file(output, name):
    if not isinstance(name, str):
        raise ValueError('Evidence paths must be strings')
    path = PurePosixPath(name)
    if (path.is_absolute() or '..' in path.parts
            or path.as_posix() != name or any(p in EXCLUDED for p in path.parts)):
        raise ValueError(f'Unsafe evidence path: {name}')
    target = output
    for part in path.parts:
        target = target / part
        if target.is_symlink():
            raise ValueError(f'Evidence must be retained as regular files, not links: {name}')
    if not target.is_file() or target.stat().st_size == 0:
        raise ValueError(f'Missing or empty retained evidence: {name}')
    return target


def snapshot(output, items=None):
    """Validate the Agent index and return hashes of precisely the cited files."""
    output = Path(output)
    doc = read_json(regular_file(output, INDEX))
    if (not isinstance(doc, dict) or set(doc) != {'observations'}
            or not isinstance(doc['observations'], list) or not doc['observations']):
        raise ValueError('Evidence index must contain a nonempty observations list')
    result_path = regular_file(output, 'evaluation/result.json')
    judgments = normalize_judgments(read_json(result_path))
    if not isinstance(judgments, dict):
        raise ValueError('Evidence/result rubric coverage mismatch')
    expected = {i['id'] for i in items} if items is not None else set(judgments)
    if set(judgments) != expected:
        raise ValueError('Evidence/result rubric coverage mismatch')
    covered, paths, item_files = set(), set(), {key: set() for key in expected}
    for row in doc['observations']:
        if not isinstance(row, dict) or set(row) != {'ids', 'files', 'note'}:
            raise ValueError('Each observation requires exactly ids, files and note')
        ids, files = row['ids'], row['files']
        if (not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids)
                or len(ids) != len(set(ids)) or not set(ids) <= expected):
            raise ValueError('Evidence observation has invalid rubric IDs')
        if (not isinstance(row['note'], str) or not row['note'].strip()
                or not isinstance(files, list) or not files):
            raise ValueError('Each observation requires files and a concrete note')
        for name in files:
            if not isinstance(name, str):
                raise ValueError('Evidence paths must be strings')
            path = PurePosixPath(name)
            if len(path.parts) < 3 or path.parts[:2] not in {
                    ('evaluation', 'evidence'), ('evaluation', 'screenshots'), ('evaluation', 'logs')}:
                raise ValueError(f'Evidence must be retained outside evaluation/work/: {name}')
            regular_file(output, name)
            paths.add(name)
        for key in ids:
            item_files[key].update(files)
        covered.update(ids)
    if covered != expected:
        raise ValueError(f'Missing evidence for rubric IDs: {sorted(expected - covered)}')
    for key, files in item_files.items():
        reason = judgments[key].get('reason', '') if isinstance(judgments[key], dict) else ''
        if not isinstance(reason, str) or not any(name in reason for name in files):
            raise ValueError(f'{key}: reason must cite an indexed retained evidence path')
    paths.update({INDEX, 'evaluation/result.json'})
    return {name: sha256(regular_file(output, name)) for name in sorted(paths)}


def seal(output, items):
    files = snapshot(output, items)
    proof = Path(output) / PROOF
    write_json(proof, {'version': 1, 'files': files, 'sha256': fingerprint(files)})
    return sha256(proof)


def verify(output, digest, items=None):
    proof = regular_file(Path(output), PROOF)
    if not digest or sha256(proof) != digest:
        raise ValueError('Evidence integrity receipt mismatch')
    files = snapshot(output, items)
    if read_json(proof) != {'version': 1, 'files': files, 'sha256': fingerprint(files)}:
        raise ValueError('Retained evaluation evidence changed or was lost')
