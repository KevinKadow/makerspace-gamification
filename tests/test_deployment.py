"""Deployment contracts; no GPU, Docker daemon, Slack, or tunnel credentials needed."""
import ast
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from slack_sdk import WebClient
import yaml

from ledger.http import HTTPApp
from ledger.slack_app import SlackUI, build_app
from test_slack import request


ROOT = Path(__file__).parents[1]
MANIFEST = json.loads((ROOT / "slack-manifest.json").read_text(encoding="utf-8"))
COMMANDS = MANIFEST["features"]["slash_commands"]
EVENTS = MANIFEST["settings"]["event_subscriptions"]["bot_events"]


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: c["command"])
def test_manifest_commands_dispatch_through_signed_http(joined, command):
    ledger, store, _, composer, *_ = joined
    ui = SlackUI(ledger, composer)
    app = HTTPApp(build_app(ui, 'xoxb-test', 'test-signing-secret', 'T1', 'UBOT', WebClient(token='xoxb-test')), store)
    body = {'team_id': 'T1', 'user_id': 'U1', 'command': command['command'], 'text': '', 'trigger_id': 'trigger'}
    with patch.object(ui, 'command') as handler:
        assert request(app, body, 'application/x-www-form-urlencoded', path='/slack/commands')[0] == 200
    handler.assert_called_once()
    assert handler.call_args.args[0]['command'] == command['command']
    assert command['url'] == 'https://LEDGER_HOST/slack/commands'
    assert command['should_escape'] is True


@pytest.mark.parametrize("subscription", EVENTS)
def test_manifest_events_are_durably_accepted(joined, subscription):
    ledger, store, _, composer, *_ = joined
    app = HTTPApp(build_app(SlackUI(ledger, composer), 'xoxb-test', 'test-signing-secret', 'T1', 'UBOT', WebClient(token='xoxb-test')), store)
    event_type, _, channel_type = subscription.partition('.')
    event = {'type': event_type, 'user': 'U1', 'channel': 'D1' if channel_type == 'im' else 'CCHAT',
             'channel_type': channel_type, 'text': 'hello', 'ts': '1'}
    if event_type == 'user_change':
        event['user'] = {'id': 'U1', 'deleted': True}
    payload = {'type': 'event_callback', 'team_id': 'T1', 'event_id': 'EvManifest', 'event': event}
    assert request(app, payload)[0] == 200
    assert store.get('ledger_inbox', 'slack:EvManifest')['kind'] == 'slack_event'


def test_manifest_event_coverage_interactions_and_bot_permissions():
    tree = ast.parse((ROOT / 'ledger/slack_app.py').read_text(encoding='utf-8'))
    supported = next(ast.literal_eval(node.value) for node in ast.walk(tree) if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == 'supported' for target in node.targets))
    assert {event.split('.')[0] for event in EVENTS} == supported
    assert {'message.im', 'message.groups'} <= set(EVENTS)
    assert len(COMMANDS) == len({c['command'] for c in COMMANDS}) == 7
    settings = MANIFEST['settings']
    assert settings['event_subscriptions']['request_url'] == 'https://LEDGER_HOST/slack/events'
    assert settings['interactivity'] == {'is_enabled': True, 'request_url': 'https://LEDGER_HOST/slack/interactions',
                                       'message_menu_options_url': 'https://LEDGER_HOST/slack/interactions'}
    assert not settings['socket_mode_enabled'] and not settings['token_rotation_enabled']
    scopes = set(MANIFEST['oauth_config']['scopes']['bot'])
    assert scopes == {'commands', 'app_mentions:read', 'chat:write', 'groups:read', 'groups:history',
                      'groups:write', 'im:write', 'im:history', 'users:read', 'files:write'}
    # Inventory actual SDK calls: new methods require an explicit permission review.
    method_scopes = {
        'users_info': {'users:read'}, 'conversations_info': {'groups:read'},
        'conversations_members': {'groups:read'}, 'conversations_create': {'groups:write'},
        'conversations_invite': {'groups:write'}, 'conversations_kick': {'groups:write'},
        'conversations_open': {'im:write'}, 'files_upload_v2': {'files:write'},
        'chat_postMessage': {'chat:write'}, 'chat_getPermalink': set(),
        'views_open': set(), 'views_update': set(), 'views_publish': set(),
    }
    calls = set()
    for path in (ROOT / 'ledger').glob('*.py'):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and hasattr(WebClient, node.func.attr):
                calls.add(node.func.attr)
    assert calls == set(method_scopes)
    assert all(needed <= scopes for needed in method_scopes.values())


def test_compose_routes_private_inference_and_keeps_cleanup_independent():
    config = yaml.safe_load((ROOT / 'compose.yaml').read_text(encoding='utf-8'))
    services = config['services']
    ai, tunnel = services['ledger-ai'], services['cloudflared']
    assert 'ghcr.io/timothystewart6/vllm-gb10' in ai['image']
    assert ai['platform'] == 'linux/arm64'
    assert ai['deploy']['resources']['reservations']['devices'] == [{'driver': 'nvidia', 'count': 1, 'capabilities': ['gpu']}]
    assert ai['command'][:3] == ['vllm', 'serve', 'nvidia/Qwen3.8-27B-NVFP4']
    assert json.loads(ai['command'][ai['command'].index('--default-chat-template-kwargs') + 1]) == {'enable_thinking': False}
    assert not ai.get('ports') and not tunnel.get('ports')
    assert 'cloudflare/cloudflared' in tunnel['image']
    assert tunnel['command'] == ['tunnel', '--no-autoupdate', 'run']
    assert set(tunnel['environment']) == {'TUNNEL_TOKEN'}
    assert 'env_file' not in ai and 'env_file' not in tunnel
    assert set(tunnel['depends_on']) == {'ledger-web'}
    for name in ('ledger-web', 'ledger-accounting', 'ledger-delivery', 'ledger-channels'):
        service = services[name]
        assert 'env_file' not in service
        assert 'ledger-ai' not in service.get('depends_on', {})
        assert service['environment']['LEDGER_LLM_API_KEY'] == ai['environment']['VLLM_API_KEY']
        assert service['environment']['LEDGER_LLM_MODEL'] == ai['command'][ai['command'].index('--served-model-name') + 1]
        assert 'http://ledger-ai:8000/v1' in service['environment']['LEDGER_LLM_BASE_URL']
        assert 'CLOUDFLARE_TUNNEL_TOKEN' not in service['environment']
