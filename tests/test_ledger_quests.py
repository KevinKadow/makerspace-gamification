from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json

import pytest

from conftest import oid
from ledger.authority import Authority
from ledger.community import Community
from ledger.domain import Denied
from ledger.ledger_quests import LedgerQuests
from ledger.quest_policy import DEFINITION_FIELDS, LEDGER_AUTHOR
from ledger.quests import Quests
from ledger.slack_app import SlackUI
from ledger.storage import now
from ledger.views import quest_browser
from ledger.worker import Worker
from test_quest_generation import generator, proposal
from test_slack import form
from test_worker import claim


def pending(env, quest_type="individual", target=1, key="ledger-proposal", shops=()):
    l, s, *_ = env
    q = {"_id": key, "logical_id": key, "kind": "ledger_quest", "creator": LEDGER_AUTHOR,
         "quest_type": quest_type, "target_rank": target, "status": "pending_review", "at": now(),
         **proposal(quest_type)}
    q["shop_ids"] = list(shops)
    s.put("ledger_quests", q)
    return q


def test_human_review_edits_preserve_original_and_immutable_published_revision(joined):
    l, s, *_ = joined
    q = pending(joined)
    before = {k: deepcopy(q[k]) for k in DEFINITION_FIELDS}
    edited = {**before, "title": "Build a repeatable marking jig"}
    service = LedgerQuests(l)
    published = service.review(str(oid(10)), q["_id"], 123, edits=edited)
    assert published["_id"] != q["_id"] and published["creator"] == LEDGER_AUTHOR
    original = s.get("ledger_quests", q["_id"])
    assert {k: original[k] for k in DEFINITION_FIELDS} == before and original["status"] == "superseded"
    assert s.get("ledger_catalog", "quest-head:" + q["logical_id"])["revision"] == published["_id"]
    assert service.review(str(oid(10)), q["_id"], 123)["_id"] == published["_id"]
    assert s.select("ledger_awards") == []
    with pytest.raises(ValueError):
        service.review(str(oid(10)), published["_id"], 123, edits=before)


@pytest.mark.parametrize('scope,capability,contributor,visible', [
    ({'kind': 'global'}, 'quest_complete', False, True),
    ({'kind': 'quest', 'quest': 'ledger-proposal'}, 'quest_complete', False, True),
    ({'kind': 'shops', 'shops': [str(oid(201))]}, 'quest_complete', False, True),
    ({'kind': 'shops', 'shops': [str(oid(202))]}, 'quest_complete', False, False),
    ({'kind': 'global'}, 'learning_review', False, False),
    ({'kind': 'global'}, 'quest_complete', True, False)])
def test_delegated_review_queue_discovers_only_authorized_cooperative_work(joined, scope, capability, contributor, visible):
    l, s, _, composer, _, slack = joined
    l.join(str(oid(3)))
    service = LedgerQuests(l)
    q = service.review(str(oid(10)), pending(joined, 'cooperative', shops=[str(oid(201))])['_id'], 100)
    Authority(l).grant(str(oid(10)), str(oid(3)), [capability], scope, 'Discover authorized shared reviews')
    for member, role in ((1, 'Design'), (2, 'Fabrication')):
        service.contribute(str(oid(member)), q['_id'], 'join', role=role)
        service.contribute(str(oid(member)), q['_id'], 'submit', description='Observable contribution evidence')
    if contributor:
        service.contribute(str(oid(3)), q['_id'], 'join', role='Design')
    worker = Worker(l, composer, slack)
    worker.admin_command(str(oid(3)), ['review'], 'pending-queue')
    summary = s.get('ledger_outbox', 'dm:pending-queue')['payload']['facts']['summary']
    assert ('Cooperative contribution:' in summary) == visible
    assert ('verify-quest ' + q['_id'] + ' <@U1>') in summary if visible else q['_id'] not in summary
    assert 'Shared project ready:' not in summary
    for member in (1, 2):
        service.contribute(str(oid(10)), q['_id'], 'verify', member=str(oid(member)))
    worker.admin_command(str(oid(3)), ['review'], 'ready-queue')
    summary = s.get('ledger_outbox', 'dm:ready-queue')['payload']['facts']['summary']
    assert ('Shared project ready:' in summary) == visible
    assert 'Cooperative contribution:' not in summary
    if visible:
        assert '/ledger-admin complete-quest ' + q['_id'] in summary
    assert not any(j['payload'].get('member_id') == str(oid(3))
                   for j in s.select('ledger_outbox', {'kind': 'review_channel_invite'}))


@pytest.mark.parametrize('shared', [False, True])
@pytest.mark.parametrize('malformed', [None, [], {str(oid(4)): None},
                                    {str(oid(4)): 'invalid'}, {str(oid(4)): {}}, {str(oid(3)): None}])
def test_review_queue_preserves_valid_reviews_beside_malformed_contributions(joined, shared, malformed):
    l, s, _, composer, _, slack = joined
    l.join(str(oid(3)))
    Authority(l).grant(str(oid(10)), str(oid(3)), ['quest_complete', 'learning_review'],
                       {'kind': 'global'}, 'Review pending work')
    evidence = l.submit(str(oid(1)), 'learning-challenge', 'Valid learning evidence.')
    service = LedgerQuests(l)
    q = service.review(str(oid(10)), pending(joined, 'cooperative')['_id'], 100)
    if shared:
        bad = service.project(q)
        bad['contributions'] = deepcopy(malformed)
        s.put('ledger_relationships', bad)
    else:
        s.put('ledger_quests', {'_id': 'legacy-malformed', 'status': 'open', 'contributions': deepcopy(malformed)})
    # Also exercise valid siblings inside a malformed mapping, where possible.
    for member, role in ((1, 'Design'), (2, 'Fabrication')):
        state = service.project(q)
        if not isinstance(state['contributions'], dict):
            state['contributions'] = {}
        state['contributions'][str(oid(member))] = {'status': 'pending', 'role': role, 'description': 'Valid contribution.'}
        s.put('ledger_relationships', state)
    # A separate malformed shared project preserves the non-map cases.
    if shared and not isinstance(malformed, dict):
        bad = deepcopy(service.project(q))
        bad.update(_id='malformed-project', contributions=deepcopy(malformed))
        s.put('ledger_relationships', bad)
    before = s.select('ledger_relationships')
    worker = Worker(l, composer, slack)
    worker.admin_command(str(oid(3)), ['review'], 'legacy-queue')
    summary = s.get('ledger_outbox', 'dm:legacy-queue')['payload']['facts']['summary']
    assert evidence['_id'] in summary
    independent = not (shared and isinstance(malformed, dict) and str(oid(3)) in malformed)
    assert ('/ledger-admin verify-quest ' + q['_id'] + ' <@U1>' in summary) == independent
    assert 'Shared project ready:' not in summary
    assert s.select('ledger_relationships') == before
    state = service.project(q)
    for member in (1, 2):
        state['contributions'][str(oid(member))]['status'] = 'verified'
    s.put('ledger_relationships', state)
    before = s.select('ledger_relationships')
    worker.admin_command(str(oid(3)), ['review'], 'legacy-ready-queue')
    summary = s.get('ledger_outbox', 'dm:legacy-ready-queue')['payload']['facts']['summary']
    assert evidence['_id'] in summary
    ready = independent and not (shared and isinstance(malformed, dict))
    assert ('/ledger-admin complete-quest ' + q['_id'] in summary) == ready
    assert s.select('ledger_relationships') == before


def test_review_rechecks_authority_rank_resources_and_edited_scope(joined):
    l, s, src, *_ = joined
    q = pending(joined, shops=[str(oid(201))])
    grant = Authority(l).grant(str(oid(10)), str(oid(2)), ["quest_publish"],
        {"kind": "shops", "shops": [str(oid(201))]}, "Review this shop")
    edits = {k: q[k] for k in DEFINITION_FIELDS}
    edits["shop_ids"] = [str(oid(202))]
    with pytest.raises(Denied):
        LedgerQuests(l).review(str(oid(2)), q["_id"], 100, edits=edits)
    Authority(l).revoke(str(oid(10)), grant["_id"], "Revoke")
    with pytest.raises(Denied):
        LedgerQuests(l).review(str(oid(2)), q["_id"], 100)
    src.data["shops"][0]["disabled"] = True
    with pytest.raises(ValueError, match="enabled shops"):
        LedgerQuests(l).review(str(oid(10)), q["_id"], 100)
    assert s.get("ledger_quests", q["_id"])["status"] == "pending_review"
    rejected = LedgerQuests(l).review(str(oid(10)), q["_id"], 100, approve=False, reason="Prerequisite shop disabled")
    assert rejected["status"] == "rejected"


@pytest.mark.parametrize('malformed', [None, 'invalid', {}])
def test_ready_queue_omits_malformed_project_and_advertised_completion_succeeds(joined, malformed):
    l, s, _, composer, _, slack = joined
    actor = str(oid(3))
    l.join(actor)
    Authority(l).grant(str(oid(10)), actor, ['quest_complete'], {'kind': 'global'}, 'Review shared outcomes')
    service = LedgerQuests(l)
    quests = []
    for key in ('malformed-ready', 'valid-ready'):
        q = service.review(str(oid(10)), pending(joined, 'cooperative', key=key)['_id'], 100)
        for member, role in ((1, 'Design'), (2, 'Fabrication')):
            service.contribute(str(oid(member)), key, 'join', role=role)
            service.contribute(str(oid(member)), key, 'submit', description='Observed contribution.')
            service.contribute(str(oid(10)), key, 'verify', member=str(oid(member)))
        quests.append(q)
    damaged = service.project(quests[0])
    damaged['contributions'][str(oid(4))] = deepcopy(malformed)
    s.put('ledger_relationships', damaged)
    before = deepcopy(service.project(quests[0]))
    Worker(l, composer, slack).admin_command(actor, ['review'], 'ready-actions')
    summary = s.get('ledger_outbox', 'dm:ready-actions')['payload']['facts']['summary']
    assert '/ledger-admin complete-quest malformed-ready' not in summary
    assert '/ledger-admin complete-quest valid-ready' in summary
    assert service.finalize(actor, 'valid-ready', 'Independently observed shared result.')['status'] == 'completed'
    assert service.project(quests[0]) == before


def test_disabling_superseded_cooperative_revision_preserves_active_project(joined):
    l, s, *_ = joined
    original = pending(joined, 'cooperative')
    service = LedgerQuests(l)
    edits = {k: original[k] for k in DEFINITION_FIELDS}
    edits['title'] = 'Build a repeatable marking jig'
    current = service.review(str(oid(10)), original['_id'], 100, edits=edits)
    project = deepcopy(service.project(current))
    Quests(l).disable(str(oid(10)), original['_id'], 'Retire the superseded proposal')
    assert service.project(current) == project
    assert s.get('ledger_quests', current['_id'])['status'] == 'published'
    assert service.available(str(oid(1)), current) == current
    Quests(l).disable(str(oid(10)), current['_id'], 'Close the current project')
    assert service.project(current)['status'] == 'disabled'
    with pytest.raises(Denied):
        service.available(str(oid(1)), s.get('ledger_quests', current['_id']))


@pytest.mark.parametrize('edited', [False, True])
def test_rank_name_proposals_and_human_edits_cannot_be_published(joined, edited):
    l, s, *_ = joined
    q = pending(joined, 'cooperative', target=6)
    unsafe = {**{k: q[k] for k in DEFINITION_FIELDS}, 'title': 'Meet the Adept'}
    if not edited:
        q.update(unsafe)
        s.put('ledger_quests', q)
    before = deepcopy(s.data)
    with pytest.raises(ValueError, match='numeric rank slots'):
        LedgerQuests(l).review(str(oid(10)), q['_id'], 100, edits=unsafe if edited else None)
    assert s.data == before


@pytest.mark.parametrize('quest_type', ['individual', 'cooperative'])
@pytest.mark.parametrize('unsafe', ['renamed_rank', 'invisible_label'])
def test_existing_rank_name_quests_are_hidden_after_rank_name_changes(joined, quest_type, unsafe):
    from ledger.conversations import conversation_facts
    l, s, *_ = joined
    service = LedgerQuests(l)
    q = service.review(str(oid(10)), pending(joined, quest_type)['_id'], 100)
    if unsafe == 'renamed_rank':
        display = s.get('ledger_catalog', 'rank_display')
        display['ranks'][5]['name'] = q['title']
        s.put('ledger_catalog', display)
    else:
        q['title'] = 'Meet the Ad\u200bept'
        s.put('ledger_quests', q)
    assert not any(key.endswith(q['_id']) for key, _ in Quests(l).options(str(oid(1))))
    assert q['title'] not in json.dumps(conversation_facts(l, str(oid(1)), True, 'Which quests can I do?'), ensure_ascii=False)
    assert s.get('ledger_quests', q['_id'])['title'] == q['title']
    if quest_type == 'cooperative':
        with pytest.raises(Denied):
            service.finalize(str(oid(10)), q['_id'], 'Shared result')
    else:
        with pytest.raises(Denied):
            Quests(l).accept(str(oid(1)), q['_id'])


def test_cooperative_finalization_requires_all_disciplines_and_live_clearances(joined):
    l, s, src, *_ = joined
    q = pending(joined, "cooperative", shops=[str(oid(201))])
    q["tool_ids"] = [str(oid(311))]
    q["disciplines"].append({"name": "Evaluation", "expectation": "Measure and document repeatability."})
    s.put("ledger_quests", q)
    src.data["tool_checkouts"] = [{"_id": oid(800 + m), "tool_id": oid(311), "member_id": oid(m)} for m in (1, 2)]
    service = LedgerQuests(l)
    q = service.review(str(oid(10)), q["_id"], 100)
    for member, role in ((1, "Design"), (2, "Fabrication")):
        service.contribute(str(oid(member)), q["_id"], "join", role=role)
        service.contribute(str(oid(member)), q["_id"], "submit", description="Verified work")
        service.contribute(str(oid(10)), q["_id"], "verify", member=str(oid(member)))
    with pytest.raises(ValueError, match="every discipline"):
        service.finalize(str(oid(10)), q["_id"], "Shared outcome")
    src.data["tools"][0]["out_of_service"] = True
    with pytest.raises(Denied):
        service.available(str(oid(1)), q)
    assert s.select("ledger_awards") == []


def test_individual_uses_exact_rank_and_survives_promotion(joined):
    l, s, *_ = joined
    q = LedgerQuests(l).review(str(oid(10)), pending(joined)["_id"], 123)
    service = Quests(l)
    assert ("q:" + q["_id"], q["title"]) in service.options(str(oid(1)))
    service.accept(str(oid(1)), q["_id"])
    p = l.participant(str(oid(1)))
    p["rank"] = 3
    s.put("ledger_participants", p)
    evidence = service.submit(str(oid(1)), q["_id"], "Working jig and measured improvements")
    service.verify(str(oid(10)), evidence["_id"])
    assert l.participant(str(oid(1)))["xp"] == "123"
    assert not s.get("ledger_participants", LEDGER_AUTHOR)
    assert not any(job["payload"].get("member_id") == LEDGER_AUTHOR for job in s.select("ledger_outbox"))
    with pytest.raises(Denied):
        service.accept(str(oid(1)), q["_id"])
    q2 = LedgerQuests(l).review(str(oid(10)), pending(joined, target=6, key="higher")["_id"], 100)
    with pytest.raises(Denied):
        service.accept(str(oid(2)), q2["_id"])
    rendered = json.dumps(quest_browser(l, str(oid(2)), "q:" + q["_id"]))
    assert "The Ledger" in rendered and "Unavailable Slack identity" not in rendered


def test_cooperative_any_rank_independent_finalization_and_once_only_rewards(joined):
    l, s, *_ = joined
    q = LedgerQuests(l).review(str(oid(10)), pending(joined, "cooperative", target=6)["_id"], 125)
    l.join(str(oid(3)))
    community, service = Community(l), LedgerQuests(l)
    for member, role in ((1, "Design"), (2, "Fabrication"), (3, "Design")):
        community.quest(str(oid(member)), q["_id"], "join", role=role)
        community.quest(str(oid(member)), q["_id"], "submit", description="Evidence of my contribution")
    with pytest.raises(Denied):
        service.finalize(str(oid(1)), q["_id"], "Working shared outcome")
    with pytest.raises(Denied):
        community.quest(str(oid(1)), q["_id"], "verify", member=str(oid(2)))
    with pytest.raises(ValueError, match="two eligible"):
        service.finalize(str(oid(10)), q["_id"], "Working shared outcome")
    for member in (1, 2):
        community.quest(str(oid(10)), q["_id"], "verify", member=str(oid(member)))
    assert service.project(q)["status"] == "open" and l.participant(str(oid(1)))["xp"] == "0"
    definition = {k: q[k] for k in DEFINITION_FIELDS}
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: service.finalize(str(oid(10)), q["_id"], "Demonstrated working jig and shared handoff"), range(2)))
    assert all(result["status"] == "completed" for result in results)
    assert l.participant(str(oid(1)))["xp"] == l.participant(str(oid(2)))["xp"] == "125"
    assert l.participant(str(oid(3)))["xp"] == "0"
    assert service.project(q)["contributions"][str(oid(3))]["status"] == "closed"
    assert len(s.select("ledger_evidence", {"kind": "quest_completion"})) == 2
    assert {k: s.get("ledger_quests", q["_id"])[k] for k in DEFINITION_FIELDS} == definition
    assert not s.select("ledger_evidence", {"achievement": "boss"})
    with pytest.raises(Denied):
        community.quest(str(oid(3)), q["_id"], "join", role="Design")


def test_disabled_or_opted_out_contributors_cannot_complete(joined):
    l, *_ = joined
    q = LedgerQuests(l).review(str(oid(10)), pending(joined, "cooperative")["_id"], 100)
    service = LedgerQuests(l)
    for member, role in ((1, "Design"), (2, "Fabrication")):
        service.contribute(str(oid(member)), q["_id"], "join", role=role)
        service.contribute(str(oid(member)), q["_id"], "submit", description="Evidence")
        service.contribute(str(oid(10)), q["_id"], "verify", member=str(oid(member)))
    l.leave(str(oid(2)))
    with pytest.raises(ValueError):
        service.finalize(str(oid(10)), q["_id"], "Shared result")
    Quests(l).disable(str(oid(10)), q["_id"], "Needs review")
    with pytest.raises(Denied):
        service.finalize(str(oid(10)), q["_id"], "Shared result")


def test_slack_review_button_forms_and_shared_completion_are_authorized(joined):
    l, s, _, composer, _, slack = joined
    l.join(str(oid(10)))
    q = pending(joined, "cooperative")
    ui = SlackUI(l, composer)
    ui.action({"user": {"id": "U10"}, "actions": [{"action_id": "review_ledger_quest", "value": q["_id"]}], "trigger_id": "T"}, slack)
    view = slack.views_open.call_args.kwargs["view"]
    assert view["callback_id"] == "ledger_quest_review"
    with pytest.raises(Denied):
        ui.action({"user": {"id": "U1"}, "actions": [{"action_id": "review_ledger_quest", "value": q["_id"]}], "trigger_id": "T"}, slack)
    data = {"title": q["title"], "description": q["description"], "criteria": q["criteria"], "shops": "", "tools": "", "reward": "100", "reason": "Reviewed"}
    for i, discipline in enumerate(q["disciplines"]):
        data[f"discipline_name_{i}"] = discipline["name"]
        data[f"discipline_expectation_{i}"] = discipline["expectation"]
    ui.submission(form(view, data, user="U10"), slack)
    assert s.get("ledger_quests", q["_id"])["status"] == "published"
    ui.command({"user_id": "U10", "command": "/ledger-admin", "text": "complete-quest " + q["_id"], "trigger_id": "T"}, slack)
    assert slack.views_open.call_args.kwargs["view"]["callback_id"] == "ledger_group_complete"
    with pytest.raises(ValueError):
        ui.action({"user": {"id": "U1"}, "actions": [{"action_id": "review_ledger_quest", "value": q["_id"]}], "trigger_id": "T"}, slack)


def test_staff_notice_is_durable_private_and_contains_no_chat(generator):
    g, (l, s, _, composer, _, slack) = generator
    l.join(str(oid(10)))
    result = g.run(rank=1, request_id="notice")
    Worker(l, composer, slack).outbox(claim(s, result["notice_id"]))
    call = slack.chat_postMessage.call_args.kwargs
    assert call["channel"] == "CSTAFF" and call["client_msg_id"]
    assert call["blocks"][-1]["elements"][0]["action_id"] == "review_ledger_quest"
    assert "chat" not in json.dumps(call)
    worker = Worker(l, composer, slack)
    worker.admin_command(str(oid(10)), ["review"], "review-test")
    summaries = [job["payload"].get("facts", {}).get("summary", "") for job in s.select("ledger_outbox")]
    assert any(result["quest_id"] in text for text in summaries)
