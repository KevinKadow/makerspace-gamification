"""Deterministic, private skill-tree rendering with equivalent text descriptions."""
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
from pymongo import timeout
from pymongo.errors import PyMongoError
from .sources import object_id, sid
from .read_options import optimized_reads


def highest_skill(sources, member_id):
    """Deepest currently cleared tool in the enabled prerequisite DAG, not mastery."""
    if optimized_reads():
        graph = sources.skill_graph(member_id)
        cleared, shops, tools = graph["cleared"], graph["shops"], graph["tools"]
    else:
        cleared = {sid(c["tool_id"]) for c in sources.rows("tool_checkouts", {"member_id": object_id(member_id)}) if not c.get("revoked_at")}
        shops = {sid(s["_id"]): s for s in sources.rows("shops", {"disabled": {"$ne": True}})}
        tools = {sid(t["_id"]): t for t in sources.rows("tools", {"disabled": {"$ne": True}}) if sid(t.get("shop_id")) in shops}
    depths = {}
    pending = dict(tools)
    while pending:
        resolved = []
        for identifier, tool in pending.items():
            prerequisites = [sid(p) for p in tool.get("prerequisite_ids", [])]
            if all(p in depths for p in prerequisites):
                depths[identifier] = 1 + max((depths[p] for p in prerequisites), default=-1)
                resolved.append(identifier)
        if not resolved:  # Cycles, dangling IDs, and paths depending on them are omitted.
            break
        for identifier in resolved:
            del pending[identifier]
    candidates = [identifier for identifier in cleared & depths.keys() if not tools[identifier].get("open")]
    if not candidates:
        return {}
    identifier = min(candidates, key=lambda i: (-depths[i], tools[i]["name"].casefold(), i))
    tool = tools[identifier]
    return {"highest_skill": tool["name"], "highest_skill_shop": shops[sid(tool["shop_id"])]["name"],
            "highest_skill_depth": depths[identifier]}


def skill_summary(ledger, member_id, search=""):
    if not optimized_reads():
        return _legacy_skill_summary(ledger, member_id, search)
    try:
        with timeout(2):
            return _optimized_skill_summary(ledger, member_id, search)
    except (PyMongoError, OSError, TimeoutError):
        return {"text": "Skill paths are temporarily unavailable. Please try again.",
                "nodes": [], "status": "unavailable"}


def _optimized_skill_summary(ledger, member_id, search):
    from .catalog_cache import display_catalog
    catalog = display_catalog(ledger, search)
    if catalog is None:
        catalog = ledger.sources.skill_catalog(search)
    else:
        catalog = _live_display_catalog(ledger.sources, catalog)
    tools = catalog["tools"]
    relevant = {sid(tool["_id"]) for tool in tools} | {sid(value) for tool in tools for value in tool.get("prerequisite_ids", [])}
    active = {sid(checkout["tool_id"]) for checkout in ledger.sources.clearances(member_id, relevant)}
    names = {sid(tool["_id"]): tool.get("name", "Unavailable prerequisite") for tool in catalog["prerequisite_tools"]}
    nodes, lines = [], []
    by_shop = {}
    for tool in tools:
        by_shop.setdefault(sid(tool.get("shop_id")), []).append(tool)
    for shop in catalog["shops"]:
        lines.append(shop["name"])
        for tool in by_shop.get(sid(shop["_id"]), []):
            prereqs = [sid(value) for value in tool.get("prerequisite_ids", [])]
            state = "Open access" if tool.get("open") else "Cleared" if sid(tool["_id"]) in active else "Next step" if set(prereqs).issubset(active) else "Prerequisites remain"
            label = f"{tool['name']}: {state}" + ("; prerequisites: " + ", ".join(names.get(value, "Unavailable prerequisite") for value in prereqs) if prereqs else "")
            if tool.get("out_of_service"):
                label += "; temporarily unavailable"
            lines.append("  " + label)
            nodes.append({"id": sid(tool["_id"]), "name": tool["name"], "shop": shop["name"], "state": state, "prerequisites": prereqs})
    return {"text": "\n".join(lines) or "No matching shops. Try /ledger-skills <shop name>.", "nodes": nodes}


def _live_display_catalog(sources, catalog):
    """Only descriptive labels come from cache; safety and prerequisites stay live."""
    live_tools = sources.tools_by_id([tool["_id"] for tool in catalog["tools"]],
        ["shop_id", "disabled", "open", "out_of_service", "prerequisite_ids"])
    live_shops = sources.shops_by_id([shop["_id"] for shop in catalog["shops"]], ["disabled", "out_of_service"])
    shops = [{**shop, **live_shops[sid(shop["_id"])]} for shop in catalog["shops"]
             if sid(shop["_id"]) in live_shops and not live_shops[sid(shop["_id"])].get("disabled")]
    shop_ids = {sid(shop["_id"]) for shop in shops}
    tools = []
    for tool in catalog["tools"]:
        live = live_tools.get(sid(tool["_id"]))
        if (not live or live.get("disabled") or sid(live.get("shop_id")) not in shop_ids
                or sid(live.get("shop_id")) != sid(tool.get("shop_id"))):
            continue
        tools.append({**tool, **live, "prerequisite_ids": live.get("prerequisite_ids", [])})
    names = {sid(tool["_id"]): tool for tool in catalog["prerequisite_tools"]}
    missing = {sid(value) for tool in tools for value in tool.get("prerequisite_ids", [])} - names.keys()
    names.update(sources.tools_by_id(missing, ["name"]))
    return {"shops": shops, "tools": tools, "prerequisite_tools": list(names.values())}


def _legacy_skill_summary(ledger, member_id, search=""):
    active = {sid(c["tool_id"]) for c in ledger.sources.rows("tool_checkouts", {"member_id": object_id(member_id)}) if not c.get("revoked_at")}
    shops = [s for s in ledger.sources.rows("shops", {"disabled": {"$ne": True}}) if search.casefold() in s["name"].casefold() or search == sid(s["_id"])]
    nodes, lines = [], []
    for shop in shops:
        lines.append(shop["name"])
        for tool in ledger.sources.rows("tools", {"shop_id": shop["_id"], "disabled": {"$ne": True}}):
            prereqs = [sid(i) for i in tool.get("prerequisite_ids", [])]
            state = "Open access" if tool.get("open") else "Cleared" if sid(tool["_id"]) in active else "Next step" if set(prereqs).issubset(active) else "Prerequisites remain"
            names = [(ledger.sources.tool(i) or {}).get("name", "Unavailable prerequisite") for i in prereqs]
            label = f"{tool['name']}: {state}" + ("; prerequisites: " + ", ".join(names) if names else "")
            if tool.get("out_of_service"):
                label += "; temporarily unavailable"
            lines.append("  " + label)
            nodes.append({"id": sid(tool["_id"]), "name": tool["name"], "shop": shop["name"], "state": state, "prerequisites": prereqs})
    return {"text": "\n".join(lines) or "No matching shops. Try /ledger-skills <shop name>.", "nodes": nodes}


def render_tree(summary):
    nodes = summary["nodes"][:60]
    positions = {n["id"]: (30, 65 + i * 72) for i, n in enumerate(nodes)}
    image = Image.new("RGB", (1100, max(200, 100 + len(nodes) * 72)), "#111c29")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 17)
    except OSError:
        font = ImageFont.load_default(size=17)
    draw.text((30, 20), "THE LEDGER / Skill paths — clearances remain subject to shop safety rules", fill="white", font=font)
    for node in nodes:
        x, y = positions[node["id"]]
        color = "#285f4a" if node["state"] == "Cleared" else "#293e56"
        draw.rounded_rectangle((x, y, 920, y + 54), radius=8, fill=color)
        draw.text((x + 14, y + 7), f"{node['shop']} / {node['name']}", fill="white", font=font)
        draw.text((x + 14, y + 30), node["state"], fill="#d0deed", font=font)
        for i, prerequisite in enumerate(node["prerequisites"]):
            if prerequisite in positions:
                py = positions[prerequisite][1] + 27
                lane = 950 + (i % 4) * 25
                draw.line([(920, py), (lane, py), (lane, y + 27), (920, y + 27)], fill="#cbaa68", width=2)
                draw.polygon([(920, y + 27), (932, y + 21), (932, y + 33)], fill="#cbaa68")
    output = BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return output
