import io
from pathlib import Path

from PIL import Image

from conftest import oid
from ledger.skills import render_tree, skill_summary
from ledger.worker import Worker


def test_skill_tree_matches_prerequisites_and_accessible_text(joined):
    l, _, src, *_ = joined
    src.data['tool_checkouts'].append({'_id': oid(501), 'tool_id': oid(311), 'member_id': oid(1)})
    summary = skill_summary(l, str(oid(1)), 'Shop1')
    assert len(summary['nodes']) == 4
    assert summary['nodes'][0]['state'] == 'Cleared'
    assert summary['nodes'][1]['state'] == 'Next step'
    assert summary['nodes'][1]['prerequisites'] == [str(oid(311))]
    assert 'prerequisites: Tool1-1' in summary['text']
    image = Image.open(render_tree(summary))
    assert image.format == 'PNG' and image.width == 1100 and image.height >= 388


def test_six_supplied_rank_images_are_packaged_and_readable():
    root = Path(__file__).parents[1] / 'ledger' / 'assets'
    for slot in range(1, 7):
        with Image.open(root / f'rank-{slot}.png') as image:
            assert image.format == 'PNG' and image.width >= 1024
            image.verify()


def test_skill_tree_image_is_cached_by_member_and_text_checksum(joined):
    ledger, store, source, composer, _, slack = joined
    source.data['tool_checkouts'].append({'_id': oid(501), 'tool_id': oid(311), 'member_id': oid(1)})
    slack.files_info.return_value = {'file': {'id': 'F_RANK_ICON', 'is_deleted': False}}
    worker = Worker(ledger, composer, slack)
    member_id = str(oid(1))

    worker.command(member_id, '/ledger-skills Shop1', 'skill-tree-first')
    saved = store.get('ledger_files', f'skill_tree:{member_id}')
    assert saved['file_id'] == 'F_RANK_ICON'
    assert len(saved['text_sha256']) == 64 and saved.get('cached_at')
    png_uploads = [call for call in slack.files_upload_v2.call_args_list
                   if call.kwargs.get('filename') == 'ledger-skill-tree.png']
    assert len(png_uploads) == 1

    worker.command(member_id, '/ledger-skills Shop1', 'skill-tree-cached')
    png_uploads = [call for call in slack.files_upload_v2.call_args_list
                   if call.kwargs.get('filename') == 'ledger-skill-tree.png']
    assert len(png_uploads) == 1
    assert any(call.kwargs.get('blocks', [{}])[0].get('slack_file', {}).get('id') == 'F_RANK_ICON'
               for call in slack.chat_postMessage.call_args_list)

    slack.files_info.return_value = {'file': {'id': 'F_REPLACED', 'is_deleted': False}}
    worker.command(member_id, '/ledger-skills Shop1', 'skill-tree-reupload')
    png_uploads = [call for call in slack.files_upload_v2.call_args_list
                   if call.kwargs.get('filename') == 'ledger-skill-tree.png']
    assert len(png_uploads) == 2
