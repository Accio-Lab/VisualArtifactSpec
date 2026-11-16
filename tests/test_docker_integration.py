"""Opt-in real Docker + mock Responses server integration; no live model call."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_judge import fixture
from vas_bench.data import read_json
from vas_bench.judge import Settings, run_case


@unittest.skipUnless(os.environ.get('VAS_TEST_DOCKER') == '1', 'Set VAS_TEST_DOCKER=1 after building the image')
class DockerIntegrationTests(unittest.TestCase):
    def test_http_tools_visual_evidence_readonly_mounts_and_scoring(self):
        requests = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data, task, artifact = fixture(root)
            rows = read_json(data / 'tasks' / task['id'] / 'rubric.json')
            evidence_path = 'evaluation/screenshots/page.png'
            result = {'results': [{'id': row['id'],
                                  'answer': 'Yes' if 'Yes' in row['answers'] else '0',
                                  'reason': 'Synthetic test observation: ' + evidence_path}
                                 for row in rows]}
            index = {'observations': [{'ids': [row['id'] for row in rows],
                                       'files': [evidence_path], 'note': 'Synthetic integration test'}]}
            script = (
                'from pathlib import Path; import json; from pptx import Presentation; '
                'p=Presentation(); p.slides.add_slide(p.slide_layouts[0]); '
                'p.save("evaluation/work/deck.pptx"); '
                f'Path("evaluation/result.json").write_text({json.dumps(result)!r}); '
                f'Path("evaluation/evidence.json").write_text({json.dumps(index)!r})'
            )
            command = '\n'.join([
                'set -eu',
                'test -z "${JUDGE_API_KEY+x}"',
                'if touch artifact/forbidden-write 2>/dev/null; then exit 13; fi',
                'python -c ' + shlex.quote(script),
                'libreoffice -env:UserInstallation=file:///tmp/lo-profile --headless '
                '--convert-to pdf --outdir evaluation/evidence evaluation/work/deck.pptx',
                'python /opt/vas/observe.py pdf evaluation/evidence/deck.pdf '
                'evaluation/screenshots/slide',
                'python /opt/vas/observe.py web file:///work/artifact/index.html ' + evidence_path,
            ])

            class Handler(BaseHTTPRequestHandler):
                def do_POST(self):
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    requests.append(body)
                    step = len(requests)
                    name, args = (
                        ('shell', {'command': command, 'timeout': 120}) if step == 1 else
                        ('view_image', {'path': evidence_path}) if step == 2 else
                        ('finish', {})
                    )
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(json.dumps({'status': 'completed', 'output': [
                        {'type': 'function_call', 'name': name, 'arguments': json.dumps(args),
                         'call_id': f'synthetic-call-{step}'}]}).encode())

                def log_message(self, *args):
                    pass

            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            case = root / 'results' / task['id']
            try:
                with patch.dict(os.environ, {
                    'JUDGE_BASE_URL': f'http://127.0.0.1:{server.server_port}/v1',
                    'JUDGE_API_KEY': 'synthetic-test-key', 'JUDGE_API_KEY_HEADER': 'Authorization'
                }):
                    value = run_case(data, task, artifact, case,
                                     Settings(model='synthetic-test', max_turns=4))
                self.assertEqual(value['unified']['total'], 50)
                self.assertEqual(len(requests), 3)
                output = json.loads(requests[1]['input'][-1]['output'])
                self.assertEqual(output['exit_code'], 0, output)
                images = requests[2]['input'][-1]['output']
                self.assertTrue(images[1]['image_url'].startswith('data:image/png;base64,'))
                self.assertTrue((case / 'evaluation/screenshots/slide-1.png').is_file())
                self.assertEqual(read_json(case / 'run.json')['status'], 'completed')
                self.assertTrue(read_json(case / 'run.json')['sandbox_image_id'].startswith('sha256:'))
                self.assertFalse((artifact / 'forbidden-write').exists())
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
