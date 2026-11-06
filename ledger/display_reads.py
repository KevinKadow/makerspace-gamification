"""Request-local read contexts for admin views; never used to commit decisions."""
from copy import deepcopy
from bson import json_util

from .domain import Ledger
from .sources import object_id, sid
from .storage import matches, project


class DisplayStore:
    def __init__(self, store):
        self.store, self.documents, self.queries = store, {}, {}

    def warm(self, collection, keys):
        keys = list({key for key in keys if key is not None and (collection, key) not in self.documents})
        for start in range(0, len(keys), 200):
            batch = keys[start:start + 200]
            rows = self.store.select(collection, {"_id": {"$in": batch}}, max_time_ms=2000)
            self.documents.update({(collection, key): None for key in batch})
            self.documents.update({(collection, row["_id"]): row for row in rows})

    def get(self, collection, key, projection=None):
        if (collection, key) not in self.documents:
            self.documents[(collection, key)] = self.store.get(collection, key)
        value = self.documents[(collection, key)]
        return project(value, projection) if value is not None else None

    def select(self, collection, query=None, **options):
        key = json_util.dumps([collection, query, options], sort_keys=True)
        if key not in self.queries:
            self.queries[key] = self.store.select(collection, query, **options)
        return deepcopy(self.queries[key])

    def aggregate(self, collection, pipeline, **options):
        return self.store.aggregate(collection, pipeline, **options)


class DisplaySources:
    def __init__(self, source):
        self.source, self.people, self.tools, self.shops, self.checkouts = source, {}, {}, {}, {}

    def warm_people(self, members):
        missing = {sid(m) for m in members if m is not None} - self.people.keys()
        self.people.update({m: None for m in missing})
        self.people.update(self.source.identities(missing))

    def member(self, member):
        self.warm_people([member])
        return deepcopy(self.people.get(sid(member)))

    def slack_id(self, member):
        return (self.member(member) or {}).get("slack_id")

    def permitted(self, member):
        doc = self.member(member)
        return bool(doc and doc.get("status") not in ("suspended", "revoked"))

    def good_standing(self, member):
        return (self.member(member) or {}).get("status") in ("activeMember", "pending")

    def role(self, member):
        return (self.member(member) or {}).get("role", "member")

    def warm_quests(self, quests, members=()):
        required = {sid(i) for q in quests for i in q.get("tool_ids", [])}
        tool_ids = required - self.tools.keys()
        self.tools.update({i: None for i in tool_ids})
        self.tools.update(self.source.tools_by_id(tool_ids, ["shop_id", "disabled", "out_of_service"]))
        shop_ids = ({sid(i) for q in quests for i in q.get("shop_ids", [])} |
                    {sid(t.get("shop_id")) for t in self.tools.values() if t and t.get("shop_id")}) - self.shops.keys()
        self.shops.update({i: None for i in shop_ids})
        self.shops.update(self.source.shops_by_id(shop_ids, ["disabled", "out_of_service"]))
        people = {sid(m) for m in members}
        if people:
            self.checkouts.update({m: self.checkouts.get(m, []) for m in people})
        if people and required:
            query = {"member_id": {"$in": [object_id(m) for m in people]}, "tool_id": {"$in": [object_id(t) for t in required]}, "revoked_at": None}
            if hasattr(self.source, "data"):
                rows = [{k: r[k] for k in ("_id", "member_id", "tool_id") if k in r} for r in self.source.rows("tool_checkouts", query)]
            else:
                rows = self.source._aggregate("tool_checkouts", [{"$match": query}, {"$project": {"member_id": 1, "tool_id": 1}}])
            for row in rows:
                found = self.checkouts.setdefault(sid(row["member_id"]), [])
                if not any(r["_id"] == row["_id"] for r in found):
                    found.append(row)

    def shop(self, identifier):
        key = sid(identifier)
        if key not in self.shops:
            self.shops[key] = self.source.shop(identifier)
        return deepcopy(self.shops[key])

    def tool(self, identifier):
        key = sid(identifier)
        if key not in self.tools:
            self.tools[key] = self.source.tool(identifier)
        return deepcopy(self.tools[key])

    def rows(self, name, query=None):
        query = query or {}
        member = query.get("member_id")
        if name == "tool_checkouts" and member is not None and not isinstance(member, dict) and sid(member) in self.checkouts:
            return [deepcopy(row) for row in self.checkouts[sid(member)] if matches(row, query)]
        return self.source.rows(name, query)


class AdminDisplay:
    def __init__(self, ledger, actor):
        from .authority import Authority
        self.original, self.actor = ledger, actor
        self.store, self.source = DisplayStore(ledger.store), DisplaySources(ledger.sources)
        self.ledger = Ledger(self.store, self.source)
        self.authority = Authority(self.ledger)
        grants = self.store.select("ledger_relationships", {"kind": "delegation", "delegate": actor, "status": "active"})
        self.grants = grants
        people = {actor} | {g["grantor"] for g in grants}
        self.warm_people(people)
        self.store.warm("ledger_catalog", ["control", "rank_display"])
        self.store.warm("ledger_quests", [g.get("quest_revision") or g["scope"].get("quest") for g in grants])
        self.scopes = []
        scope = self.authority.staff_scope(actor)
        if scope:
            self.scopes.append(scope)
        for grant in grants:
            if self.authority.grant_valid(grant):
                scope = grant["scope"]
                if scope["kind"] == "quest":
                    q = self.store.get("ledger_quests", grant.get("quest_revision") or scope["quest"])
                    if q:
                        scope = {"kind": "quest", "quest": q.get("logical_id", q["_id"])}
                self.scopes.append(scope)

    def warm_people(self, people):
        self.source.warm_people(people)
        self.store.warm("ledger_participants", people)
        self.store.warm("ledger_catalog", ["identity:" + p for p in people])

    def joined_pages(self, collection, query, pointer, projection=None):
        """Filter referenced quest scope on the server before returning bodies."""
        choices = []
        if not any(s["kind"] == "global" for s in self.scopes):
            shops = sorted({shop for s in self.scopes if s["kind"] == "shops" for shop in s["shops"]})
            logicals = [s["quest"] for s in self.scopes if s["kind"] == "quest"]
            if shops:
                choices.extend([{"quest.shop_id": {"$in": shops}}, {"quest.shop_ids": {"$in": shops}}])
            if logicals:
                choices.append({"quest.logical_id": {"$in": logicals}})
                choices.append({"quest._id": {"$in": logicals}})
            if not choices:
                return
        cursor = None
        while True:
            current = query if cursor is None else {"$and": [query, {"_id": {"$gt": cursor}}]}
            pipeline = [{"$match": current}, {"$sort": {"_id": 1}},
                {"$lookup": {"from": "ledger_quests", "localField": pointer, "foreignField": "_id", "as": "quest"}},
                {"$unwind": "$quest"}]
            if choices:
                pipeline.append({"$match": {"$or": choices}})
            pipeline.append({"$limit": 32})
            if projection:
                pipeline.append({"$project": projection})
            page = self.original.store.aggregate(collection, pipeline, max_time_ms=2000)
            if not page:
                return
            yield page
            if len(page) < 32:
                return
            cursor = page[-1]["_id"]

    def pages(self, collection, query, *, projection=None, scope=True):
        if scope and not any(s["kind"] == "global" for s in self.scopes):
            shops = sorted({shop for s in self.scopes if s["kind"] == "shops" for shop in s["shops"]})
            logicals = [s["quest"] for s in self.scopes if s["kind"] == "quest"]
            choices = []
            if shops:
                choices.extend([{"shop_id": {"$in": shops}}, {"shop_ids": {"$in": shops}}])
            if logicals:
                choices.extend([{"logical_id": {"$in": logicals}}, {"quest_link": {"$in": logicals}}])
                if collection == "ledger_quests":
                    choices.append({"_id": {"$in": logicals}})
                # Completion records reference revisions instead of logical IDs.
                if collection == "ledger_evidence":
                    revisions = self.original.store.select("ledger_quests", {"logical_id": {"$in": logicals}}, projection={"_id": 1})
                    choices.append({"quest_revision": {"$in": [q["_id"] for q in revisions]}})
            if not choices:
                return
            # Quest submissions carry their scope on the referenced definition.
            # Join/filter those later instead of incorrectly excluding them here.
            if query.get("kind") != "quest_submission":
                query = {"$and": [query, {"$or": choices}]}
        cursor = None
        while True:
            current = query if cursor is None else {"$and": [query, {"_id": {"$gt": cursor}}]}
            page = self.original.store.select(collection, current, projection=projection,
                                              sort=[("_id", 1)], limit=32, max_time_ms=2000)
            if not page:
                return
            yield page
            if len(page) < 32:
                return
            cursor = page[-1]["_id"]

    def pending_reviews(self):
        from .authority import evidence_capability
        from .domain import Denied
        from .quest_policy import REVIEWED_KINDS
        lines = []
        for page in self.pages("ledger_evidence", {"kind": "submission", "status": "pending"}, projection=dict.fromkeys(
                ("member_id", "shop_id", "shop_ids", "quest_link", "achievement", "description"), 1)):
            for row in page:
                if row.get("quest_link"):
                    continue
                try:
                    self.authority.authorize(self.actor, row["member_id"], evidence_capability(row),
                        row.get("shop_ids") or [row.get("shop_id")], row.get("quest_link"))
                    lines.append(f"{row['_id']}: {row['achievement']} — {row['description']}")
                except Denied:
                    continue
            if len(lines) >= 100:
                break
        ordinary = "\n".join(lines[:100]) or "No pending submissions you can review."
        publication = []
        for page in self.pages("ledger_quests", {"kind": {"$in": list(REVIEWED_KINDS)}, "status": "pending_review"},
                projection=dict.fromkeys(("creator", "shop_ids", "logical_id", "title"), 1)):
            for q in page:
                try:
                    self.authority.authorize(self.actor, q["creator"], "quest_publish", q["shop_ids"], q["logical_id"])
                    publication.append(f"Quest publication: {q['_id']} — {q['title']}; use /ledger-admin publish-quest {q['_id']} or reject-quest {q['_id']}.")
                except Denied:
                    continue
            if len(publication) >= 100:
                break
        # Use separate counters so no candidate limit is applied before coverage.
        completion = []
        for page in self.joined_pages("ledger_evidence", {"kind": "quest_submission", "status": "pending"}, "quest_revision",
                projection={"member_id": 1, "description": 1, "quest.creator": 1, "quest.shop_ids": 1, "quest.logical_id": 1}):
            for doc in page:
                q = doc["quest"]
                try:
                    self.authority.authorize(self.actor, doc["member_id"], "quest_complete", q["shop_ids"], q["logical_id"], excluded=[q["creator"]])
                    completion.append(f"Quest completion: {doc['_id']} — {doc['description']}")
                except Denied:
                    continue
            if len(completion) >= 100:
                break
        return ordinary + "".join("\n" + line for line in publication[:100] + completion[:100])
