"""Small Responses-compatible client; credentials stay on the host."""
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class ResponsesClient:
    def __init__(self):
        base = os.environ.get('JUDGE_BASE_URL', 'https://api.openai.com/v1').rstrip('/')
        parts = urlsplit(base)
        if parts.scheme not in {'https', 'http'} or not parts.netloc or parts.query or parts.fragment:
            raise ValueError('JUDGE_BASE_URL must be an API root URL without query or fragment')
        if parts.username or parts.password:
            raise ValueError('Pass Judge credentials through environment variables, not the URL')
        if parts.scheme == 'http' and parts.hostname not in {'localhost', '127.0.0.1', '::1'}:
            raise ValueError('Use HTTPS for remote Judge endpoints')
        key = os.environ.get('JUDGE_API_KEY')
        if not key:
            raise ValueError('Set JUDGE_API_KEY in the host environment')
        if any(character.isspace() for character in key):
            raise ValueError('JUDGE_API_KEY must not contain whitespace')
        header = os.environ.get('JUDGE_API_KEY_HEADER', 'Authorization')
        if header not in {'Authorization', 'api-key'}:
            raise ValueError('JUDGE_API_KEY_HEADER must be Authorization or api-key')
        self.url = base + '/responses'
        self.headers = {'Content-Type': 'application/json',
                        header: 'Bearer ' + key if header == 'Authorization' else key}

    def create(self, payload, timeout):
        request = Request(self.url, json.dumps(payload).encode(), self.headers, method='POST')
        deadline = time.monotonic() + timeout
        for attempt in range(3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Judge request timed out')
            try:
                with urlopen(request, timeout=remaining) as response:
                    result = json.load(response)
                if result.get('status') != 'completed' or not isinstance(result.get('output'), list):
                    raise RuntimeError('Judge response was incomplete or failed')
                return result
            except HTTPError as error:
                if error.code not in {429, 500, 502, 503, 504} or attempt == 2:
                    # Provider error bodies can echo private URLs or authentication data.
                    raise RuntimeError(f'Judge HTTP {error.code}; check endpoint, model and access') from None
                time.sleep(min(2 ** attempt, max(0, deadline - time.monotonic())))
            except URLError:
                raise RuntimeError('Judge connection failed; check endpoint and network') from None
