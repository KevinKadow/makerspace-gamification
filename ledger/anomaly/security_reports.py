"""Security commands: suppressed summaries for readers, private evidence for staff."""
import json

from ..domain import Denied
from .security import SecurityEngine
from .security_delivery import render_incident
from .security_evidence import enabled
from .security_learning import FEATURES, SPEC_KEYS
from .schema import fingerprint
from .state import stamp


def build(reports, job, args, capability):
    state, config, payload = reports.state, reports.config, job['payload']
    if not enabled(config):
        return {'text': 'Security monitoring is disabled.', 'charts': [], 'capability': capability}
    engine = SecurityEngine(state, config)
    verb = args[0].lower() if args else 'summary'
    if verb == 'summary':
        metrics = engine.aggregate()
        text = 'Security trends over the last seven days. Counts are review signals, not proof of compromise.\n'
        text += '\n'.join(f"{r['report']} / {r['cohort']}: {r['value']:g} (n={r['sample_size']})." for r in metrics)
        if not metrics:
            text += 'Insufficient evidence or small populations; all counts suppressed.'
        text += '\nReset issuance is observable; requests without a database change are not. Missing completion evidence does not prove failure. Historical audit coverage may be incomplete.'
        cards = state.get('metabase', 'bootstrap', {}).get('cards', {})
        charts = [{'type': 'saved_card', 'card_id': card} for key, card in cards.items() if str(key).startswith('security')][:3] if metrics else []
        return {'text': text, 'charts': charts, 'capability': capability, 'period': 'Last seven days'}
    if capability != 'staff_detail' or not reports.access.staff(payload['user_id']):
        raise Denied('Security incident detail and administration require a verified staff DM.')
    if verb == 'incidents':
        rows = sorted(state.rows('security_incidents'), key=lambda pair: pair[1]['created_at'], reverse=True)
        text = '\n'.join(f"{key}: {r['severity']} / {r['rule']} / {r['status']}" for key, r in rows[:50]) or 'No security incidents.'
        from datetime import datetime, timedelta, timezone
        unattributed = [r for r in state.security_events((datetime.now(timezone.utc)-timedelta(days=7)).isoformat(), kind='role_change') if not r.get('actor_id')]
        text += '\nUnattributed role changes (no verified chain): ' + ', '.join(r['id'] for r in unattributed[-20:])
    elif verb == 'evidence' and len(args) == 2:
        row = state.security_get_event(args[1])
        if not row:
            raise ValueError('Unknown security evidence ID')
        text = json.dumps({k:v for k,v in row.items() if k in {'id','at','kind','member_id','actor_id','fields',
            'before_role','after_role','scope_count','grant','action','confidence','historical','origin'}})
    elif verb in {'incident', 'detail'} and len(args) == 2:
        row = state.get('security_incidents', args[1])
        if not row:
            raise ValueError('Unknown security incident ID')
        text = render_incident(row) + '\nStatus: ' + row['status']
    elif verb == 'resolve' and len(args) == 2:
        engine.resolve(args[1], payload['user_id'])
        text = 'Security incident resolved after staff review: ' + args[1]
    elif verb in {'detector', 'detectors'}:
        action = args[1] if len(args) > 1 else 'list'
        revision = args[2] if len(args) > 2 else None
        builtin_ids = [r['id'] for r in engine.rules['rules']] + ['signup_alias_family', 'signup_similar', 'bounce_burst',
            'privilege_propagation', 'privileged_mfa_change', 'bulk_privilege_grants']
        builtins = {fingerprint([engine.revision, rule]): rule for rule in builtin_ids}
        if action == 'list':
            text = '\n'.join(f"{key}: {rule} / " + state.get('security_rule_status', key, {}).get('status', 'active') for key, rule in builtins.items())
            text += '\n' + '\n'.join(f"{key}: learned {r['spec']['feature']} / {r['status']}" for key, r in state.rows('security_detectors'))
        elif action == 'review' and revision:
            if revision in builtins:
                text = json.dumps({'rule': builtins[revision], 'revision': revision, 'configuration': engine.rules})
            else:
                row = state.get('security_detectors', revision)
                if not row:
                    raise ValueError('Unknown detector revision')
                evaluation = engine.detectors.backtest(row['spec'])
                text = json.dumps({**row, 'evaluation': evaluation, 'shadow': state.get('security_shadow', revision, {})})
        elif action in {'approve', 'disable'} and revision:
            if revision in builtins:
                state.put('security_rule_status', revision, {'status': 'disabled' if action == 'disable' else 'active',
                    'by': payload['user_id'], 'at': stamp(), 'rule': builtins[revision]})
            else:
                getattr(engine.detectors, action)(revision, payload['user_id'])
            text = 'Security detector ' + action + ' completed for exact revision ' + revision
        elif action == 'propose':
            if reports.api is None:
                raise ValueError('Inference unavailable')
            saved = state.get('security_proposal_requests', job['_id'])
            if saved:
                spec = saved['spec']
            else:
                summary = {'features_and_floors': FEATURES, 'required_keys': sorted(SPEC_KEYS),
                    'scopes': ['global', 'member'], 'window': 'one hour; same local weekday/hour over 12 weeks',
                    'minimum_comparable': 8, 'mad_threshold_range': [3, 20], 'quantile_range': [.9, .999],
                    'description_fields': ['reference', 'confounders', 'falsification'],
                    'metrics': engine.aggregate(), 'policy': 'Propose one bounded security detector; no activation or source queries'}
                prompt = reports.prompt(job['_id'] + ':security', 'anomaly_propose', json.dumps(summary))
                output = reports.api.complete([{'role': 'system', 'content': prompt['system']},
                    {'role': 'user', 'content': prompt['prompt']}], temperature=.3, max_tokens=1200)
                spec = json.loads(output)
            revision = engine.detectors.propose(spec, payload['user_id'])
            state.put('security_proposal_requests', job['_id'], {'spec': spec, 'revision': revision})
            text = 'Security detector proposed in shadow: ' + revision
        else:
            raise ValueError('Use security detectors list, review, propose, approve or disable with an exact revision')
    else:
        raise ValueError('Use security summary, incidents, incident <ID>, resolve <ID>, or detectors')
    return {'text': text[:30000], 'charts': [], 'capability': capability}
