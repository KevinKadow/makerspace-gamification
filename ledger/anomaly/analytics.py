"""Temporal, evidence-aware features and CPU models. No model makes source decisions."""
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
from statistics import median
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta

from .privacy import timestamp, aggregate_rows
from .evidence import state_at, source_observations
from .schema import fingerprint
from .state import stamp

FEATURES = ("visit_days_7", "visit_days_28", "visit_days_90", "days_since_visit", "visit_ratio", "payment_failures_28",
            "weekday_shift", "hour_shift", "weekend_fraction", "evening_fraction", "season_sin", "season_cos")
FEATURE_VERSION = fingerprint([2, FEATURES, "prospective_90_days", "local_unique_visit_days"])
OUTCOMES = ("cancellation", "lapse", "revocation")


def merge_intervals(intervals):
    result = []
    for start, end in sorted((timestamp(a), timestamp(b)) for a, b in intervals if timestamp(a) and timestamp(b) and timestamp(a) < timestamp(b)):
        if result and start <= result[-1][1]:
            result[-1] = result[-1][0], max(end, result[-1][1])
        else:
            result.append((start, end))
    return result


def paid_fraction(intervals, window_end):
    """Actual duration within exactly 24 calendar months, never rounded to months."""
    end = timestamp(window_end)
    start = end - relativedelta(months=24)
    covered = sum(max(0, (min(b, end) - max(a, start)).total_seconds()) for a, b in merge_intervals(intervals))
    return covered / (end - start).total_seconds()


def sustained_membership(intervals, observed_until):
    """Day-granular 24-month windows with a prefix integral, not repeated merges."""
    merged = merge_intervals(intervals)
    observed = timestamp(observed_until)
    if not merged or not observed:
        return {"qualifies": False, "fraction": 0.0, "coverage": "unknown"}
    starts = [a for a, _ in merged]
    totals = [0.0]
    for a, b in merged:
        totals.append(totals[-1] + (b-a).total_seconds())
    def cumulative(at):
        i = bisect_right(starts, at) - 1
        if i < 0:
            return 0.0
        a, b = merged[i]
        return totals[i] + max(0, (min(at, b)-a).total_seconds())
    # Known positive coverage can prove the >75% threshold before all 24 months
    # are observed; missing history can never prove a negative.
    cursor = merged[0][0].replace(hour=0, minute=0, second=0, microsecond=0)
    best, best_end = 0.0, None
    while cursor <= observed:
        start = cursor - relativedelta(months=24)
        fraction = (cumulative(cursor)-cumulative(start)) / (cursor-start).total_seconds()
        if fraction > best:
            best, best_end = fraction, cursor
        cursor += timedelta(days=1)
    return {"qualifies": best > .75, "fraction": best, "window_end": best_end.isoformat() if best_end else None,
            "coverage": "verified_positive" if best > .75 else "unknown"}


def visit_features(visits, as_of, failures=(), timezone_name="America/New_York"):
    cutoff = timestamp(as_of)
    zone = ZoneInfo(timezone_name)
    # Count a repeated badge scan only once per visit hour; days remain the frequency unit.
    eligible = sorted({timestamp(v) for v in visits if timestamp(v) and timestamp(v) <= cutoff})
    windows = {n: {v.astimezone(zone).date() for v in eligible if v > cutoff - timedelta(days=n)} for n in (7, 28, 90)}
    prior = len({v.astimezone(zone).date() for v in eligible if cutoff - timedelta(days=90) < v <= cutoff - timedelta(days=28)})
    recent = {v.astimezone(zone).replace(minute=0, second=0, microsecond=0) for v in eligible if v > cutoff - timedelta(days=28)}
    previous = {v.astimezone(zone).replace(minute=0, second=0, microsecond=0) for v in eligible if cutoff-timedelta(days=90) < v <= cutoff-timedelta(days=28)}
    weekdays = lambda values: dict(Counter(str(v.weekday()) for v in values))
    hours = lambda values: dict(Counter(str(v.hour // 4) for v in values))
    local = cutoff.astimezone(zone)
    phase = 2 * math.pi * (local.timetuple().tm_yday - 1) / (366 if local.year % 4 == 0 else 365)
    return {"visit_days_7": len(windows[7]), "visit_days_28": len(windows[28]), "visit_days_90": len(windows[90]),
            "days_since_visit": min(366, (cutoff-eligible[-1]).total_seconds()/86400) if eligible else 366,
            "visit_ratio": (len(windows[28])/28)/(prior/62) if prior else 1.0,
            "payment_failures_28": len({timestamp(v) for v in failures if timestamp(v) and cutoff-timedelta(days=28) < timestamp(v) <= cutoff}),
            "weekday_shift": distribution_distance(weekdays(recent), weekdays(previous)) or 0.0,
            "hour_shift": distribution_distance(hours(recent), hours(previous)) or 0.0,
            "weekend_fraction": sum(v.weekday() >= 5 for v in recent)/len(recent) if recent else 0.0,
            "evening_fraction": sum(v.hour >= 17 for v in recent)/len(recent) if recent else 0.0,
            "season_sin": math.sin(phase), "season_cos": math.cos(phase),
            "weekday_histogram": weekdays(recent), "hour_histogram": hours(recent)}


def robust_score(value, history, minimum=8):
    clean = [float(x) for x in history if type(x) in (int, float) and math.isfinite(x)]
    if len(clean) < minimum:
        return {"ready": False, "score": 0.0, "sample_size": len(clean)}
    center = median(clean)
    mad = median(abs(v - center) for v in clean)
    scale = max(1.4826 * mad, .1 * abs(center), 1.0)
    ordered = sorted(clean)
    def quantile(q):
        index = q*(len(ordered)-1)
        low = int(index)
        high = min(len(ordered)-1,low+1)
        return ordered[low]+(ordered[high]-ordered[low])*(index-low)
    percentile = (sum(x < value for x in clean)+.5*sum(x == value for x in clean))/len(clean)
    return {"ready":True,"score":abs(value-center)/scale,"median":center,"scale":scale,"sample_size":len(clean),
            "p05":quantile(.05),"p95":quantile(.95),"empirical_percentile":percentile}


def distribution_distance(left, right):
    """Total variation distance, robust to empty histograms."""
    a, b = sum(left.values()), sum(right.values())
    if not a or not b:
        return None
    return .5 * sum(abs(left.get(k, 0) / a - right.get(k, 0) / b) for k in set(left) | set(right))


def complete_window(start, end, coverage_start, observed_until, gaps=()):
    start, end, first, last = map(timestamp, (start, end, coverage_start, observed_until))
    if not all((start, end, first, last)) or first > start or last < end:
        return False
    return not any(timestamp(a) and timestamp(a) <= end and
                   (timestamp(b) if b else last) >= start for a, b in gaps)


def label_at(events, as_of, horizon, observed_until, coverage_start=None, gaps=()):
    """Absence is negative only after complete prospective follow-up."""
    start, observed = timestamp(as_of), timestamp(observed_until)
    if not start or not observed:
        return None
    end = start + timedelta(days=horizon)
    if any(start < timestamp(t) <= min(end, observed) for t in events if timestamp(t)):
        return 1
    return 0 if complete_window(start, end, coverage_start, observed, gaps) else None


def _test_group(group):
    return int(sha256(str(group).encode()).hexdigest()[:8], 16) % 5 == 0


def temporal_partition(rows, horizon):
    """Household partition is independent of time; purge training label overlap."""
    times = sorted({r['as_of'] for r in rows})
    if len(times) < 2:
        return [], [], None
    split = times[min(len(times)-1, int(len(times)*.8))]
    training = [r for r in rows if not _test_group(r['group']) and
                timestamp(r['as_of']) + timedelta(days=horizon) < timestamp(split)]
    testing = [r for r in rows if _test_group(r['group']) and r['as_of'] >= split]
    return training, testing, split


def train_predictor(rows, outcome, horizon, minimum_positives=50):
    """Group-disjoint, chronological evaluation; all transforms fit on training."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import average_precision_score, precision_score, recall_score, brier_score_loss
    from sklearn.preprocessing import StandardScaler
    import numpy as np
    eligible = sorted((r for r in rows if r.get('label') in (0, 1) and r.get('confidence') == 'verified'
                       and timestamp(r.get('as_of')) and all(type(r.get('features', {}).get(f)) in (int, float)
                           and math.isfinite(r['features'][f]) for f in FEATURES)), key=lambda r: r['as_of'])
    # One snapshot per member/week prevents collection frequency changing model weight.
    weekly = {}
    for row in eligible:
        weekly[row['member_id'], timestamp(row['as_of']).date().isocalendar()[:2]] = row
    eligible = list(weekly.values())
    positive_groups = {r['group'] for r in eligible if r['label']}
    if len(positive_groups) < minimum_positives or len(eligible) < 100:
        return {'status': 'insufficient_evidence', 'positive_groups': len(positive_groups)}
    training, testing, split = temporal_partition(eligible, horizon)
    test_positive = {r['group'] for r in testing if r['label']}
    test_negative = {r['group'] for r in testing if not r['label']}
    if len(training) < 50 or min(len(test_positive), len(test_negative)) < 10:
        return {'status': 'insufficient_temporal_holdout', 'positive_groups': len(positive_groups)}
    if min(len({r['group'] for r in training if r['label'] == label}) for label in (0,1)) < minimum_positives:
        return {'status': 'insufficient_training_classes'}
    x = np.array([[r['features'][f] for f in FEATURES] for r in training])
    y = np.array([r['label'] for r in training])
    xt = np.array([[r['features'][f] for f in FEATURES] for r in testing])
    yt = np.array([r['label'] for r in testing])
    counts = Counter(r['group'] for r in training)
    weights = np.array([len(training)/len(counts)/counts[r['group']] for r in training])
    test_counts = Counter(r['group'] for r in testing)
    test_weights = np.array([1/test_counts[r['group']] for r in testing])
    scaler = StandardScaler().fit(x, sample_weight=weights)
    model = LogisticRegression(C=.1, max_iter=1000, random_state=0).fit(scaler.transform(x), y, sample_weight=weights)
    probabilities = model.predict_proba(scaler.transform(xt))[:, 1]
    ap = float(average_precision_score(yt, probabilities, sample_weight=test_weights))
    base_rate = float(np.average(yt, weights=test_weights))
    recency_ap = float(average_precision_score(yt, xt[:, FEATURES.index('days_since_visit')], sample_weight=test_weights))
    metrics = {'pr_auc': ap, 'base_rate': base_rate, 'recency_pr_auc': recency_ap,
               'precision': float(precision_score(yt, probabilities >= .5, sample_weight=test_weights, zero_division=0)),
               'recall': float(recall_score(yt, probabilities >= .5, sample_weight=test_weights, zero_division=0)),
               'brier': float(brier_score_loss(yt, probabilities, sample_weight=test_weights))}
    bins, calibration_error = [], 0.0
    for lower in (0, .2, .4, .6, .8):
        mask = (probabilities >= lower) & (probabilities <= 1 if lower == .8 else probabilities < lower+.2)
        if not mask.any():
            continue
        predicted = float(np.average(probabilities[mask],weights=test_weights[mask]))
        observed = float(np.average(yt[mask],weights=test_weights[mask]))
        calibration_error += float(test_weights[mask].sum())*abs(predicted-observed)
        unique = {testing[i]['group'] for i,included in enumerate(mask) if included}
        if len(unique) >= 5:
            bins.append({'predicted':predicted,'observed':observed,'n':len(unique)})
    calibration_error /= float(test_weights.sum())
    # Probabilities are published only when discrimination and calibration beat
    # a constant-prior forecast on the untouched temporal holdout.
    metrics.update(calibration_error=float(calibration_error),prior_brier=base_rate*(1-base_rate))
    calibrated = metrics['brier'] <= metrics['prior_brier'] and calibration_error <= .1 and all(abs(b['predicted']-b['observed']) <= .2 for b in bins)
    status = 'baseline_not_beaten' if ap <= max(base_rate,recency_ap) else 'validated' if calibrated else 'calibration_not_validated'
    return {'status': status, 'outcome': outcome,
            'horizon': horizon, 'features': list(FEATURES), 'mean': scaler.mean_.tolist(), 'scale': scaler.scale_.tolist(),
            'coefficients': model.coef_[0].tolist(), 'intercept': float(model.intercept_[0]),
            'metrics': metrics, 'calibration': bins, 'sample_size': len(test_counts), 'test_rows': len(testing),
            'training_groups': len(counts), 'training_cutoff': split,
            'associations': sorted([{'feature': f, 'standardized_coefficient': float(c)} for f,c in zip(FEATURES, model.coef_[0])], key=lambda r: -abs(r['standardized_coefficient'])),
            'feature_version': FEATURE_VERSION,
            'version': fingerprint([outcome, horizon, split, metrics, FEATURES, model.coef_[0].tolist(), scaler.mean_.tolist()])}


def predict(model, features):
    if model.get("status") != "validated" or model.get("feature_version") != FEATURE_VERSION:
        return None
    z = model["intercept"] + sum(c * (features[f] - m) / s for f, c, m, s in
        zip(model["features"], model["coefficients"], model["mean"], model["scale"]))
    return 1 / (1 + math.exp(-max(-700, min(700, z))))


def isolation_scores(rows, state=None):
    from sklearn.ensemble import IsolationForest
    rows = [r for r in rows if r.get('confidence') == 'verified' and r.get('feature_version') == FEATURE_VERSION
            and all(type(r.get('features',{}).get(f)) in (int,float) and math.isfinite(r['features'][f]) for f in FEATURES)]
    if len(rows) < 50:
        return {}
    # Fit on the historical generation only, then score its held-out latest cohort.
    times = sorted({r["as_of"] for r in rows})
    if len(times) < 8:
        return {}
    cutoff = times[-1]
    training = [r for r in rows if r["as_of"] < cutoff]
    testing = [r for r in rows if r["as_of"] == cutoff]
    if len(training) < 50 or not testing:
        return {}
    model = IsolationForest(n_estimators=100, contamination=.01, random_state=0, n_jobs=1).fit(
        [[r["features"][f] for f in FEATURES] for r in training])
    if state is not None:
        training_fingerprint = fingerprint([[r['member_id'],r['as_of'],r['features']] for r in training])
        version = fingerprint(['isolation',FEATURE_VERSION,training_fingerprint])
        artifact = {'version':version,'status':'shadow','model_type':'isolation_forest','features':list(FEATURES),
            'feature_version':FEATURE_VERSION,'training_fingerprint':training_fingerprint,'training_cutoff':cutoff,
            'offset':float(model.offset_),'max_samples':int(model.max_samples_),
            'trees':[{'children_left':est.tree_.children_left.tolist(),'children_right':est.tree_.children_right.tolist(),
                'feature':est.tree_.feature.tolist(),'threshold':est.tree_.threshold.tolist(),
                'n_node_samples':est.tree_.n_node_samples.tolist(),'features':columns.tolist()}
                for est,columns in zip(model.estimators_,model.estimators_features_)]}
        with state.atomic():
            if not state.get('model_generations','isolation:'+version):
                state.put('model_generations','isolation:'+version,artifact)
            state.put('models','isolation',artifact)
    scores = model.decision_function([[r["features"][f] for f in FEATURES] for r in testing])
    return {r["member_id"]: float(s) for r, s in zip(testing, scores)}


class Analytics:
    def __init__(self, state, config):
        self.state, self.config = state, config

    def baseline(self, member):
        prior = self.state.get('baselines',member)
        if prior and prior.get('feature_version') == FEATURE_VERSION:
            return prior
        if prior:
            key = member+':'+fingerprint([prior.get('version'),prior.get('feature_version','legacy')])
            if not self.state.get('baseline_generations',key):
                self.state.put('baseline_generations',key,prior)
        return {'history':[], 'version':(prior or {}).get('version',0)+1, 'feature_version':FEATURE_VERSION}

    def refresh(self, as_of=None):
        now = timestamp(as_of) or datetime.now(timezone.utc)
        observations, invalid = source_observations(self.state)
        history_cutoff = now-relativedelta(months=self.config.history_months)
        members = {k.split(':', 1)[1]: v for k,v in observations.items() if k.startswith('members:')}
        changes = defaultdict(list)
        active_collections = self.state.get('schemas', 'active', {}).get('collections')
        for _, row in self.state.rows('transitions'):
            if timestamp(row.get('at')) and (active_collections is None or row.get('collection') in active_collections):
                changes[row.get('collection'), row.get('entity')].append(row)
        first = self.state.get('health', 'bootstrap', {}).get('completed_at')
        gaps = [(v['start'], v.get('end')) for _,v in self.state.rows('gaps')]
        visits, uncertain, failures = defaultdict(list), defaultdict(list), defaultdict(list)
        cards = defaultdict(set)
        for key, obs in observations.items():
            if key.startswith('cards:'):
                card = key.split(':',1)[1]
                for values in [obs['values']] + [c.get(side,{}) for c in changes['cards',card] for side in ('before','after')]:
                    if values.get('uid_hash'):
                        cards[values['uid_hash']].add(card)
        outcomes = {}
        def outcome(member, kind, when, cause, evidence):
            if not member or not when or not history_cutoff <= when <= now:
                return
            key = fingerprint([member, kind, when.isoformat(), cause])
            outcomes[key] = {'member_id': member, 'outcome': kind, 'at': when.isoformat(),
                             'cause': cause, 'confidence': 'verified', 'evidence': evidence}
        for key, observation in observations.items():
            row = observation['values']
            if key.startswith('checkins:'):
                when = timestamp(row.get('timeOf')) or timestamp(row.get('time'))
                if not when or not history_cutoff <= when <= now:
                    continue
                known, guessed = set(), set()
                for card in cards.get(row.get('uid_hash'), ()):
                    obs = observations.get('cards:' + card)
                    saved = state_at(obs, changes['cards',card], when)
                    if saved and saved.get('uid_hash') == row.get('uid_hash') and saved.get('member_id'):
                        known.add(saved['member_id'])
                    current = (obs or {}).get('values',{})
                    if current.get('uid_hash') == row.get('uid_hash') and current.get('member_id'):
                        guessed.add(current['member_id'])
                if len(known) == 1:
                    visits[next(iter(known))].append(when)
                elif not known and len(guessed) == 1:
                    member = next(iter(guessed))
                    visits[member].append(when)
                    uncertain[member].append(when)
            elif key.startswith('audit_logs:'):
                member, when = row.get('subject_id'), timestamp(row.get('created_at'))
                kind = row.get('event_type')
                if kind == 'membership_revoked':
                    outcome(member, 'revocation', when, kind, key)
                elif kind in {'subscription_cancelled','subscription_auto_cancelled_payment_failure'}:
                    invoice = observations.get('invoices:' + str(row.get('resource_id')), {}).get('values',{})
                    resource_class = row.get('resource_class') or invoice.get('resource_class')
                    # Rental cancellations and unknown deleted invoices cannot label churn.
                    if resource_class in {'member','household'} or row.get('resource_type') == 'Member':
                        outcome(member, 'cancellation', when, kind, key)
                elif kind == 'invoice_payment_failed' and member and when:
                    invoice = observations.get('invoices:' + str(row.get('resource_id')), {}).get('values',{})
                    if (row.get('resource_class') or invoice.get('resource_class')) in {'member','household'}:
                        failures[member].append(when)
        for (collection, entity), history in changes.items():
            for change in history:
                if change.get('confidence') != 'observed':
                    continue
                before, after = change.get('before',{}), change.get('after',{})
                when = timestamp(change['at'])
                if collection == 'members' and before and after:
                    if before.get('status') != 'revoked' and after.get('status') == 'revoked':
                        outcome(entity, 'revocation', when, 'observed_status_transition', 'stream')
                    if before.get('subscription') is True and after.get('subscription') is False:
                        outcome(entity, 'cancellation', when, 'observed_subscription_ended', 'stream')
                if collection == 'invoices' and after.get('resource_class') in {'member','household'}:
                    if not before.get('payment_failed') and after.get('payment_failed'):
                        member = after.get('member_id') or after.get('resource_id')
                        if member:
                            failures[member].append(when)
        for member in visits:
            visits[member] = sorted(set(visits[member]))
        # The split unit connects every observed household relationship. Future
        # linkage is used solely to prevent household leakage, never as a feature.
        parents = {}
        def find(member):
            parents.setdefault(member,member)
            if parents[member] != member:
                parents[member] = find(parents[member])
            return parents[member]
        for member, obs in members.items():
            for row in [obs['values']] + [v.get(side,{}) for v in changes['members',member] for side in ('before','after')]:
                if row.get('groupName'):
                    left, right = find(member), find(row['groupName'])
                    parents[max(left,right)] = min(left,right)
        blocked_members = {r.get('member_id') for namespace in ('detector_findings','behavior_findings') for _,r in self.state.rows(namespace)
                           if r.get('status') in {'active','shadow'} and not r.get('resolved_at')}
        # Invalid check-ins without a usable owner make visit completeness unknown.
        unassigned_invalid_visits = False
        for key, obs in invalid.items():
            values = obs.get('values',{})
            if key.startswith('cards:') and values.get('member_id'):
                blocked_members.add(values['member_id'])
            elif key.startswith('checkins:'):
                when = timestamp(values.get('timeOf')) or timestamp(values.get('time'))
                if not when or when > now-timedelta(days=90):
                    owners = {observations.get('cards:'+card,{}).get('values',{}).get('member_id')
                              for card in cards.get(values.get('uid_hash'),())}
                    owners.discard(None)
                    blocked_members.update(owners)
                    unassigned_invalid_visits |= not bool(owners)
            elif key.startswith('invoices:'):
                blocked_members.add(values.get('member_id') or values.get('resource_id'))
        coverage, paying_coverage, payer_types = defaultdict(list), defaultdict(list), defaultdict(set)
        for _, row in self.state.rows('coverage'):
            if row.get('confidence') == 'verified':
                coverage[row['member_id']].append((row['start'],row['end']))
                payer_types[row['member_id']].add(row.get('payer_type'))
                if row.get('payer_type') in {'direct','household_payer'}:
                    paying_coverage[row['member_id']].append((row['start'],row['end']))
        def snapshot(member, obs, cutoff, historical=False):
            values = visits[member]
            end = bisect_right(values, cutoff)
            begin = bisect_right(values, cutoff-timedelta(days=90))
            features = visit_features(values[max(0,begin-1):end], cutoff, failures[member], self.config.timezone)
            window = cutoff-timedelta(days=90)
            verified = (not historical and member not in blocked_members and not unassigned_invalid_visits and complete_window(window, cutoff, first, now, gaps)
                        and timestamp(obs.get('first_seen')) and timestamp(obs['first_seen']) <= window
                        and not any(window < v <= cutoff for v in uncertain[member]))
            return {'member_id': member, 'group': find(member), 'as_of': cutoff.isoformat(), 'features': features,
                    'feature_version': FEATURE_VERSION,
                    'at_risk': (state_at(obs,changes['members',member],cutoff) or {}).get('status') in {'activeMember','pending','inactive','nonMember'},
                    'confidence': 'verified' if verified else 'historical_ownership_uncertain',
                    'coverage': 'complete_90_days' if verified else 'incomplete_or_uncertain_history'}
        rows, cohort = [], defaultdict(set)
        for member, obs in members.items():
            row = obs['values']
            # Rebuild historical visit patterns as explicitly uncertain descriptive
            # data. No current status/address is projected backwards or used as a label.
            if not self.state.get('feature_backfills', member):
                earliest = max(visits[member][0], now-relativedelta(months=self.config.history_months)) if visits[member] else now
                cursor = earliest.replace(hour=0,minute=0,second=0,microsecond=0) + timedelta(days=7)
                while cursor < now-timedelta(days=1):
                    key = member+':'+cursor.date().isoformat()
                    if not self.state.get('features',key):
                        self.state.put('features',key,snapshot(member,obs,cursor,historical=True))
                    cursor += timedelta(days=7)
                self.state.put('feature_backfills',member,{'at': now.isoformat()})
            feature = snapshot(member,obs,now)
            self.state.put('features',member+':'+now.date().isoformat(),feature)
            rows.append(feature)
            features = feature['features']
            baseline = self.baseline(member)
            if now.date().isoformat() not in {v['day'] for v in baseline['history']}:
                prior = [v for v in baseline['history'] if v.get('confidence') == 'verified'
                         and v['day'] >= (now-timedelta(days=90)).date().isoformat()
                         and datetime.fromisoformat(v['day']).weekday() == now.weekday()]
                frozen = [v for v in baseline.get('stable_reference',{}).get('history',[])
                          if datetime.fromisoformat(v['day']).weekday() == now.weekday()]
                scores = {reference+':'+f: robust_score(features[f],[v['features'][f] for v in history if f in v['features']])
                          for reference,history in (('recent',prior),('stable',frozen))
                          for f in FEATURES if not f.startswith('season_')}
                unusual = {f:v for f,v in scores.items() if v['ready'] and v['score'] >= 4}
                if unusual and feature['confidence'] == 'verified':
                    self.state.put('behavior_findings',member+':'+now.date().isoformat(),
                        {'member_id':member,'as_of':now.isoformat(),'scores':unusual,'status':'shadow','baseline_version':baseline['version']})
                elif feature['confidence'] == 'verified':
                    # Custom detectors run next. Commit only after their findings
                    # have been persisted, so the first hit cannot teach "normal".
                    self.state.put('baseline_candidates',member,{'day':now.date().isoformat(),'features':features,
                        'confidence':feature['confidence'],'baseline_version':baseline['version'],'feature_version':FEATURE_VERSION})
            intervals = merge_intervals(coverage[member])
            for _, expiry in intervals:
                grace_end = expiry+timedelta(days=self.config.grace_days)
                at_grace = state_at(obs,changes['members',member],grace_end)
                source_expiry = timestamp((at_grace or {}).get('expirationTime'))
                if (complete_window(expiry,grace_end,first,now,gaps) and at_grace
                        and at_grace.get('status') not in {'revoked','suspended'}
                        and source_expiry and source_expiry <= grace_end
                        and not any(a <= grace_end < b for a,b in intervals)):
                    outcome(member,'lapse',grace_end,'expired_'+str(self.config.grace_days)+'_day_grace','paid_coverage_and_source_expiry')
            # Paying subscribers exclude household beneficiaries from the main cohort.
            paying = bool(payer_types[member] & {'direct','household_payer'})
            sustained = sustained_membership(paying_coverage[member],now) if paying else {'qualifies':False,'fraction':0.0,'coverage':'unknown'}
            archived = self.state.get('coverage_qualifications',member,{})
            if archived.get('qualifies') and archived.get('fraction',0) > sustained['fraction']:
                sustained = {k:v for k,v in archived.items() if k not in {'invoice_evidence','proof'}}
            self.state.put('member_analytics',member,{**feature,'sustained':sustained,
                'current_status':row.get('status','unknown'),'payer_type':sorted(payer_types[member]) or ['unknown']})
            cohort['membership','current_status',row.get('status','unknown')].add(member)
            if sustained['qualifies']:
                cohort['retention_24m','cohort','verified_positive'].add(member)
                for dimension,field in (('city','address_city'),('postal_code','address_postal_code')):
                    if row.get(field):
                        cohort['retention_24m',dimension,row[field]].add(member)
                histogram = features['weekday_histogram']
                if histogram:
                    cohort['retention_24m','visit_weekday',max(histogram,key=histogram.get)].add(member)
                frequency = features['visit_days_28']
                band = '0' if frequency == 0 else '1-4' if frequency <= 4 else '5-12' if frequency <= 12 else '13+'
                cohort['retention_24m','visit_days_28',band].add(member)
            else:
                cohort['retention_24m','cohort','insufficient_payment_history'].add(member)
        with self.state.atomic():
            # Outcomes are derived from the current evidence, including corrections
            # and deletions. A redated event must replace its former label.
            for key, _ in self.state.rows('outcomes'):
                if key not in outcomes:
                    self.state.delete('outcomes', key)
            for key,row in outcomes.items():
                self.state.put('outcomes',key,row)
        for _, outcome_row in self.state.rows('outcomes'):
            when = timestamp(outcome_row.get('at'))
            if not when or not history_cutoff <= when <= now or outcome_row.get('confidence') != 'verified':
                continue
            report = 'revocation' if outcome_row['outcome'] == 'revocation' else 'churn'
            cohort[report,'outcome_month',outcome_row['outcome']+':'+when.strftime('%Y-%m')].add(outcome_row['member_id'])
        metrics = [{'report':report,'dimension':dim,'bucket':bucket,'value':len(ids),'sample_size':len(ids),'as_of':now.isoformat()}
                   for (report,dim,bucket),ids in cohort.items()]
        metrics += [{'report':'attendance','dimension':'window','bucket':str(n),
                     'value':sum(r['features'][f'visit_days_{n}'] for r in rows),
                     'sample_size':sum(r['features'][f'visit_days_{n}'] > 0 for r in rows),'as_of':now.isoformat()} for n in (7,28,90)]
        metrics += self.model_reports(now.isoformat())
        self.state.put('health','retention_comparison',{'status':'insufficient_verified_comparison',
            'reason':'Missing paid intervals do not prove nonqualification; current positive-cohort patterns are descriptive only.',
            'updated_at':now.isoformat()})
        with self.state.atomic():
            for key,_ in list(self.state.rows('report_metrics')):
                self.state.delete('report_metrics',key)
            for metric in aggregate_rows(metrics,self.config.minimum_cohort):
                self.state.put('report_metrics',fingerprint([metric['report'],metric['dimension'],metric['bucket']]),metric)
        self.state.put('health','features',{'status':'ready','updated_at':now.isoformat(),'members':len(rows),
            'verified_members':sum(r['confidence']=='verified' for r in rows)})
        return rows

    def model_reports(self, now):
        metrics = []
        for _, model in self.state.rows('models'):
            if model.get('status') != 'validated' or model.get('feature_version') != FEATURE_VERSION:
                continue
            report = 'revocation' if model['outcome'] == 'revocation' else 'churn'
            for row in model.get('associations',[]):
                metrics.append({'report':report,'dimension':f"validated_association_{model['outcome']}_{model['horizon']}d",
                    'bucket':row['feature'],'value':row['standardized_coefficient'],'sample_size':model['training_groups'],
                    'as_of':model.get('created_at',now),'model_version':model['version'],
                    'interpretation':'standardized log-odds association, not causation'})
        return metrics

    def accept_baselines(self):
        """Called after detector evaluation; unresolved findings quarantine candidates."""
        blocked = {r.get('member_id') for namespace in ('detector_findings','behavior_findings')
                   for _,r in self.state.rows(namespace) if r.get('status') in {'active','shadow'} and not r.get('resolved_at')}
        observations, invalid = source_observations(self.state)
        for key, row in invalid.items():
            values = row.get('values',{})
            if key.startswith('members:'):
                blocked.add(key.split(':',1)[1])
            elif key.startswith(('cards:','invoices:')):
                blocked.add(values.get('member_id') or values.get('resource_id'))
            elif key.startswith('checkins:'):
                owners = {r['values'].get('member_id') for k,r in observations.items()
                          if k.startswith('cards:') and r['values'].get('uid_hash') == values.get('uid_hash')}
                if owners:
                    blocked.update(owners)
                else:
                    blocked.update(member for member,_ in self.state.rows('baseline_candidates'))
        with self.state.atomic():
            for member,candidate in list(self.state.rows('baseline_candidates')):
                if member not in blocked and candidate.get('confidence') == 'verified' and candidate.get('feature_version') == FEATURE_VERSION:
                    baseline = self.baseline(member)
                    if candidate['baseline_version'] == baseline['version'] and candidate['day'] not in {r['day'] for r in baseline['history']}:
                        baseline['history'] = (baseline['history']+[{k:v for k,v in candidate.items() if k != 'baseline_version'}])[-400:]
                        verified = [r for r in baseline['history'] if r.get('confidence') == 'verified']
                        if len(verified) >= 56 and not baseline.get('stable_reference'):
                            baseline['stable_reference'] = {'history':verified[:56], 'version':baseline['version'],
                                'feature_version':FEATURE_VERSION,'frozen_at':candidate['day']}
                        self.state.put('baselines',member,baseline)
                self.state.delete('baseline_candidates',member)

    def train(self):
        features = [v for _, v in self.state.rows("features") if v.get('feature_version') == FEATURE_VERSION and v.get('at_risk',True)]
        quarantine = defaultdict(list)
        for namespace in ('detector_findings','behavior_findings'):
            for _, row in self.state.rows(namespace):
                if row.get('status') in {'active','shadow'} and not row.get('resolved_at'):
                    quarantine[row.get('member_id')].append(timestamp(row.get('at') or row.get('as_of')))
        features = [r for r in features if not any(at is None or timestamp(r['as_of']) >= at
                    for at in quarantine[r['member_id']])]
        # Reconcile household links across every snapshot before splitting.
        parents = {}
        def find(value):
            parents.setdefault(value, value)
            if parents[value] != value:
                parents[value] = find(parents[value])
            return parents[value]
        for row in features:
            left, right = find(row['member_id']), find(row['group'])
            parents[max(left, right)] = min(left, right)
        features = [{**row, 'group': find(row['member_id'])} for row in features]
        outcomes = defaultdict(lambda: defaultdict(list))
        for _, row in self.state.rows("outcomes"):
            if row.get('confidence') == 'verified':
                outcomes[row["member_id"]][row["outcome"]].append(row["at"])
        start = self.state.get("health", "bootstrap", {}).get("completed_at")
        gaps = [(v["start"], v.get("end")) for _, v in self.state.rows("gaps")]
        now = stamp()
        for outcome in OUTCOMES:
            for horizon in (30, 90):
                # Predict first recorded outcomes. Post-outcome observations are
                # outside the risk set rather than convenient negative examples.
                data = [{**r, "label": label_at(outcomes[r["member_id"]][outcome], r["as_of"], horizon, now, start, gaps)} for r in features
                        if not any(timestamp(t) <= timestamp(r['as_of']) for t in outcomes[r['member_id']][outcome])]
                candidate = train_predictor(data, outcome, horizon)
                candidate.update(outcome=outcome, horizon=horizon, feature_version=FEATURE_VERSION)
                candidate['version'] = fingerprint([candidate, [(r['member_id'],r['as_of'],r['label']) for r in data]])
                key = f"{outcome}:{horizon}"
                generation = key+':'+candidate['version']
                candidate['created_at'] = now
                with self.state.atomic():
                    if not self.state.get('model_generations',generation):
                        self.state.put('model_generations',generation,candidate)
                    previous = self.state.get('models',key,{})
                    if candidate['status'] == 'validated':
                        model = candidate
                    elif previous.get('status') == 'validated' and previous.get('feature_version') == FEATURE_VERSION and previous.get('features') == list(FEATURES):
                        model = previous
                    else:
                        model = candidate
                    self.state.put('models',key,model)
                    self.state.put('model_evaluations',key,{'candidate_version':candidate['version'],
                        'candidate_status':candidate['status'],'active_version':model['version'],'evaluated_at':now})
                    for metric_key, _ in list(self.state.rows('model_metrics')):
                        if metric_key.startswith(key+':'):
                            self.state.delete('model_metrics',metric_key)
                    for name, value in model.get('metrics',{}).items():
                        self.state.put('model_metrics',key+':'+name,{'outcome':outcome,'model_version':model['version'],
                            'metric':name,'value':value,'sample_size':model['sample_size'],'as_of':model.get('created_at',now)})
        with self.state.atomic():
            for metric_key, row in list(self.state.rows('report_metrics')):
                if row.get('dimension','').startswith('validated_association_'):
                    self.state.delete('report_metrics',metric_key)
            for row in aggregate_rows(self.model_reports(now),self.config.minimum_cohort):
                self.state.put('report_metrics',fingerprint([row['report'],row['dimension'],row['bucket']]),row)
        for member, score in isolation_scores(features, state=self.state).items():
            if score < 0:
                self.state.put("behavior_findings", "isolation:" + member + ":" + now[:10],
                    {"member_id": member, "as_of": now, "score": score, "status": "shadow", "rule": "isolation_forest"})
