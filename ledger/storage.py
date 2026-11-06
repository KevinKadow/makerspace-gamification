"""Ledger-only writes. Mongo sessions are explicitly passed through every operation."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import RLock
from uuid import uuid4
import re
import os

from pymongo import ASCENDING, MongoClient, ReplaceOne, ReturnDocument
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern
from pymongo.errors import DuplicateKeyError


def now():
    return datetime.now(timezone.utc)


def owned(name):
    if not name.startswith("ledger_"):
        raise ValueError("Writes are restricted to ledger_* collections")
    return name


def display_keys(collection, doc):
    if isinstance(doc.get("title"), str) and (
            collection == "ledger_catalog" and doc.get("kind") == "challenge" or
            collection == "ledger_quests" and doc.get("status") == "open"):
        doc["title_key"] = doc["title"].casefold()
    return doc


class MongoStore:
    def __init__(self, database, session=None):
        self.db, self.session = database, session

    def get(self, collection, key, projection=None):
        return self.db[owned(collection)].find_one({"_id": key}, projection, session=self.session)

    def select(self, collection, query=None, *, projection=None, sort=None, limit=None, max_time_ms=None):
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("Read limits must be positive integers")
        cursor = self.db[owned(collection)].find(query or {}, projection, session=self.session)
        if sort:
            cursor = cursor.sort(sort)
        if limit is not None:
            cursor = cursor.limit(limit)
        if max_time_ms is not None:
            cursor = cursor.max_time_ms(max_time_ms)
        return list(cursor)

    def count(self, collection, query=None, *, max_time_ms=None):
        options = {"session": self.session}
        if max_time_ms is not None:
            options["maxTimeMS"] = max_time_ms
        return self.db[owned(collection)].count_documents(query or {}, **options)

    def exists(self, collection, query=None):
        return bool(self.db[owned(collection)].find_one(query or {}, {"_id": 1}, session=self.session))

    def aggregate(self, collection, pipeline, *, max_time_ms=2000):
        # Pipelines are authored by application code, never passed by a model.
        return list(self.db[owned(collection)].aggregate(pipeline, session=self.session,
                    maxTimeMS=max_time_ms, allowDiskUse=False))

    def legacy_review_channels(self, collection, destination):
        """Select old parents with mismatched addresses in dynamic contribution keys."""
        address = {"$ifNull": ["$$contribution.v.review_channel_id", None]}
        contributions = {"$cond": [{"$eq": [{"$type": "$contributions"}, "object"]}, "$contributions", {}]}
        mismatched = {"$map": {"input": {"$objectToArray": contributions}, "as": "contribution",
            "in": {"$and": [{"$ne": [address, None]}, {"$ne": [address, destination]}]}}}
        query = {"review_notice_channel": None, "contributions": {"$type": "object"},
                 "$expr": {"$anyElementTrue": [mismatched]}}
        if collection == "ledger_relationships":
            query["kind"] = "quest_project"
        return self.select(collection, query)

    def put(self, collection, doc):
        from .review_notifications import prepare, watched
        display_keys(collection, doc)
        if watched(collection):
            if not self.session:
                return self.atomic(lambda s: s.put(collection, doc))
            prepare(self, collection, doc, self.get(collection, doc["_id"]))
        self.db[owned(collection)].replace_one({"_id": doc["_id"]}, doc, upsert=True, session=self.session)

    def delete(self, collection, key):
        self.db[owned(collection)].delete_one({"_id": key}, session=self.session)

    def put_many(self, collection, documents):
        from .review_notifications import watched
        if watched(collection):
            raise ValueError("Reviewable records require individual transactional preparation")
        records = [display_keys(collection, d) for d in documents]
        if records:
            self.db[owned(collection)].bulk_write([ReplaceOne({"_id": d["_id"]}, d, upsert=True) for d in records],
                                                   ordered=True, session=self.session)

    def atomic(self, fn):
        if self.session:
            return fn(self)
        # Racing first upserts of a deterministic budget/cooldown/receipt ID
        # can raise DuplicateKeyError without a transient-transaction label.
        # The transaction is aborted, so retry the entire pure callback.
        for attempt in range(3):
            try:
                with self.db.client.start_session() as session:
                    return session.with_transaction(lambda s: fn(MongoStore(self.db, s)),
                                                    read_concern=ReadConcern("snapshot"), write_concern=WriteConcern("majority"))
            except DuplicateKeyError:
                if attempt == 2:
                    raise

    def claim(self, collection, clock=None, kinds=None, exclude=None):
        clock = clock or now()
        query = {"status": {"$in": ["pending", "working"]}, "available_at": {"$lte": clock}}
        if kinds:
            query["kind"] = {"$in": kinds}
        if exclude:
            query["kind"] = {"$nin": exclude}
        return self.db[owned(collection)].find_one_and_update(
            query,
            {"$set": {"status": "working", "lease": str(uuid4()),
                      "available_at": clock + timedelta(seconds=120)}, "$inc": {"attempts": 1}},
            sort=[("available_at", ASCENDING)], return_document=ReturnDocument.AFTER)

    def ready(self):
        self.db.command("ping")
        hello = self.db.client.admin.command("hello")
        if not (hello.get("setName") or hello.get("msg") == "isdbgrid"):
            raise RuntimeError("The Ledger requires MongoDB replica-set transactions")

    def indexes(self):
        self.db.ledger_participants.create_index("member_id", unique=True)
        self.db.ledger_inbox.create_index([("status", 1), ("available_at", 1)])
        self.db.ledger_outbox.create_index([("status", 1), ("available_at", 1)])
        self.db.ledger_outbox.create_index([("kind", 1), ("status", 1), ("review_reconcile_resolved", 1)])
        for collection in ("ledger_evidence", "ledger_quests", "ledger_relationships"):
            self.db[collection].create_index("review_notice_channel")
            self.db[collection].create_index("review_channel_id")
            self.db[collection].create_index("review_notice_dirty")
        self.db.ledger_quests.create_index("status")
        self.db.ledger_evidence.create_index([("recipient", 1), ("kind", 1)])
        self.db.ledger_evidence.create_index([("kind", 1), ("action_id", 1), ("authorization", 1)])
        self.db.ledger_evidence.create_index([("recipient", 1), ("kind", 1), ("day", 1), ("xp_awarded", 1)])
        self.db.ledger_evidence.create_index([("recipient", 1), ("giver", 1), ("week", 1), ("xp_awarded", 1)])
        self.db.ledger_awards.create_index([("member_id", 1), ("kind", 1)])
        self.db.ledger_context.create_index("expires_at", expireAfterSeconds=0)
        self.db.ledger_relationships.create_index([("recipient", 1), ("kind", 1)])
        self.db.ledger_relationships.create_index([("kind", 1), ("delegate", 1), ("status", 1)])
        self.db.ledger_relationships.create_index([("kind", 1), ("grantor", 1), ("status", 1)])
        self.db.ledger_relationships.create_index([("kind", 1), ("scope.kind", 1), ("scope.shops", 1), ("status", 1)])
        self.db.ledger_relationships.create_index([("member_id", 1), ("logical_id", 1), ("kind", 1)])
        self.db.ledger_relationships.create_index([("kind", 1), ("status", 1), ("quest_revision", 1)])
        self.db.ledger_quests.create_index([("kind", 1), ("status", 1), ("target_rank", 1)])
        self.db.ledger_quests.create_index([("creator", 1), ("logical_id", 1)])
        self.db.ledger_evidence.create_index([("kind", 1), ("status", 1), ("shop_id", 1)])
        self.db.ledger_evidence.create_index([("kind", 1), ("member_id", 1), ("day", 1)])
        self.db.ledger_evidence.create_index([("kind", 1), ("status", 1), ("lease_until", 1)])
        self.db.ledger_evidence.create_index([("kind", 1), ("announcement_channel", 1), ("announcement_ts", 1)])
        self.db.ledger_catalog.create_index([("kind", 1), ("quest_type", 1), ("target_rank", 1), ("title_key", 1), ("revision", 1), ("_id", 1)])
        self.db.ledger_catalog.create_index([("kind", 1), ("quest_type", 1), ("title_key", 1), ("revision", 1), ("_id", 1)])
        self.db.ledger_catalog.create_index([("kind", 1), ("active", 1), ("title_key", 1), ("_id", 1)])
        self.db.ledger_quests.create_index([("status", 1), ("title_key", 1), ("_id", 1)])
        self.db.ledger_catalog.create_index([("kind", 1), ("generation", 1), ("source_id", 1)])
        self.db.ledger_catalog.create_index([("kind", 1), ("generation", 1), ("shop_id", 1), ("source_id", 1)])
        self.db.ledger_catalog.create_index("expires_at", expireAfterSeconds=0)
        self.db.ledger_relationships.create_index([("kind", 1), ("member_id", 1), ("title_key", 1), ("quest_revision", 1), ("_id", 1)])
        self.db.ledger_relationships.create_index([("kind", 1), ("status", 1), ("grantor", 1), ("at", 1), ("_id", 1)])
        for collection in ("ledger_evidence", "ledger_quests", "ledger_relationships"):
            self.db[collection].create_index([("kind", 1), ("status", 1), ("_id", 1)])
        self.db.ledger_evidence.create_index([("kind", 1), ("status", 1), ("shop_id", 1), ("_id", 1)])
        self.db.ledger_evidence.create_index([("kind", 1), ("status", 1), ("shop_ids", 1), ("_id", 1)])
        self.db.ledger_quests.create_index([("kind", 1), ("status", 1), ("shop_ids", 1), ("_id", 1)])
        self.db.ledger_context.create_index([("channel", 1), ("thread", 1), ("at_order", -1), ("_id", -1)])
        self.db.ledger_context.create_index([("channel", 1), ("at_order", -1), ("_id", -1)])


def field(doc, path, default=None):
    value = doc
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


def project(doc, projection=None):
    if not projection:
        return deepcopy(doc)
    fields = dict.fromkeys(projection, 1) if not isinstance(projection, dict) else projection
    include = any(v for k, v in fields.items() if k != "_id") or fields.get("_id") == 1
    result = {} if include else deepcopy(doc)
    missing = object()
    for path, enabled in fields.items():
        value = field(doc, path, missing)
        if value is missing:
            continue
        target = result
        parts = path.split(".")
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        if enabled:
            target[parts[-1]] = deepcopy(value)
        else:
            target.pop(parts[-1], None)
    if include and fields.get("_id", 1) and "_id" in doc:
        result["_id"] = deepcopy(doc["_id"])
    return result


def sort_rows(rows, sort):
    # Stable passes reproduce compound ordering and Mongo's missing-before-value
    # behavior for the homogeneous application fields used by read pagination.
    for key, direction in reversed(sort or []):
        rows.sort(key=lambda d: (field(d, key) is not None, field(d, key)), reverse=direction < 0)
    return rows


def matches(doc, query):
    for key, val in query.items():
        if key == "$or":
            if not any(matches(doc, branch) for branch in val):
                return False
            continue
        if key == "$and":
            if not all(matches(doc, branch) for branch in val):
                return False
            continue
        actual = field(doc, key)
        if isinstance(val, dict):
            for op, target in val.items():
                if op == "$exists" and (field(doc, key, ...) is not ...) != bool(target):
                    return False
                contained = actual in target or (isinstance(actual, list) and any(v in target for v in actual)) if op in ("$in", "$nin") else False
                if op == "$in" and not contained:
                    return False
                if op == "$nin" and contained:
                    return False
                if op == "$ne" and actual == target:
                    return False
                if op == "$lte" and (actual is None or actual > target):
                    return False
                if op == "$gte" and (actual is None or actual < target):
                    return False
                if op == "$gt" and (actual is None or actual <= target):
                    return False
                if op == "$lt" and (actual is None or actual >= target):
                    return False
                if op == "$type":
                    valid = {"object": isinstance(actual, dict), "string": isinstance(actual, str),
                             "date": isinstance(actual, datetime), "number": isinstance(actual, (int, float))}
                    if not valid.get(target, False):
                        return False
                if op == "$regex" and (not isinstance(actual, str) or not re.search(target, actual, re.I if val.get("$options") == "i" else 0)):
                    return False
        elif actual != val and not (isinstance(actual, list) and val in actual):
            return False
    return True


class MemoryStore:
    """Transactional test double; never an option for production startup."""
    def __init__(self):
        self.data, self.lock = {}, RLock()

    def get(self, collection, key, projection=None):
        doc = self.data.get(owned(collection), {}).get(key)
        return project(doc, projection) if doc is not None else None

    def select(self, collection, query=None, *, projection=None, sort=None, limit=None, max_time_ms=None):
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("Read limits must be positive integers")
        rows = [x for x in self.data.get(owned(collection), {}).values() if matches(x, query or {})]
        sort_rows(rows, sort)
        return [project(x, projection) for x in rows[:limit]]

    def count(self, collection, query=None, *, max_time_ms=None):
        return sum(matches(x, query or {}) for x in self.data.get(owned(collection), {}).values())

    def exists(self, collection, query=None):
        return any(matches(x, query or {}) for x in self.data.get(owned(collection), {}).values())

    def aggregate(self, collection, pipeline, *, max_time_ms=2000):
        from .read_aggregation import aggregate
        return aggregate(self, collection, pipeline)

    def legacy_review_channels(self, collection, destination):
        query = {"review_notice_channel": None, "contributions": {"$exists": True}}
        if collection == "ledger_relationships":
            query["kind"] = "quest_project"
        return [doc for doc in self.select(collection, query)
                if isinstance(doc.get("contributions"), dict) and any(
                    isinstance(c, dict) and c.get("review_channel_id") not in (None, destination)
                    for c in doc["contributions"].values())]

    def put(self, collection, doc):
        from .review_notifications import prepare, watched
        display_keys(collection, doc)
        if watched(collection):
            return self.atomic(lambda s: self._put_review(collection, doc, prepare))
        self.data.setdefault(owned(collection), {})[doc["_id"]] = deepcopy(doc)

    def _put_review(self, collection, doc, prepare):
        prepare(self, collection, doc, self.get(collection, doc["_id"]))
        self.data.setdefault(owned(collection), {})[doc["_id"]] = deepcopy(doc)

    def delete(self, collection, key):
        self.data.get(owned(collection), {}).pop(key, None)

    def put_many(self, collection, documents):
        from .review_notifications import watched
        if watched(collection):
            raise ValueError("Reviewable records require individual transactional preparation")
        for document in documents:
            self.put(collection, document)

    def atomic(self, fn):
        with self.lock:
            previous = deepcopy(self.data)
            try:
                return fn(self)
            except Exception:
                self.data = previous
                raise

    def claim(self, collection, clock=None, kinds=None, exclude=None):
        clock = clock or now()
        with self.lock:
            jobs = self.select(collection, {"status": {"$in": ["pending", "working"]}, "available_at": {"$lte": clock}})
            jobs = [j for j in jobs if (not kinds or j["kind"] in kinds) and (not exclude or j["kind"] not in exclude)]
            if not jobs:
                return None
            job = min(jobs, key=lambda j: j["available_at"])
            job.update(status="working", lease=str(uuid4()), available_at=clock + timedelta(seconds=120), attempts=job.get("attempts", 0) + 1)
            self.put(collection, job)
            return job


def enqueue(store, collection, key, kind, payload, delay=0):
    if not store.get(collection, key):
        stamp = now()
        store.put(collection, {"_id": key, "kind": kind, "payload": payload,
                               "status": "pending", "attempts": 0,
                               "created_at": stamp, "available_at": stamp + timedelta(seconds=delay)})


def connect_database(uri, database=None):
    """Create an independent client; an explicit database overrides the URI path."""
    options = {}
    if os.environ.get("LEDGER_QUERY_METRICS", "false").lower() in ("true", "1", "yes"):
        from .read_metrics import READ_METRICS
        options["event_listeners"] = [READ_METRICS]
    client = MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=2000,
                         connectTimeoutMS=2000, socketTimeoutMS=10000, **options)
    return client[database] if database else client.get_default_database(default="makerauth")


def connect(uri, database=None):
    return MongoStore(connect_database(uri, database))
