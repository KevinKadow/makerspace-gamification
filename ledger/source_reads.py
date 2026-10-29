"""Fixed read pipelines; callers cannot supply aggregation stages or projections."""
import re

CATALOG_FIELDS = {
    "shops": "name disabled wiki_url out_of_service out_of_service_note",
    "tools": "name description wiki_url shop_id prerequisite_ids open out_of_service disabled",
    "tool_checkouts": "tool_id checked_out_at",
    "volunteer_tasks": "title description shop_id status prerequisite_tool_ids next_available days",
    "volunteer_events": "title description shop_id status event_date prerequisite_tool_ids",
}
SKILL_TOOL_FIELDS = "name shop_id prerequisite_ids disabled open out_of_service"
SKILL_SHOP_FIELDS = "name disabled out_of_service"


def projection(fields):
    return {"_id": 1, **dict.fromkeys(fields.split() if isinstance(fields, str) else fields, 1)}


def enabled_shop_lookup(local_field="shop_id", name="_enabled_shop", fields="_id"):
    return {"$lookup": {"from": "shops", "localField": local_field, "foreignField": "_id",
        "pipeline": [{"$match": {"disabled": {"$ne": True}}}, {"$project": projection(fields)}], "as": name}}


def catalog_pipeline(collection, *, search="", shop_id=None, tool_id=None, out_of_service=None,
                     member_id=None, boundary=None, limit=11):
    if collection not in CATALOG_FIELDS or type(limit) is not int or not 1 <= limit <= 26:
        raise ValueError("Unsupported bounded catalog read")
    if not isinstance(search, str) or len(search) > 100 or (out_of_service is not None and type(out_of_service) is not bool):
        raise ValueError("Invalid catalog filters")
    if collection not in ("tools", "tool_checkouts") and (tool_id is not None or out_of_service is not None):
        raise ValueError("Tool filters apply only to tools and clearances")
    match, joins = {}, []
    name_filter = {"$regex": re.escape(search), "$options": "i"} if search else None
    tool_match = {"disabled": {"$ne": True}}
    if name_filter:
        tool_match["name"] = name_filter
    if tool_id is not None:
        tool_match["_id"] = tool_id
    if shop_id is not None:
        tool_match["shop_id"] = shop_id
    if out_of_service is not None:
        tool_match["out_of_service"] = True if out_of_service else {"$ne": True}
    if collection == "shops":
        match["disabled"] = {"$ne": True}
        if shop_id is not None:
            match["_id"] = shop_id
        if name_filter:
            match["name"] = name_filter
    elif collection == "tools":
        match = tool_match
        joins = [enabled_shop_lookup(), {"$match": {"_enabled_shop.0": {"$exists": True}}}]
    elif collection == "tool_checkouts":
        if member_id is None:
            raise ValueError("A caller is required for clearance reads")
        match = {"member_id": member_id, "revoked_at": None}
        if tool_id is not None:
            match["tool_id"] = tool_id
        joins = [{"$lookup": {"from": "tools", "localField": "tool_id", "foreignField": "_id",
            "pipeline": [{"$match": tool_match}, enabled_shop_lookup(),
                         {"$match": {"_enabled_shop.0": {"$exists": True}}}, {"$project": {"_id": 1}}], "as": "_enabled_tool"}},
            {"$match": {"_enabled_tool.0": {"$exists": True}}}]
    else:
        if boundary is None:
            raise ValueError("Volunteer reads require a date boundary")
        if shop_id is not None:
            match["shop_id"] = shop_id
        if name_filter:
            match["title"] = name_filter
        if collection == "volunteer_tasks":
            match.update(status={"$in": ["available", "reusable", "repeatable", "recurring"]},
                         **{"$or": [{"next_available": None}, {"next_available": {"$lte": boundary}}]})
        else:
            match.update(status="open", **{"$or": [{"event_date": None}, {"event_date": {"$gte": boundary}}]})
        joins = [enabled_shop_lookup(), {"$match": {"$or": [
            {"shop_id": None}, {"_enabled_shop.0": {"$exists": True}}]}}]
    return [{"$match": match}, *joins, {"$sort": {"_id": 1}}, {"$limit": limit},
            {"$project": projection(CATALOG_FIELDS[collection])}]


def unique_mapping_pipeline(member_ids):
    """One valid mapping per member and one live mapping per Slack identity."""
    return [{"$match": {"member_id": {"$in": member_ids}, "invalidated_at": None}},
        {"$project": {"member_id": 1, "slack_id": 1}},
        {"$group": {"_id": "$member_id", "count": {"$sum": 1}, "slack_id": {"$first": "$slack_id"}}},
        {"$match": {"count": 1, "slack_id": {"$regex": r"\A[UW][A-Z0-9]+\z"},
                    "$expr": {"$eq": [{"$type": "$slack_id"}, "string"]}}},
        {"$lookup": {"from": "slack_users", "localField": "slack_id", "foreignField": "slack_id",
            "pipeline": [{"$match": {"invalidated_at": None}}, {"$limit": 2}, {"$project": {"_id": 1}}], "as": "_reverse"}},
        {"$match": {"_reverse.0": {"$exists": True}, "_reverse.1": {"$exists": False}}},
        {"$project": {"slack_id": 1}}]


def identity_pipeline(slack_id, member_fields):
    return [{"$match": {"slack_id": slack_id, "invalidated_at": None}},
        {"$project": projection("member_id slack_id")}, {"$limit": 2},
        {"$group": {"_id": None, "count": {"$sum": 1}, "link": {"$first": "$$ROOT"}}},
        {"$match": {"count": 1, "link.member_id": {"$ne": None}, "link.slack_id": {"$regex": r"\A[UW][A-Z0-9]+\z"},
                    "$expr": {"$eq": [{"$type": "$link.slack_id"}, "string"]}}},
        {"$lookup": {"from": "slack_users", "localField": "link.member_id", "foreignField": "member_id",
            "pipeline": [{"$match": {"invalidated_at": None}}, {"$limit": 2}, {"$project": {"_id": 1}}], "as": "_forward"}},
        {"$match": {"_forward.0": {"$exists": True}, "_forward.1": {"$exists": False}}},
        {"$lookup": {"from": "members", "localField": "link.member_id", "foreignField": "_id",
            "pipeline": [{"$match": {"merged_at": None}}, {"$project": projection(member_fields)}], "as": "member"}},
        {"$unwind": "$member"}, {"$replaceWith": "$member"}]


def member_identity_pipeline(member_ids, member_fields):
    return [{"$match": {"_id": {"$in": member_ids}, "merged_at": None}},
        {"$project": projection(member_fields)},
        {"$lookup": {"from": "slack_users", "localField": "_id", "foreignField": "member_id",
            "pipeline": [{"$match": {"invalidated_at": None}}, {"$limit": 2}, {"$project": {"slack_id": 1}}], "as": "_links"}},
        {"$match": {"_links.0": {"$exists": True}, "_links.1": {"$exists": False}}},
        {"$unwind": "$_links"}, {"$match": {"_links.slack_id": {"$regex": r"\A[UW][A-Z0-9]+\z"},
                                                   "$expr": {"$eq": [{"$type": "$_links.slack_id"}, "string"]}}},
        {"$lookup": {"from": "slack_users", "localField": "_links.slack_id", "foreignField": "slack_id",
            "pipeline": [{"$match": {"invalidated_at": None}}, {"$limit": 2}, {"$project": {"_id": 1}}], "as": "_reverse"}},
        {"$match": {"_reverse.0": {"$exists": True}, "_reverse.1": {"$exists": False}}},
        {"$project": {**projection(member_fields), "slack_id": "$_links.slack_id"}}]


def skill_graph_pipeline(member_id):
    # The server returns only the union of cleared tools and their ancestors.
    # Python still computes longest DAG depth; graphLookup distance is unsuitable.
    return [{"$match": {"member_id": member_id, "revoked_at": None}},
        {"$group": {"_id": "$tool_id"}},
        {"$lookup": {"from": "tools", "localField": "_id", "foreignField": "_id",
            "pipeline": [{"$match": {"disabled": {"$ne": True}}}, {"$project": projection(SKILL_TOOL_FIELDS)}], "as": "tool"}},
        {"$unwind": "$tool"}, *_skill_closure_stages(cleared=True)]


def skill_ancestor_pipeline(tool_ids):
    """Fetch one normalized prerequisite frontier plus its native-ID closure."""
    return [{"$match": {"_id": {"$in": list(tool_ids)}, "disabled": {"$ne": True}}},
        {"$project": projection(SKILL_TOOL_FIELDS)}, {"$project": {"tool": "$$ROOT"}},
        *_skill_closure_stages(cleared=False)]


def _skill_closure_stages(cleared):
    ancestor = {field: "$$node." + field for field in ("_id", *SKILL_TOOL_FIELDS.split())}
    return [{"$graphLookup": {"from": "tools", "startWith": {"$ifNull": ["$tool.prerequisite_ids", []]},
            "connectFromField": "prerequisite_ids", "connectToField": "_id",
            "restrictSearchWithMatch": {"disabled": {"$ne": True}}, "as": "ancestors"}},
        # graphLookup reads foreign documents internally. Strip everything else
        # before flattening/deduplication so those stages retain only skill data.
        {"$project": {"nodes": {"$concatArrays": [
            {"$map": {"input": "$ancestors", "as": "node", "in": {**ancestor, "_cleared": False}}},
            [{"$mergeObjects": ["$tool", {"_cleared": cleared}]}]]}}},
        {"$unwind": "$nodes"}, {"$replaceWith": "$nodes"},
        {"$group": {"_id": "$_id", "node": {"$first": "$$ROOT"}, "cleared": {"$max": "$_cleared"}}},
        {"$replaceWith": {"$mergeObjects": ["$node", {"_cleared": "$cleared"}]}},
        enabled_shop_lookup(name="_shop", fields=SKILL_SHOP_FIELDS), {"$unwind": "$_shop"},
        {"$project": {**projection(SKILL_TOOL_FIELDS), "_cleared": 1, "_shop": 1}}, {"$sort": {"_id": 1}}]
