from copy import deepcopy
from decimal import Decimal, InvalidOperation
import re

RANKS = [
    ("Newbie", "🌱", 0, {}),
    ("Novice", "🔨", 300, {"checkouts": 2, "first_build": 1}),
    ("Initiate", "✨", 600, {"checkouts": 4, "shops": 2, "mentoring": 1, "volunteer": 1}),
    ("Apprentice", "🔧", 1500, {"checkouts": 8, "shops": 3, "completed_shops": 1, "mentoring": 3, "learners": 2, "volunteer": 4}),
    ("Journeyman", "⚙️", 3000, {"checkouts": 12, "shops": 4, "completed_shops": 1, "mentoring": 6, "learners": 3, "volunteer": 8, "boss": 1}),
    ("Adept", "🌟", 5000, {"checkouts": 16, "shops": 5, "completed_shops": 2, "mentoring": 12, "learners": 5, "volunteer": 16, "develop_mentor": 1, "stewardship": 1}),
    ("Unconfigured", "", None, {}),
]
METRICS = {"checkouts", "shops", "completed_shops", "mentoring", "learners", "volunteer", "first_build", "boss", "develop_mentor", "stewardship"}
XP = {"basic_checkout": "31", "checkout": "100", "teaching": "67", "volunteer": "61", "challenge": "100", "boss": "500", "stewardship": "500", "recruitment": "11", "kudos": "17"}


def default_rules():
    return {"_id": "initial", "xp": deepcopy(XP), "ranks": [
        {"slot": i, "name": name, "emoji": emoji, "floor": str(floor) if floor is not None else None,
         "enabled": i < 7, "requirements": requirements}
        for i, (name, emoji, floor, requirements) in enumerate(RANKS, 1)]}


def amount(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Invalid numeric amount") from exc
    if not result.is_finite():
        raise ValueError("Amounts must be finite")
    return result


def validate_rules(rules):
    ranks = rules["ranks"]
    if [r["slot"] for r in ranks] != list(range(1, 8)):
        raise ValueError("Exactly seven ordered stable slots are required")
    previous, disabled = Decimal(-1), False
    names = set()
    for r in ranks:
        if not r["enabled"]:
            disabled = True
            continue
        if disabled:
            raise ValueError("Enabled ranks must be consecutive")
        name = r["name"].strip()
        if not name or len(name) > 40 or name.casefold() in names:
            raise ValueError("Rank names must be unique and contain 1–40 characters")
        names.add(name.casefold())
        emoji = r["emoji"].strip()
        shortcode = re.fullmatch(r":[a-zA-Z0-9_+\-]+:", emoji)
        pictograph = any(ord(c) >= 0x1F000 or 0x2300 <= ord(c) <= 0x2BFF or c in '©®™\u20e3' for c in emoji)
        unicode_emoji = pictograph and all(ord(c) >= 0x1F000 or 0x2300 <= ord(c) <= 0x2BFF or c in '©®™\ufe0f\ufe0e\u200d\u20e3#*0123456789' for c in emoji)
        if not emoji or len(emoji) > 100 or not (shortcode or unicode_emoji):
            raise ValueError("Provide a Unicode emoji or Slack :shortcode:")
        floor = amount(r["floor"])
        if floor < 0 or floor <= previous:
            raise ValueError("XP floors must be nonnegative and strictly increasing")
        previous = floor
        if set(r["requirements"]) - METRICS or any(amount(v) < 0 for v in r["requirements"].values()):
            raise ValueError("Invalid milestone requirements")
        if r["slot"] == 7 and (name == "Unconfigured" or not r["requirements"]):
            raise ValueError("Slot seven needs a name, emoji, XP floor, and milestones")
    if not ranks[0]["enabled"]:
        raise ValueError("The entry rank must remain enabled")


def attainable_rank(rules, xp, metrics):
    highest = 0
    cumulative = {}
    for rank in rules["ranks"]:
        if not rank["enabled"]:
            break
        for metric, minimum in rank["requirements"].items():
            cumulative[metric] = max(amount(minimum), cumulative.get(metric, Decimal(0)))
        if amount(xp) < amount(rank["floor"]) or any(amount(metrics.get(k, 0)) < v for k, v in cumulative.items()):
            break
        highest = rank["slot"]
    return highest
