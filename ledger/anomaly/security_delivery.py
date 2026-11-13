"""Durable, configured, reauthorized staff-only security DMs."""
from datetime import datetime, timedelta, timezone
from uuid import UUID

from ..domain import Denied
from .privacy import timestamp
from .schema import fingerprint
from .state import stamp
from .security_evidence import enabled
from .security_learning import load_rules


def render_incident(row):
    lines = [f"Security review — {row['severity']}: {row['rule']}",
        f"Observed: {row['observed']}; threshold / expected: {row['expected']}.",
        f"Period: {row['period_start']} to {row['period_end']}.",
        'Changed categories: ' + (', '.join(row.get('fields', [])) or row['rule']),
        'Confidence: ' + row['confidence'],
        'Member source IDs: ' + (', '.join(row.get('members', [])[:20]) or 'unattributed'),
        'Actor source IDs: ' + (', '.join(row.get('actors', [])[:20]) or 'unattributed'),
        'Rule revision: ' + row['revision'], 'Incident: ' + row['id']]
    if 'denominator' in row:
        lines.append(f"Distinct message outcomes: {row['denominator']}; denominator uncertainty: {row['denominator_uncertain']}.")
    lines.append('Review signal only; no account action was taken. Use /ledger-anomaly security incident ' + row['id'])
    return '\n'.join(lines)


class SecurityDelivery:
    def __init__(self, state, config, slack=None, access=None):
        self.state, self.config, self.slack, self.access = state, config, slack, access
        self.rule_revision = fingerprint(load_rules(getattr(config, 'security_rules_path', None) if enabled(config) else None))

    def allowed(self, incident):
        if incident.get('learned'):
            return self.state.get('security_detectors', incident['revision'], {}).get('status') == 'active' and not self.state.get('security_control', 'learning_correction')
        return incident['revision'] == self.rule_revision and self.state.get('security_rule_status',
            fingerprint([incident['revision'], incident['rule']]), {}).get('status') != 'disabled'

    def schedule(self, now):
        for incident_id, incident in self.state.rows('security_incidents'):
            if incident.get('status') != 'open' or not incident.get('alertable') or not self.allowed(incident):
                continue
            for user in self.config.security_dm_user_ids:
                key = fingerprint([incident_id, user])
                if not self.state.get('security_deliveries', key):
                    self.state.put('security_deliveries', key, {'id': key, 'incident_id': incident_id, 'user': user,
                        'status': 'pending', 'attempts': 0, 'available_at': now.isoformat(), 'created_at': stamp()})

    def destination(self, user):
        if user not in self.config.security_dm_user_ids:
            raise Denied('Security recipient is not a configured current staff member')
        if self.slack is None or self.access is None:
            # Missing dependencies are retryable; they do not revoke staff authority.
            raise RuntimeError('Security delivery is not configured')
        if not self.access.staff(user):
            raise Denied('Security recipient is not a configured current staff member')
        response = self.slack.conversations_open(users=user)
        channel = response.get('channel', {}).get('id')
        if response.get('ok') is False or self.access.authorize(user, channel) != 'staff_detail':
            raise Denied('Security delivery requires a verified private staff DM')
        return channel

    def cooled(self, user, incident, now):
        cooldown = self.state.get('security_cooldowns', fingerprint([user, incident['rule'], incident['entity']]), {})
        at = timestamp(cooldown.get('at'))
        return not incident['unique'] and at and at > now-timedelta(hours=24)

    def evidence_current(self, incident):
        current = self.state.get('security_incidents', incident['id'], {})
        return current.get('status') == 'open' and self.allowed(incident) and all(
            fingerprint(self.state.security_get_event(e)) == value for e,value in incident['evidence_hashes'].items())

    def step(self, now=None):
        if not enabled(self.config):
            return
        now = timestamp(now) or datetime.now(timezone.utc)
        authorized = 0
        for user in self.config.security_dm_user_ids:
            try:
                authorized += bool(self.slack is not None and self.access is not None and self.access.staff(user))
            except Exception:
                pass
        self.state.put('health', 'security_delivery', {'status': 'ready' if authorized else 'configuration_problem',
            'authorized_recipients': authorized, 'updated_at': stamp()})
        if self.config.security_alert_mode != 'active':
            return
        self.schedule(now)
        jobs = []
        for key, job in self.state.rows('security_deliveries'):
            if job['status'] != 'pending' or timestamp(job['available_at']) > now:
                continue
            incident = self.state.get('security_incidents', job['incident_id'], {})
            if incident.get('status') != 'open' or not incident.get('alertable') or not self.allowed(incident):
                self.state.put('security_deliveries', key, {**job, 'status': 'cancelled'})
            elif any(fingerprint(self.state.security_get_event(e)) != value for e,value in incident['evidence_hashes'].items()):
                continue  # Correction evaluation must finish before a queued alert is delivered.
            else:
                jobs.append((key, job, incident))
        jobs.sort(key=lambda item: (item[2]['severity'] != 'critical', item[1]['created_at'], item[0]))
        attempted = set()
        for key, job, incident in jobs:
            job = self.state.get('security_deliveries', key)
            if job['status'] != 'pending':
                continue
            user = job['user']
            if self.cooled(user, incident, now) and not job.get('message_id'):
                self.state.put('security_deliveries', key, {**job, 'status': 'cooldown', 'completed_at': now.isoformat()})
                continue
            selected = [(key, job, incident)]
            try:
                channel = self.destination(user)
                budget_key = user + ':' + now.strftime('%Y-%m-%dT%H')
                budget = self.state.get('security_budgets', budget_key, {'count': 0})
                message = self.state.get('security_messages', job.get('message_id', ''))
                if message:
                    selected = [(k, self.state.get('security_deliveries', k), i) for k,i in message['incidents'].items()]
                    message_key, summary, text = message['id'], message['summary'], message['text']
                else:
                    if budget['count'] >= 10:
                        continue
                    summary = incident['severity'] != 'critical' and budget['count'] >= 9
                    if summary:
                        selected = [(k, self.state.get('security_deliveries', k), i) for k,j,i in jobs
                            if j['user'] == user and i['severity'] != 'critical' and
                            self.state.get('security_deliveries', k, {}).get('status') == 'pending' and
                            not self.state.get('security_deliveries', k, {}).get('message_id') and not self.cooled(user,i,now)]
                    message_key = fingerprint(['overflow', budget_key, sorted(k for k,_,_ in selected)]) if summary else fingerprint([key, incident['evidence_hashes']])
                    text = ('Additional security rate incidents require review: ' + ', '.join(i['id'] for _, _, i in selected[:20]) +
                            '. Use /ledger-anomaly security incidents for all evidence.') if summary else render_incident(incident)
                    message = {'id': message_key, 'user': user, 'summary': summary, 'text': text,
                               'incidents': {k:i for k,_,i in selected}, 'created_at': stamp()}
                    with self.state.atomic():
                        self.state.put('security_messages', message_key, message)
                        for k,j,_ in selected:
                            self.state.put('security_deliveries', k, {**j, 'message_id': message_key})
                if message_key in attempted:
                    continue
                attempted.add(message_key)
                if self.access.authorize(user, channel) != 'staff_detail' or user not in self.config.security_dm_user_ids:
                    raise Denied('Security recipient authorization changed')
                if any(not self.evidence_current(i) for _,_,i in selected):
                    # Unbind surviving incidents; a corrected summary must receive a new identity.
                    for k,j,i in selected:
                        updated = dict(j)
                        updated.pop('message_id', None)
                        if self.state.get('security_incidents',i['id'],{}).get('status') != 'open':
                            updated['status'] = 'cancelled'
                        self.state.put('security_deliveries', k, updated)
                    continue
                # Reserve unique-message budget BEFORE I/O, including uncertain sends.
                attempt_key = fingerprint([message_key, budget_key])
                with self.state.atomic():
                    if not self.state.get('security_message_attempts', attempt_key):
                        budget = self.state.get('security_budgets', budget_key, {'count': 0})
                        if budget['count'] >= 10:
                            continue
                        self.state.put('security_budgets', budget_key, {'count': budget['count']+1})
                        self.state.put('security_message_attempts', attempt_key, {'at': now.isoformat()})
                response = self.slack.chat_postMessage(channel=channel, text=text, client_msg_id=str(UUID(message_key[:32])),
                    unfurl_links=False, unfurl_media=False)
                if response.get('ok') is False:
                    raise RuntimeError('Slack security delivery failed')
                with self.state.atomic():
                    for selected_key, selected_job, selected_incident in selected:
                        self.state.put('security_deliveries', selected_key, {**selected_job, 'status': 'summarized' if summary else 'sent',
                            'completed_at': now.isoformat(), 'message_id': message_key})
                        if not selected_incident['unique']:
                            self.state.put('security_cooldowns', fingerprint([user, selected_incident['rule'], selected_incident['entity']]), {'at': now.isoformat()})
            except Denied:
                for k,j,_ in selected:
                    self.state.put('security_deliveries', k, {**self.state.get('security_deliveries', k, j), 'status': 'cancelled', 'reason': 'Authorization unavailable'})
            except Exception as exc:
                for k,j,_ in selected:
                    attempts = j['attempts'] + 1
                    self.state.put('security_deliveries', k, {**self.state.get('security_deliveries', k, j), 'attempts': attempts, 'error': type(exc).__name__,
                        'available_at': (now+timedelta(seconds=min(300, 2**min(attempts, 8)))).isoformat()})
