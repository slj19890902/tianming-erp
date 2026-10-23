"""Owner-confirmed aliases change names, never infer physical structure."""
import pytest
from fastapi.testclient import TestClient

from tests.test_master_data_versioning_p4_writers import writer_app
from tests.test_product_unknown_structure import seed, body, save, URL
from tests.test_product_unknown_structure_frontend import runtime
from tests.test_p1_46_common_box_final_layout import INDEX, _run_node
from app.models.product import Product
from app.services.box_type_rules import BOX_TYPE_RULES, canonical_box_style

A1 = 'A1/0201 普通开槽箱'
ALIASES = ['A1', '0201', 'A1/0201', 'A1 型纸箱', '普通开槽箱',
           'WCX1 五层箱 1', 'WCZX 五层粘箱', 'SCX 单瓦箱', 'QCX 七层纸箱',
           '001思展钉箱', '008 外箱无钉', '006 华元外箱', 'WZX 外纸箱',
           'NZX 内纸箱', 'THWX 腾华外箱', '007 内盒无钉', '004 豪迪纸箱',
           '005 粘合成型', 'THYW 天华压外', 'A1 /0201 普通开槽箱']


@pytest.mark.parametrize('alias', ALIASES)
def test_owner_list_has_one_standard_name(alias):
    assert canonical_box_style(alias) == A1


def test_every_registered_alias_keeps_its_own_standard_type():
    for rule in BOX_TYPE_RULES:
        for alias in rule.aliases:
            assert canonical_box_style(alias) == rule.display_name
    assert canonical_box_style('未知客户箱型') == '未知客户箱型'
    assert canonical_box_style(None) is None


def test_editor_shows_and_submits_only_standard_name_without_filling_unknowns(tmp_path):
    _run_node(tmp_path, runtime() + """
const form=legacy();
assert.equal(form.box_style,'A1/0201 普通开槽箱');
const payload=vm.buildProductWritePayload();
assert.equal(payload.box_style,'A1/0201 普通开槽箱');
assert.deepEqual(payload.structure_edit_fields,[]);
for(const key of ['splice_mode','pieces_per_box','flap_mm','production_process']) {
 assert.equal(form[key],null,key); assert.equal(payload[key],null,key);
}
""")
    editor = INDEX[INDEX.index('<label>箱型</label>', INDEX.index('modal.type === \'product\'')):]
    editor = editor[:editor.index('</select>') + 200]
    assert '兼容名称' not in editor
    assert 'productCompatibleBoxStyleOptions' not in editor


@pytest.mark.parametrize('mask', [None, [], ['box_style']])
def test_read_save_and_reread_use_standard_name_but_structure_stays_unknown(writer_app, mask):
    pid, _, _ = seed(writer_app)
    with TestClient(writer_app) as client:
        original = body(client, pid)
        assert original['box_style'] == A1
        payload = body(client, pid, box_style='WC 五层钉箱', production_notes='只改备注')
        if mask is None:
            payload.pop('structure_edit_fields')
        else:
            payload['structure_edit_fields'] = mask
        updated, _ = save(client, pid, payload)
        assert updated['box_style'] == A1
        assert body(client, pid)['box_style'] == A1
        assert all(updated[k] is None for k in ['splice_mode','pieces_per_box','flap_mm','production_process'])
    with writer_app.state.session_factory() as db:
        assert db.get(Product, pid).box_style == A1
