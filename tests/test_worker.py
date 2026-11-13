from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from slack_sdk.errors import SlackApiError

from conftest import oid
from ledger.domain import Denied
from ledger.storage import now
from ledger.worker import Worker, ingest_mqtt


def claim(store, key):
    job = store.get('ledger_outbox', key)
    job.update(status='working', lease='test', attempts=1)
    store.put('ledger_outbox', job)
    return job


def worker(env):
    l, s, src, composer, api, slack = env
    return Worker(l, composer, slack, bot_id='UBOT')


def test_channel_kick_silently_skips_all_bots_and_slack_failures_are_not_raised(joined, caplog):
    _, _, _, _, _, slack = joined
    w = worker(joined)
    slack.users_info.side_effect = lambda user: {"user": {"id": user, "is_bot": user == "UOTHERBOT"}}

    assert not w.kick('CRANK1', 'UBOT')
    assert not w.kick('CRANK1', 'USLACKBOT')
    assert not w.kick('CRANK1', 'UOTHERBOT')
    slack.conversations_kick.assert_not_called()
    assert 'Slack channel removal skipped' not in caplog.text
    assert [call.kwargs['user'] for call in slack.users_info.call_args_list] == ['UOTHERBOT']

    response = SimpleNamespace(status_code=500, get=lambda key, default=None: 'fatal_error' if key == 'error' else default)
    slack.conversations_kick.side_effect = SlackApiError('failed', response)
    assert not w.kick('CRANK1', 'U1')
    slack.conversations_kick.assert_called_once_with(channel='CRANK1', user='U1')
    assert 'Slack channel removal failed' in caplog.text and 'not retrying' in caplog.text


def test_remove_job_transport_failure_is_logged_once_without_retry(joined, caplog):
    ledger, store, _, _, _, slack = joined
    w = worker(joined)
    member_id = str(oid(1))
    ledger.leave(member_id)
    removals = store.select('ledger_outbox', {'kind': 'remove', 'payload.member_id': member_id})
    target = next(job for job in removals if job['payload']['channel'] == 'CRANK1')
    for job in removals:
        if job['_id'] != target['_id']:
            job['status'] = 'done'
            store.put('ledger_outbox', job)
    slack.conversations_kick.side_effect = RuntimeError('private transport details')

    assert w.step('ledger_outbox', kinds=['remove'])

    saved = store.get('ledger_outbox', target['_id'])
    assert saved['status'] == 'done' and saved['attempts'] == 1
    slack.conversations_kick.assert_called_once_with(channel='CRANK1', user='U1')
    assert 'transport_error=RuntimeError' in caplog.text and 'not retrying' in caplog.text
    assert 'private transport details' not in caplog.text
    assert not w.step('ledger_outbox', kinds=['remove'])
    slack.conversations_kick.assert_called_once()


def test_channel_reconcile_excludes_actual_bots_but_not_authorized_app_users(env):
    _, store, source, _, _, slack = env
    for channel in store.select('ledger_channels', {'kind': 'channel'}):
        if channel['_id'] != 'chat':
            store.delete('ledger_channels', channel['_id'])
    slack.conversations_members.return_value = {
        'members': ['UBOT', 'UOTHERBOT', 'UUNLINKED'], 'response_metadata': {}}
    slack.users_info.side_effect = lambda user: {
        'user': {'id': user, 'deleted': False, 'is_bot': user == 'UOTHERBOT',
                 'is_app_user': user == 'UUNLINKED'}}
    store.put('ledger_catalog', {'_id': 'slack-user:UUNLINKED', 'kind': 'slack_user',
        'slack_id': 'UUNLINKED', 'bot': True, 'at': now()})
    source.identity = MagicMock(wraps=source.identity)
    w = worker(env)

    w.reconcile_channels()
    w.reconcile_channels()

    removals = store.select('ledger_outbox', {'kind': 'remove'})
    assert len(removals) == 1
    assert removals[0]['_id'] == 'unauthorized:chat:UUNLINKED'
    assert removals[0]['payload'] == {'slack_id': 'UUNLINKED', 'channel': 'CCHAT'}
    assert [call.kwargs['user'] for call in slack.users_info.call_args_list].count('UOTHERBOT') == 1
    assert sum(call.args == ('UOTHERBOT',) for call in source.identity.call_args_list) == 1
    cached_bot = store.get('ledger_catalog', 'slack-user:UOTHERBOT')
    assert cached_bot['bot'] is True and cached_bot['bot_identity_source'] == 'is_bot'
    corrected = store.get('ledger_catalog', 'slack-user:UUNLINKED')
    assert corrected['bot'] is False and corrected['bot_identity_source'] == 'is_bot'


def test_welcome_waits_for_accounting_without_exhausting_delivery_retries(joined, caplog):
    ledger, store, _, _, _, slack = joined
    w = worker(joined)
    key = f'dm:join:{oid(1)}:1'
    # Select only this welcome so other queued work cannot mask its outcome.
    for other in store.select('ledger_outbox'):
        if other['_id'] != key:
            other['status'] = 'done'
            store.put('ledger_outbox', other)
    for _ in range(12):
        job = store.get('ledger_outbox', key)
        job['available_at'] = now() - timedelta(seconds=1)
        store.put('ledger_outbox', job)
        assert w.step('ledger_outbox')
    job = store.get('ledger_outbox', key)
    assert job['status'] == 'pending' and job['attempts'] == 0
    assert job['last_error'] == 'HistoryImportPending'
    assert caplog.text.count('check ledger-accounting') == 1
    slack.chat_postMessage.assert_not_called()
    ledger.reconcile(str(oid(1)), historical=True)
    job['available_at'] = now() - timedelta(seconds=1)
    store.put('ledger_outbox', job)
    assert w.step('ledger_outbox')
    assert store.get('ledger_outbox', key)['status'] == 'done'
    slack.chat_postMessage.assert_called_once()


def test_public_kudos_keeps_body_selected_emoji_and_independent_receipts(joined):
    l, s, _, _, api, slack = joined
    w = worker(joined)
    a, b = str(oid(1)), str(oid(2))
    body = '*Thanks* _maker_ ~oops~ <https://example.com|link>\n> quote\n`code` :hammer: 🌱'
    l.kudos(a, b, body, key='render', public=True, expected_participation=True, emoji=':clap:')
    dm = claim(s, 'kudos:render:recipient')
    w.outbox(dm)
    assert slack.chat_postMessage.call_args.kwargs['blocks'][2]['text']['text'] == body
    assert slack.chat_postMessage.call_args.kwargs['text'].startswith(':clap: You have received kudos from <@U1>\n')
    l.leave(b)
    ranks = s.get('ledger_catalog', 'rank_display')['ranks']
    ranks[0].update(emoji=':custom_maker:', name='Seedling')
    l.publish_ranks(str(oid(10)), ranks)
    public = claim(s, 'kudos:render:shared')
    w.outbox(public)
    call = slack.chat_postMessage.call_args.kwargs
    assert call['channel'] == 'CCHAT'
    assert call['blocks'][0]['text']['text'] == ':clap: <@U2> has received kudos from <@U1>'
    assert call['blocks'][2]['text']['text'] == body
    assert 'Seedling' not in call['text'] and ':custom_maker:' not in call['text']
    assert set(s.get('ledger_evidence', 'kudos:render')['deliveries']) == {'shared', 'recipient'}
    assert l.participant(b)['xp'] == '17'
    calls = slack.chat_postMessage.call_count
    w.outbox(dm)
    assert slack.chat_postMessage.call_count == calls  # receipt survives job completion failure
    assert api.complete.call_count == 2


def test_failed_public_retry_reuses_composition_and_reports_partial(joined):
    l, s, _, _, api, slack = joined
    w = worker(joined)
    l.kudos(str(oid(1)), str(oid(2)), 'Thanks!', key='retry', public=True, expected_participation=True)
    w.outbox(claim(s, 'kudos:retry:recipient'))
    public = claim(s, 'kudos:retry:shared')
    slack.chat_postMessage.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        w.outbox(public)
    saved = s.get('ledger_outbox', public['_id'])['composed']
    w.finish('ledger_outbox', public, 'failed', error='TimeoutError')
    e = s.get('ledger_evidence', 'kudos:retry')
    assert e['deliveries']['recipient']['status'] == 'delivered'
    assert e['deliveries']['shared']['status'] == 'failed'
    slack.chat_postMessage.side_effect = None
    w.outbox(claim(s, public['_id']))
    assert api.complete.call_count == 2
    assert s.get('ledger_outbox', public['_id'])['composed'] == saved
    assert l.participant(str(oid(2)))['xp'] == '17'


def test_ai_failure_does_not_block_kudos_delivery_or_channel_cleanup(joined):
    l, s, _, _, api, slack = joined
    w = worker(joined)
    api.complete.side_effect = TimeoutError()
    l.kudos(str(oid(1)), str(oid(2)), 'Thank you', key='fallback', expected_participation=True)
    w.outbox(claim(s, 'kudos:fallback:recipient'))
    assert s.get('ledger_outbox', 'kudos:fallback:recipient')['composed']['outcome'] == 'fallback'
    l.leave(str(oid(2)))
    for j in s.select('ledger_outbox', {'kind': 'remove'}):
        w.outbox(claim(s, j['_id']))
    assert slack.conversations_kick.call_count == 7
    assert api.complete.call_count == 1


def test_invite_opt_out_during_slack_call_is_compensated(joined):
    l, s, _, _, _, slack = joined
    w = worker(joined)
    m = str(oid(1))
    job = next(j for j in s.select('ledger_outbox', {'kind': 'invite'}) if j['payload']['member_id'] == m)
    slack.conversations_invite.side_effect = lambda **kwargs: l.leave(m)
    w.outbox(claim(s, job['_id']))
    slack.conversations_kick.assert_called_once()


def test_rank_transition_announces_privately_to_old_and_new_rank_in_order(joined):
    l, s, _, _, _, slack = joined
    w = worker(joined)
    member_id = str(oid(1))
    participant = l.participant(member_id)
    participant.update(rank=2, revision=participant['revision'] + 1)
    s.put('ledger_participants', participant)
    s.put('ledger_channels', {'_id': f'membership:{member_id}:rank:1', 'kind': 'membership',
        'member_id': member_id, 'channel_key': 'rank:1', 'desired': True, 'present': True})
    s.put('ledger_channels', {'_id': f'membership:{member_id}:rank:2', 'kind': 'membership',
        'member_id': member_id, 'channel_key': 'rank:2', 'desired': True, 'present': False})
    job = next(j for j in s.select('ledger_outbox', {'kind': 'invite'}) if j['payload']['member_id'] == member_id)
    job['payload'].update(channel='CRANK2', channel_key='rank:2', revision=participant['revision'],
        rank_transition={'old_slot': 1, 'new_slot': 2,
                         'consent_generation': participant['consent_generation']})
    s.put('ledger_outbox', job)

    w.outbox(claim(s, job['_id']))

    calls = [call[0] for call in slack.method_calls]
    assert calls.index('chat_postMessage') < calls.index('conversations_invite')
    assert calls.index('conversations_invite') < calls.index('conversations_kick')
    assert calls.index('conversations_kick') < max(i for i, name in enumerate(calls) if name == 'chat_postMessage')
    messages = [call.kwargs for call in slack.chat_postMessage.call_args_list]
    assert messages[0]['channel'] == 'CRANK1'
    assert messages[-1]['channel'] == 'CRANK2'
    assert 'Novice' not in messages[0]['text']


def test_rank_transition_rejects_a_superseded_lower_rank_job(joined):
    l, s, _, _, _, slack = joined
    w = worker(joined)
    member_id = str(oid(1))
    participant = l.participant(member_id)
    participant.update(rank=3, revision=participant['revision'] + 2)
    s.put('ledger_participants', participant)
    s.put('ledger_channels', {'_id': f'membership:{member_id}:rank:1', 'kind': 'membership',
        'member_id': member_id, 'channel_key': 'rank:1', 'desired': True, 'present': True})
    s.put('ledger_channels', {'_id': f'membership:{member_id}:rank:2', 'kind': 'membership',
        'member_id': member_id, 'channel_key': 'rank:2', 'desired': True, 'present': False})
    job = next(j for j in s.select('ledger_outbox', {'kind': 'invite'}) if j['payload']['member_id'] == member_id)
    job['payload'].update(channel='CRANK2', channel_key='rank:2', revision=participant['revision'] - 2,
        rank_transition={'old_slot': 1, 'new_slot': 2,
                         'consent_generation': participant['consent_generation']})
    s.put('ledger_outbox', job)

    with pytest.raises(Denied):
        w.outbox(claim(s, job['_id']))

    slack.chat_postMessage.assert_not_called()
    slack.conversations_invite.assert_not_called()
    assert not s.get('ledger_channels', f'membership:{member_id}:rank:2')['present']


@pytest.mark.parametrize('stale_cause', ['rank_correction', 'identity_change'])
def test_rank_transition_retry_compensates_a_committed_invite(joined, stale_cause):
    l, s, source, _, _, slack = joined
    w = worker(joined)
    member_id = str(oid(1))
    participant = l.participant(member_id)
    participant.update(rank=2, revision=participant['revision'] + 1)
    s.put('ledger_participants', participant)
    s.put('ledger_channels', {'_id': f'membership:{member_id}:rank:1', 'kind': 'membership',
        'member_id': member_id, 'channel_key': 'rank:1', 'desired': True, 'present': True})
    s.put('ledger_channels', {'_id': f'membership:{member_id}:rank:2', 'kind': 'membership',
        'member_id': member_id, 'channel_key': 'rank:2', 'desired': True, 'present': False})
    job = next(j for j in s.select('ledger_outbox', {'kind': 'invite'}) if j['payload']['member_id'] == member_id)
    job['payload'].update(channel='CRANK2', channel_key='rank:2', revision=participant['revision'],
        rank_transition={'old_slot': 1, 'new_slot': 2,
                         'consent_generation': participant['consent_generation']})
    s.put('ledger_outbox', job)

    def fail_welcome(**kwargs):
        if kwargs['channel'] == 'CRANK2':
            raise RuntimeError('temporary Slack failure')
        return {'ts': '123.456'}

    slack.chat_postMessage.side_effect = fail_welcome
    first_attempt = claim(s, job['_id'])
    with pytest.raises(RuntimeError, match='temporary Slack failure'):
        w.outbox(first_attempt)
    committed = s.get('ledger_outbox', job['_id'])['rank_transition_commit']
    assert committed['slack_id'] == 'U1' and committed['channel'] == 'CRANK2'
    w.finish('ledger_outbox', first_attempt, 'pending', error='RuntimeError')

    if stale_cause == 'rank_correction':
        l.correct_rank(str(oid(10)), member_id, 1, 'Correction before retry')
    else:
        source.data['slack_users'] = [row for row in source.data['slack_users'] if row['slack_id'] != 'U1']

    with pytest.raises(Denied):
        w.outbox(claim(s, job['_id']))

    assert [call.kwargs['channel'] for call in slack.conversations_kick.call_args_list] == ['CRANK1', 'CRANK2']
    membership = s.get('ledger_channels', f'membership:{member_id}:rank:2')
    assert not membership['present']
    if stale_cause == 'rank_correction':
        assert not membership['desired']


def test_rank_transition_preserves_invite_if_access_is_restored_under_new_consent(joined):
    l, s, _, _, _, slack = joined
    w = worker(joined)
    member_id = str(oid(1))
    participant = l.participant(member_id)
    participant.update(rank=1, revision=participant['revision'] + 2)
    s.put('ledger_participants', participant)
    s.put('ledger_channels', {'_id': f'membership:{member_id}:rank:1', 'kind': 'membership',
        'member_id': member_id, 'channel_key': 'rank:1', 'desired': True, 'present': True})
    s.put('ledger_channels', {'_id': f'membership:{member_id}:rank:2', 'kind': 'membership',
        'member_id': member_id, 'channel_key': 'rank:2', 'desired': True, 'present': True})
    job = next(j for j in s.select('ledger_outbox', {'kind': 'invite'}) if j['payload']['member_id'] == member_id)
    job['payload'].update(channel='CRANK2', channel_key='rank:2', revision=participant['revision'] - 2,
        rank_transition={'old_slot': 1, 'new_slot': 2,
                         'consent_generation': participant['consent_generation']})
    job['rank_transition_commit'] = {'slack_id': 'U1', 'channel': 'CRANK2', 'new_slot': 2,
        'consent_generation': participant['consent_generation'], 'at': now()}
    s.put('ledger_outbox', job)
    original_check = w._rank_transition_membership_authorized_now

    def restore_access_before_kick(transition_job, uid):
        latest = l.participant(member_id)
        latest.update(rank=2, revision=latest['revision'] + 1,
            consent_generation=latest['consent_generation'] + 1, rank_hold=False)
        s.put('ledger_participants', latest)
        membership = s.get('ledger_channels', f'membership:{member_id}:rank:2')
        membership.update(present=True, desired=True, voluntary_leave=False)
        s.put('ledger_channels', membership)
        return original_check(transition_job, uid)

    w._rank_transition_membership_authorized_now = restore_access_before_kick
    with pytest.raises(Denied):
        w.outbox(claim(s, job['_id']))

    slack.conversations_kick.assert_not_called()
    membership = s.get('ledger_channels', f'membership:{member_id}:rank:2')
    assert membership['present'] and membership['desired']


def test_rank_transition_does_not_compensate_superseded_committed_invite(joined):
    l, s, _, _, _, slack = joined
    w = worker(joined)
    member_id = str(oid(1))
    participant = l.participant(member_id)
    s.put('ledger_channels', {'_id': f'membership:{member_id}:rank:2', 'kind': 'membership',
        'member_id': member_id, 'channel_key': 'rank:2', 'desired': False, 'present': True})
    job = next(j for j in s.select('ledger_outbox', {'kind': 'invite'}) if j['payload']['member_id'] == member_id)
    job['payload'].update(channel='CRANK2', channel_key='rank:2', rank_transition={
        'old_slot': 1, 'new_slot': 2, 'consent_generation': participant['consent_generation']})
    job['rank_transition_commit'] = {'slack_id': 'U1', 'channel': 'CRANK2', 'new_slot': 2,
        'consent_generation': participant['consent_generation'], 'at': now()}
    s.put('ledger_outbox', job)

    def supersede_commit_before_kick(transition_job, uid):
        saved = s.get('ledger_outbox', job['_id'])
        saved['rank_transition_commit']['new_slot'] = 3
        s.put('ledger_outbox', saved)
        return False

    w._rank_transition_membership_authorized_now = supersede_commit_before_kick
    assert not w._compensate_rank_transition_invite(job, 'U1', committed_only=True)

    slack.conversations_kick.assert_not_called()
    membership = s.get('ledger_channels', f'membership:{member_id}:rank:2')
    assert membership['present']


@pytest.mark.parametrize(('thread_ts', 'timestamp', 'broadcast'), [
    ('1791323999.100', '1791323999.100', True),
    ('1791323999.100', '1791324000.200', False)])
def test_conversation_broadcasts_only_first_reply_to_a_thread(joined, thread_ts, timestamp, broadcast):
    l, s, _, composer, _, slack = joined
    w = worker(joined)
    member_id = str(oid(1))
    participant = l.participant(member_id)
    message_id = f'message:D123:{timestamp}'
    s.put('ledger_context', {'_id': message_id, 'kind': 'message', 'member_id': member_id,
        'channel': 'D123', 'thread': thread_ts, 'text': 'Can you help me?', 'at': timestamp,
        'at_order': float(timestamp), 'conversation_requested': True, 'participating': True,
        'consent_generation': participant['consent_generation'], 'expires_at': now() + timedelta(days=1)})
    job = {'_id': f'reply:D123:{timestamp}', 'kind': 'conversation', 'status': 'working',
        'lease': 'test', 'attempts': 1, 'payload': {'member_id': member_id, 'channel': 'D123',
            'thread': thread_ts, 'message_id': message_id, 'text': 'Can you help me?',
            'participating': True, 'consent_generation': participant['consent_generation'],
            'ambient': False, 'use_tools': False}, 'composed': {'text': 'I can help with that.'}}
    s.put('ledger_outbox', job)

    w.outbox(job)

    assert slack.chat_postMessage.call_args.kwargs.get('reply_broadcast', False) is broadcast


@pytest.mark.parametrize('change', ['rank_correction', 'voluntary_departure'])
def test_rank_transition_revalidates_access_after_prior_channel_post(joined, change):
    l, s, _, _, _, slack = joined
    w = worker(joined)
    member_id = str(oid(1))
    participant = l.participant(member_id)
    participant.update(rank=2, revision=participant['revision'] + 1)
    s.put('ledger_participants', participant)
    s.put('ledger_channels', {'_id': f'membership:{member_id}:rank:1', 'kind': 'membership',
        'member_id': member_id, 'channel_key': 'rank:1', 'desired': True, 'present': True})
    s.put('ledger_channels', {'_id': f'membership:{member_id}:rank:2', 'kind': 'membership',
        'member_id': member_id, 'channel_key': 'rank:2', 'desired': True, 'present': False})
    job = next(j for j in s.select('ledger_outbox', {'kind': 'invite'}) if j['payload']['member_id'] == member_id)
    job['payload'].update(channel='CRANK2', channel_key='rank:2', revision=participant['revision'],
        rank_transition={'old_slot': 1, 'new_slot': 2,
                         'consent_generation': participant['consent_generation']})
    s.put('ledger_outbox', job)

    def mutate_during_post(**kwargs):
        assert kwargs['channel'] == 'CRANK1'
        if change == 'rank_correction':
            l.correct_rank(str(oid(10)), member_id, 1, 'Correction during transition')
        else:
            target_membership = s.get('ledger_channels', f'membership:{member_id}:rank:2')
            target_membership.update(voluntary_leave=True, desired=False)
            s.put('ledger_channels', target_membership)
        return {'ts': '123.456'}

    slack.chat_postMessage.side_effect = mutate_during_post
    with pytest.raises(Denied):
        w.outbox(claim(s, job['_id']))

    slack.conversations_invite.assert_not_called()
    slack.conversations_kick.assert_not_called()
    target_membership = s.get('ledger_channels', f'membership:{member_id}:rank:2')
    if change == 'rank_correction':
        assert l.participant(member_id)['rank'] == 1
        assert not target_membership['present']
    else:
        assert not target_membership['desired']
        assert target_membership['voluntary_leave']


def test_rank_transition_compensates_if_rank_changes_during_invite(joined):
    l, s, _, _, _, slack = joined
    w = worker(joined)
    member_id = str(oid(1))
    participant = l.participant(member_id)
    participant.update(rank=2, revision=participant['revision'] + 1)
    s.put('ledger_participants', participant)
    s.put('ledger_channels', {'_id': f'membership:{member_id}:rank:1', 'kind': 'membership',
        'member_id': member_id, 'channel_key': 'rank:1', 'desired': True, 'present': True})
    s.put('ledger_channels', {'_id': f'membership:{member_id}:rank:2', 'kind': 'membership',
        'member_id': member_id, 'channel_key': 'rank:2', 'desired': True, 'present': False})
    job = next(j for j in s.select('ledger_outbox', {'kind': 'invite'}) if j['payload']['member_id'] == member_id)
    job['payload'].update(channel='CRANK2', channel_key='rank:2', revision=participant['revision'],
        rank_transition={'old_slot': 1, 'new_slot': 2,
                         'consent_generation': participant['consent_generation']})
    s.put('ledger_outbox', job)
    slack.conversations_invite.side_effect = lambda **kwargs: l.correct_rank(
        str(oid(10)), member_id, 1, 'Correction during invite')

    with pytest.raises(Denied):
        w.outbox(claim(s, job['_id']))

    slack.conversations_invite.assert_called_once_with(channel='CRANK2', users='U1')
    slack.conversations_kick.assert_called_once_with(channel='CRANK2', user='U1')
    assert l.participant(member_id)['rank'] == 1
    assert not s.get('ledger_channels', f'membership:{member_id}:rank:2')['present']


def test_rank_transition_compensates_if_identity_changes_during_invite(joined):
    l, s, source, _, _, slack = joined
    w = worker(joined)
    member_id = str(oid(1))
    participant = l.participant(member_id)
    participant.update(rank=2, revision=participant['revision'] + 1)
    s.put('ledger_participants', participant)
    s.put('ledger_channels', {'_id': f'membership:{member_id}:rank:1', 'kind': 'membership',
        'member_id': member_id, 'channel_key': 'rank:1', 'desired': True, 'present': True})
    s.put('ledger_channels', {'_id': f'membership:{member_id}:rank:2', 'kind': 'membership',
        'member_id': member_id, 'channel_key': 'rank:2', 'desired': True, 'present': False})
    job = next(j for j in s.select('ledger_outbox', {'kind': 'invite'}) if j['payload']['member_id'] == member_id)
    job['payload'].update(channel='CRANK2', channel_key='rank:2', revision=participant['revision'],
        rank_transition={'old_slot': 1, 'new_slot': 2,
                         'consent_generation': participant['consent_generation']})
    s.put('ledger_outbox', job)

    def reassign_during_invite(**kwargs):
        source.data['slack_users'][0]['slack_id'], source.data['slack_users'][1]['slack_id'] = (
            source.data['slack_users'][1]['slack_id'], source.data['slack_users'][0]['slack_id'])
        return {'ok': True}

    slack.conversations_invite.side_effect = reassign_during_invite
    with pytest.raises(Denied):
        w.outbox(claim(s, job['_id']))

    slack.conversations_invite.assert_called_once_with(channel='CRANK2', users='U1')
    slack.conversations_kick.assert_called_once_with(channel='CRANK2', user='U1')
    assert not s.get('ledger_channels', f'membership:{member_id}:rank:2')['present']


@pytest.mark.parametrize('change_at', ['before_prior_kick', 'before_welcome'])
def test_rank_transition_rechecks_after_invite_commit(joined, change_at):
    l, s, _, _, _, slack = joined
    w = worker(joined)
    member_id = str(oid(1))
    participant = l.participant(member_id)
    participant.update(rank=2, revision=participant['revision'] + 1)
    s.put('ledger_participants', participant)
    s.put('ledger_channels', {'_id': f'membership:{member_id}:rank:1', 'kind': 'membership',
        'member_id': member_id, 'channel_key': 'rank:1', 'desired': True, 'present': True})
    s.put('ledger_channels', {'_id': f'membership:{member_id}:rank:2', 'kind': 'membership',
        'member_id': member_id, 'channel_key': 'rank:2', 'desired': True, 'present': False})
    job = next(j for j in s.select('ledger_outbox', {'kind': 'invite'}) if j['payload']['member_id'] == member_id)
    job['payload'].update(channel='CRANK2', channel_key='rank:2', revision=participant['revision'],
        rank_transition={'old_slot': 1, 'new_slot': 2,
                         'consent_generation': participant['consent_generation']})
    s.put('ledger_outbox', job)
    original_revalidate = w._revalidate_committed_rank_transition
    checks = 0

    def correct_rank_at_boundary(transition_job, uid):
        nonlocal checks
        checks += 1
        target_check = 1 if change_at == 'before_prior_kick' else 2
        if checks == target_check:
            l.correct_rank(str(oid(10)), member_id, 1, 'Correction after invite commit')
        return original_revalidate(transition_job, uid)

    w._revalidate_committed_rank_transition = correct_rank_at_boundary
    with pytest.raises(Denied):
        w.outbox(claim(s, job['_id']))

    kicked_channels = [call.kwargs['channel'] for call in slack.conversations_kick.call_args_list]
    expected = ['CRANK2'] if change_at == 'before_prior_kick' else ['CRANK1', 'CRANK2']
    assert kicked_channels == expected
    assert [call.kwargs['channel'] for call in slack.chat_postMessage.call_args_list] == ['CRANK1']
    membership = s.get('ledger_channels', f'membership:{member_id}:rank:2')
    assert not membership['present'] and not membership['desired']


def test_rank_transition_re_resolves_identity_immediately_before_invite(joined):
    l, s, source, _, _, slack = joined
    w = worker(joined)
    member_id = str(oid(1))
    participant = l.participant(member_id)
    participant.update(rank=2, revision=participant['revision'] + 1)
    s.put('ledger_participants', participant)
    s.put('ledger_channels', {'_id': f'membership:{member_id}:rank:1', 'kind': 'membership',
        'member_id': member_id, 'channel_key': 'rank:1', 'desired': True, 'present': True})
    s.put('ledger_channels', {'_id': f'membership:{member_id}:rank:2', 'kind': 'membership',
        'member_id': member_id, 'channel_key': 'rank:2', 'desired': True, 'present': False})
    job = next(j for j in s.select('ledger_outbox', {'kind': 'invite'}) if j['payload']['member_id'] == member_id)
    job['payload'].update(channel='CRANK2', channel_key='rank:2', revision=participant['revision'],
        rank_transition={'old_slot': 1, 'new_slot': 2,
                         'consent_generation': participant['consent_generation']})
    s.put('ledger_outbox', job)
    original_valid_identity = w.valid_identity
    identity_checks = 0

    def reassign_on_final_identity_check(target_member):
        nonlocal identity_checks
        identity_checks += 1
        if identity_checks == 3:
            # Simulate the source mapping changing after prior-channel delivery,
            # at the final authorization boundary before Slack's invite call.
            source.data['slack_users'][0]['slack_id'], source.data['slack_users'][1]['slack_id'] = (
                source.data['slack_users'][1]['slack_id'], source.data['slack_users'][0]['slack_id'])
        return original_valid_identity(target_member)

    w.valid_identity = reassign_on_final_identity_check
    with pytest.raises(Denied):
        w.outbox(claim(s, job['_id']))

    slack.conversations_invite.assert_not_called()
    slack.conversations_kick.assert_not_called()
    assert not s.get('ledger_channels', f'membership:{member_id}:rank:2')['present']


def test_voluntary_departure_and_lower_rank_reinvite(joined):
    l, s, *_ = joined
    w = worker(joined)
    m = str(oid(1))
    p = l.participant(m)
    p['rank'] = 2
    s.put('ledger_participants', p)
    w.event({'type': 'member_left_channel', 'channel': 'CRANK1', 'user': 'U1'}, 'leave')
    with pytest.raises(Denied):
        l.invite(m, m, 'rank:1')
    other = str(oid(2))
    s.put('ledger_channels', {'_id': f'membership:{other}:rank:1', 'kind': 'membership', 'member_id': other, 'channel_key': 'rank:1', 'present': True})
    l.invite(other, m, 'rank:1')
    assert not s.get('ledger_channels', f'membership:{m}:rank:1')['voluntary_leave']
    w.event({'type': 'member_joined_channel', 'channel': 'CCHAT', 'user': 'U3'}, 'unauthorized')
    removal = s.get('ledger_outbox', 'unauthorized:chat:U3')
    assert removal['payload']['member_id'] == str(oid(3))
    reconciliation = s.get('ledger_inbox', 'channel-reconcile:unauthorized')
    assert reconciliation['kind'] == 'channel_reconcile'
    assert reconciliation['payload'] == {'channel': 'CCHAT', 'source': 'member_joined_channel'}
    w.slack.conversations_members.return_value = {'members': ['U3'], 'response_metadata': {}}
    w.inbox(reconciliation)
    removals = [job for job in s.select('ledger_outbox', {'kind': 'remove'})
                if job['payload'].get('channel') == 'CCHAT' and job['payload'].get('slack_id') == 'U3']
    assert [job['_id'] for job in removals] == ['unauthorized:chat:U3']
    removal = removals[0]
    removal.update(status='done', attempts=1)
    s.put('ledger_outbox', removal)
    w.event({'type': 'member_joined_channel', 'channel': 'CCHAT', 'user': 'U3'}, 'unauthorized-again')
    refreshed = s.get('ledger_outbox', 'unauthorized:chat:U3')
    assert refreshed['status'] == 'pending' and refreshed['attempts'] == 0
    assert len([job for job in s.select('ledger_outbox', {'kind': 'remove'})
                if job['payload'].get('channel') == 'CCHAT' and job['payload'].get('slack_id') == 'U3']) == 1


def test_bot_join_is_exempt_but_authorized_app_user_join_is_removed(joined):
    _, store, _, _, _, slack = joined
    w = worker(joined)
    w.event({'type': 'member_joined_channel', 'channel': 'CRANK1', 'user': 'UBOT'}, 'bot-join')
    job = store.get('ledger_inbox', 'channel-reconcile:bot-join')
    assert job['kind'] == 'channel_reconcile'
    assert job['payload'] == {'channel': 'CRANK1', 'source': 'member_joined_channel'}
    assert not store.get('ledger_outbox', 'bot-join:remove')

    slack.users_info.side_effect = lambda user: {'user': {
        'id': user, 'deleted': False, 'is_bot': False, 'is_app_user': True}}
    w.event({'type': 'member_joined_channel', 'channel': 'CRANK1', 'user': 'UAPPUSER'}, 'app-user-join')

    removal = store.get('ledger_outbox', 'unauthorized:rank:1:UAPPUSER')
    assert removal['payload'] == {'slack_id': 'UAPPUSER', 'channel': 'CRANK1'}
    assert store.get('ledger_catalog', 'slack-user:UAPPUSER') is None


def test_only_major_automatic_posts_coalesce_and_historical_posts_suppressed(joined):
    l, s, *_ = joined
    m = str(oid(1))
    l.tx('major', m, 'shop_complete', {'shop': 'Wood'}, 'wood')
    l.tx('major', m, 'rank_up', {'rank': 'Novice'}, 'rank')
    l.tx('major', m, 'boss', {}, 'boss')
    l.tx('major', m, 'boss', {}, 'old', historical=True)
    shared = [j for j in s.select('ledger_outbox') if j['payload'].get('audience') == 'shared']
    assert len(shared) == 1 and len(shared[0]['payload']['facts']['achievements']) == 2
    assert {fact['type'] for fact in shared[0]['payload']['facts']['achievements']} == {'boss', 'shop_complete'}
    assert shared[0]['available_at'] > now() + timedelta(seconds=55)
    assert len(s.select('ledger_outbox', {'kind': 'mqtt'})) == 3


def test_rank_up_is_not_added_to_existing_shared_announcement(joined):
    l, s, *_ = joined
    m = str(oid(1))
    l.tx('major', m, 'shop_complete', {'shop': 'Wood'}, 'wood')
    l.tx('major', m, 'rank_up', {'rank': 'Novice'}, 'rank')
    shared = [j for j in s.select('ledger_outbox') if j['payload'].get('audience') == 'shared']
    assert len(shared) == 1
    assert [fact['type'] for fact in shared[0]['payload']['facts']['achievements']] == ['shop_complete']


def test_context_edits_deletes_and_thread_authorization(joined):
    l, s, _, _, api, slack = joined
    w = worker(joined)
    event = {'type': 'message', 'user': 'U1', 'channel': 'CCHAT', 'ts': '1', 'text': 'Ambient private comment'}
    w.event(event, 'ambient')
    assert not s.get('ledger_outbox', 'reply:CCHAT:1')
    event.update(ts='2', text='<@UBOT> Help me plan a build')
    w.event(event, 'mention')
    w.event({'type': 'message', 'channel': 'CCHAT', 'subtype': 'message_changed', 'message': {'ts': '2', 'text': 'Revised request'}}, 'edit')
    w.outbox(claim(s, 'reply:CCHAT:2'))
    assert 'Revised request' in str(api.complete.call_args)
    assert 'Ambient private comment' not in str(api.complete.call_args)
    assert slack.chat_postMessage.call_args.kwargs['thread_ts'] == '2'
    w.event({'type': 'message', 'channel': 'CCHAT', 'subtype': 'message_deleted', 'deleted_ts': '2'}, 'delete')
    with pytest.raises(Denied):
        w.outbox(claim(s, 'reply:CCHAT:2'))


def test_bridge_dedup_null_delete_and_minimal_payload(joined):
    l, s, *_ = joined
    assert ingest_mqtt(s, 'tool_checkouts/delete', b'delete 1780000000 {"document":null}')
    assert ingest_mqtt(s, 'tool_checkouts/delete', b'delete 1780000000 {"document":null}')
    assert not ingest_mqtt(s, 'ledger_evidence/insert', b'secret')
    triggers = [j for j in s.select('ledger_inbox') if j['_id'].startswith('mqtt:')]
    assert len(triggers) == 1 and triggers[0]['payload'] == {}
    w = worker(joined)
    w.mqtt = MagicMock()
    l.tx('major', str(oid(1)), 'rank_up', {'slot': 2}, 'mqtt-test')
    w.outbox(claim(s, 'mqtt:mqtt-test'))
    call = w.mqtt.publish.call_args
    assert call.args[0] == 'ledger/v1/advancements' and call.kwargs == {'qos': 1, 'retain': False}
    l.leave(str(oid(1)))
    with pytest.raises(Denied):
        w.outbox(claim(s, 'mqtt:mqtt-test'))


def test_null_ticket_delete_enqueues_full_quest_reconciliation(joined):
    _, store, *_ = joined
    payload = b'delete 1780000000 {"document":null}'
    assert ingest_mqtt(store, 'fix_tickets/delete', payload)
    job = next(j for j in store.select('ledger_inbox') if j['kind'] == 'ticket_quest_reconcile')
    assert job['payload'] == {}


def test_expired_lease_recovered_without_old_worker_completing_new_job(joined):
    _, s, *_ = joined
    w = worker(joined)
    first = s.claim('ledger_outbox')
    second = s.claim('ledger_outbox', first['available_at'] + timedelta(seconds=1))
    # There may be older queued jobs; isolate the reclaimed job explicitly.
    first['available_at'] = now() - timedelta(seconds=1)
    s.put('ledger_outbox', first)
    recovered = s.claim('ledger_outbox')
    assert recovered['_id'] == first['_id'] and recovered['lease'] != first['lease']
    w.finish('ledger_outbox', first, 'done')
    assert s.get('ledger_outbox', first['_id'])['status'] == 'working'
