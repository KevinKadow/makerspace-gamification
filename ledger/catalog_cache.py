"""Short-lived display metadata; live source reads still authorize every result."""
from collections import Counter
from datetime import datetime, timedelta
import hashlib
import time
from uuid import uuid4

from bson import ObjectId, json_util
from pymongo import timeout
from pymongo.errors import PyMongoError

from .read_options import optimized_reads
from .sources import sid
from .storage import now

MANIFEST_ID = "catalog-display-active"
REFRESH_JOB_ID = "catalog-refresh"
SHOP_KIND = "catalog_display_shop"
TOOL_KIND = "catalog_display_tool"
MAX_AGE_SECONDS = 300
PAGE_SIZE = 200
REFRESH_SECONDS = 60
READ_SECONDS = 2
EXPIRY_HEADROOM_SECONDS = 330
DISPLAY_FIELDS = {
    "shops": ("name", "wiki_url"),
    "tools": ("name", "description", "wiki_url", "shop_id", "prerequisite_ids"),
}


def schedule_refresh(store):
    """Coalesce bursts into one durable refresh; the periodic sweep also retries it."""
    if not optimized_reads():
        return None
    def schedule(s):
        job = s.get("ledger_inbox", REFRESH_JOB_ID)
        if job and job.get("status") in ("pending", "working"):
            return job
        job = {"_id": REFRESH_JOB_ID, "kind": "catalog_refresh", "payload": {},
               "status": "pending", "attempts": 0, "available_at": now()}
        s.put("ledger_inbox", job)
        return job
    return store.atomic(schedule)


def _remaining(deadline, cap=2000):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Catalog display read deadline exceeded")
    return min(cap, max(1, int(remaining * 1000)))


def _source_rows(sources, name, deadline):
    rows, last = [], None
    while True:
        query = {} if last is None else {"_id": {"$gt": last}}
        page = sources.bounded(name, query, list(DISPLAY_FIELDS[name]), PAGE_SIZE,
                               _remaining(deadline))
        if not page:
            return rows
        if any("_id" not in row for row in page) or (last is not None and page[-1]["_id"] == last):
            raise ValueError("Catalog display paging did not advance")
        rows.extend(page)
        last = page[-1]["_id"]
        if len(page) < PAGE_SIZE:
            return rows


def _identifier(value):
    return isinstance(value, (ObjectId, str, int)) and not isinstance(value, bool)


def _sanitize(name, row):
    if not _identifier(row.get("_id")) or not isinstance(row.get("name"), str):
        raise ValueError("Catalog display record has no usable ID or name")
    doc = {"_id": row["_id"]}
    for field in DISPLAY_FIELDS[name]:
        value = row.get(field)
        if field == "shop_id":
            if value is not None and not _identifier(value):
                raise ValueError("Catalog display parent ID is invalid")
            if field in row:
                doc[field] = value
        elif field == "prerequisite_ids":
            value = value or []
            if not isinstance(value, list) or any(not _identifier(item) for item in value):
                raise ValueError("Catalog display prerequisite IDs are invalid")
            doc[field] = list(value)
        elif isinstance(value, str):
            doc[field] = value
    return doc


def _complete(store, manifest, deadline):
    base = {"generation": manifest.get("generation")}
    return (store.count("ledger_catalog", {**base, "kind": SHOP_KIND}, max_time_ms=_remaining(deadline)) == manifest.get("shop_count", -1)
            and store.count("ledger_catalog", {**base, "kind": TOOL_KIND}, max_time_ms=_remaining(deadline)) == manifest.get("tool_count", -1))


def refresh(ledger, job=None):
    """Publish a complete immutable generation, or retain the previous manifest."""
    if not optimized_reads():
        return None
    started, deadline = now(), time.monotonic() + REFRESH_SECONDS
    previous = ledger.store.get("ledger_catalog", MANIFEST_ID)
    shops = [_sanitize("shops", row) for row in _source_rows(ledger.sources, "shops", deadline)]
    tools = [_sanitize("tools", row) for row in _source_rows(ledger.sources, "tools", deadline)]
    if len({sid(row["_id"]) for row in shops}) != len(shops) or len({sid(row["_id"]) for row in tools}) != len(tools):
        raise ValueError("Catalog display IDs are ambiguous")
    digest = hashlib.sha256(json_util.dumps({"shops": shops, "tools": tools}, sort_keys=True).encode()).hexdigest()
    reuse = bool(previous and previous.get("digest") == digest
                 and isinstance(previous.get("expires_at"), datetime)
                 and previous["expires_at"] > now() + timedelta(seconds=EXPIRY_HEADROOM_SECONDS)
                 and _complete(ledger.store, previous, deadline))
    if reuse:
        manifest = {**previous, "refreshed_at": started}
    else:
        generation = uuid4().hex
        expires = started + timedelta(days=1)
        names = {sid(row["_id"]): {"_id": row["_id"], "name": row["name"]} for row in tools}
        batch = []
        for name, kind, rows in (("shops", SHOP_KIND, shops), ("tools", TOOL_KIND, tools)):
            for row in rows:
                doc = {"_id": f"catalog-display:{generation}:{name}:{sid(row['_id'])}",
                       "kind": kind, "generation": generation, "source_id": row["_id"],
                       "display_fields": {field: row[field] for field in DISPLAY_FIELDS[name] if field in row},
                       "expires_at": expires}
                if name == "tools":
                    doc["shop_id"] = row.get("shop_id")
                    doc["prerequisite_labels"] = [names[sid(identifier)] for identifier in row.get("prerequisite_ids", [])
                                                    if sid(identifier) in names]
                batch.append(doc)
                if len(batch) == PAGE_SIZE:
                    _remaining(deadline)
                    ledger.store.put_many("ledger_catalog", batch)
                    batch = []
        if batch:
            _remaining(deadline)
            ledger.store.put_many("ledger_catalog", batch)
        manifest = {"_id": MANIFEST_ID, "kind": "catalog_display_manifest", "generation": generation,
                    "refreshed_at": started, "expires_at": expires, "digest": digest,
                    "shop_count": len(shops), "tool_count": len(tools),
                    "tool_counts": dict(Counter(sid(row.get("shop_id")) or "" for row in tools))}
    _remaining(deadline)
    def publish(s):
        if job:
            current_job = s.get("ledger_inbox", job["_id"])
            if (not current_job or current_job.get("status") != "working"
                    or current_job.get("lease") != job.get("lease")
                    or current_job.get("available_at", now()) <= now()):
                return None
        current = s.get("ledger_catalog", MANIFEST_ID)
        previous_generation = (previous or {}).get("generation")
        if current and (current.get("refreshed_at", started) > started or
                        (current.get("generation") != previous_generation and current.get("refreshed_at", started) >= started)):
            return None
        s.put("ledger_catalog", manifest)
        return manifest
    return ledger.store.atomic(publish)


def display_catalog(ledger, search=""):
    """Return selected cached labels/topology, or None so callers read live sources.

    This helper does not establish current enabled status, parent safety, tool
    availability, member clearances, consent, or prerequisite eligibility.
    """
    if not optimized_reads():
        return None
    with timeout(READ_SECONDS):
        return _read_display_catalog(ledger, search)


def _read_display_catalog(ledger, search):
    deadline = time.monotonic() + READ_SECONDS
    try:
        manifest = ledger.store.get("ledger_catalog", MANIFEST_ID)
        clock = now()
        if (not manifest or not isinstance(manifest.get("refreshed_at"), datetime)
                or not isinstance(manifest.get("expires_at"), datetime)
                or not 0 <= (clock - manifest["refreshed_at"]).total_seconds() <= MAX_AGE_SECONDS
                or manifest["expires_at"] <= clock + timedelta(seconds=EXPIRY_HEADROOM_SECONDS)
                or not isinstance(manifest.get("tool_counts"), dict)):
            return None
        base = {"generation": manifest["generation"]}
        # The small shop directory verifies selection completeness. Tool payloads
        # are fetched only for matching shops; labels for edges are embedded.
        shop_rows = ledger.store.select("ledger_catalog", {**base, "kind": SHOP_KIND},
                                        projection={"_id": 0, "source_id": 1, "display_fields.name": 1},
                                        sort=[("source_id", 1)],
                                        max_time_ms=_remaining(deadline))
        if len(shop_rows) != manifest["shop_count"]:
            return None
        shops = [{"_id": row["source_id"], **row["display_fields"]} for row in shop_rows]
        shops = [row for row in shops if search.casefold() in row["name"].casefold() or search == sid(row["_id"])]
        shop_ids = [row["_id"] for row in shops]
        tool_rows = ledger.store.select("ledger_catalog", {**base, "kind": TOOL_KIND, "shop_id": {"$in": shop_ids}},
                                        projection={"_id": 0, "source_id": 1, "display_fields.name": 1,
                                                    "display_fields.shop_id": 1, "display_fields.prerequisite_ids": 1,
                                                    "prerequisite_labels": 1},
                                        sort=[("source_id", 1)],
                                        max_time_ms=_remaining(deadline)) if shop_ids else []
        expected = sum(manifest["tool_counts"].get(sid(identifier), 0) for identifier in shop_ids)
        if len(tool_rows) != expected:
            return None
        prerequisites = {}
        for row in tool_rows:
            for label in row.get("prerequisite_labels", []):
                prerequisites[sid(label["_id"])] = {"_id": label["_id"], "name": label["name"]}
        _remaining(deadline)
        return {"shops": shops,
                "tools": [{"_id": row["source_id"], **row["display_fields"]} for row in tool_rows],
                "prerequisite_tools": list(prerequisites.values()), "refreshed_at": manifest["refreshed_at"]}
    except (PyMongoError, OSError, TimeoutError, KeyError, TypeError, ValueError):
        return None
