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


def multi_external_input(key, label, selected=(), optional=True, max_selected_items=20):
    element = {"type": "multi_external_select", "action_id": key, "min_query_length": 0,
               "max_selected_items": max_selected_items,
               "placeholder": {"type": "plain_text", "text": label[:150]}}
    if selected:
        element["initial_options"] = list(selected)[:max_selected_items]
    return {"type": "input", "block_id": key, "optional": optional,
            "label": {"type": "plain_text", "text": label}, "element": element}


def file_input(key, label, optional=True):
    return {"type": "input", "block_id": key, "optional": optional,
            "label": {"type": "plain_text", "text": label},
            "element": {"type": "file_input", "action_id": key,
                        "filetypes": ["jpg", "jpeg", "png", "gif"], "max_files": 1}}


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
            elif value.get("type") in ("multi_static_select", "multi_external_select"):
                out[action] = [item.get("value") for item in value.get("selected_options", []) if item.get("value")]
            elif value.get("type") == "file_input":
                out[action] = list(value.get("selected_files") or [])
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
        section("The Ledger customizes messages using relevant game facts. Observation is on by default for eligible members in configured Ledger channels, independently of joining, after an explanatory notice. The System records suggestions for audit only; they do not change XP or ranks or send recognition messages. To opt out, use /ledger preferences and uncheck Allow observation. Joining, observation, and arrival mentions are separate choices. Participation never changes tool safety clearances."),
        checkbox("agree", "I choose to participate")], {"sponsor": sponsor}, "Opt in")


def participation(ledger, member_id, title="Already opted in"):
    participant = ledger.participant(member_id)
    rank = ledger.presentation(participant["rank"])
    blocks = [section("Your opt-in is saved. You do not need to join again."),
              section(f"*Rank:* {escape(rank['name'])}\n*XP:* {escape(participant['xp'])}")]
    if participant.get("import_pending"):
        blocks.append(section("Your verified history import is queued or in progress. The Ledger will send a summary when it finishes."))
    if not ledger.active(member_id):
        blocks.append(section("Your opt-in is retained, but Ledger access is currently unavailable. Ask makerspace staff to check your member and Slack account status."))
    blocks.append(section("Use /ledger for progress or /ledger leave to opt out. Channel invitations and messages are processed in the background."))
    return modal("dismiss", title, blocks, submit="Done")


def kudos_recipient():
    return modal("kudos_recipient", "Give kudos", [select_input("recipient", "Choose a member first")], {"key": str(uuid4())})


def admin_invitation():
    return modal("admin_invitation", "Invite to The Ledger", [
        select_input("invite_recipient", "Member to invite"),
        text_input("sender", "Sender name (optional)", optional=True, max_length=100),
        section("Leave sender blank to use your real name. A sender name replaces your real name in the invitation."),
        text_input("message", "Personal message (optional)", optional=True, multiline=True),
        section("This sends an invitation. The recipient chooses whether to join.")], {"key": str(uuid4())}, "Send invitation")


def kudos_form(ledger, recipient, key, draft=None):
    from .kudos import require_recipient
    require_recipient(ledger, recipient)
    draft = draft or {}
    participating = ledger.active(recipient)
    member = ledger.sources.member(recipient)
    name = " ".join([member.get("firstname", ""), member.get("lastname", "")]).strip()
    blocks = [section(f"Kudos for *{escape(name)}*"),
              {"type": "actions", "elements": [button("Change recipient", "kudos_change", key)]}]
    if not participating:
        blocks.append(section("This member will receive your kudos message, but will not earn XP because they are not participating in The Ledger."))
        blocks.append(select_input("invitation", "Also invite this member?", [option("Send kudos only", "no"), option("Send kudos and invite them to The Ledger", "yes")],
                                   selected=option("Send kudos and invite them to The Ledger" if draft.get("invitation") == "yes" else "Send kudos only", draft["invitation"]) if draft.get("invitation") else None))
    blocks.append(text_input("message", "Your kudos message", draft.get("message", ""), multiline=True))
    from .kudos import EMOJI, selected_emoji
    selected = selected_emoji(draft.get("emoji"))
    blocks.append(select_input("emoji", "Emoji (optional)", [option(label, value) for label, value in EMOJI],
                               selected=next((option(label, value) for label, value in EMOJI if value == selected), None), optional=True))
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


def home_processing():
    return {"type": "home", "callback_id": "ledger_home_processing", "blocks": [
        {"type": "header", "text": {"type": "plain_text", "text": "Character Sheet"}},
        section("Processing...")
    ]}


def home_private_metadata(ledger, member_id):
    participant = ledger.participant(member_id)
    generation = (participant or {}).get("consent_generation", 0)
    return f"{member_id}:{generation}"


def avatar_block(ledger, member):
    from .avatars import visible
    row = visible(ledger, member)
    if row and row.get("avatar", {}).get("file_id"):
        return {"type": "image", "slack_file": {"id": row["avatar"]["file_id"]},
                "alt_text": "Your fantasy maker avatar"}
    return None


def home(ledger, member_id, *, rank_icon_file_id=None, skill_tree_file_id=None):
    p = ledger.participant(member_id)
    if not ledger.active(member_id):
        return {"type": "home", "callback_id": "ledger_home_public",
            "private_metadata": home_private_metadata(ledger, member_id), "blocks": [section("*The Ledger*\nChoose your own path through learning, making, and helping."),
            {"type": "actions", "elements": [button("Opt in", "join", ""), button("Preferences", "preferences", "")]}]}
    display = ledger.presentation(p["rank"])
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": "Character Sheet"}},
              section(f"{display['emoji']} *{escape(display['name'])}* · {p['xp']} XP")]
    avatar = avatar_block(ledger, member_id)
    if avatar:
        blocks.append(avatar)
    elif rank_icon_file_id:
        blocks.append({"type": "image", "title": {"type": "plain_text", "text": display["name"]},
                       "slack_file": {"id": rank_icon_file_id}, "alt_text": f"Rank icon for {display['name']}"})
    if skill_tree_file_id:
        blocks.append({"type": "image", "title": {"type": "plain_text", "text": "Your skill tree"},
                       "slack_file": {"id": skill_tree_file_id},
                       "alt_text": "Your current skill paths and clearance states"})
    blocks.extend([navigation(),
              section("Choose your next step: `/ledger-skills`, `/ledger-quests`, `/ledger-mentor`, `/kudos`, or `/ledger-project`."),
              section("*Your progress*\n" + "\n".join(f"{k.replace('_', ' ').title()}: {v}" for k, v in p.get("metrics", {}).items()))])
    if p["rank"] <= 2:
        blocks.append(section("*A small first step*\nTry a personalized keychain or another safe First Build. Choose your own materials and pace; ask a Success Buddy for help. Submit with `/ledger-quests submit first-build`."))
    elif p["rank"] <= 4:
        blocks.append(section("*Pass it on*\nComfortable with a tool? Ask its resource manager about becoming a volunteer checkout approver, or offer a small skill-sharing session. Teaching and project help both count as mentoring."))
    else:
        blocks.append(section("*Choose your next contribution*\nDevelop another mentor, design a class, or leave a usable stewardship handoff. Tool Captain, Workshop Instructor, Design Challenge Judge, and Resource Manager appointments remain human decisions. Choose a commitment that fits your capacity."))
    blocks.extend(progress_blocks(ledger, member_id))
    projects = ledger.store.select("ledger_projects")[-10:]
    if projects:
        blocks.append(section("*Project gallery*"))
        for project in projects:
            title = escape(project["title"])
            blocks.append(section(f"<{project['permalink']}|{title}>" if project.get("permalink") else title))
    blocks.append({"type": "actions", "elements": [button("Current rank channel", "reinvite", ""), button("Opt out", "leave", "")]})
    from .admin_access import help_text
    administrative_help = help_text(ledger, member_id)
    if administrative_help:
        blocks.append(section(administrative_help))
    return {"type": "home", "callback_id": "ledger_home_generated",
            "private_metadata": home_private_metadata(ledger, member_id), "blocks": blocks}


def navigation():
    return {"type": "actions", "elements": [button(label, action, "") for label, action in
        [("Next rank", "progress"), ("Browse quests", "browse_quests"), ("Skill tree", "skill_tree"),
         ("Achievements", "achievements"), ("Preferences", "preferences")]]}


def progress_blocks(ledger, member):
    from .progress import progress
    facts = progress(ledger, member)
    blocks = [section(f"*Next rank:* {escape(facts['next_rank'] or 'Highest configured rank held')}\n*Remaining XP:* {facts['remaining_xp']}")]
    if facts["milestones"]:
        blocks.append(section("\n".join(f"{m['metric'].replace('_', ' ').title()}: {m['current']} / {m['required']} (remaining {m['remaining']})" for m in facts["milestones"])))
    if facts["blockers"]:
        blocks.append(section("*Private eligibility details*\n" + "\n".join(escape(b) for b in facts["blockers"])))
    if facts["suggestions"]:
        blocks.append(section("*Optional next steps*\n" + "\n".join(escape(b) for b in facts["suggestions"])))
    return blocks


def character_sheet(ledger, member):
    from .progress import progress
    facts = progress(ledger, member)
    blocks = [section(f"*{escape(facts['rank'])}* · {facts['xp']} XP\n*Deepest cleared skill:* {escape(facts.get('highest_skill', 'No recorded clearance'))}")]
    avatar = avatar_block(ledger, member)
    if avatar:
        blocks.append(avatar)
    if facts["metrics"]:
        blocks.append(section("\n".join(f"{k.replace('_', ' ').title()}: {escape(v)}" for k, v in facts["metrics"].items())))
    else:
        pending = (ledger.participant(member) or {}).get("import_pending")
        blocks.append(section("Verified history import pending; progress may be incomplete." if pending else "No recorded milestones yet."))
    blocks.append(navigation())
    recorded = ledger.store.select("ledger_awards", {"member_id": member})
    blocks.append(section(f"Recorded achievements: {len(recorded)}. Open Achievements for details."))
    return modal("dismiss", "Character sheet", blocks, submit="Done")


def progress_view(ledger, member):
    return modal("dismiss", "Your next rank", progress_blocks(ledger, member) + [
        {"type": "actions", "elements": [button("Suggest a next step", "guidance_next_step", "")]},
        navigation()], submit="Done")


def preferences(ledger, member):
    if not ledger.member_eligible(member):
        from .domain import Denied
        raise Denied("A valid linked human Slack account is required.")
    p = ledger.preference_profile(member)
    pref = p.get("preferences", {})
    return modal("preferences_save", "Ledger preferences", [
        section("Observation is on by default for eligible members, whether or not you join The Ledger, after an explanatory notice. To opt out, uncheck Allow observation and save. The System observes only new eligible activity in configured Ledger channels, kudos issuance metadata, and verified volunteer activity. Original kudos text and DMs are excluded. Suggestions are audit-only and do not change XP or ranks or send recognition messages. Game participation and arrival mentions are separate. Ask staff about the audit process."),
        checkbox("observation", "Allow observation", pref.get("observation", True)),
        checkbox("arrival_mentions", "Allow arrival mentions", pref.get("arrival_mentions", True)),
        section("Personalized fantasy avatars use your profile, verified progress, your observed Ledger posts and your conversations with The Ledger. Disable avatars to use the default character image. An optional reference image replaces your Slack profile photo; validation runs in the background."),
        checkbox("avatars", "Allow personalized avatars", pref.get("avatars", True)),
        {**file_input("avatar_reference", "Reference image (optional, smaller than 2 MB)"),
         "element": {"type": "file_input", "action_id": "avatar_reference", "filetypes": ["jpg", "jpeg", "png", "webp"], "max_files": 1}},
        checkbox("remove_avatar_reference", "Remove current avatar reference image")], submit="Save")


def achievements(ledger, member):
    ledger.require(member)
    rows = ledger.store.select("ledger_awards", {"member_id": member})
    blocks = [section("*Recorded milestones and achievements*")]
    for row in sorted(rows, key=lambda r: r.get("at"), reverse=True)[:35]:
        if row.get("kind") not in ("review", "rank_correction", "rank_hold_release"):
            blocks.append(section(escape(row.get("name") or row.get("kind", "Achievement").replace("_", " ").title()) + (" · " + escape(row["delta"]) + " XP" if row.get("delta") else "")))
    for row in ledger.store.select("ledger_evidence", {"kind": "ai_decision", "member_id": member, "status": "committed"})[-30:]:
        if row.get("achievement"):
            blocks.append(section(f"*{escape(row['achievement']['title'])}*\n{escape(row['achievement']['description'])}"))
    for row in ledger.store.select("ledger_evidence", {"kind": "quest_completion", "member_id": member})[-10:]:
        quest = ledger.store.get("ledger_quests", row["quest_revision"]) or {}
        blocks.append(section("*Verified quest completion:* " + escape(quest.get("title", "Quest"))))
    for row in ledger.store.select("ledger_evidence", {"kind": "submission", "member_id": member, "status": "approved"})[-10:]:
        blocks.append(section("*Verified milestone:* " + escape(row["achievement"].replace("_", " ").title())))
    return modal("dismiss", "Achievements", blocks[:95] or [section("No recorded achievements yet.")], submit="Done")


def quest_browser(ledger, member, selected=None):
    from .quests import Quests
    service = Quests(ledger)
    blocks = [select_input("quest_selection", "Search quest titles", dispatch=True)]
    metadata = {}
    if selected:
        item = service.detail(member, selected)
        blocks[0]["element"]["initial_option"] = option(item["title"], selected)
        blocks.extend([section(f"*{escape(item['title'])}*\n{escape(item.get('description') or item.get('criteria', ''))}"),
                       section("*Acceptance criteria*\n" + escape(item.get("criteria", "")))])
        duration = item.get("duration")
        if isinstance(duration, dict):
            blocks.append(section(f"*Estimated time:* {duration.get('value')} {escape(duration.get('unit', ''))}"))
        photo = item.get("photo")
        if isinstance(photo, dict) and photo.get("id") and photo.get("available", True):
            blocks.append({"type": "image", "slack_file": {"id": photo["id"]},
                           "alt_text": "Example photo for " + item["title"][:150]})
        elif isinstance(photo, dict) and photo.get("id"):
            blocks.append(section("*Example photo:* unavailable."))
        from .quest_policy import cooperative, generated, individual
        if individual(item):
            author_uid = None if generated(item) else ledger.sources.slack_id(item["creator"])
            author = "The Ledger" if generated(item) else (f"<@{author_uid}>" if author_uid and ledger.active(item["creator"]) else escape(author_uid or "Unavailable Slack identity"))
            rank_label = "Minimum rank" if item.get("rank_mode") == "minimum" else "Exact target rank"
            blocks.extend([section(f"*Creator:* {author}\n*{rank_label}:* {escape(ledger.presentation(item['target_rank'])['name'])}\n*Approved reward:* {item['reward']} XP\n*Verification:* independent authorized reviewer"),
                section("*Prerequisites*\nShops: " + escape(", ".join((ledger.sources.shop(i) or {}).get("name", i) for i in item["shop_ids"]) or "None") +
                        "\nTools: " + escape(", ".join((ledger.sources.tool(i) or {}).get("name", i) for i in item["tool_ids"]) or "None"))])
            accepted = service.acceptance(member, item["logical_id"])
            blocks.append({"type": "actions", "elements": [button("Submit completion" if accepted else "Accept quest", "quest_complete_form" if accepted else "quest_accept", item["_id"])]})
        elif item.get("kind") == "challenge":
            blocks.append(section("*Verification:* existing milestone evidence requirements and independent review.\nUse /ledger-quests submit " + escape(item["_id"])))
        else:
            if cooperative(item):
                if generated(item):
                    author, eligibility = "The Ledger", f"Intended rank slot: {item['target_rank']} (any participant may join)"
                else:
                    uid = ledger.sources.slack_id(item["creator"])
                    author = f"<@{uid}>" if uid and ledger.active(item["creator"]) else "Unavailable Slack identity"
                    eligibility = "Minimum rank: " + ledger.presentation(item["target_rank"])["name"]
                blocks.append(section(f"*Creator:* {author}\n*{escape(eligibility)}*\n*Approved reward:* {item['reward']} XP per verified contributor"))
                disciplines = "\n".join(f"{d['name']}: {d['expectation']}" for d in item["disciplines"])
            else:
                disciplines = ", ".join(item["roles"])
            blocks.append(section("*Disciplines:* " + escape(disciplines) + "\nUse /ledger-quests join " + escape(item["_id"]) + " <discipline>, then contribute for independent verification."))
        metadata["selected"] = selected
    blocks.append({"type": "actions", "elements": [button("Create quest", "quest_author", "")]})
    return modal("dismiss", "Explore quests", blocks, metadata, "Done")


def ledger_quest_review(quest, approve=True):
    blocks = [section(f"*The Ledger proposal* — {quest['quest_type']}, rank slot {quest['target_rank']}"),
        text_input("title", "Title", quest["title"], max_length=100),
        text_input("description", "Instructions", quest["description"], multiline=True),
        text_input("criteria", "Observable completion criteria", quest["criteria"], multiline=True),
        text_input("shops", "Shop prerequisite IDs (comma separated)", ", ".join(quest["shop_ids"]), optional=True),
        text_input("tools", "Tool prerequisite IDs (comma separated)", ", ".join(quest["tool_ids"]), optional=True)]
    if quest["quest_type"] == "cooperative":
        # Explicit name/expectation fields avoid a JSON-editing member workflow.
        for index in range(4):
            discipline = quest["disciplines"][index] if index < len(quest["disciplines"]) else {}
            blocks.extend([text_input(f"discipline_name_{index}", f"Discipline {index + 1}", discipline.get("name", ""), optional=index >= 2, max_length=40),
                text_input(f"discipline_expectation_{index}", f"Discipline {index + 1} contribution evidence", discipline.get("expectation", ""), optional=index >= 2, max_length=400)])
    blocks.extend([text_input("reward", "Whole-number reward per member (0–500 XP)", "100"),
        text_input("reason", "Review reason", optional=approve, multiline=True),
        section("Edits create a new reviewed revision; The Ledger remains the author. Publication grants no XP. "
                "Cooperative rewards require independent contribution verification and a final shared-outcome review.")])
    return modal("ledger_quest_review", "Review Ledger quest", blocks, {"quest": quest["_id"], "approve": approve},
                 "Approve quest" if approve else "Reject quest")


def quest_author(ledger, member, draft=None, suggestion=False):
    from .quests import Quests
    service = Quests(ledger)
    targets = service.targets(member)
    draft = draft or {}
    choices = [option(ledger.presentation(i)["name"], i) for i in targets]
    type_choices = [option("Individual", "individual"), option("Cooperative", "cooperative")]
    duration_choices = [option(unit.title(), unit) for unit in ("minutes", "hours", "days", "weeks")]
    eligible = {row["value"]: row for row in service.eligible_tools(member, limit=1000)}
    selected_tools = [option(eligible[value]["label"], value) for value in draft.get("tool_ids", draft.get("quest_tools", []))
                      if value in eligible]
    disciplines = draft.get("disciplines") if isinstance(draft.get("disciplines"), list) else []
    if not disciplines:
        disciplines = [{"name": draft.get(f"discipline_name_{index}", ""),
                        "expectation": draft.get(f"discipline_expectation_{index}", "")}
                       for index in range(4)
                       if draft.get(f"discipline_name_{index}") or draft.get(f"discipline_expectation_{index}")]
    if disciplines and not isinstance(disciplines[0], dict):
        disciplines = [{"name": value, "expectation": ""} for value in disciplines]
    duration = draft.get("duration") if isinstance(draft.get("duration"), dict) else {}
    result = modal("member_quest_submit", "Author a quest", [
        select_input("quest_type", "Quest type", type_choices,
                     selected=next((c for c in type_choices if c["value"] == draft.get("quest_type", "individual")), None)),
        text_input("title", "Title", draft.get("title", ""), max_length=100),
        text_input("description", "Description", draft.get("description", ""), multiline=True),
        text_input("criteria", "Observable acceptance criteria", draft.get("criteria", ""), multiline=True),
        select_input("target_rank", "Suggested minimum rank", choices, selected=next((c for c in choices if c["value"] == str(draft.get("target_rank"))), None)),
        multi_external_input("quest_tools", "Tools you are checked out on", selected_tools),
        text_input("duration_value", "Estimated duration", duration.get("value", draft.get("duration_value", "1")), max_length=3),
        select_input("duration_unit", "Duration unit", duration_choices,
                     selected=next((c for c in duration_choices if c["value"] == duration.get("unit", draft.get("duration_unit", "hours"))), None)),
        file_input("example_photo", "Optional example photo (JPEG, PNG, or GIF; 10 MiB max)"),
        *[field for index in range(4) for field in (
            text_input(f"discipline_name_{index}", f"Discipline {index + 1} name",
                       disciplines[index].get("name", "") if index < len(disciplines) else "", optional=True, max_length=40),
            text_input(f"discipline_expectation_{index}", f"Discipline {index + 1} observable expectation",
                       disciplines[index].get("expectation", "") if index < len(disciplines) else "", optional=True, multiline=True, max_length=400))],
        {"type": "actions", "elements": [button("Help me draft", "quest_draft_help", "draft"),
                                             button("Rewrite for clarity", "quest_draft_rewrite", "rewrite")]},
        section("Suggestions change only editable prose. You explicitly submit the proposal; an independent reviewer sets completion XP and a proposer bonus.")],
        {"revision_of": draft.get("revision_of"), "submission_key": draft.get("submission_key") or str(uuid4())}, "Submit for review")
    if suggestion:
        # Matching block/action IDs preserve Slack state. New block IDs apply
        # generated initial values while leaving the other fields intact.
        marker = str(uuid4())
        for block in result["blocks"]:
            if block.get("block_id") in ("title", "description", "criteria") or block.get("block_id", "").startswith(("discipline_name_", "discipline_expectation_")):
                block["block_id"] += ":suggestion:" + marker
    return result


def member_quest_review(ledger, quest, approve=True):
    from .quests import Quests
    from .domain import Denied
    service = Quests(ledger)
    try:
        targets = service.targets(quest["creator"])
    except Denied:
        participant = ledger.participant(quest["creator"]) or {}
        rules = ledger.store.get("ledger_rulesets", participant.get("ruleset")) or {}
        targets = [row["slot"] for row in rules.get("ranks", [])
                   if row.get("enabled") and row["slot"] <= participant.get("rank", 0)]
        if quest.get("target_rank") not in targets:
            targets.append(quest["target_rank"])
    ranks = [option(ledger.presentation(slot)["name"], slot) for slot in targets]
    types = [option("Individual", "individual"), option("Cooperative", "cooperative")]
    units = [option(unit.title(), unit) for unit in ("minutes", "hours", "days", "weeks")]
    tools = []
    for identifier in quest.get("tool_ids", []):
        tool = ledger.sources.tool(identifier) or {}
        shop = ledger.sources.shop(tool.get("shop_id")) or {}
        tools.append(option(f"{tool.get('name', identifier)} — {shop.get('name', 'Shop')}", identifier))
    explicit_shops = service.explicit_shops(quest)
    duration = quest.get("duration") or {"value": 1, "unit": "hours"}
    raw_disciplines = quest.get("disciplines") or []
    disciplines = [row if isinstance(row, dict) else {"name": row, "expectation": ""}
                   for row in (raw_disciplines if isinstance(raw_disciplines, list) else [])
                   if isinstance(row, (dict, str))]
    blocks = [section("*Original participant proposal*"),
        select_input("quest_type", "Quest type", types,
                     selected=next(c for c in types if c["value"] == quest.get("quest_type", "individual"))),
        text_input("title", "Title", quest["title"], max_length=100),
        text_input("description", "Description", quest["description"], multiline=True),
        text_input("criteria", "Observable completion criteria", quest["criteria"], multiline=True),
        select_input("target_rank", "Minimum rank", ranks,
                     selected=next((c for c in ranks if c["value"] == str(quest["target_rank"])), None)),
        multi_external_input("quest_tools", "Required tools", tools),
        section("*Shop-only prerequisites:* " + escape(", ".join(
            (ledger.sources.shop(identifier) or {}).get("name", identifier) for identifier in explicit_shops) or "None")),
        text_input("duration_value", "Estimated duration", duration["value"], max_length=3),
        select_input("duration_unit", "Duration unit", units,
                     selected=next(c for c in units if c["value"] == duration["unit"])),
        *[field for index in range(4) for field in (
            text_input(f"discipline_name_{index}", f"Discipline {index + 1} name",
                       disciplines[index].get("name", "") if index < len(disciplines) else "", optional=True, max_length=40),
            text_input(f"discipline_expectation_{index}", f"Discipline {index + 1} observable expectation",
                       disciplines[index].get("expectation", "") if index < len(disciplines) else "", optional=True, multiline=True, max_length=400))],
        text_input("reward", "Completion reward (0–500 XP)", "100", max_length=3),
        text_input("proposer_bonus", "Proposer approval bonus (0–500 XP)", "100", max_length=3),
        select_input("classification", "Milestone classification", [option(c.replace("_", " ").title(), c)
                     for c in ("challenge", "first_build", "boss", "stewardship", "develop_mentor", "mentoring")]),
        text_input("catalog", "Existing catalog ID (specialized milestones)", optional=True),
        text_input("reason", "Review reason", optional=approve, multiline=True)]
    if (quest.get("photo") or {}).get("id"):
        blocks.insert(1, {"type": "image", "slack_file": {"id": quest["photo"]["id"]},
                          "alt_text": "Participant-provided example photo"})
    blocks.append(section("Edits create an immutable reviewed child revision. Tool additions remain limited to the proposer's current eligible checkouts."))
    return modal("member_quest_review", "Review quest", blocks,
                 {"quest": quest["_id"], "approve": approve, "proposer": quest["creator"]},
                 "Approve quest" if approve else "Reject quest")


def delegates(ledger, actor):
    from .authority import Authority, CAPABILITIES
    if not Authority(ledger).staff_scope(actor):
        raise ValueError("Delegation requires current staff authority.")
    blocks = [select_input("delegate", "Choose a participating member")]
    blocks.extend(checkbox("cap_" + cap, cap.replace("_", " ").title()) for cap in sorted(CAPABILITIES))
    blocks.extend([select_input("scope_kind", "Authority scope", [option(k.title(), k) for k in ("global", "shops", "quest")]),
        text_input("scope_ids", "Shop IDs or quest ID (any revision)", optional=True), text_input("reason", "Required reason", multiline=True)])
    from .read_options import optimized_reads
    scope = Authority(ledger).staff_scope(actor)
    if optimized_reads():
        query = {"kind": "delegation", "status": "active"}
        if not scope or scope["kind"] != "global":
            query["grantor"] = actor
        grants = ledger.store.select("ledger_relationships", query,
            projection=dict.fromkeys(("delegate", "grantor", "capabilities", "scope"), 1),
            sort=[("at", 1), ("_id", 1)], limit=30, max_time_ms=2000)
        names = ledger.sources.slack_ids([g["delegate"] for g in grants])
    else:
        grants = ledger.store.select("ledger_relationships", {"kind": "delegation", "status": "active"})[:30]
        names = {g["delegate"]: ledger.sources.slack_id(g["delegate"]) for g in grants}
    for g in grants:
        if scope and (scope["kind"] == "global" or g["grantor"] == actor):
            blocks.append(section(f"Grant {escape(g['_id'])}\nDelegate: {escape(names.get(g['delegate']))}\nCapabilities: {escape(', '.join(g['capabilities']))}\nScope: {escape(json.dumps(g['scope']))}"))
            blocks.append({"type": "actions", "elements": [button("Revoke grant", "delegate_revoke", g["_id"])]})
    return modal("delegate_grant", "Review delegates", blocks, submit="Grant authority")
