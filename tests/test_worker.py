from datetime import timedelta
from unittest.mock import MagicMock

import pytest

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


def test_public_kudos_keeps_body_live_ranks_and_independent_receipts(joined):
    l, s, _, _, api, slack = joined
    w = worker(joined)
    a, b = str(oid(1)), str(oid(2))
    body = '*Thanks* _maker_ ~oops~ <https://example.com|link>\n> quote\n`code` :hammer: 🌱'
    l.kudos(a, b, body, key='render', public=True, expected_participation=True)
    dm = claim(s, 'kudos:render:recipient')
    w.outbox(dm)
    assert slack.chat_postMessage.call_args.kwargs['blocks'][2]['text']['text'] == body
    l.leave(b)
    ranks = s.get('ledger_catalog', 'rank_display')['ranks']
    ranks[0].update(emoji=':custom_maker:', name='Seedling')
    l.publish_ranks(str(oid(10)), ranks)
    public = claim(s, 'kudos:render:shared')
    w.outbox(public)
    call = slack.chat_postMessage.call_args.kwargs
    assert call['channel'] == 'CCHAT'
    assert call['blocks'][0]['text']['text'] == ':custom_maker: <@U1> → <@U2>'
    assert call['blocks'][2]['text']['text'] == body
    assert 'Seedling <@U1>' in call['text'] and 'Seedling <@U2>' not in call['text']
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
    assert s.get('ledger_outbox', 'unauthorized:remove')


def test_only_major_automatic_posts_coalesce_and_historical_posts_suppressed(joined):
    l, s, *_ = joined
    m = str(oid(1))
    l.tx('major', m, 'challenge', {'challenge': 'small'}, 'small')
    l.tx('major', m, 'rank_up', {'rank': 'Novice'}, 'rank')
    l.tx('major', m, 'shop_complete', {'shop': 'Wood'}, 'wood')
    l.tx('major', m, 'boss', {}, 'old', historical=True)
    shared = [j for j in s.select('ledger_outbox') if j['payload'].get('audience') == 'shared']
    assert len(shared) == 1 and len(shared[0]['payload']['facts']['achievements']) == 2
    assert shared[0]['available_at'] > now() + timedelta(seconds=55)
    assert len(s.select('ledger_outbox', {'kind': 'mqtt'})) == 2


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
