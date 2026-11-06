from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock
from xml.etree import ElementTree

import pytest

from ledger.messages import ChatAPI, ChatTransportError, Composer, _ShortCircuitBreaker
from ledger.prompt_library import AUDIENCES, TYPES, library_template
from ledger.prompt_matrix import MAX_MATRIX_BYTES, REQUIRED_ROLES, bundled_matrix


def reservation(store, composer, profile="receipt"):
    kind = "conversation" if profile == "guidance" else "delivery" if profile == "receipt" else "status"
    return store.atomic(lambda tx: composer.reserve(tx, kind, "member", "dm:caller", profile=profile))


def test_short_profiles_snapshot_exact_projection_and_keep_full_policy(env):
    _, store, _, composer, api, _ = env
    for name, sections, tokens in (
        ("receipt", ["identity", "authority", "kudos", "privacy", "response"], 128),
        ("summary", ["identity", "authority", "privacy", "response"], 128),
        ("guidance", ["identity", "authority", "privacy", "response"], 256),
    ):
        saved = reservation(store, composer, name)
        profile = saved["generation_profile"]
        assert profile["version"] == 1
        assert profile["deadline"] == 10 and profile["max_tokens"] == tokens
        projected = ElementTree.fromstring(profile["policy_text"])
        full = ElementTree.fromstring(saved["matrix"]["text"])
        assert [node.tag for node in projected] == sections
        assert profile["matrix_sha256"] == saved["matrix"]["sha256"]
        for section in projected:
            assert section.text == full.find(section.tag).text
        assert "<![CDATA[" in profile["policy_text"]
        assert full.find("roles") is not None and projected.find("roles") is None
    api.complete.assert_not_called()


def test_short_generation_uses_saved_policy_settings_and_paired_style(env):
    _, store, _, composer, api, _ = env
    composer.choose = lambda variants: variants[-1]
    saved = reservation(store, composer)
    before = deepcopy(saved)
    composer.matrix._current["text"] = "POLICY_CHANGED_AFTER_RESERVATION"
    api.complete.return_value = "The ink may rest."
    result = composer.compose("delivery", "member", {"summary": "Confirmed."}, selection=saved)
    assert result["text"] == "The ink may rest." and result["outcome"] == "generated"
    assert result["generation_profile"] == "receipt" and result["generation_ms"] >= 0
    assert result["latency"] == result["generation_ms"] / 1000
    assert result["fallback_reason"] is None
    args, kwargs = api.complete.call_args
    assert args[2] == 128 and kwargs == {"deadline": 10}
    assert before["generation_profile"]["policy_text"] in args[0][0]["content"]
    assert "POLICY_CHANGED_AFTER_RESERVATION" not in str(args)
    assert saved == before


def test_legacy_reservation_keeps_full_policy_and_original_bounds(env):
    _, store, _, composer, api, _ = env
    saved = store.atomic(lambda tx: composer.reserve(tx, "status", "member", "legacy"))
    assert "generation_profile" not in saved
    composer.compose("status", "member", {}, selection=saved)
    assert api.complete.call_args.args[2] == 384
    assert api.complete.call_args.kwargs == {}
    assert saved["matrix"]["text"] in api.complete.call_args.args[0][0]["content"]


def test_complete_accepts_keyword_deadline_and_keeps_thinking_disabled(monkeypatch):
    api = ChatAPI("http://unused.example/v1", "model")
    exchange = Mock(return_value={"choices": [{"finish_reason": "stop", "message": {"content": "Brief."}}]})
    monkeypatch.setattr(api, "_exchange", exchange)
    assert api.complete([], max_tokens=128, deadline=10) == "Brief."
    assert exchange.call_args.args[2] == 10
    assert exchange.call_args.args[1]["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.parametrize("text", ["Your kudos reached <@U2>.", "You earned 17 XP.", "Shop1 is complete.", "Pending.", "x" * 241,
                                  "Use /ledger next.", "Kindness matters. The ink may rest.", "The ink\nmay rest."])
def test_short_output_leaves_all_authoritative_facts_to_python(env, text):
    _, store, _, composer, api, _ = env
    api.complete.return_value = text
    result = composer.compose("delivery", "member", {"shop": "Shop1"}, selection=reservation(store, composer))
    assert result["text"] == "" and result["outcome"] == "fallback"
    assert result["fallback_reason"] == "invalid_output"
    assert api.complete.call_count == 1


@pytest.mark.parametrize("key,label", [("shop_name", "Woodworking"), ("tool_name", "Bandsaw"),
    ("quest_title", "Repair a stool"), ("challenge_title", "Organize storage"), ("project_title", "Shelf assembly"),
    ("new_rank", "Apprentice"), ("milestone", "First Build")])
def test_optional_narration_rejects_nested_achievement_labels(env, key, label):
    _, store, _, composer, api, _ = env
    api.complete.return_value = label + " is worth recording."
    facts = {"achievements": [{"type": "challenge", "details": [{key: label}]}]}
    result = composer.compose("status", "member", facts, selection=reservation(store, composer, "summary"))
    assert result["text"] == "" and result["fallback_reason"] == "invalid_output"


def test_optional_narration_checks_only_named_nested_fields(env):
    _, store, _, composer, api, _ = env
    api.complete.return_value = "The ink may rest."
    facts = {"achievements": [{"type": "challenge", "summary": "The ink may rest", "details": [
        {"description": "The ink may rest", "tool_name": "Bandsaw"}]}]}
    result = composer.compose("status", "member", facts, selection=reservation(store, composer, "summary"))
    assert result["text"] == "The ink may rest." and result["outcome"] == "generated"


def test_transport_failure_breaker_opens_after_three_without_retry(env, caplog):
    _, store, _, composer, api, _ = env
    ticks = [0]
    composer._short_breaker = _ShortCircuitBreaker(lambda: ticks[0])
    api.complete.side_effect = ChatTransportError("PRIVATE_UPSTREAM_DETAIL")
    saved = reservation(store, composer)
    for _ in range(3):
        result = composer.compose("delivery", "member", {}, selection=saved)
        assert result["fallback_reason"] == "transport_failure" and result["text"] == ""
    with caplog.at_level("INFO"):
        result = composer.compose("delivery", "member", {}, selection=saved)
    assert result["fallback_reason"] == "circuit_open"
    assert api.complete.call_count == 3
    assert "PRIVATE_UPSTREAM_DETAIL" not in caplog.text
    ticks[0] = 60
    api.complete.side_effect = None
    api.complete.return_value = "A small kindness matters."
    assert composer.compose("delivery", "member", {}, selection=saved)["outcome"] == "generated"
    assert composer.compose("delivery", "member", {}, selection=saved)["outcome"] == "generated"
    assert api.complete.call_count == 5


def test_circuit_allows_single_probe_and_reopens_for_sixty_seconds():
    ticks = [10]
    circuit = _ShortCircuitBreaker(lambda: ticks[0])
    for _ in range(3):
        circuit.finish(circuit.acquire(), transport_failed=True)
    assert circuit.acquire() is None
    ticks[0] = 70
    probe = circuit.acquire()
    assert probe is not None and circuit.acquire() is None
    circuit.finish(probe, transport_failed=True)
    ticks[0] = 129
    assert circuit.acquire() is None
    ticks[0] = 130
    circuit.finish(circuit.acquire())
    assert circuit.acquire() is not None


def test_circuit_ignores_old_inflight_success_and_serializes_probe():
    ticks = [0]
    circuit = _ShortCircuitBreaker(lambda: ticks[0])
    previous = circuit.acquire()
    for _ in range(3):
        circuit.finish(circuit.acquire(), transport_failed=True)
    circuit.finish(previous)
    assert circuit.acquire() is None
    ticks[0] = 60
    with ThreadPoolExecutor(max_workers=8) as pool:
        probes = list(pool.map(lambda _: circuit.acquire(), range(8)))
    assert sum(probe is not None for probe in probes) == 1


def test_malformed_responses_do_not_trip_transport_breaker(env):
    _, store, _, composer, api, _ = env
    composer._short_breaker = _ShortCircuitBreaker(lambda: 0)
    api.complete.side_effect = ValueError("Malformed model result")
    saved = reservation(store, composer)
    for _ in range(5):
        assert composer.compose("delivery", "member", {}, selection=saved)["fallback_reason"] == "invalid_output"
    assert api.complete.call_count == 5


def test_guidance_uses_private_progress_without_future_rank_or_unrelated_fields(env):
    _, store, _, composer, api, _ = env
    facts = {"rank": "Current", "next_rank": "SECRET_FUTURE_RANK", "remaining_xp": "300",
             "blockers": [], "suggestions": ["Explore /ledger-skills for a safe next clearance."],
             "milestones": [{"metric": "checkouts", "remaining": "2", "internal_notes": "SECRET_NOTE"}],
             "billing_details": "SECRET_BILLING", "other_member": "SECRET_MEMBER"}
    api.complete.return_value = "Explore /ledger-skills and choose a clearance that interests you."
    result = composer.guidance(facts, selection=reservation(store, composer, "guidance"))
    assert result["outcome"] == "generated"
    args, kwargs = api.complete.call_args
    assert args[2] == 256 and kwargs == {"deadline": 10}
    assert "checkouts" in str(args) and "SECRET_" not in str(args)
    assert "tools" not in kwargs


def test_guidance_preserves_saved_system_user_pair_and_narrower_template_cap(env):
    _, store, _, composer, api, _ = env
    saved = reservation(store, composer, "guidance")
    saved["template"]["variations"][0].update(system="SAVED_SYSTEM {current_rank}", user="SAVED_USER {facts}")
    saved["template"]["audience_instruction"] = "PRIVATE_AUDIENCE_GUARD"
    api.complete.return_value = "Explore your skill tree."
    composer.guidance({"rank": "Current", "suggestions": ["Explore your skill tree."]}, selection=saved)
    system, user = api.complete.call_args.args[0]
    assert "SAVED_SYSTEM" in system["content"] and "PRIVATE_AUDIENCE_GUARD" in system["content"]
    assert "SAVED_USER" in user["content"] and "Explore your skill tree." in user["content"]
    template = library_template("status", "member")
    template.update(_id="small-template", max_tokens=32)
    store.put("ledger_message_templates", template)
    store.put("ledger_message_templates", {"_id": "head:status:member", "version": "small-template"})
    small = reservation(store, composer, "summary")
    assert small["generation_profile"]["max_tokens"] == 32
    composer.compose("status", "member", {}, selection=small)
    assert api.complete.call_args.args[2] == 32


@pytest.mark.parametrize("failure", [TimeoutError(), ValueError(), "Try to earn SECRET_FUTURE_RANK next."])
def test_guidance_falls_back_to_blocker_before_optional_suggestion(env, failure):
    _, store, _, composer, api, _ = env
    if isinstance(failure, Exception):
        api.complete.side_effect = failure
    else:
        api.complete.return_value = failure
    result = composer.guidance({"next_rank": "SECRET_FUTURE_RANK", "blockers": ["History import is pending."],
                               "suggestions": ["Explore another shop."]}, selection=reservation(store, composer, "guidance"))
    assert result["text"] == "History import is pending." and result["outcome"] == "fallback"
    assert api.complete.call_count == 1


def test_packaged_result_styles_and_policy_preserve_schema():
    assert len(TYPES) == 23
    for kind in ("delivery", "status"):
        for audience in AUDIENCES:
            variants = library_template(kind, audience)["variations"]
            assert len(variants) == 5
            assert variants[-1]["id"] == "wry_grimoire"
            assert "optional" in variants[-1]["system"]
            assert len({v["user"] for v in variants}) == 5
    matrix = bundled_matrix()
    assert matrix["version"] == "28" and len(matrix["text"].encode()) <= MAX_MATRIX_BYTES
    root = ElementTree.fromstring(matrix["text"])
    assert {role.get("id") for role in root.find("roles")} == REQUIRED_ROLES
    assert "sixty seconds" in root.find("kudos").text
    assert "never force jokes" in root.find("identity").text
