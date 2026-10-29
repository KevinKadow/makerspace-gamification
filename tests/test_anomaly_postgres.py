"""Opt-in disposable PostgreSQL validation; never targets a production-named DB."""
import os
from urllib.parse import urlparse
from uuid import uuid4

import pytest
pytest.importorskip("psycopg")
from ledger.anomaly.state import State
from datetime import datetime, timedelta, timezone


@pytest.mark.skipif(not os.environ.get("ANOMALY_TEST_DATABASE_URL"), reason="Requires a disposable PostgreSQL database")
def test_checkpoint_restart_and_transaction_rollback():
    uri = os.environ["ANOMALY_TEST_DATABASE_URL"]
    assert urlparse(uri).path.lstrip("/").startswith("ledger_anomaly_test_"), "Use an explicitly disposable ledger_anomaly_test_* DB"
    key = "test:" + uuid4().hex
    state = State(uri)
    state.initialize()
    try:
        with pytest.raises(RuntimeError):
            with state.atomic():
                state.capture(key, key, {"safe": True}, {"token": key})
                raise RuntimeError("crash before commit")
        assert state.checkpoint(key) is None
        assert state.capture(key, key, {"safe": True}, {"token": key})
        state.close()

        state = State(uri)
        assert state.checkpoint(key) == {"token": key}
        assert not state.capture(key, key, {"safe": True}, {"token": key})
        with pytest.raises(RuntimeError):
            with state.atomic():
                state.advance_checkpoint(key, {"idle": 1})
                raise RuntimeError("crash before checkpoint commit")
        assert state.checkpoint(key) == {"token": key}
        for idle in range(100):
            state.advance_checkpoint(key, {"idle": idle})
        state.close()
        state = State(uri)
        assert state.checkpoint(key) == {"idle": 99}
        assert state.connection.execute("SELECT count(*) AS n FROM anomaly_events WHERE collection=%s", (key,)).fetchone()["n"] == 1
    finally:
        state.connection.execute("DELETE FROM anomaly_events WHERE id=%s", (key,))
        state.connection.execute("DELETE FROM anomaly_checkpoints WHERE collection=%s", (key,))
        state.close()


@pytest.mark.skipif(not os.environ.get('ANOMALY_TEST_DATABASE_URL'), reason='Requires a disposable PostgreSQL database')
def test_security_atomic_evidence_rollups_restart_and_reporting_permissions():
    from psycopg import sql, errors
    uri = os.environ['ANOMALY_TEST_DATABASE_URL']
    assert urlparse(uri).path.lstrip('/').startswith('ledger_anomaly_test_')
    key = 'security-test:' + uuid4().hex
    role = 'security_reader_' + uuid4().hex
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
    event = {'id': key, 'source_key': key, 'kind': 'profile_change', 'at': now, 'member_id': 'member1', 'actor_id': None}
    state = State(uri)
    state.connection.execute(sql.SQL('CREATE ROLE {} NOLOGIN').format(sql.Identifier(role)))
    state.initialize(reporting_user=role)
    try:
        with pytest.raises(RuntimeError):
            with state.atomic():
                state.capture(key, key, {'safe': True}, {'token': key})
                state.security_replace(key, [event])
                state.security_put_rollup(now, key, {'at': now, 'kind': key, 'value': 1})
                raise RuntimeError('crash')
        assert not state.security_events(source=key)
        assert state.checkpoint(key) is None
        assert not any(r['kind']==key for r in state.security_rollups())
        with state.atomic():
            state.capture(key, key, {'safe': True}, {'token': key})
            assert state.security_replace(key, [event])
            state.security_put_rollup(now, key, {'at': now, 'kind': key, 'value': 1})
            state.processed(key)
        state.close()
        state = State(uri)
        assert state.security_events(source=key)==[event]
        assert not state.security_replace(key,[event])
        assert state.security_events(kind='profile_change',member='member1',since=now)
        assert state.checkpoint(key)=={'token':key}
        assert state.security_replace(key,[])
        assert not state.security_get_event(key)
        state.put('security_report_metrics',key,{'report':'account_changes','cohort':'profile_change','value':1,'sample_size':1})
        state.connection.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(role)))
        assert not state.connection.execute("SELECT * FROM reporting.cohort_metrics WHERE report='security_account_changes'").fetchall()
        for table in ('anomaly_security_events','anomaly_security_rollups','anomaly_documents'):
            with pytest.raises(errors.InsufficientPrivilege):
                state.connection.execute(sql.SQL('SELECT * FROM {}').format(sql.Identifier(table)))
        state.connection.execute('RESET ROLE')
        state.put('security_report_metrics',key,{'report':'account_changes','cohort':'profile_change','value':5,'sample_size':5})
        state.connection.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(role)))
        assert state.connection.execute("SELECT * FROM reporting.cohort_metrics WHERE report='security_account_changes'").fetchone()['value']==5
        assert not state.connection.execute("SELECT has_table_privilege(current_user,'reporting.cohort_metrics','DELETE') AS allowed").fetchone()['allowed']
        with pytest.raises((errors.InsufficientPrivilege, errors.ObjectNotInPrerequisiteState)):
            state.connection.execute("DELETE FROM reporting.cohort_metrics")
    finally:
        state.connection.execute('RESET ROLE')
        state.connection.execute('DELETE FROM anomaly_events WHERE id=%s',(key,))
        state.connection.execute('DELETE FROM anomaly_checkpoints WHERE collection=%s',(key,))
        state.connection.execute('DELETE FROM anomaly_documents WHERE key=%s',(key,))
        state.connection.execute('DELETE FROM anomaly_security_events WHERE source_key=%s',(key,))
        state.connection.execute('DELETE FROM anomaly_security_rollups WHERE kind=%s',(key,))
        state.connection.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
        state.connection.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
        state.close()


@pytest.mark.skipif(not os.environ.get('ANOMALY_TEST_DATABASE_URL'), reason='Requires a disposable PostgreSQL database')
def test_security_learning_receipts_expire_with_history_or_superseded_generation():
    uri = os.environ['ANOMALY_TEST_DATABASE_URL']
    assert urlparse(uri).path.lstrip('/').startswith('ledger_anomaly_test_')
    prefix = 'learning-retention:' + uuid4().hex + ':'
    state = State(uri)
    state.initialize()
    recent = datetime.now(timezone.utc).isoformat()
    expired = (datetime.now(timezone.utc)-timedelta(days=366*6)).isoformat()
    try:
        for status in ('active', 'shadow', 'disabled', 'superseded'):
            state.put('security_detectors', prefix+status, {'status': status})
            state.put('security_learning_receipts', prefix+status, {'at': recent, 'revision': prefix+status, 'hit': False})
        state.put('security_learning_receipts', prefix+'legacy-old', {'at': expired, 'hit': True})
        state.put('security_learning_receipts', prefix+'legacy-recent', {'at': recent, 'hit': False})
        state.put('security_learning_receipts', prefix+'active-old', {'at': expired, 'revision': prefix+'active', 'hit': True})
        with pytest.raises(RuntimeError, match='rollback'):
            with state.atomic():
                state.security_prune(60)
                assert not state.get('security_learning_receipts', prefix+'legacy-old')
                raise RuntimeError('rollback')
        assert state.get('security_learning_receipts', prefix+'legacy-old')
        state.security_prune(60)
        for name in ('superseded', 'legacy-old', 'active-old'):
            assert not state.get('security_learning_receipts', prefix+name)
        for name in ('active', 'shadow', 'disabled', 'legacy-recent'):
            assert state.get('security_learning_receipts', prefix+name)
        assert state.get('security_detectors', prefix+'superseded')  # Keep model review history.
    finally:
        state.connection.execute('DELETE FROM anomaly_documents WHERE key LIKE %s', (prefix+'%',))
        state.close()


@pytest.mark.skipif(not os.environ.get('ANOMALY_TEST_DATABASE_URL'), reason='Requires a disposable PostgreSQL database')
def test_security_retention_prunes_completed_snapshots_and_preserves_pending_groups():
    from psycopg.types.json import Jsonb
    uri = os.environ['ANOMALY_TEST_DATABASE_URL']
    assert urlparse(uri).path.lstrip('/').startswith('ledger_anomaly_test_')
    prefix = 'retention-test:' + uuid4().hex + ':'
    state = State(uri)
    state.initialize()
    recent = datetime.now(timezone.utc).isoformat()
    expired = (datetime.now(timezone.utc)-timedelta(days=366*6)).isoformat()
    namespaces = ('security_incidents', 'security_deliveries', 'security_messages')
    evidence_ids = []

    def incident(name, status='resolved', at=expired):
        key = prefix + name
        evidence = {'id': key+':event', 'source_key': key, 'kind': 'profile_change', 'at': at,
                    'member_id': name+':member', 'actor_id': name+':actor'}
        evidence_ids.append(evidence['id'])
        state.connection.execute('INSERT INTO anomaly_security_events(id,source_key,kind,at,member_id,actor_id,data) '
            'VALUES(%s,%s,%s,%s,%s,%s,%s)', (evidence['id'], key, evidence['kind'], at,
                evidence['member_id'], evidence['actor_id'], Jsonb(evidence)))
        row = {'id': key, 'status': status, 'created_at': at, 'evidence': [evidence['id']],
               'evidence_snapshot': [evidence], 'members': [evidence['member_id']], 'actors': [evidence['actor_id']]}
        state.put('security_incidents', key, row)
        return row

    def notification(name, rows, statuses):
        message_id = prefix + name + ':message'
        deliveries = []
        for n, (row, status) in enumerate(zip(rows, statuses)):
            job = {'id': prefix+name+':job:'+str(n), 'incident_id': row['id'], 'status': status,
                   'message_id': message_id, 'user': 'UADMIN', 'created_at': expired}
            state.put('security_deliveries', job['id'], job)
            deliveries.append(job)
        message = {'id': message_id, 'incidents': {j['id']: r for j,r in zip(deliveries, rows)},
                   'text': 'Immutable private alert text', 'created_at': expired}
        state.put('security_messages', message_id, message)
        return deliveries, message

    def documents():
        return {ns: dict(state.rows(ns, prefix=prefix)) for ns in namespaces}

    try:
        removed, retained, pending_jobs = [], [], []
        for status in ('sent', 'summarized', 'cancelled', 'cooldown'):
            row = incident(status, status='retracted' if status == 'cancelled' else 'resolved')
            jobs, message = notification(status, [row], [status])
            removed.extend([('security_incidents', row['id']), ('security_deliveries', jobs[0]['id']),
                            ('security_messages', message['id'])])
        for name, status, at in [('recent', 'resolved', recent), ('open', 'open', expired), ('shadow', 'shadow', expired)]:
            row = incident(name, status, at)
            jobs, message = notification(name, [row], ['sent'])
            retained.extend([('security_incidents', row['id']), ('security_deliveries', jobs[0]['id']),
                             ('security_messages', message['id'])])
        for bound in (False, True):
            row = incident('pending'+str(bound))
            jobs, message = notification('pending'+str(bound), [row], ['pending'])
            if not bound:
                jobs[0].pop('message_id')
                state.put('security_deliveries', jobs[0]['id'], jobs[0])
                state.delete('security_messages', message['id'])
            else:
                retained.append(('security_messages', message['id']))
            retained.extend([('security_incidents', row['id']), ('security_deliveries', jobs[0]['id'])])
            pending_jobs.extend(jobs)

        # A pending summary still reads its completed sibling receipt and evidence.
        sibling, active = incident('pending-summary-closed'), incident('pending-summary-open', 'open')
        jobs, message = notification('pending-summary', [sibling, active], ['sent', 'pending'])
        retained.extend([('security_incidents', r['id']) for r in (sibling, active)])
        retained.extend([('security_deliveries', j['id']) for j in jobs])
        retained.append(('security_messages', message['id']))
        pending_jobs.append(jobs[1])
        pending_message = message

        # A completed mixed-age summary cannot retain the expired sibling's snapshot.
        old, new = incident('mixed-old'), incident('mixed-new', at=recent)
        jobs, message = notification('mixed', [old, new], ['summarized', 'summarized'])
        removed.extend([('security_incidents', old['id']), ('security_deliveries', jobs[0]['id']),
                        ('security_messages', message['id'])])
        retained.extend([('security_incidents', new['id']), ('security_deliveries', jobs[1]['id'])])

        for status in ('sent', 'pending'):
            row = incident('legacy-'+status)
            jobs, message = notification('legacy-'+status, [row], [status])
            state.delete('security_incidents', row['id'])  # Left behind by older retention code.
            target = retained if status == 'pending' else removed
            target.extend([('security_deliveries', jobs[0]['id']), ('security_messages', message['id'])])
            if status == 'pending':
                pending_jobs.extend(jobs)
                orphan_evidence = row['evidence'][0]

        before = documents()
        with pytest.raises(RuntimeError, match='rollback retention'):
            with state.atomic():
                state.prune(90, 60)
                assert state.get('security_incidents', old['id']) is None
                raise RuntimeError('rollback retention')
        assert documents() == before
        assert all(state.security_get_event(key) for key in evidence_ids)

        state.prune(90, 60)
        assert all(state.get(ns, key) is None for ns, key in removed)
        assert all(state.get(ns, key) == before[ns][key] for ns, key in retained)
        assert state.get('security_messages', pending_message['id']) == pending_message
        assert state.security_get_event(sibling['evidence'][0])
        assert state.security_get_event(orphan_evidence)
        assert state.security_get_event(old['evidence'][0]) is None

        for job in pending_jobs:
            state.put('security_deliveries', job['id'], {**job, 'status': 'sent'})
        state.put('security_incidents', active['id'], {**active, 'status': 'resolved'})
        state.prune(90, 60)
        assert all(state.get('security_deliveries', job['id']) is None for job in pending_jobs)
        assert state.get('security_messages', pending_message['id']) is None
        assert state.get('security_incidents', sibling['id']) is None
        assert state.security_get_event(sibling['evidence'][0]) is None
        assert state.security_get_event(orphan_evidence) is None
        after = documents()
        state.prune(90, 60)
        assert documents() == after
    finally:
        state.connection.execute('DELETE FROM anomaly_documents WHERE key LIKE %s', (prefix+'%',))
        state.connection.execute('DELETE FROM anomaly_security_events WHERE source_key LIKE %s', (prefix+'%',))
        state.close()
