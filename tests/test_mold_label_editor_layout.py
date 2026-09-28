from copy import deepcopy
import pytest
from app.services import mold_label_layout as layouts


def test_v8_first_row_and_qr_are_readable_without_overlap():
    layout = layouts.normalize_layout(layouts.default_layout())
    elements = {e['id']: e for e in layout['elements']}
    assert layout['catalog_version'] == 'p1-119-v1'
    location, cutting = elements['rack_location'], elements['cutting_mode']
    assert location['y_mm'] == cutting['y_mm']
    assert location['x_mm'] + location['width_mm'] < cutting['x_mm']
    assert cutting['text_align'] == 'right'
    qr = elements['mold_qr']
    assert qr['width_mm'] == qr['height_mm'] == 15
    for key in ('product_name', 'customer_inventory_code'):
        item = elements[key]
        assert item['x_mm'] + item['width_mm'] < qr['x_mm']


def test_old_print_snapshot_stays_original_but_new_jobs_use_v8():
    original = layouts._default_layout_v7()
    snapshot = deepcopy(original)
    assert layouts._normalize_snapshot_layout(original) == original
    current = layouts._upgrade_to_current_catalog(original)
    assert original == snapshot
    assert current == layouts.default_layout()
    assert layouts._normalize_snapshot_layout(original)['elements'][6]['width_mm'] == 14.2


@pytest.mark.parametrize('mutation', ['overlap', 'qr_size', 'old_catalog'])
def test_new_writes_reject_unsafe_or_stale_layouts(mutation):
    layout = layouts.default_layout()
    elements = {e['id']: e for e in layout['elements']}
    if mutation == 'overlap':
        elements['customer_inventory_code']['width_mm'] = 70
    elif mutation == 'qr_size':
        elements['mold_qr']['width_mm'] = 14.2
    else:
        layout = layouts._default_layout_v7()
    with pytest.raises(layouts.MoldLabelLayoutError):
        layouts.normalize_layout(layout)
