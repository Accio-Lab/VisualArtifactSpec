"""Offline Judge orchestration tests; no model credentials or Docker needed."""
import json
from pathlib import Path
import tempfile
import unittest

from vas_bench.batch import score_all, score_case, summarize
from vas_bench.aggregation import VERSION
from vas_bench.data import fingerprint, read_json
from vas_bench.judge import Settings, protocol_text, run_case, run_loop
from vas_bench.sandbox import image_output, tree_hash
from vas_bench.storage import write_json


SYNTHETIC = Path(__file__).resolve().parent / 'fixtures'


def write_evaluation(case):
    rows = read_json(SYNTHETIC / 'rubric.json')
    path = 'evaluation/evidence/check.txt'
    (case / path).parent.mkdir(parents=True, exist_ok=True)
    (case / path).write_text('Synthetic test observation; no real artifact evaluated.')
    write_json(case / 'evaluation/result.json', {'results': [
        {'id': row['id'], 'answer': 'Yes' if 'Yes' in row['answers'] else '0',
         'reason': 'Synthetic fixture: ' + path} for row in rows]})
    write_json(case / 'evaluation/evidence.json', {'observations': [
        {'ids': [row['id'] for row in rows], 'files': [path], 'note': 'Synthetic fixture'}]})
    return rows


class FakeClient:
    def __init__(self, names=('shell', 'finish')):
        self.names = iter(names)
        self.requests = []

    def create(self, payload, timeout):
        self.requests.append(json.loads(json.dumps(payload)))
        name = next(self.names)
        return {'status': 'completed', 'id': 'test-response', 'usage': {}, 'output': [
            {'type': 'function_call', 'call_id': f'call{len(self.requests)}',
             'name': name, 'arguments': json.dumps(
                 {'command': 'synthetic fixture', 'timeout': 1} if name == 'shell' else {})}]}


class FakeSandbox:
    def __init__(self, workspace, evaluation, **kwargs):
        self.case = Path(evaluation).parent

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def shell(self, command, timeout):
        write_evaluation(self.case)
        return {'exit_code': 0, 'output': 'Synthetic fixture written', 'truncated': False}


def fixture(root):
    data = root / 'data'
    task = {'id': 'synthetic-001', 'category': 'synthetic', 'artifact_type': 'website'}
    folder = data / 'tasks' / task['id']
    folder.mkdir(parents=True)
    (folder / 'prompt.md').write_text('Synthetic test task')
    (folder / 'platform_instructions.txt').write_text('Synthetic test platform')
    (folder / 'rubric.json').write_bytes((SYNTHETIC / 'rubric.json').read_bytes())
    write_json(folder / 'inputs.manifest.json', {'files': []})
    write_json(data / 'benchmark.json', {'tasks': [task]})
    artifact = root / 'submissions' / task['id'] / 'artifact'
    artifact.mkdir(parents=True)
    (artifact / 'index.html').write_text('<h1>Synthetic test</h1>')
    return data, task, artifact


class JudgeTests(unittest.TestCase):
    def test_task_profile_selects_formal_instructions_and_resume_fingerprint(self):
        for artifact_type in ('website', 'presentation', 'single_page_graphic',
                              'image_series', 'dynamic_media'):
            with self.subTest(artifact_type=artifact_type), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                data, task, artifact = fixture(root)
                task['artifact_type'] = artifact_type
                case = root / 'results' / task['id']
                client = FakeClient()
                settings = Settings(model='synthetic-test')
                run_case(data, task, artifact, case, settings,
                         client_factory=lambda: client, sandbox_factory=FakeSandbox)
                instructions = client.requests[0]['instructions']
                self.assertIn('highest consecutively satisfied level', instructions)
                self.assertIn('Minor harmless imperfections do not automatically lower a grade', instructions)
                if artifact_type == 'website':
                    self.assertIn('1440×900 and 390×844', instructions)
                    self.assertIn('answer every CP/FN No and every quality item -3', instructions)
                    self.assertNotIn('Inspect audio only when required', instructions)
                else:
                    self.assertIn('Inspect audio only when required', instructions)
                    self.assertNotIn('1440×900 and 390×844', instructions)
                    self.assertNotIn('answer every CP/FN No', instructions)
                run = read_json(case / 'run.json')
                self.assertEqual(run['source']['protocol'], fingerprint(protocol_text(artifact_type)))
                run['source']['protocol'] = 'previous-protocol'
                write_json(case / 'run.json', run)
                with self.assertRaisesRegex(ValueError, 'Cannot resume changed or failed case'):
                    run_case(data, task, artifact, case, settings, resume=True)

    def test_unknown_observation_profile_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unknown artifact observation profile'):
            protocol_text('unknown')

    def test_case_completion_resume_and_evidence_tamper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data, task, artifact = fixture(root)
            case = root / 'results' / task['id']
            settings = Settings(model='synthetic-test', max_turns=4)
            kwargs = {'client_factory': FakeClient, 'sandbox_factory': FakeSandbox}
            result = run_case(data, task, artifact, case, settings, **kwargs)
            self.assertEqual(result['unified']['total'], 50)
            self.assertEqual(read_json(case / 'run.json')['status'], 'completed')
            self.assertEqual(run_case(data, task, artifact, case, settings,
                                     resume=True, **kwargs)['unified']['total'], 50)
            (case / 'evaluation/evidence/check.txt').write_text('Changed')
            with self.assertRaises(ValueError):
                run_case(data, task, artifact, case, settings, resume=True, **kwargs)

    def test_invalid_finish_can_be_corrected_and_reasoning_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            case = Path(directory)
            (case / 'trace').mkdir()
            (case / 'evaluation/logs').mkdir(parents=True)
            rows = read_json(SYNTHETIC / 'rubric.json')
            client = FakeClient(('finish', 'shell', 'finish'))
            value, _ = run_loop(case, rows, Settings(model='synthetic-test'),
                                client, FakeSandbox(None, case / 'evaluation'))
            self.assertEqual(value['unified']['total'], 50)
            self.assertEqual(client.requests[0]['reasoning'], {'effort': 'medium'})
            self.assertFalse(client.requests[0]['store'])
            self.assertEqual(client.requests[1]['input'][-1]['type'], 'function_call_output')
            self.assertIn('error', client.requests[1]['input'][-1]['output'])

    def test_failed_case_has_no_score_and_does_not_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data, task, artifact = fixture(root)
            case = root / 'results' / task['id']
            settings = Settings(model='synthetic-test', max_turns=1)
            with self.assertRaises(TimeoutError):
                run_case(data, task, artifact, case, settings,
                         client_factory=FakeClient, sandbox_factory=FakeSandbox)
            self.assertFalse((case / 'score.json').exists())
            self.assertEqual(read_json(case / 'run.json')['status'], 'failed')
            self.assertTrue((case / 'evaluation/error.json').exists())
            with self.assertRaises(ValueError):
                run_case(data, task, artifact, case, settings, resume=True)

    def test_input_hash_mismatch_fails_before_model_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data, task, artifact = fixture(root)
            folder = data / 'tasks' / task['id']
            (folder / 'inputs').mkdir()
            (folder / 'inputs/input.txt').write_text('Changed attachment')
            write_json(folder / 'inputs.manifest.json', {'files': [
                {'path': 'input.txt', 'sha256': '0' * 64, 'bytes': 1}]})
            with self.assertRaisesRegex(ValueError, 'fingerprint mismatch'):
                run_case(data, task, artifact, root / 'results/case', Settings(model='test'))

    def test_unsafe_sources_and_image_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'link').symlink_to('/etc/passwd')
            with self.assertRaises(ValueError):
                tree_hash(root)
            with self.assertRaises(ValueError):
                image_output(root, '../secret.png')
            folder = root / 'evaluation/screenshots'
            folder.mkdir(parents=True)
            (folder / 'image.png').symlink_to('/etc/passwd')
            with self.assertRaises(ValueError):
                image_output(root, 'evaluation/screenshots/image.png')

    def test_score_all_distinguishes_invalid_and_missing_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data, task, _ = fixture(root)
            extra = {**task, 'id': 'synthetic-002'}
            write_json(data / 'benchmark.json', {'tasks': [task, extra]})
            case = root / 'results' / task['id']
            write_evaluation(case)
            result = score_all(data, root / 'results', root / 'summary.json')
            self.assertEqual((result['completed'], result['failed'], result['missing']), (1, 0, 1))
            self.assertEqual(result['mean'], 50)
            write_json(case / 'evaluation/result.json', {'results': []})
            result = score_all(data, root / 'results', root / 'summary.json')
            self.assertEqual((result['completed'], result['failed'], result['missing']), (0, 1, 1))
            self.assertIsNone(result['mean'])
            self.assertFalse((case / 'score.json').exists())

    def test_overall_mean_is_by_task_not_category(self):
        tasks = [{'id': str(i), 'category': 'A' if i < 2 else 'B', 'artifact_type': 'website'}
                 for i in range(3)]
        rows = {str(i): {'status': 'completed', 'total': score}
                for i, score in enumerate([0, 0, 90])}
        self.assertEqual(summarize(tasks, rows)['mean'], 30)

    def test_reaggregate_old_linear_score_without_rejudging(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            data,task,_=fixture(root)
            case=root/'results'/task['id']
            rows=write_evaluation(case)
            judgments=read_json(case/'evaluation/result.json')
            for row in judgments['results']:
                if row['answer']=='0':row['answer']='1'
            write_json(case/'evaluation/result.json',judgments)
            historical=score_case(case,rows)
            historical['schema_version']=1
            historical['unified'].update(version='unified-211-signed-50-50-group-all-pass-v2',
                                         quality=50/3,total=50+50/3)
            write_json(case/'score.json',historical)
            original=(case/'evaluation/result.json').read_bytes()
            result=score_all(data,root/'results',root/'summary.json')
            self.assertEqual(result['scoring_version'],VERSION)
            self.assertEqual(result['mean'],60)
            self.assertEqual(read_json(case/'score.json')['unified']['version'],VERSION)
            self.assertEqual((case/'evaluation/result.json').read_bytes(),original)


if __name__ == '__main__':
    unittest.main()
