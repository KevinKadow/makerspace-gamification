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
def test_real_transactions_and_concurrent_exactly_once_kudos(env):
    _, _, source, *_ = env
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
    finally:
        assert database.startswith('ledger_test_') and len(database) == len('ledger_test_') + 32
        store.db.client.drop_database(database)
        store.db.client.close()
