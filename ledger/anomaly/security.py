"""Deterministic security review signals. No detector changes source accounts."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from .privacy import timestamp
from .schema import fingerprint
from .state import stamp
from .security_projection import similar, COLLECTIONS
from .security_evidence import covered, effective, enabled, mail_counts, reset_outcomes
from .security_learning import FEATURES, SecurityDetectors, load_rules


def hour(value):
    return value.replace(minute=0, second=0, microsecond=0)


def grants(row):
    return row['kind'] in {'scope_grant', 'role_change'} and row.get('grant') is True


def promotion(row):
    return row['kind'] == 'role_change' and row.get('grant') is True and row.get('after_role') in {'board_member', 'admin'}


class SecurityEngine:
    def __init__(self, state, config):
        self.state, self.config = state, config
        self.rules = load_rules(getattr(config, 'security_rules_path', None) if enabled(config) else None)
        self.revision = fingerprint(self.rules)
        self.detectors = SecurityDetectors(state, config, self.rules)
        if enabled(config):
            self.detectors.install()

    def _signal(self, rule, rows, now, expected, *, entity='global', severity='warning', minutes=60, unique=False, **extra):
        rows = sorted({r['id']: r for r in rows}.values(), key=lambda r: (r['at'], r['id']))
        if not rows:
            return None
        anchor = [r['id'] for r in rows] if unique else int(now.timestamp()) // (minutes * 60)
        key = fingerprint([self.revision, rule, entity, anchor])
        evidence = {r['id']: r for r in rows}
        for row in rows:
            for reference in row.get('audit_refs', []):
                audit = self.state.security_get_event(reference)
                if audit:
                    evidence[reference] = audit
        return {'id': key, 'rule': rule, 'revision': self.revision, 'severity': severity, 'entity': entity,
            'observed': len(rows), 'expected': expected, 'period_start': (now - timedelta(minutes=minutes)).isoformat(),
            'period_end': now.isoformat(), 'evaluated_at': now.isoformat(), 'evidence': sorted(evidence),
            'evidence_hashes': {r['id']: fingerprint(self.state.security_get_event(r['id'])) for r in evidence.values()},
            'evidence_snapshot': [{k:v for k,v in r.items() if k in {'id', 'kind', 'at', 'member_id', 'actor_id',
                'fields', 'before_role', 'after_role', 'scope_count', 'action', 'confidence', 'origin'}} for r in evidence.values()],
            'members': sorted({r['member_id'] for r in rows if r.get('member_id')}),
            'actors': sorted({r['actor_id'] for r in rows if r.get('actor_id')}),
            'fields': sorted({f for r in rows for f in r.get('fields', [])}),
            'confidence': 'verified_audit' if all(r['origin'] == 'audit_logs' for r in rows) else 'observed_with_coverage_limits',
            'historical': all(r['historical'] for r in rows), 'unique': unique, **extra}

    def detect(self, now, rows=None):
        rows = self.context(now) if rows is None else rows
        rows = [r for r in rows if timestamp(r['at']) <= now]
        signals = []
        for rule in self.rules['rules']:
            candidates = [r for r in rows if r['kind'] == rule['kind'] and
                          timestamp(r['at']) >= now - timedelta(minutes=rule['window_minutes'])]
            groups = defaultdict(list)
            for row in candidates:
                groups[row.get('member_id') if rule['group'] == 'member' else 'global'].append(row)
            for entity, group in groups.items():
                if entity and len(group) >= rule['count'] and len({r['member_id'] for r in group if r.get('member_id')}) >= rule['members']:
                    signals.append(self._signal(rule['id'], group, now, rule['count'], entity=entity, minutes=rule['window_minutes']))
        recent = [r for r in rows if timestamp(r['at']) >= now - timedelta(minutes=self.rules['email']['window_minutes']) and
                  r['kind'] in {'signup', 'account_created'} and r.get('email')]
        # Each member is a single account even when its audit arrives after insert.
        recent = list({r['member_id']: r for r in sorted(recent, key=lambda r: r['kind'] == 'signup')}.values())
        limit = self.rules['email']['candidate_limit']
        truncated = len(recent) > limit
        recent = sorted(recent, key=lambda r: (r['at'], r['id']))[-limit:]
        families, inverted, representatives = defaultdict(list), defaultdict(set), []
        comparisons = 0
        for row in recent:
            features = row['email']
            families[features['family']].append(row)
            keys = ['local:' + features['local']] + features['trigrams']
            candidates = sorted(set().union(*(inverted[k] for k in keys)))
            match = None
            for index in candidates:
                comparisons += 1
                if comparisons > 100000:
                    truncated = True
                    break
                if similar(features, representatives[index][0]['email']):
                    match = index
                    break
            if match is None:
                match = len(representatives)
                representatives.append([row])
                for key in keys:
                    inverted[key].add(match)
            else:
                representatives[match].append(row)
        for family, group in families.items():
            if len(group) >= self.rules['email']['alias_members'] and any(r['email']['plus'] for r in group):
                signals.append(self._signal('signup_alias_family', group, now, self.rules['email']['alias_members'],
                                            entity=family, minutes=self.rules['email']['window_minutes']))
        for group in representatives:
            if len(group) >= self.rules['email']['similar_members']:
                signals.append(self._signal('signup_similar', group, now, self.rules['email']['similar_members'],
                    entity=group[0]['email']['local'], minutes=self.rules['email']['window_minutes'],
                    cross_domain=len({r['email']['domain'] for r in group}) > 1))
        self.state.put('health', 'security_similarity', {'status': 'bounded' if truncated else 'ready', 'updated_at': stamp()})
        mail = [r for r in rows if timestamp(r['at']) >= now - timedelta(minutes=self.rules['bounce']['window_minutes'])]
        bounced, outcomes, unknown = mail_counts(mail)
        if (len(bounced) >= self.rules['bounce']['count'] and len(outcomes) >= self.rules['bounce']['denominator'] and
                len(bounced) / len(outcomes) >= self.rules['bounce']['fraction']):
            signals.append(self._signal('bounce_burst', outcomes, now, self.rules['bounce']['fraction'],
                minutes=self.rules['bounce']['window_minutes'], observed=len(bounced), denominator=len(outcomes),
                fraction=len(bounced) / len(outcomes), denominator_uncertain=bool(unknown), unknown_outcomes=unknown))
        increases = list({r['source_key']: r for r in rows if grants(r) and r.get('actor_id') and r.get('member_id')}.values())
        promoted = defaultdict(list)
        for row in rows:
            if promotion(row):
                promoted[row['member_id']].append(row)
        for second in increases:
            if second['actor_id'] == second['member_id']:
                continue
            for first in promoted.get(second['actor_id'], []):
                delta = timestamp(second['at']) - timestamp(first['at'])
                if first.get('actor_id') and timedelta(0) <= delta <= timedelta(hours=48):
                    signal = self._signal('privilege_propagation', [first, second], now, 'review every verified chain',
                        entity=second['actor_id'], severity='critical', minutes=48*60, unique=True)
                    # Role and scope markers from one audit are one grant operation.
                    signal['id'] = fingerprint([self.revision, 'privilege_propagation', first['source_key'], second['source_key']])
                    signals.append(signal)
        actors = defaultdict(list)
        for row in increases:
            if timestamp(row['at']) >= now - timedelta(minutes=self.rules['privileges']['bulk_window_minutes']):
                actors[row['actor_id']].append(row)
        for actor, group in actors.items():
            targets = {r['member_id'] for r in group}
            if len(targets) >= self.rules['privileges']['bulk_members']:
                signals.append(self._signal('bulk_privilege_grants', group, now, self.rules['privileges']['bulk_members'],
                    entity=actor, severity='critical', observed=len(targets), minutes=self.rules['privileges']['bulk_window_minutes']))
        for row in rows:
            if row['kind'] != 'mfa_change' or not row.get('actor_id'):
                continue
            role = row.get('role')
            role_evidence = []
            if not role:
                previous = self.state.security_events(until=(timestamp(row['at']) + timedelta(microseconds=1)).isoformat(),
                    kind='role_change', member=row['member_id'])
                if previous:
                    role = previous[-1]['after_role']
                    role_evidence = [previous[-1]]
                else:
                    member = self.state.get('security_members', row['member_id'], {})
                    if timestamp(member.get('at')) and timestamp(member['at']) <= timestamp(row['at']):
                        role = member.get('role')
            if role in {'admin', 'board_member', 'resource_manager'}:
                signal = self._signal('privileged_mfa_change', [row] + role_evidence, now, 'review every verified MFA change',
                    entity=row['member_id'], severity='critical', minutes=48*60, unique=True, privileged_role=role, observed=1)
                signal['id'] = fingerprint([self.revision, 'privileged_mfa_change', row['id']])
                signals.append(signal)
        return [s for s in signals if s and self.state.get('security_rule_status',
            fingerprint([self.revision, s['rule']]), {}).get('status') != 'disabled']

    def context(self, now, extra_minutes=0):
        minutes = max([r['window_minutes'] for r in self.rules['rules']] +
            [self.rules['email']['window_minutes'], self.rules['bounce']['window_minutes'], self.rules['privileges']['bulk_window_minutes']])
        rows = effective(self.state, now-timedelta(minutes=minutes+extra_minutes), now+timedelta(microseconds=1))
        # Long chain context needs only privilege evidence, not two days of every profile/mail event.
        for kind in ('role_change', 'scope_grant', 'mfa_change'):
            rows.extend(self.state.security_events((now-timedelta(hours=49, minutes=extra_minutes)).isoformat(),
                (now+timedelta(microseconds=1)).isoformat(), kind=kind))
        return sorted({r['id']:r for r in rows}.values(), key=lambda r: (r['at'], r['id']))

    def save(self, signal, *, backfill=False, active=True):
        old = self.state.get('security_incidents', signal['id'], {})
        if old.get('status') in {'resolved', 'retracted'}:
            return
        control = self.state.get('security_control', 'monitor', {})
        started = timestamp(control.get('enabled_at'))
        latest = max(timestamp(self.state.security_get_event(e)['at']) for e in signal['evidence'])
        historical = old.get('historical', backfill or signal['historical'] or not started or latest < started)
        signal.update(status=old.get('status', 'open' if active else 'shadow'),
            created_at=old.get('created_at', stamp()), historical=historical,
            alertable=active and not historical and self.config.security_alert_mode == 'active' and not old.get('mode_shadow', False),
            mode_shadow=old.get('mode_shadow', self.config.security_alert_mode == 'shadow'),
            learned=signal['rule'].startswith('learned:'))
        with self.state.atomic():
            self.state.put('security_incidents', signal['id'], signal)
            for evidence in signal['evidence']:
                row = self.state.security_get_event(evidence)
                self.state.put('security_excluded_hours', hour(timestamp(row['at'])).isoformat(), {'incident': signal['id']})

    def retract(self):
        current_revision = self.state.get('security_meta', 'revision', {})
        previous_revision = self.state.get('security_control', 'retraction_revision')
        for key, row in self.state.rows('security_incidents'):
            if row['status'] not in {'open', 'shadow', 'resolved'}:
                continue
            changed = any(fingerprint(self.state.security_get_event(e)) != value for e, value in row['evidence_hashes'].items())
            if not changed and not (row['rule'] == 'privileged_mfa_change' and current_revision != previous_revision):
                continue
            supported = [] if row.get('learned') else self.detect(timestamp(row['evaluated_at']))
            replacement = next((r for r in supported if r['id'] == key), None)
            if replacement:
                if row['status'] == 'resolved':
                    # Refresh corrected facts without undoing the staff resolution
                    # or making a previously resolved incident alertable again.
                    self.state.put('security_incidents', key, {**row, **replacement,
                        'historical': row['historical'], 'status': 'resolved', 'alertable': False})
                else:
                    self.save(replacement, backfill=row['historical'])
            else:
                self.state.put('security_incidents', key, {**row, 'status': 'retracted', 'alertable': False,
                    'retracted_at': stamp(), 'reason': 'Source evidence was corrected or deleted'})
        self.state.put('security_control', 'retraction_revision', current_revision)

    def rollup(self, start):
        end = start + timedelta(hours=1)
        rows = effective(self.state, start, end)
        bounced, outcomes, unknown = mail_counts(rows)
        for kind in FEATURES:
            selected = outcomes if kind.startswith('bounce_') else list({r['source_key']:r for r in rows if grants(r)}.values()) if kind == 'privilege_grant' else [r for r in rows if r['kind'] == kind]
            counts = Counter(r['member_id'] for r in selected if r.get('member_id'))
            value = len(bounced) if kind == 'bounce_count' else len(bounced)/len(outcomes) if kind == 'bounce_fraction' and outcomes else 0 if kind == 'bounce_fraction' else len(selected)
            collection = 'mailtrap' if kind.startswith('bounce_') else 'audit_logs' if kind in {'signup', 'privilege_grant'} else 'members'
            complete = covered(self.state, collection, start, end)
            if kind == 'bounce_fraction':
                complete &= len(outcomes) >= self.rules['bounce']['denominator'] and not unknown
            self.state.security_put_rollup(start.isoformat(), kind, {'at': start.isoformat(), 'kind': kind,
                'value': value, 'members': sorted(counts), 'member_counts': dict(counts), 'complete': bool(complete),
                'denominator': len(outcomes) if kind.startswith('bounce_') else len(selected),
                'evidence': [r['id'] for r in selected]})

    def evaluate(self, now=None):
        if not enabled(self.config):
            return []
        now = timestamp(now) or datetime.now(timezone.utc)
        if self.state.pending(1):
            self.state.put('health', 'security', {'status': 'catching_up', 'updated_at': stamp()})
            return []
        self.retract()
        self.detectors.corrections()
        signals = self.detect(now)
        for signal in signals:
            self.save(signal)
        # Incremental source corrections and historical backfill, bounded per evaluation.
        work_remaining = 50
        for bucket, dirty in list(self.state.rows('security_dirty'))[:20]:
            start = timestamp(bucket)
            self.rollup(start)
            if start <= now:
                # A correction can change a later window, including a grant made
                # up to 48 hours after a corrected promotion. Replay recent hours
                # too: detect(now) no longer sees their short burst windows.
                horizon = max([r['window_minutes'] for r in self.rules['rules']] +
                    [self.rules['email']['window_minutes'], self.rules['bounce']['window_minutes'],
                     self.rules['privileges']['bulk_window_minutes'], self.rules['privileges']['chain_hours'] * 60])
                end = min(now, start + timedelta(hours=1, minutes=horizon))
                rows = self.state.security_events(start.isoformat(), (end+timedelta(microseconds=1)).isoformat())
                cursor = self.state.get('security_backfill_cursor', bucket, {})
                after = cursor.get('at', '') if cursor.get('revision') == dirty['revision'] else ''
                times = sorted({r['at'] for r in rows if r['at'] > after})
                batch = times[:work_remaining]
                for at in batch:
                    for signal in self.detect(timestamp(at)):
                        self.save(signal, backfill=True)
                work_remaining -= len(batch)
                if len(times) > len(batch):
                    if batch:
                        self.state.put('security_backfill_cursor', bucket, {'at': batch[-1], 'revision': dirty['revision']})
                    break
            else:
                continue
            with self.state.atomic():
                if self.state.get('security_dirty', bucket) == dirty:
                    self.state.delete('security_dirty', bucket)
                    self.state.delete('security_backfill_cursor', bucket)
        # Materialize quiet complete hours, including after restart, without inventing gap zeros.
        previous = self.state.get('security_control', 'rollup_clock', {})
        cursor = timestamp(previous.get('at')) or hour(now) - timedelta(hours=1)
        for _ in range(168):
            if cursor >= hour(now):
                break
            self.rollup(cursor)
            cursor += timedelta(hours=1)
        self.state.put('security_control', 'rollup_clock', {'at': cursor.isoformat()})
        last = self.state.get('security_control', 'learned_clock', {})
        if last.get('at') != hour(now).isoformat():
            for finding in self.detectors.evaluate(hour(now)):
                rows = effective(self.state, timestamp(finding['at']), timestamp(finding['at']) + timedelta(hours=1))
                selected = [r for r in rows if (r['kind'] == finding['feature'] or finding['feature'].startswith('bounce_') and r['kind'] == 'mail_outcome' or finding['feature'] == 'privilege_grant' and grants(r)) and
                            (finding['entity'] == 'global' or r.get('member_id') == finding['entity'])]
                signal = self._signal(finding['rule'], selected, timestamp(finding['at']) + timedelta(hours=1), finding['expected'],
                    entity=finding['entity'], observed=finding['value'])
                if signal:
                    signal['revision'] = finding['revision']
                    self.save(signal, active=finding['active'])
            self.state.put('security_control', 'learned_clock', {'at': hour(now).isoformat()})
        published = timestamp(self.state.get('security_control', 'report_clock', {}).get('at'))
        if not published or now-published >= timedelta(minutes=5):
            self.publish(now)
            self.state.put('security_control', 'report_clock', {'at': now.isoformat()})
        self.state.put('health', 'security', {'status': 'ready', 'updated_at': stamp(), 'rule_revision': self.revision,
            'backfill_pending': any(self.state.rows('security_dirty'))})
        return signals

    def aggregate(self, now=None):
        """Only whole, sufficiently large sections; no residual/complementary totals."""
        now = timestamp(now) or datetime.now(timezone.utc)
        rows = effective(self.state, now - timedelta(days=7), now)
        minimum = max(5, self.config.minimum_cohort)
        sections = {'account_changes': {'profile_change', 'password_change'}, 'reset_activity': {'reset_request'},
            'registrations': {'signup', 'account_created'}, 'bounce_outcomes': {'mail_outcome'},
            'privilege_changes': {'role_change', 'scope_grant', 'mfa_change'}}
        output = []
        for section, kinds in sections.items():
            categories = kinds
            selected = [r for r in rows if r['kind'] in kinds]
            if section == 'bounce_outcomes':
                bounced, messages, unknown = mail_counts(selected)
                bounce_ids = {r['id'] for r in bounced}
                selected = [{**r, 'kind': 'bounced_messages' if r['id'] in bounce_ids else 'delivered_messages'} for r in messages]
                categories = {'bounced_messages', 'delivered_messages'}
            members = {r['member_id'] for r in selected if r.get('member_id')}
            if len(members) < minimum:
                continue
            counts = Counter(r['kind'] for r in selected)
            # Absent categories are zero-sized cells, so suppress their complements too.
            if any(len({r['member_id'] for r in selected if r['kind'] == k and r.get('member_id')}) < minimum for k in categories):
                continue
            for kind, value in counts.items():
                sample = len({r['member_id'] for r in selected if r['kind'] == kind and r.get('member_id')})
                output.append({'report': section, 'cohort': kind, 'sample_size': sample, 'value': value,
                               'period_start': (now-timedelta(days=7)).isoformat(), 'period_end': now.isoformat()})
        resets = reset_outcomes(self.state, rows, now)
        reset_groups = defaultdict(list)
        for reset in resets:
            reset_groups[reset['outcome']].append(reset)
        reset_categories = {'observed_completion', 'possible_completion', 'pending',
                            'no_observed_completion', 'incomplete_coverage'}
        if all(len({r['member_id'] for r in reset_groups.get(outcome, [])}) >= minimum for outcome in reset_categories):
            for outcome, group in reset_groups.items():
                output.append({'report': 'reset_completions', 'cohort': outcome, 'sample_size': len({r['member_id'] for r in group}), 'value': len(group)})
        clusters = [r for _, r in self.state.rows('security_incidents') if r['rule'] in {'signup_alias_family', 'signup_similar'} and
                    r['status'] != 'retracted' and timestamp(r['period_end']) >= now-timedelta(days=7)]
        members = {m for r in clusters for m in r['members']}
        if len(members) >= minimum:
            output.append({'report': 'email_similarity', 'cohort': 'review_clusters', 'sample_size': len(members),
                'value': len({tuple(sorted(r['evidence'])) for r in clusters})})
        # Coverage is a service property, but report it only alongside an eligible monitored population.
        population = {r['member_id'] for r in rows if r.get('member_id')}
        if len(population) >= minimum:
            for collection in sorted(COLLECTIONS):
                complete = sum(covered(self.state, collection, hour(now)-timedelta(hours=i+1), hour(now)-timedelta(hours=i)) for i in range(168))
                output.append({'report': 'monitoring_coverage', 'cohort': collection + '_complete_hours_of_168',
                    'sample_size': len(population), 'value': complete})
        return output

    def publish(self, now):
        metrics = self.aggregate(now)
        with self.state.atomic():
            for key, _ in self.state.rows('security_report_metrics'):
                self.state.delete('security_report_metrics', key)
            for row in metrics:
                row.setdefault('period_start', (now-timedelta(days=7)).isoformat())
                row.setdefault('period_end', now.isoformat())
                self.state.put('security_report_metrics', fingerprint([row['report'], row['cohort']]), row)

    def resolve(self, incident_id, actor):
        row = self.state.get('security_incidents', incident_id)
        if not row:
            raise ValueError('Unknown security incident ID')
        if row['status'] not in {'open', 'shadow'}:
            raise ValueError('Only open or shadow security incidents can be resolved')
        self.state.put('security_incidents', incident_id, {**row, 'status': 'resolved', 'resolved_by': actor,
            'resolved_at': stamp(), 'alertable': False})
