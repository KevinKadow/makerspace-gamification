from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import Mock, patch

import pytest

from conftest import oid
from ledger.domain import Denied
from ledger.messages import Composer, default_template
from ledger.prompt_library import EXAMPLE_FACTS
from ledger.storage import enqueue, now
from ledger.worker import Worker
from test_worker import claim


def message(env, key, member=1, audience='member', kind='rank_up'):
    ledger, store, _, composer, _, slack = env
    enqueue(store, 'ledger_outbox', key, 'message', {'member_id': str(oid(member)),
        'type': kind, 'audience': audience, 'facts': EXAMPLE_FACTS})
    Worker(ledger, composer, slack).outbox(claim(store, key))
    return store.get('ledger_outbox', key)['composed']


def test_shared_posts_avoid_two_recent_voices_across_types_and_members(joined):
    _, store, _, composer, *_ = joined
    composer.choose = Mock(side_effect=lambda candidates: candidates[0])
    selected = [message(joined, f'public-{i}', member=1 + i % 2, audience='shared',
                        kind=['rank_up', 'shop_complete', 'boss'][i % 3])['prompt_variation'] for i in range(9)]
    for i, variation in enumerate(selected):
        assert variation not in selected[max(0, i - 2):i]
    assert len(composer.choose.call_args_list[0].args[0]) == 3
    assert len(composer.choose.call_args_list[1].args[0]) == 2
    assert len(composer.choose.call_args_list[2].args[0]) == 1
    history = store.get('ledger_context', 'prompt_history:shared')
    assert history['recent'] == selected[-2:][::-1]
    assert history['expires_at'] > now() + timedelta(days=29)
    # Neither another member's DM nor a DM about the same public subject consumes this history.
    assert message(joined, 'private-one')['prompt_variation'] == selected[0]
    assert message(joined, 'private-two', member=2)['prompt_variation'] == selected[0]
    assert store.get('ledger_context', 'prompt_history:shared') == history


def test_member_history_is_shared_by_kudos_notifications_and_nonparticipant_kudos(joined):
    ledger, store, _, composer, _, slack = joined
    composer.choose = lambda candidates: candidates[0]
    giver, recipient = str(oid(1)), str(oid(2))
    ledger.kudos(giver, recipient, 'Thanks!', key='rotation-1', expected_participation=True)
    Worker(ledger, composer, slack).outbox(claim(store, 'kudos:rotation-1:recipient'))
    first = store.get('ledger_outbox', 'kudos:rotation-1:recipient')['composed']
    second = message(joined, 'progress', member=2)
    ledger.leave(recipient)
    ledger.kudos(giver, recipient, 'Thanks again!', key='rotation-2', expected_participation=False)
    # A new composer/worker must retain history across a process restart and an audience change.
    Worker(ledger, Composer(store, composer.api, chooser=lambda cs: cs[0]), slack).outbox(claim(store, 'kudos:rotation-2:recipient'))
    third = store.get('ledger_outbox', 'kudos:rotation-2:recipient')['composed']
    assert len({c['prompt_variation'] for c in (first, second, third)}) == 3
    assert {c['prompt_scope'] for c in (first, second, third)} == {'member:' + recipient}
    assert not store.get('ledger_context', 'prompt_history:shared')


def test_channel_conversations_share_public_history_but_dm_conversations_use_member(joined):
    ledger, store, _, composer, _, slack = joined
    composer.choose = lambda candidates: candidates[0]
    first = message(joined, 'announcement', audience='shared')
    worker = Worker(ledger, composer, slack, bot_id='UBOT')
    worker.event({'type': 'message', 'user': 'U2', 'channel': 'CCHAT', 'ts': '1', 'text': '<@UBOT> Hello'}, 'event-shared')
    worker.outbox(claim(store, 'reply:CCHAT:1'))
    shared = store.get('ledger_outbox', 'reply:CCHAT:1')['composed']
    assert shared['prompt_scope'] == 'shared' and shared['prompt_variation'] != first['prompt_variation']
    worker.event({'type': 'message', 'user': 'U2', 'channel': 'DU2', 'ts': '2', 'text': 'Hello'}, 'event-dm')
    worker.outbox(claim(store, 'reply:DU2:2'))
    private = store.get('ledger_outbox', 'reply:DU2:2')['composed']
    assert private['prompt_scope'] == 'member:' + str(oid(2))
    assert private['prompt_variation'] == first['prompt_variation']


@pytest.mark.parametrize('size', [1, 2, 3, 5])
def test_small_and_large_custom_sets_relax_only_oldest_exclusions(joined, size):
    ledger, store, _, composer, *_ = joined
    composer.choose = lambda candidates: candidates[0]
    template = default_template('rank_up', 'member')
    template['variations'] = [{**template['variations'][0], 'id': f'voice-{i}'} for i in range(size)]
    composer.publish(str(oid(10)), template, ledger.admin)
    selected = [message(joined, f'sized-{i}')['prompt_variation'] for i in range(12)]
    for i, variation in enumerate(selected):
        window = min(2, size - 1)
        assert variation not in selected[max(0, i - window):i]
    assert len(store.get('ledger_context', 'prompt_history:member:' + str(oid(1)))['recent']) == 2


def test_publication_preserves_history_and_removed_variants_do_not_block(joined):
    ledger, _, _, composer, *_ = joined
    composer.choose = lambda candidates: candidates[0]
    first = message(joined, 'before-publish')
    template = default_template('rank_up', 'member')
    template['variations'][0]['user'] += ' Be brief.'
    composer.publish(str(oid(10)), template, ledger.admin)
    second = message(joined, 'after-publish')
    assert second['prompt_variation'] != first['prompt_variation']
    template['variations'] = [{**v, 'id': 'new-' + v['id']} for v in template['variations']]
    composer.publish(str(oid(10)), template, ledger.admin)
    assert message(joined, 'replaced')['prompt_variation'].startswith('new-')


def test_crash_after_reservation_reuses_snapshot_and_does_not_advance_history(joined):
    ledger, store, _, composer, api, slack = joined
    composer.choose = Mock(side_effect=lambda candidates: candidates[0])
    api.complete.side_effect = RuntimeError('Simulated worker crash before saving composed text')
    with pytest.raises(RuntimeError):
        message(joined, 'interrupted')
    job = store.get('ledger_outbox', 'interrupted')
    assert 'composed' not in job and job['prompt_selection']['template']['variations'][0]['id'] == 'archivist'
    original = api.complete.call_args
    history = store.get('ledger_context', 'prompt_history:member:' + str(oid(1)))
    template = default_template('rank_up', 'member')
    template['variations'][0]['user'] = 'Changed after crash: {facts}'
    composer.publish(str(oid(10)), template, ledger.admin)
    restarted = Composer(store, api, chooser=Mock(side_effect=AssertionError('A retry must not select again')))
    api.complete.side_effect = None
    Worker(ledger, restarted, slack).outbox(claim(store, 'interrupted'))
    assert api.complete.call_args == original
    assert store.get('ledger_context', 'prompt_history:member:' + str(oid(1))) == history
    assert composer.choose.call_count == 1


def test_fallback_and_slack_retry_consume_only_one_choice(joined):
    ledger, store, _, composer, api, slack = joined
    composer.choose = Mock(side_effect=lambda cs: cs[0])
    api.complete.side_effect = TimeoutError()
    slack.chat_postMessage.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        message(joined, 'failed-send', audience='shared')
    composition = store.get('ledger_outbox', 'failed-send')['composed']
    history = store.get('ledger_context', 'prompt_history:shared')
    assert composition['outcome'] == 'fallback'
    slack.chat_postMessage.side_effect = None
    Worker(ledger, composer, slack).outbox(claim(store, 'failed-send'))
    assert store.get('ledger_outbox', 'failed-send')['composed'] == composition
    assert store.get('ledger_context', 'prompt_history:shared') == history
    assert api.complete.call_count == composer.choose.call_count == 1
    assert message(joined, 'next', audience='shared')['prompt_variation'] != composition['prompt_variation']


def test_selection_and_job_reservation_rollback_together_and_reject_stale_leases(joined):
    ledger, store, _, composer, _, slack = joined
    put = store.put
    def fail_job_save(collection, document):
        if collection == 'ledger_outbox' and document.get('prompt_selection'):
            raise RuntimeError('Job save failed')
        put(collection, document)
    with patch.object(store, 'put', side_effect=fail_job_save), pytest.raises(RuntimeError):
        message(joined, 'rollback')
    assert not store.get('ledger_context', 'prompt_history:member:' + str(oid(1)))
    assert not store.get('ledger_outbox', 'rollback').get('prompt_selection')
    stale = claim(store, 'rollback')
    current = {**stale, 'lease': 'another-worker'}
    put('ledger_outbox', current)
    with pytest.raises(Denied):
        Worker(ledger, composer, slack).persist_composition(stale, 'rank_up', 'member', {})
    assert not store.get('ledger_context', 'prompt_history:member:' + str(oid(1)))


def test_concurrent_workers_reserve_distinct_choices_before_generation(joined):
    ledger, store, _, _, api, slack = joined
    jobs = []
    for i in range(3):
        key = f'concurrent-{i}'
        enqueue(store, 'ledger_outbox', key, 'message', {'member_id': str(oid(1 + i % 2)),
            'type': 'rank_up', 'audience': 'shared', 'facts': EXAMPLE_FACTS})
        jobs.append(claim(store, key))
    barrier = Barrier(3)
    def complete(*args):
        barrier.wait(timeout=5)
        return 'Progress recorded.'
    api.complete.side_effect = complete
    def deliver(job):
        Worker(ledger, Composer(store, api, chooser=lambda cs: cs[0]), slack).outbox(job)
        return store.get('ledger_outbox', job['_id'])['composed']['prompt_variation']
    with ThreadPoolExecutor(max_workers=3) as pool:
        chosen = list(pool.map(deliver, jobs))
    assert len(set(chosen)) == 3


def test_history_expiry_and_admin_preview_isolation(joined):
    _, store, _, composer, *_ = joined
    composer.choose = lambda cs: cs[0]
    first = message(joined, 'first', audience='shared')
    history = store.get('ledger_context', 'prompt_history:shared')
    composer.compose('rank_up', 'shared', EXAMPLE_FACTS)  # Template-test is an isolated preview.
    assert store.get('ledger_context', 'prompt_history:shared') == history
    history['expires_at'] = now() - timedelta(seconds=1)
    store.put('ledger_context', history)  # Enforce expiry even before Mongo's TTL pass.
    assert message(joined, 'after-expiry', audience='shared')['prompt_variation'] == first['prompt_variation']
