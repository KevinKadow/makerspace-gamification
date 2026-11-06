"""Ledger-only writes. Mongo sessions are explicitly passed through every operation."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import RLock
from uuid import uuid4

from pymongo import ASCENDING, MongoClient, ReturnDocument
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern


def now():
    return datetime.now(timezone.utc)


def owned(name):
    if not name.startswith("ledger_"):
        raise ValueError("Writes are restricted to ledger_* collections")
    return name


class MongoStore:
    def __init__(self, database, session=None):
        self.db, self.session = database, session

    def get(self, collection, key):
        return self.db[owned(collection)].find_one({"_id": key}, session=self.session)

    def select(self, collection, query=None):
        return list(self.db[owned(collection)].find(query or {}, session=self.session))

    def put(self, collection, doc):
        self.db[owned(collection)].replace_one({"_id": doc["_id"]}, doc, upsert=True, session=self.session)

    def delete(self, collection, key):
        self.db[owned(collection)].delete_one({"_id": key}, session=self.session)

    def atomic(self, fn):
        if self.session:
            return fn(self)
        with self.db.client.start_session() as session:
            return session.with_transaction(lambda s: fn(MongoStore(self.db, s)),
                                            read_concern=ReadConcern("snapshot"), write_concern=WriteConcern("majority"))

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
        self.db.ledger_evidence.create_index([("recipient", 1), ("kind", 1)])
        self.db.ledger_evidence.create_index([("recipient", 1), ("kind", 1), ("day", 1), ("xp_awarded", 1)])
        self.db.ledger_evidence.create_index([("recipient", 1), ("giver", 1), ("week", 1), ("xp_awarded", 1)])
        self.db.ledger_awards.create_index([("member_id", 1), ("kind", 1)])
        self.db.ledger_context.create_index("expires_at", expireAfterSeconds=0)
        self.db.ledger_relationships.create_index([("recipient", 1), ("kind", 1)])


def matches(doc, query):
    for key, val in query.items():
        actual = doc.get(key)
        if isinstance(val, dict):
            for op, target in val.items():
                if op == "$in" and actual not in target:
                    return False
                if op == "$nin" and actual in target:
                    return False
                if op == "$ne" and actual == target:
                    return False
                if op == "$lte" and (actual is None or actual > target):
                    return False
        elif actual != val:
            return False
    return True


class MemoryStore:
    """Transactional test double; never an option for production startup."""
    def __init__(self):
        self.data, self.lock = {}, RLock()

    def get(self, collection, key):
        return deepcopy(self.data.get(owned(collection), {}).get(key))

    def select(self, collection, query=None):
        return [deepcopy(x) for x in self.data.get(owned(collection), {}).values() if matches(x, query or {})]

    def put(self, collection, doc):
        self.data.setdefault(owned(collection), {})[doc["_id"]] = deepcopy(doc)

    def delete(self, collection, key):
        self.data.get(owned(collection), {}).pop(key, None)

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
        store.put(collection, {"_id": key, "kind": kind, "payload": payload,
                               "status": "pending", "attempts": 0,
                               "available_at": now() + timedelta(seconds=delay)})


def connect_database(uri, database=None):
    """Create an independent client; an explicit database overrides the URI path."""
    client = MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=2000,
                         connectTimeoutMS=2000, socketTimeoutMS=10000)
    return client[database] if database else client.get_default_database(default="makerauth")


def connect(uri, database=None):
    return MongoStore(connect_database(uri, database))
