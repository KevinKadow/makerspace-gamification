"""Standalone Slack transport diagnostics; no Ledger services or message writes."""
import json
import logging
import os
import time
from importlib.metadata import version

import websocket
from slack_sdk import WebClient


class CheckFailure(Exception):
    """A fixed, non-sensitive diagnostic code."""


def probe_socket(url, seconds=20):
    """Require Slack hello and repeated matching pongs on one stable connection."""
    socket = websocket.create_connection(url, timeout=10)
    try:
        start = time.monotonic()
        next_ping = start
        pending = {}
        hello = False
        pongs = 0
        sequence = 0
        while time.monotonic() - start < seconds:
            now = time.monotonic()
            if any(now - sent >= 5 for sent in pending.values()):
                raise CheckFailure('pong_timeout')
            if now >= next_ping:
                payload = f'ledger-check-{sequence}'.encode()
                socket.ping(payload)
                pending[payload] = now
                sequence += 1
                next_ping = now + 5
            socket.settimeout(min(1, max(0.01, seconds - (now - start))))
            try:
                opcode, data = socket.recv_data(control_frame=True)
            except websocket.WebSocketTimeoutException:
                continue
            if opcode == websocket.ABNF.OPCODE_CLOSE:
                raise CheckFailure('socket_closed')
            if opcode == websocket.ABNF.OPCODE_PONG and data in pending:
                del pending[data]
                pongs += 1
            if opcode == websocket.ABNF.OPCODE_TEXT:
                message = json.loads(data)
                if message.get('envelope_id'):
                    # Never acknowledge application work that was not persisted.
                    raise CheckFailure('application_envelope_received_stop_other_consumers')
                if message.get('type') == 'disconnect':
                    raise CheckFailure('slack_disconnect')
                hello = hello or message.get('type') == 'hello'
        if not hello:
            raise CheckFailure('missing_slack_hello')
        if pongs < 2 or pending:
            raise CheckFailure('missing_pongs')
        return {'hello': True, 'pongs': pongs, 'observation_seconds': seconds}
    finally:
        socket.close()


def run(*, socket=False):
    result = {'ok': False, 'checks': [], 'versions': {
        package: version(package) for package in ('slack-bolt', 'slack-sdk', 'websocket-client')
    }}
    stage = 'configuration'
    # Keep SDK exception bodies, tokens, signed URLs and payloads out of output.
    logger = logging.Logger('ledger.slack_check', level=logging.CRITICAL + 1)
    logger.addHandler(logging.NullHandler())
    try:
        token = os.environ.get('SLACK_BOT_TOKEN', '').strip()
        team = os.environ.get('SLACK_TEAM_ID', '').strip()
        user = os.environ.get('SLACK_BOT_USER_ID', '').strip()
        if not token or not team or not user:
            raise CheckFailure('requires_bot_token_team_id_and_bot_user_id')
        client = WebClient(token=token, timeout=10, retry_handlers=[], logger=logger)
        stage = 'bot_auth'
        identity = client.auth_test()
        if not identity.get('ok') or not identity.get('bot_id'):
            raise CheckFailure('not_an_authenticated_bot')
        if identity.get('team_id') != team or identity.get('user_id') != user:
            raise CheckFailure('configured_identity_mismatch')
        result['checks'].append({'check': stage, 'ok': True})
        if socket:
            stage = 'app_connection'
            app_token = os.environ.get('SLACK_APP_TOKEN', '').strip()
            if not app_token.startswith('xapp-'):
                raise CheckFailure('requires_app_token')
            connection = client.apps_connections_open(app_token=app_token)
            result['checks'].append({'check': stage, 'ok': True})
            stage = 'socket_hello_and_heartbeat'
            details = probe_socket(connection['url'])
            result['checks'].append({'check': stage, 'ok': True, **details})
        result['ok'] = True
    except Exception as exc:
        result['checks'].append({'check': stage, 'ok': False,
                                 'error': str(exc) if isinstance(exc, CheckFailure) else type(exc).__name__})
    return result
