"""Aggregate pilot measurements without logging member bodies or source records."""
from collections import Counter

SHORT_PROFILES = ("receipt", "summary", "guidance")
FALLBACK_REASONS = ("transport_failure", "invalid_output", "circuit_open")
SUMMARY_OPERATIONS = ("post", "update", "post_and_update")


def _category(value, allowed):
    return value if value in allowed else "other"


def _duration(rows, name):
    count = sum(row.get(name + "_count", 0) for row in rows)
    total = sum(row.get(name + "_total", 0) for row in rows)
    return {"count": count, "total": round(total, 2), "mean": round(total / count, 2) if count else 0}


def _numeric_totals(values, names):
    # Match Mongo $sum's handling of missing/nonnumeric historical fields.
    return {key: value for name in names for key, value in (
        (name + "_count", int(values.get(name) is not None)),
        (name + "_total", values[name] if isinstance(values.get(name), (int, float)) else 0))}


def _diagnostics(short, delivered, owners):
    profiles, reasons, operations = Counter(), Counter(), Counter()
    for row in short:
        profiles[row["_id"]["profile"]] += row["count"]
        reason = row["_id"]["fallback_reason"]
        if reason is not None:
            reasons[reason] += row["count"]
    for row in delivered:
        operations[row["_id"]] += row["count"]
    return {"short_generation": {"attempts": sum(row["count"] for row in short),
        "profiles": dict(profiles), "fallback_reasons": dict(reasons),
        "generation_ms": _duration(short, "generation_ms")},
        "result_notifications": {"actions": sum(row["count"] for row in owners),
            "posts": sum(row["posts"] for row in owners), "updates": sum(row["updates"] for row in owners),
            "operations": dict(operations), "queue_age_ms": _duration(delivered, "queue_age_ms"),
            "summary_latency_ms": _duration(delivered, "summary_latency_ms")}}


def _legacy_diagnostics(evidence, jobs):
    short, delivered, owners = [], [], []
    for job in jobs:
        composed = job.get("composed") or {}
        profile, reason = composed.get("generation_profile"), composed.get("fallback_reason")
        if profile in SHORT_PROFILES:
            short.append({"_id": {"profile": profile,
                "fallback_reason": _category(reason, FALLBACK_REASONS) if reason is not None else None},
                "count": 1, **_numeric_totals(composed, ("generation_ms",))})
        metrics = job.get("delivery_metrics")
        if job.get("kind") == "summary_delivery" and isinstance(metrics, dict) and "operation" in metrics:
            delivered.append({"_id": _category(metrics.get("operation"), SUMMARY_OPERATIONS), "count": 1,
                **_numeric_totals(metrics, ("queue_age_ms", "summary_latency_ms"))})
    for item in evidence:
        if item.get("kind") == "notification_summary":
            owners.append({"count": 1, "posts": _numeric_totals(item, ("post_count",))["post_count_total"],
                           "updates": _numeric_totals(item, ("update_count",))["update_count_total"]})
    return _diagnostics(short, delivered, owners)


def _legacy_snapshot(store):
    people = store.select('ledger_participants')
    evidence = store.select('ledger_evidence')
    jobs = store.select('ledger_outbox')
    compositions = [j['composed'] for j in jobs if j.get('composed')]
    inference_jobs = [j for j in store.select('ledger_inbox') if j['kind'] == 'engagement']
    decisions = [e for e in evidence if e.get('kind') == 'ai_decision']
    return {
        'participants': len(people), 'opted_in': sum(p['opted_in'] for p in people),
        'ranks': dict(Counter(str(p['rank']) for p in people)),
        'mentoring_sessions': sum(p.get('metrics', {}).get('mentoring', 0) for p in people),
        'verified_learning': sum(e.get('status') == 'approved' and e.get('achievement') in ('first_build', 'challenge') for e in evidence),
        'feedback_received': sum(e.get('kind') == 'feedback' for e in evidence),
        'projects': len(store.select('ledger_projects')),
        'completed_group_quests': len(store.select('ledger_quests', {'status': 'completed'})) + len(
            store.select('ledger_relationships', {'kind': 'quest_project', 'status': 'completed'})),
        'quest_generation_requests': dict(Counter(e['status'] for e in evidence if e.get('kind') == 'quest_generation')),
        'deliveries': dict(Counter(j['status'] for j in jobs)),
        'generation_attempts': len(compositions),
        'fallbacks': sum(c['outcome'] == 'fallback' for c in compositions),
        'shared_deliveries': sum(j['status'] == 'done' and j['payload'].get('audience') == 'shared' for j in jobs),
        'inference_latency_seconds': [c['latency'] for c in compositions if 'latency' in c],
        'queue_backlog': sum(j['status'] in ('pending', 'working') for j in jobs + store.select('ledger_inbox')),
        'engagement_jobs': dict(Counter(j['status'] for j in inference_jobs)),
        'engagement_decisions': dict(Counter(e['status'] for e in decisions)),
        'budget_rejections': sum(e.get('kind') == 'ai_rejection' and e.get('reason_code') == 'budget' for e in evidence),
        'deductions': sum(e['category'] == 'imitation_deduction' and e['status'] == 'committed' for e in decisions),
        'delegation_revocations': sum(e.get('kind') == 'delegation_audit' and e.get('action') == 'revoked' for e in evidence),
        'delegation_failures': sum(j.get('last_error') == 'Denied' for j in store.select('ledger_inbox')),
        'arrival_outcomes': dict(Counter(e['status'] for e in evidence if e.get('kind') == 'arrival')),
        **_legacy_diagnostics(evidence, jobs),
    }


def snapshot(store):
    from .read_options import optimized_reads
    if not optimized_reads():
        return _legacy_snapshot(store)
    def conditional(condition):
        return {"$sum": {"$cond": [condition, 1, 0]}}
    def groups(collection, pipeline):
        return store.aggregate(collection, pipeline, max_time_ms=2000)
    def durations(names, prefix):
        return {key: value for name in names for key, value in (
            (name + "_count", conditional({"$ne": [{"$ifNull": ["$" + prefix + name, None]}, None]})),
            (name + "_total", {"$sum": {"$ifNull": ["$" + prefix + name, 0]}}))}
    people = groups("ledger_participants", [{"$group": {"_id": "$rank", "count": {"$sum": 1},
        "opted_in": conditional({"$eq": ["$opted_in", True]}),
        "mentoring": {"$sum": {"$ifNull": ["$metrics.mentoring", 0]}}}}])
    evidence = groups("ledger_evidence", [{"$group": {
        "_id": {k: "$" + k for k in ("kind", "status", "achievement", "reason_code", "category", "action")},
        "count": {"$sum": 1}}}])
    composed = {"$and": [{"$ne": [{"$ifNull": ["$composed", None]}, None]}, {"$ne": ["$composed", {}]}]}
    jobs = groups("ledger_outbox", [{"$group": {"_id": "$status", "count": {"$sum": 1},
        "generation_attempts": conditional(composed),
        "fallbacks": conditional({"$and": [composed, {"$eq": ["$composed.outcome", "fallback"]}]}),
        "shared_deliveries": conditional({"$and": [{"$eq": ["$status", "done"]},
                                                   {"$eq": ["$payload.audience", "shared"]}]})}}])
    inbox = groups("ledger_inbox", [{"$group": {"_id": {"kind": "$kind", "status": "$status"},
        "count": {"$sum": 1}, "denied": conditional({"$eq": ["$last_error", "Denied"]})}}])
    def counter(rows, key, predicate=lambda d: True):
        counts = Counter()
        for row in rows:
            if predicate(row["_id"]):
                counts[row["_id"].get(key)] += row["count"]
        return dict(counts)
    def total(predicate):
        return sum(e["count"] for e in evidence if predicate(e["_id"]))
    # Do not $push the historical latency array into a single aggregation BSON
    # document. The existing API intentionally retains this narrow list.
    latencies = store.select("ledger_outbox", {"composed.latency": {"$exists": True}},
                            projection={"_id": 0, "composed.latency": 1}, max_time_ms=2000)
    # New diagnostics return constant-sized aggregate categories/totals only.
    # Unknown categories collapse to "other" rather than exposing free text.
    reason = {"$ifNull": ["$composed.fallback_reason", None]}
    short = groups("ledger_outbox", [{"$match": {"composed.generation_profile": {"$in": list(SHORT_PROFILES)}}},
        {"$group": {"_id": {"profile": "$composed.generation_profile", "fallback_reason": {
            "$cond": [{"$in": [reason, [*FALLBACK_REASONS, None]]}, reason, "other"]}},
            "count": {"$sum": 1}, **durations(("generation_ms",), "composed.")}}])
    operation = "$delivery_metrics.operation"
    delivered = groups("ledger_outbox", [{"$match": {"kind": "summary_delivery", "delivery_metrics.operation": {"$exists": True}}},
        {"$group": {"_id": {"$cond": [{"$in": [operation, list(SUMMARY_OPERATIONS)]}, operation, "other"]},
            "count": {"$sum": 1}, **durations(("queue_age_ms", "summary_latency_ms"), "delivery_metrics.")}}])
    owners = groups("ledger_evidence", [{"$match": {"kind": "notification_summary"}}, {"$group": {"_id": None,
        "count": {"$sum": 1}, "posts": {"$sum": {"$ifNull": ["$post_count", 0]}},
        "updates": {"$sum": {"$ifNull": ["$update_count", 0]}}}}])
    return {
        "participants": sum(p["count"] for p in people),
        "opted_in": sum(p["opted_in"] for p in people),
        "ranks": {str(p["_id"]): p["count"] for p in people},
        "mentoring_sessions": sum(p["mentoring"] for p in people),
        "verified_learning": total(lambda d: d.get("status") == "approved" and d.get("achievement") in ("first_build", "challenge")),
        "feedback_received": total(lambda d: d.get("kind") == "feedback"),
        "projects": store.count("ledger_projects", max_time_ms=2000),
        "completed_group_quests": store.count("ledger_quests", {"status": "completed"}, max_time_ms=2000) +
            store.count("ledger_relationships", {"kind": "quest_project", "status": "completed"}, max_time_ms=2000),
        "quest_generation_requests": counter(evidence, "status", lambda d: d.get("kind") == "quest_generation"),
        "deliveries": {j["_id"]: j["count"] for j in jobs},
        "generation_attempts": sum(j["generation_attempts"] for j in jobs),
        "fallbacks": sum(j["fallbacks"] for j in jobs),
        "shared_deliveries": sum(j["shared_deliveries"] for j in jobs),
        "inference_latency_seconds": [j["composed"]["latency"] for j in latencies],
        "queue_backlog": sum(j["count"] for j in jobs if j["_id"] in ("pending", "working")) +
                         sum(j["count"] for j in inbox if j["_id"]["status"] in ("pending", "working")),
        "engagement_jobs": counter(inbox, "status", lambda d: d.get("kind") == "engagement"),
        "engagement_decisions": counter(evidence, "status", lambda d: d.get("kind") == "ai_decision"),
        "budget_rejections": total(lambda d: d.get("kind") == "ai_rejection" and d.get("reason_code") == "budget"),
        "deductions": total(lambda d: d.get("kind") == "ai_decision" and d.get("category") == "imitation_deduction" and d.get("status") == "committed"),
        "delegation_revocations": total(lambda d: d.get("kind") == "delegation_audit" and d.get("action") == "revoked"),
        "delegation_failures": sum(j["denied"] for j in inbox),
        "arrival_outcomes": counter(evidence, "status", lambda d: d.get("kind") == "arrival"),
        **_diagnostics(short, delivered, owners),
    }
