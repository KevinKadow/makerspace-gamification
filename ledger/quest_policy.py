"""Application guardrails shared by generated quest proposals and human review."""
from copy import deepcopy
from html import unescape
import re
from unicodedata import category, normalize

from .sources import sid

LEDGER_AUTHOR = "system:ledger"
REVIEWED_KINDS = ("member_quest", "ledger_quest")
DEFINITION_FIELDS = ("title", "description", "criteria", "shop_ids", "tool_ids", "disciplines")
SENSITIVE = re.compile(
    r"\b(password|passcode|credentials?|api[ _-]?key|access[ _-]?token|secret|"
    r"(?:door|access|alarm|lock|entry)[ _-]?code|billing|credit[ -]?card|bank[ -]?account|"
    r"internal[ -]?notes?|private[ -]?(?:message|conversation)|direct[ -]?message)\b|"
    r"(?:xox[baprs]-|sk-[A-Za-z0-9]{10}|AKIA[A-Z0-9]{12}|-----BEGIN .*PRIVATE KEY)", re.I)
CONTROL = re.compile(r"<[@!#]|<\|[^>]*\|>|</?(?:think|tool_call|system|assistant|prompt_matrix)\b|"
                     r"\[/?INST\]|\[im_(?:start|end)\]|\[endoftext\]|\bSpeaker\s*\d+\b", re.I)
PERSONAL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\b[UW][A-Z0-9]{6,}\b|"
                      r"(?<!\w)\+?\d[\d ()-]{8,}\d(?!\w)")


def generated(q):
    return q.get("kind") == "ledger_quest" and q.get("creator") == LEDGER_AUTHOR


def individual(q):
    return q.get("kind") == "member_quest" or (generated(q) and q.get("quest_type") == "individual")


def cooperative(q):
    return generated(q) and q.get("quest_type") == "cooperative"


def enabled_rank(ledger, slot):
    display = ledger.store.get("ledger_catalog", "rank_display") or {}
    return type(slot) is int and any(r["slot"] == slot and r["enabled"] for r in display.get("ranks", []))


def sanitize(text, names=()):
    """Drop sensitive messages; discard link destinations and identifying syntax."""
    if not isinstance(text, str):
        return ""
    text = unescape(text)
    if SENSITIVE.search(text):
        return ""
    text = re.sub(r"<[@!#][^>]*>", "[identity removed]", text)
    text = re.sub(r"<https?://[^>|]+(?:\|([^>]+))?>", lambda m: m[1] or "[link removed]", text)
    text = re.sub(r"https?://\S+", "[link removed]", text)
    text = PERSONAL.sub("[identity removed]", text)
    for name in sorted(set(names), key=len, reverse=True):
        if name:
            text = re.sub(r"(?<!\w)" + re.escape(name) + r"(?!\w)", "[identity removed]", text, flags=re.I)
    return text.strip()


def canonical_prose(text):
    """Compare visible text without changing stored member-authored prose."""
    text = normalize("NFKC", unescape(text)).casefold()
    text = "".join(c for c in text if category(c) != "Cf" and c != "\u034f"
                   and not ("\ufe00" <= c <= "\ufe0f" or "\U000e0100" <= c <= "\U000e01ef"
                            or "\u180b" <= c <= "\u180f"))
    return " ".join(re.sub(r"[*_~`]", "", text).split())


def contains_rank_name(ledger, value):
    """Shared definitions have no audience-safe configured rank labels."""
    prose = [value.get(k, "") for k in ("title", "description", "criteria")]
    disciplines = value.get("disciplines")
    prose.extend(d.get(k, "") for d in (disciplines if isinstance(disciplines, list) else []) if isinstance(d, dict)
                 for k in ("name", "expectation"))
    names = [canonical_prose(r["name"]) for r in (ledger.store.get("ledger_catalog", "rank_display") or {}).get("ranks", [])
             if isinstance(r.get("name"), str) and r["name"].strip()]
    return any(re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", canonical_prose(text))
               for text in prose if isinstance(text, str) for name in names)


def validate_definition(ledger, value, quest_type):
    if quest_type not in ("individual", "cooperative") or not isinstance(value, dict) or set(value) != set(DEFINITION_FIELDS):
        raise ValueError("Quest proposal must contain exactly the six definition fields.")
    result = deepcopy(value)
    if contains_rank_name(ledger, result):
        raise ValueError("Quest text must use numeric rank slots instead of configured rank names.")
    for key, limit in (("title", 100), ("description", 2000), ("criteria", 2000)):
        text = result[key]
        if (not isinstance(text, str) or not text.strip() or len(text) > limit or
                CONTROL.search(text) or SENSITIVE.search(text) or PERSONAL.search(text)):
            raise ValueError("Provide safe, nonblank quest text within the field limits, without identities or control tokens.")
        result[key] = text.strip()
    for key in ("shop_ids", "tool_ids"):
        ids = result[key]
        if not isinstance(ids, list) or len(ids) > 20 or any(not isinstance(i, str) or not i or len(i) > 100 for i in ids):
            raise ValueError("Prerequisites must be bounded lists of existing IDs.")
        result[key] = sorted(set(ids))
    shops = set(result["shop_ids"])
    for shop in shops:
        doc = ledger.sources.shop(shop)
        if not doc or doc.get("disabled") or doc.get("out_of_service"):
            raise ValueError("Choose enabled shops.")
    for tool in result["tool_ids"]:
        doc = ledger.sources.tool(tool)
        shop = ledger.sources.shop(doc.get("shop_id")) if doc else None
        if (not doc or doc.get("disabled") or not shop or shop.get("disabled")
                or doc.get("out_of_service") or shop.get("out_of_service")):
            raise ValueError("Choose available enabled tools in enabled shops.")
        shops.add(sid(doc["shop_id"]))
    result["shop_ids"] = sorted(shops)
    disciplines = result["disciplines"]
    if not isinstance(disciplines, list) or (quest_type == "individual" and disciplines):
        raise ValueError("Individual quests have no required disciplines.")
    if quest_type == "cooperative":
        if not 2 <= len(disciplines) <= 4:
            raise ValueError("Cooperative quests require two to four disciplines.")
        for discipline in disciplines:
            if (not isinstance(discipline, dict) or set(discipline) != {"name", "expectation"} or
                    any(not isinstance(discipline[k], str) or not discipline[k].strip() or len(discipline[k]) > limit
                        or CONTROL.search(discipline[k]) or SENSITIVE.search(discipline[k]) or PERSONAL.search(discipline[k])
                        for k, limit in (("name", 40), ("expectation", 400)))):
                raise ValueError("Each discipline needs a name and observable contribution expectation.")
        if len({d["name"].strip().casefold() for d in disciplines}) != len(disciplines):
            raise ValueError("Disciplines must be distinct.")
        result["disciplines"] = [{k: d[k].strip() for k in ("name", "expectation")} for d in disciplines]
    return result


QUEST_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": list(DEFINITION_FIELDS),
    "properties": {
        **{key: {"type": "string", "minLength": 1, "maxLength": limit}
           for key, limit in (("title", 100), ("description", 2000), ("criteria", 2000))},
        **{key: {"type": "array", "maxItems": 20, "items": {"type": "string"}} for key in ("shop_ids", "tool_ids")},
        "disciplines": {"type": "array", "maxItems": 4, "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "expectation"],
            "properties": {"name": {"type": "string", "maxLength": 40},
                           "expectation": {"type": "string", "maxLength": 400}}}}
    }
}
