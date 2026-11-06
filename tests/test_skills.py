import io
from pathlib import Path

from PIL import Image

from conftest import oid
from ledger.skills import render_tree, skill_summary


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
