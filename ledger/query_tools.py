"""Fixed, read-only makerspace tools; the caller is supplied by the application."""
from datetime import datetime, timezone
import re
import time
from zoneinfo import ZoneInfo

from bson import ObjectId
from pymongo.errors import PyMongoError
from pymongo import timeout

from .domain import Denied
from .progress import progress
from .sources import object_id
from .source_reads import CATALOG_FIELDS
from .storage import now
from .read_options import optimized_reads

PROJECTIONS = {name: fields.split() for name, fields in CATALOG_FIELDS.items()}
QUERY_TOOL = {"type": "function", "function": {"name": "query_makerspace", "description": "Read enabled shops/tools, your clearances, and available volunteer opportunities.",
    "parameters": {"type": "object", "properties": {
        "collection": {"type": "string", "enum": list(PROJECTIONS)}, "search": {"type": "string", "maxLength": 100},
        "shop_id": {"type": "string"}, "tool_id": {"type": "string"}, "out_of_service": {"type": "boolean"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 25}}, "required": ["collection"], "additionalProperties": False}}}
PROGRESS_TOOL = {"type": "function", "function": {"name": "my_progress", "description": "Read your own authoritative Ledger progress and deficits.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}


def normalize(value):
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat()
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    return value


class QueryTools:
    def __init__(self, ledger, member, private=True):
        self.l, self.member, self.private = ledger, member, private
        self.started, self.calls = time.monotonic(), 0

    def call(self, name, arguments):
        self.calls += 1
        if self.calls > 3 or time.monotonic() - self.started >= 30:
            raise ValueError("The Ledger query budget is exhausted.")
        with timeout(max(0.001, 30 - (time.monotonic() - self.started))):
            self.l.require_member(self.member)
            if name == "my_progress":
                self.l.require(self.member)
                if arguments != {}:
                    raise ValueError("Progress accepts no member IDs or arguments.")
                facts = progress(self.l, self.member)
                facts.pop("next_rank", None)  # Next requirements are visible, future names are not.
                if not self.private:
                    facts.pop("blockers", None)
                return facts
            if name != "query_makerspace":
                raise ValueError("Unknown read-only tool.")
            return self.query(arguments)

    def query(self, args):
        if not isinstance(args, dict) or set(args) - {"collection", "search", "shop_id", "tool_id", "out_of_service", "limit"}:
            raise ValueError("Unsupported query arguments.")
        collection = args.get("collection")
        if not isinstance(collection, str) or collection not in PROJECTIONS:
            raise ValueError("Choose an enabled query collection.")
        self.l.require_member(self.member)
        if not self.l.active(self.member) and collection not in ("shops", "tools"):
            raise Denied("Join The Ledger before using personal game queries.")
        search = args.get("search", "")
        limit = args.get("limit", 10)
        if not isinstance(search, str) or len(search) > 100 or type(limit) is not int or not 1 <= limit <= 25:
            raise ValueError("Search or result limit is invalid.")
        for key in ("shop_id", "tool_id"):
            if key in args and (not isinstance(args[key], str) or not ObjectId.is_valid(args[key])):
                raise ValueError("Select a valid shop or tool ID.")
        if "out_of_service" in args and type(args["out_of_service"]) is not bool:
            raise ValueError("Out-of-service must be a boolean.")
        if collection not in ("tools", "tool_checkouts") and ("tool_id" in args or "out_of_service" in args):
            raise ValueError("Tool filters apply only to tools or your clearances.")
        started = time.monotonic()
        def read(name, query, fields, cap):
            remaining = 2 - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("Makerspace read deadline exceeded")
            return self.l.sources.bounded(name, query, fields, cap, max(1, int(remaining * 1000)))
        try:
            if optimized_reads():
                today = now().astimezone(ZoneInfo("America/New_York")).date()
                boundary = datetime.combine(today, datetime.min.time(), timezone.utc)
                remaining = 2 - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError("Makerspace read deadline exceeded")
                rows = self.l.sources.catalog_query(collection, search=search, shop_id=args.get("shop_id"),
                    tool_id=args.get("tool_id"), out_of_service=args.get("out_of_service"), member_id=self.member,
                    boundary=boundary, limit=limit + 1, timeout_ms=max(1, int(remaining * 1000)))
            else:
                rows = self._legacy_rows(args, read)
            truncated = len(rows) > limit
            rows = rows[:limit]
            for r in rows:
                for field in ("name", "title", "description", "wiki_url", "out_of_service_note"):
                    if isinstance(r.get(field), str):
                        truncated = truncated or len(r[field]) > 400
                        r[field] = r[field][:400]
                if collection == "volunteer_events":
                    r["schedule"] = "unscheduled" if not r.get("event_date") else r["event_date"].date().isoformat()
            return {"status": "ok", "collection": collection, "results": normalize(rows), "retrieved_at": now().isoformat(), "truncated": truncated}
        except (PyMongoError, OSError, TimeoutError):
            return {"status": "unavailable", "collection": collection, "results": [], "retrieved_at": now().isoformat(), "truncated": False}

    def _legacy_rows(self, args, read):
        collection, search = args["collection"], args.get("search", "")
        query = {}
        if search:
            query["name" if collection in ("shops", "tools") else "title"] = {"$regex": re.escape(search), "$options": "i"}
        if collection in ("shops", "tools"):
            query["disabled"] = {"$ne": True}
        shops = read("shops", {"disabled": {"$ne": True}}, ["name", "disabled"], 1001)
        if len(shops) > 1000:
            raise ValueError("Enabled catalog exceeds the supported bound")
        shop_ids = [row["_id"] for row in shops]
        if "shop_id" in args:
            shop_ids = [identifier for identifier in shop_ids if identifier == object_id(args["shop_id"])]
        if collection == "shops":
            query["_id"] = {"$in": shop_ids}
        elif collection == "tools":
            query["shop_id"] = {"$in": shop_ids}
            if "tool_id" in args:
                query["_id"] = object_id(args["tool_id"])
            if "out_of_service" in args:
                query["out_of_service"] = args["out_of_service"] if args["out_of_service"] else {"$ne": True}
        elif collection == "tool_checkouts":
            query.pop("title", None)
            tool_query = {"disabled": {"$ne": True}, "shop_id": {"$in": shop_ids}}
            if search:
                tool_query["name"] = {"$regex": re.escape(search), "$options": "i"}
            if "tool_id" in args:
                tool_query["_id"] = object_id(args["tool_id"])
            if "out_of_service" in args:
                tool_query["out_of_service"] = args["out_of_service"] if args["out_of_service"] else {"$ne": True}
            tools = read("tools", tool_query, ["name", "shop_id"], 1001)
            if len(tools) > 1000:
                raise ValueError("Tool catalog exceeds the supported bound")
            query.update(member_id=object_id(self.member), revoked_at=None, tool_id={"$in": [tool["_id"] for tool in tools]})
        else:
            today = now().astimezone(ZoneInfo("America/New_York")).date()
            boundary = datetime.combine(today, datetime.min.time(), timezone.utc)
            query["$and"] = [{"$or": [{"shop_id": {"$in": shop_ids}}, {"shop_id": None}]}]
            if "shop_id" in args:
                query["shop_id"] = object_id(args["shop_id"])
            if collection == "volunteer_tasks":
                query.update(status={"$in": ["available", "reusable", "repeatable", "recurring"]})
                query["$and"].append({"$or": [{"next_available": None}, {"next_available": {"$lte": boundary}}]})
            else:
                query.update(status="open")
                query["$and"].append({"$or": [{"event_date": None}, {"event_date": {"$gte": boundary}}]})
        return read(collection, query, PROJECTIONS[collection], args.get("limit", 10) + 1)
