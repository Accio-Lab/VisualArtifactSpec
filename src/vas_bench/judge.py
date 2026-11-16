"""Tool-using Judge loop with a local Docker workspace and strict completion."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import time

from .batch import score_case, select_tasks, summarize
from . import __version__
from .data import fingerprint, read_json, sha256
from .evidence import verify
from .provider import ResponsesClient
from .sandbox import DockerSandbox, image_output, tree_hash
from .storage import write_json


def tool(name, description, properties):
    return {'type': 'function', 'name': name, 'description': description, 'strict': True,
            'parameters': {'type': 'object', 'properties': properties,
                           'required': list(properties), 'additionalProperties': False}}


TOOLS = [
    tool('shell', 'Run a Linux shell command in /work. Preserve evidence. '
         'Use evaluation/work/ for builds and writable copies; artifact/ and inputs/ are read-only.',
         {'command': {'type': 'string'}, 'timeout': {'type': 'integer'}}),
    tool('view_image', 'Inspect an actual retained PNG/JPEG/WebP image. Render screenshots, '
         'pages or video frames first; use this tool for visual judgments.',
         {'path': {'type': 'string'}}),
    tool('finish', 'Validate evaluation/result.json and evaluation/evidence.json and compute '
         'the score. Call only after observing every criterion and retaining its evidence.', {}),
]

RUNTIME = '''
Local runtime: your tools execute in /work inside an isolated Docker container.
Read prompt.md, platform_instructions.txt, rubric.json and inputs/. Inspect artifact/.
Linux tools include Python, Node/npm, Playwright Chromium, LibreOffice, pdftoppm,
ffmpeg and image/document parsing libraries. Python Playwright examples are available
at /opt/vas/observe.py: use --help. You may write scripts for richer interactions.
Copy artifact/ to evaluation/work/artifact before builds or startup; never repair it.
The network policy and resource limits are recorded by the harness, not product requirements.
Use shell to retain evidence and write the result and evidence index. Use view_image
to inspect actual rendered visual evidence. Tool logs alone cannot prove visual quality.
Finally call finish. Validation errors must be corrected using actual observations.
If observation cannot be completed, write evaluation/error.json and call finish to
report the failure. Do not guess judgments or insert zero scores.
'''


@dataclass(frozen=True)
class Settings:
    model: str
    reasoning: str = 'medium'
    image: str = 'vas-judge:local'
    network: str = 'none'
    timeout: int = 3600
    command_timeout: int = 120
    api_timeout: int = 300
    max_turns: int = 100
    max_output_tokens: int = 16384


def stamp():
    return datetime.now(timezone.utc).isoformat()


def protocol_text(artifact_type='single_page_graphic'):
    # Installed wheels carry the protocol too; no dependency on the checkout cwd.
    text = (Path(__file__).parent / 'judge_protocol.md').read_text(encoding='utf-8')
    sections = {}
    for section in text.split('\n## ')[1:]:
        heading, body = section.split('\n', 1)
        sections[heading] = body.strip()
    if artifact_type not in {'website', 'presentation', 'single_page_graphic',
                             'image_series', 'dynamic_media'}:
        raise ValueError('Unknown artifact observation profile: ' + str(artifact_type))
    headings = ['Common rules', 'Web Interfaces observation' if artifact_type == 'website'
                else 'Other artifacts observation', 'Item judgments']
    if artifact_type == 'website':
        headings.append('Web Interfaces failure rule')
    headings.append('Results and evidence')
    return '\n\n'.join(sections[heading] for heading in headings) + '\n'


def run_loop(case, rows, settings, client, sandbox, *, artifact_type='single_page_graphic'):
    case = Path(case)
    instructions = protocol_text(artifact_type) + '\n' + RUNTIME
    history = [{'role': 'user', 'content': 'Evaluate the submission now. Read all task files. '
                f'Network policy: {settings.network}. Call finish when ready.'}]
    deadline = time.monotonic() + settings.timeout
    trace = case / 'trace/events.jsonl'
    usage = []

    def record(value):
        with trace.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'time': stamp(), **value}, ensure_ascii=False) + '\n')

    for turn in range(settings.max_turns):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Evaluation time budget exhausted')
        response = client.create({
            'model': settings.model, 'reasoning': {'effort': settings.reasoning},
            'instructions': instructions, 'input': history, 'tools': TOOLS,
            'parallel_tool_calls': False, 'store': False,
            'include': ['reasoning.encrypted_content'],
            'max_output_tokens': settings.max_output_tokens,
        }, timeout=min(settings.api_timeout, remaining))
        history.extend(response['output'])
        usage.append(response.get('usage', {}))
        record({'turn': turn, 'response_id': response.get('id'),
                'output': response['output'], 'usage': usage[-1]})
        calls = [item for item in response['output'] if item['type'] == 'function_call']
        if not calls:
            history.append({'role': 'user', 'content': 'Continue using tools; call finish to complete.'})
        for call in calls:
            if deadline <= time.monotonic():
                raise TimeoutError('Evaluation time budget exhausted')
            try:
                args = json.loads(call['arguments'])
                if not isinstance(args, dict):
                    raise ValueError('Tool arguments must be an object')
                if call['name'] == 'shell':
                    if set(args) != {'command', 'timeout'} or type(args['timeout']) is not int:
                        raise ValueError('shell requires command and integer timeout')
                    limit = min(args['timeout'], settings.command_timeout,
                                max(1, int(deadline - time.monotonic())))
                    output = sandbox.shell(args['command'], limit)
                    log = case / 'evaluation/logs' / f'tool-{turn:04d}-{len(history):05d}.json'
                    if log.parent.is_symlink() or not log.parent.is_dir():
                        raise ValueError('evaluation/logs must remain a regular directory')
                    write_json(log, {'command': args['command'], **output})
                    output = json.dumps({**output, 'retained_log': log.relative_to(case).as_posix()})
                elif call['name'] == 'view_image':
                    if set(args) != {'path'}:
                        raise ValueError('view_image requires path')
                    output = image_output(case, args['path'])
                    record({'viewed_image': args['path'], 'sha256': sha256(case / args['path'])})
                elif call['name'] == 'finish':
                    if args:
                        raise ValueError('finish takes no arguments')
                    if (case / 'evaluation/error.json').exists():
                        raise RuntimeError('Judge reported blocked observation in evaluation/error.json')
                    value = score_case(case, rows)
                    return value, usage
                else:
                    raise ValueError('Unknown tool: ' + call['name'])
            except (ValueError, OSError, KeyError, TypeError) as error:
                output = json.dumps({'error': str(error)})
            history.append({'type': 'function_call_output', 'call_id': call['call_id'],
                            'output': output})
    raise TimeoutError('Judge turn budget exhausted before validated completion')


def run_case(data, task, artifact, case, settings, *, resume=False,
             client_factory=ResponsesClient, sandbox_factory=DockerSandbox):
    data, artifact, case = Path(data), Path(artifact), Path(case)
    key = task['id']
    task_dir = data / 'tasks' / key
    rows = read_json(task_dir / 'rubric.json')
    source = {'task': tree_hash(task_dir), 'artifact': tree_hash(artifact),
              'protocol': fingerprint(protocol_text(task['artifact_type'])), 'settings': asdict(settings)}
    if case.exists():
        if not resume:
            raise ValueError(f'Output case already exists: {case}; use --resume or a new output directory')
        previous = read_json(case / 'run.json')
        if previous.get('status') != 'completed' or previous.get('source') != source:
            raise ValueError(f'Cannot resume changed or failed case: {key}; use a new output directory')
        value = read_json(case / 'score.json')
        verify(case, value.get('evidence_integrity_sha256'), rows)
        # Recompute rather than trusting a manually edited score.json.
        return score_case(case, rows)
    case.mkdir(parents=True, exist_ok=False)
    run = {'task_id': key, 'status': 'running', 'started_at': stamp(), 'source': source,
           'harness_version': __version__, 'resources': {'cpus': 2, 'memory': '4g'}}
    write_json(case / 'run.json', run)
    try:
        workspace = case / 'workspace'
        workspace.mkdir()
        for name in ('prompt.md', 'platform_instructions.txt', 'rubric.json'):
            shutil.copy2(task_dir / name, workspace / name)
        shutil.copytree(artifact, workspace / 'artifact')
        if tree_hash(workspace / 'artifact') != source['artifact']:
            raise ValueError('Artifact changed while staging the workspace')
        if (task_dir / 'inputs').exists():
            shutil.copytree(task_dir / 'inputs', workspace / 'inputs')
        else:
            manifest = read_json(task_dir / 'inputs.manifest.json')
            if manifest['files']:
                raise ValueError('Task inputs are missing; download the full Hugging Face dataset')
            (workspace / 'inputs').mkdir()
        # Check bytes, not just input-manifest syntax.
        manifest = read_json(task_dir / 'inputs.manifest.json')
        for entry in manifest['files']:
            name = Path(entry['path'])
            if name.is_absolute() or '..' in name.parts:
                raise ValueError('Unsafe input manifest path')
            path = workspace / 'inputs' / name
            if sha256(path) != entry['sha256']:
                raise ValueError('Input attachment fingerprint mismatch: ' + str(name))
        (workspace / 'evaluation').mkdir()
        for name in ('work', 'evidence', 'screenshots', 'logs'):
            (case / 'evaluation' / name).mkdir(parents=True)
        (case / 'trace').mkdir()
        client = client_factory()
        with sandbox_factory(workspace, case / 'evaluation', image=settings.image,
                             network=settings.network) as sandbox:
            run['sandbox_image_id'] = getattr(sandbox, 'image_id', None)
            write_json(case / 'run.json', run)
            value, usage = run_loop(case, rows, settings, client, sandbox,
                                    artifact_type=task['artifact_type'])
        verify(case, value['evidence_integrity_sha256'], rows)
        if tree_hash(artifact) != source['artifact'] or tree_hash(task_dir) != source['task']:
            raise ValueError('Original task or artifact changed during evaluation')
        run.update(status='completed', finished_at=stamp(), usage=usage)
        write_json(case / 'run.json', run)
        return value
    except Exception as error:
        run.update(status='failed', finished_at=stamp(), error=str(error))
        write_json(case / 'run.json', run)
        # A failed run must never leave a score that looks valid to batch consumers.
        (case / 'score.json').unlink(missing_ok=True)
        write_json(case / 'evaluation/error.json', {'error': str(error)})
        raise


def judge_all(data, artifacts, output, settings, *, task_ids=None, workers=6, resume=False):
    data, artifacts, output = Path(data).resolve(), Path(artifacts).resolve(), Path(output).resolve()
    if (output.is_relative_to(data) or output.is_relative_to(artifacts)
            or data.is_relative_to(output) or artifacts.is_relative_to(output)):
        raise ValueError('Output, dataset and submission trees must be separate')
    if workers < 1:
        raise ValueError('workers must be positive')
    tasks = select_tasks(data, task_ids)
    records = {}
    output.mkdir(parents=True, exist_ok=True)
    # Fail on credentials before creating any cases; credentials are never mounted.
    ResponsesClient()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for task in tasks:
            key = task['id']
            artifact = artifacts / key / 'artifact'
            if not artifact.is_dir():
                records[key] = {'status': 'missing'}
                continue
            futures[pool.submit(run_case, data, task, artifact, output / key,
                                settings, resume=resume)] = key
            records[key] = {'status': 'running'}
        for future in as_completed(futures):
            key = futures[future]
            try:
                value = future.result()
                records[key] = {'status': 'completed', 'total': value['unified']['total']}
            except Exception as error:
                records[key] = {'status': 'failed', 'error': str(error)}
            print(json.dumps({'task_id': key, **records[key]}), flush=True)
            write_json(output / 'summary.json', summarize(tasks, records))
    result = summarize(tasks, records)
    write_json(output / 'summary.json', result)
    return result
