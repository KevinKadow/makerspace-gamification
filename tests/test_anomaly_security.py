"""Security evidence, detection, privacy and staff delivery contracts."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ledger.anomaly.config import Config
from ledger.anomaly.ingest import Ingestor
from ledger.anomaly.privacy import timestamp
from ledger.anomaly.schema import fingerprint
from ledger.anomaly.state import MemoryState
from ledger.anomaly.security import SecurityEngine, hour
from ledger.anomaly.security_evidence import effective, covered, reset_outcomes
from ledger.anomaly.security_projection import email_features, similar, project
from ledger.anomaly.security_learning import load_rules, validate_spec, threshold
from ledger.anomaly.security_delivery import SecurityDelivery
from ledger.anomaly.reports import Reports, request_kind
from ledger.domain import Denied

NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)


def config(**values):
    return replace(Config('postgresql://test/test', 'mongodb://source/test', 'test', 'unused',
        security_enabled=True, security_dm_user_ids=('UADMIN',)), **values)


def setup(**values):
    state = MemoryState()
    state.put('security_control', 'monitor', {'enabled': True, 'enabled_at': (NOW-timedelta(days=1)).isoformat()})
    return state, SecurityEngine(state, config(**values))


def event(state, key, kind, member='member1', at=NOW, origin='stream', **extra):
    row = {'id': fingerprint(key), 'source_key': key, 'kind': kind, 'member_id': member,
           'actor_id': None, 'at': at.isoformat(), 'origin': origin, 'historical': False,
           'confidence': 'observed', **extra}
    state.security_replace(key, [row])
    return row


def coverage(state, collection='members', start=NOW-timedelta(days=1), end=NOW+timedelta(hours=25)):
    state.put('security_coverage', collection+':test', {'collection': collection, 'start': start.isoformat(), 'end': end.isoformat()})


def ingest(state=None):
    state = state or MemoryState()
    registry = SimpleNamespace(version='v1', contracts=lambda: {c: {'fields': {}, 'dynamic': True, 'indexes': [], 'references': []}
        for c in ['members', 'audit_logs', 'mailtrap', 'mailtrap_messages']}, required_since=lambda c: {}, provenance=lambda: {})
    return Ingestor(state, {}, registry, config())


def test_projection_privacy_multifield_reset_once_and_no_snapshot_changes():
    private = {'firstname': 'PROFILE-CANARY', 'lastname': 'PRIVATE-NAME', 'email': 'PRIVATE-EMAIL@example.org',
        'encrypted_password': 'PASSWORD-CANARY', 'reset_password_token': 'TOKEN-CANARY',
        'reset_password_sent_at': NOW, 'ip_address': 'IP-CANARY', 'notes': 'NOTE-CANARY'}
    projection = project('members', private, 'secret', operation='update', updates=private)
    assert [r['kind'] for r in projection['markers']] == ['profile_change', 'password_change', 'reset_request']
    assert projection['markers'][0]['fields'] == ['email', 'firstname', 'lastname']
    assert not project('members', private, 'secret', operation='snapshot').get('markers')
    assert not project('members', private, 'secret', operation='replace').get('markers')
    assert not project('members', private, 'secret', operation='update', updates={'reset_password_token': None, 'reset_password_sent_at': NOW})['markers']
    assert all(v not in json.dumps(projection) for v in private.values() if isinstance(v, str))


def test_stream_replay_scan_and_transaction_rollback_preserve_one_operation():
    worker = ingest()
    doc = {'_id': 'member1', 'firstname': 'CANARY', 'encrypted_password': 'HASH-CANARY'}
    worker.capture('members', doc, 'update', {'token': 1}, occurred_at=NOW.isoformat(), updated_fields=doc)
    pending = worker.state.pending()[0]
    original = worker.state.security_replace
    def crash(*args):
        original(*args)
        raise RuntimeError('crash')
    worker.state.security_replace = crash
    with pytest.raises(RuntimeError):
        worker.process_one(pending)
    assert not worker.state.security_events()
    assert worker.state.pending()
    worker.state.security_replace = original
    worker.process_one(pending)
    worker.capture('members', doc, 'update', {'token': 1}, occurred_at=NOW.isoformat(), updated_fields=doc)
    assert not worker.state.pending()
    for generation in range(5):
        worker.capture('members', doc, 'snapshot', generation=generation)
        worker.drain()
    assert len(worker.state.security_events(kind='profile_change')) == 1
    assert len(worker.state.security_events(kind='password_change')) == 1
    assert 'CANARY' not in json.dumps(worker.state.events)
    restarted = ingest(worker.state)
    restarted.capture('members', doc, 'snapshot')
    restarted.drain()
    assert len(worker.state.security_events()) == 2


def test_audit_overlap_enriches_without_double_counting_and_correction_retracts():
    state, engine = setup()
    coverage(state)
    for n in range(5):
        event(state, str(n), 'profile_change', at=NOW-timedelta(seconds=5-n), fields=['email'])
        event(state, 'audit'+str(n), 'profile_change', at=NOW-timedelta(seconds=5-n), origin='audit_logs', actor_id='admin')
    assert len(effective(state, NOW-timedelta(minutes=15), NOW)) == 5
    signals = engine.detect(NOW)
    assert len(signals) == 1
    engine.save(signals[0])
    state.security_replace('0', [])
    engine.retract()
    assert state.get('security_incidents', signals[0]['id'])['status'] == 'retracted'
    # Uncovered audit history is explicitly dated evidence, never a zero-coverage baseline.
    event(state, 'old', 'profile_change', at=NOW-timedelta(days=10), origin='audit_logs')
    assert len(effective(state, NOW-timedelta(days=11), NOW-timedelta(days=9))) == 1


def resolved_email_incidents(state, engine, members):
    for n in range(members):
        event(state, 'signup'+str(n), 'signup', member='m'+str(n), origin='audit_logs',
              email=email_features('subscriber+'+str(n)+'@example.org', 'key'))
    signals = engine.detect(NOW)
    assert {s['rule'] for s in signals} == {'signup_alias_family', 'signup_similar'}
    for signal in signals:
        engine.save(signal)
        engine.resolve(signal['id'], 'UADMIN')
    return {s['id']: state.get('security_incidents', s['id']) for s in signals}


@pytest.mark.parametrize('correction', ['delete', 'update'])
def test_resolved_email_incidents_retract_and_leave_reports_after_correction(correction):
    state, engine = setup()
    resolved = resolved_email_incidents(state, engine, 5)
    engine.publish(NOW+timedelta(seconds=1))
    assert any(r['report'] == 'email_similarity' for _,r in state.rows('security_report_metrics'))
    names = ['orchardcopper', 'violinmeadow', 'penguincloud', 'tulipmariner', 'granitesparrow']
    for n, name in enumerate(names):
        source = 'signup'+str(n)
        if correction == 'delete':
            state.security_replace(source, [])
        else:
            row = state.security_events(source=source)[0]
            state.security_replace(source, [{**row, 'email': email_features(name+'@example.org', 'key')}])
    engine.retract()
    for key, previous in resolved.items():
        current = state.get('security_incidents', key)
        assert current['status'] == 'retracted' and current['alertable'] is False
        assert current['resolved_by'] == previous['resolved_by']
        assert current['resolved_at'] == previous['resolved_at']
        assert current['evidence_snapshot'] == previous['evidence_snapshot']
        assert current['reason'] == 'Source evidence was corrected or deleted'
    assert not any(r['report'] == 'email_similarity' for r in engine.aggregate(NOW+timedelta(seconds=1)))
    engine.publish(NOW+timedelta(seconds=1))
    assert not any(r['report'] == 'email_similarity' for _,r in state.rows('security_report_metrics'))
    SecurityDelivery(state, engine.config).schedule(NOW)
    assert not list(state.rows('security_deliveries'))


def test_supported_resolved_incidents_refresh_evidence_without_reopening(monkeypatch):
    state, engine = setup()
    resolved = resolved_email_incidents(state, engine, 6)
    removed = state.security_events(source='signup5')[0]
    state.security_replace('signup5', [])
    engine.retract()
    for key, previous in resolved.items():
        current = state.get('security_incidents', key)
        assert current['status'] == 'resolved' and current['alertable'] is False
        assert current['observed'] == 5 and current['members'] == ['m0', 'm1', 'm2', 'm3', 'm4']
        assert removed['id'] not in current['evidence_hashes']
        assert all(fingerprint(state.security_get_event(e)) == value for e,value in current['evidence_hashes'].items())
        for field in ('created_at', 'resolved_at', 'resolved_by', 'historical', 'mode_shadow'):
            assert current[field] == previous[field]
    metric = next(r for r in engine.aggregate(NOW+timedelta(seconds=1)) if r['report'] == 'email_similarity')
    assert metric['sample_size'] == 5
    detect = Mock(wraps=engine.detect)
    monkeypatch.setattr(engine, 'detect', detect)
    engine.retract()
    detect.assert_not_called()  # Updated hashes stop repeat work for unchanged evidence.
    SecurityDelivery(state, engine.config).schedule(NOW)
    assert not list(state.rows('security_deliveries'))


def test_previous_hour_late_burst_is_replayed():
    state, engine = setup()
    start = NOW-timedelta(hours=1)
    for n in range(5):
        event(state, str(n), 'profile_change', at=start+timedelta(minutes=n), origin='audit_logs')
    assert not engine.detect(NOW+timedelta(minutes=30))
    engine.evaluate(NOW+timedelta(minutes=30))
    incidents = [r for _, r in state.rows('security_incidents')]
    assert any(r['rule'] == 'profile_member' for r in incidents)
    assert all(r['historical'] and not r['alertable'] for r in incidents)
    assert not list(state.rows('security_dirty'))


@pytest.mark.parametrize('delay,expected', [(timedelta(hours=48), True),
                                         (timedelta(hours=48, microseconds=1), False)])
def test_corrected_promotion_replays_unchanged_later_grant(delay, expected):
    state, engine = setup()
    first_at = NOW-timedelta(days=4)
    first = event(state, 'first', 'role_change', member='promoted', at=first_at, origin='audit_logs',
                  actor_id='admin', grant=False, before_role='member', after_role='member')
    event(state, 'second', 'role_change', member='target', at=first_at+delay, origin='audit_logs',
          actor_id='promoted', grant=True, before_role='member', after_role='admin')
    engine.evaluate(NOW)
    assert not list(state.rows('security_incidents'))
    state.security_replace('first', [{**first, 'grant': True, 'after_role': 'admin'}])
    engine.evaluate(NOW)
    chains = [r for _, r in state.rows('security_incidents') if r['rule'] == 'privilege_propagation']
    assert bool(chains) is expected
    assert not list(state.rows('security_dirty'))


def test_correction_replay_resumes_and_restarts_on_new_revision():
    state, engine = setup()
    start = NOW-timedelta(days=4)
    for n in range(55):
        event(state, str(n), 'profile_change', at=start+timedelta(seconds=n), origin='audit_logs')
    bucket = start.isoformat()
    engine.evaluate(NOW)
    cursor = state.get('security_backfill_cursor', bucket)
    assert cursor['at'] == (start+timedelta(seconds=49)).isoformat()
    assert state.get('security_dirty', bucket)
    restarted = SecurityEngine(state, engine.config)
    restarted.evaluate(NOW)
    assert not state.get('security_dirty', bucket)
    # A saved cursor from an older evidence revision must not skip early windows.
    state.put('security_backfill_cursor', bucket, cursor)
    changed = state.security_events(source='0')[0]
    state.security_replace('0', [{**changed, 'fields': ['email']}])
    restarted.evaluate(NOW)
    updated = state.get('security_backfill_cursor', bucket)
    assert updated['revision'] != cursor['revision']
    assert updated['at'] == cursor['at']


@pytest.mark.parametrize('status', ['open', 'shadow', 'resolved', 'retracted'])
def test_resolution_only_accepts_reviewable_incidents(status):
    state, engine = setup()
    row = {'id': 'incident', 'status': status, 'alertable': False}
    state.put('security_incidents', 'incident', row)
    if status in {'open', 'shadow'}:
        engine.resolve('incident', 'UADMIN')
        assert state.get('security_incidents', 'incident')['status'] == 'resolved'
    else:
        with pytest.raises(ValueError, match='Only open or shadow'):
            engine.resolve('incident', 'UADMIN')
        assert state.get('security_incidents', 'incident') == row


@pytest.mark.parametrize('kind,count,global_count', [('profile_change',5,20), ('password_change',3,10), ('reset_request',5,20)])
def test_individual_and_distributed_bursts(kind, count, global_count):
    state, engine = setup()
    for n in range(count-1):
        event(state, str(n), kind)
    assert not engine.detect(NOW)
    event(state, 'threshold', kind)
    assert any(s['entity'] == 'member1' for s in engine.detect(NOW))
    state, engine = setup()
    for n in range(global_count):
        event(state, str(n), kind, member='m'+str(n%5))
    assert any(s['entity'] == 'global' for s in engine.detect(NOW))


def test_reset_outcomes_supersession_completion_pending_and_gaps():
    state, _ = setup()
    coverage(state)
    first = event(state, 'r1', 'reset_request', at=NOW-timedelta(hours=2))
    second = event(state, 'r2', 'reset_request', at=NOW-timedelta(hours=1))
    event(state, 'complete', 'password_change', at=NOW, token_cleared=True)
    output = reset_outcomes(state, [first, second], NOW)
    assert [r['outcome'] for r in output] == ['no_observed_completion', 'observed_completion']
    third = event(state, 'r3', 'reset_request', member='m2')
    assert reset_outcomes(state, [third], NOW+timedelta(minutes=1))[0]['outcome'] == 'pending'
    assert reset_outcomes(state, [third], NOW+timedelta(hours=24))[0]['outcome'] == 'no_observed_completion'
    state.delete('security_coverage', 'members:test')
    assert reset_outcomes(state, [third], NOW+timedelta(hours=24))[0]['outcome'] == 'incomplete_coverage'
    event(state, 'possible', 'password_change', member='m2', at=NOW+timedelta(minutes=2), origin='audit_logs')
    assert reset_outcomes(state, [third], NOW+timedelta(minutes=3))[0]['outcome'] == 'possible_completion'


@pytest.mark.parametrize('left,right,match', [('longusername+one@a.org','longusername+two@a.org',True),
    ('longusername@a.org','longusername@b.org',True), ('admin@a.org','admin@b.org',False),
    ('shortone@a.org','shorttwo@a.org',False), ('short@a.org','shorl@a.org',False),
    ('abcdefghijklmnop1@a.org','abcdefghijklmnop2@b.org',True)])
def test_keyed_email_similarity(left, right, match):
    a,b = email_features(left,'secret'),email_features(right,'secret')
    assert similar(a,b) is match
    assert left not in json.dumps(a) and right not in json.dumps(b)
    assert a != email_features(left,'other-secret')


def test_signup_alias_clusters_and_verified_rate_separate_creations():
    state, engine = setup()
    for n in range(10):
        event(state, str(n), 'account_created', member='m'+str(n), email=email_features(f'longusername+{n}@a.org','key'))
    rules = {s['rule'] for s in engine.detect(NOW)}
    assert 'signup_alias_family' in rules and not any('signup_burst' == rule for rule in rules)
    for n in range(10):
        event(state, 'audit'+str(n), 'signup', member='m'+str(n), origin='audit_logs', email=email_features(f'longusername+{n}@a.org','key'))
    rows = effective(state, NOW-timedelta(hours=1), NOW+timedelta(seconds=1))
    assert len(rows) == 10 and all(r['kind']=='signup' for r in rows)
    assert any(s['rule'] == 'signup_burst' for s in engine.detect(NOW))


def test_distinct_bounce_messages_campaign_denominator_and_duplicate_webhooks():
    state, engine = setup()
    for n in range(20):
        event(state, str(n), 'mail_outcome', member='m'+str(n%5), origin='mailtrap',
              outcome='bounce' if n<5 else 'delivery', provider_message='message'+str(n), provider_event='event'+str(n))
    for n in range(5):
        event(state, 'duplicate'+str(n), 'mail_outcome', origin='mailtrap', outcome='bounce', provider_message='message0', provider_event='event0')
    signal = next(s for s in engine.detect(NOW) if s['rule']=='bounce_burst')
    assert signal['observed']==5 and signal['denominator']==20
    for n in range(20, 100):
        event(state, str(n), 'mail_outcome', origin='mailtrap', outcome='delivery', provider_message='message'+str(n))
    assert not any(s['rule']=='bounce_burst' for s in engine.detect(NOW))


@pytest.mark.parametrize('seconds,expected', [(0,True),(48*3600,True),(48*3600+1,False)])
def test_promotion_chain_inclusive_boundary(seconds, expected):
    state, engine = setup()
    event(state, 'first', 'role_change', member='promoted', at=NOW-timedelta(seconds=seconds),
          origin='audit_logs', actor_id='admin', before_role='board_member', after_role='admin', grant=True)
    event(state, 'second', 'scope_grant', member='target', origin='audit_logs', actor_id='promoted', scope_count=1, grant=True)
    assert any(s['rule']=='privilege_propagation' for s in engine.detect(NOW)) is expected


def test_mfa_bulk_grants_noop_demotion_and_missing_actor():
    state, engine = setup()
    event(state, 'mfa', 'mfa_change', origin='audit_logs', actor_id='admin', role='board_member', action='totp_reset')
    for n in range(3):
        event(state, str(n), 'scope_grant', member='m'+str(n), origin='audit_logs', actor_id='admin', scope_count=1, grant=True)
    assert {s['rule'] for s in engine.detect(NOW)} == {'privileged_mfa_change','bulk_privilege_grants'}
    state.security_replace('mfa', [])
    assert not any(s['rule']=='privileged_mfa_change' for s in engine.detect(NOW))
    for pair in [['admin','board_member'],['admin','admin']]:
        projection = project('audit_logs', {'resource_type':'Member','subject_id':'m1','created_at':NOW,
            'event_type':'member_updated','field_changes':{'role':pair}}, 'key', operation='snapshot')
        assert not any(r.get('grant') for r in projection['markers'])


def test_rollups_zero_coverage_dirty_correction_and_shadow_gates():
    state, engine = setup()
    engine.rollup(NOW-timedelta(hours=1))
    assert not any(r['complete'] for r in state.security_rollups())
    coverage(state)
    engine.rollup(NOW-timedelta(hours=1))
    profile = next(r for r in state.security_rollups() if r['kind']=='profile_change')
    assert profile['complete'] and profile['value']==0
    for revision, detector in state.rows('security_detectors'):
        with pytest.raises(ValueError, match='seven days'):
            engine.detectors.approve(revision,'UADMIN',NOW)
    for n in range(5):
        event(state, str(n), 'profile_change', at=NOW-timedelta(minutes=1))
    engine.evaluate(NOW)
    assert list(state.rows('security_incidents'))
    for n in range(5): state.security_replace(str(n), [])
    engine.evaluate(NOW+timedelta(seconds=15))
    assert all(r['status']=='retracted' for _,r in state.rows('security_incidents'))


def test_small_and_complementary_cohorts_suppressed_everywhere():
    state, engine = setup()
    for n in range(5):
        event(state, str(n), 'profile_change', member='m'+str(n))
    assert not engine.aggregate(NOW)  # end is exclusive
    assert not any(r['report']=='account_changes' for r in engine.aggregate(NOW+timedelta(seconds=1)))
    for n in range(4):
        event(state, 'password'+str(n), 'password_change', member='m'+str(n))
    assert not any(r['report']=='account_changes' for r in engine.aggregate(NOW+timedelta(seconds=1)))
    event(state, 'password4', 'password_change', member='m4')
    engine.publish(NOW+timedelta(seconds=1))
    assert {r['cohort'] for _,r in state.rows('security_report_metrics') if r['report']=='account_changes'} == {'profile_change', 'password_change'}
    state.security_replace('password4', [])
    engine.publish(NOW+timedelta(seconds=1))
    assert not any(r['report']=='account_changes' for _,r in state.rows('security_report_metrics'))
    assert all(r['sample_size']>=5 for _,r in state.rows('security_report_metrics'))


@pytest.mark.parametrize('minimum', [5, 7])
@pytest.mark.parametrize('section,categories', [
    ('account_changes', ('profile_change', 'password_change')),
    ('registrations', ('signup', 'account_created')),
    ('privilege_changes', ('role_change', 'scope_grant', 'mfa_change')),
    ('bounce_outcomes', ('bounced_messages', 'delivered_messages')),
])
def test_security_partitions_require_all_expected_categories(section, categories, minimum):
    for absent in categories:
        state, engine = setup(minimum_cohort=minimum)

        def add_category(category):
            for n in range(minimum):
                key = category + ':' + str(n)
                if section == 'bounce_outcomes':
                    event(state, key, 'mail_outcome', member=key, origin='mailtrap',
                        outcome='bounce' if category == 'bounced_messages' else 'delivery', provider_message=key)
                else:
                    event(state, key, category, member=key)

        for category in categories:
            if category != absent:
                add_category(category)
        engine.publish(NOW+timedelta(seconds=1))
        assert not any(r['report'] == section for _,r in state.rows('security_report_metrics'))
        add_category(absent)
        engine.publish(NOW+timedelta(seconds=1))
        published = [r for _,r in state.rows('security_report_metrics') if r['report'] == section]
        assert {r['cohort'] for r in published} == set(categories)
        assert all(r['value'] == minimum and r['sample_size'] == minimum for r in published)


@pytest.mark.parametrize('absent', ['observed_completion', 'possible_completion', 'pending',
                                  'no_observed_completion', 'incomplete_coverage'])
def test_reset_completion_partitions_suppress_absent_outcomes(absent):
    state, engine = setup()
    coverage(state, start=NOW-timedelta(days=3))
    ages = {'observed_completion': timedelta(hours=1), 'possible_completion': timedelta(hours=2),
            'pending': timedelta(hours=3), 'no_observed_completion': timedelta(days=2),
            'incomplete_coverage': timedelta(days=4)}

    def add_outcome(outcome):
        for n in range(5):
            key = outcome + ':' + str(n)
            at = NOW-ages[outcome]
            event(state, key, 'reset_request', member=key, at=at)
            if outcome in {'observed_completion', 'possible_completion'}:
                event(state, key+':password', 'password_change', member=key, at=at+timedelta(minutes=1),
                      token_cleared=outcome == 'observed_completion')

    for outcome in ages:
        if outcome != absent:
            add_outcome(outcome)
    assert not any(r['report'] == 'reset_completions' for r in engine.aggregate(NOW))
    add_outcome(absent)
    published = [r for r in engine.aggregate(NOW) if r['report'] == 'reset_completions']
    assert {r['cohort'] for r in published} == set(ages)
    assert all(r['sample_size'] == r['value'] == 5 for r in published)


def test_security_questions_route_before_membership_and_privacy_administration():
    assert request_kind('What signup patterns changed?') == ('security', [])
    assert request_kind('security detectors list') == ('security',['detectors','list'])
    state, engine = setup()
    access=Mock();access.authorize.return_value='aggregate';access.staff.return_value=False
    reports=Reports(state,None,None,access,None,config())
    job={'_id':'job','payload':{'text':'security incidents','user_id':'UREADER','channel':'CCHANNEL'}}
    with pytest.raises(Denied): reports.build(job)
    job['payload']['text']='security summary'
    assert 'suppressed' in reports.build(job)['text']


def alert(state, engine, key='first', member='member1', severity='critical'):
    row=event(state,key,'mfa_change',member=member,origin='audit_logs',actor_id='admin',role='admin',action='totp_reset')
    signal=engine._signal('privileged_mfa_change',[row],NOW,'review',entity=member,severity=severity,unique=severity=='critical')
    engine.save(signal)
    return signal


def test_security_delivery_independent_retries_deterministic_ids_and_revocation():
    state,engine=setup(security_dm_user_ids=('UADMIN','UBOARD'))
    alert(state,engine)
    slack=Mock();slack.conversations_open.side_effect=lambda users:{'channel':{'id':'D'+users}};slack.chat_postMessage.return_value={'ok':True}
    access=Mock();access.staff.return_value=True;access.authorize.return_value='staff_detail'
    delivery=SecurityDelivery(state,engine.config,slack,access)
    def post(**kwargs):
        if kwargs['channel']=='DUADMIN': raise TimeoutError()
        return {'ok':True}
    slack.chat_postMessage.side_effect=post
    delivery.step(NOW)
    assert sorted(r['status'] for _,r in state.rows('security_deliveries'))==['pending','sent']
    first=slack.chat_postMessage.call_args_list[0].kwargs['client_msg_id']
    slack.chat_postMessage.side_effect=None
    delivery.step(NOW+timedelta(seconds=10))
    assert slack.chat_postMessage.call_args.kwargs['client_msg_id']==first
    alert(state,engine,'new','new-member')
    access.staff.return_value=False
    delivery.step(NOW+timedelta(seconds=20))
    assert state.get('health','security_delivery')['status']=='configuration_problem'
    assert all(r['status']!='pending' for _,r in state.rows('security_deliveries'))


@pytest.mark.parametrize('missing', ['both', 'slack', 'access'])
def test_unconfigured_security_delivery_retries_after_restart(missing):
    state, engine = setup()
    incident = alert(state, engine)
    slack = Mock()
    slack.conversations_open.return_value = {'channel': {'id': 'DADMIN'}}
    slack.chat_postMessage.return_value = {'ok': True}
    access = Mock()
    access.staff.return_value = True
    access.authorize.return_value = 'staff_detail'
    delivery = SecurityDelivery(state, engine.config,
        None if missing in {'both', 'slack'} else slack,
        None if missing in {'both', 'access'} else access)
    delivery.step(NOW)
    key = fingerprint([incident['id'], 'UADMIN'])
    pending = state.get('security_deliveries', key)
    assert pending['status'] == 'pending' and pending['attempts'] == 1
    assert timestamp(pending['available_at']) > NOW
    assert state.get('health', 'security_delivery')['status'] == 'configuration_problem'
    assert not list(state.rows('security_budgets'))
    slack.chat_postMessage.assert_not_called()
    delivery.step(NOW+timedelta(seconds=1))
    assert state.get('security_deliveries', key) == pending

    # A fresh worker uses the same persisted receipt and rechecks current staff access.
    restarted = SecurityDelivery(state, engine.config, slack, access)
    restarted.step(NOW+timedelta(seconds=10))
    assert len(list(state.rows('security_deliveries'))) == 1
    assert state.get('security_deliveries', key)['status'] == 'sent'
    assert state.get('health', 'security_delivery')['status'] == 'ready'
    access.staff.assert_called_with('UADMIN')
    access.authorize.assert_called_with('UADMIN', 'DADMIN')
    assert slack.chat_postMessage.call_args.kwargs['channel'] == 'DADMIN'
    restarted.step(NOW+timedelta(seconds=20))
    slack.chat_postMessage.assert_called_once()


def test_delivery_disabled_empty_recipients_budget_priority_and_no_channel_fallback():
    state,engine=setup(security_dm_user_ids=())
    alert(state,engine)
    delivery=SecurityDelivery(state,engine.config)
    delivery.step(NOW)
    assert not list(state.rows('security_deliveries'))
    assert state.get('health','security_delivery')['status']=='configuration_problem'
    state,engine=setup()
    for n in range(15): alert(state,engine,str(n),'m'+str(n),severity='warning')
    alert(state,engine,'critical','critical')
    slack=Mock();slack.conversations_open.return_value={'channel':{'id':'DADMIN'}};slack.chat_postMessage.return_value={'ok':True}
    access=Mock();access.staff.return_value=True;access.authorize.return_value='staff_detail'
    delivery=SecurityDelivery(state,engine.config,slack,access)
    delivery.step(NOW)
    assert slack.chat_postMessage.call_count==10
    assert 'critical' in slack.chat_postMessage.call_args_list[0].kwargs['text']
    assert 'Additional security rate incidents' in slack.chat_postMessage.call_args.kwargs['text']
    delivery.config=replace(engine.config,security_enabled=False)
    delivery.step(NOW+timedelta(hours=1))
    assert slack.chat_postMessage.call_count==10


def test_rule_configuration_and_untrusted_proposal_validation():
    assert load_rules()['privileges']['chain_hours']==48
    for raw in ['{}','["UADMIN", "UADMIN"]','["CCHANNEL"]','[1]']:
        if raw=='["UADMIN", "UADMIN"]': assert Config.security_recipients(raw)==('UADMIN',)
        else:
            with pytest.raises(ValueError): Config.security_recipients(raw)
    for value in [{}, {'feature':[]}, {'feature':'email'}]:
        with pytest.raises(ValueError): validate_spec(value)
    assert threshold({'floor':5,'mad_threshold':4,'quantile':.99},[0]*8)==5


def test_audit_scan_is_stable_and_deletion_removes_derived_security_evidence():
    worker = ingest()
    doc = {'_id':'audit1', 'resource_type':'Member','subject_id':'m1','actor_id':'admin','created_at':NOW,
           'event_type':'member_updated','field_changes':{'role':['member','admin']}}
    worker.capture('audit_logs',doc,'insert',{'token':1},occurred_at=NOW.isoformat())
    worker.drain()
    original=worker.state.security_events()
    worker.capture('audit_logs',doc,'snapshot');worker.drain()
    assert worker.state.security_events()==original
    assert not worker.state.get('security_control','learning_correction')
    worker.capture('audit_logs',None,'delete',{'token':2},document_id='audit1',occurred_at=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat())
    worker.drain()
    assert not worker.state.security_events()


def test_unknown_and_descriptive_admin_rm_scopes_are_not_privilege_grants():
    base={'resource_type':'Member','subject_id':'m1','created_at':NOW,'event_type':'member_updated',
          'field_changes':{'resource_manager_shop_ids':[[],['shop1']]}}
    for role, expected in [(None,False),('admin',False),('board_member',False),('resource_manager',True)]:
        projection=project('audit_logs',{**base,'after_snapshot':{'role':role}},'key',operation='snapshot')
        assert projection['markers'][0]['grant'] is expected


def test_mfa_role_correction_retracts_and_queued_alert_waits_for_evaluation():
    state,engine=setup()
    role=event(state,'role','role_change',member='m1',at=NOW-timedelta(hours=1),origin='audit_logs',
        actor_id='admin',before_role='member',after_role='admin',grant=True)
    event(state,'mfa','mfa_change',member='m1',origin='audit_logs',actor_id='admin',action='totp_reset')
    signal=next(s for s in engine.detect(NOW) if s['rule']=='privileged_mfa_change')
    engine.save(signal)
    slack=Mock();access=Mock();access.staff.return_value=True;access.authorize.return_value='staff_detail'
    delivery=SecurityDelivery(state,engine.config,slack,access);delivery.schedule(NOW)
    state.security_replace('role',[{**role,'after_role':'member','grant':False}])
    delivery.step(NOW)
    slack.chat_postMessage.assert_not_called()
    engine.retract()
    assert state.get('security_incidents',signal['id'])['status']=='retracted'
    assert state.get('security_incidents',signal['id'])['evidence_snapshot']


def test_learned_detector_frozen_holdout_shadow_approval_and_correction_generation():
    state,engine=setup()
    revision,detector=next((k,r) for k,r in state.rows('security_detectors') if r['spec']['feature']=='profile_change' and r['spec']['scope']=='global')
    for n in range(12*7*24):
        at=NOW-timedelta(hours=12*7*24-n)
        state.security_put_rollup(at.isoformat(),'profile_change',{'at':at.isoformat(),'kind':'profile_change',
            'value':0,'complete':True,'members':[],'member_counts':{},'evidence':[]})
    result=engine.detectors.backtest(detector['spec'],NOW)
    assert result['passed'] and result['holdout_windows']>=20
    state.put('security_detectors',revision,{**detector,'created_at':(NOW-timedelta(days=9)).isoformat()})
    state.put('security_shadow',revision,{'windows':50,'hits':0,'first_at':(NOW-timedelta(days=6)).isoformat()})
    with pytest.raises(ValueError): engine.detectors.approve(revision,'UADMIN',NOW)
    state.put('security_shadow',revision,{'windows':50,'hits':0,'first_at':(NOW-timedelta(days=8)).isoformat()})
    engine.detectors.approve(revision,'UADMIN',NOW)
    assert state.get('security_detectors',revision)['status']=='active'
    event(state,'old-audit','profile_change',at=NOW-timedelta(days=5),origin='audit_logs')
    state.security_replace('old-audit',[])
    engine.detectors.corrections()
    assert state.get('security_detectors',revision)['status']=='superseded'
    assert engine.detectors.revision(detector['spec']) != revision
    assert all(r['status']=='shadow' for _,r in state.rows('security_detectors') if r['status']!='superseded')


def test_dst_repeated_hour_is_not_two_comparable_observations():
    state,engine=setup()
    spec=next(r['spec'] for _,r in state.rows('security_detectors') if r['spec']['feature']=='signup')
    fall=datetime(2026,11,1,5,tzinfo=timezone.utc)
    dates=[fall-timedelta(weeks=i) for i in range(7,0,-1)]+[fall,fall+timedelta(hours=1),fall+timedelta(weeks=1,hours=1)]
    for at in dates:
        state.security_put_rollup(at.isoformat(),'signup',{'at':at.isoformat(),'kind':'signup','value':0,'complete':True})
    samples=list(engine.detectors._samples(spec,dates[-1]+timedelta(hours=1)))
    assert len(samples)==1 and len(samples[0][3])==8


def test_generic_cross_domain_emails_and_nontransitive_clusters_do_not_chain():
    state,engine=setup()
    for n in range(4):
        event(state,str(n),'signup',member='m'+str(n),origin='audit_logs',email=email_features('admin@d'+str(n)+'.org','key'))
    assert not any(s['rule']=='signup_similar' for s in engine.detect(NOW))
    assert not similar(email_features('a'*100+'@a.org','key'),email_features('validname@a.org','key'))
    state,engine=setup()
    for n,trigrams in enumerate([list(range(10)),list(range(12)),list(range(2,12)),list(range(2,12))]):
        features={'domain':'same','family':str(n),'local':str(n),'plus':False,'length':12,'generic':False,'trigrams':list(map(str,trigrams))}
        event(state,str(n),'signup',member='m'+str(n),at=NOW-timedelta(seconds=4-n),origin='audit_logs',email=features)
    assert not any(s['rule']=='signup_similar' for s in engine.detect(NOW))


def test_security_config_composition_and_required_source_collections():
    import yaml
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    compose=yaml.safe_load((root/'compose.anomaly.yaml').read_text())
    env=compose['services']['ledger-anomaly']['environment']
    assert env['ANOMALY_SECURITY_ENABLED']=='${ANOMALY_SECURITY_ENABLED:-false}'
    assert env['ANOMALY_SECURITY_DM_USER_IDS']=='${ANOMALY_SECURITY_DM_USER_IDS:-[]}'
    assert env['ANOMALY_SECURITY_EVALUATION_SECONDS']=='${ANOMALY_SECURITY_EVALUATION_SECONDS:-15}'
    assert set(compose['services'])=={'ledger-anomaly','anomaly-postgres','metabase-anomaly','metabase-raw'}
    from ledger.anomaly.security_projection import COLLECTIONS
    assert COLLECTIONS=={'members','audit_logs','mailtrap','mailtrap_messages'}


@pytest.mark.parametrize('question',['Show profile changes','Who granted admin privileges?','Are there similar emails?',
    'Show password reset attempts','What registration trends changed?'])
def test_security_intents_precede_general_handlers(question):
    from ledger.anomaly.slack import _ANALYTICS
    assert _ANALYTICS.search(question)
    assert request_kind(question)==('security',[])


def test_uncertain_sends_reserve_budget_and_overflow_retry_keeps_text():
    state,engine=setup()
    for n in range(14): alert(state,engine,str(n),'m'+str(n),severity='warning')
    slack=Mock();slack.conversations_open.return_value={'channel':{'id':'DADMIN'}}
    access=Mock();access.staff.return_value=True;access.authorize.return_value='staff_detail'
    def post(**kwargs):
        if kwargs['text'].startswith('Additional'): raise TimeoutError()
        return {'ok':True}
    slack.chat_postMessage.side_effect=post
    delivery=SecurityDelivery(state,engine.config,slack,access)
    delivery.step(NOW)
    assert slack.chat_postMessage.call_count==10
    pending=slack.chat_postMessage.call_args.kwargs
    alert(state,engine,'new','new-member',severity='warning')
    slack.chat_postMessage.side_effect=None;slack.chat_postMessage.return_value={'ok':True}
    delivery.step(NOW+timedelta(seconds=10))
    assert slack.chat_postMessage.call_count==11
    assert slack.chat_postMessage.call_args.kwargs==pending
    assert state.get('security_budgets','UADMIN:'+NOW.strftime('%Y-%m-%dT%H'))['count']==10
    assert any(j['status']=='pending' for _,j in state.rows('security_deliveries'))


def test_shadow_reports_recipient_configuration_and_sink_outages_are_independent():
    state,engine=setup(security_dm_user_ids=(),security_alert_mode='shadow')
    SecurityDelivery(state,engine.config).step(NOW)
    assert state.get('health','security_delivery')['status']=='configuration_problem'
    from ledger.anomaly.service import Service
    service=Service.__new__(Service)
    service.state=state
    service.job=Mock(side_effect=TimeoutError())
    service.delivery=Mock();service.delivery.step.side_effect=TimeoutError()
    service.security_delivery=Mock()
    service.external_io()
    service.security_delivery.step.assert_called_once()


def test_role_and_scope_markers_from_one_audit_are_one_stable_chain():
    state,engine=setup()
    event(state,'first','role_change',member='promoted',at=NOW-timedelta(hours=1),origin='audit_logs',
          actor_id='admin',before_role='member',after_role='admin',grant=True)
    role=event(state,'grant','role_change',member='target',origin='audit_logs',actor_id='promoted',
               before_role='member',after_role='resource_manager',grant=True)
    scope={**role,'id':'scope-marker','kind':'scope_grant','scope_count':1}
    state.security_replace('grant',[role,scope])
    first=[s for s in engine.detect(NOW) if s['rule']=='privilege_propagation']
    assert len(first)==1
    state.security_replace('grant',[scope])
    second=[s for s in engine.detect(NOW) if s['rule']=='privilege_propagation']
    assert len(second)==1 and first[0]['id']==second[0]['id']
