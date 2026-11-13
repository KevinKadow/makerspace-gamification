"""Paid coverage requires a paid renewal and a dated entitlement change."""
from collections import defaultdict
from datetime import datetime, timezone

from dateutil.relativedelta import relativedelta

from .privacy import timestamp, digest
from .schema import fingerprint


def state_at(observation, transitions, when):
    """Reconstruct observed state; a present-day value is not historic evidence."""
    instant = timestamp(when)
    first = timestamp((observation or {}).get('first_seen'))
    if not instant or not first or first > instant:
        return None
    value = dict(observation.get('values', {}))
    for change in sorted(transitions, key=lambda r: r['at'], reverse=True):
        at = timestamp(change.get('at'))
        if at and at > instant:
            if change.get('confidence') != 'observed':
                return None
            value = dict(change.get('before', {}))
    return value or None


def source_observations(state):
    """Keep unresolved source violations and unapproved contracts out of learning."""
    observations = dict(state.rows('observations'))
    active = state.get('schemas', 'active', {})
    pending = active.get('pending', {})
    secret = state.get('config', 'pseudonym_key', {}).get('value')
    findings = {(r.get('collection'), r.get('entity')) for _,r in state.rows('findings') if r.get('status') == 'open'}
    invalid = {}
    for key, row in observations.items():
        collection, entity = key.split(':', 1)
        if ('collections' in active and collection not in active['collections']) or collection in pending or (secret and (collection,digest(entity,secret)) in findings) or (
                not secret and any(c == collection for c,_ in findings)):
            invalid[key] = row
    return {k:v for k,v in observations.items() if k not in invalid}, invalid


def reconstruct_coverage(state, history_months=60, as_of=None):
    observations, invalid = source_observations(state)
    now = timestamp(as_of) or datetime.now(timezone.utc)
    cutoff = now - relativedelta(months=history_months)
    invoices, transitions = defaultdict(list), defaultdict(list)
    changes = []
    active = state.get('schemas', 'active', {})
    for key, change in state.rows('transitions'):
        if 'collections' in active and change.get('collection') not in active['collections']:
            continue
        transitions[change.get('collection'), change.get('entity')].append(change)
        if (change.get('confidence') == 'observed' and change.get('collection') in {'members', 'groups'}
                and change.get('collection') not in active.get('pending',{})
                and change.get('collection')+':'+str(change.get('entity')) not in invalid):
            field = 'expiry' if change['collection'] == 'groups' else 'expirationTime'
            before, after = change.get('before', {}), change.get('after', {})
            if before.get(field) != after.get(field):
                changes.append(('transition:' + key, change['collection'], change['entity'],
                                before.get(field), after.get(field), change.get('at')))
    for key, observation in observations.items():
        row = observation['values']
        if key.startswith('invoices:'):
            when = timestamp(row.get('settlement_processed_at') or row.get('settled_at'))
            quantity = row.get('quantity')
            if (row.get('resource_class') in {'member', 'household'} and row.get('operation') == 'renew='
                    and row.get('paid_positive') is True and row.get('refunded') is not True and when
                    and type(quantity) is int and 0 < quantity <= 120):
                resource = row.get('resource_id') or row.get('member_id')
                invoices[row['resource_class'], resource].append((key, row, when))
        elif key.startswith('audit_logs:') and row.get('resource_type') in {'Member', 'Group'}:
            collection = 'groups' if row['resource_type'] == 'Group' else 'members'
            pair = row.get('field_changes', {}).get('expiry' if collection == 'groups' else 'expirationTime')
            resource = row.get('resource_id') if collection == 'groups' else row.get('subject_id') or row.get('resource_id')
            if isinstance(pair, list) and len(pair) == 2 and resource:
                changes.append((key, collection, resource, pair[0], pair[1], row.get('created_at')))
    # Multiple audit/stream receipts of the same paid renewal produce one interval.
    candidates = {}
    for evidence_key, collection, resource, raw_before, raw_after, raw_when in changes:
        before, after, when = timestamp(raw_before), timestamp(raw_after), timestamp(raw_when)
        if not when or not after or (before and after <= before):
            continue
        resource_class = 'household' if collection == 'groups' else 'member'
        matches = []
        for invoice_key, invoice, paid_at in invoices[resource_class, resource]:
            start = max(before, paid_at) if before else paid_at
            expected = start + relativedelta(months=invoice['quantity'])
            if abs((paid_at - when).total_seconds()) <= 120 and abs((expected - after).total_seconds()) <= 120:
                matches.append((invoice_key, invoice, start))
        if len(matches) != 1:
            continue
        invoice_key, invoice, start = matches[0]
        recipients = [(resource, 'direct', after)] if resource_class == 'member' else []
        if resource_class == 'household':
            # The invoice links the paying account even when household history is absent.
            if invoice.get('member_id'):
                recipients.append((invoice['member_id'], 'household_payer', after))
            group = state_at(observations.get('groups:' + resource), transitions['groups', resource], when)
            if group and group.get('groupName'):
                for member_key, observation in observations.items():
                    if not member_key.startswith('members:'):
                        continue
                    member = member_key.split(':', 1)[1]
                    if member == invoice.get('member_id'):
                        continue
                    history = transitions['members', member]
                    snapshot = state_at(observation, history, start)
                    if not snapshot or snapshot.get('groupName') != group['groupName']:
                        continue
                    end = after
                    for change in sorted(history, key=lambda r: r['at']):
                        at = timestamp(change.get('at'))
                        if at and start < at < end and (change.get('confidence') != 'observed' or
                                change.get('after', {}).get('groupName') != group['groupName']):
                            end = at
                    if start < end:
                        recipients.append((member, 'household', end))
        for member, payer, end in recipients:
            key = fingerprint([member, invoice_key, start.isoformat(), end.isoformat()])
            existing = candidates.get(key)
            evidence = sorted(set((existing or {}).get('evidence', []) + [evidence_key, invoice_key]))
            candidates[key] = {'member_id': member, 'start': start.isoformat(), 'end': end.isoformat(),
                               'payer_type': payer, 'confidence': 'verified', 'evidence': evidence}
    # Detailed evidence is bounded, while an old verified qualification survives
    # event retention. Its invoice references remain available to invalidate a refund.
    from .analytics import sustained_membership
    paying = defaultdict(list)
    for row in candidates.values():
        if row['payer_type'] in {'direct', 'household_payer'}:
            paying[row['member_id']].append(row)
    qualifications = {}
    def evidence_value(ref):
        if ref.startswith('transition:'):
            return state.get('transitions',ref.split(':',1)[1])
        source = state.get('observations',ref)
        return source.get('values') if source else None
    for member, rows in paying.items():
        result = sustained_membership([(r['start'],r['end']) for r in rows],now)
        if result['qualifies']:
            refs = {e for r in rows for e in r['evidence']}
            qualifications[member] = {**result, 'invoice_evidence': sorted(e for e in refs if e.startswith('invoices:')),
                                      'proof': {e:fingerprint(evidence_value(e)) for e in refs},
                                      'verified_at': now.isoformat()}
    # A refund or corrected source observation invalidates its inferred coverage.
    with state.atomic():
        for member, prior in list(state.rows('coverage_qualifications')):
            refs = prior.get('invoice_evidence', [])
            # Retention alone never revokes proved history. A known correction or
            # refund does, including when other old receipts have been pruned.
            bad = any(state.get('observation_tombstones', ref) or
                      (state.get('observations', ref) and (ref not in observations or
                       observations[ref]['values'].get('refunded') or not observations[ref]['values'].get('paid_positive'))) for ref in refs)
            changed = any(evidence_value(ref) is not None and fingerprint(evidence_value(ref)) != revision
                          for ref,revision in prior.get('proof',{}).items())
            if bad or changed:
                state.delete('coverage_qualifications', member)
        for member, row in qualifications.items():
            state.put('coverage_qualifications', member, row)
        for key, _ in list(state.rows('coverage')):
            state.delete('coverage', key)
        for key, row in candidates.items():
            if timestamp(row['end']) >= cutoff:
                state.put('coverage', key, row)
    return sum(timestamp(r['end']) >= cutoff for r in candidates.values())
