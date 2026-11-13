import hashlib
import json
import logging
from types import SimpleNamespace

import pytest
from slack_sdk.errors import SlackApiError

from conftest import oid
from ledger.community import Community
from ledger.domain import enqueue_home_refresh
from ledger.views import home, home_processing
from ledger.worker import Worker


class SlackResponseLike:
    """Minimal SlackResponse mapping interface used by WebClient endpoints."""
    def __init__(self, data):
        self.data = data
        self.keys_read = []

    def get(self, key, default=None):
        self.keys_read.append(key)
        return self.data.get(key, default)


def working_job(store, key):
    job = store.get("ledger_outbox", key)
    job.update(status="working", lease="test-home", attempts=1)
    store.put("ledger_outbox", job)
    return job


def test_first_home_open_publishes_processing_and_queues_one_build(env):
    ledger, store, _, composer, _, slack = env
    def normalized_response(user_id, view):
        normalized = json.loads(json.dumps(view))
        normalized["blocks"][0]["block_id"] = "generated-by-slack"
        return SlackResponseLike({"ok": True, "view": normalized})
    slack.views_publish.side_effect = normalized_response
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


def test_home_open_requeues_after_latest_refresh_failed(joined):
    ledger, store, _, composer, _, slack = joined
    member_id = str(oid(1))
    enqueue_home_refresh(store, member_id, "rank-correction", "U1")
    failed = store.get("ledger_outbox", f"home:{member_id}:rank-correction")
    failed.update(status="failed", attempts=10)
    store.put("ledger_outbox", failed)
    worker = Worker(ledger, composer, slack)

    worker.event({"type": "app_home_opened", "tab": "home", "user": "U1",
                  "view": home(ledger, member_id)}, "retry-home-after-failure")

    slack.views_publish.assert_called_once()
    assert slack.views_publish.call_args.kwargs["view"]["callback_id"] == "ledger_home_processing"
    retried = store.get("ledger_outbox", f"home:{member_id}:rank-correction")
    assert retried["status"] == "pending" and retried["attempts"] == 0
    assert retried["payload"]["slack_id"] == "U1"


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
    slack.views_publish.side_effect = lambda user_id, view: SlackResponseLike({"ok": True, "view": view})
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


def test_home_publish_accepts_slack_normalized_blocks(joined):
    ledger, store, _, composer, _, slack = joined
    member_id = str(oid(1))
    participant = ledger.participant(member_id)
    participant["import_pending"] = False
    store.put("ledger_participants", participant)
    enqueue_home_refresh(store, member_id, "normalized", "U1")
    job = working_job(store, f"home:{member_id}:normalized")
    def normalized_response(user_id, view):
        normalized = json.loads(json.dumps(view))
        normalized["blocks"][0]["block_id"] = "generated-by-slack"
        normalized["blocks"][0]["text"]["verbatim"] = False
        return SlackResponseLike({"ok": True, "view": normalized})
    slack.views_publish.side_effect = normalized_response

    Worker(ledger, composer, slack).publish_home(job)

    saved = store.get("ledger_homes", member_id)
    assert saved["published_rank"] == participant["rank"]
    assert saved["needs_replacement"] is False


def test_invalid_home_slack_file_is_diagnosed_discarded_and_regenerated(joined, caplog):
    ledger, store, _, composer, _, slack = joined
    member_id = str(oid(1))
    participant = ledger.participant(member_id)
    participant["import_pending"] = False
    store.put("ledger_participants", participant)
    enqueue_home_refresh(store, member_id, "invalid-slack-file", "U1")
    job = working_job(store, f"home:{member_id}:invalid-slack-file")
    slack.files_upload_v2.side_effect = [
        {"files": [{"id": "F_RANK_VALID"}]},
        {"files": [{"id": "F_TREE_REJECTED"}]},
    ]
    slack.files_info.side_effect = lambda file: {
        "file": {"id": file, "mimetype": "image/png", "filetype": "png", "mode": "hosted",
                 "size": 1234, "is_external": False, "is_public": False,
                 "public_url_shared": False, "display_as_bot": False, "is_deleted": False}}
    response_data = {"ok": False, "error": "invalid_arguments", "response_metadata": {
        "messages": ["[ERROR] invalid slack file [json-pointer:view/blocks/3/slack_file.id/slack_file]"]}}
    response = SimpleNamespace(status_code=200, data=response_data,
        headers={"x-slack-req-id": "req-invalid-file"}, get=response_data.get)
    slack.views_publish.side_effect = SlackApiError("invalid Slack file", response)
    worker = Worker(ledger, composer, slack)
    caplog.set_level(logging.ERROR)

    with pytest.raises(SlackApiError):
        worker.publish_home(job)

    rank_file = store.get("ledger_files", f"rank_icon:{participant['rank']}")
    tree_file = store.get("ledger_files", f"skill_tree:{member_id}")
    assert rank_file["file_id"] == "F_RANK_VALID"
    assert tree_file["file_id"] is None
    assert tree_file["invalidation_reason"] == "views_publish_invalid_slack_file"
    assert tree_file["invalidated_slack_request_id"] == "req-invalid-file"
    assert tree_file["invalidated_home_job"] == job["_id"]
    assert "block_index=3" in caplog.text
    assert "file_id=F_TREE_REJECTED" in caplog.text
    assert f"asset_key=skill_tree:{member_id}" in caplog.text
    assert '"available": true' in caplog.text and '"mode": "hosted"' in caplog.text

    slack.files_upload_v2.side_effect = None
    slack.files_upload_v2.return_value = {"files": [{"id": "F_TREE_REPLACEMENT"}]}
    slack.views_publish.side_effect = lambda user_id, view: SlackResponseLike({"ok": True, "view": view})
    worker.publish_home(job)

    assert store.get("ledger_files", f"skill_tree:{member_id}")["file_id"] == "F_TREE_REPLACEMENT"
    assert store.get("ledger_homes", member_id)["published_rank"] == participant["rank"]


def test_user_change_clears_and_refreshes_profile_photo_caption(joined):
    ledger, store, _, composer, _, slack = joined
    member_id = str(oid(1))
    store.put("ledger_homes", {"_id": member_id, "profile_photo_cksum": "old",
        "profile_photo_description": "A previous photo", "profile_photo_described_at": "old-date"})
    worker = Worker(ledger, composer, slack, bot_id="UBOT")
    observed = []
    worker._describe_home_profile_photo = lambda mid, uid, refresh_key=None: observed.append((mid, uid, refresh_key))
    worker.event({"type": "user_change", "user": {"id": "U1", "deleted": False, "is_bot": False}}, "profile-change")

    assert observed == []
    queued = store.get("ledger_outbox", f"home-photo:{member_id}:profile-change")
    assert queued["kind"] == "home_profile_photo"
    assert queued["payload"] == {"member_id": member_id, "slack_id": "U1", "refresh_key": "profile-change"}
    saved = store.get("ledger_homes", member_id)
    assert saved["profile_photo_refresh_key"] == "profile-change"
    assert not {"profile_photo_cksum", "profile_photo_description", "profile_photo_described_at"} & set(saved)

    worker.outbox(working_job(store, queued["_id"]))
    assert observed == [(member_id, "U1", "profile-change")]


def test_profile_lookup_accepts_slack_response_mapping(joined):
    ledger, store, _, composer, _, slack = joined
    worker = Worker(ledger, composer, slack)
    response = SlackResponseLike({"profile": {}})
    slack.users_profile_get.return_value = response

    worker._describe_home_profile_photo(str(oid(1)), "U1")

    # The response's mapping API is consulted; a dict-only check returns early.
    assert response.keys_read == ["profile"]


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
