"""Self-only, authoritative cumulative progression facts."""
from .rules import amount
from .skills import highest_skill


def progress(ledger, member):
    p = ledger.require(member)
    rules = ledger.store.get("ledger_rulesets", p["ruleset"])
    enabled = [r for r in rules["ranks"] if r["enabled"]]
    nxt = next((r for r in enabled if r["slot"] > p["rank"]), None)
    blockers = []
    if p.get("import_pending"):
        blockers.append("Verified history import pending; progress may be incomplete.")
    if p.get("rank_hold"):
        blockers.append("Rank advancement is held for staff review.")
    if not ledger.sources.eligible_for_rank(member, ledger.store.get("ledger_evidence", f"coverage:{member}")):
        blockers.append("Current membership eligibility needs staff verification.")
    result = {"rank": ledger.presentation(p["rank"])["name"], "slot": p["rank"], "xp": p["xp"],
              "ruleset": p["ruleset"], "metrics": p.get("metrics", {}), "blockers": blockers,
              "next_rank": None, "remaining_xp": "0", "milestones": [], "suggestions": [],
              **highest_skill(ledger.sources, member)}
    if not nxt:
        result["suggestions"] = ["You hold the highest configured rank. Choose a contribution that fits your interests."]
        return result
    requirements = {}
    for r in enabled:
        if r["slot"] > nxt["slot"]:
            break
        for metric, minimum in r["requirements"].items():
            requirements[metric] = max(amount(minimum), requirements.get(metric, amount(0)))
    result.update(next_rank=ledger.presentation(nxt["slot"])["name"],
                  remaining_xp=str(max(amount(0), amount(nxt["floor"]) - amount(p["xp"]))))
    suggestions = {"checkouts": "Explore /ledger-skills for a safe next clearance.", "shops": "Explore a new enabled shop with /ledger-skills.",
                   "completed_shops": "Review your shop skill tree for remaining clearances.",
                   "mentoring": "Offer skill sharing and record it with /ledger-mentor log.", "learners": "Invite a willing learner to a skill-sharing session.",
                   "volunteer": "Ask about available volunteer tasks.", "first_build": "Browse quests for First Build.",
                   "boss": "Browse approved stretch goals.", "develop_mentor": "Guide a mentor, then document their independently verified teaching.",
                   "stewardship": "Document completed stewardship and a usable handoff."}
    for key, required in requirements.items():
        current = amount(p.get("metrics", {}).get(key, 0))
        deficit = max(amount(0), required - current)
        result["milestones"].append({"metric": key, "current": str(current), "required": str(required), "remaining": str(deficit)})
        if deficit:
            result["suggestions"].append(suggestions.get(key, "Browse quests for eligible opportunities."))
    if amount(result["remaining_xp"]):
        result["suggestions"].append("Browse quests and verified learning or volunteer opportunities for XP.")
    result["suggestions"] = list(dict.fromkeys(result["suggestions"]))[:3]
    return result
