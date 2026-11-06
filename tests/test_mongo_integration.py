"""Run only against an explicitly supplied disposable replica set."""
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from pymongo.errors import DuplicateKeyError

from conftest import oid
from ledger.domain import Ledger
from ledger.messages import Composer
from ledger.storage import connect


@pytest.mark.skipif(not os.environ.get('LEDGER_TEST_MONGO_URI'), reason='Set LEDGER_TEST_MONGO_URI to a disposable Mongo replica set')
def test_real_transactions_and_concurrent_exactly_once_kudos(env, monkeypatch):
    _, _, source, _, _, slack = env
    database = 'ledger_test_' + uuid4().hex
    store = connect(os.environ['LEDGER_TEST_MONGO_URI'], database)
    try:
        store.ready()
        store.indexes()
        l = Ledger(store, source)
        l.seed()
        a, b = str(oid(1)), str(oid(2))
        l.join(a)
        l.join(b)
        def submit(_):
            try:
                return l.kudos(a, b, 'Thank you', key='one', public=True, expected_participation=True)
            except DuplicateKeyError:
                return l.kudos(a, b, 'Thank you', key='one', public=True, expected_participation=True)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(submit, range(12)))
        assert l.participant(b)['xp'] == '17'
        assert len(store.select('ledger_evidence', {'kind': 'kudos'})) == 1
        assert len(store.select('ledger_outbox', {'kind': 'kudos'})) == 2
        def fail(s):
            s.put('ledger_evidence', {'_id': 'must-rollback'})
            raise RuntimeError('rollback')
        with pytest.raises(RuntimeError):
            store.atomic(fail)
        assert not store.get('ledger_evidence', 'must-rollback')
        # Independent workers serialize prompt history through the same Mongo document.
        composer = Composer(store, None, chooser=lambda candidates: candidates[0])
        def reserve(_):
            try:
                return store.atomic(lambda s: composer.reserve(s, 'rank_up', 'shared', 'shared'))
            except DuplicateKeyError:
                return store.atomic(lambda s: composer.reserve(s, 'rank_up', 'shared', 'shared'))
        with ThreadPoolExecutor(max_workers=3) as pool:
            selections = list(pool.map(reserve, range(3)))
        assert len({s['template']['variations'][0]['id'] for s in selections}) == 3
        assert len(store.get('ledger_context', 'prompt_history:shared')['recent']) == 2
        # Real transaction races for first daily budget creation and revocation.
        from ledger.authority import Authority
        from ledger.domain import Denied
        from ledger.engagement import Engagement
        from ledger.storage import now
        from datetime import timedelta
        grant = Authority(l).grant(str(oid(10)), b, ['learning_review'], {'kind': 'global'}, 'Integration review')
        evidence = l.submit(a, 'learning-challenge', 'Observed integration evidence')
        def approve():
            try:
                return l.review(b, evidence['_id'])['status']
            except Denied:
                return 'denied'
        with ThreadPoolExecutor(2) as pool:
            approval = pool.submit(approve)
            revocation = pool.submit(Authority(l).revoke, str(oid(10)), grant['_id'], 'Concurrent explicit revocation')
            outcome = approval.result()
            revocation.result()
        assert outcome in ('approved', 'denied')
        assert store.get('ledger_relationships', grant['_id'])['status'] == 'revoked'
        with pytest.raises(Denied):
            l.review(b, evidence['_id'])
        monkeypatch.setenv('LEDGER_OBSERVATION', 'true')
        monkeypatch.setenv('LEDGER_OBSERVATION_AUDIT_ONLY', 'false')
        participant = l.participant(a)
        participant['observation_notice_delivered_at'] = now() - timedelta(minutes=1)
        store.put('ledger_participants', participant)
        store.put('ledger_channels', {'_id': 'chat', 'kind': 'channel', 'channel_id': 'CCHAT'})
        proposals = []
        for i in range(6):
            reference = f'message:CCHAT:integration-{i}'
            store.put('ledger_context', {'_id': reference, 'kind': 'message', 'member_id': a, 'text': 'Constructive feedback'})
            observed = Engagement(l).capture(a, reference, 'message', 'Constructive feedback', 'CCHAT', now())
            proposals.append({'member_id': a, 'evidence': [observed['_id']], 'category': 'recognition', 'xp': 4, 'reason': 'Verified feedback'})
        before = l.participant(a)['xp']
        def recognize(i):
            try:
                return Engagement(l).commit(proposals[i], 'integration-' + str(i))['status']
            except Denied:
                return 'denied'
        with ThreadPoolExecutor(6) as pool:
            outcomes = list(pool.map(recognize, range(6)))
        assert outcomes == ['audit_only'] * 6
        from ledger.rules import amount
        assert amount(l.participant(a)['xp']) == amount(before)
        l.reconcile(a)
        assert amount(l.participant(a)['xp']) == amount(before)
        # Game consent and observation preferences are independent. A notice
        # retry remains valid regardless of which game-leave transaction wins.
        c = str(oid(4))
        l.join(c)
        notice_key = f'observation-notice:{c}:1'
        notice = store.get('ledger_outbox', notice_key)
        notice['status'] = 'cancelled'
        store.put('ledger_outbox', notice)
        with ThreadPoolExecutor(2) as pool:
            retry = pool.submit(Engagement(l).notice, c)
            leave = pool.submit(l.leave, c)
            retry.result()
            leave.result()
        assert not l.active(c)
        assert l.preference_profile(c)['preferences']['observation'] is True
        assert store.get('ledger_outbox', notice_key)['status'] == 'pending'
        # An explicit observation opt-out must block a concurrent retry and
        # delivery, including a notice reopened before the preference commits.
        notice = store.get('ledger_outbox', notice_key)
        notice['status'] = 'cancelled'
        store.put('ledger_outbox', notice)
        with ThreadPoolExecutor(2) as pool:
            retry = pool.submit(Engagement(l).notice, c)
            optout = pool.submit(l.preferences, c, False, True)
            retry.result()
            optout.result()
        assert l.preference_profile(c)['preferences']['observation'] is False
        from ledger.worker import Worker
        from test_worker import claim
        worker = Worker(l, Composer(store, None), slack)
        job = store.get('ledger_outbox', notice_key)
        if job['status'] == 'pending':
            job = claim(store, notice_key)
            with pytest.raises(Denied):
                worker.outbox(job)
            worker.finish('ledger_outbox', job, 'cancelled', error='Denied')
        Engagement(l).notice(c)
        assert store.get('ledger_outbox', notice_key)['status'] == 'cancelled'
        slack.chat_postMessage.assert_not_called()
        # Dynamic contribution keys must select only legacy mismatched notices
        # on the real server, including when other rows have missing/bad shapes.
        monkeypatch.delenv('LEDGER_QUEST_REVIEW_CHANNEL_ID', raising=False)
        for key, fields in (
            ('legacy-channel', {'contributions': {a: {'review_channel_id': 'COLD'}}}),
            ('legacy-current', {'contributions': {a: {'review_channel_id': 'CNEW'}}}),
            ('indexed-parent', {'review_notice_channel': 'CNEW', 'contributions': {a: {'review_channel_id': 'COLD'}}}),
            ('no-contributions', {}), ('null-contributions', {'contributions': None}),
            ('list-contributions', {'contributions': []})):
            # Historical addresses belong to existing activities; new-record
            # preparation intentionally strips inherited Slack addresses.
            store.put('ledger_quests', {'_id': key, 'status': 'completed'})
            store.put('ledger_quests', {'_id': key, 'status': 'completed', **fields})
        assert {row['_id'] for row in store.legacy_review_channels('ledger_quests', 'CNEW')} == {'legacy-channel'}
    finally:
        assert database.startswith('ledger_test_') and len(database) == len('ledger_test_') + 32
        store.db.client.drop_database(database)
        store.db.client.close()
