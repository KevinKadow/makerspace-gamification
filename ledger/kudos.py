"""Application-selected kudos emoji; authored message bodies are never filtered."""
import re

EMOJI = [("silver star", ":kudo:"), ("gold star", ":kudos:"),
         ("fixed it!", ":fix_parrot:"), ("First place", ":first_place_medal:"),
         ("fistbump!", ":fistbump:"), ("used the force", ":duct_tape:"),
         ("Thank You", ":thankyou:"), ("Teamwork!", ":teamwork:"),
         ("👏 Applause", ":clap:"), ("🙌 Celebration", ":raised_hands:"),
         ("👍 Thumbs up", ":+1:"), ("💚 Green heart", ":green_heart:"),
         ("❤️ Heart", ":heart:"), ("🌟 Shining star", ":star2:"),
         ("✨ Sparkles", ":sparkles:"), ("🎉 Party", ":tada:"),
         ("🏆 Trophy", ":trophy:"), ("💯 Excellent", ":100:"),
         ("🛠️ Craft", ":hammer_and_wrench:"), ("🌱 Growth", ":seedling:"),
         ("🙏 Thanks", ":pray:"), ("🤝 Teamwork", ":handshake:"),
         ("💡 Idea", ":bulb:"), ("🚀 Lift off", ":rocket:"),
         ("🔥 Brilliant", ":fire:"), ("😊 Smile", ":blush:")]
ALLOWED = {value for _, value in EMOJI}
BLOCKED = {"poop", "shit", "hankey", "-1", "thumbsdown", "thumbs_down",
           "middle_finger", "reversed_hand_with_middle_finger_extended", "clown_face"}


def selected_emoji(value):
    if not isinstance(value, str):
        return ""
    value = value.strip().casefold()
    base = re.sub(r"::skin-tone-[2-6]:?$", "", value).strip(":")
    if base in BLOCKED:
        return ""
    # Unknown/forged picker values are silently omitted as well.
    return value if value in ALLOWED else ""


def delivery_facts(ledger, evidence, acknowledged=False):
    """Only authoritative receipt metadata enters delivery narration."""
    destinations = [("recipient", "DM")] + ([("shared", "Ledge Chat")] if evidence["public"] else [])
    statuses = {key: evidence["deliveries"].get(key, {}).get("status", "pending") for key, _ in destinations}
    outcome = ("delivered" if all(s == "delivered" for s in statuses.values()) else
               "partial" if "delivered" in statuses.values() else "pending" if "pending" in statuses.values() else
               "failed" if "failed" in statuses.values() else "cancelled")
    xp_result = "17 XP awarded" if evidence["xp_awarded"] else "0 XP"
    summary = ("Kudos accepted; " + "; ".join(label + ": queued" for _, label in destinations) if acknowledged else
               outcome + "; " + "; ".join(label + ": " + statuses[key] for key, label in destinations))
    summary += "; once-only XP result: " + xp_result
    recipient = ledger.sources.member(evidence["recipient"]) or {}
    return {"summary": summary, "recipient_full_name": " ".join(str(recipient.get(k) or "").strip() for k in ("firstname", "lastname")).strip() or None,
            "recipient_slack_id": ledger.sources.slack_id(evidence["recipient"]),
            "delivery_status": "queued" if acknowledged else outcome,
            "dm_status": "queued" if acknowledged else statuses["recipient"],
            "public_status": ("queued" if acknowledged else statuses["shared"]) if evidence["public"] else "not requested",
            "xp_result": xp_result}
