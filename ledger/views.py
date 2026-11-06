"""Slack Block Kit views. Forms never contain LLM-produced labels or consent text."""
import json
from uuid import uuid4

from .messages import button, escape, section


def text_input(key, label, initial="", optional=False, multiline=False, max_length=2000):
    element = {"type": "plain_text_input", "action_id": key, "multiline": multiline, "max_length": min(3000, max_length)}
    if initial:
        element["initial_value"] = str(initial)
    return {"type": "input", "block_id": key, "optional": optional,
            "label": {"type": "plain_text", "text": label}, "element": element}


def option(label, value):
    return {"text": {"type": "plain_text", "text": str(label)[:75]}, "value": str(value)}


def select_input(key, label, choices=None, selected=None, optional=False, dispatch=False):
    element = {"type": "static_select" if choices is not None else "external_select", "action_id": key,
               "placeholder": {"type": "plain_text", "text": label[:150]}}
    if choices is not None:
        element["options"] = choices[:100]
    else:
        element["min_query_length"] = 0
    if selected:
        element["initial_option"] = selected
    return {"type": "input", "block_id": key, "optional": optional, "dispatch_action": dispatch,
            "label": {"type": "plain_text", "text": label}, "element": element}


def checkbox(key, label, checked=False, hint=None):
    item = option(label, "yes")
    element = {"type": "checkboxes", "action_id": key, "options": [item]}
    if checked:
        element["initial_options"] = [item]
    block = {"type": "input", "block_id": key, "optional": True,
             "label": {"type": "plain_text", "text": label}, "element": element}
    if hint:
        block["hint"] = {"type": "plain_text", "text": hint}
    return block


def modal(callback, title, blocks, metadata=None, submit="Continue"):
    return {"type": "modal", "callback_id": callback, "title": {"type": "plain_text", "text": title[:24]},
            "close": {"type": "plain_text", "text": "Cancel"}, "submit": {"type": "plain_text", "text": submit},
            "private_metadata": json.dumps(metadata or {}), "blocks": blocks}


def values(body):
    raw = body.get("view", {}).get("state", {}).get("values", {})
    out = {}
    for block in raw.values():
        for action, value in block.items():
            if value.get("type") in ("static_select", "external_select", "radio_buttons"):
                out[action] = (value.get("selected_option") or {}).get("value")
            elif value.get("type") == "checkboxes":
                out[action] = bool(value.get("selected_options"))
            elif value.get("type") == "users_select":
                out[action] = value.get("selected_user")
            else:
                out[action] = value.get("value", "")
    return out


def consent(sponsor=None):
    return modal("consent", "Join The Ledger", [
        section("The Ledger recognizes learning, mentoring, and community contribution. Joining imports verified makerspace history and invites you to private game channels."),
        section("If you opt out, channel access and game announcements stop. Your skills and XP are retained and eligible makerspace activity continues accruing silently. Peer-addressed kudos may still be delivered, but earns no kudos XP while you are opted out."),
        section("AI customizes messages using relevant game facts. Participation is optional and never changes tool safety clearances."),
        checkbox("agree", "I choose to participate")], {"sponsor": sponsor}, "Opt in")


def kudos_recipient():
    return modal("kudos_recipient", "Give kudos", [select_input("recipient", "Choose a member first")], {"key": str(uuid4())})


def kudos_form(ledger, recipient, key, draft=None):
    draft = draft or {}
    participating = ledger.active(recipient)
    member = ledger.sources.member(recipient)
    if not member or not ledger.sources.good_standing(recipient):
        raise ValueError("Choose a member in good standing with a valid Slack identity.")
    name = " ".join([member.get("firstname", ""), member.get("lastname", "")]).strip()
    blocks = [section(f"Kudos for *{escape(name)}*"),
              {"type": "actions", "elements": [button("Change recipient", "kudos_change", key)]}]
    if not participating:
        blocks.append(section("This member will receive your kudos message, but will not earn XP because they are not participating in The Ledger."))
        blocks.append(select_input("invitation", "Also invite this member?", [option("Send kudos only", "no"), option("Send kudos and invite them to The Ledger", "yes")],
                                   selected=option("Send kudos and invite them to The Ledger" if draft.get("invitation") == "yes" else "Send kudos only", draft["invitation"]) if draft.get("invitation") else None))
    blocks.append(text_input("message", "Your kudos message", draft.get("message", ""), multiline=True))
    blocks.append(section("Slack formatting and emoji are welcome. Your message is delivered as written."))
    shop = ledger.sources.shop(draft.get("shop")) if draft.get("shop") else None
    blocks.append(select_input("shop", "Shop (optional)", selected=option(shop["name"], str(shop["_id"])) if shop else None, optional=True, dispatch=True))
    if shop:
        tool = ledger.sources.tool(draft.get("tool")) if draft.get("tool") else None
        tool_block = select_input("tool", "Tool (optional)", selected=option(tool["name"], str(tool["_id"])) if tool else None, optional=True)
        # Slack preserves input state for matching block/action IDs. A new shop must
        # change the block ID to actually clear the previous tool in the client.
        tool_block["block_id"] = "tool:" + str(shop["_id"])
        blocks.append(tool_block)
    blocks.append(checkbox("public", "Make public", draft.get("public", False), "Also share this kudos in Ledge Chat, the private gamification channel."))
    return modal("kudos_send", "Give kudos", blocks, {"recipient": recipient, "participating": participating, "key": key, "shop": draft.get("shop")}, "Send kudos")


def ranks_form(rules):
    blocks = [section("Names and emoji update current displays. XP floors and milestones apply only to new participants; existing participants keep their progression version.")]
    for rank in rules["ranks"]:
        i = rank["slot"]
        blocks.extend([section(f"*Rank slot {i}*"), text_input(f"name_{i}", "Name", rank["name"], max_length=40),
                       text_input(f"emoji_{i}", "Rank emoji", rank["emoji"], optional=i == 7, max_length=100),
                       text_input(f"floor_{i}", "XP floor", rank["floor"] or "", optional=i == 7, max_length=30),
                       text_input(f"gates_{i}", "Milestone requirements (JSON)", json.dumps(rank["requirements"]), multiline=True),
                       checkbox(f"enabled_{i}", "Rank enabled", rank["enabled"])])
    return modal("ranks_preview", "Configure ranks", blocks, {"base": rules["_id"]}, "Preview")


def ranks_preview(ranks, draft_id):
    return modal("ranks_publish", "Preview ranks", [section("Publication creates a new progression version for future participants. No existing member is migrated or demoted.")] +
                 [section(f"{r['emoji']} *{escape(r['name'])}* — {'XP ' + str(r['floor']) if r['enabled'] else 'inactive'}\n{escape(json.dumps(r['requirements']))}") for r in ranks],
                 {"draft": draft_id}, "Publish")


def home(ledger, member_id):
    p = ledger.participant(member_id)
    if not ledger.active(member_id):
        return {"type": "home", "blocks": [section("*The Ledger*\nChoose your own path through learning, making, and helping."),
            {"type": "actions", "elements": [button("Opt in", "join", "")]}]}
    display = ledger.presentation(p["rank"])
    rules = ledger.store.get("ledger_rulesets", p["ruleset"])
    blocks = [section(f"{display['emoji']} *{escape(display['name'])}* · {p['xp']} XP"),
              section("Choose your next step: `/ledger-skills`, `/ledger-quests`, `/ledger-mentor`, `/kudos`, or `/ledger-project`."),
              section("*Your progress*\n" + "\n".join(f"{k.replace('_', ' ').title()}: {v}" for k, v in p.get("metrics", {}).items()))]
    if p["rank"] <= 2:
        blocks.append(section("*A small first step*\nTry a personalized keychain or another safe First Build. Choose your own materials and pace; ask a Success Buddy for help. Submit with `/ledger-quests submit first-build`."))
    elif p["rank"] <= 4:
        blocks.append(section("*Pass it on*\nComfortable with a tool? Ask its resource manager about becoming a volunteer checkout approver, or offer a small skill-sharing session. Teaching and project help both count as mentoring."))
    else:
        blocks.append(section("*Choose your next contribution*\nDevelop another mentor, design a class, or leave a usable stewardship handoff. Tool Captain, Workshop Instructor, Design Challenge Judge, and Resource Manager appointments remain human decisions. Choose a commitment that fits your capacity."))
    if p["rank"] < 7 and rules["ranks"][p["rank"]]["enabled"]:
        nxt = rules["ranks"][p["rank"]]
        blocks.append(section(f"*Next: {escape(ledger.presentation(nxt['slot'])['name'])}*\nXP floor: {nxt['floor']}\n" +
            "\n".join(f"{k.replace('_', ' ').title()}: {v}" for k, v in nxt["requirements"].items()) + "\nAdvancement also requires current paid or earned membership."))
    projects = ledger.store.select("ledger_projects")[-10:]
    if projects:
        blocks.append(section("*Project gallery*"))
        for project in projects:
            title = escape(project["title"])
            blocks.append(section(f"<{project['permalink']}|{title}>" if project.get("permalink") else title))
    blocks.append({"type": "actions", "elements": [button("Current rank channel", "reinvite", ""), button("Opt out", "leave", "")]})
    return {"type": "home", "blocks": blocks}
