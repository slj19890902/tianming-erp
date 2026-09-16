import json
from pathlib import Path
import pytest
from test_processed_sheet_matching import setup, candidates
from test_semi_finished_lot_eligibility import eligibility_db
from app.api.warehouse import _semi_candidate_dict


@pytest.mark.parametrize('processing', ['raw', 'cut', 'die_cut', 'creased', 'printed', None])
def test_candidate_preserves_recorded_name_processing_and_stock(eligibility_db, processing):
    db, data = eligibility_db
    product, lot, profile, facts = setup(db, data, length=800, approved=True)
    row = candidates(db, product)[0]
    lot.semi_finished_detail.internal_name = '现场录入货物名称'
    before = (lot.quantity_available, lot.quantity_reserved, lot.version)
    if processing is None:
        db.delete(profile)
    else:
        facts['processing'] = processing
        profile.data_json = json.dumps(facts)
    db.flush()
    result = _semi_candidate_dict(row)
    assert result['internal_name'] == '现场录入货物名称'
    assert result['processing'] == processing
    assert result['board_length_mm'] == 800
    assert result['board_width_mm'] == 600
    assert before == (lot.quantity_available, lot.quantity_reserved, lot.version)


def test_both_deduction_tables_have_same_requested_column_order():
    html = Path('static/index.html').read_text(encoding='utf-8')
    expected = ['货物名称', '纸板长（mm）', '纸板宽（mm）', '楞型', '材质', '加工类型', '数量', '位置', '采用 / 不采用']
    import re
    for marker in ['class="semi-stock-table"', 'class="line-items pdf-material-table"']:
        table = html.split(marker, 1)[1].split('</table>', 1)[0]
        headers = re.findall(r'<th(?:\s[^>]*)?>(.*?)</th>', table.split('</thead>')[0])
        assert headers == expected
        assert 'internal_name' in table
        assert 'processing' in table
        assert 'lot_number' not in table
