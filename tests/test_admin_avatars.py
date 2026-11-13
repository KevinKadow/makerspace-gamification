import json
from datetime import timedelta

import pytest

from conftest import oid
from ledger import admin_avatars, avatars
from ledger.domain import Denied
from ledger.slack_app import SlackUI
from ledger.storage import enqueue, now
from ledger.worker import Worker
from test_slack import form


@pytest.fixture
def admin(joined):
    ledger, store, source, composer, api, slack = joined
    ledger.join(str(oid(10)))
    return ledger, store, source, composer, api, slack


def command(ui, slack, text, user="U10"):
    ui.command({"user_id": user, "command": "/ledger-admin", "text": text, "trigger_id": "T"}, slack)
    return slack.views_open.call_args.kwargs["view"]


def action(view, user="U10"):
    control = view["blocks"][-1]["elements"][0]
    return {"user": {"id": user}, "actions": [control], "view": {"id": "V", "hash": "h"}}


def test_inspect_avatar_metadata_without_private_context_and_force_once(admin):
    ledger, store, _, composer, api, slack = admin
    member = str(oid(2))
    current = {"_id": "current:" + member, "kind": "current", "member_id": member, "slack_id": "U2",
        "rank": ledger.participant(member)["rank"], "revision": "original",
        "avatar": {"file_id": "F_FULL", "permalink": "https://example.slack.com/full"},
        "avatar512": {"file_id": "F_SMALL"}}
    store.put(avatars.COLLECTION, current)
    store.put(avatars.COLLECTION, {"_id": "job:original", **{k: v for k, v in current.items() if k != "_id"},
        "job_id": "original", "status": "activated", "prompt": "An artisan in a workshop.",
        "token_usage": {"total_tokens": 110}, "duration_seconds": 12.5, "job_end_time": now(),
        "settings": {"steps": 40}, "context": {"chat": [{"text": "PRIVATE_CHAT"}]},
        "composition_messages": ["PRIVATE_POLICY"], "selection": {"matrix": "PRIVATE_MATRIX"}})
    ui = SlackUI(ledger, composer)
    view = command(ui, slack, "avatar <@U2>")
    rendered = json.dumps(view)
    assert "F_SMALL" in rendered and "https://example.slack.com/full" in rendered
    assert "110" in rendered and "12.5" in rendered and "job_end_time" in rendered
    assert "An artisan in a workshop" in rendered and "Force regenerate avatar" in rendered
    assert "PRIVATE" not in rendered
    before = store.select("ledger_outbox", {"kind": "avatar_generate"})
    picker = command(ui, slack, "avatar")
    selected = ui.submission(form(picker, {"avatar_participant": member}, user="U10"), slack)["view"]
    assert "F_SMALL" in json.dumps(selected) and "110" in json.dumps(selected)
    assert "Force regenerate avatar" in json.dumps(selected)
    assert store.select("ledger_outbox", {"kind": "avatar_generate"}) == before
    pending = store.select("ledger_outbox", {"kind": "avatar_generate", "payload.member_id": member})
    payload = action(view)
    ui.action(payload, slack)
    ui.action(payload, slack)
    replacements = store.select("ledger_outbox", {"kind": "avatar_generate", "payload.member_id": member, "status": "pending"})
    assert len(replacements) == 1
    assert replacements[0]["available_at"] <= now()
    assert all(store.get("ledger_outbox", job["_id"])["status"] == "cancelled" for job in pending)
    assert avatars.current(store, member) == current
    assert len(store.select(avatars.COLLECTION, {"kind": "admin_request"})) == 1
    api.complete.assert_not_called()
    slack.files_delete.assert_not_called()


def test_missing_avatar_picker_inspects_then_optionally_generates_and_filters_opt_out(admin):
    ledger, store, _, composer, _, slack = admin
    member = str(oid(2))
    ui = SlackUI(ledger, composer)
    view = command(ui, slack, "avatar <@U2>")
    assert "No current personalized avatar" in json.dumps(view)
    assert "Force generate avatar" in json.dumps(view)
    picker = command(ui, slack, "avatar")
    assert picker["callback_id"] == "admin_avatar_picker"
    choices = ui.options({"user_id": "U10", "action_id": "avatar_participant", "value": "Maker2"})
    assert [option["value"] for option in choices["options"]] == [member]
    before = store.select("ledger_outbox", {"kind": "avatar_generate"})
    response = ui.submission(form(picker, {"avatar_participant": member}, user="U10"), slack)
    assert "No current personalized avatar" in json.dumps(response)
    assert "Force generate avatar" in json.dumps(response)
    assert store.select("ledger_outbox", {"kind": "avatar_generate"}) == before
    ui.action(action(response["view"]), slack)
    assert "generation queued" in json.dumps(slack.views_update.call_args.kwargs["view"])
    p = ledger.participant(member)
    p["preferences"]["avatars"] = False
    store.put("ledger_participants", p)
    assert ui.options({"user_id": "U10", "action_id": "avatar_participant", "value": "Maker2"}) == {"options": []}
    opted_out = command(ui, slack, "avatar <@U2>")
    assert "opted out of personalized avatars" in json.dumps(opted_out)
    assert "admin_avatar_generate" not in json.dumps(opted_out)


def test_opt_out_after_button_render_does_not_queue(admin):
    ledger, store, _, composer, _, slack = admin
    member = str(oid(2))
    ui = SlackUI(ledger, composer)
    view = command(ui, slack, "avatar <@U2>")
    p = ledger.participant(member)
    p["preferences"]["avatars"] = False
    store.put("ledger_participants", p)
    before = store.select("ledger_outbox", {"kind": "avatar_generate"})
    ui.action(action(view), slack)
    assert store.select("ledger_outbox", {"kind": "avatar_generate"}) == before
    assert "opted out" in json.dumps(slack.views_update.call_args.kwargs["view"])


@pytest.mark.parametrize("entry", ["command", "options", "button", "submission"])
def test_admin_authority_is_required_and_rechecked(admin, entry):
    ledger, _, source, composer, _, slack = admin
    ui = SlackUI(ledger, composer)
    view = command(ui, slack, "avatar <@U2>")
    picker = command(ui, slack, "avatar")
    source.data["members"][9]["role"] = "resource_manager"
    with pytest.raises(Denied):
        if entry == "command":
            command(ui, slack, "avatar")
        elif entry == "options":
            ui.options({"user_id": "U10", "action_id": "avatar_participant", "value": ""})
        elif entry == "button":
            ui.action(action(view), slack)
        else:
            ui.submission(form(picker, {"avatar_participant": str(oid(2))}, user="U10"), slack)


def test_force_invalidates_running_candidate_but_preserves_reference_job(admin):
    ledger, store, _, composer, _, slack = admin
    member = str(oid(2))
    p = ledger.participant(member)
    generation = store.claim("ledger_outbox", now() + timedelta(seconds=61), kinds=["avatar_generate"])
    while generation["payload"]["member_id"] != member:
        generation = store.claim("ledger_outbox", now() + timedelta(seconds=61), kinds=["avatar_generate"])
    enqueue(store, "ledger_outbox", "reference-pending", "avatar_reference", {"member_id": member})
    store.put(avatars.COLLECTION, {"_id": "job:" + generation["_id"], "kind": "job", "status": "uploaded",
        "avatar": {"file_id": "F_CANDIDATE"}})
    admin_avatars.generate(ledger, str(oid(10)), member, "force-running")
    assert store.get("ledger_outbox", generation["_id"])["status"] == "cancelled"
    assert store.get("ledger_outbox", "reference-pending")["status"] == "pending"
    cleanup = store.get("ledger_outbox", generation["_id"] + ":failed-cleanup")
    assert cleanup["payload"]["files"] == ["F_CANDIDATE"]
    assert cleanup["payload"]["failed_candidate"] is True
    with pytest.raises(Denied, match="lease changed"):
        avatars.AvatarPipeline(Worker(ledger, composer, slack)).live(generation)
    assert ledger.participant(member).get("avatar_generation", 0) == p.get("avatar_generation", 0)


def test_board_member_allowed_and_nonparticipant_rejected(admin):
    ledger, _, _, composer, _, slack = admin
    ledger.join(str(oid(11)))
    ui = SlackUI(ledger, composer)
    assert "Force generate" in json.dumps(command(ui, slack, "avatar <@U2>", user="U11"))
    with pytest.raises(Denied, match="participant"):
        command(ui, slack, "avatar <@U3>")
