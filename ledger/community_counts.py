"""Public, deterministic aggregate answers about attendance and new members."""
from datetime import datetime, time, timedelta, timezone
import re
from zoneinfo import ZoneInfo

from pymongo.errors import PyMongoError


NEW_YORK = ZoneInfo("America/New_York")
PERIOD_PATTERNS = {
    "right_now": re.compile(r"\b(?:right\s+now|currently|in\s+the\s+last\s+two\s+hours?)\b", re.I),
    "today": re.compile(r"\btoday\b", re.I),
    "yesterday": re.compile(r"\byesterday\b", re.I),
    "this_week": re.compile(r"\b(?:this\s+week|week\s+to\s+date|week\s+so\s+far)\b", re.I),
    "this_month": re.compile(r"\b(?:this\s+month|month\s+to\s+date|month\s+so\s+far)\b", re.I),
}
ATTENDANCE_DESTINATION_SUFFIX = (
    r"(?=\s*(?:(?:right\s+now|currently|in\s+the\s+last\s+two\s+hours?|today|yesterday|"
    r"this\s+(?:week|month)|(?:week|month)\s+(?:to\s+date|so\s+far))\b|[?!.;,:]|$))"
)
HERE_DESTINATION = r"here" + ATTENDANCE_DESTINATION_SUFFIX
SPACE_DESTINATION = r"(?:the\s+)?(?:space|makerspace)" + ATTENDANCE_DESTINATION_SUFFIX
NONPHYSICAL_ATTENDANCE_CONTEXT = re.compile(
    r"\b(?:space|makerspace)\s+(?:website|web\s*site|web\s*page|site|page|station)\b|"
    r"\b(?:website|web\s*site|web\s*page|site|page|station|class(?:room)?|course|workshop)\b[^?!.]{0,80}\b"
    r"(?:busy|attendance|visitors?|visits?|check-?ins?)\b|"
    r"\b(?:busy|attendance|visitors?|visits?|check-?ins?)\b[^?!.]{0,80}\b"
    r"(?:website|web\s*site|web\s*page|site|page|station|class(?:room)?|course|workshop)\b", re.I)
SPACE_QUESTION = re.compile(
    r"\b(?:how\s+busy|how\s+many\s+(?:visitors?|check-?ins?)|"
    r"how\s+many\s+people(?:\s+(?:are|were|have\s+been))?\s+(?:" + HERE_DESTINATION +
    r"|(?:at|in)\s+" + SPACE_DESTINATION + r")|"
    r"how\s+many\s+people\s+(?:"
    r"(?:have\s+)?visited\s+(?:" + HERE_DESTINATION + r"|" + SPACE_DESTINATION + r")|"
    r"(?:came|have\s+come)\s+(?:" + HERE_DESTINATION + r"|to\s+" + SPACE_DESTINATION + r")|"
    r"(?:have\s+)?checked-?\s*in\s+(?:" + HERE_DESTINATION + r"|at\s+" + SPACE_DESTINATION + r"))|"
    r"(?:space|makerspace)\s+(?:busy|attendance|visitors?)|"
    r"(?:busy|crowded)\s+(?:is|was|has)|attendance\s+(?:today|yesterday|this))\b", re.I)
MEMBER_QUESTION = re.compile(
    r"\bhow\s+many\b.{0,80}\bnew\s+members?\b|"
    r"\bnew\s+members?\b.{0,80}\b(?:join(?:ed)?|sign\s*ups?|added)\b", re.I)


def _is_space_question(text):
    """Recognize attendance wording only when it has no nonphysical target."""
    return bool(SPACE_QUESTION.search(text) and not NONPHYSICAL_ATTENDANCE_CONTEXT.search(text))


def recognize(text):
    """Return a fixed (subject, period) request; ambiguous requests are ignored."""
    if not isinstance(text, str) or len(text) > 6000:
        return None
    space = _is_space_question(text)
    members = bool(MEMBER_QUESTION.search(text))
    if space == members:
        return None
    periods = [name for name, pattern in PERIOD_PATTERNS.items() if pattern.search(text)]
    if len(periods) != 1:
        return None
    period = periods[0]
    subject = "space" if space else "members"
    if subject == "members" and period == "right_now":
        return None
    return subject, period


def is_count_question(text):
    """Identify count topics even when the requested period needs clarification."""
    if not isinstance(text, str) or len(text) > 6000:
        return False
    return bool(_is_space_question(text) or MEMBER_QUESTION.search(text))


def clarification(text):
    space = _is_space_question(text)
    members = bool(MEMBER_QUESTION.search(text))
    periods = "today, yesterday, this week, or this month so far"
    if space:
        periods = "the last two hours, today, yesterday, this week, or this month so far"
    if space and members:
        return ("I can count members who used the space in the last two hours or a calendar period, and new members for "
                "today, yesterday, this week, or this month so far. Which period would you like?")
    if space:
        return f"I can count members who used the space in {periods}. Which period would you like?"
    return f"I can count new members for {periods}. Which period would you like?"


def bounds(period, instant=None):
    """Return a half-open UTC range using New York local calendar boundaries."""
    instant = instant or datetime.now(timezone.utc)
    local_now = instant.astimezone(NEW_YORK)
    if period == "right_now":
        start = instant - timedelta(hours=2)
        end = instant
    elif period in ("today", "yesterday"):
        day = local_now.date() - (timedelta(days=1) if period == "yesterday" else timedelta())
        local_start = datetime.combine(day, time.min, NEW_YORK)
        local_end = local_start + timedelta(days=1)
        start, end = local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)
    elif period == "this_week":
        day = local_now.date() - timedelta(days=local_now.weekday())
        start = datetime.combine(day, time.min, NEW_YORK).astimezone(timezone.utc)
        end = instant
    elif period == "this_month":
        start = datetime.combine(local_now.date().replace(day=1), time.min, NEW_YORK).astimezone(timezone.utc)
        end = instant
    else:
        raise ValueError("Unsupported community count period")
    return start, end


def count(sources, subject, period, instant=None):
    """Return only a count and period label from a fixed source aggregation."""
    if subject not in ("space", "members") or period not in PERIOD_PATTERNS:
        raise ValueError("Unsupported community count request")
    if subject == "members" and period == "right_now":
        raise ValueError("New-member counts do not support a rolling period")
    start, end = bounds(period, instant)
    if subject == "space":
        start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        start_s, end_s = int(start.timestamp()), int(end.timestamp())
        pipeline = [
            {"$match": {"uid": {"$type": "string", "$ne": ""}, "$or": [
                {"timeOf": {"$gte": start_ms, "$lt": end_ms}},
                {"timeOf": {"$gte": start_s, "$lt": end_s}},
                {"timeOf": {"$gte": start, "$lt": end}},
            ]}},
            {"$project": {"_id": 0, "uid": 1}},
            {"$group": {"_id": "$uid"}},
            {"$count": "count"},
        ]
        collection = "checkins"
    elif subject == "members":
        pipeline = [
            {"$match": {"merged_at": None, "startDate": {"$gte": start, "$lt": end}}},
            {"$project": {"_id": 0, "startDate": 1}},
            {"$count": "count"},
        ]
        collection = "members"
    else:
        raise ValueError("Unsupported community count subject")
    rows = sources._aggregate(collection, pipeline, timeout_ms=2000)
    return {"subject": subject, "period": period, "count": int(rows[0]["count"]) if rows else 0}


def timeframe(period):
    """Return the only public timeframe wording supplied to narration."""
    return {"right_now": "in the last two hours", "today": "today", "yesterday": "yesterday",
            "this_week": "this week so far", "this_month": "this month so far"}[period]


def format_answer(result):
    """Render the authoritative fallback without exposing storage terminology."""
    number = result["count"]
    period = result["period"]
    if result["subject"] == "space":
        answer = f"A total of {number:,} members used the space {timeframe(period)}."
        if period == "right_now":
            answer += " This is a recent-use estimate, not a live occupancy count."
        return answer
    return f"{number:,} new member records have a start date {timeframe(period)}. Merged duplicate records are excluded."


def valid_space_answer(text, result):
    """Accept only a small affirmative grammar around authoritative facts."""
    if (not isinstance(text, str) or not text.strip() or len(text) > 400 or result.get("subject") != "space"
            or type(result.get("count")) is not int or result["count"] < 0):
        return False
    lowered = re.sub(r"\s+", " ", text.strip().lower())
    if (re.search(r"\b(?:uids?|identifiers?|records?|databases?|queries|check-?ins?)\b", lowered)
            or "<@" in lowered or "<!" in lowered):
        return False
    label = timeframe(result.get("period"))
    if lowered.count(label) != 1:
        return False
    numeric_values = [int(value.replace(",", "")) for value in re.findall(r"(?<!\w)\d[\d,]*(?!\w)", text)]
    if numeric_values != [result["count"]]:
        return False
    expected_note = "this is a recent-use estimate, not a live occupancy count."
    if result["period"] == "right_now":
        if lowered.count(expected_note) != 1:
            return False
        lowered = re.sub(r"\s+", " ", lowered.replace(expected_note, "")).strip()
    elif "estimate" in lowered or "occupancy" in lowered:
        return False
    if re.search(r"\b(?:not|never|no|more\s+than|less\s+than|fewer\s+than|at\s+least|at\s+most|"
                 r"over|under|approximately|about|around|nearly|up\s+to|between|possibly|perhaps)\b",
                 lowered):
        return False
    without_label = lowered.replace(label, "")
    if re.search(r"\b(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|dozen|hundred|thousand|million)\b",
                 without_label):
        return False
    sentence = lowered.removesuffix(".").strip()
    number = re.escape(f"{result['count']:,}")
    plain_number = re.escape(str(result["count"]))
    number = number if number == plain_number else f"(?:{number}|{plain_number})"
    period = re.escape(label)
    members_first = (rf"(?:a total of )?{number} members? (?:used|visited) "
                     rf"(?:the )?(?:space|makerspace)")
    space_first = (rf"(?:the )?(?:space|makerspace) (?:welcomed|hosted|saw) "
                   rf"(?:a total of )?{number} members?")
    patterns = (
        rf"{members_first} {period}", rf"{period},? {members_first}",
        rf"{space_first} {period}", rf"{period},? {space_first}",
    )
    return any(re.fullmatch(pattern, sentence) for pattern in patterns)


def safe_count(sources, subject, period, instant=None):
    """Return None on source failures; never expose raw database errors."""
    try:
        return count(sources, subject, period, instant)
    except (PyMongoError, OSError, TimeoutError, ValueError, KeyError, TypeError):
        return None
