from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json

import pytest

from conftest import oid
from ledger.authority import Authority
from ledger.domain import Denied
from ledger.progress import progress
from ledger.quests import Quests
from ledger.slack_app import SlackUI
from ledger.storage import now
from ledger import views
from ledger.worker import Worker, ingest_mqtt
from test_slack import form


def member(n):
    return str(oid(n))


def set_participant(l, s, n, **fields):
    if not l.participant(member(n)):
        l.join(member(n))
    p = l.participant(member(n))
    p.update(import_pending=False, **fields)
    s.put('ledger_participants', p)
    return p


def published(env, target=1, reward=123, classification='challenge', catalog=None):
    l, s, *_ = env
    set_participant(l, s, 3, rank=4)
    q = Quests(l).draft(member(3), 'Build a useful jig', 'Make a safe jig with feedback.', 'Show a working jig and applied feedback.', target)
    Quests(l).submit_draft(member(3), q['_id'])
    return Quests(l).publish(member(10), q['_id'], reward, classification, catalog_id=catalog)


def test_progress_uses_pinned_cumulative_rules_and_reports_blockers(joined):
    l, s, src, *_ = joined
    p = set_participant(l, s, 1, rank=2, xp='400', metrics={'checkouts': 3}, rank_hold=True)
    rules = deepcopy(s.get('ledger_rulesets', p['ruleset']))
    rules['_id'] = 'pinned-cumulative'
    rules['ranks'][1]['requirements']['checkouts'] = 8
    rules['ranks'][2]['requirements']['checkouts'] = 2
    s.put('ledger_rulesets', rules)
    p['ruleset'] = rules['_id']
    p['import_pending'] = True
    s.put('ledger_participants', p)
    src.data['members'][0]['subscription'] = False
    result = progress(l, member(1))
    checkout = next(x for x in result['milestones'] if x['metric'] == 'checkouts')
    assert checkout == {'metric': 'checkouts', 'current': '3', 'required': '8', 'remaining': '5'}
    assert result['remaining_xp'] == '200' and len(result['blockers']) == 3
    assert 1 <= len(result['suggestions']) <= 3
    set_participant(l, s, 1, rank=6, xp='9000')
    assert progress(l, member(1))['next_rank'] is None


@pytest.mark.parametrize('command', ['stats', 'progress', 'preferences', 'achievements'])
def test_member_modals_use_deterministic_values_and_no_inference(joined, command):
    l, _, _, composer, api, slack = joined
    SlackUI(l, composer).command({'user_id': 'U1', 'command': '/ledger', 'text': command, 'trigger_id': 'T'}, slack)
    view = slack.views_open.call_args.kwargs['view']
    assert view['type'] == 'modal'
    assert 'Qwen' not in json.dumps(view)
    api.complete.assert_not_called()


@pytest.mark.parametrize('pending', [True, False])
def test_new_character_sheet_uses_nonempty_metrics_state_without_inference(env, pending):
    l, s, _, composer, api, slack = env
    l.join(member(1))
    p = l.participant(member(1))
    assert p['metrics'] == {}
    p['import_pending'] = pending
    s.put('ledger_participants', p)
    SlackUI(l, composer).command({'user_id': 'U1', 'command': '/ledger', 'text': 'stats', 'trigger_id': 'T'}, slack)
    view = slack.views_open.call_args.kwargs['view']
    sections = [block['text']['text'] for block in view['blocks'] if block['type'] == 'section']
    assert all(text.strip() for text in sections)
    assert any('0 XP' in text and 'Deepest cleared skill' in text for text in sections)
    expected = 'Verified history import pending; progress may be incomplete.' if pending else 'No recorded milestones yet.'
    assert expected in sections
    assert any(block['type'] == 'actions' for block in view['blocks'])
    assert 'Recorded achievements: 0. Open Achievements for details.' in sections
    assert l.participant(member(1)) == p
    api.complete.assert_not_called()
    api.tool_response.assert_not_called()


def test_character_sheet_keeps_populated_metrics_and_zero_values(joined):
    l, s, *_ = joined
    set_participant(l, s, 1, metrics={'checkouts': 0, 'volunteer': '1.5'})
    view = views.character_sheet(l, member(1))
    sections = [block['text']['text'] for block in view['blocks'] if block['type'] == 'section']
    assert 'Checkouts: 0\nVolunteer: 1.5' in sections
    assert 'No recorded milestones yet.' not in sections
    assert all(text.strip() for text in sections)


def test_quest_browser_hash_and_forged_selection_revalidation(joined):
    l, _, _, composer, _, slack = joined
    q = published(joined)
    ui = SlackUI(l, composer)
    options = ui.options({'user': {'id': 'U1'}, 'action_id': 'quest_selection', 'value': 'jig'})['options']
    assert options == [views.option(q['title'], 'q:' + q['_id'])]
    body = {'user': {'id': 'U1'}, 'view': {'id': 'V', 'hash': 'protected-hash'}, 'actions': [
        {'action_id': 'quest_selection', 'selected_option': {'value': 'q:' + q['_id']}}]}
    ui.action(body, slack)
    sent = slack.views_update.call_args.kwargs
    assert sent['hash'] == 'protected-hash' and '123 XP' in json.dumps(sent['view'])
    body['actions'][0]['selected_option']['value'] = 'q:forged'
    with pytest.raises(ValueError):
        ui.action(body, slack)
    assert ui.options({'user': {'id': 'U3'}, 'action_id': 'quest_selection', 'value': 'jig'})['options'] == []


def test_exact_rank_acceptance_retains_reward_and_rankup_completion(joined):
    l, s, *_ = joined
    q = published(joined)
    set_participant(l, s, 2, rank=2)
    with pytest.raises(Denied):
        Quests(l).accept(member(2), q['_id'])
    accepted = Quests(l).accept(member(1), q['_id'])
    assert accepted['rank'] == 1 and accepted['reward'] == 123
    set_participant(l, s, 1, rank=2)
    doc = Quests(l).submit(member(1), q['_id'], 'Observed working jig; feedback applied.')
    Quests(l).verify(member(10), doc['_id'])
    assert l.participant(member(1))['xp'] == '123'
    l.reconcile(member(1))
    assert l.participant(member(1))['xp'] == '123'
    with pytest.raises(ValueError):
        Quests(l).verify(member(10), doc['_id'])
    assert len([j for j in s.select('ledger_outbox') if j['_id'].endswith(':author')]) == 1


@pytest.mark.parametrize('reward', [-1, 501, 0.5, True, '100'])
def test_quest_reward_boundaries_reject_invalid_values(joined, reward):
    l, s, *_ = joined
    set_participant(l, s, 3, rank=3)
    q = Quests(l).draft(member(3), 'Quest', 'Description', 'Observable evidence', 1)
    Quests(l).submit_draft(member(3), q['_id'])
    with pytest.raises(ValueError):
        Quests(l).publish(member(10), q['_id'], reward)
    assert s.get('ledger_quests', q['_id'])['status'] == 'pending_review'


@pytest.mark.parametrize('reward', [0, 500])
def test_quest_zero_and_max_rewards(joined, reward):
    l, s, *_ = joined
    q = published(joined, reward=reward)
    assert l.participant(member(3))['xp'] == '0'
    Quests(l).accept(member(1), q['_id'])
    doc = Quests(l).submit(member(1), q['_id'], 'Done and observed.')
    Quests(l).verify(member(10), doc['_id'])
    assert l.participant(member(1))['xp'] == str(reward)
    assert s.get('ledger_evidence', f"quest-complete:{member(1)}:{q['logical_id']}")


def test_author_rank_submission_publication_and_self_exclusions(joined):
    l, s, *_ = joined
    with pytest.raises(Denied):
        Quests(l).draft(member(1), 'Quest', 'Description', 'Evidence', 1)
    set_participant(l, s, 3, rank=3)
    with pytest.raises(Denied):
        Quests(l).draft(member(3), 'Quest', 'Description', 'Evidence', 2)
    q = Quests(l).draft(member(3), 'Quest', 'Description', 'Evidence', 1)
    Quests(l).submit_draft(member(3), q['_id'])
    with pytest.raises(Denied):
        Quests(l).publish(member(3), q['_id'], 100)
    set_participant(l, s, 3, rank=2)
    with pytest.raises(Denied):
        Quests(l).publish(member(10), q['_id'], 100)


def test_revision_deduplication_and_opted_out_author_literal_attribution(joined):
    l, s, *_ = joined
    service = Quests(l)
    q = published(joined)
    service.accept(member(1), q['_id'])
    new = service.draft(member(3), 'Revised jig', 'New description', 'New observable criteria', 1, revision_of=q['_id'])
    service.submit_draft(member(3), new['_id'])
    service.publish(member(10), new['_id'], 499)
    assert service.listing(member(1))[0]['_id'] == q['_id']
    l.leave(member(3))
    rendered = json.dumps(views.quest_browser(l, member(1), 'q:' + q['_id']))
    assert 'U3' in rendered and '<@U3>' not in rendered
    doc = service.submit(member(1), q['_id'], 'Working jig.')
    service.verify(member(10), doc['_id'])
    assert l.participant(member(1))['xp'] == '123'
    assert not [j for j in s.select('ledger_outbox') if j['_id'].endswith(':author')]
    assert service.listing(member(1)) == []
    assert s.get('ledger_quests', q['_id'])['description'] == q['description']


def test_disabled_author_and_rank_correction_block_outstanding_awards(joined):
    l, s, src, *_ = joined
    q = published(joined, target=2)
    set_participant(l, s, 1, rank=2)
    Quests(l).accept(member(1), q['_id'])
    doc = Quests(l).submit(member(1), q['_id'], 'Complete evidence')
    src.data['members'][2]['status'] = 'suspended'
    with pytest.raises(Denied):
        Quests(l).verify(member(10), doc['_id'])
    l.reconcile(member(3))
    assert s.get('ledger_quests', q['_id'])['status'] == 'disabled'
    src.data['members'][2]['status'] = 'activeMember'
    l.reconcile(member(3))
    assert s.get('ledger_quests', q['_id'])['status'] == 'disabled'
    l.correct_rank(member(10), member(3), 2, 'Source correction')
    assert l.participant(member(1))['xp'] == '0'


def test_specialized_milestones_do_not_add_catalog_xp(joined):
    l, _, *_ = joined
    q = published(joined, classification='first_build', catalog='first-build', reward=42)
    Quests(l).accept(member(1), q['_id'])
    doc = Quests(l).submit(member(1), q['_id'], 'Completed safe jig with feedback')
    with pytest.raises(Denied):
        l.review(member(10), doc['specialized_evidence'])
    Quests(l).verify(member(10), doc['_id'])
    assert l.participant(member(1))['metrics']['first_build'] == 1
    assert l.participant(member(1))['xp'] == '42'
    l.reconcile(member(1))
    assert l.participant(member(1))['xp'] == '42'


@pytest.mark.parametrize('classification', ['first_build', 'mentoring', 'stewardship', 'develop_mentor'])
@pytest.mark.parametrize('legacy', [False, True])
def test_specialized_resubmission_reviews_corrected_attempt_and_keeps_old_evidence(joined, classification, legacy):
    l, s, src, *_ = joined
    service = Quests(l)
    catalog = {'first_build': 'first-build', 'mentoring': 'mentoring-session'}.get(classification, 'specialized-v1')
    if classification in ('stewardship', 'develop_mentor'):
        entry = {'_id': catalog, 'kind': 'challenge', 'achievement': classification, 'criteria': 'Verified milestone evidence'}
        if classification == 'stewardship':
            src.data['volunteer_tasks'].append({'_id': oid(900), 'title': 'Workshop handoff'})
            entry['task_id'] = member(900)
        l.publish_catalog(member(10), entry)
    q = published(joined, classification=classification, catalog=catalog, reward=42)
    if classification == 'develop_mentor':
        guidance = l.submit(member(1), 'mentoring-session', 'Guided the corrected mentor', [member(2)])
        l.acknowledge(member(2), guidance['_id'])
        l.review(member(10), guidance['_id'])
        teaching = l.submit(member(2), 'mentoring-session', 'The corrected mentor taught independently', [member(3)])
        l.acknowledge(member(3), teaching['_id'])
        l.review(member(10), teaching['_id'])
    initial_xp = int(l.participant(member(1))['xp'])
    service.accept(member(1), q['_id'])
    first = service.submit(member(1), q['_id'], 'First attempt', [member(2)], member(4), 'Old handoff')
    l.acknowledge(member(2), first['specialized_evidence'])
    if legacy:
        # Reproduce records written before submission attempts were versioned.
        old = s.get('ledger_evidence', first['specialized_evidence'])
        s.delete('ledger_evidence', old['_id'])
        old['_id'] = f"submission:{member(1)}:member-quest:{q['logical_id']}"
        s.put('ledger_evidence', old)
        first['specialized_evidence'] = old['_id']
        first.pop('submission_version')
        s.delete('ledger_evidence', first['_id'])
        first['_id'] = f"quest-submission:{member(1)}:{q['logical_id']}"
        s.put('ledger_evidence', first)
        accepted = service.acceptance(member(1), q['logical_id'])
        accepted.pop('submission_id')
        s.put('ledger_relationships', accepted)
    old = s.get('ledger_evidence', first['specialized_evidence'])
    service.verify(member(10), first['_id'], approve=False, reason='Correct the evidence')
    rejected = s.get('ledger_evidence', first['_id'])
    first_review_id = f"review:quest-submission:{member(1)}:{q['logical_id']}:attempt:1"
    first_review = s.get('ledger_evidence', first_review_id)
    second = service.submit(member(1), q['_id'], 'Corrected attempt', [member(3)], member(2), 'Corrected usable handoff')
    assert second['submission_version'] == 2
    assert second['_id'] != first['_id']
    assert s.get('ledger_evidence', first['_id']) == rejected
    assert s.get('ledger_evidence', first_review_id) == first_review
    assert second['specialized_evidence'] != first['specialized_evidence']
    assert s.get('ledger_evidence', old['_id']) == old
    corrected = s.get('ledger_evidence', second['specialized_evidence'])
    assert corrected['description'] == second['description'] == 'Corrected attempt'
    assert corrected['learners'] == [member(3)] and corrected['acknowledged'] == []
    assert corrected['mentor'] == member(2) and corrected['handoff'] == 'Corrected usable handoff'
    acknowledgments = len([row for row in s.select('ledger_outbox') if row['_id'].startswith('ack:submission:')])
    assert service.submit(member(1), q['_id'], 'Repeated pending request', [member(2)]) == second
    assert len([row for row in s.select('ledger_outbox') if row['_id'].startswith('ack:submission:')]) == acknowledgments
    if classification == 'mentoring':
        with pytest.raises(ValueError, match='acknowledge'):
            service.verify(member(10), second['_id'])
    l.acknowledge(member(3), corrected['_id'])
    service.verify(member(11), second['_id'])
    assert s.get('ledger_evidence', first['_id']) == rejected
    assert s.get('ledger_evidence', first_review_id) == first_review
    assert first_review['status'] == 'rejected' and first_review['actor'] == member(10)
    assert first_review['reason'] == 'Correct the evidence'
    second_review = s.get('ledger_evidence', 'review:' + second['_id'])
    assert second_review['status'] == 'approved' and second_review['actor'] == member(11)
    assert second_review['evidence'] == second['_id'] and second_review['submission_version'] == 2
    assert s.get('ledger_evidence', corrected['_id'])['status'] == 'approved'
    assert s.get('ledger_evidence', old['_id']) == old
    assert l.participant(member(1))['metrics'][classification] == 1
    assert int(l.participant(member(1))['xp']) == initial_xp + 42
    l.reconcile(member(1))
    assert int(l.participant(member(1))['xp']) == initial_xp + 42
    with pytest.raises(Denied, match='already completed'):
        service.submit(member(1), q['_id'], 'Duplicate completion')


def test_submission_attempts_and_reviews_preserve_history_and_once_only_completion(joined):
    l, s, *_ = joined
    service = Quests(l)
    q = published(joined, reward=42)
    accepted = service.accept(member(1), q['_id'])
    attempts, reviews = [], []
    for version in (1, 2):
        attempt = service.submit(member(1), q['_id'], f'Attempt {version}')
        assert attempt['submission_version'] == version
        service.verify(member(10), attempt['_id'], False, f'Rejection {version}')
        attempts.append(s.get('ledger_evidence', attempt['_id']))
        reviews.append(s.get('ledger_evidence', 'review:' + attempt['_id']))
    with ThreadPoolExecutor(2) as pool:
        submitted = list(pool.map(lambda description: service.submit(member(1), q['_id'], description), ['Corrected final attempt', 'Concurrent retry']))
    assert submitted[0] == submitted[1]
    final = submitted[0]
    assert final['submission_version'] == 3
    assert service.acceptance(member(1), q['logical_id']) == {**accepted, 'submission_id': final['_id']}
    for attempt in attempts:
        with pytest.raises(ValueError, match='pending quest completion'):
            service.verify(member(11), attempt['_id'])
    service.verify(member(11), final['_id'])
    for attempt, review in zip(attempts, reviews):
        assert s.get('ledger_evidence', attempt['_id']) == attempt
        assert s.get('ledger_evidence', review['_id']) == review
    assert len(s.select('ledger_evidence', {'kind': 'quest_submission'})) == 3
    assert len(s.select('ledger_evidence', {'kind': 'quest_completion_review'})) == 3
    assert s.get('ledger_evidence', f"quest-complete:{member(1)}:{q['logical_id']}")['submission_id'] == final['_id']
    l.reconcile(member(1))
    assert l.participant(member(1))['xp'] == '42'
    assert len(s.select('ledger_awards', {'kind': 'quest'})) == 1


def test_reviewing_legacy_pending_attempt_preserves_existing_legacy_review(joined):
    l, s, *_ = joined
    service = Quests(l)
    q = published(joined, reward=42)
    accepted = service.accept(member(1), q['_id'])
    attempt = service.submit(member(1), q['_id'], 'Legacy corrected attempt')
    s.delete('ledger_evidence', attempt['_id'])
    logical_id = f"quest-submission:{member(1)}:{q['logical_id']}"
    attempt.update(_id=logical_id, submission_version=2)
    s.put('ledger_evidence', attempt)
    legacy_review = {'_id': 'review:' + logical_id, 'kind': 'quest_completion_review',
                     'actor': member(10), 'reason': 'Historical rejection', 'at': now()}
    s.put('ledger_evidence', legacy_review)
    # Legacy acceptance did not have a submission pointer.
    s.put('ledger_relationships', accepted)
    assert service.submit(member(1), q['_id'], 'Repeated pending request') == attempt
    service.verify(member(11), logical_id)
    assert s.get('ledger_evidence', legacy_review['_id']) == legacy_review
    new_review = s.get('ledger_evidence', 'review:' + logical_id + ':attempt:2')
    assert new_review['actor'] == member(11) and new_review['status'] == 'approved'
    assert new_review['evidence'] == logical_id
    assert l.participant(member(1))['xp'] == '42'


def test_grant_capability_scope_and_review_audit_without_staff_gate(joined):
    l, s, _, composer, _, slack = joined
    grant = Authority(l).grant(member(10), member(2), ['learning_review'], {'kind': 'global'}, 'Experienced independent reviewer')
    doc = l.submit(member(1), 'learning-challenge', 'Applied feedback')
    Worker(l, composer, slack).admin_command(member(2), ['approve', doc['_id']], 'review-test')
    review = s.select('ledger_awards', {'kind': 'review'})[-1]
    assert review['grant_id'] == grant['_id'] and review['grant_version'] == 1
    with pytest.raises(Denied):
        l.review(member(2), doc['_id'], False, 'Cannot reverse finalized approvals')
    with pytest.raises(Denied):
        l.publish_ranks(member(2), s.get('ledger_rulesets', 'initial')['ranks'])
    with pytest.raises(Denied):
        Authority(l).grant(member(2), member(1), ['learning_review'], {'kind': 'global'}, 'No onward delegation')


def test_resource_manager_limits_multishop_and_unscoped_evidence(joined):
    l, s, src, *_ = joined
    src.data['members'][9].update(role='resource_manager', resource_manager_shop_ids=[oid(201), oid(202)])
    service = Authority(l)
    with pytest.raises(Denied):
        service.grant(member(10), member(2), ['learning_review'], {'kind': 'global'}, 'Overbroad')
    with pytest.raises(Denied):
        service.grant(member(10), member(2), ['learning_review'], {'kind': 'shops', 'shops': [member(203)]}, 'Overbroad')
    grant = service.grant(member(10), member(2), ['learning_review'], {'kind': 'shops', 'shops': [member(201)]}, 'Scoped')
    assert service.authorize(member(2), member(1), 'learning_review', [member(201)])['grant_id'] == grant['_id']
    for shops in [[], [member(201), member(202)], [member(203)]]:
        with pytest.raises(Denied):
            service.authorize(member(2), member(1), 'learning_review', shops)
    with pytest.raises(Denied):
        service.authorize(member(2), member(1), 'mentoring_review', [member(201)])
    src.data['members'][9]['resource_manager_shop_ids'] = []
    with pytest.raises(Denied):
        service.authorize(member(2), member(1), 'learning_review', [member(201)])
    l.reconcile(member(10))
    assert s.get('ledger_relationships', grant['_id'])['status'] == 'revoked'


def test_optout_revokes_in_same_transaction_and_return_never_revives(joined):
    l, s, *_ = joined
    grant = Authority(l).grant(member(10), member(2), ['learning_review'], {'kind': 'global'}, 'Independent review')
    l.leave(member(2))
    assert s.get('ledger_relationships', grant['_id'])['status'] == 'revoked'
    l.join(member(2))
    with pytest.raises(Denied):
        Authority(l).authorize(member(2), member(1), 'learning_review')
    assert len(s.select('ledger_evidence', {'kind': 'delegation_audit'})) == 2


def test_revoke_serializes_with_approval_and_preserves_prior_legitimate_review(joined):
    l, s, *_ = joined
    grant = Authority(l).grant(member(10), member(2), ['learning_review'], {'kind': 'global'}, 'Independent review')
    doc = l.submit(member(1), 'learning-challenge', 'Observed evidence')
    def approve():
        try:
            return l.review(member(2), doc['_id'])['status']
        except Denied:
            return 'denied'
    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(approve)
        b = pool.submit(Authority(l).revoke, member(10), grant['_id'], 'Explicit revocation')
        result, _ = a.result(), b.result()
    assert result in ('approved', 'denied')
    assert s.get('ledger_relationships', grant['_id'])['status'] == 'revoked'
    with pytest.raises(Denied):
        l.review(member(2), doc['_id'])
    assert s.get('ledger_evidence', doc['_id'])['status'] == ('approved' if result == 'approved' else 'pending')


def test_source_membership_revocation_is_permanent_before_reconciliation(joined):
    l, s, *_ = joined
    grant = Authority(l).grant(member(10), member(2), ['learning_review'], {'kind': 'global'}, 'Review')
    from bson import json_util
    payload = ('update 123 ' + json_util.dumps({'document': {'_id': oid(2), 'status': 'revoked'}})).encode()
    ingest_mqtt(s, 'members/update', payload)
    assert s.get('ledger_relationships', grant['_id'])['status'] == 'revoked'
    l.reconcile(member(2))
    assert s.get('ledger_relationships', grant['_id'])['status'] == 'revoked'


def test_launch_unlock_notice_is_once_per_highest_capability(joined):
    l, s, *_ = joined
    def unlocks():
        legacy = [j['_id'] for j in s.select('ledger_outbox') if j['_id'].startswith('dm:quest-unlock:')]
        grouped = [event['event_id'] for owner in s.select('ledger_evidence', {'kind': 'notification_summary'})
                   for event in owner['events'] if event['event_id'].startswith('quest-unlock:')]
        return legacy + grouped
    set_participant(l, s, 1, rank=3)
    l.reconcile(member(1))
    l.reconcile(member(1))
    assert len(unlocks()) == 1
    set_participant(l, s, 1, rank=4)
    l.reconcile(member(1))
    assert len(unlocks()) == 2
    l.reconcile(member(1))
    assert len(unlocks()) == 2


def test_async_draft_applies_suggestions_with_new_input_ids_and_preserves_review_submission(joined):
    l, s, _, composer, api, slack = joined
    set_participant(l, s, 1, rank=3)
    ui = SlackUI(l, composer)
    view = views.quest_author(l, member(1))
    body = form(view, {'title': 'Original', 'description': 'Make a jig', 'criteria': 'Show it working', 'target_rank': '1'}, 'U1')
    body['trigger_id'] = 'draft-help-trigger'
    body['view'].update(id='Vdraft', hash='original-hash')
    body['actions'] = [{'action_id': 'quest_draft_help'}]
    ui.action(body, slack)
    api.complete.assert_not_called()
    api.complete.return_value = json.dumps({'title': 'Suggested jig', 'description': 'Make a safe jig', 'criteria': 'Demonstrate feedback applied'})
    from test_worker import claim
    Worker(l, composer, slack).outbox(claim(s, 'draft-help:draft-help-trigger'))
    updated = slack.views_update.call_args.kwargs
    assert updated['hash'] == 'original-hash'
    assert updated['view']['blocks'][0]['block_id'].startswith('title:suggestion:')
    assert not s.select('ledger_quests')
    ui.submission(form(updated['view'], {'title': 'My edited jig', 'description': 'My edited description', 'criteria': 'My edited criteria', 'target_rank': '1'}, 'U1'), slack)
    ui.submission(form(updated['view'], {'title': 'My edited jig', 'description': 'My edited description', 'criteria': 'My edited criteria', 'target_rank': '1'}, 'U1'), slack)
    assert len(s.select('ledger_quests')) == 1
    q = s.select('ledger_quests')[0]
    assert q['title'] == 'My edited jig' and q['status'] == 'pending_review'
    assert l.participant(member(1))['xp'] == '0'


@pytest.mark.parametrize('select_revision', [False, True])
def test_revised_quest_grant_normalizes_scope_and_authorizes_publication_and_completion(joined, select_revision):
    l, s, *_ = joined
    service, authority = Quests(l), Authority(l)
    original = published(joined)
    revised = service.draft(member(3), 'Revised jig', 'Corrected jig description', 'Demonstrate the corrected jig',
                            1, revision_of=original['_id'])
    service.submit_draft(member(3), revised['_id'])
    requested = {'kind': 'quest', 'quest': revised['_id'] if select_revision else original['_id']}
    grant = authority.grant(member(10), member(2), ['quest_publish', 'quest_complete'], requested, 'Quest-specific review')
    assert revised['_id'] != original['logical_id']
    assert grant['scope'] == {'kind': 'quest', 'quest': original['logical_id']}
    assert requested['quest'] == (revised['_id'] if select_revision else original['_id'])
    assert s.get('ledger_relationships', grant['_id'])['scope'] == grant['scope']
    assert grant['quest_revision'] == requested['quest']
    service.publish(member(2), revised['_id'], 17)
    service.accept(member(1), revised['_id'])
    evidence = service.submit(member(1), revised['_id'], 'Completed the corrected jig')
    completed = service.verify(member(2), evidence['_id'])
    for record in (s.get('ledger_quests', revised['_id']), completed):
        assert record['review_authority'] == {'authority': 'delegated', 'grant_id': grant['_id'], 'grant_version': 1}
    assert l.participant(member(1))['xp'] == '17'
    with pytest.raises(Denied):
        authority.authorize(member(2), member(1), 'quest_complete', quest='unrelated-quest')
    with pytest.raises(Denied):
        authority.authorize(member(2), member(1), 'learning_review', quest=original['logical_id'])
    with pytest.raises(Denied):
        authority.authorize(member(2), member(2), 'quest_complete', quest=original['logical_id'])
    authority.revoke(member(10), grant['_id'], 'Review finished')
    with pytest.raises(Denied):
        authority.authorize(member(2), member(1), 'quest_publish', quest=original['logical_id'])


@pytest.mark.parametrize('legacy_grant', [False, True])
def test_revised_quest_grant_uses_selected_shops_and_preserves_legacy_audit(joined, legacy_grant):
    l, s, src, *_ = joined
    set_participant(l, s, 3, rank=3)
    service, authority = Quests(l), Authority(l)
    original = service.draft(member(3), 'Jig', 'Original shop', 'Observable jig', 1, shops=[member(201)])
    revised = service.draft(member(3), 'Revised jig', 'New shop', 'Observable jig', 1,
                            shops=[member(202)], revision_of=original['_id'])
    service.submit_draft(member(3), revised['_id'])
    src.data['members'][9].update(role='resource_manager', resource_manager_shop_ids=[oid(202)])
    grant = authority.grant(member(10), member(2), ['quest_publish'], {'kind': 'quest', 'quest': revised['_id']}, 'Shop two review')
    if legacy_grant:
        grant['scope']['quest'] = revised['_id']
        grant.pop('quest_revision')
        s.put('ledger_relationships', grant)
    assert authority.grant_valid(grant)
    review = service.publish(member(2), revised['_id'], 17)
    assert review['review_authority']['grant_id'] == grant['_id']
    assert s.get('ledger_relationships', grant['_id'])['scope'] == grant['scope']
    assert s.get('ledger_relationships', grant['_id'])['version'] == 1
    with pytest.raises(Denied):
        authority.authorize(member(2), member(1), 'quest_publish', [member(201)], original['logical_id'])
    src.data['members'][9]['resource_manager_shop_ids'] = []
    with pytest.raises(Denied):
        authority.authorize(member(2), member(1), 'quest_publish', [member(202)], original['logical_id'])


def test_revision_normalization_preserves_resource_manager_shop_limits(joined):
    l, s, src, *_ = joined
    set_participant(l, s, 3, rank=3)
    service, authority = Quests(l), Authority(l)
    original = service.draft(member(3), 'Jig', 'Shop one jig', 'Observable jig', 1, shops=[member(201)])
    service.submit_draft(member(3), original['_id'])
    service.publish(member(10), original['_id'], 17)
    within = service.draft(member(3), 'Revised jig', 'Still shop one', 'Observable jig', 1,
                           shops=[member(201)], revision_of=original['_id'])
    outside = service.draft(member(3), 'Multi-shop jig', 'Additional shop', 'Observable jig', 1,
                            shops=[member(201), member(202)], revision_of=original['_id'])
    src.data['members'][9].update(role='resource_manager', resource_manager_shop_ids=[oid(201)])
    grant = authority.grant(member(10), member(2), ['quest_publish'], {'kind': 'quest', 'quest': within['_id']}, 'Shop one review')
    assert grant['scope']['quest'] == original['logical_id']
    with pytest.raises(Denied, match="grantor's current staff authority"):
        authority.grant(member(10), member(2), ['quest_publish'], {'kind': 'quest', 'quest': outside['_id']}, 'Outside assignment')
    service.submit_draft(member(3), within['_id'])
    assert service.publish(member(2), within['_id'], 17)['status'] == 'published'
    service.submit_draft(member(3), outside['_id'])
    with pytest.raises(Denied):
        service.publish(member(2), outside['_id'], 17)
    assert s.get('ledger_quests', outside['_id'])['status'] == 'pending_review'


@pytest.mark.parametrize('scope', [{'kind': 'quest'}, {'kind': 'quest', 'quest': 'missing'},
                                  {'kind': 'quest', 'quest': 'missing', 'extra': 'forged'}])
def test_quest_grant_rejects_missing_or_malformed_selection(joined, scope):
    l, s, *_ = joined
    with pytest.raises(ValueError, match='existing quest'):
        Authority(l).grant(member(10), member(2), ['quest_publish'], scope, 'Invalid selection')
    assert not s.select('ledger_relationships', {'kind': 'delegation'})


def test_quest_scoped_grants_and_publication_ui_admit_valid_delegates(joined):
    l, s, _, composer, _, slack = joined
    set_participant(l, s, 3, rank=3)
    q = Quests(l).draft(member(3), 'Quest', 'Description', 'Observable criteria', 1)
    Quests(l).submit_draft(member(3), q['_id'])
    grant = Authority(l).grant(member(10), member(2), ['quest_publish'], {'kind': 'quest', 'quest': q['_id']}, 'Quest-specific review')
    ui = SlackUI(l, composer)
    ui.command({'user_id': 'U2', 'command': '/ledger-admin', 'text': 'publish-quest ' + q['_id'], 'trigger_id': 'T'}, slack)
    view = slack.views_open.call_args.kwargs['view']
    ui.submission(form(view, {'reward': '17', 'classification': 'challenge'}, 'U2'), slack)
    assert s.get('ledger_quests', q['_id'])['review_authority']['grant_id'] == grant['_id']
    with pytest.raises(Denied):
        Authority(l).authorize(member(2), member(1), 'quest_publish', quest='another-quest')


@pytest.mark.parametrize('cause', ['suspended', 'revoked', 'identity', 'mapping'])
def test_delegate_live_invalidation_cleanup_and_restoration_require_new_grant(joined, cause):
    l, s, src, *_ = joined
    grant = Authority(l).grant(member(10), member(2), ['learning_review'], {'kind': 'global'}, 'Independent review')
    if cause in ('suspended', 'revoked'):
        src.data['members'][1]['status'] = cause
    elif cause == 'identity':
        s.put('ledger_catalog', {'_id': f'identity:{member(2)}', 'deactivated': True})
    else:
        src.data['slack_users'][1]['invalidated_at'] = now()
    with pytest.raises(Denied):
        Authority(l).authorize(member(2), member(1), 'learning_review')
    l.reconcile(member(2))
    assert s.get('ledger_relationships', grant['_id'])['status'] == 'revoked'
    src.data['members'][1]['status'] = 'activeMember'
    src.data['slack_users'][1].pop('invalidated_at', None)
    s.put('ledger_catalog', {'_id': f'identity:{member(2)}', 'deactivated': False})
    with pytest.raises(Denied):
        Authority(l).authorize(member(2), member(1), 'learning_review')
