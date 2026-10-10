"""Fictional identities only; real planning/freezing/assembly engines."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from app.services.composite_bom_execution import CompositeBOMExecutionError
from app.services.joint_cutting_bom_plan import (
    freeze_joint_bom, load_joint_bom, plan_joint_bom, plan_joint_batch, plan_joint_assembly,
)
from app.services.multilevel_bom_plan import (
    BomEdge, BomModes, FrozenBom, MaterialRoute, ProductNode,
)
from app.services.multilevel_bom_snapshot import dump_graph
from tests.test_joint_cutting_contract import group


def compiled_recipe():
    graph = FrozenBom(100, 20, (
        ProductNode(100, 20, 9, '虚构粘合成品', '套', 'assembled'),
        *(ProductNode(pid, 20, 2, name, '片', 'manufactured',
                      (MaterialRoute('whole', 1, per_input * 6),))
          for pid, name, per_input in [(101, 'A片', 3), (102, 'B片', 2), (103, 'C片', 2), (104, '三角片', 1)]),
    ), tuple(BomEdge(100, pid, count, 'assembly') for pid, count in [(101, 3), (102, 2), (103, 2), (104, 1)]),
        BomModes('expand_children', 'assembled', 'parent'))
    snapshots = []
    for node in graph.nodes:
        per_input = node.routes[0].pieces_per_sheet // 6 if node.routes else 1
        per_set = {100: 1, 101: 3, 102: 2, 103: 2, 104: 1}[node.product_id]
        snapshots.append(SimpleNamespace(
            component_product_id=node.product_id, component_product_version=node.version,
            parent_product_version=9, is_die_cut=bool(node.routes),
            snapshot_schema_version=5, order_set_quantity=100, quantity_per_set=per_set,
            required_piece_quantity=100 * per_set, snapshot_component_product_name=node.name,
            snapshot_component_box_style='模切内盒', snapshot_component_pieces_per_box=1,
            snapshot_component_splice_mode=None, mold_max_yield_per_sheet=None,
            snapshot_component_default_cutting_mode='一开一',
            snapshot_mold_tool_id=41 if node.product_id == 101 else 42,
            spare_sheet_quantity=0,
            snapshot_component_report_length_mm=Decimal('375.00'),
            snapshot_component_report_width_mm=Decimal('226.00'),
            sheet_cutting_settings_snapshot={'schema_version': 2, 'whole': {
                'length_parts': 2, 'width_parts': 3, 'mold_count': per_input, 'is_die_cut': True}},
            snapshot_component_material_id=7, snapshot_component_material='TEST-BOARD',
            snapshot_component_supplier_name='虚构纸板厂', snapshot_component_layer_count=3,
            snapshot_component_flute_type='B', snapshot_component_crease_type='none',
            snapshot_component_crease_left_mm=None, snapshot_component_crease_middle_mm=None,
            snapshot_component_crease_right_mm=None, snapshot_component_report_notes=None,
        ))
    a = group(factor=(2, 3), outputs=[{'product_id': 101, 'quantity_per_input': 3}])
    a.update(key='tool-a', mold_tool_id=41)
    return SimpleNamespace(graph=graph, snapshots=tuple(snapshots)), [a, group(factor=(2, 3))]


def frozen_recipe():
    compiled, groups = compiled_recipe()
    return freeze_joint_bom(compiled, groups)


def test_save_readback_keeps_dimensions_and_real_bom_and_does_not_recalculate_master(tmp_path):
    compiled, groups = compiled_recipe()
    before = dump_graph(compiled.graph)
    document = freeze_joint_bom(compiled, groups)
    path = tmp_path / 'joint-cut-bom.json'
    path.write_text(document, encoding='utf-8')
    compiled.snapshots[2].snapshot_component_report_length_mm = 999
    groups[1]['outputs'][0]['quantity_per_input'] = 9
    saved = path.read_text(encoding='utf-8')
    graph, bindings = load_joint_bom(saved)
    assert dump_graph(graph) == before
    assert bindings[0]['frozen_group']['group']['theory_length_mm'] == '375'
    result = plan_joint_bom(saved, 100)
    assert result['picking'] == [(100, 100)]
    assert result['ordinary_materials'] == []
    assert sum(op['theory_input_sheets'] for op in result['operations']) == 200
    assert sum(op['purchase_sheets'] for op in result['operations']) == 34
    assert sum(op['uncut_theory_sheets'] for op in result['operations']) == 4
    assert {o['product_id']: o['planned_output'] for op in result['operations'] for o in op['outputs']} == {
        101: 300, 102: 200, 103: 200, 104: 100}


def test_existing_finished_and_component_stock_each_credit_once():
    result = plan_joint_bom(frozen_recipe(), 10, eligible_stock={100: 2, 101: 3, 102: 16, 103: 14})
    rows = {op['group_key']: op for op in result['operations']}
    assert rows['tool-a']['theory_input_sheets'] == 7
    assert rows['mixed-tool-b']['theory_input_sheets'] == 8
    assert {o['product_id']: o['surplus_output'] for o in rows['mixed-tool-b']['outputs']} == {
        102: 16, 103: 14, 104: 0}


def test_physical_piece_stock_is_not_credited_twice_as_finished_stock():
    result = plan_joint_bom(frozen_recipe(), 1, eligible_stock={101: 1},
                            eligible_pieces={(101, 'whole'): 2})
    assert next(op for op in result['operations'] if op['group_key'] == 'tool-a')['theory_input_sheets'] == 0


@pytest.mark.parametrize('case', ['stale_child', 'stale_parent', 'wrong_mold', 'wrong_customer',
                                 'wrong_dimensions', 'wrong_split', 'wrong_yield', 'non_die_cut',
                                 'missing_setting', 'hidden_spares', 'different_material',
                                 'different_flute', 'different_crease', 'unknown_material',
                                 'missing_snapshot', 'duplicate_snapshot'])
def test_freezing_rejects_inconsistent_facts(case):
    compiled, groups = compiled_recipe()
    row = compiled.snapshots[2]
    if case == 'stale_child': row.component_product_version += 1
    elif case == 'stale_parent': row.parent_product_version += 1
    elif case == 'wrong_mold': row.snapshot_mold_tool_id = 99
    elif case == 'wrong_customer': groups[1]['customer_id'] = 99
    elif case == 'wrong_dimensions': row.snapshot_component_report_width_mm = 227
    elif case == 'wrong_split': row.sheet_cutting_settings_snapshot['whole']['length_parts'] = 1
    elif case == 'wrong_yield': row.sheet_cutting_settings_snapshot['whole']['mold_count'] = 5
    elif case == 'non_die_cut': row.is_die_cut = False
    elif case == 'missing_setting': row.sheet_cutting_settings_snapshot = None
    elif case == 'hidden_spares': row.spare_sheet_quantity = 1
    elif case == 'different_material': row.snapshot_component_material_id = 8
    elif case == 'different_flute': row.snapshot_component_flute_type = 'A'
    elif case == 'different_crease': row.snapshot_component_crease_left_mm = 20
    elif case == 'unknown_material': row.snapshot_component_material_id = None
    elif case == 'missing_snapshot': compiled.snapshots = compiled.snapshots[:-1]
    else: compiled.snapshots += (row,)
    with pytest.raises(CompositeBOMExecutionError):
        freeze_joint_bom(compiled, groups)


@pytest.mark.parametrize('case', ['not_a_piece', 'different_route_yield', 'multiple_routes', 'same_output_twice'])
def test_freezing_rejects_ambiguous_output_identity(case):
    compiled, groups = compiled_recipe()
    nodes = list(compiled.graph.nodes)
    if case == 'not_a_piece': nodes[2] = replace(nodes[2], unit='只')
    elif case == 'different_route_yield': nodes[2] = replace(nodes[2], routes=(MaterialRoute('whole', 1, 18),))
    elif case == 'multiple_routes': nodes[2] = replace(nodes[2], routes=(MaterialRoute('cover', 1, 12), MaterialRoute('base', 1, 12)))
    else:
        another = deepcopy(groups[1])
        another['key'] = 'duplicate-member'
        groups.append(another)
    compiled.graph = replace(compiled.graph, nodes=tuple(nodes))
    with pytest.raises(CompositeBOMExecutionError):
        freeze_joint_bom(compiled, groups)


def test_saved_document_rejects_tamper_unknown_fields_and_duplicate_json_keys():
    document = frozen_recipe()
    data = json.loads(document)
    data['bindings'][0]['output_versions'][0]['version'] += 1
    with pytest.raises(CompositeBOMExecutionError): load_joint_bom(json.dumps(data))
    data = json.loads(document)
    data['extra'] = 'ignore me'
    with pytest.raises(CompositeBOMExecutionError): load_joint_bom(json.dumps(data))
    with pytest.raises(CompositeBOMExecutionError):
        load_joint_bom(document.replace('"kind":', '"kind":"duplicate","kind":', 1))


def test_batch_counts_loss_by_shape_and_assembly_consumes_exact_recipe():
    document = frozen_recipe()
    a = plan_joint_batch(document, 'tool-a', input_sheets=10, good_outputs={101: 30})
    b = plan_joint_batch(document, 'mixed-tool-b', input_sheets=10, good_outputs={102: 19, 103: 20, 104: 8})
    assert a['input_sheets'] + b['input_sheets'] == 20
    assert {r['product_id']: r['rejected_output'] for r in b['outputs']} == {102: 1, 103: 0, 104: 2}
    balances = {r['product_id']: r['good_output'] for batch in (a, b) for r in batch['outputs']}
    assembly = plan_joint_assembly(document, 10, eligible_stock=balances)
    assert assembly.steps[0].produced_units == 8
    assert dict(assembly.steps[0].consumed) == {101: 24, 102: 16, 103: 16, 104: 8}
    assert dict(assembly.remaining_stock) == {100: 8, 101: 6, 102: 3, 103: 4, 104: 0}
    second = plan_joint_assembly(document, 10, eligible_stock=dict(assembly.remaining_stock))
    assert second.steps == ()


def test_all_scrap_cannot_create_finished_stock():
    document = frozen_recipe()
    result = plan_joint_batch(document, 'mixed-tool-b', input_sheets=1, good_outputs={102: 0, 103: 0, 104: 0})
    assert sum(r['rejected_output'] for r in result['outputs']) == 5
    assert plan_joint_assembly(document, 1, eligible_stock={101: 3}).steps == ()


@pytest.mark.parametrize('outputs', [{102: 2, 103: 2}, {102: 2, 103: 2, 104: 2},
                                    {102: 2, 103: 2, 104: 1, 999: 1},
                                    {102: -1, 103: 2, 104: 1}])
def test_batch_cannot_omit_convert_or_overproduce_an_output(outputs):
    with pytest.raises(CompositeBOMExecutionError):
        plan_joint_batch(frozen_recipe(), 'mixed-tool-b', input_sheets=1, good_outputs=outputs)


def test_compiler_database_roundtrip_uses_frozen_material_after_master_changes(context):
    from app.models.product import Product
    from app.models.material import Material
    from app.models.mold_tool import MoldTool, MoldToolCustomer
    from app.services.composite_bom import replace_product_bom
    from app.services.multilevel_bom_orders import freeze_master_order_bom, read_compiled_order_bom
    db, actor, item, _ = context
    material = Material(code='JOINT-TEST', layer_count=3, flute_type='B', supplier_name='虚构供应商')
    db.add(material)
    db.add(Product(id=5, customer_id=136, product_code='JOINT-TRI',
                   customer_material_code='JOINT-TRI', product_name='三角片', unit='片'))
    molds = [MoldTool(mold_code='JOINT-' + name, mold_name=name, label_name=name,
                      identity_status='frozen', rack_location='UAT-JOINT-R1')
             for name in ('A模', 'B混排模')]
    db.add_all(molds)
    db.flush()
    db.add_all(MoldToolCustomer(mold_tool_id=m.id, customer_id=136) for m in molds)
    rows = []
    for pid, per_set in [(2, 3), (3, 2), (4, 2), (5, 1)]:
        child = db.get(Product, pid)
        mold = molds[0 if pid == 2 else 1]
        child.unit = '片'
        child.box_category = 'die_cut'
        child.box_style = '模切内盒'
        child.production_process = '无需结合,模切'
        child.material_id = material.id
        child.flute_type = 'B'
        child.pieces_per_box = 1
        child.report_length_mm, child.report_width_mm = 375, 226
        child.mold_tool_id = mold.id
        child.sheet_cutting_settings = {'schema_version': 2, 'whole': {
            'length_parts': 2, 'width_parts': 3, 'mold_count': per_set, 'is_die_cut': True}}
        rows.append({'component_product_id': pid, 'quantity_per_set': per_set,
                     'inventory_relation': 'assembly', 'is_die_cut': True, 'mold_tool_id': mold.id})
    replace_product_bom(db, parent_product_id=1, inventory_mode='assembled',
                        components=rows, expected_version=db.get(Product, 1).version, user=actor)
    db.commit()
    compiled = freeze_master_order_bom(db, order_item_id=item.id, actor=actor)
    db.commit()
    _, groups = compiled_recipe()
    groups[0]['outputs'] = [{'product_id': 2, 'quantity_per_input': 3}]
    groups[1]['outputs'] = [{'product_id': pid, 'quantity_per_input': per} for pid, per in [(3, 2), (4, 2), (5, 1)]]
    for g, m in zip(groups, molds):
        g.update(customer_id=136, mold_tool_id=m.id)
    document = freeze_joint_bom(compiled, groups)
    db.get(Product, 3).report_width_mm = 999
    db.get(Product, 3).version += 1
    material.code = 'LATER-MASTER'
    db.commit()
    reread = read_compiled_order_bom(db, item.id)
    assert freeze_joint_bom(reread, groups) == document
    assert [op['theory_input_sheets'] for op in plan_joint_bom(document, 100)['operations']] == [100, 100]
    assert all(op['paper_identity']['material'] == 'JOINT-TEST' for op in plan_joint_bom(document, 1)['operations'])


# Shared fixture creates a new fictional database inside the owned UAT task.
from tests.test_multilevel_bom_orders import context
