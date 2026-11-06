import hashlib
import json

from conftest import oid
from ledger.community import Community
from ledger.domain import enqueue_home_refresh
from ledger.views import home, home_processing
from ledger.worker import Worker


def working_job(store, key):
    job = store.get("ledger_outbox", key)
    job.update(status="working", lease="test-home", attempts=1)
    store.put("ledger_outbox", job)
    return job


def test_first_home_open_publishes_processing_and_queues_one_build(env):
    ledger, store, _, composer, _, slack = env
    worker = Worker(ledger, composer, slack)
    event = {"type": "app_home_opened", "tab": "home", "user": "U1"}

    worker.event(event, "slack:event-1")
    worker.event({**event, "view": home_processing()}, "slack:event-2")

    slack.views_publish.assert_called_once()
    placeholder = slack.views_publish.call_args.kwargs["view"]
    assert placeholder["blocks"][0]["text"]["text"] == "Character Sheet"
    assert placeholder["blocks"][1]["text"]["text"] == "Processing..."
    jobs = store.select("ledger_outbox", {"kind": "home_publish", "payload.member_id": str(oid(1))})
    assert len(jobs) == 1


def test_home_open_ignores_other_tabs_and_preserves_generated_view(env):
    ledger, _, _, composer, _, slack = env
    worker = Worker(ledger, composer, slack)

    worker.event({"type": "app_home_opened", "tab": "messages", "user": "U1"}, "messages")
    worker.event({"type": "app_home_opened", "tab": "home", "user": "U1",
                  "view": home(ledger, str(oid(1)))}, "generated")

    slack.views_publish.assert_not_called()


def test_home_rebuilds_when_generated_view_belongs_to_another_member(joined):
    ledger, store, source, composer, _, slack = joined
    worker = Worker(ledger, composer, slack)
    previous_member, current_member = str(oid(1)), str(oid(2))
    stale_view = home(ledger, previous_member)
    # Reassign U1 to member 2 and remove member 2's former U2 link so the new
    # identity is valid and unambiguous.
    source.data["slack_users"] = [
        {**row, "member_id": oid(2)} if row["slack_id"] == "U1" else row
        for row in source.data["slack_users"] if row["slack_id"] != "U2"]

    worker.event({"type": "app_home_opened", "tab": "home", "user": "U1", "view": stale_view}, "reassigned-home")

    slack.views_publish.assert_called_once()
    assert slack.views_publish.call_args.kwargs["user_id"] == "U1"
    assert slack.views_publish.call_args.kwargs["view"]["callback_id"] == "ledger_home_processing"
    assert store.get("ledger_outbox", f"home:{current_member}:open:reassigned-home")


def test_home_rebuilds_when_consent_generation_changes(joined):
    ledger, store, _, composer, _, slack = joined
    member_id = str(oid(1))
    stale_view = home(ledger, member_id)
    ledger.leave(member_id)
    ledger.join(member_id)
    worker = Worker(ledger, composer, slack)

    worker.event({"type": "app_home_opened", "tab": "home", "user": "U1", "view": stale_view}, "rejoined-home")

    slack.views_publish.assert_called_once()
    assert slack.views_publish.call_args.kwargs["view"]["callback_id"] == "ledger_home_processing"
    assert any(job["kind"] == "home_publish" and job["payload"]["member_id"] == member_id
               for job in store.select("ledger_outbox"))


def test_home_build_publishes_rank_and_reuses_skill_tree_cache(joined):
    ledger, store, _, composer, _, slack = joined
    member_id = str(oid(1))
    participant = ledger.participant(member_id)
    participant["import_pending"] = False
    ledger.store.put("ledger_participants", participant)
    slack.files_info.return_value = {"file": {"id": "F_RANK_ICON", "is_deleted": False}}
    worker = Worker(ledger, composer, slack)
    enqueue_home_refresh(store, member_id, "home-test", "U1")

    assert worker.step("ledger_outbox", kinds=["home_publish"])
    assert store.get("ledger_outbox", f"home:{member_id}:home-test")["status"] == "done"
    first_upload_count = slack.files_upload_v2.call_count
    posted = slack.views_publish.call_args.kwargs["view"]
    blocks = json.dumps(posted["blocks"])
    assert posted["callback_id"] == "ledger_home_generated"
    assert "Character Sheet" in blocks and ledger.presentation(participant["rank"])["name"] in blocks
    assert '"id": "F_RANK_ICON"' in blocks

    enqueue_home_refresh(store, member_id, "home-test-again", "U1")
    second_job = working_job(store, f"home:{member_id}:home-test-again")
    worker.publish_home(second_job)
    assert slack.files_upload_v2.call_count == first_upload_count


def test_home_tree_cache_refreshes_when_content_changes_or_file_disappears(joined, monkeypatch):
    ledger, store, _, composer, _, slack = joined
    member_id = str(oid(1))
    participant = ledger.participant(member_id)
    participant["import_pending"] = False
    ledger.store.put("ledger_participants", participant)
    from ledger import skills
    summary = skills.skill_summary(ledger, member_id)
    checksum = hashlib.sha256(summary["text"].encode("utf-8")).hexdigest()
    store.put("ledger_files", {"_id": f"skill_tree:{member_id}", "kind": "skill_tree",
        "member_id": member_id, "file_id": "F_CACHED", "text_sha256": checksum})
    slack.files_info.return_value = {"file": {"id": "F_CACHED", "is_deleted": False}}
    worker = Worker(ledger, composer, slack)
    enqueue_home_refresh(store, member_id, "home-cache", "U1")
    job = working_job(store, f"home:{member_id}:home-cache")

    assert worker._home_skill_tree(member_id, job) == "F_CACHED"
    slack.files_upload_v2.assert_not_called()

    monkeypatch.setattr(skills, "skill_summary", lambda *_: {**summary, "text": summary["text"] + "\nChanged"})
    worker._home_skill_tree(member_id, job)
    assert slack.files_upload_v2.call_count == 1
    saved = store.get("ledger_files", f"skill_tree:{member_id}")
    assert saved["text_sha256"] != checksum

    slack.files_info.return_value = {"file": {"id": "F_RANK_ICON", "is_deleted": True}}
    worker._home_skill_tree(member_id, job)
    assert slack.files_upload_v2.call_count == 2


def test_verified_milestone_refreshes_home_but_xp_tick_does_not(joined):
    ledger, store, _, _, _, _ = joined
    member_id = str(oid(1))
    ledger.reconcile(member_id, historical=True)

    ledger.notify(member_id, "challenge", {"verified_milestone": True}, "verified-one", action_id="action-one")
    ledger.notify(member_id, "checkout_earned", {"xp_change": "17"}, "xp-only", action_id="action-two")
    ledger.major(member_id, "rank_up", {"rank": "Novice"}, "rank-change", action_id="action-three")

    assert store.get("ledger_outbox", f"home:{member_id}:action-one")
    assert not store.get("ledger_outbox", f"home:{member_id}:action-two")
    assert store.get("ledger_outbox", f"home:{member_id}:action-three")


def test_project_gallery_changes_enqueue_home_refreshes(joined):
    ledger, store, _, composer, _, slack = joined
    project = Community(ledger).project(str(oid(1)), "New workbench", "Built a safer work surface.")
    project_key = f"project:{project['_id']}:1"
    recorded_trigger = f"project:{project['_id']}:1:recorded"
    for member_id in (str(oid(1)), str(oid(2))):
        assert store.get("ledger_outbox", f"home:{member_id}:{recorded_trigger}")

    worker = Worker(ledger, composer, slack)
    job = working_job(store, project_key)
    worker.outbox(job)

    saved = store.get("ledger_projects", project["_id"])
    assert saved["permalink"] == "https://example.slack.com/archives/thread"
    published_trigger = f"project:{project['_id']}:1:published"
    for member_id in (str(oid(1)), str(oid(2))):
        assert store.get("ledger_outbox", f"home:{member_id}:{published_trigger}")


def test_rank_correction_queues_home_refresh(joined):
    ledger, store, _, composer, _, slack = joined
    member_id = str(oid(1))
    participant = ledger.participant(member_id)
    participant.update(rank=2, import_pending=False, revision=participant["revision"] + 1)
    store.put("ledger_participants", participant)

    ledger.correct_rank(str(oid(10)), member_id, 1, "Corrected after review")

    corrected = ledger.participant(member_id)
    job = store.get("ledger_outbox", f"home:{member_id}:rank-correction:{corrected['revision']}")
    assert job and job["payload"]["member_id"] == member_id
    Worker(ledger, composer, slack).publish_home(working_job(store, job["_id"]))
    view = slack.views_publish.call_args.kwargs["view"]
    assert f"*{ledger.presentation(1)['name']}*" in json.dumps(view["blocks"])


def test_opt_out_refreshes_to_public_home(joined):
    ledger, store, _, composer, _, slack = joined
    member_id = str(oid(1))
    ledger.leave(member_id)
    job = working_job(store, f"home:{member_id}:leave:{ledger.participant(member_id)['revision']}")
    slack.users_info.return_value = {"user": {"id": "U1", "deleted": False, "is_bot": False}}
    Worker(ledger, composer, slack).publish_home(job)

    published = slack.views_publish.call_args.kwargs["view"]
    assert published["callback_id"] == "ledger_home_public"
    assert "XP" not in json.dumps(published["blocks"])
    assert "Opt in" in json.dumps(published["blocks"])
    assert home(ledger, member_id)["callback_id"] == "ledger_home_public"
