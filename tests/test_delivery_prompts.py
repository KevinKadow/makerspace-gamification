import json
from unittest.mock import patch

import pytest

from conftest import oid
from ledger.kudos import delivery_facts
from ledger.messages import default_template
from ledger.prompt_library import variables_for
from ledger.slack_app import SlackUI
from ledger import views
from ledger.worker import Worker
from test_slack import form
from test_worker import claim


def member(n):
    return str(oid(n))


def legacy_evidence(store, evidence):
    """Simulate actions accepted before summary routing; their receipts still drain."""
    owner_id = evidence.pop('summary_id', None)
    evidence.pop('result_summary', None)
    evidence.pop('action_id', None)
    store.put('ledger_evidence', evidence)
    if owner_id:
        store.delete('ledger_evidence', owner_id)
        for job in store.select('ledger_outbox'):
            if job['payload'].get('summary_id') == owner_id:
                store.delete('ledger_outbox', job['_id'])
    return evidence


def test_legacy_kudos_ack_and_success_dm_use_custom_json_and_sender_history(joined):
    l, s, _, composer, api, slack = joined
    template = default_template('delivery', 'member')
    for index, variation in enumerate(template['variations']):
        variation['system'] = 'CUSTOM_RECEIPT_' + str(index)
        variation['user'] = 'State {delivery_status}; recipient {recipient_mention}; DM {dm_status}; public {public_status}; XP {xp_result}.'
    composer.publish(member(10), template, l.admin)
    composer.choose = lambda variants: variants[0]
    legacy_evidence(s, l.kudos(member(1), member(2), 'PRIVATE_AUTHORED_KUDOS',
                             key='receipt-prompts', expected_participation=True))
    ui = SlackUI(l, composer)
    ui.submission(form(views.kudos_form(l, member(2), 'receipt-prompts'), {'message': 'PRIVATE_AUTHORED_KUDOS'}), slack)
    api.complete.assert_not_called()  # Interaction never waits for inference.
    w = Worker(l, composer, slack)
    api.complete.return_value = 'Your kudos to <@U2> are queued.'
    w.outbox(claim(s, 'ack:kudos:receipt-prompts'))
    queued = s.get('ledger_outbox', 'ack:kudos:receipt-prompts')['composed']
    prompt = api.complete.call_args.args[0]
    assert 'CUSTOM_RECEIPT_0' in prompt[0]['content']
    assert 'recipient "<@U2>"' in prompt[-1]['content'] and 'State "queued"' in prompt[-1]['content']
    assert slack.chat_postMessage.call_args.kwargs['blocks'][0]['text']['text'] == api.complete.return_value
    assert 'Kudos accepted; DM: queued' in slack.chat_postMessage.call_args.kwargs['blocks'][1]['text']['text']
    w.outbox(claim(s, 'kudos:receipt-prompts:recipient'))
    receipt_key = 'dm:receipt:kudos:receipt-prompts:recipient:delivered'
    api.complete.return_value = 'The delivery to <@U2> was successful.'
    job = claim(s, receipt_key)
    w.outbox(job)
    success = s.get('ledger_outbox', receipt_key)['composed']
    assert success['prompt_variation'] != queued['prompt_variation']
    assert success['prompt_scope'] == queued['prompt_scope'] == 'member:' + member(1)
    prompt = api.complete.call_args.args[0]
    assert 'CUSTOM_RECEIPT_1' in prompt[0]['content']
    assert 'State "delivered"' in prompt[-1]['content'] and 'recipient "<@U2>"' in prompt[-1]['content']
    assert 'DM "delivered"' in prompt[-1]['content'] and 'public "not requested"' in prompt[-1]['content']
    assert 'PRIVATE_AUTHORED_KUDOS' not in json.dumps(prompt)
    call = slack.chat_postMessage.call_args.kwargs
    assert call['channel'] == 'DU1' and call['blocks'][0]['text']['text'] == api.complete.return_value
    assert call['blocks'][1]['text']['text'] == 'delivered; DM: delivered; once-only XP result: 17 XP awarded'
    before = api.complete.call_count
    changed = default_template('delivery', 'member')
    changed['variations'][0]['system'] = 'CHANGED_AFTER_RETRY'
    composer.publish(member(10), changed, l.admin)
    w.outbox(job)
    assert api.complete.call_count == before
    assert slack.chat_postMessage.call_args.kwargs['blocks'][0]['text']['text'] == 'The delivery to <@U2> was successful.'
    assert l.participant(member(2))['xp'] == '17'


@pytest.mark.parametrize('dm,public,expected', [
    ('delivered', 'pending', 'partial'), ('delivered', 'failed', 'partial'),
    ('failed', 'pending', 'pending'), ('failed', 'failed', 'failed'),
    ('cancelled', 'cancelled', 'cancelled'), ('delivered', 'delivered', 'delivered'),
])
def test_legacy_receipt_statuses_remain_independent_and_body_private(joined, dm, public, expected):
    l, s, _, composer, api, slack = joined
    e = l.kudos(member(1), member(2), 'PRIVATE_KUDOS_BODY', key='states', public=True, expected_participation=True)
    legacy_evidence(s, e)
    e['deliveries'] = {'recipient': {'status': dm}, 'shared': {'status': public}}
    s.put('ledger_evidence', e)
    w = Worker(l, composer, slack)
    s.atomic(lambda tx: w.kudos_receipt(tx, {'payload': {'evidence': e['_id'], 'audience': 'shared'}}, {'status': public}))
    key = 'dm:receipt:kudos:states:shared:' + public
    facts = s.get('ledger_outbox', key)['payload']['facts']
    assert facts['delivery_status'] == expected and facts['dm_status'] == dm and facts['public_status'] == public
    assert facts['recipient_full_name'] == 'Maker2 Test' and facts['recipient_slack_id'] == 'U2'
    api.complete.side_effect = TimeoutError()
    w.outbox(claim(s, key))
    prompt = json.dumps(api.complete.call_args.args[0])
    assert 'PRIVATE_KUDOS_BODY' not in prompt
    assert '"xp_total"' not in prompt and '"current_rank"' not in prompt
    # Provider downtime leaves truthful facts and does not report success itself.
    call = slack.chat_postMessage.call_args.kwargs
    assert 'successful' not in call['blocks'][0]['text']['text']
    assert call['blocks'][1]['text']['text'] == facts['summary']
    assert expected + '; DM: ' + dm + '; Ledge Chat: ' + public in call['text']
    assert l.participant(member(2))['xp'] == '17'


def test_legacy_nonparticipant_receipts_are_varied_without_personal_progress(env):
    l, s, _, composer, api, slack = env
    e = l.kudos(member(1), member(2), 'Thanks', key='outside', expected_participation=False)
    legacy_evidence(s, e)
    w = Worker(l, composer, slack)
    w.outbox(claim(s, 'kudos:outside:recipient'))
    key = 'dm:receipt:kudos:outside:recipient:delivered'
    w.outbox(claim(s, key))
    prompt = json.dumps(api.complete.call_args.args[0][-1])
    assert '<@U2>' in prompt and '0 XP' in prompt
    assert s.get('ledger_outbox', key)['composed']['prompt_variation']
    assert slack.chat_postMessage.call_args.kwargs['channel'] == 'DU1'
    assert not s.select('ledger_participants')
    with patch.object(l.sources, 'slack_id', return_value='not-a-valid-id'):
        values = variables_for(delivery_facts(l, e), 'delivery', 'member')
    assert values['recipient_mention'] is None
