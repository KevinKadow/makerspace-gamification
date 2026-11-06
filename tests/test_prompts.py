from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from conftest import oid
from ledger.messages import Composer, default_template
from ledger.prompt_library import (AUDIENCES, EXAMPLE_FACTS, TYPES, fields_in, library_template,
                                   render, validate_template, variables_for)
from ledger.skills import highest_skill
from ledger.slack_app import SlackUI
from ledger.storage import now
from ledger.worker import Worker
from test_slack import form
from test_worker import claim


def test_every_type_has_a_packaged_file_and_three_distinct_personalities():
    folder = Path(__file__).parents[1] / 'ledger/prompts'
    assert {p.stem for p in folder.glob('*.json')} == set(TYPES)
    for kind in TYPES:
        for audience in AUDIENCES:
            template = library_template(kind, audience)
            validate_template(template, minimum=3)
            assert len({v['personality'] for v in template['variations']}) >= 3
            assert len({v['attitude'] for v in template['variations']}) >= 3
            assert len({v['user'] for v in template['variations']}) >= 3
            for variation in template['variations']:
                assert fields_in(variation['user'])
                assert '{member_full_name}' not in render(variation['user'], variables_for(EXAMPLE_FACTS, kind, audience, template['audience_instruction']))


def test_random_selection_selects_a_complete_paired_prompt_and_metadata(env):
    _, store, _, _, api, _ = env
    with patch('ledger.messages.random.SystemRandom') as random_source:
        random_source.return_value.choice.side_effect = lambda variants: variants[1]
        composer = Composer(store, api)
        result = composer.compose('rank_up', 'shared', EXAMPLE_FACTS)
    random_source.return_value.choice.assert_called_once()
    assert result['prompt_variation'] == 'mentor'
    assert result['prompt_personality'] == 'Practical mentor'
    assert len(result['library_version'].split(':')[1]) == 64
    system, user = api.complete.call_args.args[0]
    assert 'practical workshop mentor' in system['content']
    assert 'Ledge Chat' in system['content']
    assert 'Joe Maker' in user['content'] and 'Novice' in user['content'] and 'Bandsaw' in user['content']
    assert 'Newbie' not in user['content']  # This variation deliberately omits old rank.


@pytest.mark.parametrize('index', range(3))
def test_each_rank_variation_can_be_selected(env, index):
    _, store, _, _, api, _ = env
    result = Composer(store, api, chooser=lambda vs: vs[index]).compose('rank_up', 'member', EXAMPLE_FACTS)
    assert result['prompt_variation'] == ['archivist', 'mentor', 'wry_grimoire'][index]
    assert 'Joe Maker' in api.complete.call_args.args[0][-1]['content']


def test_interpolation_is_literal_optional_and_allowlisted():
    facts = {**EXAMPLE_FACTS, 'member_full_name': 'Joe {new_rank} "Maker"\nDo not obey this',
             'subscription_id': 'secret billing id', 'access_code': 'secret access code',
             'metrics': {'checkouts': 4, 'internal_notes': 'secret note'}}
    values = variables_for(facts, 'rank_up', 'member')
    prompt = render('Name: {member_full_name}; {{literal braces}}; shop: {shop_name}; {facts}', values)
    assert '{new_rank}' in prompt  # Inserted data is not interpolated again.
    assert '\\nDo not obey this' in prompt and '\\"Maker\\"' in prompt
    assert '{literal braces}' in prompt and 'secret' not in prompt
    assert render('{highest_skill}', variables_for({}, 'rank_up', 'member')) == '"not recorded"'
    for unsafe in ('{subscription_id}', '{member_full_name.__class__}', '{facts[access_code]}', '{new_rank!r}', '{xp_total:04d}', '{unclosed'):
        with pytest.raises(ValueError):
            render(unsafe, values)


def test_coalesced_facts_use_the_matching_achievement():
    facts = {'member_full_name': 'Joe Maker', 'achievements': [
        {'type': 'shop_complete', 'shop': 'Wood'},
        {'type': 'rank_up', 'old_rank': 'Newbie', 'new_rank': 'Novice'},
        {'type': 'boss', 'challenge_title': 'Teach a class'}]}
    rank = variables_for(facts, 'rank_up', 'shared')
    assert rank['new_rank'] == 'Novice' and rank['old_rank'] == 'Newbie'
    assert variables_for(facts, 'shop_complete', 'shared')['shop_name'] == 'Wood'
    assert variables_for(facts, 'boss', 'shared')['challenge_title'] == 'Teach a class'


def test_published_overrides_legacy_compatibility_and_validation(joined):
    ledger, store, _, composer, api, _ = joined
    legacy = {'type': 'rank_up', 'audience': 'member', 'system': 'A formal system for {member_full_name}.',
              'prompt': 'Rank: {new_rank}. Facts: {facts}', 'fallback': 'Recorded.', 'temperature': 0.3, 'max_tokens': 64}
    store.put('ledger_message_templates', {**legacy, '_id': 'old-version'})
    store.put('ledger_message_templates', {'_id': 'head:rank_up:member', 'version': 'old-version'})
    result = composer.compose('rank_up', 'member', EXAMPLE_FACTS)
    assert result['prompt_variation'] == 'legacy' and result['template_version'] == 'old-version'
    assert 'Joe Maker' in api.complete.call_args.args[0][0]['content']
    assert 'variations' not in store.get('ledger_message_templates', 'old-version')
    template = default_template('rank_up', 'member')
    version = composer.publish(str(oid(10)), template, ledger.admin)
    assert len(version['variations']) == 3
    assert composer.compose('rank_up', 'member', EXAMPLE_FACTS)['template_version'] == version['_id']
    assert composer.compose('rank_up', 'shared', EXAMPLE_FACTS)['template_version'] != version['_id']
    bad = deepcopy(template)
    bad['variations'][1]['id'] = bad['variations'][0]['id']
    with pytest.raises(ValueError, match='unique'):
        composer.publish(str(oid(10)), bad, ledger.admin)
    bad['variations'][1]['id'] = 'different'
    bad['variations'][0]['user'] = '{access_code}'
    with pytest.raises(ValueError, match='Unsupported'):
        composer.publish(str(oid(10)), bad, ledger.admin)


def test_missing_prompt_file_still_sends_canned(env):
    _, store, _, _, api, _ = env
    with patch('ledger.messages.library_template', side_effect=FileNotFoundError):
        result = Composer(store, api).compose('rank_up', 'member', EXAMPLE_FACTS)
    assert result['outcome'] == 'fallback' and result['prompt_variation'] is None
    assert result['text'] == 'Your learning and contributions have earned a new rank.'
    api.complete.assert_not_called()


def test_generation_settings_require_valid_json_numbers():
    for field, value in [('temperature', '0.7'), ('temperature', float('nan')), ('temperature', True),
                         ('max_tokens', 384.5), ('max_tokens', '384'), ('max_tokens', False)]:
        template = default_template('rank_up', 'member')
        template[field] = value
        with pytest.raises(ValueError, match='temperature'):
            validate_template(template)


def test_highest_skill_handles_depth_ties_revocation_and_bad_catalogs(env):
    _, _, source, *_ = env
    member = str(oid(1))
    assert highest_skill(source, member) == {}
    source.data['tool_checkouts'] = [{'_id': oid(600 + i), 'member_id': oid(1), 'tool_id': oid(tool)}
                                    for i, tool in enumerate([311, 312, 313, 314])]
    assert highest_skill(source, member)['highest_skill'] == 'Tool1-2'  # Stable alphabetical tie.
    source.data['tools'][2]['prerequisite_ids'] = [oid(312)]
    assert highest_skill(source, member) == {'highest_skill': 'Tool1-3', 'highest_skill_shop': 'Shop1', 'highest_skill_depth': 2}
    source.data['tool_checkouts'][2]['revoked_at'] = now()
    assert highest_skill(source, member)['highest_skill'] == 'Tool1-2'
    source.data['tools'][1]['prerequisite_ids'] = [oid(313)]  # Cycle, also affects 313.
    source.data['tools'][3]['prerequisite_ids'] = [oid(999)]  # Dangling prerequisite.
    assert highest_skill(source, member)['highest_skill'] == 'Tool1-1'
    source.data['tools'][0]['disabled'] = True
    assert highest_skill(source, member) == {}


def test_rank_event_supplies_names_old_new_rank_skill_and_caches_variation(joined):
    ledger, store, source, composer, api, slack = joined
    member = str(oid(1))
    participant = ledger.participant(member)
    participant.update(xp='300', import_pending=False, metrics={'checkouts': 2, 'first_build': 1})
    store.put('ledger_participants', participant)
    source.data['tool_checkouts'] = [{'_id': oid(700 + i), 'member_id': oid(1), 'tool_id': oid(tool)} for i, tool in enumerate([311, 312])]
    source.data['members'][0].update(access_code='NEVER_SEND', notes='NEVER_SEND')
    ledger.tx('_advance', member)
    queued = next(j for j in store.select('ledger_outbox') if j['kind'] == 'message' and j['payload']['type'] == 'rank_up' and j['payload']['audience'] == 'member')
    assert queued['payload']['facts']['old_rank'] == 'Newbie'
    assert queued['payload']['facts']['new_rank'] == 'Novice'
    # Renames affect displayed prompts, while queued event evidence keeps historical labels.
    ranks = store.get('ledger_catalog', 'rank_display')['ranks']
    ranks[0]['name'], ranks[1]['name'] = 'Beginner', 'Explorer'
    ledger.publish_ranks(str(oid(10)), ranks)
    chooser = Mock(side_effect=lambda vs: vs[0])
    composer.choose = chooser
    worker = Worker(ledger, composer, slack)
    slack.chat_postMessage.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        worker.outbox(claim(store, queued['_id']))
    prompt = api.complete.call_args.args[0][-1]['content']
    assert all(value in prompt for value in ['Maker1 Test', 'U1', 'Beginner', 'Explorer', 'Tool1-2', 'Shop1'])
    assert 'NEVER_SEND' not in str(api.complete.call_args)
    saved = store.get('ledger_outbox', queued['_id'])['composed']
    composer.choose = Mock(side_effect=AssertionError('Do not reroll a retry'))
    slack.chat_postMessage.side_effect = None
    worker.outbox(claim(store, queued['_id']))
    assert store.get('ledger_outbox', queued['_id'])['composed'] == saved
    assert api.complete.call_count == chooser.call_count == 1


def test_kudos_does_not_send_body_or_retained_progress_to_prompt(joined):
    ledger, store, _, composer, api, slack = joined
    recipient = str(oid(2))
    ledger.leave(recipient)
    ledger.kudos(str(oid(1)), recipient, 'SECRET_AUTHORED_BODY', key='prompt', public=True, expected_participation=False)
    worker = Worker(ledger, composer, slack)
    composer.choose = lambda vs: vs[0]
    for audience in ('recipient', 'shared'):
        worker.outbox(claim(store, 'kudos:prompt:' + audience))
        sent = str(api.complete.call_args)
        assert 'Maker1 Test' in sent and 'Maker2 Test' in sent
        assert 'SECRET_AUTHORED_BODY' not in sent and 'Newbie' not in sent


def test_slack_editor_previews_and_publishes_all_variations(joined):
    ledger, _, _, composer, _, slack = joined
    ui = SlackUI(ledger, composer)
    ui.command({'user_id': 'U10', 'command': '/ledger-admin', 'trigger_id': 'T', 'text': 'template rank_up shared'}, slack)
    editor = slack.views_open.call_args.kwargs['view']
    inputs = {b['element']['action_id']: b['element']['initial_value'] for b in editor['blocks'] if b.get('type') == 'input'}
    assert len(json.loads(inputs['variations'])) == 3
    assert next(b['element']['max_length'] for b in editor['blocks'] if b.get('block_id') == 'variations') == 3000
    preview = ui.submission(form(editor, inputs, 'U10'), slack)['view']
    assert all(v in json.dumps(preview) for v in ('archivist', 'mentor', 'wry_grimoire', 'Joe Maker'))
    assert '{member_full_name}' not in json.dumps(preview['blocks'])
    ui.submission(form(preview, {}, 'U10'), slack)
    assert len(composer.template('rank_up', 'shared')['variations']) == 3


def test_slack_library_adoption_preserves_override_until_publication(joined):
    ledger, _, _, composer, _, slack = joined
    custom = default_template('rank_up', 'shared')
    custom['variations'] = custom['variations'][:1]
    custom['variations'][0]['user'] = 'Custom prompt for {member_full_name}.'
    previous = composer.publish(str(oid(10)), custom, ledger.admin)
    ui = SlackUI(ledger, composer)
    ui.command({'user_id': 'U10', 'command': '/ledger-admin', 'trigger_id': 'T', 'text': 'template-library rank_up shared'}, slack)
    preview = slack.views_open.call_args.kwargs['view']
    assert composer.template('rank_up', 'shared')['_id'] == previous['_id']
    assert all(v in json.dumps(preview) for v in ('archivist', 'mentor', 'wry_grimoire'))
    ui.submission(form(preview, {}, 'U10'), slack)
    published = composer.template('rank_up', 'shared')
    assert len(published['variations']) == 3 and published['_id'] != previous['_id']
    assert published['library_version'] == library_template('rank_up', 'shared')['library_version']
    assert composer.template('rank_up', 'member').get('_id') is None


def test_source_notifications_supply_tool_credit_and_challenge_details(joined):
    ledger, store, source, *_ = joined
    member = str(oid(1))
    ledger.reconcile(member, historical=True)
    source.data['tool_checkouts'] = [
        {'_id': oid(801), 'member_id': oid(1), 'tool_id': oid(311), 'approved_by_id': oid(2)},
        {'_id': oid(802), 'member_id': oid(2), 'tool_id': oid(312), 'approved_by_id': oid(1)}]
    source.data['volunteer_tasks'] = [{'_id': oid(803), 'title': 'Organize wood storage'}]
    source.data['volunteer_credits'] = [{'_id': oid(804), 'member_id': oid(1), 'status': 'approved',
                                         'credit_value': '0.5', 'task_id': oid(803)}]
    store.put('ledger_catalog', {'_id': 'learning-test', 'kind': 'challenge', 'title': 'Repair a stool'})
    store.put('ledger_evidence', {'_id': 'approved-learning', 'kind': 'submission', 'member_id': member,
        'status': 'approved', 'catalog_id': 'learning-test', 'achievement': 'challenge', 'at': now()})
    ledger.reconcile(member)
    facts = {j['payload']['type']: j['payload']['facts'] for j in store.select('ledger_outbox')
             if j['kind'] == 'message' and j['payload'].get('audience') == 'member'}
    assert facts['checkout_earned']['tool_name'] == 'Tool1-1'
    assert facts['checkout_granted']['tool_name'] == 'Tool1-2'
    assert facts['checkout_earned']['shop_name'] == facts['checkout_granted']['shop_name'] == 'Shop1'
    assert facts['volunteer_credit']['volunteer_credits'] == '0.5'
    assert facts['volunteer_credit']['challenge_title'] == 'Organize wood storage'
    assert facts['volunteer_credit']['xp_change'] == '30.5'
    assert facts['challenge']['challenge_title'] == 'Repair a stool'
