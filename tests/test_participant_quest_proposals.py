import json
from unittest.mock import Mock

import pytest
from slack_sdk.errors import SlackApiError

from conftest import oid
from ledger.domain import Denied
from ledger.ledger_quests import LedgerQuests
from ledger.quests import Quests
from ledger.slack_app import SlackUI
from ledger.review_notifications import activities, render
from ledger import views
from test_slack import form


def member(value):
    return str(oid(value))


def rank(ledger, store, value, slot):
    if not ledger.participant(member(value)):
        ledger.join(member(value))
    participant = ledger.participant(member(value))
    participant.update(rank=slot, import_pending=False)
    store.put("ledger_participants", participant)


def checkout(source, person, tool, key):
    source.data["tool_checkouts"].append({"_id": oid(key), "member_id": oid(person), "tool_id": oid(tool)})


def proposal(ledger, store, source, *, author=1, quest_type="individual", reward=130, bonus=100):
    rank(ledger, store, author, 1)
    checkout(source, author, 311, 900 + author)
    disciplines = ([{"name": "Design", "expectation": "Document a tested design."},
                    {"name": "Fabrication", "expectation": "Build and inspect the result."}]
                   if quest_type == "cooperative" else [])
    quest = Quests(ledger).draft(member(author), "Participant jig", "Build a safe useful jig.",
        "Demonstrate the working result and feedback applied.", 1, tools=[member(311)],
        disciplines=disciplines, quest_type=quest_type, duration={"value": 2, "unit": "hours"})
    Quests(ledger).submit_draft(member(author), quest["_id"])
    return Quests(ledger).publish(member(10), quest["_id"], reward, proposer_bonus=bonus)


def test_every_active_rank_can_propose_and_tool_options_are_live(joined):
    ledger, store, source, *_ = joined
    checkout(source, 1, 311, 901)
    checkout(source, 1, 312, 902)
    checkout(source, 1, 321, 903)
    source.data["tools"][1]["out_of_service"] = True
    source.data["shops"][1]["disabled"] = True
    service = Quests(ledger)
    assert service.targets(member(1)) == [1]
    assert service.eligible_tools(member(1)) == [{"value": member(311), "label": "Tool1-1 — Shop1", "shop_id": member(201)}]
    source.data["tool_checkouts"][0]["revoked_at"] = "revoked"
    assert service.eligible_tools(member(1)) == []
    with pytest.raises(Denied, match="current checkout"):
        service.draft(member(1), "Forged", "Description", "Observable result", 1, tools=[member(311)])


def test_minimum_rank_bonus_individual_royalty_and_self_completion(joined):
    ledger, store, source, *_ = joined
    quest = proposal(ledger, store, source)
    assert quest["rank_mode"] == "minimum" and quest["duration"] == {"value": 2, "unit": "hours"}
    assert ledger.participant(member(1))["xp"] == "100"
    assert store.get("ledger_evidence", "quest-proposer-approval:" + quest["logical_id"])["xp"] == "100"

    rank(ledger, store, 2, 3)
    checkout(source, 2, 311, 912)
    Quests(ledger).accept(member(2), quest["_id"])
    evidence = Quests(ledger).submit(member(2), quest["_id"], "Observed result")
    Quests(ledger).verify(member(10), evidence["_id"])
    assert ledger.participant(member(2))["xp"] == "130"
    assert ledger.participant(member(1))["xp"] == "107"  # 6.5 rounds half-up.
    royalties = store.select("ledger_evidence", {"kind": "quest_proposer_royalty"})
    assert len(royalties) == 1 and royalties[0]["first"] is True and royalties[0]["xp"] == "7"

    Quests(ledger).accept(member(1), quest["_id"])
    own = Quests(ledger).submit(member(1), quest["_id"], "Proposer's independently reviewed result")
    with pytest.raises(Denied):
        Quests(ledger).verify(member(1), own["_id"])
    Quests(ledger).verify(member(10), own["_id"])
    assert len(store.select("ledger_evidence", {"kind": "quest_proposer_royalty"})) == 1


def test_cooperative_share_rounds_once_and_excludes_proposer(joined):
    ledger, store, source, *_ = joined
    quest = proposal(ledger, store, source, quest_type="cooperative", reward=125)
    service = LedgerQuests(ledger)
    rank(ledger, store, 3, 2)
    checkout(source, 2, 311, 922)
    checkout(source, 3, 311, 923)
    for person, discipline in ((2, "Design"), (3, "Fabrication")):
        service.contribute(member(person), quest["_id"], "join", role=discipline)
        service.contribute(member(person), quest["_id"], "submit", description="Observable contribution")
        service.contribute(member(10), quest["_id"], "verify", member=member(person))
    service.finalize(member(10), quest["_id"], "Independently observed shared outcome")
    assert ledger.participant(member(1))["xp"] == "113"  # 250 * .05 = 12.5, rounded once.
    royalty = store.select("ledger_evidence", {"kind": "quest_proposer_royalty"})
    assert len(royalty) == 1 and royalty[0]["credited_xp"] == "250" and royalty[0]["xp"] == "13"


def test_member_cooperative_quest_may_use_a_configured_rank_name(joined):
    ledger, store, source, *_ = joined
    rank(ledger, store, 1, 1)
    rank(ledger, store, 2, 1)
    rank(ledger, store, 3, 1)
    quest = Quests(ledger).draft(member(1), "Newbie workshop project", "Build a useful shared project.",
        "Demonstrate the completed project.", 1, quest_type="cooperative",
        duration={"value": 2, "unit": "hours"}, disciplines=[
            {"name": "Design", "expectation": "Document the design."},
            {"name": "Fabrication", "expectation": "Build and inspect the result."}])
    Quests(ledger).submit_draft(member(1), quest["_id"])
    quest = Quests(ledger).publish(member(10), quest["_id"], 100)

    service = LedgerQuests(ledger)
    assert service.available(member(2), quest) == quest
    for person, discipline in ((2, "Design"), (3, "Fabrication")):
        service.contribute(member(person), quest["_id"], "join", role=discipline)
        service.contribute(member(person), quest["_id"], "submit", description="Observable contribution")
        service.contribute(member(10), quest["_id"], "verify", member=member(person))
    assert service.finalize(member(10), quest["_id"], "Observed shared outcome")["status"] == "completed"


def test_cooperative_finalization_rechecks_member_minimum_rank(joined):
    ledger, store, source, *_ = joined
    quest = proposal(ledger, store, source, quest_type="cooperative", reward=100)
    service = LedgerQuests(ledger)
    for person, discipline, checkout_key in ((2, "Design", 1922), (3, "Fabrication", 1923)):
        rank(ledger, store, person, 2)
        checkout(source, person, 311, checkout_key)
        service.contribute(member(person), quest["_id"], "join", role=discipline)
        service.contribute(member(person), quest["_id"], "submit", description="Observable contribution")
        service.contribute(member(10), quest["_id"], "verify", member=member(person))

    rank(ledger, store, 2, 0)
    state = service.project(quest)
    assert set(service.verified_contributors(quest, state)) == {member(3)}
    with pytest.raises(ValueError, match="at least two"):
        service.finalize(member(10), quest["_id"], "Independently observed shared outcome")
    assert ledger.participant(member(2))["xp"] == "0"


def test_legacy_cooperative_quest_uses_exact_rank_at_finalization(joined):
    ledger, store, source, *_ = joined
    quest = proposal(ledger, store, source, quest_type="cooperative", reward=100)
    rank(ledger, store, 1, 3)  # Legacy author eligibility required two ranks above the target.
    quest = store.get("ledger_quests", quest["_id"])
    quest.pop("rank_mode", None)
    store.put("ledger_quests", quest)
    service = LedgerQuests(ledger)
    for person, discipline, checkout_key in ((2, "Design", 1932), (3, "Fabrication", 1933)):
        rank(ledger, store, person, 1)
        checkout(source, person, 311, checkout_key)
        service.contribute(member(person), quest["_id"], "join", role=discipline)
        service.contribute(member(person), quest["_id"], "submit", description="Legacy exact-rank contribution")
        service.contribute(member(10), quest["_id"], "verify", member=member(person))

    assert service.finalize(member(10), quest["_id"], "Observed legacy shared outcome")["status"] == "completed"


def test_proposal_modal_uses_external_tools_and_file_input(joined):
    ledger, store, source, composer, _, slack = joined
    checkout(source, 1, 311, 931)
    view = views.quest_author(ledger, member(1))
    elements = {block.get("element", {}).get("action_id"): block.get("element", {}) for block in view["blocks"]}
    assert elements["quest_tools"]["type"] == "multi_external_select"
    assert elements["quest_tools"]["max_selected_items"] == 20
    assert elements["example_photo"]["type"] == "file_input" and elements["example_photo"]["max_files"] == 1
    result = SlackUI(ledger, composer).options({"user": {"id": "U1"}, "action_id": "quest_tools",
        "value": "tool1", "view": {"private_metadata": view["private_metadata"]}})
    assert result["options"] == [views.option("Tool1-1 — Shop1", member(311))]
    assert "shop_ids" not in json.dumps(view)


def test_review_modal_normalizes_legacy_string_disciplines(joined):
    ledger, store, source, *_ = joined
    quest = Quests(ledger).draft(member(1), "Legacy cooperative quest", "Build it together.",
        "Show the shared result.", 1, quest_type="cooperative", duration={"value": 1, "unit": "hours"},
        disciplines=[{"name": "Design", "expectation": "Document the design."},
                     {"name": "Build", "expectation": "Inspect the result."}])
    quest["disciplines"] = ["Design", "Build"]
    store.put("ledger_quests", quest)

    view = views.member_quest_review(ledger, quest)
    fields = {block.get("block_id"): block.get("element", {}) for block in view["blocks"]}
    assert fields["discipline_name_0"]["initial_value"] == "Design"
    assert fields["discipline_name_1"]["initial_value"] == "Build"
    assert fields["discipline_expectation_0"].get("initial_value", "") == ""


def test_modal_verifies_and_stores_bounded_slack_photo_metadata(joined):
    ledger, store, source, composer, _, slack = joined
    checkout(source, 1, 311, 941)
    view = views.quest_author(ledger, member(1))
    body = form(view, {"quest_type": "individual", "title": "Photo quest", "description": "Build it safely.",
        "criteria": "Show the observed result.", "target_rank": "1", "duration_value": "3",
        "duration_unit": "hours"}, "U1")
    values = body["view"]["state"]["values"]
    values["quest_tools"]["quest_tools"] = {"type": "multi_external_select",
        "selected_options": [views.option("Tool1-1 — Shop1", member(311))]}
    values["example_photo"]["example_photo"] = {"type": "file_input", "selected_files": ["FEXAMPLE"]}
    slack.files_info.return_value = {"file": {"id": "FEXAMPLE", "user": "U1", "name": "jig.png",
        "mimetype": "image/png", "filetype": "png", "size": 1024, "url_private": "never-store-this"}}
    SlackUI(ledger, composer).submission(body, slack)
    quest = store.select("ledger_quests", {"kind": "member_quest"})[0]
    assert quest["photo"] == {"id": "FEXAMPLE", "name": "jig.png", "mimetype": "image/png",
                              "filetype": "png", "size": 1024}
    assert quest["shop_ids"] == [member(201)] and quest["tool_ids"] == [member(311)]


def test_bonus_is_once_per_logical_quest_and_opted_out_share_is_silent(joined):
    ledger, store, source, *_ = joined
    quest = proposal(ledger, store, source, reward=100)
    revised = Quests(ledger).draft(member(1), "Revised participant jig", "Safer instructions.",
        "Demonstrate a repeatable result.", 1, tools=[member(311)], revision_of=quest["_id"],
        duration={"value": 3, "unit": "hours"})
    Quests(ledger).submit_draft(member(1), revised["_id"])
    Quests(ledger).publish(member(10), revised["_id"], 100, proposer_bonus=500)
    assert ledger.participant(member(1))["xp"] == "100"
    assert len(store.select("ledger_evidence", {"kind": "quest_proposer_approval"})) == 1

    ledger.leave(member(1))
    rank(ledger, store, 2, 2)
    checkout(source, 2, 311, 952)
    Quests(ledger).accept(member(2), revised["_id"])
    evidence = Quests(ledger).submit(member(2), revised["_id"], "Observed revised result")
    Quests(ledger).verify(member(10), evidence["_id"])
    assert ledger.participant(member(1))["xp"] == "105"
    royalty = store.select("ledger_evidence", {"kind": "quest_proposer_royalty"})[0]
    assert not store.get("ledger_outbox", "dm:account:" + member(1) + ":" + royalty["_id"] + ":1")


def test_revoked_proposer_clearance_prevents_new_share(joined):
    ledger, store, source, *_ = joined
    quest = proposal(ledger, store, source, reward=100)
    source.data["tool_checkouts"][0]["revoked_at"] = "revoked"
    rank(ledger, store, 2, 2)
    checkout(source, 2, 311, 962)
    Quests(ledger).accept(member(2), quest["_id"])
    evidence = Quests(ledger).submit(member(2), quest["_id"], "Observed result")
    Quests(ledger).verify(member(10), evidence["_id"])
    assert ledger.participant(member(1))["xp"] == "100"
    assert not store.select("ledger_evidence", {"kind": "quest_proposer_royalty"})


def test_later_proposer_share_uses_deterministic_system_message(joined):
    ledger, store, source, *_ = joined
    quest = proposal(ledger, store, source, reward=100)
    for person, checkout_key in ((2, 972), (3, 973)):
        rank(ledger, store, person, 2)
        checkout(source, person, 311, checkout_key)
        Quests(ledger).accept(member(person), quest["_id"])
        evidence = Quests(ledger).submit(member(person), quest["_id"], "Observed result")
        Quests(ledger).verify(member(10), evidence["_id"])
    royalties = sorted(store.select("ledger_evidence", {"kind": "quest_proposer_royalty"}),
                       key=lambda row: row["at"])
    assert [row["first"] for row in royalties] == [True, False]
    second_award = store.get("ledger_evidence", "account:" + member(1) + ":" + royalties[1]["_id"])
    job = store.get("ledger_outbox", "dm:" + second_award["_id"] + ":" + str(second_award["revision"]))
    assert job["payload"]["deterministic_text"] == "The System records another completion of your approved quest."


def test_approval_revalidates_tools_and_review_edits_create_child(joined):
    ledger, store, source, *_ = joined
    rank(ledger, store, 1, 2)
    checkout(source, 1, 311, 981)
    original = Quests(ledger).draft(member(1), "Shared jig", "Build it safely.", "Show it working.", 2,
        tools=[member(311)], quest_type="cooperative", duration={"value": 4, "unit": "hours"},
        disciplines=[{"name": "Design", "expectation": "Document the design."},
                     {"name": "Build", "expectation": "Inspect the finished jig."}])
    Quests(ledger).submit_draft(member(1), original["_id"])
    source.data["tool_checkouts"][-1]["revoked_at"] = "revoked"
    with pytest.raises(Denied, match="current checkout"):
        Quests(ledger).publish(member(10), original["_id"], 100)
    edits = {"title": "Solo jig", "description": "Build it safely.", "criteria": "Show it working.",
             "shop_ids": [], "tool_ids": [], "disciplines": [], "quest_type": "individual",
             "target_rank": 1, "duration": {"value": 90, "unit": "minutes"}}
    reviewed = Quests(ledger).publish(member(10), original["_id"], 100, edits=edits)
    saved_original = store.get("ledger_quests", original["_id"])
    assert reviewed["_id"] != original["_id"] and reviewed["revision_of"] == original["_id"]
    assert reviewed["quest_type"] == "individual" and reviewed["tool_ids"] == []
    assert saved_original["quest_type"] == "cooperative" and saved_original["status"] == "superseded"


def test_slack_review_preserves_shop_only_prerequisites(joined):
    ledger, store, source, composer, _, slack = joined
    rank(ledger, store, 10, 2)
    original = Quests(ledger).draft(member(1), "Shop-only quest", "Work safely in the shop.",
        "Show the completed result.", 1, shops=[member(202)], duration={"value": 1, "unit": "hours"})
    Quests(ledger).submit_draft(member(1), original["_id"])

    view = views.member_quest_review(ledger, original)
    assert "Shop2" in json.dumps(view)
    body = form(view, {"quest_type": "individual", "title": original["title"],
        "description": original["description"], "criteria": original["criteria"],
        "target_rank": "1", "duration_value": "1", "duration_unit": "hours",
        "reward": "100", "proposer_bonus": "100", "classification": "challenge"}, "U10")
    body["view"]["state"]["values"]["quest_tools"]["quest_tools"] = {
        "type": "multi_external_select", "selected_options": []}
    SlackUI(ledger, composer).submission(body, slack)

    receipt = store.get("ledger_evidence", "quest-review:" + original["_id"])
    reviewed = store.get("ledger_quests", receipt["quest"])
    assert reviewed["shop_ids"] == [member(202)]
    assert reviewed["explicit_shop_ids"] == [member(202)]


@pytest.mark.parametrize("prior_status", ["withdrawn", "disabled"])
def test_cooperative_revision_replaces_closed_project(joined, prior_status):
    ledger, store, source, *_ = joined
    original = proposal(ledger, store, source, quest_type="cooperative", reward=100)
    service = LedgerQuests(ledger)
    rank(ledger, store, 2, 2)
    checkout(source, 2, 311, 1982)
    service.contribute(member(2), original["_id"], "join", role="Design")

    if prior_status == "withdrawn":
        Quests(ledger).withdraw(member(1), original["_id"])
    else:
        Quests(ledger).disable(member(10), original["_id"], "Replace with a reviewed revision.")

    prior_project = store.get("ledger_relationships", "cooperative:" + original["logical_id"])
    assert prior_project["status"] == prior_status
    revised = Quests(ledger).draft(member(1), "Revised cooperative jig", "Build the revised jig safely.",
        "Demonstrate the revised result.", 1, tools=[member(311)], revision_of=original["_id"],
        quest_type="cooperative", duration={"value": 3, "unit": "hours"},
        disciplines=[{"name": "Design", "expectation": "Document the revised design."},
                     {"name": "Fabrication", "expectation": "Build and inspect the revised jig."}])
    Quests(ledger).submit_draft(member(1), revised["_id"])
    reviewed = Quests(ledger).publish(member(10), revised["_id"], 100)

    current = store.get("ledger_relationships", "cooperative:" + original["logical_id"])
    archived = store.get("ledger_relationships",
        f"cooperative-revision:{original['logical_id']}:{original['_id']}")
    assert current["status"] == "open" and current["quest_revision"] == reviewed["_id"]
    assert current["contributions"] == {}
    assert archived["status"] == "superseded" and archived["prior_status"] == prior_status
    assert archived["contributions"][member(2)]["status"] == "closed"


@pytest.mark.parametrize("photo", [
    {"id": "F1", "name": "sample.pdf", "mimetype": "application/pdf", "filetype": "pdf", "size": 100},
    {"id": "F2", "name": "huge.png", "mimetype": "image/png", "filetype": "png", "size": 10 * 1024 * 1024 + 1},
])
def test_proposal_rejects_invalid_example_photo(joined, photo):
    ledger, store, source, *_ = joined
    with pytest.raises(ValueError, match="JPEG, PNG, or GIF"):
        Quests(ledger).draft(member(1), "Photo", "Build it.", "Show the result.", 1,
                             duration={"value": 1, "unit": "hours"}, photo=photo)


def test_rank_correction_below_minimum_blocks_an_accepted_completion(joined):
    ledger, store, source, *_ = joined
    rank(ledger, store, 1, 2)
    checkout(source, 1, 311, 991)
    quest = Quests(ledger).draft(member(1), "Ranked jig", "Build it safely.", "Show it working.", 2,
        tools=[member(311)], duration={"value": 1, "unit": "hours"})
    Quests(ledger).submit_draft(member(1), quest["_id"])
    quest = Quests(ledger).publish(member(10), quest["_id"], 100)
    rank(ledger, store, 2, 2)
    checkout(source, 2, 311, 992)
    Quests(ledger).accept(member(2), quest["_id"])
    rank(ledger, store, 2, 1)
    with pytest.raises(Denied, match="below this quest's approved minimum"):
        Quests(ledger).submit(member(2), quest["_id"], "Observed result")


def test_photo_appears_in_private_review_and_invalid_slack_file_is_discarded(joined):
    ledger, store, source, composer, _, slack = joined
    photo = {"id": "FREVIEW", "name": "example.png", "mimetype": "image/png", "filetype": "png", "size": 500}
    quest = Quests(ledger).draft(member(1), "Photo review", "Build it.", "Show the result.", 1,
        duration={"value": 1, "unit": "hours"}, photo=photo)
    Quests(ledger).submit_draft(member(1), quest["_id"])
    saved = store.get("ledger_quests", quest["_id"])
    facts = next(activities(store, "ledger_quests", saved))[2]
    _, blocks = render({"activity_id": quest["_id"]}, facts)
    assert {"type": "image", "slack_file": {"id": "FREVIEW"},
            "alt_text": "Participant-provided quest example photo"} in blocks

    response = Mock()
    response.data = {"ok": False, "error": "invalid_arguments", "response_metadata": {
        "messages": ["[ERROR] invalid slack file [json-pointer:view/blocks/3/slack_file.id/slack_file]"]}}
    response.get.side_effect = lambda key, default=None: response.data.get(key, default)
    slack.views_open.side_effect = [SlackApiError("invalid file", response), {"ok": True}]
    view = {"type": "modal", "blocks": blocks}
    SlackUI(ledger, composer).open(slack, {"trigger_id": "TRIGGER"}, view)
    assert store.get("ledger_quests", quest["_id"])["photo"]["available"] is False
    assert all(block.get("type") != "image" for block in slack.views_open.call_args_list[-1].kwargs["view"]["blocks"])
