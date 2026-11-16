"""Verify the actual HTTP client without calling a paid model endpoint."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import unittest
from unittest.mock import patch

from vas_bench.provider import ResponsesClient


class ProviderTests(unittest.TestCase):
    def test_http_contract_authentication_and_incomplete_responses(self):
        requests = []
        responses = [(200, {'status': 'completed', 'output': []}),
                     (200, {'status': 'incomplete', 'output': []}),
                     (401, {'error': 'Do not echo this private server message'})]

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append((self.path, self.headers.get('api-key'),
                                 json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                status, body = responses.pop(0)
                self.send_response(status)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(os.environ, {
                'JUDGE_BASE_URL': f'http://127.0.0.1:{server.server_port}/v1',
                'JUDGE_API_KEY': 'synthetic-test-key', 'JUDGE_API_KEY_HEADER': 'api-key'
            }):
                client = ResponsesClient()
                self.assertEqual(client.create({'model': 'synthetic-test'}, 5)['output'], [])
                with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                    client.create({}, 5)
                with self.assertRaisesRegex(RuntimeError, '^Judge HTTP 401;'):
                    client.create({}, 5)
            self.assertEqual(requests[0], ('/v1/responses', 'synthetic-test-key',
                                          {'model': 'synthetic-test'}))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_remote_http_is_rejected(self):
        with patch.dict(os.environ, {'JUDGE_BASE_URL': 'http://example.com/v1'}):
            with self.assertRaisesRegex(ValueError, 'HTTPS'):
                ResponsesClient()
