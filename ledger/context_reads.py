"""Bounded private history reads with request-scoped live author checks."""
from .read_options import optimized_reads
from .storage import now
from math import isfinite


FIELDS = {name: 1 for name in ("kind", "channel", "thread", "text", "at", "at_order", "member_id",
                              "conversation_requested", "participating", "consent_generation")}


def backfill_context_order(store, page_size=32):
    updated, cursor = 0, None
    while True:
        query = {"kind": {"$in": ["message", "reply"]}, "at_order": {"$exists": False}}
        if cursor is not None:
            query["_id"] = {"$gt": cursor}
        rows = store.select("ledger_context", query, projection={"at": 1}, sort=[("_id", 1)], limit=page_size)
        if not rows:
            return updated
        for row in rows:
            def write(s):
                current = s.get("ledger_context", row["_id"])
                if current is None or "at_order" in current:
                    return False
                try:
                    order = float(current["at"])
                    if not isfinite(order):
                        return False
                    current["at_order"] = order
                except (KeyError, TypeError, ValueError):
                    return False
                s.put("ledger_context", current)
                return True
            updated += bool(store.atomic(write))
        cursor = rows[-1]["_id"]


def history(ledger, payload, unrelated=False):
    from .conversations import restriction_filter
    member = payload["member_id"]
    restricted = restriction_filter(ledger, member)
    query = {"kind": {"$in": ["message", "reply"]}, "channel": payload["channel"]}
    if not payload["channel"].startswith("D"):
        query["thread"] = payload["thread"]
    if unrelated:
        query["$or"] = [{"kind": "reply"}, {"conversation_requested": True}]
    # Expired bodies must not be revived while Mongo's asynchronous TTL waits.
    query["$and"] = [{"$or": [{"expires_at": {"$exists": False}}, {"expires_at": {"$gt": now()}}]}]
    identities, result, cursor = {}, [], None
    legacy = not optimized_reads() or ledger.store.exists("ledger_context", {
        "$and": [query, {"at_order": {"$exists": False}}]})
    while True:
        page_query = dict(query)
        if cursor:
            page_query["$and"] = [*query["$and"], {"$or": [
                {"at_order": {"$lt": cursor[0]}}, {"at_order": cursor[0], "_id": {"$lt": cursor[1]}}]}]
        page = ledger.store.select("ledger_context", page_query, projection=FIELDS,
                    sort=None if legacy else [("at_order", -1), ("_id", -1)],
                    limit=None if legacy else 32, max_time_ms=2000)
        if legacy:
            page.sort(key=lambda d: (float(d["at"]), d["_id"]), reverse=True)
        if not page:
            break
        authors = {c["member_id"] for c in page if c["kind"] != "reply"} - identities.keys()
        identities.update({m: None for m in authors})
        identities.update(ledger.sources.identities(authors))
        for c in page:
            author = identities.get(c["member_id"])
            if c["_id"] == payload["message_id"] or (c["kind"] != "reply" and
                    (not author or author.get("status") in ("suspended", "revoked"))):
                continue
            if restricted(c["text"]) or (c["member_id"] == member and (
                    c.get("consent_generation", 0) != payload.get("consent_generation", 0) or
                    c.get("participating", True) != payload.get("participating", True))):
                continue
            result.append({"role": "assistant" if c["kind"] == "reply" else "user",
                           "content": c["text"][:600] if c["kind"] == "reply" else
                                      f"Author {author['slack_id']}: {c['text'][:600]}"})
            if len(result) == 6:
                return list(reversed(result))
        if legacy or len(page) < 32:
            break
        cursor = (page[-1]["at_order"], page[-1]["_id"])
    return list(reversed(result))
