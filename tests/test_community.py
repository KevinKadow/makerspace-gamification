from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from conftest import oid
from ledger.community import Community
from ledger.domain import Denied
from ledger.storage import now
from ledger.worker import Worker


def test_quest_requires_two_people_disciplines_and_independent_acceptance(joined):
    l, s, src, *_ = joined
    src.data['volunteer_tasks'].append({'_id': oid(900), 'title': 'Community arcade'})
    c = Community(l)
    q = c.create_quest(str(oid(10)), 'Build arcade', 'Working cabinet, safe wiring, usable handoff', ['wood', 'electronics'], str(oid(900)))
    for member, discipline in [(str(oid(1)), 'wood'), (str(oid(2)), 'electronics')]:
        c.quest(member, q['_id'], 'join', role=discipline)
        c.quest(member, q['_id'], 'submit', description='Built and documented my part')
    with pytest.raises(Denied):
        c.quest(str(oid(1)), q['_id'], 'verify', member=str(oid(1)))
    c.quest(str(oid(10)), q['_id'], 'verify', member=str(oid(1)))
    assert l.participant(str(oid(1)))['xp'] == '0'
    c.quest(str(oid(10)), q['_id'], 'verify', member=str(oid(2)))
    assert s.get('ledger_quests', q['_id'])['status'] == 'completed'
    for member in (str(oid(1)), str(oid(2))):
        assert l.participant(member)['xp'] == '500'
        assert l.participant(member)['metrics']['boss'] == 1


def test_buddy_mutual_acceptance_and_project_collaborator_credit(joined):
    l, s, *_ = joined
    c = Community(l)
    a, b = str(oid(1)), str(oid(2))
    with pytest.raises(Denied):
        c.buddy(a, b)
    p = l.participant(a)
    p['rank'] = 3
    s.put('ledger_participants', p)
    rel = c.buddy(a, b)
    assert rel['status'] == 'offered'
    with pytest.raises(Denied):
        c.buddy(a, rel['_id'], 'accept')
    assert c.buddy(b, rel['_id'], 'accept')['status'] == 'active'
    project = c.project(a, 'Cabinet', 'Advice on joinery welcome', [b])
    c.project(a, 'Cabinet', 'Applied feedback and revised the joint', [b], project['_id'])
    assert len(s.get('ledger_projects', project['_id'])['updates']) == 2
    assert s.get('ledger_projects', project['_id'])['collaborators'] == [b]
    assert l.participant(a)['xp'] == '0'
    with pytest.raises(Denied):
        c.project(b, 'Hijack', 'No', project_id=project['_id'])


def test_resource_manager_can_review_only_own_shop_without_rank_permission(joined):
    l, s, src, comp, _, slack = joined
    rm, author, learner = str(oid(3)), str(oid(1)), str(oid(2))
    src.data['members'][2].update(role='resource_manager', resource_manager_shop_ids=[oid(201)])
    own = l.submit(author, 'mentoring-session', 'Shop one lesson', [learner], shop=str(oid(201)))
    other = l.submit(author, 'mentoring-session', 'Shop two lesson', [learner], shop=str(oid(202)))
    l.acknowledge(learner, own['_id'])
    l.acknowledge(learner, other['_id'])
    w = Worker(l, comp, slack)
    w.admin_command(rm, ['approve', own['_id']], 'rm-review')
    assert s.get('ledger_evidence', own['_id'])['status'] == 'approved'
    with pytest.raises(Denied):
        w.admin_command(rm, ['approve', other['_id']], 'outside')
    with pytest.raises(Denied):
        w.admin_command(rm, ['rollback', 'initial'], 'forbidden')


def test_rank_correction_does_not_reaward_until_independent_release(joined):
    l, s, *_ = joined
    m = str(oid(1))
    p = l.participant(m)
    p.update(xp='1000', rank=2, metrics={'checkouts': 2, 'first_build': 1})
    s.put('ledger_participants', p)
    l.correct_rank(str(oid(10)), m, 1, 'Mistaken approval; under review')
    l.tx('_advance', m)
    assert l.participant(m)['rank'] == 1
    with pytest.raises(Denied):
        l.release_rank(m, m, 'Self review')
    l.release_rank(str(oid(11)), m, 'Resolved with independent evidence')
    assert l.participant(m)['rank'] == 2
    assert len(s.select('ledger_awards', {'kind': 'rank_correction'})) == 1


def test_kudos_calendar_boundaries_and_historical_sponsor_exclusion(joined):
    l, _, *_ = joined
    a, b = str(oid(1)), str(oid(2))
    # At the fall DST transition, both 01:30 occurrences belong to the same NY day/week.
    for key, instant in [('before', datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)),
                         ('after', datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)),
                         ('monday', datetime(2026, 11, 2, 5, 1, tzinfo=timezone.utc))]:
        with patch('ledger.domain.now', return_value=instant):
            l.kudos(a, b, 'Thanks', key=key, expected_participation=True)
    assert l.participant(b)['xp'] == '34'


def test_invalid_mapping_and_suspension_prevent_access(joined):
    l, _, src, *_ = joined
    a = str(oid(1))
    src.data['slack_users'].append({'_id': oid(999), 'member_id': oid(3), 'slack_id': 'U1'})
    assert not l.active(a) and src.identity('U1') is None
    src.data['slack_users'].pop()
    src.data['members'][0]['status'] = 'revoked'
    assert not l.active(a)
    assert l.participant(a)['xp'] == '0'


def test_pause_preserves_opt_out_and_separate_channel_queue(joined):
    l, s, _, comp, _, slack = joined
    w = Worker(l, comp, slack)
    w.admin_command(str(oid(10)), ['pause'], 'pause')
    with pytest.raises(Denied):
        l.join(str(oid(3)))
    l.leave(str(oid(1)))
    assert w.step('ledger_outbox', kinds=['remove'])
    assert slack.conversations_kick.called
    w.admin_command(str(oid(10)), ['resume'], 'resume')
    l.join(str(oid(3)))


def test_develop_mentor_requires_guidance_then_independently_verified_teaching(joined):
    l, s, *_ = joined
    a, b, learner, admin = map(lambda n: str(oid(n)), [1, 2, 3, 10])
    l.publish_catalog(admin, {'_id': 'develop-v1', 'kind': 'challenge', 'achievement': 'develop_mentor', 'criteria': 'Guide a new mentor who teaches independently'})
    candidate = l.submit(a, 'develop-v1', 'Guided a new mentor', mentor=b)
    with pytest.raises(ValueError, match='guidance'):
        l.review(admin, candidate['_id'])
    guidance = l.submit(a, 'mentoring-session', 'Taught how to teach', [b])
    l.acknowledge(b, guidance['_id'])
    l.review(admin, guidance['_id'])
    lesson = l.submit(b, 'mentoring-session', 'Independent lesson', [learner])
    l.acknowledge(learner, lesson['_id'])
    l.review(admin, lesson['_id'])
    l.review(admin, candidate['_id'])
    assert l.participant(a)['metrics']['develop_mentor'] == 1
