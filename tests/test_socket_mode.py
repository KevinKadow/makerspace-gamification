"""Socket transport lifecycle and real Bolt envelope dispatch, without Slack IO."""
import logging
from unittest.mock import Mock, patch

import pytest
import yaml
from slack_bolt.adapter.socket_mode.websocket_client import SocketModeHandler
from slack_sdk import WebClient
from slack_sdk.socket_mode.request import SocketModeRequest

from ledger import server
from ledger.http import HTTPApp
from ledger.slack_app import SlackUI, build_app
from test_deployment import ROOT
from test_slack import request


def dispatch(ui, payload, kind='slash_commands'):
    handler = object.__new__(SocketModeHandler)
    handler.app = build_app(ui, 'xoxb-test', '', 'T1', 'UBOT', WebClient(token='xoxb-test'))
    client = Mock(logger=logging.getLogger('socket-test'))
    handler.handle(client, SocketModeRequest(type=kind, envelope_id='envelope', payload=payload))
    return client


def test_socket_commands_interactions_and_workspace_guard(joined):
    ledger, _, _, composer, *_ = joined
    ui = SlackUI(ledger, composer)
    payload = {'team_id': 'T1', 'user_id': 'U1', 'command': '/kudos', 'text': '', 'trigger_id': 'trigger'}
    with patch.object(ui, 'command') as command:
        client = dispatch(ui, payload)
        command.assert_called_once()
        assert client.send_socket_mode_response.call_args.args[0].envelope_id == 'envelope'
        command.reset_mock()
        dispatch(ui, {**payload, 'team_id': 'WRONG'}).send_socket_mode_response.assert_not_called()
        command.assert_not_called()
    with patch.object(ui, 'submission', return_value={}) as submission:
        client = dispatch(ui, {'type': 'view_submission', 'team': {'id': 'T1'},
                              'user': {'id': 'U1'}, 'view': {'type': 'modal', 'callback_id': 'dismiss'}}, 'interactive')
        submission.assert_called_once()
        client.send_socket_mode_response.assert_called_once()


def test_socket_event_replay_and_failed_persistence_are_not_acknowledged(joined):
    ledger, store, _, composer, *_ = joined
    ui = SlackUI(ledger, composer)
    payload = {'type': 'event_callback', 'team_id': 'T1', 'event_id': 'socket-event',
               'event': {'type': 'message', 'user': 'U1', 'channel': 'D1', 'text': 'hello', 'ts': '1'}}
    for _ in range(2):
        dispatch(ui, payload, 'events_api').send_socket_mode_response.assert_called_once()
    assert len(store.select('ledger_inbox', {'_id': 'slack:socket-event'})) == 1
    with patch.object(store, 'atomic', side_effect=RuntimeError('unavailable')):
        dispatch(ui, {**payload, 'event_id': 'failed'}, 'events_api').send_socket_mode_response.assert_not_called()
    assert not store.get('ledger_inbox', 'slack:failed')


def test_socket_health_disables_http_ingress_and_checks_connection():
    app = HTTPApp(Mock(), Mock(), Mock(), callbacks_enabled=False)
    def get(path):
        statuses = []
        body = b''.join(app({'PATH_INFO': path, 'REQUEST_METHOD': 'GET'},
                            lambda status, headers: statuses.append(status)))
        return statuses[0], body
    app.transport_ready = lambda: False
    assert get('/health')[0] == '200 OK'
    assert get('/ready')[0] == '503 Service Unavailable'
    app.transport_ready = lambda: True
    assert get('/ready')[0] == '200 OK'
    assert request(app, {}, path='/slack/commands')[0] == 404
    app.bolt.dispatch.assert_not_called()


def test_websocket_transport_reconnects_and_readiness_tracks_new_session():
    # Exercise the real adapter with network IO and background threads replaced.
    from slack_sdk.socket_mode import websocket_client

    web_client = Mock(spec=WebClient)
    web_client.apps_connections_open.side_effect = [
        {'url': 'wss://example.invalid/first'},
        {'url': 'wss://example.invalid/second'},
    ]
    app = Mock(client=web_client, logger=logging.getLogger('socket-test'))
    first, second = Mock(sock=None), Mock(sock=None)
    with patch.object(websocket_client.IntervalRunner, 'start', lambda runner: runner), \
            patch.object(websocket_client.websocket, 'WebSocketApp', side_effect=[first, second]) as sockets:
        handler = server.SocketModeHandler(app, 'xapp-test')
        try:
            handler.connect()
            assert not handler.client.is_connected()
            first.sock = Mock()
            assert handler.client.is_connected()
            first.sock = None
            assert not handler.client.is_connected()
            sockets.call_args.kwargs['on_close'](first, 1006, None)
            assert web_client.apps_connections_open.call_count == 2
            first.close.assert_called_once()
            assert not handler.client.is_connected()
            second.sock = Mock()
            assert handler.client.is_connected()
        finally:
            handler.close()
        second.close.assert_called_once()
        assert not handler.client.auto_reconnect_enabled


def test_default_entrypoint_preserves_gunicorn(monkeypatch):
    monkeypatch.delenv('SLACK_SOCKET_MODE', raising=False)
    with patch.object(server.os, 'execvp') as execute, patch.object(server, 'serve_socket_mode') as socket:
        server.main()
        execute.assert_called_once_with('gunicorn', server.HTTP_COMMAND)
        socket.assert_not_called()


def test_enabled_entrypoint_uses_one_socket_consumer(monkeypatch):
    monkeypatch.setenv('SLACK_SOCKET_MODE', 'true')
    with patch.object(server.os, 'execvp') as execute, patch.object(server, 'serve_socket_mode') as socket:
        server.main()
        socket.assert_called_once_with()
        execute.assert_not_called()


def test_socket_app_does_not_require_signing_secret(joined, monkeypatch):
    from ledger.cli import make_app
    ledger, _, _, composer, *_ = joined
    slack = WebClient(token='xoxb-test')
    for key, value in {'SLACK_BOT_TOKEN': 'xoxb-test', 'SLACK_TEAM_ID': 'T1', 'SLACK_BOT_USER_ID': 'UBOT'}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('SLACK_SIGNING_SECRET', raising=False)
    with patch('ledger.cli.dependencies', return_value=(ledger, composer, slack)):
        app = make_app(socket_mode=True)
        assert app.callbacks_enabled is False
        with pytest.raises(KeyError, match='SLACK_SIGNING_SECRET'):
            make_app()


@pytest.mark.parametrize('failure', [False, True])
def test_socket_lifecycle_closes_on_shutdown_or_startup_failure(monkeypatch, failure):
    monkeypatch.setenv('SLACK_APP_TOKEN', 'xapp-test')
    app, handler = Mock(), Mock()
    if failure:
        handler.connect.side_effect = RuntimeError('unavailable')
    with patch('ledger.cli.make_app', return_value=app) as factory, \
            patch.object(server, 'SocketModeHandler', return_value=handler) as adapter, \
            patch.object(server, 'make_server') as http:
        if failure:
            with pytest.raises(RuntimeError):
                server.serve_socket_mode()
        else:
            server.serve_socket_mode()
            http.return_value.__enter__.return_value.serve_forever.assert_called_once()
        factory.assert_called_once_with(socket_mode=True)
        adapter.assert_called_once_with(app.bolt, 'xapp-test')
        handler.close.assert_called_once()


def test_socket_configuration_fails_closed_and_token_stays_in_web(monkeypatch):
    monkeypatch.setenv('SLACK_SOCKET_MODE', 'typo')
    with pytest.raises(ValueError, match='SLACK_SOCKET_MODE'):
        server.main()
    monkeypatch.setenv('SLACK_SOCKET_MODE', 'true')
    monkeypatch.delenv('SLACK_APP_TOKEN', raising=False)
    with pytest.raises(ValueError, match='SLACK_APP_TOKEN'):
        server.main()
    services = yaml.safe_load((ROOT/'compose.yaml').read_text())['services']
    assert services['ledger-web']['environment']['SLACK_SOCKET_MODE'] == '${SLACK_SOCKET_MODE:-false}'
    assert [name for name, service in services.items() if 'SLACK_APP_TOKEN' in service.get('environment', {})] == ['ledger-web']
