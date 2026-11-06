from copy import deepcopy
import hashlib

import pytest
import yaml
from pathlib import Path
from slack_sdk.errors import SlackApiError

from conftest import oid
from ledger.cli import outbox_filters
from ledger.domain import Denied
from ledger.progress import progress
from ledger.result_summaries import SummaryPending, collect, finish_action
from ledger.slack_app import SlackUI
from ledger.storage import enqueue
from ledger.views import progress_view
from ledger.worker import Worker
from test_worker import claim


def action(value='', trigger='next-step', user='U1'):
    return {'user': {'id': user}, 'trigger_id': trigger,
            'actions': [{'action_id': 'guidance_next_step', 'value': value}]}


def member(n=1):
    return str(oid(n))


def test_progress_button_queues_only_and_provider_failure_delivers_useful_next_step(joined):
    ledger, store, _, composer, api, slack = joined
    ledger.reconcile(member(), historical=True)
    view = progress_view(ledger, member())
    assert any(element.get('action_id') == 'guidance_next_step' for block in view['blocks']
               for element in block.get('elements', []))
    ui = SlackUI(ledger, composer)
    ui.action(action(), slack)
    ui.action(action(), slack)
    api.complete.assert_not_called()
    assert len(store.select('ledger_outbox', {'kind': 'guidance'})) == 1
    api.complete.side_effect = TimeoutError()
    job = claim(store, 'guidance:next-step')
    worker = Worker(ledger, composer, slack)
    worker.outbox(job)
    assert slack.chat_postMessage.call_args.kwargs['text'] == progress(ledger, member())['suggestions'][0]
    assert api.complete.call_args.kwargs == {'deadline': 10}
    saved = store.get('ledger_outbox', job['_id'])
    assert saved['prompt_selection']['generation_profile']['max_tokens'] == 256
    assert 'next_rank' not in saved['guidance_facts']
    worker.outbox(job)
    assert api.complete.call_count == slack.chat_postMessage.call_count == 1


def test_result_button_guidance_is_threaded_self_only_and_retry_text_stable(joined):
    ledger, store, _, composer, api, slack = joined
    owner_id = collect(ledger, 'verified-result', member(), 'rank_up', {'rank': 'Newbie'}, 'rank-event')
    finish_action(ledger, 'verified-result')
    owner = store.get('ledger_evidence', owner_id)
    owner.update(channel='DU1', ts='parent-result')
    store.put('ledger_evidence', owner)
    ui = SlackUI(ledger, composer)
    with pytest.raises(Denied):
        ui.action(action(owner_id, user='U2'), slack)
    ui.action(action(owner_id), slack)
    api.complete.return_value = 'Explore a safe next clearance with /ledger-skills.'
    worker = Worker(ledger, composer, slack)
    slack.chat_postMessage.side_effect = TimeoutError()
    job = claim(store, 'guidance:next-step')
    with pytest.raises(TimeoutError):
        worker.outbox(job)
    saved = deepcopy(store.get('ledger_outbox', job['_id'])['composed'])
    api.complete.side_effect = AssertionError('Retries must reuse narration')
    slack.chat_postMessage.side_effect = None
    worker.outbox(job)
    assert store.get('ledger_outbox', job['_id'])['composed'] == saved
    assert slack.chat_postMessage.call_args.kwargs['thread_ts'] == 'parent-result'
    assert slack.chat_postMessage.call_args.kwargs['channel'] == 'DU1'


def test_guidance_cancelled_after_opt_out_rejoin_even_if_job_is_reclaimed(joined):
    ledger, store, _, composer, api, slack = joined
    SlackUI(ledger, composer).action(action(), slack)
    ledger.leave(member())
    ledger.join(member())
    with pytest.raises(Denied):
        Worker(ledger, composer, slack).outbox(claim(store, 'guidance:next-step'))
    api.complete.assert_not_called()
    slack.chat_postMessage.assert_not_called()


def test_opt_out_during_guidance_generation_prevents_delivery(joined):
    ledger, store, _, composer, api, slack = joined
    SlackUI(ledger, composer).action(action(), slack)
    def generate(*args, **kwargs):
        ledger.leave(member())
        return 'Try /ledger-skills.'
    api.complete.side_effect = generate
    with pytest.raises(Denied):
        Worker(ledger, composer, slack).outbox(claim(store, 'guidance:next-step'))
    slack.chat_postMessage.assert_not_called()


@pytest.mark.parametrize('threaded', [False, True])
def test_guidance_identity_change_during_generation_cannot_disclose_to_old_dm(joined, threaded):
    ledger, store, source, composer, api, slack = joined
    owner_id = None
    if threaded:
        owner_id = collect(ledger, 'result', member(), 'rank_up', {'rank': 'Newbie'}, 'rank')
        owner = store.get('ledger_evidence', owner_id)
        owner.update(channel='DU1', ts='parent')
        store.put('ledger_evidence', owner)
    SlackUI(ledger, composer).action(action(owner_id or ''), slack)
    def generate(*args, **kwargs):
        source.data['slack_users'][0]['slack_id'] = 'U99'
        return 'Try /ledger-skills.'
    api.complete.side_effect = generate
    with pytest.raises(Denied):
        Worker(ledger, composer, slack).outbox(claim(store, 'guidance:next-step'))
    slack.chat_postMessage.assert_not_called()


def test_guidance_identity_change_while_opening_dm_prevents_post(joined):
    ledger, store, source, composer, _, slack = joined
    SlackUI(ledger, composer).action(action(), slack)
    def open_dm(users):
        source.data['slack_users'][0]['slack_id'] = 'U99'
        return {'channel': {'id': 'DU1'}}
    slack.conversations_open.side_effect = open_dm
    with pytest.raises(Denied):
        Worker(ledger, composer, slack).outbox(claim(store, 'guidance:next-step'))
    slack.chat_postMessage.assert_not_called()


def test_unparented_new_art_cannot_revive_after_opt_out_rejoin(joined):
    ledger, store, _, composer, _, slack = joined
    generation = ledger.participant(member())['consent_generation']
    enqueue(store, 'ledger_outbox', 'old-art', 'rank_art', {'member_id': member(), 'slot': 1,
            'consent_generation': generation})
    ledger.leave(member())
    ledger.join(member())
    with pytest.raises(Denied):
        Worker(ledger, composer, slack).outbox(claim(store, 'old-art'))
    slack.files_upload_v2.assert_not_called()


def test_missing_cached_rank_icon_is_reuploaded(joined):
    ledger, store, _, composer, _, slack = joined
    image = Path(__file__).parents[1] / 'ledger' / 'assets' / 'rank-1.png'
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    store.put('ledger_files', {'_id': 'rank_icon:1', 'kind': 'rank_icon', 'slot': 1,
        'filename': 'rank-1.png', 'sha256': digest, 'file_id': 'F_DELETED'})
    missing = type('SlackResponse', (), {'status_code': 200,
        'get': lambda self, key: 'file_not_found' if key == 'error' else None})()
    slack.files_info.side_effect = SlackApiError('missing file', missing)
    enqueue(store, 'ledger_outbox', 'missing-rank-file', 'rank_art',
        {'member_id': member(), 'slot': 1, 'consent_generation': ledger.participant(member())['consent_generation']})
    job = claim(store, 'missing-rank-file')

    Worker(ledger, composer, slack).outbox(job)

    slack.files_upload_v2.assert_called_once()
    assert store.get('ledger_files', 'rank_icon:1')['file_id'] == 'F_RANK_ICON'


def test_consolidated_rank_art_waits_without_attempts_then_uses_parent_thread(joined):
    ledger, store, _, composer, _, slack = joined
    owner_id = collect(ledger, 'rank-result', member(), 'rank_up', {'rank': 'Newbie'}, 'rank-event')
    owner = store.get('ledger_evidence', owner_id)
    enqueue(store, 'ledger_outbox', 'threaded-art', 'rank_art', {'member_id': member(), 'slot': 1,
            'summary_id': owner_id, 'consent_generation': owner['consent_generation']})
    worker = Worker(ledger, composer, slack)
    with pytest.raises(SummaryPending):
        worker.outbox(claim(store, 'threaded-art'))
    for job in store.select('ledger_outbox'):
        if job['_id'] != 'threaded-art':
            job['status'] = 'done'
            store.put('ledger_outbox', job)
    job = store.get('ledger_outbox', 'threaded-art')
    job['status'] = 'pending'
    from ledger.storage import now
    job['available_at'] = now()
    store.put('ledger_outbox', job)
    assert worker.step('ledger_outbox')
    assert store.get('ledger_outbox', 'threaded-art')['attempts'] == 1  # manual claim above consumed one
    owner.update(channel='DU1', ts='result-parent')
    store.put('ledger_evidence', owner)
    worker.outbox(claim(store, 'threaded-art'))
    assert slack.files_upload_v2.call_args.kwargs['thread_ts'] == 'result-parent'


def test_worker_lanes_are_disjoint_and_compose_starts_them_without_inference_dependency():
    groups = [set(outbox_filters(name)['kinds']) for name in ('channels', 'results', 'interactive')]
    assert all(not left & right for i, left in enumerate(groups) for right in groups[i + 1:])
    assert set(outbox_filters('outbox')['exclude']) == set.union(*groups)
    config = yaml.safe_load((Path(__file__).parents[1] / 'compose.yaml').read_text())
    for name in ('results', 'interactive'):
        service = config['services']['ledger-' + name]
        assert service['command'][-1] == name
        assert not service.get('depends_on')
        assert service['environment']['LEDGER_LLM_BASE_URL'].endswith('http://ledger-ai:8000/v1}')
