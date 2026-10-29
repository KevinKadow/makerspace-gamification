from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import patch
from zoneinfo import ZoneInfo

from bson import json_util
from pymongo.errors import ServerSelectionTimeoutError
import pytest

from conftest import oid
from ledger.arrivals import Arrivals, checkin_time
from ledger.conversations import converse, self_progress_question
from ledger.domain import Denied
from ledger.engagement import Engagement
from ledger.query_tools import QueryTools, QUERY_TOOL
from ledger.storage import now
from ledger.worker import Worker, ingest_mqtt
from test_worker import claim
from test_progress_quests_delegation import member, set_participant


def observation(env, monkeypatch, n=1, source='event', text='Shared useful project feedback', category='recognition', xp=3):
    monkeypatch.setenv('LEDGER_OBSERVATION', 'true')
    monkeypatch.setenv('LEDGER_OBSERVATION_AUDIT_ONLY', 'false')
    l, s, *_ = env
    p = set_participant(l, s, n)
    p['observation_notice_delivered_at'] = now() - timedelta(minutes=1)
    s.put('ledger_participants', p)
    source = 'message:CCHAT:' + source
    s.put('ledger_context', {'_id': source, 'kind': 'message', 'member_id': member(n), 'text': text, 'at': str(now().timestamp())})
    doc = Engagement(l).capture(member(n), source, 'message', text, 'CCHAT', now())
    return {'member_id': member(n), 'evidence': [doc['_id']], 'category': category, 'xp': xp, 'reason': 'Observed constructive contribution.', 'confidence': 0.99}


def arrival(env, monkeypatch, identifier=900, n=1):
    monkeypatch.setenv('LEDGER_WELCOMES', 'true')
    l, s, src, *_ = env
    p = set_participant(l, s, n, rank=1)
    src.data['cards'].append({'_id': oid(identifier + 1000), 'uid': 'PRIVATE_CARD_' + str(identifier), 'member_id': oid(n), 'validity': 'activeMember'})
    src.data['checkins'].append({'_id': oid(identifier), 'uid': 'PRIVATE_CARD_' + str(identifier), 'timeOf': int(now().timestamp() * 1000)})
    s.put('ledger_channels', {'_id': f'membership:{member(n)}:rank:1', 'kind': 'membership', 'present': True, 'voluntary_leave': False})
    return Arrivals(l, draw=lambda: 0), str(oid(identifier))


@pytest.mark.parametrize('args', [
    {}, {'collection': 'members'}, {'collection': 'shops', '$where': 'evil'}, {'collection': 'shops', 'member_id': 'another'},
    {'collection': 'shops', 'limit': True}, {'collection': 'shops', 'limit': 26}, {'collection': 'shops', 'limit': 0},
    {'collection': 'shops', 'search': 'x' * 101}, {'collection': 'shops', 'search': {'$ne': ''}},
    {'collection': 'tools', 'shop_id': {'$ne': None}}, {'collection': 'tools', 'shop_id': 'bad-id'},
    {'collection': 'tools', 'out_of_service': 'false'},
])
def test_query_rejects_arbitrary_arguments(joined, args):
    with pytest.raises(ValueError):
        QueryTools(joined[0], member(1)).call('query_makerspace', args)


def test_query_excludes_disabled_parents_and_retains_downtime_tools(joined):
    l, _, src, *_ = joined
    src.data['shops'][0]['disabled'] = True
    src.data['tools'][4]['out_of_service'] = True
    src.data['tools'][5]['disabled'] = True
    result = QueryTools(l, member(1)).call('query_makerspace', {'collection': 'tools', 'out_of_service': True})
    assert len(result['results']) == 1 and result['results'][0]['_id'] == member(321)
    result = QueryTools(l, member(1)).query({'collection': 'tools', 'limit': 25})
    assert all(r['shop_id'] != member(201) and r['_id'] != member(322) for r in result['results'])
    assert result['retrieved_at'] and result['status'] == 'ok'


def test_queries_project_only_caller_clearances_and_escape_search(joined):
    l, _, src, *_ = joined
    src.data['tool_checkouts'] = [
        {'_id': oid(901), 'member_id': oid(1), 'tool_id': oid(311), 'approved_by_id': oid(10), 'internal_notes': 'SECRET'},
        {'_id': oid(902), 'member_id': oid(2), 'tool_id': oid(312)},
        {'_id': oid(903), 'member_id': oid(1), 'tool_id': oid(313), 'revoked_at': now()},
    ]
    results = QueryTools(l, member(1)).query({'collection': 'tool_checkouts'})['results']
    assert results == [{'_id': member(901), 'tool_id': member(311)}]
    assert not QueryTools(l, member(1)).query({'collection': 'shops', 'search': '.*'})['results']
    assert QueryTools(l, member(1)).query({'collection': 'tools', 'limit': 1})['truncated']


def test_volunteer_dates_use_new_york_calendar_and_rails_claimable_statuses(joined):
    l, _, src, *_ = joined
    clock = datetime(2026, 10, 4, 2, 0, tzinfo=timezone.utc)  # October 3 in New York.
    today = datetime(2026, 10, 3, tzinfo=timezone.utc)
    yesterday, tomorrow = today - timedelta(days=1), today + timedelta(days=1)
    src.data['volunteer_events'] = [
        {'_id': oid(901), 'title': 'Today', 'status': 'open', 'event_date': today, 'attendee_ids': [oid(1)], 'internal_notes': 'SECRET'},
        {'_id': oid(902), 'title': 'Past', 'status': 'open', 'event_date': yesterday},
        {'_id': oid(903), 'title': 'Undated', 'status': 'open'},
        {'_id': oid(904), 'title': 'Closed', 'status': 'closed', 'event_date': tomorrow},
    ]
    src.data['volunteer_tasks'] = [
        {'_id': oid(910 + i), 'title': status, 'status': status, 'claimed_by_id': oid(2), 'verified_by_id': oid(10)}
        for i, status in enumerate(('available', 'reusable', 'repeatable', 'recurring', 'claimed', 'pending', 'closed'))]
    src.data['volunteer_tasks'].append({'_id': oid(920), 'title': 'Cooling', 'status': 'recurring', 'next_available': tomorrow})
    with patch('ledger.query_tools.now', return_value=clock):
        events = QueryTools(l, member(1)).query({'collection': 'volunteer_events'})['results']
        tasks = QueryTools(l, member(1)).query({'collection': 'volunteer_tasks'})['results']
    assert [x['title'] for x in events] == ['Today', 'Undated']
    assert events[1]['schedule'] == 'unscheduled' and events[0]['schedule'] == '2026-10-03'
    assert {x['title'] for x in tasks} == {'available', 'reusable', 'repeatable', 'recurring'}
    assert 'SECRET' not in json.dumps(events) and 'claimed_by_id' not in json.dumps(tasks)


def test_query_budget_unavailable_and_self_only_progress(joined):
    l, _, src, *_ = joined
    tools = QueryTools(l, member(1), private=False)
    result = tools.call('my_progress', {})
    assert 'blockers' not in result
    with pytest.raises(ValueError):
        tools.call('my_progress', {'member_id': member(2)})
    tools.call('query_makerspace', {'collection': 'shops'})
    with pytest.raises(ValueError):
        tools.call('query_makerspace', {'collection': 'shops'})
    with patch.object(src, 'bounded', side_effect=ServerSelectionTimeoutError('PRIVATE')):
        unavailable = QueryTools(l, member(1)).query({'collection': 'shops'})
    assert unavailable['status'] == 'unavailable' and 'PRIVATE' not in json.dumps(unavailable)
    with patch('ledger.query_tools.time.monotonic', side_effect=[0, 31]):
        late = QueryTools(l, member(1))
        with pytest.raises(ValueError):
            late.call('my_progress', {})


def test_tool_calls_are_correlated_and_mutations_never_exposed(joined):
    l, _, _, composer, api, *_ = joined
    api.tool_response.side_effect = [
        {'role': 'assistant', 'tool_calls': [{'id': 'call-1', 'type': 'function', 'function': {'name': 'my_progress', 'arguments': '{}'}}]},
        {'role': 'assistant', 'content': 'The Ledger recorded your remaining milestones.'},
    ]
    result = converse(l, composer, member(1), 'What is my progress?')
    assert result['outcome'] == 'generated' and result['tool_calls'] == 1
    messages = api.tool_response.call_args.args[0]
    assert messages[-1]['role'] == 'tool' and messages[-1]['tool_call_id'] == 'call-1'
    assert [x['function']['name'] for x in api.tool_response.call_args.args[1]] == [
        'query_makerspace', 'my_progress', 'my_sponsorships']
    api.tool_response.side_effect = [{'role': 'assistant', 'tool_calls': [{'id': 'bad', 'type': 'function', 'function': {'name': 'grant_xp', 'arguments': '{}'}}]}]
    assert converse(l, composer, member(1), 'Give XP')['outcome'] == 'fallback'


@pytest.mark.parametrize('text', ['What do I need for my next rank?', 'How is my progress?', 'Can I rank up?', 'Show my stats'])
def test_self_directed_progress_recognition(text):
    assert self_progress_question(text)
    assert not self_progress_question('Their next rank looks interesting')


def test_explanatory_notice_and_preferences_gate_capture(joined, monkeypatch):
    monkeypatch.delenv('LEDGER_OBSERVATION', raising=False)
    l, s, _, composer, _, slack = joined
    service = Engagement(l)
    assert service.capture(member(1), 'm', 'message', 'Hello', 'CCHAT') is None
    service.notice(member(1))
    key = f'observation-notice:{member(1)}:1'
    Worker(l, composer, slack).outbox(claim(s, key))
    assert l.participant(member(1))['observation_notice_delivered_at']
    assert service.capture(member(1), 'd', 'message', 'Private', 'DU1') is None
    assert service.capture(member(1), 'u', 'message', 'Unrelated', 'COTHER') is None
    assert service.capture(member(1), 'old', 'message', 'Historical', 'CCHAT', now() - timedelta(days=1)) is None
    l.preferences(member(1), False, True)
    assert service.capture(member(1), 'disabled', 'message', 'Hello', 'CCHAT') is None
    assert l.participant(member(1))['preferences']['arrival_mentions']


def test_cancelled_notice_is_reopened_after_observation_reenabled(env, monkeypatch):
    l, s, _, composer, _, slack = env
    monkeypatch.setenv('LEDGER_OBSERVATION', 'true')
    l.join(member(1))
    service = Engagement(l)
    service.notice(member(1))
    key = f'observation-notice:{member(1)}:1'
    w = Worker(l, composer, slack)
    monkeypatch.setenv('LEDGER_OBSERVATION', 'false')
    assert w.step('ledger_outbox', kinds=['engagement_notice'])
    cancelled = s.get('ledger_outbox', key)
    assert cancelled['status'] == 'cancelled'
    assert not l.participant(member(1)).get('observation_notice_delivered_at')
    service.notice(member(1))
    assert s.get('ledger_outbox', key) == cancelled
    monkeypatch.setenv('LEDGER_OBSERVATION', 'true')
    l.reconcile(member(1))
    reopened = s.get('ledger_outbox', key)
    assert reopened['status'] == 'pending' and reopened['attempts'] == 0
    assert 'lease' not in reopened and 'last_error' not in reopened
    assert reopened['payload'] == cancelled['payload']
    # A cancelled worker's old lease cannot close the requeued notice.
    w.finish('ledger_outbox', cancelled, 'done')
    assert s.get('ledger_outbox', key)['status'] == 'pending'
    assert w.step('ledger_outbox', kinds=['engagement_notice'])
    assert l.participant(member(1))['observation_notice_delivered_at']
    assert service.capture(member(1), 'after-notice', 'message', 'Constructive guidance', 'CCHAT')
    service.notice(member(1))
    assert slack.chat_postMessage.call_count == 1


@pytest.mark.parametrize('status', ['pending', 'working', 'done', 'failed'])
def test_notice_retry_leaves_live_jobs_alone_and_reopens_only_terminal_failures(joined, monkeypatch, status):
    l, s, *_ = joined
    monkeypatch.setenv('LEDGER_OBSERVATION', 'true')
    service = Engagement(l)
    service.notice(member(1))
    key = f'observation-notice:{member(1)}:1'
    job = s.get('ledger_outbox', key)
    job.update(status=status, attempts=10, lease='old-lease', last_error='ProviderError')
    s.put('ledger_outbox', job)
    if status == 'done':
        # A legitimately completed notice has a receipt; missing receipts from
        # older workers are covered by the separate recovery regression.
        profile = l.preference_profile(member(1))
        profile['observation_notice_delivered_at'] = now()
        l.save_preference_profile(profile)
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda _: service.notice(member(1)), range(4)))
    current = s.get('ledger_outbox', key)
    if status == 'failed':
        assert current['status'] == 'pending' and current['attempts'] == 0 and 'lease' not in current
    else:
        assert current == job
    assert len([j for j in s.select('ledger_outbox', {'kind': 'engagement_notice'}) if j['payload']['member_id'] == member(1)]) == 1


def test_notice_retry_never_revives_an_opted_out_or_old_generation_job(joined, monkeypatch):
    l, s, *_ = joined
    monkeypatch.setenv('LEDGER_OBSERVATION', 'true')
    service = Engagement(l)
    service.notice(member(1))
    key = f'observation-notice:{member(1)}:1'
    l.preferences(member(1), False, True)
    job = s.get('ledger_outbox', key)
    job['status'] = 'cancelled'
    s.put('ledger_outbox', job)
    l.leave(member(1))
    cancelled = s.get('ledger_outbox', key)
    assert cancelled['status'] == 'cancelled'
    service.notice(member(1))
    assert s.get('ledger_outbox', key) == cancelled
    l.join(member(1))
    service.notice(member(1))
    assert s.get('ledger_outbox', key) == cancelled
    assert not s.get('ledger_outbox', f'observation-notice:{member(1)}:2')
    l.preferences(member(1), True, True)
    assert s.get('ledger_outbox', f'observation-notice:{member(1)}:2')['status'] == 'pending'


def test_notice_retry_serializes_with_optout(joined, monkeypatch):
    l, s, *_ = joined
    monkeypatch.setenv('LEDGER_OBSERVATION', 'true')
    service = Engagement(l)
    service.notice(member(1))
    key = f'observation-notice:{member(1)}:1'
    job = s.get('ledger_outbox', key)
    job['status'] = 'cancelled'
    s.put('ledger_outbox', job)
    with ThreadPoolExecutor(2) as pool:
        retry = pool.submit(service.notice, member(1))
        optout = pool.submit(l.preferences, member(1), False, True)
        retry.result()
        optout.result()
    assert not l.participant(member(1))['preferences']['observation']
    saved = s.get('ledger_outbox', key)
    service.notice(member(1))
    assert s.get('ledger_outbox', key) == saved


def test_concurrent_proposals_and_replays_never_change_accounting(joined, monkeypatch):
    l, s, *_ = joined
    proposals = [observation(joined, monkeypatch, source=str(i), xp=4) for i in range(6)]
    service = Engagement(l)
    before = l.participant(member(1))
    with ThreadPoolExecutor(6) as pool:
        records = list(pool.map(lambda i: service.commit(proposals[i], 'concurrent-' + str(i)), range(6)))
    assert all(r['status'] == 'audit_only' and r['delta'] == 0 and r['proposed_delta'] == 4 for r in records)
    assert l.participant(member(1)) == before
    assert service.commit(proposals[0], 'concurrent-0') == records[0]
    assert len(s.select('ledger_evidence', {'kind': 'ai_decision'})) == 6
    assert not s.select('ledger_awards')
    l.reconcile(member(1))
    assert l.participant(member(1))['xp'] == '0'
    assert not s.select('ledger_evidence', {'kind': 'ai_budget'})

def test_workspace_proposals_never_consume_budgets_or_award_xp(joined, monkeypatch):
    l, s, *_ = joined
    service = Engagement(l)
    for n in range(1, 10):
        proposal = observation(joined, monkeypatch, n=n, source='workspace-' + str(n), xp=13)
        service.commit(proposal, 'workspace-' + str(n))
    assert sum(int(l.participant(member(n))['xp']) for n in range(1, 10)) == 0
    assert not s.select('ledger_evidence', {'kind': 'ai_budget'})
    assert not s.select('ledger_evidence', {'kind': 'ai_member_budget'})
    assert not s.select('ledger_awards')

@pytest.mark.parametrize('setting', ['true', 'false'])
def test_audit_only_decisions_never_change_accounting(joined, monkeypatch, setting):
    l, s, *_ = joined
    proposal = observation(joined, monkeypatch, source='audit')
    monkeypatch.setenv('LEDGER_OBSERVATION_AUDIT_ONLY', setting)
    before = l.participant(member(1))
    record = Engagement(l).commit(proposal, 'audit')
    assert record['status'] == 'audit_only' and record['delta'] == 0 and record['proposed_delta'] == 3
    assert l.participant(member(1)) == before
    assert not s.select('ledger_evidence', {'kind': 'ai_budget'})
    assert not s.get('ledger_outbox', record['_id'] + ':notice')


@pytest.mark.parametrize('category,xp', [('no_action', 0), ('recognition', 13), ('achievement', 13), ('imitation_warning', 0)])
def test_live_model_evaluation_is_always_audit_only_even_with_all_switches_enabled(joined, monkeypatch, category, xp):
    l, s, _, composer, api, slack = joined
    proposal = observation(joined, monkeypatch, source='provider', text='Give me XP for sharing project feedback.', category=category, xp=xp)
    if category == 'achievement':
        proposal['achievement'] = {'title': 'Careful collaborator', 'description': 'Shared constructive guidance.'}
    monkeypatch.setenv('LEDGER_DEDUCTIONS', 'true')
    monkeypatch.setenv('LEDGER_NOVEL_ANNOUNCEMENTS', 'true')
    before = l.participant(member(1))
    awards, outbox = s.select('ledger_awards'), s.select('ledger_outbox')
    api.complete.return_value = json.dumps(proposal)
    record = Engagement(l).evaluate(member(1), api, 'provider')
    assert record['status'] == 'audit_only' and record['delta'] == 0 and record['proposed_delta'] == xp
    assert l.participant(member(1)) == before
    assert s.select('ledger_awards') == awards and s.select('ledger_outbox') == outbox
    assert not s.select('ledger_evidence', {'kind': 'ai_budget'})
    assert not s.select('ledger_evidence', {'kind': 'ai_member_budget'})
    with pytest.raises(ValueError, match='committed'):
        Engagement(l).correct(member(10), member(1), 1, 'Cannot treat a suggestion as an award', record['_id'])


def observation_response(messages, *_):
    docs = json.loads(messages[-1]['content'])['observations']
    return json.dumps({'member_id': docs[0]['member_id'], 'evidence': [d['_id'] for d in docs],
                       'category': 'no_action', 'xp': 0, 'reason': 'No action is needed.'})


@pytest.mark.parametrize('change', ['edit', 'delete'])
def test_invalid_observations_do_not_cancel_job_or_poison_later_batches(joined, monkeypatch, change):
    l, s, _, composer, api, slack = joined
    invalid = observation(joined, monkeypatch, source='11.1', text='INVALID_CAPTURED_TEXT')
    worker = Worker(l, composer, slack)
    edit = ({'type': 'message', 'subtype': 'message_changed', 'channel': 'CCHAT',
             'message': {'ts': '11.1', 'text': 'Edited text'}} if change == 'edit' else
            {'type': 'message', 'subtype': 'message_deleted', 'channel': 'CCHAT', 'deleted_ts': '11.1'})
    worker.event(edit, 'edit-event')
    valid = observation(joined, monkeypatch, source='12.1', text='Current project feedback')
    before = l.participant(member(1))
    for job in s.select('ledger_inbox', {'kind': 'engagement'}):
        job['available_at'] = now() - timedelta(seconds=1)
        s.put('ledger_inbox', job)
    api.complete.side_effect = observation_response
    assert worker.step('ledger_inbox', kinds=['engagement'])
    assert s.select('ledger_inbox', {'kind': 'engagement', 'status': 'done'})
    discarded = s.get('ledger_evidence', invalid['evidence'][0])
    assert discarded['status'] == 'cancelled'
    assert discarded['cancellation_reason'] == 'Original message changed or was deleted.'
    assert s.get('ledger_evidence', valid['evidence'][0])['status'] == 'evaluated'
    assert 'INVALID_CAPTURED_TEXT' not in json.dumps(api.complete.call_args.args[0])
    newer = observation(joined, monkeypatch, source='13.1', text='Newer project feedback')
    assert Engagement(l).evaluate(member(1), api, 'later-job')['evidence'] == newer['evidence']
    assert l.participant(member(1))['xp'] == before['xp']
    assert not s.select('ledger_awards')


def test_all_invalid_observations_finish_without_inference(joined, monkeypatch):
    l, s, _, _, api, *_ = joined
    invalid = observation(joined, monkeypatch, source='deleted')
    s.delete('ledger_context', 'message:CCHAT:deleted')
    assert Engagement(l).evaluate(member(1), api, 'empty-job') is None
    assert s.get('ledger_evidence', invalid['evidence'][0])['status'] == 'cancelled'
    api.complete.assert_not_called()
    newer = observation(joined, monkeypatch, source='newer')
    api.complete.side_effect = observation_response
    assert Engagement(l).evaluate(member(1), api, 'newer-job')['evidence'] == newer['evidence']


@pytest.mark.parametrize('kind', ['kudos_metadata', 'volunteer'])
def test_invalid_nonmessage_source_is_cancelled_without_discarding_valid_observation(joined, monkeypatch, kind):
    l, s, src, _, api, *_ = joined
    valid = observation(joined, monkeypatch, source='valid-message')
    service = Engagement(l)
    if kind == 'kudos_metadata':
        source = 'kudos:metadata'
        s.put('ledger_evidence', {'_id': source, 'kind': 'kudos', 'giver': member(1)})
    else:
        source = 'volunteer:' + member(900)
        src.data['volunteer_credits'].append({'_id': oid(900), 'member_id': oid(1), 'status': 'approved'})
    captured = service.capture(member(1), source, kind)
    if kind == 'kudos_metadata':
        s.put('ledger_evidence', {'_id': source, 'kind': 'kudos', 'giver': member(2)})
    else:
        src.data['volunteer_credits'][0]['status'] = 'rejected'
    api.complete.side_effect = observation_response
    assert service.evaluate(member(1), api, 'source-changed')['evidence'] == valid['evidence']
    assert s.get('ledger_evidence', captured['_id'])['status'] == 'cancelled'
    assert captured['_id'] not in json.dumps(api.complete.call_args.args[0])


def test_invalid_records_before_batch_limit_do_not_strand_valid_overflow(joined, monkeypatch):
    l, s, _, _, api, *_ = joined
    for i in range(9):
        observation(joined, monkeypatch, source=f'bad-{i}', text=f'INVALID_{i}')
        s.delete('ledger_context', f'message:CCHAT:bad-{i}')
    refs = [observation(joined, monkeypatch, source=f'good-{i}')['evidence'][0] for i in range(10)]
    api.complete.side_effect = observation_response
    service = Engagement(l)
    first = service.evaluate(member(1), api, 'large-job')
    assert len(first['evidence']) == 8 and set(first['evidence']) <= set(refs)
    followup = s.get('ledger_inbox', 'engagement-followup:large-job')
    assert followup['status'] == 'pending'
    second = service.evaluate(member(1), api, followup['_id'])
    assert len(second['evidence']) == 2
    assert not s.select('ledger_evidence', {'kind': 'observation', 'status': 'pending'})
    assert len(s.select('ledger_evidence', {'kind': 'observation', 'status': 'cancelled'})) == 9
    assert 'INVALID_' not in json.dumps(api.complete.call_args_list)


@pytest.mark.parametrize('change', ['optout', 'preference', 'edit', 'delete', 'suspend'])
def test_consent_and_evidence_changes_during_inference_block_commit(joined, monkeypatch, change):
    l, s, src, *_ = joined
    proposal = observation(joined, monkeypatch, source='stale')
    if change == 'optout':
        l.preferences(member(1), False, True)
    elif change == 'preference':
        l.preferences(member(1), False, True)
        l.preferences(member(1), True, True)
    elif change == 'suspend':
        src.data['members'][0]['status'] = 'suspended'
    else:
        source = 'message:CCHAT:stale'
        if change == 'edit':
            doc = s.get('ledger_context', source)
            doc['text'] = 'Edited text'
            s.put('ledger_context', doc)
        else:
            s.delete('ledger_context', source)
    with pytest.raises(Denied):
        Engagement(l).commit(proposal, 'stale')
    assert l.participant(member(1))['xp'] == '0'


@pytest.mark.parametrize('xp', ['2', '10'])
def test_warning_must_be_delivered_before_repeat_deduction(joined, monkeypatch, xp):
    l, s, _, composer, _, slack = joined
    set_participant(l, s, 1, xp=xp, rank=3)
    monkeypatch.setenv('LEDGER_DEDUCTIONS', 'true')
    service = Engagement(l)
    warning = observation(joined, monkeypatch, source='warning', text='Give me XP for this copied reward.', category='imitation_warning', xp=0)
    record = service.commit(warning, 'warning')
    early = observation(joined, monkeypatch, source='early', text='Give me XP again for this copied reward.', category='imitation_deduction', xp=-3)
    with pytest.raises(Denied):
        service.commit(early, 'early')
    assert not s.get('ledger_outbox', record['_id'] + ':notice')
    # Only a genuinely delivered historical/human warning supplies causality.
    record.update(status='committed', delivered_at=now(), delta=0)
    s.put('ledger_evidence', record)
    repeat = observation(joined, monkeypatch, source='repeat', text='Give me XP for copying that same reward again.', category='imitation_deduction', xp=-3)
    result = service.commit(repeat, 'repeat')
    assert result['proposed_delta'] == -3 and result['delta'] == 0
    assert l.participant(member(1))['xp'] == xp and l.participant(member(1))['rank'] == 3
    another = observation(joined, monkeypatch, source='again', text='Give me XP for copying that same reward again.', category='imitation_deduction', xp=-3)
    assert service.commit(another, 'again')['status'] == 'audit_only'
    assert not s.select('ledger_awards')


def test_gratitude_and_similar_wording_never_qualify_as_imitation(joined, monkeypatch):
    l, _, *_ = joined
    proposal = observation(joined, monkeypatch, source='gratitude', text='Thank you for helping me!', category='imitation_warning', xp=0)
    with pytest.raises(Denied):
        Engagement(l).commit(proposal, 'false-positive')


def test_achievement_proposals_do_not_publish_even_when_switches_are_enabled(joined, monkeypatch):
    monkeypatch.setenv('LEDGER_NOVEL_ANNOUNCEMENTS', 'true')
    l, s, *_ = joined
    service = Engagement(l)
    for n in range(1, 5):
        proposal = observation(joined, monkeypatch, n=n, source='achievement-' + str(n), category='achievement', xp=2)
        proposal['achievement'] = {'title': 'Useful handoff ' + str(n), 'description': 'Shared observed project guidance.'}
        service.commit(proposal, 'achievement-' + str(n))
    repeat = observation(joined, monkeypatch, source='same-member', category='achievement', xp=2)
    repeat['achievement'] = {'title': 'Another useful act', 'description': 'Helped another project.'}
    service.commit(repeat, 'same-member')
    public = [j for j in s.select('ledger_outbox') if j['_id'].startswith('ai-decision:') and j['payload'].get('audience') == 'shared']
    assert not public
    assert not s.select('ledger_evidence', {'kind': 'ai_public_cooldown'})
    assert not s.select('ledger_evidence', {'kind': 'ai_budget'})


@pytest.mark.parametrize('clock', [datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc), datetime(2026, 3, 8, 7, 1, tzinfo=timezone.utc),
                                  datetime(2026, 11, 1, 5, 59, tzinfo=timezone.utc), datetime(2026, 11, 1, 6, 1, tzinfo=timezone.utc)])
def test_audit_calendar_is_stable_across_dst_transitions(joined, monkeypatch, clock):
    l, s, *_ = joined
    proposal = observation(joined, monkeypatch, source='dst')
    with patch('ledger.engagement.now', return_value=clock):
        record = Engagement(l).commit(proposal, 'dst')
    assert record['day'] == clock.astimezone(ZoneInfo('America/New_York')).date().isoformat()
    assert not s.get('ledger_evidence', 'ai-budget:' + record['day'])


def test_arrival_random_reserved_once_and_cooldown_across_rank_changes(joined, monkeypatch):
    l, s, *_ = joined
    service, key = arrival(joined, monkeypatch)
    first = service.reserve(key)
    assert first['selected'] and first['status'] == 'reserved'
    service.draw = lambda: (_ for _ in ()).throw(AssertionError('Must not reroll'))
    assert service.reserve(key) == first
    service, second = arrival(joined, monkeypatch, identifier=901)
    assert service.reserve(second)['status'] == 'skipped'
    set_participant(l, s, 1, rank=2)
    assert not service.live(first)
    assert s.get('ledger_evidence', 'welcome-cooldown:' + member(1))['arrival'] == first['_id']


def test_arrival_identity_ambiguity_retained_and_stale_packets(joined, monkeypatch):
    l, s, src, *_ = joined
    service, key = arrival(joined, monkeypatch)
    payload = ('insert 123 ' + json_util.dumps({'document': {'_id': oid(900), 'uid': 'DO_NOT_PERSIST_CARD'}})).encode()
    assert not ingest_mqtt(s, 'checkins/insert', payload, retained=True)
    assert not ingest_mqtt(s, 'checkins/update', payload)
    assert ingest_mqtt(s, 'checkins/insert', payload)
    assert 'DO_NOT_PERSIST_CARD' not in json.dumps(s.select('ledger_inbox'), default=str)
    src.data['cards'].append({'_id': oid(9999), 'uid': src.data['cards'][0]['uid'], 'member_id': oid(2)})
    assert service.reserve(key) is None
    src.data['cards'].pop()
    src.data['checkins'][0]['timeOf'] = int((now() - timedelta(minutes=11)).timestamp() * 1000)
    assert service.reserve(key) is None
    assert checkin_time({'time': 'bad'}) is None


def test_arrival_mentions_and_unknown_outcome_keep_cooldown(joined, monkeypatch):
    l, s, _, composer, api, slack = joined
    service, key = arrival(joined, monkeypatch)
    reserved = service.reserve(key)
    api.complete.return_value = 'Welcome <@U2> <!here> <@U3>.'
    w = Worker(l, composer, slack)
    job = claim(s, reserved['_id'])
    slack.chat_postMessage.side_effect = TimeoutError('unknown delivery')
    with pytest.raises(TimeoutError):
        w.outbox(job)
    sent = slack.chat_postMessage.call_args.kwargs
    assert sent['text'].count('<@') == 1 and '<@U1>' in sent['text'] and '<!here>' not in sent['text']
    assert sent['channel'] == 'CRANK1'
    assert 'PRIVATE_CARD' not in json.dumps(api.complete.call_args.args, default=str)
    assert s.get('ledger_evidence', reserved['_id'])['status'] == 'attempted'
    w.outbox(job)
    assert slack.chat_postMessage.call_count == 1
    assert s.get('ledger_evidence', 'welcome-cooldown:' + member(1))


def test_arrival_live_preferences_and_favorite_shop_distinct_clearances(joined, monkeypatch):
    l, s, src, *_ = joined
    service, key = arrival(joined, monkeypatch)
    src.data['tool_checkouts'] = [
        {'_id': oid(950 + i), 'member_id': oid(1), 'tool_id': tool} for i, tool in enumerate((oid(311), oid(311), oid(321), oid(322)))]
    assert service.favorite_shop(member(1)) == 'Shop2'
    reserved = service.reserve(key)
    l.preferences(member(1), True, False)
    assert not service.live(reserved)
    assert l.participant(member(1))['preferences']['observation']


def test_tool_conversation_reserves_prompt_policy_and_reuses_text_on_retry(joined):
    l, s, _, composer, api, slack = joined
    w = Worker(l, composer, slack, bot_id='UBOT')
    text = 'What is my progress?'
    api.tool_response.return_value = {'role': 'assistant', 'content': 'The Ledger has your calculated progress.'}
    w.event({'type': 'message', 'user': 'U1', 'channel': 'DU1', 'channel_type': 'im', 'ts': '300.123', 'text': text}, 'conversation-ev')
    key = 'reply:DU1:300.123'
    job = claim(s, key)
    w.outbox(job)
    saved = s.get('ledger_outbox', key)
    assert saved['prompt_selection']['matrix']['version'] == composer.matrix.snapshot()['version']
    assert saved['composed']['prompt_variation'] in ('archivist', 'mentor', 'wry_grimoire')
    assert saved['composed']['prompt_scope'] == 'member:' + member(1)
    api.tool_response.reset_mock()
    w.outbox(job)
    api.tool_response.assert_not_called()
    assert slack.chat_postMessage.call_args.kwargs['blocks'][-1]['elements'][0]['text']['text'] == 'Private progress detail'


def test_staff_correction_route_is_linked_append_only_and_inaccessible_to_delegates(joined, monkeypatch):
    l, s, _, composer, _, slack = joined
    l.join(member(10))
    from ledger.authority import Authority
    proposal = observation(joined, monkeypatch, source='correction-route', xp=3)
    decision = Engagement(l).commit(proposal, 'correction-route')
    # Preserve append-only correction of decisions committed by older releases.
    decision.update(status='committed', delta=3)
    s.put('ledger_evidence', decision)
    l.award(member(1), decision['_id'], '3', 'status', historical=True)
    s.put('ledger_evidence', {'_id': 'ai-budget:' + decision['day'], 'kind': 'ai_budget', 'positive': 3})
    Authority(l).grant(member(10), member(2), ['learning_review'], {'kind': 'global'}, 'Review only')
    w = Worker(l, composer, slack)
    with pytest.raises(Denied):
        w.admin_command(member(2), ['correct-ai', decision['_id'], '-3', 'No correction authority'], 'forged-correction')
    w.admin_command(member(10), ['correct-ai', decision['_id'], '-3', 'Independent staff review'], 'staff-correction')
    assert l.participant(member(1))['xp'] == '0'
    assert s.get('ledger_evidence', decision['_id']) == decision
    corrections = s.select('ledger_evidence', {'kind': 'ai_correction'})
    assert len(corrections) == 1 and corrections[0]['decision'] == decision['_id']
    assert s.get('ledger_evidence', 'ai-budget:' + decision['day'])['positive'] == 3
