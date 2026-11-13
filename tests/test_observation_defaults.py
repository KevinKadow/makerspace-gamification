import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from ledger.engagement import Engagement, enabled
from ledger.slack_app import SlackUI
from ledger.storage import now
from ledger.worker import Worker
from test_progress_quests_delegation import member
from test_query_engagement_arrivals import observation_response
from test_slack import form


def test_default_is_consistent_in_runtime_and_compose(monkeypatch):
    for name in ('OBSERVATION', 'WELCOMES', 'DEDUCTIONS', 'NOVEL_ANNOUNCEMENTS'):
        monkeypatch.delenv('LEDGER_' + name, raising=False)
    assert enabled('OBSERVATION')
    assert not any(enabled(n) for n in ('WELCOMES', 'DEDUCTIONS', 'NOVEL_ANNOUNCEMENTS'))
    assert not enabled('OBSERVATION', default=False)
    root = Path(__file__).parents[1]
    compose = yaml.safe_load((root / 'compose.yaml').read_text())
    for service in ('ledger-web', 'ledger-accounting', 'ledger-delivery', 'ledger-engagement'):
        assert compose['services'][service]['environment']['LEDGER_OBSERVATION'] == '${LEDGER_OBSERVATION:-true}'
    assert '\nLEDGER_OBSERVATION=true\n' in (root / '.env.example').read_text()


def test_nonparticipant_channel_observation_waits_for_notice_then_audits_without_enrolling(env, monkeypatch):
    monkeypatch.delenv('LEDGER_OBSERVATION', raising=False)
    l, s, _, composer, api, slack = env
    worker = Worker(l, composer, slack, bot_id='UBOT')
    def message(text):
        return {'type': 'message', 'user': 'U1', 'channel': 'CCHAT', 'text': text, 'ts': str(now().timestamp())}
    worker.event(message('First eligible message'), 'first')
    assert not s.select('ledger_evidence', {'kind': 'observation'})
    key = f'observation-notice:{member(1)}:0'
    assert s.get('ledger_outbox', key)['status'] == 'pending'
    assert worker.step('ledger_outbox', kinds=['engagement_notice'])
    text = slack.chat_postMessage.call_args.kwargs['text']
    assert 'on by default' in text and '/ledger preferences' in text and 'whether or not you join' in text
    worker.event(message('Shared useful build feedback'), 'second')
    assert len(s.select('ledger_evidence', {'kind': 'observation'})) == 1
    api.complete.side_effect = observation_response
    result = Engagement(l).evaluate(member(1), api, 'nonparticipant')
    assert result['status'] == 'audit_only' and result['delta'] == 0
    assert not l.participant(member(1)) and not l.active(member(1))
    assert not s.select('ledger_awards') and not s.select('ledger_outbox', {'kind': 'invite'})
    assert not s.select('ledger_inbox', {'kind': 'reconcile_member'})


@pytest.mark.parametrize('channel', ['COTHER', 'DU1'])
def test_unrelated_channels_and_dms_never_start_observation(env, channel):
    l, s, *_ = env
    assert Engagement(l).capture(member(1), 'private', 'message', 'Private text', channel) is None
    assert not s.select('ledger_relationships', {'kind': 'member_preferences'})
    assert not s.select('ledger_outbox', {'kind': 'engagement_notice'})


@pytest.mark.parametrize('state', ['suspended', 'bot', 'deactivated', 'unlinked'])
def test_ineligible_identity_never_starts_observation(env, state):
    l, s, src, *_ = env
    if state == 'suspended':
        src.data['members'][0]['status'] = 'suspended'
    elif state == 'unlinked':
        src.data['slack_users'] = []
    else:
        s.put('ledger_catalog', {'_id': f'identity:{member(1)}', state: True})
    assert Engagement(l).capture(member(1), 'ineligible', 'message', 'Hello', 'CCHAT') is None
    assert not s.select('ledger_evidence', {'kind': 'observation'})
    assert not s.select('ledger_outbox', {'kind': 'engagement_notice'})


def test_nonparticipant_can_opt_out_via_modal_and_notice_button_while_paused(env):
    l, s, _, composer, api, slack = env
    s.put('ledger_catalog', {'_id': 'control', 'paused': True})
    ui = SlackUI(l, composer)
    ui.command({'user_id': 'U1', 'command': '/ledger', 'text': 'preferences', 'trigger_id': 'T'}, slack)
    view = slack.views_open.call_args.kwargs['view']
    assert next(b for b in view['blocks'] if b.get('block_id') == 'observation')['element']['initial_options']
    assert 'rank' not in json.dumps(view).lower().replace('ranks', '')
    ui.submission(form(view, {'observation': False, 'arrival_mentions': True}), slack)
    ui.action({'user': {'id': 'U1'}, 'trigger_id': 'T2', 'actions': [{'action_id': 'preferences', 'value': ''}]}, slack)
    saved = slack.views_open.call_args.kwargs['view']
    assert not next(b for b in saved['blocks'] if b.get('block_id') == 'observation')['element'].get('initial_options')
    assert not l.preference_profile(member(1))['preferences']['observation']
    assert not l.participant(member(1))
    assert not s.select('ledger_awards')
    api.complete.assert_not_called()


def test_optout_blocks_queued_notice_and_reenable_retries_without_joining(env):
    l, s, _, composer, api, slack = env
    service = Engagement(l)
    service.capture(member(1), 'first', 'message', 'Hello', 'CCHAT')
    key = f'observation-notice:{member(1)}:0'
    l.preferences(member(1), False, True)
    worker = Worker(l, composer, slack)
    assert worker.step('ledger_outbox', kinds=['engagement_notice'])
    assert s.get('ledger_outbox', key)['status'] == 'cancelled'
    service.notice(member(1))
    assert s.get('ledger_outbox', key)['status'] == 'cancelled'
    slack.chat_postMessage.assert_not_called()
    api.complete.assert_not_called()
    l.preferences(member(1), True, True)
    assert s.get('ledger_outbox', key)['status'] == 'pending'
    assert worker.step('ledger_outbox', kinds=['engagement_notice'])
    assert service.capture(member(1), 'reenabled', 'message', 'Hello', 'CCHAT')
    assert not l.participant(member(1))


def test_saved_optout_survives_first_join_reconcile_leave_and_rejoin(env):
    l, s, *_ = env
    l.preferences(member(1), False, True)
    l.join(member(1))
    assert s.get('ledger_relationships', f'member-preferences:{member(1)}')['kind'] == 'member_preferences_migrated'
    l.reconcile(member(1))
    l.leave(member(1))
    l.join(member(1))
    assert not l.preference_profile(member(1))['preferences']['observation']
    assert not s.select('ledger_outbox', {'kind': 'engagement_notice'})
    assert Engagement(l).capture(member(1), 'disabled', 'message', 'Hello', 'CCHAT') is None


def test_nonparticipant_optout_stops_delayed_inference_and_old_evidence_stays_stale(env):
    l, s, _, composer, api, slack = env
    service = Engagement(l)
    service.notice(member(1))
    assert Worker(l, composer, slack).step('ledger_outbox', kinds=['engagement_notice'])
    source = 'message:CCHAT:captured'
    s.put('ledger_context', {'_id': source, 'kind': 'message', 'member_id': member(1), 'text': 'Useful feedback'})
    doc = service.capture(member(1), source, 'message', 'Useful feedback', 'CCHAT')
    l.preferences(member(1), False, True)
    service.evaluate(member(1), api, 'disabled')
    api.complete.assert_not_called()
    l.preferences(member(1), True, True)
    service.evaluate(member(1), api, 'stale')
    api.complete.assert_not_called()
    assert s.get('ledger_evidence', doc['_id'])['status'] == 'cancelled'


def test_game_leave_does_not_disable_independent_observation(joined):
    l, s, _, composer, api, slack = joined
    Worker(l, composer, slack).step('ledger_outbox', kinds=['engagement_notice'])
    l.leave(member(1))
    assert l.preference_profile(member(1))['preferences']['observation']
    assert Engagement(l).capture(member(1), 'after-leave', 'message', 'Useful feedback', 'CCHAT')
    assert not l.active(member(1))


@pytest.mark.parametrize('action', ['game_leave', 'observation_optout'])
@pytest.mark.parametrize('retry_first', [False, True])
def test_notice_retry_respects_independent_consent_in_both_orders(env, monkeypatch, action, retry_first):
    monkeypatch.setenv('LEDGER_OBSERVATION', 'true')
    l, s, _, composer, _, slack = env
    l.join(member(1))
    key = f'observation-notice:{member(1)}:1'
    job = s.get('ledger_outbox', key)
    job['status'] = 'cancelled'
    s.put('ledger_outbox', job)
    retry = lambda: Engagement(l).notice(member(1))
    change = (lambda: l.leave(member(1))) if action == 'game_leave' else (lambda: l.preferences(member(1), False, True))
    for operation in ([retry, change] if retry_first else [change, retry]):
        operation()
    worker = Worker(l, composer, slack)
    if action == 'game_leave':
        assert not l.active(member(1)) and l.preference_profile(member(1))['preferences']['observation']
        assert worker.step('ledger_outbox', kinds=['engagement_notice'])
        assert s.get('ledger_outbox', key)['status'] == 'done'
        assert l.preference_profile(member(1)).get('observation_notice_delivered_at')
        slack.chat_postMessage.assert_called_once()
    else:
        assert l.active(member(1)) and not l.preference_profile(member(1))['preferences']['observation']
        assert worker.step('ledger_outbox', kinds=['engagement_notice']) == retry_first
        assert s.get('ledger_outbox', key)['status'] == 'cancelled'
        Engagement(l).notice(member(1))
        assert s.get('ledger_outbox', key)['status'] == 'cancelled'
        slack.chat_postMessage.assert_not_called()


def test_deployment_disable_overrides_default_and_reconcile_retries_nonparticipant_notice(env, monkeypatch):
    l, s, _, composer, _, slack = env
    service = Engagement(l)
    service.notice(member(1))
    monkeypatch.setenv('LEDGER_OBSERVATION', 'false')
    worker = Worker(l, composer, slack)
    assert worker.step('ledger_outbox', kinds=['engagement_notice'])
    assert service.capture(member(1), 'disabled', 'message', 'Hello', 'CCHAT') is None
    monkeypatch.delenv('LEDGER_OBSERVATION')
    worker.inbox({'kind': 'reconcile', 'payload': {}})
    assert s.get('ledger_outbox', f'observation-notice:{member(1)}:0')['status'] == 'pending'


def test_first_join_and_nonparticipant_optout_serialize(env):
    l, s, *_ = env
    with ThreadPoolExecutor(2) as pool:
        joined = pool.submit(l.join, member(1))
        opted_out = pool.submit(l.preferences, member(1), False, True)
        joined.result()
        opted_out.result()
    assert l.participant(member(1))['preferences']['observation'] is False
    assert s.get('ledger_relationships', f'member-preferences:{member(1)}')['kind'] == 'member_preferences_migrated'


def test_notice_optout_during_slack_lookup_prevents_send(env):
    l, s, _, composer, _, slack = env
    Engagement(l).notice(member(1))
    def open_dm(**kwargs):
        l.preferences(member(1), False, True)
        return {'channel': {'id': 'DU1'}}
    slack.conversations_open.side_effect = open_dm
    assert Worker(l, composer, slack).step('ledger_outbox', kinds=['engagement_notice'])
    slack.chat_postMessage.assert_not_called()
    assert not l.preference_profile(member(1)).get('observation_notice_delivered_at')


def test_nonparticipant_delivered_notice_survives_join_without_repeating(env):
    l, s, _, composer, _, slack = env
    Engagement(l).notice(member(1))
    assert Worker(l, composer, slack).step('ledger_outbox', kinds=['engagement_notice'])
    delivered = l.preference_profile(member(1))['observation_notice_delivered_at']
    l.join(member(1))
    assert l.participant(member(1))['observation_notice_delivered_at'] == delivered
    assert not s.get('ledger_outbox', f'observation-notice:{member(1)}:1')
    assert Engagement(l).capture(member(1), 'joined', 'message', 'Useful feedback', 'CCHAT')


def test_kudos_only_nonparticipant_starts_notice_and_observes_later_metadata(env):
    l, s, _, composer, _, slack = env
    l.kudos(member(1), member(2), 'Thanks for helping with the build.', key='first-kudos', expected_participation=False)
    key = f'observation-notice:{member(1)}:0'
    assert s.get('ledger_outbox', key)['status'] == 'pending'
    assert not s.select('ledger_evidence', {'kind': 'observation'})
    assert Worker(l, composer, slack).step('ledger_outbox', kinds=['engagement_notice'])
    l.kudos(member(1), member(3), 'Thanks for the lesson.', key='second-kudos', expected_participation=False)
    captured = s.select('ledger_evidence', {'kind': 'observation'})
    assert len(captured) == 1 and captured[0]['event_kind'] == 'kudos_metadata'
    assert captured[0]['text'] == '' and not l.participant(member(1))


@pytest.mark.parametrize('change', ['pause', 'disable', 'revoke', 'preference'])
def test_notice_receipt_survives_eligibility_change_after_slack_accepts(env, monkeypatch, change):
    l, s, src, composer, _, slack = env
    service = Engagement(l)
    service.notice(member(1))
    def post(**kwargs):
        if change == 'pause': s.put('ledger_catalog', {'_id': 'control', 'paused': True})
        if change == 'disable': monkeypatch.setenv('LEDGER_OBSERVATION', 'false')
        if change == 'revoke': src.data['members'][0]['status'] = 'revoked'
        if change == 'preference': l.preferences(member(1), False, True)
        return {'ts': '125.001'}
    slack.chat_postMessage.side_effect = post
    worker = Worker(l, composer, slack)
    assert worker.step('ledger_outbox', kinds=['engagement_notice'])
    key = f'observation-notice:{member(1)}:0'
    assert s.get('ledger_outbox', key)['status'] == 'done'
    assert l.preference_profile(member(1))['observation_notice_ts'] == '125.001'
    assert service.capture(member(1), 'blocked', 'kudos_metadata') is None
    s.put('ledger_catalog', {'_id': 'control', 'paused': False})
    monkeypatch.setenv('LEDGER_OBSERVATION', 'true')
    src.data['members'][0]['status'] = 'activeMember'
    if change == 'preference': l.preferences(member(1), True, True)
    service.notice(member(1))
    assert service.capture(member(1), 'after-resume', 'kudos_metadata')
    assert slack.chat_postMessage.call_count == 1 and not l.participant(member(1))


def test_legacy_done_notice_without_receipt_is_retried(env):
    l, s, _, composer, _, slack = env
    service = Engagement(l)
    service.notice(member(1))
    key = f'observation-notice:{member(1)}:0'
    job = s.get('ledger_outbox', key); job.update(status='done', lease='expired')
    s.put('ledger_outbox', job)
    service.notice(member(1))
    job = s.get('ledger_outbox', key)
    assert job['status'] == 'pending' and 'lease' not in job
    assert Worker(l, composer, slack).step('ledger_outbox', kinds=['engagement_notice'])
    assert l.preference_profile(member(1))['observation_notice_ts'] == '123.456'


def test_volunteer_only_member_starts_notice_without_observing_before_delivery(env):
    l, s, *_ = env
    assert Engagement(l).capture(member(1), 'volunteer:first', 'volunteer', metadata={'credit_value': '1'}) is None
    assert s.get('ledger_outbox', f'observation-notice:{member(1)}:0')['status'] == 'pending'
    assert not s.select('ledger_evidence', {'kind': 'observation'})


def test_removing_configured_channel_discards_pending_observation_before_inference(env):
    l, s, _, composer, api, slack = env
    Engagement(l).notice(member(1))
    assert Worker(l, composer, slack).step('ledger_outbox', kinds=['engagement_notice'])
    source = 'message:CCHAT:removed'
    s.put('ledger_context', {'_id': source, 'kind': 'message', 'member_id': member(1), 'text': 'Useful feedback'})
    doc = Engagement(l).capture(member(1), source, 'message', 'Useful feedback', 'CCHAT')
    s.delete('ledger_channels', 'chat')
    Engagement(l).evaluate(member(1), api, 'removed-channel')
    api.complete.assert_not_called()
    saved = s.get('ledger_evidence', doc['_id'])
    assert saved['status'] == 'cancelled' and 'channel' in saved['cancellation_reason']
