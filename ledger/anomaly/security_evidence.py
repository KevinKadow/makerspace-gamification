"""Transactional security evidence, coverage and canonical event counting."""
from collections import defaultdict
from datetime import timedelta

from .privacy import timestamp
from .schema import fingerprint
from .state import stamp
from .security_projection import COLLECTIONS, BOUNCES


def enabled(config):
    return getattr(config, 'security_enabled', False)


def configure(state, config):
    with state.atomic():
        prior = state.get('security_control', 'monitor', {})
        active = enabled(config)
        state.put('security_control', 'monitor', {'enabled': active,
            'enabled_at': prior.get('enabled_at') if active and prior.get('enabled') else stamp()})


def record(state, capture, entity, config, *, stale=False):
    """Called within ingestion's transaction, before acknowledging the capture."""
    data, collection = capture['data'], capture['collection']
    projection = data.get('security')
    if not enabled(config) or collection not in COLLECTIONS or projection is None:
        return
    stream = collection == 'members'
    if stale and not stream:
        return
    source = ('stream:' + capture['id']) if stream else collection + ':' + entity
    if stream and not stale and not data.get('missing_document'):
        old = state.get('security_members', entity, {})
        if data['operation'] == 'delete':
            state.delete('security_members', entity)
        else:
            state.put('security_members', entity, {'role': projection.get('role'),
                'first_seen': old.get('first_seen', data['observed_at']),
                'at': old.get('at', data['observed_at']) if old.get('role') == projection.get('role') else data['observed_at']})
    # Inserts/updates carry precise stream markers; snapshots never invent changes.
    markers = projection.get('markers', []) if data['operation'] != 'delete' else []
    when = projection.get('at') or data.get('occurred_at')
    if not when:
        markers = []
    events = []
    for marker in markers:
        kind = marker['kind']
        events.append({**marker, 'id': fingerprint([source, kind]), 'source_key': source,
            'at': when, 'member_id': entity if stream else projection.get('member_id'),
            'actor_id': projection.get('actor_id'), 'origin': 'stream' if stream else collection,
            'historical': bool(data['historical'] or data['operation'] == 'snapshot' or data.get('reconciled')),
            'confidence': 'observed' if stream else 'dated_audit' if collection == 'audit_logs' else 'provider',
            'projection_version': projection['version']})
    # Empty member snapshots do not create receipts or delete real stream history.
    if events or not stream:
        previous = {r['id']: r for r in state.security_events(source=source)} if not stream else {}
        for row in events:
            old = previous.get(row['id'])
            if old and {k:v for k,v in old.items() if k != 'historical'} == {k:v for k,v in row.items() if k != 'historical'}:
                row['historical'] = old['historical']
        state.security_replace(source, events)


def covered(state, collection, start, end):
    start, end = timestamp(start), timestamp(end)
    intervals = sorted((timestamp(r['start']), timestamp(r['end']))
                       for _, r in state.rows('security_coverage', prefix=collection + ':') if r.get('end'))
    cursor = start
    for left, right in intervals:
        if left > cursor:
            break
        if right > cursor:
            cursor = right
        if cursor >= end:
            return True
    return False


def effective(state, start, end):
    """Prefer observed member operations; audit enrichments do not add operations."""
    start, end = timestamp(start), timestamp(end)
    rows = state.security_events((start - timedelta(minutes=2)).isoformat(), (end + timedelta(minutes=2)).isoformat())
    streams = defaultdict(list)
    for row in rows:
        if row['origin'] == 'stream':
            streams[(row['kind'], row.get('member_id'))].append(row)
    used, output, signups, creations, provider, enrichment = set(), [], {}, {}, set(), {}
    for row in rows:
        at = timestamp(row['at'])
        if not start <= at < end:
            continue
        kind, member = row['kind'], row.get('member_id')
        if row['origin'] == 'audit_logs' and kind in {'profile_change', 'password_change'}:
            candidates = [v for v in streams[(kind, member)] if v['id'] not in used and
                          abs((timestamp(v['at']) - at).total_seconds()) <= 120 and
                          (kind != 'profile_change' or v.get('fields', []) == row.get('fields', []))]
            if candidates:
                match = min(candidates, key=lambda v: abs((timestamp(v['at']) - at).total_seconds()))
                used.add(match['id'])
                enrichment[match['id']] = row
                continue
            if covered(state, 'members', at, at + timedelta(microseconds=1)):
                continue
        if kind == 'signup':
            signups.setdefault(member, row)
        elif kind == 'account_created':
            creations.setdefault(member, row)
        elif kind == 'mail_outcome':
            if not member and row.get('message_link'):
                linked = state.security_events(source='mailtrap_messages:' + row['message_link'])
                if len(linked) == 1:
                    row = {**row, 'member_id': linked[0].get('member_id')}
            key = row.get('provider_event') or row['id']
            if key not in provider:
                provider.add(key)
                output.append(row)
        else:
            output.append(row)
    output.extend(signups.values())
    # Registration evidence often follows the member insert across an hour boundary.
    for member, row in creations.items():
        if member not in signups and not any(v['kind'] == 'signup' for v in state.security_events(
                (timestamp(row['at']) - timedelta(minutes=2)).isoformat(),
                (timestamp(row['at']) + timedelta(minutes=2)).isoformat(), member=member)):
            output.append(row)
    output = [{**row, 'actor_id': enrichment[row['id']].get('actor_id'),
               'audit_refs': [enrichment[row['id']]['id']], 'confidence': 'correlated_audit'}
              if row['id'] in enrichment else row for row in output]
    return sorted(output, key=lambda r: (r['at'], r['id']))


def mail_counts(rows):
    messages, unknown = {}, 0
    links = defaultdict(set)
    for row in rows:
        if row['kind'] == 'mail_outcome' and row.get('message_link') and row.get('provider_message'):
            links[row['provider_message']].add(row['message_link'])
    for row in rows:
        if row['kind'] != 'mail_outcome' or row['outcome'] not in BOUNCES | {'delivery', 'delivered'}:
            continue
        key = row.get('message_link') or row.get('provider_message')
        known = links.get(row.get('provider_message'), set())
        if len(known) == 1:
            key = next(iter(known))
        elif len(known) > 1:
            unknown += 1
            continue
        if not key:
            unknown += 1
            continue
        old = messages.get(key)
        if old is None or row['outcome'] in BOUNCES:
            messages[key] = row
    bounced = [r for r in messages.values() if r['outcome'] in BOUNCES]
    return bounced, list(messages.values()), unknown


def reset_outcomes(state, rows, now):
    requests = sorted((r for r in rows if r['kind'] == 'reset_request'), key=lambda r: r['at'])
    results = []
    for index, request in enumerate(requests):
        at, member = timestamp(request['at']), request['member_id']
        deadline = at + timedelta(hours=24)
        # A later issuance supersedes the earlier token; a completion belongs to the latest request.
        next_request = next((timestamp(r['at']) for r in requests[index+1:] if r['member_id'] == member), deadline)
        until = min(deadline, next_request, now)
        changes = state.security_events(at.isoformat(), (until + timedelta(microseconds=1)).isoformat(),
                                         kind='password_change', member=member)
        observed = any(r.get('token_cleared') for r in changes)
        if observed:
            outcome = 'observed_completion'
        elif changes:
            outcome = 'possible_completion'
        elif not covered(state, 'members', at, until):
            outcome = 'incomplete_coverage'
        elif now < deadline and next_request >= deadline:
            outcome = 'pending'
        else:
            outcome = 'no_observed_completion'
        onboarding = bool(state.security_events((at - timedelta(hours=24)).isoformat(),
            (at + timedelta(seconds=1)).isoformat(), kind='account_created', member=member))
        results.append({'request': request['id'], 'member_id': member, 'outcome': outcome, 'onboarding': onboarding})
    return results
