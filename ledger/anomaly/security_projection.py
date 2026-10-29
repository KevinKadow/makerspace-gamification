"""Security evidence boundary. Secrets and raw profile values never leave this module."""
import re
import unicodedata

from .privacy import digest, identifier, timestamp

VERSION = 1
COLLECTIONS = frozenset({'members', 'audit_logs', 'mailtrap', 'mailtrap_messages'})
ROLES = ('member', 'resource_manager', 'board_member', 'admin')
PROFILE_FIELDS = frozenset({'firstname', 'lastname', 'email', 'phone', 'address_street', 'address_unit',
                            'address_city', 'address_state', 'address_postal_code'})
GENERIC_MAILBOXES = frozenset({'admin', 'administrator', 'info', 'contact', 'support', 'office', 'sales',
                             'user', 'test', 'member', 'hello', 'mail', 'service', 'accounts', 'billing',
                             'noreply', 'no-reply', 'no_reply', 'donotreply', 'do-not-reply', 'postmaster',
                             'webmaster', 'root', 'security', 'help', 'team', 'marketing'})
AUDIT_KINDS = {'member_registered', 'member_updated', 'password_changed', 'totp_disabled', 'totp_reset', 'totp_enabled'}
BOUNCES = {'bounce', 'soft bounce', 'hard bounce'}
MAIL_OUTCOMES = BOUNCES | {'delivery', 'delivered', 'reject', 'spam', 'suspension'}


def email_features(value, secret):
    """Keyed equality/similarity features, never mailbox identities or provider claims."""
    if not isinstance(value, str) or len(value) > 320 or value.count('@') != 1:
        return {}
    local, domain = unicodedata.normalize('NFC', value.strip()).casefold().rsplit('@', 1)
    try:
        domain = domain.encode('idna').decode('ascii')
    except UnicodeError:
        return {}
    if not local or len(local) > 64 or not domain or any(c.isspace() for c in local + domain):
        return {}
    stem = local.split('+', 1)[0]
    if not stem:
        return {}
    hashed = lambda label, text: digest('security-email-v1:' + label + ':' + text, secret)
    return {'domain': hashed('domain', domain), 'family': hashed('family', stem + '@' + domain),
            'local': hashed('local', stem), 'plus': '+' in local, 'length': len(stem),
            'generic': stem in GENERIC_MAILBOXES,
            'trigrams': sorted({hashed('trigram', stem[i:i+3]) for i in range(len(stem)-2)}) if len(stem) >= 8 else []}


def similar(left, right):
    if not left or not right:
        return False
    cross = left['domain'] != right['domain']
    if cross and (left['generic'] or right['generic']):
        return False
    if left['local'] == right['local']:
        return True
    a, b = set(left['trigrams']), set(right['trigrams'])
    return bool(a and b and len(a & b) / len(a | b) >= .8)


def _date(value):
    parsed = timestamp(value)
    return parsed.isoformat() if parsed else None


def _role(value):
    return value if isinstance(value, str) and value in ROLES else None


def _ids(value):
    return {v for item in value for v in [identifier(item)] if v} if isinstance(value, list) and len(value) <= 1000 else set()


def project(collection, document, secret, *, operation, updates=None, removed=()):
    """Project source state and exact CDC markers separately from updateLookup state."""
    doc = document if isinstance(document, dict) else {}
    result = {'version': VERSION}
    if collection == 'members':
        result['role'] = _role(doc.get('role'))
        result['email'] = email_features(doc.get('email'), secret)
        # No password/token fingerprint, including for replacement and snapshot reads.
        if operation == 'insert':
            result['markers'] = [{'kind': 'account_created', 'email': result['email']}]
        elif operation == 'update' and isinstance(updates, dict):
            fields = set(updates) | set(removed)
            markers = []
            profile = sorted(PROFILE_FIELDS & fields)
            if profile:
                markers.append({'kind': 'profile_change', 'fields': profile})
            cleared = 'reset_password_token' in removed or ('reset_password_token' in updates and not updates['reset_password_token'])
            if 'encrypted_password' in updates:
                markers.append({'kind': 'password_change', 'token_cleared': cleared})
            if not cleared and (bool(updates.get('reset_password_token')) or _date(updates.get('reset_password_sent_at'))):
                markers.append({'kind': 'reset_request', 'sent_at': _date(updates.get('reset_password_sent_at'))})
            result['markers'] = markers
    elif collection == 'audit_logs':
        kind = doc.get('event_type')
        if not isinstance(kind, str) or kind not in AUDIT_KINDS or doc.get('resource_type') != 'Member':
            return result
        subject = identifier(doc.get('subject_id')) or identifier(doc.get('resource_id'))
        when = _date(doc.get('created_at'))
        if not subject or not when:
            return result
        result.update(member_id=subject, actor_id=identifier(doc.get('actor_id')), at=when, audit_kind=kind)
        changes = doc.get('field_changes')
        changes = changes if isinstance(changes, dict) else {}
        before, after = doc.get('before_snapshot'), doc.get('after_snapshot')
        before, after = before if isinstance(before, dict) else {}, after if isinstance(after, dict) else {}
        markers = []
        if kind == 'member_updated':
            fields = sorted(field for field in PROFILE_FIELDS if isinstance(changes.get(field), list)
                            and len(changes[field]) == 2 and changes[field][0] != changes[field][1])
            if fields:
                markers.append({'kind': 'profile_change', 'fields': fields})
            pair = changes.get('role')
            if isinstance(pair, list) and len(pair) == 2 and all(_role(v) for v in pair) and pair[0] != pair[1]:
                markers.append({'kind': 'role_change', 'before_role': pair[0], 'after_role': pair[1],
                                'grant': ROLES.index(pair[1]) > ROLES.index(pair[0])})
            scopes = changes.get('resource_manager_shop_ids')
            if isinstance(scopes, list) and len(scopes) == 2 and all(isinstance(s, list) for s in scopes):
                added = _ids(scopes[1]) - _ids(scopes[0])
                if added:
                    role = _role(after.get('role')) or (_role(pair[1]) if isinstance(pair, list) and len(pair) == 2 else None) or _role(before.get('role'))
                    markers.append({'kind': 'scope_grant', 'scope_count': len(added), 'grant': role == 'resource_manager'})
        elif kind == 'member_registered':
            markers.append({'kind': 'signup', 'email': email_features(after.get('email'), secret)})
        elif kind == 'password_changed':
            markers.append({'kind': 'password_change'})
        elif kind in {'totp_disabled', 'totp_reset'}:
            markers.append({'kind': 'mfa_change', 'action': kind, 'role': _role(before.get('role')) or _role(after.get('role'))})
        result['markers'] = markers
    elif collection == 'mailtrap':
        status = doc.get('event') or doc.get('status')
        if not isinstance(status, str) or status.casefold() not in MAIL_OUTCOMES:
            return result
        when = _date(doc.get('occurred_at') or doc.get('timestamp') or doc.get('created_at'))
        if not when:
            return result
        result.update(at=when, member_id=identifier(doc.get('member_id')))
        marker = {'kind': 'mail_outcome', 'outcome': status.casefold()}
        for source, target in (('event_id', 'provider_event'), ('message_id', 'provider_message')):
            value = doc.get(source)
            if isinstance(value, str) and 0 < len(value) <= 1000:
                marker[target] = digest('security-mail:' + source + ':' + value, secret)
        link = identifier(doc.get('mailtrap_message_id'))
        if link:
            marker['message_link'] = link
        result['markers'] = [marker]
    elif collection == 'mailtrap_messages':
        when = _date(doc.get('created_at'))
        if when:
            result.update(at=when, member_id=identifier(doc.get('member_id')), markers=[{'kind': 'mail_sent'}])
    return result
