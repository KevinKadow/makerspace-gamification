from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from itertools import count
from threading import Event
from unittest.mock import patch

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.slack_response import SlackResponse

from conftest import oid
from ledger.community import Community
from ledger.domain import Denied
from ledger.ledger_quests import LedgerQuests
from ledger.quest_policy import LEDGER_AUTHOR
from ledger.quests import Quests
from ledger.review_notifications import reconcile
from ledger.storage import now
from ledger.worker import Worker
from test_progress_quests_delegation import member, published, set_participant
from test_worker import claim
from test_quest_generation import generator


@pytest.fixture
def reviews(joined, monkeypatch):
    monkeypatch.setenv('LEDGER_QUEST_REVIEW_CHANNEL_ID', 'CREVIEW')
    l, s, src, composer, api, slack = joined
    sequence = count(1)
    slack.conversations_info.return_value = {'channel': {'is_private': True, 'is_member': True}}
    slack.chat_postMessage.side_effect = lambda **kwargs: {'ts': f'100.{next(sequence)}'}
    slack.chat_update.side_effect = lambda **kwargs: {'ts': kwargs['ts']}
    return joined


def worker(env):
    return Worker(env[0], env[3], env[5])


def drain(env):
    w = worker(env)
    for _ in range(30):
        if not w.step('ledger_outbox', kinds=['review_notice']):
            return
    raise AssertionError('Review jobs did not settle')


def pending(env, key='one', catalog='first-build', learners=()):
    return env[0].submit(member(1), catalog, 'Made and tested a safe build.', list(learners), key=key)


def slack_error(code, status=200):
    response = SlackResponse(client=None, http_verb='POST', api_url='https://slack.com/api/chat.update',
        req_args={}, data={'ok': False, 'error': code}, headers={}, status_code=status)
    return SlackApiError(code, response)


@pytest.mark.parametrize('configured', [True, False])
@pytest.mark.parametrize('collection,kind', [('ledger_quests', None), ('ledger_relationships', 'quest_project')])
@pytest.mark.parametrize('fields', [{}, {'contributions': None}, {'contributions': []},
                                  {'contributions': 'invalid'}, {'contributions': {'invalid-member': None}}])
def test_malformed_contributions_preserve_record_without_review_work(reviews, monkeypatch, configured, collection, kind, fields):
    _, store, _, _, _, slack = reviews
    if not configured:
        monkeypatch.delenv('LEDGER_QUEST_REVIEW_CHANNEL_ID')
    doc = {'_id': 'legacy-malformed', 'kind': kind, 'status': 'open', **deepcopy(fields)}
    store.put(collection, doc)
    assert store.get(collection, doc['_id']) == doc
    assert not store.legacy_review_channels(collection, 'CREVIEW')
    reconcile(store)
    drain(reviews)
    assert not store.select('ledger_outbox', {'kind': 'review_notice'})
    slack.chat_postMessage.assert_not_called()


@pytest.mark.parametrize('collection,kind', [('ledger_quests', None), ('ledger_relationships', 'quest_project')])
def test_valid_contribution_notice_survives_malformed_sibling(reviews, collection, kind):
    _, store, _, _, _, slack = reviews
    store.put(collection, {'_id': 'mixed-contributions', 'kind': kind, 'status': 'open',
        'contributions': {'invalid-member': None, member(1): {'status': 'pending', 'description': 'Working cabinet.'}}})
    drain(reviews)
    saved = store.get(collection, 'mixed-contributions')['contributions']
    assert saved['invalid-member'] is None
    assert saved[member(1)]['review_message_ts'] == '100.1'
    slack.chat_postMessage.assert_called_once()
    assert 'Working cabinet.' in slack.chat_postMessage.call_args.kwargs['text']


def test_legacy_channel_lookup_preserves_existing_addresses_with_notices_disabled(reviews, monkeypatch):
    _, store, *_ = reviews
    monkeypatch.delenv('LEDGER_QUEST_REVIEW_CHANNEL_ID')
    for key, fields in (
        ('legacy-channel', {'contributions': {member(1): {'review_channel_id': 'COLD'}}}),
        ('legacy-current', {'contributions': {member(1): {'review_channel_id': 'CNEW'}}}),
        ('indexed-parent', {'review_notice_channel': 'CNEW', 'contributions': {member(1): {'review_channel_id': 'COLD'}}}),
        ('no-contributions', {}), ('null-contributions', {'contributions': None}),
        ('list-contributions', {'contributions': []})):
        store.put('ledger_quests', {'_id': key, 'status': 'completed'})
        store.put('ledger_quests', {'_id': key, 'status': 'completed', **fields})
    assert store.get('ledger_quests', 'legacy-channel')['contributions'][member(1)]['review_channel_id'] == 'COLD'
    assert {row['_id'] for row in store.legacy_review_channels('ledger_quests', 'CNEW')} == {'legacy-channel'}


@pytest.mark.parametrize('approve', [True, False])
def test_submission_saves_timestamp_and_review_updates_same_message(reviews, approve):
    l, s, _, _, api, slack = reviews
    doc = pending(reviews)
    assert not doc.get('review_message_ts')
    slack.chat_postMessage.assert_not_called()
    drain(reviews)
    posted = s.get('ledger_evidence', doc['_id'])
    assert posted['review_message_ts'] == '100.1' and posted['review_channel_id'] == 'CREVIEW'
    l.review(member(10), doc['_id'], approve, 'Independently verified.' if approve else 'Need corrected evidence.')
    drain(reviews)
    slack.chat_postMessage.assert_called_once()
    slack.chat_update.assert_called_once()
    args = slack.chat_update.call_args.kwargs
    assert args['channel'] == 'CREVIEW' and args['ts'] == '100.1'
    assert ('approved' if approve else 'rejected') in args['text']
    assert s.get('ledger_evidence', doc['_id'])['review_message_ts'] == '100.1'
    api.complete.assert_not_called()


def test_missing_original_posts_replacement_and_saves_new_timestamp(reviews):
    l, s, _, _, _, slack = reviews
    doc = pending(reviews)
    drain(reviews)
    l.review(member(10), doc['_id'], False, 'Missing evidence.')
    slack.chat_update.side_effect = slack_error('message_not_found')
    drain(reviews)
    assert slack.chat_postMessage.call_count == 2
    assert s.get('ledger_evidence', doc['_id'])['review_message_ts'] == '100.2'
    assert 'rejected' in slack.chat_postMessage.call_args.kwargs['text']
    slack.chat_update.side_effect = lambda **kwargs: {'ts': kwargs['ts']}
    l.review(member(10), doc['_id'], True)
    drain(reviews)
    assert slack.chat_update.call_args.kwargs['ts'] == '100.2'
    assert slack.chat_postMessage.call_count == 2


def test_update_permission_or_rate_limit_failure_does_not_create_duplicate(reviews):
    l, s, _, _, _, slack = reviews
    doc = pending(reviews)
    drain(reviews)
    l.review(member(10), doc['_id'], False, 'Correct the evidence.')
    slack.chat_update.side_effect = slack_error('cant_update_message')
    assert worker(reviews).step('ledger_outbox', kinds=['review_notice'])
    slack.chat_postMessage.assert_called_once()
    assert s.get('ledger_evidence', doc['_id'])['status'] == 'rejected'
    job = s.get('ledger_outbox', s.get('ledger_evidence', doc['_id'])['review_notice_job_id'])
    assert job['status'] == 'pending' and job['last_error'] == 'SlackApiError'
    slack.chat_update.side_effect = lambda **kwargs: {'ts': kwargs['ts']}
    worker(reviews).outbox(claim(s, job['_id']))
    assert slack.chat_postMessage.call_count == 1


@pytest.mark.parametrize('close', ['publish', 'reject', 'withdraw', 'disable', 'author_loss'])
def test_quest_publication_and_all_closure_paths_update_notice(reviews, close):
    l, s, _, _, _, slack = reviews
    set_participant(l, s, 3, rank=4)
    q = Quests(l).draft(member(3), 'Build a jig', 'Make a safe jig.', 'Show observable evidence.', 1)
    assert not s.select('ledger_outbox', {'kind': 'review_notice'})
    Quests(l).submit_draft(member(3), q['_id'])
    drain(reviews)
    if close in ('publish', 'reject'):
        Quests(l).publish(member(10), q['_id'], 20, approve=close == 'publish', reason='Review decision.')
    elif close == 'withdraw':
        Quests(l).withdraw(member(3), q['_id'])
    elif close == 'disable':
        Quests(l).disable(member(10), q['_id'], 'Not available.')
    else:
        set_participant(l, s, 3, rank=1)
        l.reconcile(member(3))
    drain(reviews)
    slack.chat_postMessage.assert_called_once()
    assert slack.chat_update.call_args.kwargs['ts'] == '100.1'
    assert 'Review closed' in slack.chat_update.call_args.kwargs['text']


def test_quest_completion_resubmissions_have_separate_messages_and_specialized_evidence_is_not_duplicated(reviews):
    l, s, _, _, _, slack = reviews
    q = published(reviews, classification='first_build', catalog='first-build', reward=42)
    drain(reviews)
    Quests(l).accept(member(1), q['_id'])
    first = Quests(l).submit(member(1), q['_id'], 'First attempt.')
    drain(reviews)
    first_ts = s.get('ledger_evidence', first['_id'])['review_message_ts']
    assert not s.get('ledger_evidence', first['specialized_evidence']).get('review_message_ts')
    Quests(l).verify(member(10), first['_id'], False, 'Need correction.')
    drain(reviews)
    second = Quests(l).submit(member(1), q['_id'], 'Corrected observable completion.')
    drain(reviews)
    second_ts = s.get('ledger_evidence', second['_id'])['review_message_ts']
    assert second_ts != first_ts
    Quests(l).verify(member(10), second['_id'])
    drain(reviews)
    assert s.get('ledger_evidence', first['_id'])['review_message_ts'] == first_ts
    assert slack.chat_update.call_args.kwargs['ts'] == second_ts
    assert l.participant(member(1))['xp'] == '42'
    assert slack.chat_postMessage.call_count == 3  # publication and two completion attempts


def test_group_contribution_has_its_own_saved_ts_and_verified_update(reviews):
    l, s, src, _, _, slack = reviews
    src.data['volunteer_tasks'].append({'_id': oid(900), 'title': 'Build arcade'})
    c = Community(l)
    q = c.create_quest(member(10), 'Build arcade', 'Working cabinet', ['wood', 'electronics'], member(900))
    c.quest(member(1), q['_id'], 'join', role='wood')
    c.quest(member(1), q['_id'], 'submit', description='Built the cabinet.')
    drain(reviews)
    assert s.get('ledger_quests', q['_id'])['contributions'][member(1)]['review_message_ts'] == '100.1'
    c.quest(member(10), q['_id'], 'verify', member=member(1))
    drain(reviews)
    slack.chat_postMessage.assert_called_once()
    assert 'verified' in slack.chat_update.call_args.kwargs['text']


def test_reconciliation_recovers_legacy_closed_nested_notices(reviews, monkeypatch):
    l, s, src, _, _, slack = reviews
    src.data['volunteer_tasks'].append({'_id': oid(900), 'title': 'Build arcade'})
    service = Community(l)
    q = service.create_quest(member(10), 'Build arcade', 'Working cabinet', ['wood', 'electronics'], member(900))
    service.quest(member(1), q['_id'], 'join', role='wood')
    service.quest(member(1), q['_id'], 'submit', description='Built the cabinet.')
    drain(reviews)
    current = s.get('ledger_quests', q['_id'])
    current.update(status='disabled', disable_reason='Closed project.')
    s.put('ledger_quests', current)
    drain(reviews)
    # Before the parent marker existed only contributions carried addresses.
    s.data['ledger_quests'][q['_id']].pop('review_notice_channel')
    monkeypatch.setenv('LEDGER_QUEST_REVIEW_CHANNEL_ID', 'CNEWREVIEW')
    reconcile(s)
    drain(reviews)
    current = s.get('ledger_quests', q['_id'])
    assert current['contributions'][member(1)]['review_channel_id'] == 'CNEWREVIEW'
    assert current['review_notice_channel'] == 'CNEWREVIEW'
    assert 'closed' in slack.chat_postMessage.call_args.kwargs['text']
    assert slack.chat_postMessage.call_count == 2
    with patch.object(s, 'atomic', wraps=s.atomic) as transactions:
        reconcile(s)
        transactions.assert_not_called()


def test_mentoring_acknowledgments_refresh_pending_message_without_new_post(reviews):
    l, s, _, _, _, slack = reviews
    doc = pending(reviews, catalog='mentoring-session', learners=[member(2)])
    drain(reviews)
    assert '0/1' in slack.chat_postMessage.call_args.kwargs['text']
    with pytest.raises(ValueError, match='acknowledge'):
        l.review(member(10), doc['_id'])
    l.acknowledge(member(2), doc['_id'])
    drain(reviews)
    assert '1/1' in slack.chat_update.call_args.kwargs['text']
    l.review(member(10), doc['_id'])
    drain(reviews)
    slack.chat_postMessage.assert_called_once()
    assert slack.chat_update.call_count == 2


def test_duplicate_jobs_use_current_closed_state_and_post_once(reviews):
    l, s, _, _, _, slack = reviews
    doc = pending(reviews)
    l.review(member(10), doc['_id'], False, 'Closed before delivery.')
    assert len(s.select('ledger_outbox', {'kind': 'review_notice'})) == 2
    drain(reviews)
    slack.chat_postMessage.assert_called_once()
    slack.chat_update.assert_not_called()
    assert 'rejected' in slack.chat_postMessage.call_args.kwargs['text']


def test_outbox_and_activity_rollback_together_and_denied_review_creates_no_closure(reviews):
    l, s, *_ = reviews
    before = deepcopy(s.data)
    def failed(tx):
        tx.put('ledger_evidence', {'_id': 'rollback', 'kind': 'submission', 'status': 'pending', 'description': 'Evidence'})
        raise ValueError('rollback')
    with pytest.raises(ValueError):
        s.atomic(failed)
    assert s.data == before
    doc = pending(reviews)
    jobs = s.select('ledger_outbox', {'kind': 'review_notice'})
    with pytest.raises(Denied):
        l.review(member(1), doc['_id'])
    assert s.select('ledger_outbox', {'kind': 'review_notice'}) == jobs
    assert s.get('ledger_evidence', doc['_id'])['status'] == 'pending'


def test_backfill_after_configuration_and_terminal_failure_retry(reviews, monkeypatch):
    l, s, _, _, _, slack = reviews
    monkeypatch.delenv('LEDGER_QUEST_REVIEW_CHANNEL_ID')
    doc = pending(reviews)
    assert not s.select('ledger_outbox', {'kind': 'review_notice'})
    monkeypatch.setenv('LEDGER_QUEST_REVIEW_CHANNEL_ID', 'CREVIEW')
    reconcile(s)
    job = s.select('ledger_outbox', {'kind': 'review_notice'})[0]
    job.update(status='failed', attempts=10)
    s.put('ledger_outbox', job)
    reconcile(s)
    reconcile(s)
    assert len(s.select('ledger_outbox', {'kind': 'review_notice', 'status': 'pending'})) == 1
    drain(reviews)
    slack.chat_postMessage.assert_called_once()
    assert s.get('ledger_evidence', doc['_id'])['review_message_ts'] == '100.1'


def test_reconciliation_does_not_scan_or_rewrite_settled_history(reviews, monkeypatch):
    l, s, *_ = reviews
    doc = pending(reviews)
    drain(reviews)
    l.review(member(10), doc['_id'], False, 'Closed review')
    drain(reviews)
    monkeypatch.delenv('LEDGER_QUEST_REVIEW_CHANNEL_ID')
    for i in range(100):
        s.put('ledger_evidence', {'_id': f'historical-{i}', 'kind': 'submission', 'status': 'approved', 'description': 'Old evidence'})
    monkeypatch.setenv('LEDGER_QUEST_REVIEW_CHANNEL_ID', 'CREVIEW')
    with patch.object(s, 'select', wraps=s.select) as selects, patch.object(s, 'atomic', wraps=s.atomic) as transactions:
        reconcile(s)
    assert transactions.call_count == 0
    assert all(call.args[1] or call.kwargs.get('query') for call in selects.call_args_list)


def test_reconciliation_refreshes_closed_notices_after_channel_change(reviews, monkeypatch):
    l, s, _, _, _, slack = reviews
    doc = pending(reviews)
    drain(reviews)
    l.review(member(10), doc['_id'], False, 'Closed review')
    drain(reviews)
    monkeypatch.setenv('LEDGER_QUEST_REVIEW_CHANNEL_ID', 'CNEWREVIEW')
    reconcile(s)
    drain(reviews)
    assert s.get('ledger_evidence', doc['_id'])['review_channel_id'] == 'CNEWREVIEW'
    assert slack.chat_postMessage.call_count == 2
    with patch.object(s, 'atomic', wraps=s.atomic) as transactions:
        reconcile(s)
    assert transactions.call_count == 0


@pytest.mark.parametrize('approve', [True, False])
def test_closed_notice_recovers_when_same_channel_configuration_returns(reviews, monkeypatch, approve):
    l, s, _, _, _, slack = reviews
    doc = pending(reviews)
    drain(reviews)
    monkeypatch.delenv('LEDGER_QUEST_REVIEW_CHANNEL_ID')
    l.review(member(10), doc['_id'], approve, 'Reviewed while notices were disabled.')
    assert s.get('ledger_evidence', doc['_id'])['review_notice_dirty'] is True
    assert not s.select('ledger_outbox', {'kind': 'review_notice', 'status': 'pending'})
    monkeypatch.setenv('LEDGER_QUEST_REVIEW_CHANNEL_ID', 'CREVIEW')
    reconcile(s)
    drain(reviews)
    assert ('approved' if approve else 'rejected') in slack.chat_update.call_args.kwargs['text']
    assert slack.chat_update.call_args.kwargs['ts'] == '100.1'
    assert 'review_notice_dirty' not in s.get('ledger_evidence', doc['_id'])
    slack.chat_postMessage.assert_called_once()
    with patch.object(s, 'atomic', wraps=s.atomic) as transactions:
        reconcile(s)
        transactions.assert_not_called()


@pytest.mark.parametrize('info', [{'is_private': False}, {'is_private': True, 'is_ext_shared': True}, {'is_private': True, 'is_member': False}])
def test_review_payload_never_posts_into_unprotected_or_unjoined_channel(reviews, info):
    _, s, _, _, _, slack = reviews
    doc = pending(reviews)
    slack.conversations_info.return_value = {'channel': info}
    assert worker(reviews).step('ledger_outbox', kinds=['review_notice'])
    slack.chat_postMessage.assert_not_called()
    assert not s.get('ledger_evidence', doc['_id']).get('review_message_ts')


def test_uncertain_initial_post_retry_uses_same_client_id(reviews):
    _, s, _, _, _, slack = reviews
    doc = pending(reviews)
    slack.chat_postMessage.side_effect = [TimeoutError(), {'ts': '100.2'}]
    w = worker(reviews)
    assert w.step('ledger_outbox', kinds=['review_notice'])
    job = s.get('ledger_outbox', s.get('ledger_evidence', doc['_id'])['review_notice_job_id'])
    w.outbox(claim(s, job['_id']))
    calls = slack.chat_postMessage.call_args_list
    assert calls[0].kwargs['client_msg_id'] == calls[1].kwargs['client_msg_id']
    assert s.get('ledger_evidence', doc['_id'])['review_message_ts'] == '100.2'


def test_concurrent_status_change_defers_second_worker_and_updates_after_first_post(reviews):
    l, s, _, _, _, slack = reviews
    doc = pending(reviews)
    started, proceed = Event(), Event()
    def slow_post(**kwargs):
        started.set()
        assert proceed.wait(5)
        return {'ts': '100.1'}
    slack.chat_postMessage.side_effect = slow_post
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(worker(reviews).step, 'ledger_outbox', kinds=['review_notice'])
        assert started.wait(5)
        l.review(member(10), doc['_id'], False, 'Reviewed while Slack delivery was in flight.')
        assert worker(reviews).step('ledger_outbox', kinds=['review_notice'])
        latest = s.get('ledger_outbox', s.get('ledger_evidence', doc['_id'])['review_notice_job_id'])
        assert latest['last_error'] == 'ReviewDeliveryBusy' and latest['attempts'] == 0
        proceed.set()
        assert first.result()
    worker(reviews).outbox(claim(s, latest['_id']))
    slack.chat_postMessage.assert_called_once()
    assert 'rejected' in slack.chat_update.call_args.kwargs['text']
    assert s.get('ledger_evidence', doc['_id'])['status'] == 'rejected'


def test_expired_activity_lock_is_recoverable_and_old_owner_cannot_overwrite(reviews):
    _, s, *_ = reviews
    doc = pending(reviews)
    current = s.get('ledger_evidence', doc['_id'])
    current['review_delivery_lock'] = {'token': 'dead-worker', 'until': now() - timedelta(seconds=1)}
    s.put('ledger_evidence', current)
    drain(reviews)
    saved = s.get('ledger_evidence', doc['_id'])
    assert saved['review_message_ts'] == '100.1' and 'review_delivery_lock' not in saved


def test_large_escaped_evidence_stays_within_slack_block_limits(reviews):
    _, s, _, _, _, slack = reviews
    s.put('ledger_evidence', {'_id': 'long', 'kind': 'submission', 'status': 'pending', 'description': '<' * 2000})
    drain(reviews)
    blocks = slack.chat_postMessage.call_args.kwargs['blocks']
    assert all(0 < len(b['text']['text']) <= 3000 for b in blocks)


def test_ledger_proposal_edit_closes_original_without_copying_its_message_ts(reviews):
    l, s, _, _, _, slack = reviews
    q = {'_id': 'proposal', 'logical_id': 'proposal', 'kind': 'ledger_quest', 'creator': LEDGER_AUTHOR,
         'quest_type': 'individual', 'title': 'Build a jig', 'description': 'Make and test a safe jig.',
         'criteria': 'Show a usable jig and feedback.', 'shop_ids': [], 'tool_ids': [], 'disciplines': [],
         'target_rank': 1, 'status': 'pending_review'}
    s.put('ledger_quests', q)
    drain(reviews)
    edits = {k: q[k] for k in ('title', 'description', 'criteria', 'shop_ids', 'tool_ids', 'disciplines')}
    edits['title'] = 'Build a useful jig'
    reviewed = LedgerQuests(l).review(member(10), 'proposal', 50, edits=edits)
    drain(reviews)
    assert reviewed['_id'] != q['_id']
    assert not s.get('ledger_quests', reviewed['_id']).get('review_message_ts')
    assert 'superseded' in slack.chat_update.call_args.kwargs['text']
    slack.chat_postMessage.assert_called_once()


def cooperative(env):
    l, s, *_ = env
    q = {'_id': 'shared', 'logical_id': 'shared', 'kind': 'ledger_quest', 'creator': LEDGER_AUTHOR,
         'quest_type': 'cooperative', 'title': 'Build an arcade', 'description': 'Build and test a working arcade.',
         'criteria': 'Show the shared working result.', 'shop_ids': [], 'tool_ids': [],
         'disciplines': [{'name': 'wood', 'expectation': 'Working cabinet'}, {'name': 'electronics', 'expectation': 'Working controls'}],
         'target_rank': 1, 'status': 'pending_review'}
    s.put('ledger_quests', q)
    LedgerQuests(l).review(member(10), q['_id'], 13)
    return q['_id']


def test_shared_completion_request_and_unverified_contribution_close_in_place(reviews):
    l, s, _, _, _, slack = reviews
    key = cooperative(reviews)
    service = LedgerQuests(l)
    set_participant(l, s, 3, rank=1)
    for n, discipline in ((1, 'wood'), (2, 'electronics'), (3, 'wood')):
        service.contribute(member(n), key, 'join', role=discipline)
        service.contribute(member(n), key, 'submit', description=f'Documented contribution {n}.')
    drain(reviews)
    for n in (1, 2):
        service.contribute(member(10), key, 'verify', member=member(n))
    drain(reviews)
    state = s.get('ledger_relationships', 'cooperative:shared')
    completion_ts = state['review_message_ts']
    pending_ts = state['contributions'][member(3)]['review_message_ts']
    assert 'Awaiting review' in slack.chat_postMessage.call_args.kwargs['text']
    posts = slack.chat_postMessage.call_count
    service.finalize(member(10), key, 'The shared arcade was observed working.')
    drain(reviews)
    assert slack.chat_postMessage.call_count == posts
    updates = {c.kwargs['ts']: c.kwargs['text'] for c in slack.chat_update.call_args_list}
    assert 'completed' in updates[completion_ts] and 'observed working' in updates[completion_ts]
    assert 'closed' in updates[pending_ts] and 'No XP awarded' in updates[pending_ts]
    assert l.participant(member(3))['xp'] == '0'


def test_disabling_shared_project_closes_pending_contribution_message(reviews):
    l, s, _, _, _, slack = reviews
    key = cooperative(reviews)
    LedgerQuests(l).contribute(member(1), key, 'join', role='wood')
    LedgerQuests(l).contribute(member(1), key, 'submit', description='Cabinet evidence.')
    drain(reviews)
    ts = s.get('ledger_relationships', 'cooperative:shared')['contributions'][member(1)]['review_message_ts']
    Quests(l).disable(member(10), key, 'Project no longer available.')
    drain(reviews)
    updates = {c.kwargs['ts']: c.kwargs['text'] for c in slack.chat_update.call_args_list}
    assert 'closed' in updates[ts] and 'no longer available' in updates[ts]


def test_review_channel_cannot_be_a_registered_member_game_channel(reviews, monkeypatch):
    _, s, _, _, _, slack = reviews
    monkeypatch.setenv('LEDGER_QUEST_REVIEW_CHANNEL_ID', 'CCHAT')
    pending(reviews)
    worker(reviews).step('ledger_outbox', kinds=['review_notice'])
    slack.chat_postMessage.assert_not_called()


def test_configuration_change_posts_in_new_channel_and_saves_new_address(reviews, monkeypatch):
    l, s, _, _, _, slack = reviews
    doc = pending(reviews)
    drain(reviews)
    monkeypatch.setenv('LEDGER_QUEST_REVIEW_CHANNEL_ID', 'CNEWREVIEW')
    l.review(member(10), doc['_id'], False, 'Needs correction.')
    drain(reviews)
    assert slack.chat_postMessage.call_count == 2
    assert slack.chat_postMessage.call_args.kwargs['channel'] == 'CNEWREVIEW'
    slack.chat_update.assert_not_called()
    assert s.get('ledger_evidence', doc['_id'])['review_channel_id'] == 'CNEWREVIEW'


@pytest.mark.parametrize('legacy_first', [True, False])
def test_generated_quest_legacy_and_new_jobs_share_one_message_and_its_closure(generator, monkeypatch, legacy_first):
    g, env = generator
    l, s, _, _, api, slack = env
    monkeypatch.setenv('LEDGER_QUEST_REVIEW_CHANNEL_ID', 'CSTAFF')
    result = g.run(rank=1, request_id='shared-notice')
    # A deployment can also contain the old generated-proposal job format.
    s.put('ledger_outbox', {'_id': 'legacy-generated-notice', 'kind': 'quest_review_notice',
        'status': 'pending', 'attempts': 0, 'available_at': now(),
        'payload': {'quest_id': result['quest_id'], 'channel': 'CSTAFF'}})
    w = worker(env)
    if legacy_first:
        assert w.step('ledger_outbox', kinds=['quest_review_notice'])
        drain(env)
    else:
        drain(env)
        assert w.step('ledger_outbox', kinds=['quest_review_notice'])
    slack.chat_postMessage.assert_called_once()
    ts = s.get('ledger_quests', result['quest_id'])['review_message_ts']
    LedgerQuests(l).review(member(10), result['quest_id'], 13)
    drain(env)
    assert slack.chat_update.call_args.kwargs['ts'] == ts
    assert 'published' in slack.chat_update.call_args.kwargs['text']
    slack.chat_postMessage.assert_called_once()
