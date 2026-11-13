import io
import json
import time
from urllib.parse import urlencode
from unittest.mock import patch

import pytest
from slack_sdk import WebClient
from slack_sdk.signature import SignatureVerifier
from pymongo.errors import OperationFailure

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
    l.join(str(oid(10)))
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


def test_command_database_failure_logs_safe_cause_without_acknowledging(joined, caplog):
    l, s, _, comp, *_ = joined
    ui = SlackUI(l, comp)
    app = HTTPApp(build_app(ui, 'xoxb-test', 'test-signing-secret', 'T1', 'UBOT', WebClient(token='xoxb-test')), s)
    payload = {'team_id': 'T1', 'user_id': 'U1', 'command': '/ledger', 'text': 'PRIVATE_COMMAND',
               'trigger_id': 't1', 'response_url': 'https://example.invalid/SECRET_RESPONSE_URL'}
    with patch.object(l.sources, 'identity', side_effect=OperationFailure('PRIVATE_DATABASE_ERROR', code=13)):
        status, body = request(app, payload, 'application/x-www-form-urlencoded', path='/slack/commands')
    assert status == 500
    assert not s.select('ledger_inbox', {'kind': 'command'})
    assert 'error_type=OperationFailure code=13' in caplog.text
    assert 'path=/slack/commands status=500 duration_ms=' in caplog.text
    for secret in ('PRIVATE_COMMAND', 'SECRET_RESPONSE_URL', 'PRIVATE_DATABASE_ERROR', 'test-signing-secret'):
        assert secret not in caplog.text + body


def test_http_adapter_logs_failures_without_exception_details(joined, caplog):
    l, s, _, comp, *_ = joined
    bolt = build_app(SlackUI(l, comp), 'xoxb-test', 'test-signing-secret', 'T1', 'UBOT', WebClient(token='xoxb-test'))
    with patch.object(bolt, 'dispatch', side_effect=RuntimeError('PRIVATE_CONNECTION_STRING')):
        status, body = request(HTTPApp(bolt, s), {})
    assert status == 503
    assert 'error_type=RuntimeError' in caplog.text
    assert 'path=/slack/events status=503 duration_ms=' in caplog.text
    assert 'PRIVATE_CONNECTION_STRING' not in caplog.text + body


def test_slow_successful_callback_is_visible_in_logs(joined, caplog):
    l, s, _, comp, *_ = joined
    app = HTTPApp(build_app(SlackUI(l, comp), 'xoxb-test', 'test-signing-secret', 'T1', 'UBOT', WebClient(token='xoxb-test')), s)
    payload = {'type': 'url_verification', 'challenge': 'test'}
    with patch('ledger.http.time.monotonic', side_effect=[10, 12.6]):
        assert request(app, payload)[0] == 200
    assert 'path=/slack/events status=200 duration_ms=2600' in caplog.text


def test_join_remembers_consent_and_displays_saved_state(env):
    ledger, store, _, composer, _, slack = env
    ui = SlackUI(ledger, composer)
    command = {'user_id': 'U1', 'command': '/ledger', 'text': 'join', 'trigger_id': 't1'}
    ui.command(command, slack)
    consent = slack.views_open.call_args.kwargs['view']
    assert consent['callback_id'] == 'consent'
    reply = ui.submission(form(consent, {'agree': True}), slack)
    assert reply['response_action'] == 'update'
    assert reply['view']['title']['text'] == 'Opt-in saved'
    assert 'history import is queued' in json.dumps(reply)
    before = ledger.participant(str(oid(1)))
    queued = store.select('ledger_outbox')
    ui.command(command, slack)
    view = slack.views_open.call_args.kwargs['view']
    assert view['callback_id'] == 'dismiss'
    assert view['title']['text'] == 'Already opted in'
    assert 'Newbie' in json.dumps(view)
    ui.action({'user': {'id': 'U1'}, 'trigger_id': 't2', 'actions': [{'action_id': 'join', 'value': ''}]}, slack)
    assert slack.views_open.call_args.kwargs['view']['callback_id'] == 'dismiss'
    assert ledger.participant(str(oid(1))) == before
    assert store.select('ledger_outbox') == queued
    # Retried submissions keep consent, pinned rules, and invitations unchanged.
    ui.submission(form(consent, {'agree': True}), slack)
    assert ledger.participant(str(oid(1))) == before
    assert store.select('ledger_outbox') == queued
    ledger.leave(str(oid(1)))
    ui.command(command, slack)
    assert slack.views_open.call_args.kwargs['view']['callback_id'] == 'consent'


def test_signed_consent_submission_saves_before_success_response(env):
    ledger, store, _, composer, *_ = env
    ui = SlackUI(ledger, composer)
    app = HTTPApp(build_app(ui, 'xoxb-test', 'test-signing-secret', 'T1', 'UBOT', WebClient(token='xoxb-test')), store)
    payload = {**form(views.consent(), {'agree': True}), 'type': 'view_submission', 'team': {'id': 'T1'}}
    with patch.object(WebClient, 'users_info', return_value={'user': {'id': 'U1', 'deleted': False, 'is_bot': False}}):
        status, body = request(app, {'payload': json.dumps(payload)}, 'application/x-www-form-urlencoded', path='/slack/interactions')
    assert status == 200
    assert json.loads(body)['view']['title']['text'] == 'Opt-in saved'
    assert ledger.participant(str(oid(1)))['opted_in'] is True
    assert store.select('ledger_evidence', {'kind': 'consent'})


@pytest.mark.parametrize('channel,event_type,text,outcome', [
    ('DU1', 'message', 'Help me choose a first build', 'reply_queued'),
    ('CCHAT', 'app_mention', '<@UBOT> Help me choose a first build', 'reply_queued'),
    ('CCHAT', 'message', 'Chatting with another maker', 'ignored_unaddressed_channel_message'),
    ('COTHER', 'app_mention', '<@UBOT> Hello', 'ignored_unjoined_channel'),
])
def test_signed_chat_flows_through_accounting_and_delivery(joined, caplog, channel, event_type, text, outcome):
    from ledger.worker import Worker
    ledger, store, _, composer, api, slack = joined
    if channel == 'COTHER':
        slack.conversations_info.return_value = {'channel': {'is_member': False}}
    app = HTTPApp(build_app(SlackUI(ledger, composer), 'xoxb-test', 'test-signing-secret', 'T1', 'UBOT', WebClient(token='xoxb-test')), store)
    payload = {'type': 'event_callback', 'team_id': 'T1', 'event_id': 'EvChat', 'event': {
        'type': event_type, 'user': 'U1', 'channel': channel, 'text': text, 'ts': '100.001'}}
    assert request(app, payload)[0] == 200
    # HTTP receipt alone does not process or deliver a reply.
    api.complete.assert_not_called()
    slack.chat_postMessage.assert_not_called()
    assert store.get('ledger_inbox', 'slack:EvChat')['status'] == 'pending'
    worker = Worker(ledger, composer, slack, bot_id='UBOT')
    with caplog.at_level('INFO', logger='ledger.worker'):
        assert worker.step('ledger_inbox', kinds=['slack_event'])
    assert f'outcome={outcome}' in caplog.text
    assert text not in caplog.text
    assert worker.step('ledger_outbox', kinds=['conversation']) == (outcome == 'reply_queued')
    if outcome == 'reply_queued':
        assert slack.chat_postMessage.call_args.kwargs['channel'] == channel
        assert store.get('ledger_outbox', f'reply:{channel}:100.001')['status'] == 'done'
    else:
        slack.chat_postMessage.assert_not_called()
