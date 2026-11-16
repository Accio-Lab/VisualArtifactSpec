"""Recompute case scores and report failures separately from valid scores."""
from collections import defaultdict
from pathlib import Path

from .data import read_json
from .evidence import regular_file, seal, verify
from .scoring import score
from .storage import write_json
from .aggregation import VERSION


def select_tasks(data, task_ids=None):
    catalog = read_json(Path(data) / 'benchmark.json')
    tasks = catalog['tasks']
    ids = [task['id'] for task in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate task IDs in catalog')
    for key in ids:
        if not isinstance(key, str) or Path(key).name != key or key in {'.', '..'}:
            raise ValueError('Unsafe task ID in catalog')
    selected = set(task_ids or ids)
    if not selected <= set(ids):
        raise ValueError('Unknown task IDs: ' + ', '.join(sorted(selected - set(ids))))
    return [task for task in tasks if task['id'] in selected]


def score_case(case, rows):
    case = Path(case)
    if (case / 'evaluation/error.json').exists():
        raise ValueError('Judge recorded an evaluation error; do not score this case')
    if (case / 'score.json').exists():
        previous = read_json(case / 'score.json')
        if not isinstance(previous, dict):
            raise ValueError('Existing score.json must contain an object')
        if previous.get('evidence_integrity_sha256'):
            verify(case, previous['evidence_integrity_sha256'], rows)
    judgments = read_json(regular_file(case, 'evaluation/result.json'))
    result = score(rows, judgments)
    if 'unified' not in result:
        raise ValueError('Expected a VAS rubric with the unified score contract')
    digest = seal(case, rows)
    result['evidence_integrity_sha256'] = digest
    write_json(case / 'score.json', result)
    return result


def summarize(tasks, records):
    categories = defaultdict(list)
    valid = []
    for task in tasks:
        row = records.get(task['id'], {'status': 'missing'})
        if row['status'] == 'completed':
            valid.append(row['total'])
            categories[task.get('category', task['artifact_type'])].append(row['total'])
    return {
        'scoring_version': VERSION,
        'expected': len(tasks), 'completed': len(valid),
        'failed': sum(r['status'] == 'failed' for r in records.values()),
        'running': sum(r['status'] == 'running' for r in records.values()),
        'missing': sum(task['id'] not in records or records[task['id']]['status'] == 'missing'
                       for task in tasks),
        'mean': sum(valid) / len(valid) if valid else None,
        'categories': {key: {'count': len(values), 'mean': sum(values) / len(values)}
                       for key, values in sorted(categories.items())},
        'tasks': records,
    }


def score_all(data, results, output, task_ids=None):
    data, results, output = Path(data), Path(results), Path(output)
    if output.resolve().is_relative_to(data.resolve()):
        raise ValueError('Summary output must not overwrite dataset files')
    tasks = select_tasks(data, task_ids)
    if any(output.resolve().is_relative_to((results / task['id']).resolve()) for task in tasks):
        raise ValueError('Summary output must not overwrite a case directory')
    records = {}
    for task in tasks:
        key = task['id']
        case = results / key
        if not case.exists():
            records[key] = {'status': 'missing'}
            continue
        try:
            value = score_case(case, read_json(data / 'tasks' / key / 'rubric.json'))
            records[key] = {'status': 'completed', 'total': value['unified']['total']}
        except (OSError, ValueError, KeyError, TypeError) as error:
            try:
                (case / 'score.json').unlink(missing_ok=True)
            except OSError:
                pass  # A malformed case directory must not block the remaining cases.
            records[key] = {'status': 'failed', 'error': str(error)}
    summary = summarize(tasks, records)
    write_json(output, summary)
    return summary
