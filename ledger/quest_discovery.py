"""Bounded quest display reads; mutations still use fresh transactional checks."""
import re
from types import SimpleNamespace
from pymongo import timeout

from .quest_policy import REVIEWED_KINDS, contains_rank_name, cooperative, generated, individual, minimum_rank
from .sources import sid

PAGE_SIZE = 32
DISPLAY_LIMIT = 100
DISPLAY_FIELDS = ("_id", "kind", "logical_id", "creator", "quest_type", "title", "target_rank",
                  "rank_mode", "shop_ids", "tool_ids", "status", "disciplines", "duration", "photo")


def quest_head(q):
    """A display index, never evidence that a revision is still published."""
    return {"_id": "quest-head:" + q["logical_id"], "kind": "quest_head", "logical_id": q["logical_id"],
            "revision": q["_id"], "quest_type": "cooperative" if cooperative(q) else "individual",
            "target_rank": q["target_rank"], "creator": q["creator"], "title": q["title"],
            "title_key": q["title"].casefold()}


def _after(query, cursor, title=False, pointer=None):
    if cursor is None:
        return query
    if title:
        if pointer is None:
            key, identifier = cursor
            return {"$and": [query, {"$or": [{"title_key": {"$gt": key}},
                    {"title_key": key, "_id": {"$gt": identifier}}]}]}
        key, revision, identifier = cursor
        return {"$and": [query, {"$or": [{"title_key": {"$gt": key}},
                {"title_key": key, pointer: {"$gt": revision}},
                {"title_key": key, pointer: revision, "_id": {"$gt": identifier}}]}]}
    return {"$and": [query, {"_id": {"$gt": cursor}}]}


def backfill_quest_heads(store, page_size=PAGE_SIZE):
    """Idempotent, bounded preparation; recheck each pointer inside its transaction."""
    counts = {"heads": 0, "acceptances": 0}
    for collection, query, counter in (("ledger_catalog", {"_id": {"$regex": "^quest-head:"}}, "heads"),
            ("ledger_relationships", {"kind": "quest_acceptance"}, "acceptances")):
        cursor = None
        while True:
            page = store.select(collection, _after(query, cursor), sort=[("_id", 1)], limit=page_size)
            if not page:
                break
            for row in page:
                def update(s):
                    current = s.get(collection, row["_id"])
                    if not current:
                        return False
                    revision = current.get("revision" if counter == "heads" else "quest_revision")
                    q = s.get("ledger_quests", revision) if revision else None
                    if not q or q.get("kind") not in REVIEWED_KINDS:
                        return False
                    fields = quest_head(q) if counter == "heads" else {
                        "title": q["title"], "title_key": q["title"].casefold()}
                    updated = {**current, **fields}
                    if updated == current:
                        return False
                    s.put(collection, updated)
                    return True
                counts[counter] += bool(store.atomic(update))
            cursor = page[-1]["_id"]
    counts["display_titles"] = 0
    for collection, query in (("ledger_catalog", {"kind": "challenge"}),
                              ("ledger_quests", {"status": "open"})):
        cursor = None
        while True:
            page = store.select(collection, _after(query, cursor), projection={"title": 1},
                                sort=[("_id", 1)], limit=page_size, max_time_ms=2000)
            if not page:
                break
            for row in page:
                def update_title(s):
                    current = s.get(collection, row["_id"])
                    if not current or not isinstance(current.get("title"), str):
                        return False
                    key = current["title"].casefold()
                    if current.get("title_key") == key:
                        return False
                    current["title_key"] = key
                    s.put(collection, current)
                    return True
                counts["display_titles"] += bool(store.atomic(update_title))
            cursor = page[-1]["_id"]
    return counts


class QuestDiscovery:
    def __init__(self, ledger, member):
        self.l, self.member = ledger, member
        self.participant, self.rank_display = None, None

    def _initialize(self):
        if self.participant is not None:
            return
        self.participant = self.l.require(self.member)
        self.rank_display = self.l.store.get("ledger_catalog", "rank_display") or {}
        # The policy helper reads only rank_display. Reuse it with this request's
        # already-loaded display, rather than performing one get per candidate.
        self.policy = SimpleNamespace(store=SimpleNamespace(get=lambda *args: self.rank_display))

    def _page(self, collection, query, pointer, cursor, full):
        query = _after(query, cursor, title=True, pointer=pointer)
        projection = {"_id": 1, "title_key": 1, pointer: 1}
        if full:
            projection["quest"] = 1
        else:
            projection.update({"quest." + field: 1 for field in DISPLAY_FIELDS})
        pipeline = [{"$match": query}, {"$sort": {"title_key": 1, pointer: 1, "_id": 1}}, {"$limit": PAGE_SIZE},
                    {"$lookup": {"from": "ledger_quests", "localField": pointer,
                                 "foreignField": "_id", "as": "quest",
                                 "pipeline": [{"$match": {"status": "published"}}]}},
                    {"$unwind": {"path": "$quest", "preserveNullAndEmptyArrays": True}},
                    {"$project": projection}]
        # MemoryStore's generic adapter does not need to implement Mongo joins.
        # Its bounded primary read and batch revision read mirror this pipeline.
        if hasattr(self.l.store, "data"):
            page = self.l.store.select(collection, query, projection={"_id": 1, "title_key": 1, pointer: 1},
                                       sort=[("title_key", 1), (pointer, 1), ("_id", 1)], limit=PAGE_SIZE)
            keys = [row[pointer] for row in page if row.get(pointer)]
            revisions = self.l.store.select("ledger_quests", {"_id": {"$in": keys}, "status": "published"},
                         projection=None if full else dict.fromkeys(DISPLAY_FIELDS, 1))
            by_id = {q["_id"]: q for q in revisions}
            return [{"_id": row["_id"], "title_key": row.get("title_key", ""), pointer: row.get(pointer),
                     "quest": by_id.get(row.get(pointer))} for row in page]
        return self.l.store.aggregate(collection, pipeline)

    def _valid(self, rows, accepted=False, shared=False, full=False):
        quests = [row["quest"] for row in rows if row.get("quest")]
        if not quests:
            return []
        store, source = self.l.store, self.l.sources
        if not full:
            guard_ids = [q["_id"] for q in quests if generated(q)]
            if guard_ids:
                guards = {q["_id"]: q for q in store.select("ledger_quests", {"_id": {"$in": guard_ids}},
                    projection=dict.fromkeys(("_id", "description", "criteria"), 1))}
                quests = [{**q, **guards.get(q["_id"], {})} for q in quests
                          if not generated(q) or q["_id"] in guards]
        authors = sorted({q["creator"] for q in quests if not generated(q)})
        participants = {p["_id"]: p for p in store.select("ledger_participants", {"_id": {"$in": authors}},
                        projection={"_id": 1, "rank": 1, "ruleset": 1})} if authors else {}
        author_source = source.identities(authors) if authors else {}
        identities = {d["_id"]: d for d in store.select("ledger_catalog",
            {"_id": {"$in": ["identity:" + author for author in authors]}},
            projection={"_id": 1, "deactivated": 1, "bot": 1})} if authors else {}
        rules = {r["_id"]: r for r in store.select("ledger_rulesets",
            {"_id": {"$in": sorted({p["ruleset"] for p in participants.values()})}},
            projection={"_id": 1, "ranks": 1})} if participants else {}
        tool_ids = sorted({sid(tool) for q in quests for tool in q.get("tool_ids", [])})
        tools = source.tools_by_id(tool_ids, fields=("shop_id", "disabled", "out_of_service")) if tool_ids else {}
        shop_ids = sorted({sid(shop) for q in quests for shop in q.get("shop_ids", [])} |
                          {sid(tool["shop_id"]) for tool in tools.values() if tool.get("shop_id")})
        shops = source.shops_by_id(shop_ids, fields=("disabled", "out_of_service")) if shop_ids else {}
        cleared = {sid(c["tool_id"]) for c in source.clearances(self.member, tool_ids)} if tool_ids else set()
        logicals = sorted({q["logical_id"] for q in quests})
        completed = {d["_id"] for d in store.select("ledger_evidence",
            {"_id": {"$in": [f"quest-complete:{self.member}:{logical}" for logical in logicals]}},
            projection={"_id": 1})}
        pinned = set()
        if not accepted and not shared:
            pinned = {a["logical_id"] for a in store.select("ledger_relationships",
                {"_id": {"$in": [f"acceptance:{self.member}:{logical}" for logical in logicals]}},
                projection={"_id": 1, "logical_id": 1})}
        projects = {p["_id"]: p for p in store.select("ledger_relationships",
            {"_id": {"$in": ["cooperative:" + logical for logical in logicals]}},
            projection={"_id": 1, "status": 1, "quest_revision": 1})} if shared else {}
        enabled = {r["slot"] for r in self.rank_display.get("ranks", []) if r.get("enabled")}
        valid = []
        for q in quests:
            if q.get("status") != "published":
                continue
            if shared:
                project = projects.get("cooperative:" + q["logical_id"], {})
                if not cooperative(q) or project.get("status") != "open" or project.get("quest_revision") != q["_id"]:
                    continue
                if not generated(q):
                    rank_ineligible = (self.participant["rank"] < q["target_rank"] if minimum_rank(q)
                                       else self.participant["rank"] != q["target_rank"])
                    if rank_ineligible:
                        continue
            elif (not individual(q) or (self.member == q["creator"] and not minimum_rank(q)) or
                  (not accepted and (q["logical_id"] in pinned or
                   (self.participant["rank"] < q["target_rank"] if minimum_rank(q)
                    else q["target_rank"] != self.participant["rank"]))) or
                  f"quest-complete:{self.member}:{q['logical_id']}" in completed):
                continue
            if generated(q):
                if type(q.get("target_rank")) is not int or q["target_rank"] not in enabled or contains_rank_name(self.policy, q):
                    continue
            else:
                p, m = participants.get(q["creator"]), author_source.get(q["creator"])
                identity = identities.get("identity:" + q["creator"], {})
                author_limit = (p["rank"] if minimum_rank(q) else p["rank"] - 2) if p else -1
                if (not p or not m or not m.get("slack_id") or m.get("status") in ("suspended", "revoked") or
                    identity.get("deactivated") or identity.get("bot") or q["target_rank"] > author_limit or
                    not any(r["slot"] == q["target_rank"] and r["enabled"] for r in rules.get(p["ruleset"], {}).get("ranks", []))):
                    continue
            if any(not shops.get(sid(shop)) or shops[sid(shop)].get("disabled") or
                   shops[sid(shop)].get("out_of_service") for shop in q.get("shop_ids", [])):
                continue
            failed = False
            for identifier in q.get("tool_ids", []):
                tool = tools.get(sid(identifier))
                shop = shops.get(sid(tool.get("shop_id"))) if tool else None
                if (not tool or tool.get("disabled") or tool.get("out_of_service") or not shop
                        or shop.get("disabled") or shop.get("out_of_service") or sid(identifier) not in cleared):
                    failed = True
                    break
            if not failed:
                valid.append(q)
        return valid

    def _stream(self, query, collection="ledger_catalog", pointer="revision", accepted=False, shared=False, full=False,
                limit=DISPLAY_LIMIT):
        cursor, eligible = None, []
        while len(eligible) < limit:
            page = self._page(collection, query, pointer, cursor, full)
            if not page:
                break
            eligible.extend(self._valid(page, accepted=accepted, shared=shared, full=full))
            cursor = (page[-1].get("title_key", ""), page[-1][pointer], page[-1]["_id"])
            if len(page) < PAGE_SIZE:
                break
        return eligible[:limit]

    def listing(self, search="", full=True):
        with timeout(2):
            self._initialize()
            return self._listing(search, full)

    def _listing(self, search, full):
        title_filter = {"$regex": re.escape(search.casefold())}
        heads = self._stream({"kind": "quest_head", "quest_type": "individual",
            "target_rank": {"$lte": self.participant["rank"]}, "title_key": title_filter}, full=full)
        accepted = self._stream({"kind": "quest_acceptance", "member_id": self.member,
            "title_key": title_filter}, "ledger_relationships", "quest_revision", accepted=True, full=full)
        by_logical = {q["logical_id"]: q for q in heads}
        by_logical.update({q["logical_id"]: q for q in accepted})
        return sorted(by_logical.values(), key=lambda q: (q["title"].casefold(), q["_id"]))[:DISPLAY_LIMIT]

    def _simple_options(self, collection, query, prefix, search):
        cursor, result = None, []
        query = {**query, "title_key": {"$regex": re.escape(search.casefold())}}
        while len(result) < DISPLAY_LIMIT:
            page = self.l.store.select(collection, _after(query, cursor, title=True),
                projection={"_id": 1, "title": 1, "creator": 1, "kind": 1, "status": 1},
                sort=[("title_key", 1), ("_id", 1)], limit=PAGE_SIZE, max_time_ms=2000)
            if not page:
                break
            result.extend((prefix + ":" + row["_id"], row["title"]) for row in page
                          if search.casefold() in row["title"].casefold() and
                          (prefix != "g" or (row.get("kind") not in REVIEWED_KINDS and row.get("creator") != self.member)))
            cursor = (page[-1]["title"].casefold(), page[-1]["_id"])
            if len(page) < PAGE_SIZE:
                break
        return result[:DISPLAY_LIMIT]

    def options(self, search=""):
        with timeout(2):
            self._initialize()
            return self._options(search)

    def _options(self, search):
        result = [("q:" + q["_id"], q["title"]) for q in self._listing(search, full=False)]
        shared = self._stream({"kind": "quest_head", "quest_type": "cooperative",
            "title_key": {"$regex": re.escape(search.casefold())}}, shared=True)
        result.extend(("g:" + q["_id"], q["title"]) for q in shared)
        result.extend(self._simple_options("ledger_catalog", {"kind": "challenge", "active": True}, "c", search))
        result.extend(self._simple_options("ledger_quests", {"status": "open", "kind": {"$nin": list(REVIEWED_KINDS)}}, "g", search))
        return sorted(result, key=lambda r: (r[1].casefold(), r[0]))[:DISPLAY_LIMIT]
