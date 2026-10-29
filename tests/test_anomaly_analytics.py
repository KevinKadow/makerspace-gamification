from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ledger.anomaly.analytics import (Analytics, FEATURES, FEATURE_VERSION, label_at, paid_fraction,
    sustained_membership, temporal_partition, train_predictor, visit_features, robust_score, isolation_scores, predict)
from ledger.anomaly.evidence import reconstruct_coverage, state_at, source_observations
from ledger.anomaly.state import MemoryState


def instant(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def observation(state, collection, key, values, first='2025-01-01'):
    state.put('observations', collection + ':' + key,
              {'values': values, 'first_seen': instant(first).isoformat()})


def config():
    return SimpleNamespace(timezone='America/New_York', minimum_cohort=5, grace_days=30, history_months=60)


def test_retired_sources_remain_archived_but_cannot_feed_learning():
    state = MemoryState()
    observation(state, 'members', 'm', {'status': 'activeMember'})
    observation(state, 'cards', 'c', {'member_id': 'm', 'uid_hash': 'h'})
    state.put('schemas', 'active', {'collections': {'members': {}}, 'pending': {}})
    accepted, excluded = source_observations(state)
    assert 'cards:c' in excluded and 'cards:c' not in accepted
    assert state.get('observations', 'cards:c')  # Kept for audit, not live learning.


def test_calendar_fraction_and_positive_proof_without_complete_negative_history():
    intervals = [('2024-01-01', '2025-08-01')]
    result = sustained_membership(intervals, '2025-08-02')
    assert result['qualifies'] and result['fraction'] > .75
    assert paid_fraction([('2024-01-01', '2026-01-01')], '2026-01-01') == 1
    # Overlaps never double-count payment coverage.
    assert paid_fraction([('2024-01-01', '2026-01-01')] * 2, '2026-01-01') == 1
    assert sustained_membership([('2024-01-01', '2025-01-01')], '2026-01-01')['coverage'] == 'unknown'


def test_visit_features_are_local_deduplicated_temporal_and_seasonal():
    visits = [instant('2026-03-08T06:55'), instant('2026-03-08T06:55'),
              instant('2026-03-08T07:05'), instant('2026-03-09T22:30')]
    features = visit_features(visits + [instant('2027-01-01')], instant('2026-03-10'))
    assert features['visit_days_7'] == 2
    assert features['evening_fraction'] == pytest.approx(1/3)
    assert all(name in features for name in FEATURES)
    assert features['season_sin'] != visit_features([], instant('2026-09-10'))['season_sin']


def test_labels_require_followup_and_gaps_but_not_future_events():
    assert label_at([], '2026-01-01', 30, '2026-02-01', '2025-01-01') == 0
    assert label_at([], '2026-01-01', 30, '2026-01-20', '2025-01-01') is None
    assert label_at([], '2026-01-01', 30, '2026-02-01', '2025-01-01', [('2026-01-10','2026-01-11')]) is None
    assert label_at(['2026-01-15'], '2026-01-01', 30, '2026-01-20') == 1
    assert label_at(['2026-01-25'], '2026-01-01', 30, '2026-01-20') is None


def test_temporal_partition_retains_training_when_all_groups_persist():
    start = instant('2024-01-01')
    rows = [{'group':str(group), 'as_of':(start+timedelta(days=week*21)).isoformat()}
            for group in range(150) for week in range(20)]
    training, testing, split = temporal_partition(rows, 30)
    assert training and testing
    assert not {r['group'] for r in training} & {r['group'] for r in testing}
    assert all(datetime.fromisoformat(r['as_of'])+timedelta(days=30) < datetime.fromisoformat(split) for r in training)
    assert all(r['as_of'] >= split for r in testing)


def test_predictor_requires_distinct_positive_groups_and_beats_a_recency_baseline():
    start = instant('2024-01-01')
    rows = []
    for member in range(300):
        label = member % 2
        for week in range(20):
            at = start+timedelta(days=week*21)
            features = {name:0.0 for name in FEATURES}
            features['payment_failures_28'] = 10.0 * label
            rows.append({'member_id':str(member), 'group':str(member), 'as_of':at.isoformat(),
                         'features':features, 'confidence':'verified', 'label':label})
    model = train_predictor(rows, 'lapse', 30)
    assert model['status'] == 'validated'
    assert model['metrics']['pr_auc'] > model['metrics']['recency_pr_auc']
    assert model['metrics']['brier'] <= model['metrics']['prior_brier'] and model['metrics']['calibration_error'] <= .1
    assert model['training_groups'] and model['sample_size'] >= 20
    assert model['associations'][0]['feature'] == 'payment_failures_28'
    for row in rows:
        row['group'] = 'one-household'
    assert train_predictor(rows, 'lapse', 30)['status'] == 'insufficient_evidence'


def test_zero_visits_can_be_verified_after_complete_prospective_window():
    state = MemoryState()
    observation(state,'members','m',{'status':'activeMember'})
    state.put('health','bootstrap',{'completed_at':'2025-01-01'})
    rows = Analytics(state,config()).refresh('2026-01-01')
    assert rows[0]['confidence'] == 'verified' and rows[0]['features']['visit_days_90'] == 0
    state.put('gaps','gap',{'start':'2025-12-15','end':'2025-12-20'})
    assert Analytics(state,config()).refresh('2026-01-02')[0]['confidence'] != 'verified'


def test_old_uncertain_ownership_does_not_poison_a_later_observed_window():
    state = MemoryState()
    observation(state,'members','m',{'status':'activeMember'},first='2026-01-01')
    observation(state,'cards','c',{'uid_hash':'hash','member_id':'m'},first='2026-01-01')
    observation(state,'checkins','old',{'uid_hash':'hash','timeOf':'2025-01-01'})
    observation(state,'checkins','new',{'uid_hash':'hash','timeOf':'2026-04-20'})
    state.put('health','bootstrap',{'completed_at':'2026-01-02'})
    rows = Analytics(state,config()).refresh('2026-05-01')
    assert rows[0]['confidence'] == 'verified'
    historical = [v for _,v in state.rows('features') if v['as_of'] < '2026-01-01']
    assert historical and all(v['confidence'] != 'verified' for v in historical)


def test_source_rental_cancellation_is_not_membership_churn_and_unknown_revocation_is_not_dated():
    state = MemoryState()
    observation(state,'members','m',{'status':'revoked'})
    observation(state,'audit_logs','rental',{'subject_id':'m','event_type':'subscription_cancelled',
                'resource_type':'Invoice','resource_class':'rental','created_at':'2025-01-15'})
    Analytics(state,config()).refresh('2026-01-01')
    assert not list(state.rows('outcomes'))
    observation(state,'audit_logs','membership',{'subject_id':'m','event_type':'subscription_cancelled',
                'resource_type':'Invoice','resource_class':'member','created_at':'2025-01-15'})
    Analytics(state,config()).refresh('2026-01-01')
    assert [v['outcome'] for _,v in state.rows('outcomes')] == ['cancellation']


def test_reports_use_dashboard_keys_and_suppress_small_outcome_groups():
    state = MemoryState()
    for i in range(5):
        member = 'm'+str(i)
        observation(state,'members',member,{'status':'activeMember','address_city':'town'})
        observation(state,'audit_logs',str(i),{'subject_id':member,'event_type':'membership_revoked','created_at':'2025-10-01'})
        state.put('coverage',str(i),{'member_id':member,'start':'2024-01-01','end':'2025-08-01',
                                   'confidence':'verified','payer_type':'direct'})
    Analytics(state,config()).refresh('2026-01-01')
    metrics = [v for _,v in state.rows('report_metrics')]
    assert {'retention_24m','revocation'} <= {v['report'] for v in metrics}
    assert all(v['sample_size'] >= 5 for v in metrics)
    assert not any(v['report'] == 'sustained_membership' for v in metrics)


@pytest.mark.parametrize('kind', ['membership_revoked', 'subscription_cancelled'])
@pytest.mark.parametrize('correction', ['deleted', 'nonqualifying', 'redated'])
def test_outcomes_follow_current_audit_evidence(kind, correction):
    state = MemoryState()
    for i in range(5):
        observation(state, 'members', 'm'+str(i), {'status': 'activeMember'})
        observation(state, 'audit_logs', str(i), {'subject_id': 'm'+str(i), 'event_type': kind,
                    'resource_type': 'Member', 'created_at': '2025-10-01'})
    analytics = Analytics(state, config())
    analytics.refresh('2026-01-01')
    assert len(list(state.rows('outcomes'))) == 5
    assert any(r['dimension'] == 'outcome_month' for _, r in state.rows('report_metrics'))
    for i in range(5):
        key = 'audit_logs:'+str(i)
        if correction == 'deleted':
            state.delete('observations', key)
            state.put('observation_tombstones', key, {'operation': 'delete'})
        else:
            row = state.get('observations', key)
            row['values'].update({'event_type': 'member_updated'} if correction == 'nonqualifying' else {'created_at': '2025-11-01'})
            state.put('observations', key, row)
    analytics.refresh('2026-01-01')
    outcomes = [row for _, row in state.rows('outcomes')]
    if correction == 'redated':
        assert len(outcomes) == 5 and all(row['at'].startswith('2025-11-01') for row in outcomes)
    else:
        assert not outcomes
        assert not any(r['dimension'] == 'outcome_month' for _, r in state.rows('report_metrics'))


def paid_example():
    state = MemoryState()
    observation(state,'members','m',{'status':'activeMember','expirationTime':'2025-02-01'})
    observation(state,'invoices','i',{'member_id':'m','resource_id':'m','resource_class':'member',
                'operation':'renew=','quantity':1,'paid_positive':True,'settled_at':'2025-01-01'})
    observation(state,'audit_logs','a',{'subject_id':'m','resource_id':'m','resource_type':'Member',
                'created_at':'2025-01-01','field_changes':{'expirationTime':[None,'2025-02-01']}})
    return state


def test_paid_coverage_accepts_initial_term_but_not_free_terms_or_subscriptions():
    state = paid_example()
    assert reconstruct_coverage(state) == 1
    row = next(state.rows('coverage'))[1]
    assert row['start'].startswith('2025-01-01') and row['end'].startswith('2025-02-01')
    invoice = state.get('observations','invoices:i')
    invoice['values']['paid_positive'] = False
    state.put('observations','invoices:i',invoice)
    assert reconstruct_coverage(state) == 0
    assert not list(state.rows('coverage'))


def test_paid_coverage_requires_matching_extent_and_invalidates_refunds():
    state = paid_example()
    invoice = state.get('observations','invoices:i')
    invoice['values']['quantity'] = 2
    state.put('observations','invoices:i',invoice)
    assert reconstruct_coverage(state) == 0
    invoice['values'].update(quantity=1, refunded=True)
    state.put('observations','invoices:i',invoice)
    assert reconstruct_coverage(state) == 0


def test_paid_coverage_can_use_observed_transition_and_deduplicates_audit_receipt():
    state = paid_example()
    state.put('transitions','t',{'collection':'members','entity':'m','at':'2025-01-01',
        'confidence':'observed','before':{'expirationTime':None},'after':{'expirationTime':'2025-02-01'}})
    assert reconstruct_coverage(state) == 1
    assert len(next(state.rows('coverage'))[1]['evidence']) == 3
    state.delete('observations','audit_logs:a')
    assert reconstruct_coverage(state) == 1


def test_historical_state_refuses_present_day_ownership_and_unknown_transitions():
    obs = {'first_seen':'2025-01-01','values':{'member_id':'new'}}
    changes = [{'at':'2025-02-01','confidence':'observed','before':{'member_id':'old'},'after':{'member_id':'new'}}]
    assert state_at(obs,changes,'2025-01-15')['member_id'] == 'old'
    assert state_at(obs,changes,'2024-01-01') is None
    changes[0]['confidence'] = 'uncertain'
    assert state_at(obs,changes,'2025-01-15') is None


def test_unresolved_source_and_detector_findings_cannot_teach_the_baseline():
    from ledger.anomaly.privacy import digest
    state = MemoryState()
    state.put('config','pseudonym_key',{'value':'test-key'})
    state.put('health','bootstrap',{'completed_at':'2025-01-01'})
    observation(state,'members','m',{'status':'activeMember'})
    observation(state,'cards','card',{'uid_hash':'h','member_id':'m'})
    state.put('findings','invalid',{'collection':'cards','entity':digest('card','test-key'),'status':'open'})
    rows = Analytics(state,config()).refresh('2026-01-01')
    assert rows[0]['confidence'] != 'verified'
    assert not state.get('baselines','m')
    state.delete('findings','invalid')
    state.put('detector_findings','outlier',{'member_id':'m','status':'active'})
    assert Analytics(state,config()).refresh('2026-01-02')[0]['confidence'] != 'verified'
    state.put('detector_findings','outlier',{'member_id':'m','status':'resolved','resolved_at':'2026-01-03'})
    assert Analytics(state,config()).refresh('2026-01-04')[0]['confidence'] == 'verified'


def test_pending_contracts_do_not_feed_paid_coverage_or_member_learning():
    state = paid_example()
    state.put('schemas','active',{'pending':{'invoices':['type_changed:quantity']}})
    assert reconstruct_coverage(state) == 0
    state.put('schemas','active',{'pending':{'members':['type_changed:status']}})
    assert Analytics(state,config()).refresh('2026-01-01') == []


def test_payer_retention_does_not_count_prior_beneficiary_time():
    state = MemoryState()
    observation(state,'members','m',{'status':'activeMember'})
    state.put('coverage','dependent',{'member_id':'m','start':'2024-01-01','end':'2025-08-01',
                                    'confidence':'verified','payer_type':'household'})
    state.put('coverage','payer',{'member_id':'m','start':'2025-08-01','end':'2025-09-01',
                               'confidence':'verified','payer_type':'direct'})
    Analytics(state,config()).refresh('2026-01-01')
    assert not state.get('member_analytics','m')['sustained']['qualifies']


@pytest.mark.parametrize('correction', ['refund', 'delete'])
def test_old_qualification_is_compacted_survives_receipt_retention_and_revokes_on_correction(correction):
    state = paid_example()
    invoice = state.get('observations','invoices:i')
    invoice['values'].update(quantity=19,settled_at='2019-01-01')
    state.put('observations','invoices:i',invoice)
    audit = state.get('observations','audit_logs:a')
    audit['values'].update(created_at='2019-01-01',field_changes={'expirationTime':[None,'2020-08-01']})
    state.put('observations','audit_logs:a',audit)
    assert reconstruct_coverage(state,history_months=60,as_of='2026-01-01') == 0
    assert not list(state.rows('coverage'))
    assert state.get('coverage_qualifications','m')['qualifies']
    state.delete('observations','audit_logs:a')
    state.delete('observations','invoices:i')  # Compacted evidence without a source-deletion tombstone.
    reconstruct_coverage(state,history_months=60,as_of='2026-01-01')
    Analytics(state,config()).refresh('2026-01-01')
    assert state.get('member_analytics','m')['sustained']['qualifies']
    if correction == 'refund':
        invoice['values']['refunded'] = True
        state.put('observations','invoices:i',invoice)
    else:
        state.delete('observations','invoices:i')
        state.put('observation_tombstones','invoices:i',{'operation':'delete'})
    reconstruct_coverage(state,history_months=60,as_of='2026-01-01')
    assert not state.get('coverage_qualifications','m')
    Analytics(state,config()).refresh('2026-01-01')
    assert not state.get('member_analytics','m')['sustained']['qualifies']


def test_old_outcomes_are_not_reintroduced_after_retention_prunes_them():
    state = MemoryState()
    observation(state,'members','m',{'status':'activeMember'})
    observation(state,'audit_logs','old',{'subject_id':'m','created_at':'2018-01-01','event_type':'membership_revoked'})
    Analytics(state,config()).refresh('2026-01-01')
    assert not list(state.rows('outcomes'))


def test_model_archive_preserves_prior_validated_compatible_generation(monkeypatch):
    import ledger.anomaly.analytics as module
    state = MemoryState()
    old = {'status':'validated','version':'approved','feature_version':FEATURE_VERSION,'features':list(FEATURES),
           'outcome':'lapse','horizon':30,'metrics':{'pr_auc':.7},'sample_size':50,'training_groups':100,
           'associations':[{'feature':'visit_days_28','standardized_coefficient':-.4}]}
    state.put('models','lapse:30',old)
    monkeypatch.setattr(module,'train_predictor',lambda *args: {'status':'insufficient_evidence'})
    monkeypatch.setattr(module,'isolation_scores',lambda rows, state=None: {})
    Analytics(state,config()).train()
    assert state.get('models','lapse:30') == old
    generations = dict(state.rows('model_generations'))
    assert len(generations) == 6
    Analytics(state,config()).train()
    assert dict(state.rows('model_generations')) == generations
    associations = [r for _,r in state.rows('report_metrics')]
    assert len(associations) == 1 and associations[0]['report'] == 'churn'
    assert associations[0]['sample_size'] == 100 and associations[0]['value'] == -.4
    old['feature_version'] = 'incompatible'
    state.put('models','lapse:30',old)
    Analytics(state,config()).train()
    assert state.get('models','lapse:30')['status'] == 'insufficient_evidence'


def test_baselines_commit_only_after_new_detector_hits_are_quarantined():
    state = MemoryState()
    state.put('health','bootstrap',{'completed_at':'2025-01-01'})
    for member in ('normal','hit'):
        observation(state,'members',member,{'status':'activeMember'})
    analytics = Analytics(state,config())
    analytics.refresh('2026-01-01')
    assert not list(state.rows('baselines'))
    state.put('detector_findings','new',{'member_id':'hit','status':'active'})
    analytics.accept_baselines()
    assert len(state.get('baselines','normal')['history']) == 1
    assert not state.get('baselines','hit')
    assert not list(state.rows('baseline_candidates'))
    analytics.accept_baselines()
    assert len(state.get('baselines','normal')['history']) == 1


def test_supervised_training_does_not_label_post_outcome_rows_negative(monkeypatch):
    import ledger.anomaly.analytics as module
    state = MemoryState()
    for when in ('2025-01-01','2025-03-01'):
        state.put('features',when,{'member_id':'m','group':'m','as_of':when,'confidence':'verified',
                                  'feature_version':FEATURE_VERSION,'features':{f:0 for f in FEATURES}})
    state.put('outcomes','revoked',{'member_id':'m','outcome':'revocation','at':'2025-02-01','confidence':'verified'})
    captured = {}
    def train(rows,outcome,horizon):
        captured[outcome,horizon] = rows
        return {'status':'insufficient_evidence'}
    monkeypatch.setattr(module,'train_predictor',train)
    monkeypatch.setattr(module,'isolation_scores',lambda rows, state=None: {})
    Analytics(state,config()).train()
    assert [r['as_of'] for r in captured['revocation',90]] == ['2025-01-01']


def test_baseline_freezes_first_56_verified_rows_and_archives_semantic_changes():
    state = MemoryState()
    analytics = Analytics(state,config())
    features = {f:0 for f in FEATURES}
    for day in range(57):
        state.put('baseline_candidates','m',{'day':(instant('2025-01-01')+timedelta(days=day)).date().isoformat(),
            'features':{**features,'visit_days_28':day},'confidence':'verified','baseline_version':1,'feature_version':FEATURE_VERSION})
        analytics.accept_baselines()
    baseline = state.get('baselines','m')
    assert len(baseline['stable_reference']['history']) == 56
    assert baseline['stable_reference']['history'][-1]['features']['visit_days_28'] == 55
    baseline['feature_version'] = 'old-semantics'
    state.put('baselines','m',baseline)
    new = analytics.baseline('m')
    assert new['version'] == 2 and not new['history'] and new['feature_version'] == FEATURE_VERSION
    assert len(list(state.rows('baseline_generations'))) == 1
    state.put('baseline_candidates','m',{'day':'2025-03-01','features':features,'confidence':'verified',
                                       'baseline_version':2,'feature_version':FEATURE_VERSION})
    analytics.accept_baselines()
    assert state.get('baselines','m')['version'] == 2
    assert not state.get('baselines','m').get('stable_reference')
    assert next(state.rows('baseline_generations'))[1] == baseline


def test_frozen_reference_detects_drift_that_recent_history_has_absorbed():
    state = MemoryState()
    observation(state,'members','m',{'status':'activeMember'})
    state.put('health','bootstrap',{'completed_at':'2025-01-01'})
    current = visit_features([],instant('2026-01-01'))
    recent = [{'day':(instant('2025-10-01')+timedelta(days=day)).date().isoformat(),
               'features':current,'confidence':'verified'} for day in range(90)]
    frozen = [{'day':(instant('2025-01-01')+timedelta(days=day)).date().isoformat(),
               'features':{**current,'visit_days_28':20},'confidence':'verified'} for day in range(56)]
    state.put('baselines','m',{'version':1,'feature_version':FEATURE_VERSION,'history':recent,
                              'stable_reference':{'history':frozen,'version':1,'feature_version':FEATURE_VERSION}})
    Analytics(state,config()).refresh('2026-01-01')
    finding = state.get('behavior_findings','m:2026-01-01')
    assert 'stable:visit_days_28' in finding['scores']
    assert 'recent:visit_days_28' not in finding['scores']


def test_robust_evidence_includes_empirical_quantiles_and_percentile():
    score = robust_score(9,list(range(10)))
    assert score['ready'] and score['p05'] == pytest.approx(.45)
    assert score['p95'] == pytest.approx(8.55) and score['empirical_percentile'] == .95


def test_forest_persists_json_artifact_and_generation_without_pickle():
    import json
    state = MemoryState()
    rows = []
    for member in range(12):
        for day in range(8):
            rows.append({'member_id':str(member),'as_of':f'2026-01-{day+1:02d}',
                'features':{f:float(member+day) for f in FEATURES},'confidence':'verified','feature_version':FEATURE_VERSION})
    scores = isolation_scores(rows,state=state)
    artifact = state.get('models','isolation')
    assert len(scores) == 12 and artifact['feature_version'] == FEATURE_VERSION
    assert len(artifact['trees']) == 100 and artifact['training_fingerprint']
    assert {'children_left','children_right','feature','threshold','n_node_samples'} <= artifact['trees'][0].keys()
    assert json.loads(json.dumps(artifact)) == artifact
    previous = dict(state.rows('model_generations'))
    isolation_scores(rows,state=state)
    assert dict(state.rows('model_generations')) == previous


def test_prediction_rejects_obsolete_feature_semantics():
    model = {'status':'validated','features':list(FEATURES),'coefficients':[0]*len(FEATURES),
             'mean':[0]*len(FEATURES),'scale':[1]*len(FEATURES),'intercept':0,'feature_version':FEATURE_VERSION}
    assert predict(model,{f:0 for f in FEATURES}) == .5
    model['feature_version'] = 'previous-semantics'
    assert predict(model,{f:0 for f in FEATURES}) is None


def test_already_revoked_without_a_dated_event_is_outside_the_risk_set():
    state = MemoryState()
    observation(state,'members','m',{'status':'revoked'})
    state.put('health','bootstrap',{'completed_at':'2025-01-01'})
    row = Analytics(state,config()).refresh('2026-01-01')[0]
    assert row['confidence'] == 'verified' and row['at_risk'] is False


def test_high_ranking_but_miscalibrated_probabilities_are_not_published(monkeypatch):
    import numpy as np
    import sklearn.linear_model
    class Miscalibrated:
        def __init__(self, **kwargs):
            pass
        def fit(self, x, y, sample_weight=None):
            self.coef_, self.intercept_ = np.zeros((1,len(FEATURES))), np.array([0.0])
            return self
        def predict_proba(self, x):
            p = np.where(x[:,FEATURES.index('payment_failures_28')] > 0,.99,.98)
            return np.column_stack([1-p,p])
    monkeypatch.setattr(sklearn.linear_model,'LogisticRegression',Miscalibrated)
    rows = []
    for member in range(300):
        for week in range(20):
            rows.append({'member_id':str(member),'group':str(member),'as_of':(instant('2024-01-01')+timedelta(days=week*21)).isoformat(),
                'features':{**{f:0.0 for f in FEATURES},'payment_failures_28':float(member%2)},
                'confidence':'verified','label':member%2})
    model = train_predictor(rows,'lapse',30)
    assert model['metrics']['pr_auc'] > model['metrics']['recency_pr_auc']
    assert model['status'] == 'calibration_not_validated'
    assert model['metrics']['brier'] > model['metrics']['prior_brier']


def test_source_finding_arriving_after_refresh_blocks_baseline_acceptance():
    from ledger.anomaly.privacy import digest
    state = MemoryState()
    state.put('config','pseudonym_key',{'value':'key'})
    state.put('health','bootstrap',{'completed_at':'2025-01-01'})
    observation(state,'members','m',{'status':'activeMember'})
    analytics = Analytics(state,config())
    analytics.refresh('2026-01-01')
    state.put('findings','late',{'collection':'members','entity':digest('m','key'),'status':'open'})
    analytics.accept_baselines()
    assert not state.get('baselines','m')
