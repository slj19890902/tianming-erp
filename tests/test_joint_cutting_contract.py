"""One physical input may yield several non-interchangeable BOM components."""
from copy import deepcopy

import pytest

from app.services.composite_bom_execution import (
    CompositeBOMExecutionError, calculate_effective_component_demands,
    calculate_requisitions,
)
from app.services.joint_cutting_contract import (
    freeze_cutting_group, load_cutting_group, plan_cutting_group,
)


def group(*, factor=(1, 1), outputs=None):
    return {
        'key': 'mixed-tool-b', 'customer_id': 20, 'mold_tool_id': 42,
        'theory_length_mm': '375', 'theory_width_mm': '226',
        'length_parts': factor[0], 'width_parts': factor[1],
        'outputs': outputs or [
            {'product_id': 102, 'quantity_per_input': 2},
            {'product_id': 103, 'quantity_per_input': 2},
            {'product_id': 104, 'quantity_per_input': 1},
        ],
        'spare_supplier_sheets': 0,
    }


def components():
    return [dict(component_product_id=pid, component_name=name, customer_id=20,
                 quantity_per_set=qty, is_die_cut=True, mold_tool_id=mold,
                 mold_max_yield_per_sheet=qty, spare_sheet_quantity=0)
            for pid, name, qty, mold in [(101, 'A片', 3, 41), (102, 'B片', 2, 42),
                                          (103, 'C片', 2, 42), (104, '三角片', 1, 42)]]


def test_confirmed_recipe_has_two_inputs_not_four_and_preserves_parts():
    a = group(outputs=[{'product_id': 101, 'quantity_per_input': 3}])
    a.update(key='tool-a', mold_tool_id=41)
    demands = calculate_effective_component_demands(1, components())
    before = deepcopy(demands)
    rows = calculate_requisitions(demands, joint_cut_groups=[a, group()])
    assert sum(row['purchase_sheets'] for row in rows) == 2
    assert {o['product_id']: o['planned_output'] for r in rows for o in r['outputs']} == {
        101: 3, 102: 2, 103: 2, 104: 1}
    assert demands == before


def test_supplier_split_is_applied_once_and_uncut_remainder_stays_material():
    result = plan_cutting_group(group(factor=(2, 3)), {102: 14, 103: 14, 104: 7})
    assert result['theory_input_sheets'] == 7
    assert result['purchase_sheets'] == 2
    assert result['supplier_length_mm'] == '750'
    assert result['supplier_width_mm'] == '678'
    assert result['uncut_theory_sheets'] == 5
    assert [r['planned_output'] for r in result['outputs']] == [14, 14, 7]


def test_shortest_component_drives_joint_input_and_surplus_stays_distinct():
    result = plan_cutting_group(group(), {102: 20, 103: 20, 104: 10},
                                eligible_output_stock={102: 20, 103: 18, 104: 0})
    assert result['theory_input_sheets'] == 10
    by_id = {r['product_id']: r for r in result['outputs']}
    assert [by_id[i]['surplus_output'] for i in (102, 103, 104)] == [20, 18, 0]
    assert by_id[102]['credited_output'] == 20
    assert by_id[102]['unmet_output'] == 0


def test_zero_demand_does_not_buy_spares_or_make_stock():
    value = group()
    value['spare_supplier_sheets'] = 3
    result = plan_cutting_group(value, {102: 0, 103: 0, 104: 0})
    assert result['purchase_sheets'] == result['theory_input_sheets'] == 0


def test_spares_only_added_once_and_not_counted_as_finished_output():
    value = group(factor=(2, 3))
    value['spare_supplier_sheets'] = 2
    result = plan_cutting_group(value, {102: 2, 103: 2, 104: 1})
    assert result['purchase_sheets'] == 3
    assert result['uncut_theory_sheets'] == 17
    assert [r['planned_output'] for r in result['outputs']] == [2, 2, 1]


def test_snapshot_is_canonical_and_detects_tampering():
    one = group()
    two = deepcopy(one)
    two['outputs'].reverse()
    frozen = freeze_cutting_group(one)
    assert frozen == freeze_cutting_group(two)
    assert load_cutting_group(frozen)['outputs'] == one['outputs']
    broken = deepcopy(frozen)
    broken['group']['outputs'][0]['quantity_per_input'] = 9
    with pytest.raises(CompositeBOMExecutionError, match='校验'):
        load_cutting_group(broken)


@pytest.mark.parametrize('field,value', [
    ('length_parts', 0), ('length_parts', True), ('width_parts', 1.5),
    ('mold_tool_id', 0), ('customer_id', None), ('spare_supplier_sheets', -1),
    ('theory_length_mm', 'NaN'), ('theory_width_mm', '0'),
    ('theory_length_mm', '1.001'), ('key', ''),
])
def test_bad_group_contract_is_rejected(field, value):
    data = group()
    data[field] = value
    with pytest.raises(CompositeBOMExecutionError):
        freeze_cutting_group(data)


@pytest.mark.parametrize('mutation', ['duplicate_output', 'empty_output', 'zero_output', 'unknown_field'])
def test_ambiguous_or_partial_group_is_rejected(mutation):
    data = group()
    if mutation == 'duplicate_output':
        data['outputs'].append(deepcopy(data['outputs'][0]))
    elif mutation == 'empty_output':
        data['outputs'] = []
    elif mutation == 'zero_output':
        data['outputs'][0]['quantity_per_input'] = 0
    else:
        data['mold_count'] = 5
    with pytest.raises(CompositeBOMExecutionError):
        freeze_cutting_group(data)


@pytest.mark.parametrize('stock', [{102: -1}, {102: True}, {999: 1}])
def test_stock_is_validated_and_unknown_identity_cannot_credit_another_output(stock):
    with pytest.raises(CompositeBOMExecutionError):
        plan_cutting_group(group(), {102: 2, 103: 2, 104: 1}, eligible_output_stock=stock)


@pytest.mark.parametrize('case', ['wrong_mold', 'wrong_customer', 'missing_member',
                                 'duplicate_group', 'hidden_spares', 'override_yield',
                                 'different_snapshot'])
def test_adapter_refuses_unsafe_grouping(case):
    values = components()
    groups = [group()]
    if case == 'wrong_mold':
        values[1]['mold_tool_id'] = 99
    elif case == 'wrong_customer':
        values[1]['customer_id'] = 21
    elif case == 'missing_member':
        values.pop()
    elif case == 'duplicate_group':
        groups.append(deepcopy(groups[0]))
    elif case == 'hidden_spares':
        values[1]['spare_sheet_quantity'] = 1
    elif case == 'different_snapshot':
        values.append(dict(values[1], specification='different-frozen-shape'))
    demands = calculate_effective_component_demands(1, values)
    override = ({demands['components'][1]['signature']: 7} if case == 'override_yield' else {})
    with pytest.raises(CompositeBOMExecutionError):
        calculate_requisitions(demands, joint_cut_groups=groups,
                               actual_yields_by_signature=override)


def test_unrelated_independent_child_keeps_original_calculation():
    demands = calculate_effective_component_demands(7, components())
    rows = calculate_requisitions(demands, joint_cut_groups=[group()])
    assert len(rows) == 2
    ordinary = next(r for r in rows if r.get('kind') != 'joint_cut')
    assert ordinary['component_name'] == 'A片'
    assert ordinary['purchase_sheets'] == 7


def test_integer_ceiling_does_not_round_large_quantities_through_float():
    count = 2**54 + 1
    result = plan_cutting_group(group(), {102: count, 103: 0, 104: 0})
    assert result['theory_input_sheets'] == (count + 1) // 2


def test_empty_group_opt_in_is_identical_to_existing_behavior():
    demands = calculate_effective_component_demands(7, components())
    assert calculate_requisitions(demands) == calculate_requisitions(demands, joint_cut_groups=[])


@pytest.mark.parametrize('bad', [False, 0, '', {}])
def test_invalid_false_group_cannot_silently_select_legacy_calculation(bad):
    with pytest.raises(CompositeBOMExecutionError):
        calculate_requisitions(calculate_effective_component_demands(1, components()),
                               joint_cut_groups=bad)


@pytest.mark.parametrize('bad', [False, 0, '', []])
def test_invalid_false_stock_is_not_silently_ignored(bad):
    with pytest.raises(CompositeBOMExecutionError):
        plan_cutting_group(group(), {102: 2, 103: 2, 104: 1}, eligible_output_stock=bad)


def test_decimal_database_dimension_is_preserved_without_float_multiplication():
    data = group(factor=(2, 3))
    data['theory_length_mm'] = 375.25
    result = plan_cutting_group(data, {102: 2, 103: 2, 104: 1})
    assert result['supplier_length_mm'] == '750.50'
    assert result['frozen_group']['group']['theory_length_mm'] == '375.25'
