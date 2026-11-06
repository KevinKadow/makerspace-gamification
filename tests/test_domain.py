from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest

from conftest import oid
from ledger.domain import ConsentChanged, Denied
from ledger.rules import attainable_rank, default_rules, validate_rules
from ledger.storage import now


def test_write_boundary_and_transaction_rollback(env):
    _, store, *_ = env
    with pytest.raises(ValueError):
        store.put("members", {"_id": "x"})
    def fail(s):
        s.put("ledger_evidence", {"_id": "test"})
        raise ValueError("rollback")
    with pytest.raises(ValueError):
        store.atomic(fail)
    assert store.get("ledger_evidence", "test") is None


def test_consent_and_rejoin_preserve_version_and_xp(joined):
    l, s, *_ = joined
    member = str(oid(1))
    l.tx("award", member, "test", "12.25", "challenge")
    version = l.participant(member)["ruleset"]
    l.leave(member)
    assert not l.active(member)
    assert all(j["status"] == "cancelled" for j in s.select("ledger_outbox", {"kind": "invite", "status": "cancelled"}))
    assert len([j for j in s.select("ledger_outbox", {"kind": "remove"}) if j["payload"]["member_id"] == member]) == 7
    l.join(member)
    assert l.participant(member)["ruleset"] == version
    assert l.participant(member)["xp"] == "12.25"


def test_sponsorship_is_not_consent(env):
    l, s, *_ = env
    l.join(str(oid(1)))
    l.sponsor(str(oid(1)), str(oid(2)))
    assert l.participant(str(oid(2))) is None
    assert not any(j["kind"] == "invite" and j["payload"]["member_id"] == str(oid(2)) for j in s.select("ledger_outbox"))


def test_cohort_rules_and_live_rank_names(joined):
    l, s, *_ = joined
    rules = deepcopy(s.get("ledger_rulesets", "initial")["ranks"])
    rules[1].update(name="Explorer", emoji=":hammer:", floor="400")
    new = l.publish_ranks(str(oid(10)), rules)
    assert l.participant(str(oid(1)))["ruleset"] == "initial"
    l.join(str(oid(3)))
    assert l.participant(str(oid(3)))["ruleset"] == new["_id"]
    assert l.presentation(2)["name"] == "Explorer"
    l.leave(str(oid(1)))
    l.join(str(oid(1)))
    assert l.participant(str(oid(1)))["ruleset"] == "initial"
    restored = l.rollback_ranks(str(oid(11)), "initial")
    assert restored["_id"] != "initial"
    assert l.presentation(2)["name"] == "Novice"


def test_rank_authorization_stale_editor_and_slot_seven(joined):
    l, s, source, *_ = joined
    rules = deepcopy(s.get("ledger_rulesets", "initial")["ranks"])
    source.data["members"][0]["role"] = "resource_manager"
    with pytest.raises(Denied):
        l.publish_ranks(str(oid(1)), rules)
    l.publish_ranks(str(oid(10)), rules, expected="initial")
    with pytest.raises(ValueError, match="changed"):
        l.publish_ranks(str(oid(10)), rules, expected="initial")
    rules[-1].update(enabled=True, name="Master", emoji="🏅", floor="8000")
    with pytest.raises(ValueError, match="milestones"):
        validate_rules({"ranks": rules})
    rules[-1]["requirements"] = {"completed_shops": 3, "mentoring": 20}
    validate_rules({"ranks": rules})


@pytest.mark.parametrize("slot", range(2, 7))
def test_rank_floors_and_cumulative_milestones(slot):
    rules = default_rules()
    metrics = {}
    for rank in rules["ranks"][:slot]:
        for name, value in rank["requirements"].items():
            metrics[name] = max(metrics.get(name, 0), value)
    floor = Decimal(rules["ranks"][slot - 1]["floor"])
    assert attainable_rank(rules, floor, metrics) == slot
    assert attainable_rank(rules, floor - Decimal("0.01"), metrics) < slot
    metrics["first_build"] = 0
    assert attainable_rank(rules, floor, metrics) == 1


def test_checkout_rates_credit_precedence_reversal_and_duplicate_reconciliation(joined):
    l, _, source, *_ = joined
    m1, m2 = str(oid(1)), str(oid(2))
    source.data["tool_checkouts"] = [
        {"_id": oid(501), "member_id": oid(1), "tool_id": oid(311), "approved_by_id": oid(2), "checked_out_at": now()},
        {"_id": oid(502), "member_id": oid(1), "tool_id": oid(312), "approved_by_id": oid(2), "checked_out_at": now()}]
    source.data["volunteer_credits"] = [{"_id": oid(601), "member_id": oid(2), "status": "approved", "credit_value": 0.25, "tool_checkout_id": oid(501)}]
    l.reconcile(m1)
    l.reconcile(m2)
    assert Decimal(l.participant(m1)["xp"]) == 131
    assert Decimal(l.participant(m2)["xp"]) == 134
    assert l.participant(m2)["metrics"]["volunteer"] == "0.25"
    l.reconcile(m2)
    assert Decimal(l.participant(m2)["xp"]) == 134
    source.data["tool_checkouts"][0]["revoked_at"] = now()
    source.data["volunteer_credits"][0]["reversed"] = True
    source.data["volunteer_credits"].append({"_id": oid(602), "member_id": oid(2), "status": "reversal", "credit_value": -0.25, "reversal_of_id": oid(601)})
    l.reconcile(m1)
    l.reconcile(m2)
    assert Decimal(l.participant(m1)["xp"]) == 100
    assert Decimal(l.participant(m2)["xp"]) == 67
    assert Decimal(l.participant(m2)["metrics"]["volunteer"]) == 0


def test_credit_arrives_before_teaching_and_fractional_credit(joined):
    l, _, source, *_ = joined
    member = str(oid(2))
    source.data["volunteer_credits"] = [
        {"_id": oid(601), "member_id": oid(2), "status": "approved", "credit_value": 0.25, "tool_checkout_id": oid(501)},
        {"_id": oid(602), "member_id": oid(2), "status": "approved", "credit_value": 0.5}]
    l.reconcile(member)
    assert Decimal(l.participant(member)["xp"]) == Decimal("30.5")
    source.data["tool_checkouts"].append({"_id": oid(501), "member_id": oid(1), "tool_id": oid(311), "approved_by_id": oid(2)})
    l.reconcile(member)
    assert Decimal(l.participant(member)["xp"]) == Decimal("97.5")


def test_silent_accrual_and_historical_import_emit_no_shared_posts(joined):
    l, s, source, *_ = joined
    member = str(oid(1))
    l.leave(member)
    source.data["volunteer_credits"].append({"_id": oid(600), "member_id": oid(1), "status": "approved", "credit_value": 3})
    l.reconcile(member)
    assert l.participant(member)["xp"] == "183"
    assert not [j for j in s.select("ledger_outbox", {"status": "pending"}) if j["payload"].get("member_id") == member and (j["kind"] == "mqtt" or j["payload"].get("audience") == "shared")]


def test_kudos_nonparticipant_has_no_profile_or_retroactive_xp(joined):
    l, s, *_ = joined
    giver, recipient = str(oid(1)), str(oid(3))
    result = l.kudos(giver, recipient, "*Thanks* for your help :hammer:", key="one", public=True, invite=True, expected_participation=False)
    assert not result["xp_awarded"]
    assert l.participant(recipient) is None
    assert len([j for j in s.select("ledger_outbox") if j["kind"] == "kudos"]) == 2
    l.join(recipient, giver)
    l.reconcile(recipient, historical=True)
    assert l.participant(recipient)["xp"] == "0"
    assert l.participant(giver)["xp"] == "0"


def test_kudos_caps_delivery_and_idempotence(joined):
    l, s, *_ = joined
    recipient = str(oid(2))
    for i in [1, 3, 4, 5, 6, 7]:
        giver = str(oid(i))
        l.join(giver)
        l.kudos(giver, recipient, "Thanks for explaining the technique!", key=f"{i}", public=True, expected_participation=True)
    assert Decimal(l.participant(recipient)["xp"]) == 85
    l.kudos(str(oid(1)), recipient, "Thanks again", key="again", expected_participation=True)
    l.kudos(str(oid(1)), recipient, "Duplicate", key="1", expected_participation=True)
    assert Decimal(l.participant(recipient)["xp"]) == 85
    assert len(s.select("ledger_evidence", {"kind": "kudos"})) == 7


def test_kudos_validation_and_participation_race(joined):
    l, _, source, *_ = joined
    giver, recipient = str(oid(1)), str(oid(2))
    for message in ("", "   ", "x" * 2001):
        with pytest.raises(ValueError):
            l.kudos(giver, recipient, message, key="x", expected_participation=True)
    with pytest.raises(Denied):
        l.kudos(giver, giver, "self", key="x", expected_participation=True)
    with pytest.raises(ValueError, match="belong"):
        l.kudos(giver, recipient, "great", key="x", shop=str(oid(201)), tool=str(oid(321)), expected_participation=True)
    l.leave(recipient)
    with pytest.raises(ConsentChanged):
        l.kudos(giver, recipient, "great", key="x", expected_participation=True)
    result = l.kudos(giver, recipient, "great", key="x", expected_participation=False)
    assert not result["xp_awarded"]
    source.data["members"][1]["status"] = "suspended"
    with pytest.raises(Denied):
        l.kudos(giver, recipient, "great", key="y", expected_participation=False)


def test_expiration_and_earned_or_prepaid_coverage(joined):
    l, _, source, *_ = joined
    member = source.data["members"][0]
    member["subscription"] = False
    assert not source.eligible_for_rank(str(member["_id"]))
    source.data["earned_memberships"].append({"_id": oid(700), "member_id": member["_id"], "status": "active"})
    assert source.eligible_for_rank(str(member["_id"]))
    source.data["earned_memberships"] = []
    l.coverage(str(oid(10)), str(member["_id"]), "Verified prepaid coverage")
    assert source.eligible_for_rank(str(member["_id"]), l.store.get("ledger_evidence", f"coverage:{member['_id']}"))
    member["expirationTime"] = 1
    assert source.good_standing(str(member["_id"]))
    assert not source.eligible_for_rank(str(member["_id"]), l.store.get("ledger_evidence", f"coverage:{member['_id']}"))


def test_recruitment_requires_new_verified_milestone(joined):
    l, _, source, *_ = joined
    giver, recruit = str(oid(1)), str(oid(3))
    l.sponsor(giver, recruit)
    source.data["volunteer_credits"].append({"_id": oid(601), "member_id": oid(3), "status": "approved", "credit_value": 1, "created_at": now() - timedelta(days=1)})
    l.join(recruit, giver)
    l.reconcile(recruit, historical=True)
    assert l.participant(giver)["xp"] == "0"
    source.data["volunteer_credits"].append({"_id": oid(602), "member_id": oid(3), "status": "approved", "credit_value": 1, "created_at": now()})
    l.reconcile(recruit)
    l.reconcile(recruit)
    assert l.participant(giver)["xp"] == "11"


def test_completion_snapshot_is_not_invalidated_by_new_tool(joined):
    l, s, source, *_ = joined
    l.publish_catalog(str(oid(10)), {"_id": "shop1-v1", "kind": "shop_completion", "shop_id": str(oid(201))})
    source.data["tool_checkouts"] = [{"_id": oid(500 + i), "member_id": oid(1), "tool_id": oid(310 + i)} for i in range(1, 5)]
    l.reconcile(str(oid(1)), historical=True)
    assert l.participant(str(oid(1)))["metrics"]["completed_shops"] == 1
    source.data["tools"].append({"_id": oid(315), "shop_id": oid(201), "name": "New tool"})
    l.reconcile(str(oid(1)))
    assert l.participant(str(oid(1)))["metrics"]["completed_shops"] == 1


def test_new_shop_version_applies_to_unearned_completions(joined):
    l, s, source, *_ = joined
    admin, member = str(oid(10)), str(oid(1))
    l.publish_catalog(admin, {"_id": "shop-old", "kind": "shop_completion", "shop_id": str(oid(201))})
    source.data["tools"].append({"_id": oid(315), "shop_id": oid(201), "name": "New tool"})
    l.publish_catalog(admin, {"_id": "shop-new", "kind": "shop_completion", "shop_id": str(oid(201))})
    source.data["tool_checkouts"] = [{"_id": oid(500 + i), "member_id": oid(1), "tool_id": oid(310 + i)} for i in range(1, 5)]
    l.reconcile(member, historical=True)
    assert l.participant(member)["metrics"]["completed_shops"] == 0
    source.data["tool_checkouts"].append({"_id": oid(505), "member_id": oid(1), "tool_id": oid(315)})
    l.reconcile(member)
    assert l.participant(member)["metrics"]["completed_shops"] == 1


def test_mentoring_requires_acknowledgment_and_independent_review(joined):
    l, *_ = joined
    doc = l.submit(str(oid(1)), "mentoring-session", "Helped with joinery", [str(oid(2))])
    with pytest.raises(Denied):
        l.review(str(oid(1)), doc["_id"])
    with pytest.raises(ValueError, match="acknowledge"):
        l.review(str(oid(10)), doc["_id"])
    l.acknowledge(str(oid(2)), doc["_id"])
    l.review(str(oid(10)), doc["_id"])
    assert l.participant(str(oid(1)))["metrics"]["mentoring"] == 1
