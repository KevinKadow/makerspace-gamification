"""Aggregate pilot measurements without logging member bodies or source records."""
from collections import Counter


def snapshot(store):
    people = store.select('ledger_participants')
    evidence = store.select('ledger_evidence')
    jobs = store.select('ledger_outbox')
    compositions = [j['composed'] for j in jobs if j.get('composed')]
    return {
        'participants': len(people), 'opted_in': sum(p['opted_in'] for p in people),
        'ranks': dict(Counter(str(p['rank']) for p in people)),
        'mentoring_sessions': sum(p.get('metrics', {}).get('mentoring', 0) for p in people),
        'verified_learning': sum(e.get('status') == 'approved' and e.get('achievement') in ('first_build', 'challenge') for e in evidence),
        'feedback_received': sum(e.get('kind') == 'feedback' for e in evidence),
        'projects': len(store.select('ledger_projects')),
        'completed_group_quests': len(store.select('ledger_quests', {'status': 'completed'})),
        'deliveries': dict(Counter(j['status'] for j in jobs)),
        'generation_attempts': len(compositions),
        'fallbacks': sum(c['outcome'] == 'fallback' for c in compositions),
        'shared_deliveries': sum(j['status'] == 'done' and j['payload'].get('audience') == 'shared' for j in jobs),
    }
