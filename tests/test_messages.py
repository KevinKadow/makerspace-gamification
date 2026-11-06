import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import patch

import pytest

from conftest import oid
from ledger.messages import ChatAPI, Composer, default_template


@contextmanager
def server(payload, status=200):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length']))), self.headers))
            raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def log_message(self, *args):
            pass
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield ChatAPI(f'http://127.0.0.1:{http.server_port}/v1', 'test-model', 'secret'), requests
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


def test_vllm_http_success_and_contract(env):
    _, store, *_ = env
    with server({'choices': [{'message': {'content': 'A worthy achievement.'}, 'finish_reason': 'stop'}]}) as (api, calls):
        result = Composer(store, api).compose('rank_up', 'member', {'member': 'Joe', 'rank': 'Novice'})
    assert result['outcome'] == 'generated'
    assert result['text'] == 'A worthy achievement.'
    path, request, headers = calls[0]
    assert path == '/v1/chat/completions'
    assert request['model'] == 'test-model' and request['stream'] is False
    assert request['chat_template_kwargs'] == {'enable_thinking': False}
    assert request['temperature'] == 0.7 and request['max_tokens'] == 384
    assert [m['role'] for m in request['messages']] == ['system', 'user']
    assert headers['Authorization'] == 'Bearer secret'


@pytest.mark.parametrize('payload,status', [
    (b'not json', 200), ({}, 200), ({'choices': []}, 200),
    ({'choices': [{'message': {'content': ''}, 'finish_reason': 'stop'}]}, 200),
    ({'choices': [{'message': {'content': 'truncated'}, 'finish_reason': 'length'}]}, 200),
    ({'choices': [{'message': {'content': '<think>secret</think>'}, 'finish_reason': 'stop'}]}, 200),
    ({'choices': [{'message': {'content': 'secret</think>Visible reply'}, 'finish_reason': 'stop'}]}, 200),
    ({'choices': [{'message': {'content': None, 'reasoning_content': 'Private reasoning'}, 'finish_reason': 'stop'}]}, 200),
    ({'choices': [{'message': {'content': '<tool_call>award_xp</tool_call>'}, 'finish_reason': 'stop'}]}, 200),
    ({'choices': [{'message': {'content': 'A reply', 'tool_calls': [{'id': 'unwanted'}]}, 'finish_reason': 'stop'}]}, 200),
    ({'error': 'unavailable'}, 503),
])
def test_provider_failure_uses_canned_without_retry(env, payload, status):
    _, store, *_ = env
    with server(payload, status) as (api, calls):
        result = Composer(store, api).compose('kudos', 'nonparticipant', {})
    assert result['outcome'] == 'fallback'
    assert result['text'] == default_template('kudos', 'nonparticipant')['fallback']
    assert len(calls) == 1


def test_vllm_reasoning_fields_never_reach_slack(env):
    _, store, *_ = env
    with server({'choices': [{'message': {'content': 'Welcome, maker.',
                  'reasoning_content': 'Private reasoning', 'reasoning': 'Also private'},
                  'finish_reason': 'stop'}]}) as (api, _):
        result = Composer(store, api).compose('onboarding', 'member', {})
    assert result['text'] == 'Welcome, maker.'
    assert 'private' not in json.dumps(result, default=str).lower()


def test_timeout_template_version_and_audience_isolation(joined):
    l, _, _, composer, api, _ = joined
    template = default_template('kudos', 'shared')
    template.update(fallback='Public thanks.')
    for variation in template['variations']:
        variation['user'] = 'Shared audience facts: {facts}'
    version = composer.publish(str(oid(10)), template, l.admin)
    api.complete.side_effect = TimeoutError('private upstream failure')
    result = composer.compose('kudos', 'shared', {'tool': 'Bandsaw'})
    assert result['text'] == 'Public thanks.' and result['template_version'] == version['_id']
    assert composer.compose('kudos', 'recipient', {})['text'] != 'Public thanks.'
    with pytest.raises(ValueError):
        composer.publish(str(oid(1)), template, l.admin)
