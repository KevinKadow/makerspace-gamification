import json
from unittest.mock import Mock, patch

import pytest
import websocket

from ledger import slack_check


@pytest.fixture
def client(monkeypatch):
    for key, value in {'SLACK_BOT_TOKEN': 'xoxb-secret', 'SLACK_TEAM_ID': 'T1',
                       'SLACK_BOT_USER_ID': 'U1', 'SLACK_APP_TOKEN': 'xapp-secret'}.items():
        monkeypatch.setenv(key, value)
    with patch.object(slack_check, 'WebClient') as factory:
        client = factory.return_value
        client.auth_test.return_value = {'ok': True, 'team_id': 'T1', 'user_id': 'U1', 'bot_id': 'B1'}
        client.apps_connections_open.return_value = {'url': 'wss://example.invalid/secret'}
        yield client


def test_bot_only_never_opens_socket(client):
    assert slack_check.run()['ok']
    client.apps_connections_open.assert_not_called()


def test_wrong_identity_stops_before_socket(client):
    client.auth_test.return_value['team_id'] = 'WRONG'
    result = slack_check.run(socket=True)
    assert not result['ok']
    assert result['checks'][-1]['error'] == 'configured_identity_mismatch'
    client.apps_connections_open.assert_not_called()


def test_socket_failure_is_reported_without_secrets(client):
    with patch.object(slack_check, 'probe_socket', side_effect=BrokenPipeError('xapp-secret signed-url')):
        result = slack_check.run(socket=True)
    assert not result['ok']
    assert result['checks'][-1]['error'] == 'BrokenPipeError'
    assert 'secret' not in json.dumps(result)


def test_missing_configuration_never_calls_slack(client, monkeypatch):
    monkeypatch.delenv('SLACK_TEAM_ID')
    assert not slack_check.run()['ok']
    client.auth_test.assert_not_called()


@pytest.mark.parametrize('mode, error', [
    ('healthy', None), ('close', 'socket_closed'),
    ('no_hello', 'missing_slack_hello'), ('no_pong', 'pong_timeout'),
    ('wrong_pong', 'pong_timeout'),
    ('envelope', 'application_envelope_received_stop_other_consumers'),
    ('broken_pipe', 'BrokenPipeError'),
])
def test_socket_probe_requires_hello_and_stable_roundtrips(mode, error):
    clock = [0]
    socket = Mock()
    pings = []
    socket.ping.side_effect = lambda data: pings.append(data)
    def receive(**kwargs):
        clock[0] += 1
        if mode == 'broken_pipe':
            raise BrokenPipeError('private')
        if mode == 'close':
            return websocket.ABNF.OPCODE_CLOSE, b''
        if mode == 'envelope':
            return websocket.ABNF.OPCODE_TEXT, '{"envelope_id":"private"}'
        if clock[0] == 1 and mode != 'no_hello':
            return websocket.ABNF.OPCODE_TEXT, '{"type":"hello"}'
        if pings and mode != 'no_pong':
            return websocket.ABNF.OPCODE_PONG, pings.pop(0) if mode != 'wrong_pong' else b'other'
        raise websocket.WebSocketTimeoutException()
    socket.recv_data.side_effect = receive
    with patch.object(slack_check.websocket, 'create_connection', return_value=socket), \
            patch.object(slack_check.time, 'monotonic', side_effect=lambda: clock[0]):
        if error:
            with pytest.raises((slack_check.CheckFailure, BrokenPipeError)) as failure:
                slack_check.probe_socket('wss://example.invalid')
            assert (type(failure.value).__name__ if mode == 'broken_pipe' else str(failure.value)) == error
        else:
            result = slack_check.probe_socket('wss://example.invalid')
            assert result == {'hello': True, 'pongs': 4, 'observation_seconds': 20}
    socket.close.assert_called_once()
    socket.send.assert_not_called()


@pytest.mark.parametrize('ok, code', [(True, 0), (False, 1)])
def test_cli_has_no_database_dependency(ok, code, capsys):
    from ledger.cli import main
    with patch('sys.argv', ['ledger', 'slack-check', '--socket']), \
            patch.object(slack_check, 'run', return_value={'ok': ok}) as run, \
            patch('ledger.cli.dependencies', side_effect=AssertionError('must not initialize services')):
        with pytest.raises(SystemExit) as exit:
            main()
    assert exit.value.code == code
    assert json.loads(capsys.readouterr().out) == {'ok': ok}
    run.assert_called_once_with(socket=True)
