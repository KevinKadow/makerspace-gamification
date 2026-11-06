"""Packaged, versioned prompt sets and literal, allowlisted variable substitution."""
from copy import deepcopy
from functools import lru_cache
from hashlib import sha256
from importlib.resources import files
import json
import re
from string import Formatter

TYPES = ["onboarding", "return", "opt_out", "invitation", "checkout_earned", "checkout_granted", "volunteer_credit",
         "kudos", "recruitment", "rank_up", "shop_complete", "boss", "stewardship", "challenge", "first_build",
         "develop_mentor", "mentoring", "correction", "conversation", "status", "delivery", "project", "quest"]
AUDIENCES = ["member", "shared", "recipient", "nonparticipant"]
VARIABLES = {
    "member_full_name", "member_slack_id", "member_mention", "current_rank", "old_rank", "new_rank",
    "highest_skill", "highest_skill_shop", "highest_skill_depth", "xp_change", "xp_total", "shop_name",
    "tool_name", "challenge_title", "project_title", "quest_title", "volunteer_credits", "summary",
    "giver_full_name", "giver_slack_id", "recipient_full_name", "recipient_slack_id", "recipient_mention",
    "delivery_status", "dm_status", "public_status", "xp_result",
    "sponsor_full_name", "sponsor_slack_id", "message_type", "audience", "audience_instruction", "facts",
}
# Only these event fields can enter the model, including the legacy {facts} placeholder.
FACT_KEYS = VARIABLES - {"facts", "audience_instruction"} | {
    "member", "rank", "shop", "tool", "title", "milestone", "request", "xp", "metrics", "achievements",
    "giver", "recipient", "type", "slot", "old_slot", "new_slot",
}
METRICS = {"checkouts", "shops", "completed_shops", "mentoring", "learners", "volunteer", "boss",
           "stewardship", "develop_mentor", "challenge", "first_build"}
EXAMPLE_FACTS = {"member_full_name": "Joe Maker", "member_slack_id": "U012EXAMPLE", "old_rank": "Newbie",
                 "new_rank": "Novice", "rank": "Novice", "highest_skill": "Bandsaw", "highest_skill_shop": "Woodworking",
                 "highest_skill_depth": 1, "tool": "Bandsaw", "shop": "Woodworking", "xp_change": "100", "xp_total": "400",
                 "summary": "A verified learning milestone was recorded.", "recipient_full_name": "Alex Maker",
                 "recipient_slack_id": "U045EXAMPLE", "delivery_status": "delivered", "dm_status": "delivered",
                 "public_status": "not requested", "xp_result": "17 XP awarded"}


def clean_facts(facts):
    result = {}
    for key, value in facts.items():
        if key not in FACT_KEYS:
            continue
        if key == "achievements":
            result[key] = [clean_facts(v) for v in value if isinstance(v, dict)] if isinstance(value, list) else []
        elif key == "metrics":
            result[key] = {k: v for k, v in value.items() if k in METRICS and isinstance(v, (str, int, float, bool))} if isinstance(value, dict) else {}
        elif value is None or isinstance(value, (str, int, float, bool)):
            result[key] = value
    return result


def variables_for(facts, kind, audience, instruction=""):
    safe = clean_facts(facts)
    # A coalesced announcement can contain several types. Find its own evidence
    # rather than accidentally using another achievement's rank/shop fields.
    matching = [a for a in safe.get("achievements", []) if a.get("type") == kind]
    if not matching:  # Compatibility for jobs queued before typed achievements.
        matching = [a for a in safe.get("achievements", []) if
                    (kind == "rank_up" and (a.get("new_rank") or a.get("rank"))) or
                    (kind == "shop_complete" and a.get("shop")) or a.get("milestone") == kind]
    selected = {**safe, **(matching[-1] if matching else {})}
    values = {key: selected.get(key) for key in VARIABLES}
    aliases = {"member_full_name": "member", "current_rank": "rank", "shop_name": "shop", "tool_name": "tool",
               "project_title": "title", "quest_title": "title", "challenge_title": "title", "xp_total": "xp",
               "giver_slack_id": "giver", "recipient_slack_id": "recipient"}
    for target, source in aliases.items():
        if values[target] is None:
            values[target] = selected.get(source)
    if kind == "rank_up" and values["new_rank"] is None:
        values["new_rank"] = selected.get("rank")
    uid = values["member_slack_id"]
    values["member_mention"] = f"<@{uid}>" if isinstance(uid, str) and re.fullmatch(r"[UW][A-Z0-9]+", uid) else None
    recipient_uid = values["recipient_slack_id"]
    values["recipient_mention"] = f"<@{recipient_uid}>" if isinstance(recipient_uid, str) and re.fullmatch(r"[UW][A-Z0-9]+", recipient_uid) else None
    values.update(message_type=kind, audience=audience, audience_instruction=instruction, facts=safe)
    return values


def fields_in(text):
    fields = set()
    for _, field, spec, conversion in Formatter().parse(text):
        if field is not None:
            if field not in VARIABLES or spec or conversion:
                raise ValueError(f"Unsupported prompt variable: {field}. Use documented names without formatting or attribute access.")
            fields.add(field)
    return fields


def render(text, values):
    """One pass: braces in supplied names/messages cannot become new variables."""
    fields_in(text)
    output = []
    for literal, field, _, _ in Formatter().parse(text):
        output.append(literal)
        if field is not None:
            value = values.get(field)
            if field == "audience_instruction":
                output.append(value or "")  # Trusted template instructions, not member data.
            else:
                output.append(json.dumps(value if value not in (None, "") else "not recorded", ensure_ascii=False, default=str))
    return "".join(output)


def normalize_template(template):
    """Normalize legacy one-prompt database versions without rewriting their history."""
    result = deepcopy(template)
    if "variations" not in result:
        result["variations"] = [{"id": "legacy", "personality": "Published custom persona", "attitude": "Published custom attitude",
                                  "system": result.get("system", ""), "user": result.get("prompt", "")}]
    result.setdefault("audience_instruction", "Address the configured audience; keep participation optional.")
    return result


def validate_template(template, minimum=1):
    if template.get("type") not in TYPES or template.get("audience") not in AUDIENCES:
        raise ValueError("Unknown message type or audience")
    for field in ("fallback", "audience_instruction"):
        if not isinstance(template.get(field), str) or not template[field].strip() or len(template[field]) > 2000:
            raise ValueError(f"Invalid {field}")
    temperature, max_tokens = template.get("temperature"), template.get("max_tokens")
    if (type(temperature) not in (int, float) or not 0 <= temperature <= 2 or
            type(max_tokens) is not int or not 32 <= max_tokens <= 1024):
        raise ValueError("Use temperature 0–2 and 32–1,024 tokens")
    variations = template.get("variations")
    if not isinstance(variations, list) or not minimum <= len(variations) <= 20:
        raise ValueError(f"Provide {minimum}–20 prompt variations")
    ids = set()
    for variation in variations:
        if not isinstance(variation, dict):
            raise ValueError("Every variation must be an object")
        identifier = variation.get("id", "")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", identifier) or identifier in ids:
            raise ValueError("Variation IDs must be unique lowercase identifiers")
        ids.add(identifier)
        for key in ("personality", "attitude", "system", "user"):
            if not isinstance(variation.get(key), str) or not variation[key].strip() or len(variation[key]) > (8000 if key in ("system", "user") else 200):
                raise ValueError(f"Invalid variation {key}")
        fields_in(variation["system"])
        fields_in(variation["user"])
    return template


@lru_cache(maxsize=len(TYPES))
def _load(kind):
    if kind not in TYPES:
        raise ValueError("Unknown message type")
    raw = files("ledger").joinpath("prompts", kind + ".json").read_text(encoding="utf-8")
    document = json.loads(raw)
    if document.get("schema_version") != 1 or document.get("type") != kind or not isinstance(document.get("version"), int) or document["version"] < 1:
        raise ValueError("Invalid prompt library version/type")
    if set(document.get("audiences", {})) != set(AUDIENCES):
        raise ValueError("Every prompt file must configure all four audiences")
    for audience in AUDIENCES:
        validate_template({**document, "audience": audience, **document["audiences"][audience]}, minimum=3)
    document["library_version"] = f"{document['version']}:{sha256(raw.encode()).hexdigest()}"
    return document


def library_template(kind, audience):
    if audience not in AUDIENCES:
        raise ValueError("Unknown audience")
    document = deepcopy(_load(kind))
    return {"type": kind, "audience": audience, "variations": document["variations"],
            "temperature": document["temperature"], "max_tokens": document["max_tokens"],
            "library_version": document["library_version"], **document["audiences"][audience]}


if __name__ == "__main__":
    for kind in TYPES:
        for audience in AUDIENCES:
            library_template(kind, audience)
    print(f"Validated {len(TYPES)} prompt files and all audience configurations.")
