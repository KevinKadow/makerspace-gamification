"""Read-only projections over the Rails-owned Mongo collections."""
from bson import ObjectId
from pymongo import timeout
import re
import time
from .storage import matches, now
from .source_reads import (CATALOG_FIELDS, SKILL_SHOP_FIELDS, SKILL_TOOL_FIELDS, catalog_pipeline,
                           identity_pipeline, member_identity_pipeline, projection, skill_ancestor_pipeline, skill_graph_pipeline,
                           unique_mapping_pipeline)
from .read_options import optimized_reads

FIELDS = {
    "members": "firstname lastname status expirationTime subscription subscription_id groupName merged_at role resource_manager_shop_ids",
    "slack_users": "member_id slack_id invalidated_at",
    "shops": "name disabled wiki_url out_of_service out_of_service_note",
    "tools": "name description wiki_url shop_id prerequisite_ids disabled open out_of_service",
    "tool_checkouts": "member_id tool_id approved_by_id checked_out_at revoked_at volunteer_credit_id",
    "checkout_approvers": "member_id shop_ids tool_ids",
    "volunteer_credits": "member_id credit_value status tool_checkout_id task_id created_at reversed reversal_of_id",
    "volunteer_tasks": "title description shop_id status prerequisite_tool_ids claimed_by_id verified_by_id completed_at next_available days parent_task_id",
    "volunteer_events": "title description shop_id status event_date prerequisite_tool_ids",
    "checkins": "uid time timeOf",
    "cards": "uid member_id validity",
    "earned_memberships": "member_id status",
    "groups": "groupName subscription subscription_id expiry",
    # Ticket documents are read only for verification quests. Keep these
    # projections narrow; notes and private attribution are never sent to AI.
    "fix_tickets": "reporter_id category status created_at updated_at revision slack_ticket_ts slack_ticket_channel_id",
    "fix_ticket_events": "ticket_id actor_id kind field_changes revision created_at completed_at",
}


def object_id(value):
    if isinstance(value, str) and ObjectId.is_valid(value):
        return ObjectId(value)
    return value


def sid(value):
    return str(value) if value is not None else None


class Sources:
    def __init__(self, database):
        self.db = database

    def ready(self):
        self.db.command("ping")

    def rows(self, name, query=None):
        if name not in FIELDS:
            raise ValueError("Source collection is not allowlisted")
        projection = dict.fromkeys(FIELDS[name].split(), 1)
        with timeout(2):
            return list(self.db[name].find(query or {}, projection).max_time_ms(2000))

    def bounded(self, name, query, fields, limit, timeout_ms=2000):
        if name not in FIELDS or set(fields) - (set(FIELDS[name].split()) | {"_id"}):
            raise ValueError("Source projection is not allowlisted")
        with timeout(timeout_ms / 1000):
            return list(self.db[name].find(query, projection(fields)).sort("_id", 1).limit(limit).max_time_ms(timeout_ms))

    def _aggregate(self, name, pipeline, timeout_ms=2000):
        if name not in FIELDS:
            raise ValueError("Source collection is not allowlisted")
        with timeout(timeout_ms / 1000):
            return list(self.db[name].aggregate(pipeline, maxTimeMS=timeout_ms, allowDiskUse=False))

    def by_ids(self, name, identifiers, fields=None):
        if name not in FIELDS:
            raise ValueError("Source collection is not allowlisted")
        selected = FIELDS[name].split() if fields is None else fields.split() if isinstance(fields, str) else list(fields)
        if set(selected) - (set(FIELDS[name].split()) | {"_id"}):
            raise ValueError("Source projection is not allowlisted")
        identifiers = list({object_id(value) for value in identifiers if value is not None})
        if not identifiers:
            return {}
        query = {"_id": {"$in": identifiers}}
        if name == "members":
            query["merged_at"] = None
        rows = self.bounded(name, query, selected, len(identifiers))
        return {sid(row["_id"]): row for row in rows}

    def members_by_id(self, member_ids, fields=None):
        return self.by_ids("members", member_ids, fields)

    def tools_by_id(self, tool_ids, fields=None):
        return self.by_ids("tools", tool_ids, fields)

    def shops_by_id(self, shop_ids, fields=None):
        return self.by_ids("shops", shop_ids, fields)

    def slack_ids(self, member_ids):
        identifiers = list({object_id(value) for value in member_ids if value is not None})
        if not identifiers:
            return {}
        if not optimized_reads():
            return {sid(value): uid for value in identifiers if (uid := self.slack_id(value))}
        return {sid(row["_id"]): row["slack_id"] for row in self._aggregate("slack_users", unique_mapping_pipeline(identifiers))
                if isinstance(row.get("slack_id"), str) and re.fullmatch(r"[UW][A-Z0-9]+", row["slack_id"])}

    def identities(self, member_ids):
        """Current member facts with a valid, unique mapping in both directions."""
        identifiers = list({object_id(value) for value in member_ids if value is not None})
        if not identifiers:
            return {}
        if not optimized_reads():
            result = {}
            for value in identifiers:
                member = self.member(value)
                uid = self.slack_id(value)
                if member and uid:
                    result[sid(value)] = {**member, "slack_id": uid}
            return result
        rows = self._aggregate("members", member_identity_pipeline(identifiers, FIELDS["members"]))
        return {sid(row["_id"]): row for row in rows
                if isinstance(row.get("slack_id"), str) and re.fullmatch(r"[UW][A-Z0-9]+", row["slack_id"])}

    def clearances(self, member_id, tool_ids=None):
        query = {"member_id": object_id(member_id), "revoked_at": None}
        if tool_ids is not None:
            identifiers = list({object_id(value) for value in tool_ids if value is not None})
            if not identifiers:
                return []
            query["tool_id"] = {"$in": identifiers}
        if not optimized_reads():
            return self.rows("tool_checkouts", query)
        with timeout(2):
            return list(self.db.tool_checkouts.find(query, {"tool_id": 1, "checked_out_at": 1}).max_time_ms(2000))

    def catalog_query(self, collection, **filters):
        timeout_ms = filters.pop("timeout_ms", 2000)
        for key in ("shop_id", "tool_id", "member_id"):
            if filters.get(key) is not None:
                filters[key] = object_id(filters[key])
        return self._aggregate(collection, catalog_pipeline(collection, **filters), timeout_ms)

    def skill_graph(self, member_id):
        deadline = time.monotonic() + 2
        def remaining():
            duration = deadline - time.monotonic()
            if duration <= 0:
                raise TimeoutError("Skill graph read deadline exceeded")
            return max(1, int(duration * 1000))
        cleared, shops, tools, attempted = set(), {}, {}, set()
        with timeout(2):
            rows = self._skill_graph_rows(member_id=object_id(member_id), timeout_ms=remaining())
            while True:
                for original in rows:
                    row = dict(original)
                    shop = row.pop("_shop")
                    if row.pop("_cleared", False):
                        cleared.add(sid(row["_id"]))
                    shops[sid(shop["_id"])] = shop
                    tools[sid(row["_id"])] = row
                # Rails stores prerequisite IDs as strings as well as ObjectIds.
                # graphLookup has already attempted every native-ID edge; only
                # unresolved strings need normalization and another batch read.
                frontier = {value for tool in tools.values() for value in tool.get("prerequisite_ids", [])
                            if isinstance(value, str)} - tools.keys() - attempted
                remaining()
                if not frontier:
                    break
                attempted.update(frontier)
                rows = self._skill_graph_rows(tool_ids=[object_id(value) for value in sorted(frontier)],
                                              timeout_ms=remaining())
        return {"cleared": cleared, "shops": shops, "tools": tools}

    def _skill_graph_rows(self, *, member_id=None, tool_ids=None, timeout_ms=2000):
        if member_id is not None:
            return self._aggregate("tool_checkouts", skill_graph_pipeline(member_id), timeout_ms)
        return self._aggregate("tools", skill_ancestor_pipeline(tool_ids), timeout_ms)

    def skill_catalog(self, search=""):
        # Match casefold in Python to retain the established Unicode semantics.
        # Catalog labels are narrow projections, tools/prerequisites are batched.
        shops = self._projected_rows("shops", {"disabled": {"$ne": True}}, SKILL_SHOP_FIELDS)
        shops = [shop for shop in shops if search.casefold() in shop.get("name", "").casefold() or search == sid(shop["_id"])]
        tools = self._projected_rows("tools", {"disabled": {"$ne": True}, "shop_id": {"$in": [shop["_id"] for shop in shops]}}, SKILL_TOOL_FIELDS) if shops else []
        names = self.tools_by_id({value for tool in tools for value in tool.get("prerequisite_ids", [])}, ["name"])
        return {"shops": shops, "tools": tools, "prerequisite_tools": list(names.values())}

    def _projected_rows(self, name, query, fields):
        selected = fields.split() if isinstance(fields, str) else fields
        if name not in FIELDS or set(selected) - (set(FIELDS[name].split()) | {"_id"}):
            raise ValueError("Source projection is not allowlisted")
        with timeout(2):
            return list(self.db[name].find(query, projection(selected)).sort("_id", 1).max_time_ms(2000))

    def member(self, member_id):
        if optimized_reads():
            return self.members_by_id([member_id]).get(sid(object_id(member_id)))
        rows = self.rows("members", {"_id": object_id(member_id), "merged_at": None})
        return rows[0] if rows else None

    def identity(self, slack_id):
        if optimized_reads():
            if not isinstance(slack_id, str) or not re.fullmatch(r"[UW][A-Z0-9]+", slack_id):
                return None
            rows = self._identity_rows(slack_id)
            return rows[0] if rows else None
        rows = self.rows("slack_users", {"slack_id": slack_id, "invalidated_at": None})
        if len(rows) != 1 or not rows[0].get("member_id"):
            return None
        member_id = rows[0]["member_id"]
        return self.member(member_id) if self.slack_id(member_id) == slack_id else None

    def slack_id(self, member_id):
        if optimized_reads():
            return self.slack_ids([member_id]).get(sid(object_id(member_id)))
        rows = self.rows("slack_users", {"member_id": object_id(member_id), "invalidated_at": None})
        uid = rows[0].get("slack_id") if len(rows) == 1 else None
        return uid if isinstance(uid, str) and re.fullmatch(r"[UW][A-Z0-9]+", uid) and len(self.rows("slack_users", {"slack_id": uid, "invalidated_at": None})) == 1 else None

    def good_standing(self, member_id):
        if optimized_reads():
            m = self.identities([member_id]).get(sid(object_id(member_id)))
            return bool(m and m.get("status") in ("activeMember", "pending"))
        m = self.member(member_id)
        return bool(m and m.get("status") in ("activeMember", "pending") and self.slack_id(member_id))

    def permitted(self, member_id):
        if optimized_reads():
            m = self.identities([member_id]).get(sid(object_id(member_id)))
            return bool(m and m.get("status") not in ("suspended", "revoked"))
        m = self.member(member_id)
        return bool(m and m.get("status") not in ("suspended", "revoked") and self.slack_id(member_id))

    def role(self, member_id):
        return (self.member(member_id) or {}).get("role", "member")

    def eligible_for_rank(self, member_id, attestation=None):
        m = self.member(member_id)
        if not m or m.get("status") not in ("activeMember", "pending"):
            return False
        expiry = m.get("expirationTime") or 0
        if expiry <= int(now().timestamp() * 1000):
            return False
        verified_prepaid = attestation and attestation.get("expiration") == expiry and attestation.get("approved")
        if optimized_reads() and (m.get("subscription") or m.get("subscription_id") or verified_prepaid):
            return True
        if optimized_reads() and self.bounded("earned_memberships", {"member_id": m["_id"], "status": "active"}, [], 1):
            return True
        if optimized_reads():
            groups = self._projected_rows("groups", {"groupName": m.get("groupName")}, ["subscription", "subscription_id"]) if m.get("groupName") else []
            return any(group.get("subscription") or group.get("subscription_id") for group in groups)
        earned = self.rows("earned_memberships", {"member_id": m["_id"], "status": "active"})
        groups = self.rows("groups", {"groupName": m.get("groupName")}) if m.get("groupName") else []
        paid = m.get("subscription") or m.get("subscription_id") or any(
            g.get("subscription") or g.get("subscription_id") for g in groups)
        return bool(paid or earned or verified_prepaid)

    def tool(self, tool_id):
        if optimized_reads():
            return self.tools_by_id([tool_id]).get(sid(object_id(tool_id)))
        rows = self.rows("tools", {"_id": object_id(tool_id)})
        return rows[0] if rows else None

    def shop(self, shop_id):
        if optimized_reads():
            return self.shops_by_id([shop_id]).get(sid(object_id(shop_id)))
        rows = self.rows("shops", {"_id": object_id(shop_id)})
        return rows[0] if rows else None

    def _identity_rows(self, slack_id):
        return self._aggregate("slack_users", identity_pipeline(slack_id, FIELDS["members"]))


class MemorySources(Sources):
    def __init__(self, data):
        self.data = data

    def rows(self, name, query=None):
        if name not in FIELDS:
            raise ValueError("Source collection is not allowlisted")
        allowed = set(FIELDS[name].split()) | {"_id"}
        return [{k: v for k, v in r.items() if k in allowed} for r in self.data.get(name, []) if matches(r, query or {})]

    def bounded(self, name, query, fields, limit, timeout_ms=2000):
        if name not in FIELDS or set(fields) - (set(FIELDS[name].split()) | {"_id"}):
            raise ValueError("Source projection is not allowlisted")
        return [{k: v for k, v in r.items() if k in set(fields) | {"_id"}} for r in sorted(self.rows(name, query), key=lambda r: str(r["_id"]))[:limit]]

    def _projected_rows(self, name, query, fields):
        selected = fields.split() if isinstance(fields, str) else fields
        if name not in FIELDS or set(selected) - (set(FIELDS[name].split()) | {"_id"}):
            raise ValueError("Source projection is not allowlisted")
        return [{k: v for k, v in row.items() if k in set(selected) | {"_id"}}
                for row in sorted(self.rows(name, query), key=lambda row: sid(row["_id"]))]

    def slack_ids(self, member_ids):
        if not optimized_reads():
            return super().slack_ids(member_ids)
        identifiers = {object_id(value) for value in member_ids if value is not None}
        links = [row for row in self.data.get("slack_users", []) if row.get("invalidated_at") is None]
        result = {}
        for member_id in identifiers:
            rows = [row for row in links if row.get("member_id") == member_id]
            uid = rows[0].get("slack_id") if len(rows) == 1 else None
            if (isinstance(uid, str) and re.fullmatch(r"[UW][A-Z0-9]+", uid)
                    and sum(row.get("slack_id") == uid for row in links) == 1):
                result[sid(member_id)] = uid
        return result

    def identities(self, member_ids):
        if not optimized_reads():
            return super().identities(member_ids)
        identifiers = list(member_ids)
        members = {sid(row["_id"]): row for row in self.rows("members", {
            "_id": {"$in": [object_id(value) for value in identifiers]}, "merged_at": None})}
        return {key: {**members[key], "slack_id": uid} for key, uid in self.slack_ids(identifiers).items() if key in members}

    def _identity_rows(self, slack_id):
        links = [row for row in self.data.get("slack_users", []) if row.get("slack_id") == slack_id and row.get("invalidated_at") is None]
        if len(links) != 1 or links[0].get("member_id") is None:
            return []
        member_id = links[0]["member_id"]
        members = self.rows("members", {"_id": member_id, "merged_at": None})
        member = members[0] if members else None
        return [member] if member and self.slack_ids([member_id]).get(sid(member_id)) == slack_id else []

    def clearances(self, member_id, tool_ids=None):
        query = {"member_id": object_id(member_id), "revoked_at": None}
        if tool_ids is not None:
            query["tool_id"] = {"$in": [object_id(value) for value in tool_ids]}
        return self._projected_rows("tool_checkouts", query, ["tool_id", "checked_out_at"])

    def catalog_query(self, collection, **filters):
        timeout_ms = filters.pop("timeout_ms", 2000)
        for key in ("shop_id", "tool_id", "member_id"):
            if filters.get(key) is not None:
                filters[key] = object_id(filters[key])
        pipeline = catalog_pipeline(collection, **filters)
        candidates = self.rows(collection, pipeline[0]["$match"])
        shops = {row["_id"] for row in self.data.get("shops", []) if not row.get("disabled")}
        tools = {row["_id"]: row for row in self.data.get("tools", [])}
        if collection == "tools":
            candidates = [row for row in candidates if row.get("shop_id") in shops]
        elif collection == "tool_checkouts":
            def permitted_tool(row):
                tool = tools.get(row.get("tool_id"))
                if not tool or tool.get("disabled") or tool.get("shop_id") not in shops:
                    return False
                if filters.get("shop_id") is not None and tool.get("shop_id") != filters["shop_id"]:
                    return False
                if filters.get("search") and not re.search(re.escape(filters["search"]), tool.get("name", ""), re.I):
                    return False
                oos = filters.get("out_of_service")
                return oos is None or (tool.get("out_of_service") is True if oos else tool.get("out_of_service") is not True)
            candidates = [row for row in candidates if permitted_tool(row)]
        elif collection in ("volunteer_tasks", "volunteer_events"):
            candidates = [row for row in candidates if row.get("shop_id") is None or row.get("shop_id") in shops]
        return self.bounded(collection, {"_id": {"$in": [row["_id"] for row in candidates]}},
                            CATALOG_FIELDS[collection].split(), filters.get("limit", 11), timeout_ms)

    def _skill_graph_rows(self, *, member_id=None, tool_ids=None, timeout_ms=2000):
        # Simulate BSON equality rather than sid equality, so tests exercise the
        # same string-ID repair batches that Mongo graphLookup requires.
        deadline = time.monotonic() + timeout_ms / 1000
        seeds = {row["tool_id"] for row in self.clearances(member_id)} if member_id is not None else set(tool_ids)
        all_tools = {row["_id"]: row for row in self.data.get("tools", []) if not row.get("disabled")}
        fields, shop_fields = set(SKILL_TOOL_FIELDS.split()) | {"_id"}, set(SKILL_SHOP_FIELDS.split()) | {"_id"}
        shops = {row["_id"]: {k: v for k, v in row.items() if k in shop_fields}
                 for row in self.data.get("shops", []) if not row.get("disabled")}
        rows = {}
        for seed in seeds:
            visited, pending = set(), [seed]
            while pending:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Skill graph read deadline exceeded")
                identifier = pending.pop()
                if identifier in visited or identifier not in all_tools:
                    continue
                visited.add(identifier)
                tool = all_tools[identifier]
                pending.extend(tool.get("prerequisite_ids", []))
                shop = shops.get(tool.get("shop_id"))
                if shop is not None:
                    cleared = member_id is not None and identifier == seed
                    cleared = cleared or rows.get(identifier, {}).get("_cleared", False)
                    rows[identifier] = {**{k: v for k, v in tool.items() if k in fields},
                                        "_shop": shop, "_cleared": cleared}
        return [rows[key] for key in sorted(rows, key=sid)]
