"""Versioned security rules and human-approved chronological rate models."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

from .privacy import timestamp
from .schema import fingerprint
from .security_projection import VERSION
from .state import stamp

FEATURES = {'profile_change': 5, 'password_change': 3, 'reset_request': 5, 'signup': 3,
            'bounce_count': 5, 'bounce_fraction': .2, 'privilege_grant': 3}
SPEC_KEYS = {'feature', 'scope', 'floor', 'mad_threshold', 'quantile', 'reference', 'confounders', 'falsification'}


def load_rules(path=None):
    default = json.loads(Path(__file__).with_name('security_rules.json').read_text(encoding='utf-8'))
    value = json.loads(Path(path).read_text(encoding='utf-8')) if path else default
    if not isinstance(value, dict) or set(value) != set(default) or value['version'] != 1:
        raise ValueError('Unsupported security rule document')
    if not isinstance(value['rules'], list) or len(value['rules']) != len(default['rules']):
        raise ValueError('Security burst rules must cover the reviewed catalog')
    for rule, original in zip(value['rules'], default['rules']):
        if not isinstance(rule, dict) or set(rule) != set(original):
            raise ValueError('Unknown security rule fields')
        if any(rule[k] != original[k] for k in ('id', 'kind', 'group')):
            raise ValueError('Security rules cannot redefine evidence or group semantics')
        if any(type(rule[k]) is not int or not 1 <= rule[k] <= 100000 for k in ('count', 'members', 'window_minutes')) or rule['window_minutes'] > 1440:
            raise ValueError('Invalid security burst limits')
    for section in ('email', 'bounce', 'privileges', 'learning'):
        if not isinstance(value[section], dict) or set(value[section]) != set(default[section]):
            raise ValueError('Unknown security configuration fields')
        for key, original in default[section].items():
            item = value[section][key]
            if type(item) is not type(original) or not math.isfinite(item) or not 0 < item <= 100000:
                raise ValueError('Invalid security configuration value')
    if not 0 < value['bounce']['fraction'] <= 1 or not .9 <= value['learning']['quantile'] < 1:
        raise ValueError('Invalid security fraction or quantile')
    learning = value['learning']
    if (not 8 <= learning['minimum_comparable'] <= 52 or not learning['minimum_comparable'] <= learning['lookback_weeks'] <= 52 or
            learning['minimum_shadow_days'] < 7 or learning['minimum_shadow_windows'] < 50 or learning['maximum_alert_rate'] > .05 or
            value['privileges']['chain_hours'] != 48 or value['email']['candidate_limit'] > 10000):
        raise ValueError('Security evidence gates cannot be weakened')
    return value


def validate_spec(spec):
    if not isinstance(spec, dict) or set(spec) != SPEC_KEYS or not isinstance(spec.get('feature'), str) or spec['feature'] not in FEATURES:
        raise ValueError('Unsupported security detector specification')
    if not isinstance(spec['scope'], str) or spec['scope'] not in {'global', 'member'} or spec['scope'] == 'member' and spec['feature'] not in {'profile_change', 'password_change', 'reset_request'}:
        raise ValueError('Unsupported security detector scope')
    for name in ('floor', 'mad_threshold', 'quantile'):
        if type(spec[name]) not in (int, float) or not math.isfinite(spec[name]):
            raise ValueError('Security thresholds must be finite')
    if not FEATURES[spec['feature']] <= spec['floor'] <= 100000 or not 3 <= spec['mad_threshold'] <= 20 or not .9 <= spec['quantile'] < 1:
        raise ValueError('Security detector thresholds are out of bounds')
    for key in ('reference', 'confounders', 'falsification'):
        if not isinstance(spec[key], str) or not 3 <= len(spec[key]) <= 500 or any(t in spec[key] for t in ('<', '>', 'http', '@', '```')):
            raise ValueError('Use bounded plain-text security detector descriptions')
    return spec


def threshold(spec, values):
    ordered = sorted(values)
    center = median(ordered)
    mad = median(abs(v-center) for v in ordered)
    quantile = ordered[math.ceil(spec['quantile'] * len(ordered))-1]
    return max(spec['floor'], center + spec['mad_threshold'] * 1.4826 * mad, quantile)


class SecurityDetectors:
    def __init__(self, state, config, rules):
        self.state, self.config, self.rules = state, config, rules
        self.zone = ZoneInfo(config.timezone)

    def revision(self, spec):
        generation = self.state.get('security_control', 'learning_generation', {'revision': 0})['revision']
        return fingerprint([VERSION, spec, self.rules['learning'], generation])

    def corrections(self):
        pending = self.state.get('security_control', 'learning_correction')
        if not pending:
            return
        with self.state.atomic():
            self.state.put('security_control', 'learning_generation', pending)
            for key, detector in self.state.rows('security_detectors'):
                if detector['status'] in {'active', 'shadow'}:
                    self.state.put('security_detectors', key, {**detector, 'status': 'superseded',
                        'reason': 'Historical evidence corrected; fresh shadow evaluation and approval required'})
            for key, incident in self.state.rows('security_incidents'):
                if incident.get('learned') and incident['status'] in {'open', 'shadow'}:
                    self.state.put('security_incidents', key, {**incident, 'status': 'retracted', 'alertable': False,
                        'reason': 'Baseline evidence corrected', 'retracted_at': stamp()})
            self.install()
            self.state.delete('security_control', 'learning_correction')

    def install(self):
        for feature, floor in FEATURES.items():
            for scope in (('global', 'member') if feature in {'profile_change', 'password_change', 'reset_request'} else ('global',)):
                self.propose({'feature': feature, 'scope': scope, 'floor': floor,
                    'mad_threshold': self.rules['learning']['mad_threshold'], 'quantile': self.rules['learning']['quantile'],
                    'reference': 'Point, contextual and collective anomalies; median MAD and empirical quantiles',
                    'confounders': 'Incomplete history, onboarding, maintenance and legitimate campaigns',
                    'falsification': 'Reject when chronological evaluation exceeds the alert budget'}, 'packaged', backtest=False)

    def propose(self, spec, actor, *, backtest=True):
        spec = validate_spec(spec)
        revision = self.revision(spec)
        if not self.state.get('security_detectors', revision):
            self.state.put('security_detectors', revision, {'revision': revision, 'spec': spec, 'status': 'shadow',
                'created_at': stamp(), 'proposed_by': actor, 'evaluation': self.backtest(spec) if backtest else {'passed': False}})
        return revision

    def _samples(self, spec, now, freeze_before=None):
        start = now - timedelta(weeks=self.rules['learning']['lookback_weeks'])
        rows = [r for r in self.state.security_rollups(start.isoformat(), now.isoformat()) if r['kind'] == spec['feature']]
        history = defaultdict(list)
        comparable = defaultdict(dict)
        excluded = {key for key, _ in self.state.rows('security_excluded_hours')}
        members = dict(self.state.rows('security_members')) if spec['scope'] == 'member' else {}
        for row in sorted(rows, key=lambda r: r['at']):
            if not row['complete']:
                continue
            local = timestamp(row['at']).astimezone(self.zone)
            # A repeated DST hour is a single comparable calendar observation.
            values = {'global': row['value']} if spec['scope'] == 'global' else row.get('member_counts', {})
            for entity, value in values.items():
                key = entity, local.weekday(), local.hour
                previous = [v for day, v in history[key] if day != local.date()]
                if spec['scope'] == 'member':
                    # Quiet periods are zeros only for an existing member under complete monitoring.
                    member = members.get(entity, {})
                    first = timestamp(member.get('first_seen'))
                    previous = [r.get('member_counts', {}).get(entity, 0) for day, r in comparable[(local.weekday(), local.hour)].items()
                        if day != local.date() and first and timestamp(r['at']) >= first]
                if len(previous) >= self.rules['learning']['minimum_comparable']:
                    yield row, entity, value, previous
                if row['at'] not in excluded and (freeze_before is None or timestamp(row['at']) < freeze_before):
                    # A repeated DST hour is one calendar observation, taking the conservative maximum.
                    old = dict(history[key])
                    old[local.date()] = max(value, old.get(local.date(), 0))
                    history[key] = list(old.items())
            if row['at'] not in excluded and (freeze_before is None or timestamp(row['at']) < freeze_before):
                comparable[(local.weekday(), local.hour)][local.date()] = row

    def backtest(self, spec, now=None):
        now = timestamp(now) or datetime.now(timezone.utc)
        samples = list(self._samples(spec, now))
        # Last quarter is a chronological holdout, never part of threshold fitting.
        split = len(samples) * 3 // 4
        cutoff = timestamp(samples[split][0]['at']) if samples else now
        training = [s for s in samples if timestamp(s[0]['at']) < cutoff]
        holdout = [s for s in self._samples(spec, now, freeze_before=cutoff) if timestamp(s[0]['at']) >= cutoff]
        hits = sum(value > threshold(spec, prior) for _, _, value, prior in holdout)
        rate = hits / len(holdout) if holdout else 0
        return {'passed': len(training) >= 50 and len(holdout) >= 20 and rate <= self.rules['learning']['maximum_alert_rate'],
                'training_windows': len(training), 'holdout_windows': len(holdout), 'hits': hits, 'alert_rate': rate,
                'evaluated_at': now.isoformat(), 'evidence_revision': self.state.get('security_meta', 'revision', {'value': 0})['value']}

    def approve(self, revision, actor, now=None):
        now = timestamp(now) or datetime.now(timezone.utc)
        with self.state.atomic():
            row = self.state.get('security_detectors', revision)
            if not row or revision != self.revision(row['spec']):
                raise ValueError('Unknown or changed security detector revision')
            evaluation = self.backtest(row['spec'], now)
            shadow = self.state.get('security_shadow', revision, {'windows': 0, 'hits': 0})
            first = timestamp(shadow.get('first_at'))
            if (not evaluation['passed'] or not first or now-first < timedelta(days=self.rules['learning']['minimum_shadow_days']) or
                    shadow['windows'] < self.rules['learning']['minimum_shadow_windows'] or
                    shadow['hits'] / max(shadow['windows'], 1) > self.rules['learning']['maximum_alert_rate']):
                raise ValueError('Security detector needs a passing holdout and seven days / 50 complete shadow windows within budget')
            row.update(status='active', approved_by=actor, approved_at=now.isoformat(), evaluation=evaluation)
            self.state.put('security_detectors', revision, row)

    def disable(self, revision, actor):
        row = self.state.get('security_detectors', revision)
        if not row:
            raise ValueError('Unknown security detector revision')
        self.state.put('security_detectors', revision, {**row, 'status': 'disabled', 'disabled_by': actor})

    def evaluate(self, now):
        findings = []
        for revision, detector in self.state.rows('security_detectors'):
            if detector['status'] not in {'active', 'shadow'} or revision != self.revision(detector['spec']):
                continue
            spec = detector['spec']
            for row, entity, value, prior in self._samples(spec, now):
                if timestamp(row['at']) < max(now - timedelta(hours=2), timestamp(detector['created_at'])):
                    continue
                key = fingerprint([revision, row['at'], entity])
                if self.state.get('security_learning_receipts', key):
                    continue
                baseline_key = revision + ':' + entity
                stable = self.state.get('security_stable_baselines', baseline_key)
                if not stable:
                    stable = {'values': prior, 'at': row['at'], 'revision': revision}
                    self.state.put('security_stable_baselines', baseline_key, stable)
                expected = min(threshold(spec, prior), threshold(spec, stable['values']))
                hit = value > expected
                with self.state.atomic():
                    stats = self.state.get('security_shadow', revision, {'windows': 0, 'hits': 0})
                    stats['windows'] += 1
                    stats['hits'] += int(hit)
                    stats.setdefault('first_at', now.isoformat())
                    stats['last_at'] = now.isoformat()
                    self.state.put('security_shadow', revision, stats)
                    self.state.put('security_learning_receipts', key, {'at': row['at'], 'hit': hit, 'revision': revision})
                if hit:
                    findings.append({'rule': 'learned:' + revision, 'revision': revision, 'at': row['at'],
                        'value': value, 'expected': expected, 'entity': entity, 'feature': spec['feature'], 'active': detector['status'] == 'active'})
        return findings
