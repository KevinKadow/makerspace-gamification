import io
import json
import time
from urllib.parse import urlencode
from unittest.mock import patch

import pytest
from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier

from conftest import oid
from ledger import views
from ledger.http import HTTPApp
from ledger.slack_app import SlackUI, build_app


def form(view, fields, user='U1'):
    state = {}
    for block in view['blocks']:
        if block['type'] != 'input':
            continue
        element = block['element']
        name, kind = element['action_id'], element['type']
        value = fields.get(name)
        if kind in ('static_select', 'external_select'):
            obj = {'type': kind, 'selected_option': views.option(value, value) if value else None}
        elif kind == 'checkboxes':
            obj = {'type': kind, 'selected_options': [views.option('yes', 'yes')] if value else []}
        else:
            obj = {'type': kind, 'value': value or ''}
        state[block['block_id']] = {name: obj}
    return {'user': {'id': user}, 'view': {**view, 'id': 'V1', 'hash': 'h1', 'state': {'values': state}}}


def test_recipient_first_warning_choice_and_draft_race(joined):
    l, s, _, comp, _, slack = joined
    ui = SlackUI(l, comp)
    ui.command({'user_id': 'U1', 'command': '/kudos', 'trigger_id': 'T', 'text': ''}, slack)
    first = slack.views_open.call_args.kwargs['view']
    assert len(first['blocks']) == 1 and first['blocks'][0]['element']['action_id'] == 'recipient'
    reply = ui.submission(form(first, {'recipient': str(oid(3))}), slack)
    detail = reply['view']
    assert 'will not earn XP' in json.dumps(detail)
    with pytest.raises(ValueError, match='Choose whether'):
        ui.submission(form(detail, {'message': 'Thanks'}), slack)
    detail = views.kudos_form(l, str(oid(2)), 'race')
    l.leave(str(oid(2)))
    updated = ui.submission(form(detail, {'message': '*Keep this* :smile:', 'public': True}), slack)['view']
    message = next(b for b in updated['blocks'] if b.get('block_id') == 'message')
    assert message['element']['initial_value'] == '*Keep this* :smile:'
    assert next(b for b in updated['blocks'] if b.get('block_id') == 'public')['element']['initial_options']
    assert 'invitation' in json.dumps(updated)
    assert s.get('ledger_evidence', 'kudos:race') is None


def test_dependent_tool_reset_and_mrkdwn_authoring(joined):
    l, _, _, comp, _, slack = joined
    ui = SlackUI(l, comp)
    old = views.kudos_form(l, str(oid(2)), 'fields', {'shop': str(oid(201)), 'tool': str(oid(311))})
    body = form(old, {'shop': str(oid(202)), 'tool': str(oid(311)), 'message': '*Verbatim*', 'public': True})
    body['actions'] = [{'action_id': 'shop'}]
    ui.action(body, slack)
    updated = slack.views_update.call_args.kwargs['view']
    tool = next(b for b in updated['blocks'] if b.get('block_id', '').startswith('tool:'))
    assert tool['block_id'] == 'tool:' + str(oid(202))
    assert 'initial_option' not in tool['element']
    assert next(b for b in updated['blocks'] if b.get('block_id') == 'message')['element']['initial_value'] == '*Verbatim*'
    # Clearing the shop removes the tool and preserves the other draft fields.
    body = form(updated, {'message': '*Verbatim*', 'public': True})
    body['actions'] = [{'action_id': 'shop'}]
    ui.action(body, slack)
    assert not any(b.get('block_id', '').startswith('tool:') for b in slack.views_update.call_args.kwargs['view']['blocks'])


def test_rank_forms_preview_and_publish_keep_pinned_version(joined):
    l, s, _, comp, _, slack = joined
    ui = SlackUI(l, comp)
    view = views.ranks_form(s.get('ledger_rulesets', 'initial'))
    fields = {}
    for rank in s.get('ledger_rulesets', 'initial')['ranks']:
        i = rank['slot']
        fields.update({f'name_{i}': rank['name'], f'emoji_{i}': rank['emoji'], f'floor_{i}': rank['floor'],
                       f'gates_{i}': json.dumps(rank['requirements']), f'enabled_{i}': rank['enabled']})
    fields.update(name_2='Explorer', emoji_2=':custom:', floor_2='400')
    preview = ui.submission(form(view, fields, 'U10'), slack)['view']
    assert 'Explorer' in json.dumps(preview)
    ui.submission(form(preview, {}, 'U10'), slack)
    assert l.presentation(2)['emoji'] == ':custom:'
    assert l.participant(str(oid(1)))['ruleset'] == 'initial'
    assert s.get('ledger_channels', 'rank:2')['channel_id'] == 'CRANK2'


def request(app, payload, content_type='application/json', timestamp=None, signature=True, path='/slack/events'):
    raw = json.dumps(payload) if content_type == 'application/json' else urlencode(payload)
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    sig = SignatureVerifier('test-signing-secret').generate_signature(timestamp=stamp, body=raw) if signature else 'v0=bad'
    env = {'PATH_INFO': path, 'REQUEST_METHOD': 'POST', 'CONTENT_TYPE': content_type, 'CONTENT_LENGTH': str(len(raw.encode())),
           'HTTP_X_SLACK_REQUEST_TIMESTAMP': stamp, 'HTTP_X_SLACK_SIGNATURE': sig, 'wsgi.input': io.BytesIO(raw.encode())}
    result = []
    body = b''.join(app(env, lambda status, headers: result.append((status, headers)))).decode()
    return int(result[0][0].split()[0]), body


def test_signed_http_durable_ack_retries_and_rejection(joined):
    l, s, _, comp, *_ = joined
    bolt = build_app(SlackUI(l, comp), 'xoxb-test', 'test-signing-secret', 'T1', 'UBOT', WebClient(token='xoxb-test'))
    app = HTTPApp(bolt, s)
    payload = {'type': 'event_callback', 'team_id': 'T1', 'event_id': 'Ev1', 'event': {'type': 'message', 'user': 'U1', 'channel': 'D1', 'text': 'hello', 'ts': '1'}}
    started = time.monotonic()
    assert request(app, payload)[0] == 200
    assert time.monotonic() - started < 3
    assert request(app, payload)[0] == 200
    assert len([j for j in s.select('ledger_inbox') if j['_id'] == 'slack:Ev1']) == 1
    assert request(app, payload, signature=False)[0] == 401
    assert request(app, payload, timestamp=int(time.time()) - 600)[0] == 401
    assert request(app, {**payload, 'team_id': 'OTHER'})[0] == 403
    with patch.object(s, 'atomic', side_effect=RuntimeError('unavailable')):
        assert request(app, {**payload, 'event_id': 'Ev2'})[0] >= 500
    assert not s.get('ledger_inbox', 'slack:Ev2')


def test_http_commands_ack_without_generation(joined):
    l, s, _, comp, api, _ = joined
    app = HTTPApp(build_app(SlackUI(l, comp), 'xoxb-test', 'test-signing-secret', 'T1', 'UBOT', WebClient(token='xoxb-test')), s)
    payload = {'team_id': 'T1', 'user_id': 'U1', 'command': '/ledger', 'text': 'status', 'trigger_id': 't1'}
    assert request(app, payload, 'application/x-www-form-urlencoded', path='/slack/commands')[0] == 200
    assert s.select('ledger_inbox', {'kind': 'command'})
    api.complete.assert_not_called()
