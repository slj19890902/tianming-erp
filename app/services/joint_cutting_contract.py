"""Explicit one-input/many-output cutting math, with no database side effects.

Only an adapter holding verified, customer-scoped, frozen identities may opt in.
Same dimensions or a shared mold alone never create a joint operation. Buying a
supplier sheet, cutting it, and assembling its outputs are separate quantities.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from collections.abc import Mapping

from app.services.composite_bom_execution import (
    CompositeBOMExecutionError, calculate_requisition,
)

_FIELDS = {
    'key', 'customer_id', 'mold_tool_id', 'theory_length_mm', 'theory_width_mm',
    'length_parts', 'width_parts', 'outputs', 'spare_supplier_sheets',
}
_OUTPUT_FIELDS = {'product_id', 'quantity_per_input'}


def _fail(message):
    raise CompositeBOMExecutionError(message)


def _integer(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        _fail(f'{label}必须为不小于{minimum}的整数')
    return value


def _dimension(value):
    if type(value) not in (int, float, str, Decimal):
        _fail('模切投入尺寸必须为明确的正数毫米')
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0 or number > 100000:
            raise ValueError()
        if number * 100 != (number * 100).to_integral_value():
            raise ValueError()
        return format(number.normalize(), 'f')
    except (ValueError, InvalidOperation):
        _fail('模切投入尺寸必须为正数，最多两位小数毫米')


def validate_cutting_group(group):
    if type(group) is not dict or set(group) != _FIELDS:
        _fail('共同模切配置字段缺失或包含未知字段')
    key = group['key']
    if type(key) is not str or not key.strip() or key != key.strip() or len(key) > 100:
        _fail('共同模切组身份无效')
    result = dict(group)
    for field in ('customer_id', 'mold_tool_id', 'length_parts', 'width_parts'):
        result[field] = _integer(group[field], field, 1)
    for field in ('theory_length_mm', 'theory_width_mm'):
        result[field] = _dimension(group[field])
    result['spare_supplier_sheets'] = _integer(group['spare_supplier_sheets'], '组备用纸张')
    if type(group['outputs']) is not list or not 1 <= len(group['outputs']) <= 100:
        _fail('共同模切必须包含1至100种明确产出')
    outputs, seen = [], set()
    for output in group['outputs']:
        if type(output) is not dict or set(output) != _OUTPUT_FIELDS:
            _fail('共同模切产出字段无效')
        pid = _integer(output['product_id'], '产出产品身份', 1)
        quantity = _integer(output['quantity_per_input'], '每张产出片数', 1)
        if pid in seen:
            _fail('同一模切组不能重复登记相同产出')
        seen.add(pid)
        outputs.append({'product_id': pid, 'quantity_per_input': quantity})
    result['outputs'] = sorted(outputs, key=lambda row: row['product_id'])
    return result


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def freeze_cutting_group(group):
    canonical = validate_cutting_group(group)
    document = {'schema_version': 1, 'group': canonical}
    return {**document, 'sha256': hashlib.sha256(_encoded(document)).hexdigest()}


def load_cutting_group(snapshot):
    if type(snapshot) is not dict or set(snapshot) != {'schema_version', 'group', 'sha256'}:
        _fail('共同模切快照字段无效')
    if type(snapshot['schema_version']) is not int or snapshot['schema_version'] != 1:
        _fail('不支持的共同模切快照版本')
    expected = freeze_cutting_group(snapshot['group'])
    if snapshot != expected:
        _fail('共同模切快照校验失败，不能重新猜测配方')
    return expected['group']


def _quantities(values, product_ids, label, *, require_all=False):
    if not isinstance(values, Mapping):
        _fail(f'{label}必须按真实产品身份提供')
    result = {}
    for pid, quantity in values.items():
        _integer(pid, f'{label}产品身份', 1)
        if pid not in product_ids:
            _fail(f'{label}包含不属于本组的产品')
        result[pid] = _integer(quantity, label)
    if require_all and set(result) != product_ids:
        _fail(f'{label}缺少共同产出，零需求也须明确提供')
    return result


def plan_cutting_group(group, demands, *, eligible_output_stock=None):
    """Plan one explicit pattern; no allocations, receipts, or stock postings.

    Stock values must already be scoped, eligible and allocated by the caller.
    Rounding chooses the greatest output shortage, never the sum of per-output
    sheets. Supplier cutting factors convert input paper only; spare/raw excess
    are never silently counted as processed components or complete products.
    """
    group = validate_cutting_group(group)
    ids = {row['product_id'] for row in group['outputs']}
    needs = _quantities(demands, ids, '需求数量', require_all=True)
    stock = _quantities({} if eligible_output_stock is None else eligible_output_stock,
                        ids, '可抵扣零件数量')
    rows, theory_inputs = [], 0
    for output in group['outputs']:
        pid, per_input = output['product_id'], output['quantity_per_input']
        credited = min(needs[pid], stock.get(pid, 0))
        unmet = needs[pid] - credited
        theory_inputs = max(theory_inputs, (unmet + per_input - 1) // per_input)
        rows.append({**output, 'demand_output': needs[pid], 'credited_output': credited,
                     'unmet_output': unmet})
    factor = group['length_parts'] * group['width_parts']
    net_sheets = (theory_inputs + factor - 1) // factor
    spares = group['spare_supplier_sheets'] if theory_inputs else 0
    purchase = net_sheets + spares
    for row in rows:
        row['planned_output'] = theory_inputs * row['quantity_per_input']
        row['surplus_output'] = row['planned_output'] - row['unmet_output']
    return {
        'kind': 'joint_cut', 'group_key': group['key'],
        'customer_id': group['customer_id'], 'mold_tool_id': group['mold_tool_id'],
        'frozen_group': freeze_cutting_group(group), 'outputs': rows,
        'theory_input_sheets': theory_inputs, 'cutting_factor': factor,
        'supplier_length_mm': format(Decimal(group['theory_length_mm']) * group['length_parts'], 'f'),
        'supplier_width_mm': format(Decimal(group['theory_width_mm']) * group['width_parts'], 'f'),
        'net_sheets': net_sheets, 'spare_sheets': spares, 'purchase_sheets': purchase,
        'uncut_theory_sheets': purchase * factor - theory_inputs,
    }


def calculate_joint_requisitions(effective_demands, groups, actual_yields=None):
    """Opt-in calculation bridge; callers must freeze/authorize the pattern.

    Runtime adapters do not automatically opt in. Before using this result for
    purchasing they must persist the joint source and support multi-output stock
    posting. An ordinary scalar-yield row cannot represent this return shape.
    """
    if type(groups) not in (tuple, list):
        _fail('共同模切组列表无效')
    actual_yields = {} if actual_yields is None else actual_yields
    if not isinstance(actual_yields, Mapping):
        _fail('实际产出覆盖必须按冻结子件身份提供')
    demands = list(effective_demands.get('components', ()))
    by_product = {}
    for row in demands:
        component = row['component']
        pid = component.get('component_product_id', component.get('product_id'))
        by_product.setdefault(pid, []).append(row)
    result, assigned, keys = [], set(), set()
    for value in groups:
        group = validate_cutting_group(value)
        if group['key'] in keys:
            _fail('共同模切组身份重复')
        keys.add(group['key'])
        counts = {}
        for output in group['outputs']:
            pid = output['product_id']
            if pid in assigned:
                _fail('同一零件不能同时从两组重复计算投入')
            matches = by_product.get(pid, [])
            if len(matches) != 1:
                _fail('共同模切产出缺失或存在不同冻结规格，不能自动合并')
            row = matches[0]
            component = row['component']
            customer = component.get('customer_id', component.get('snapshot_customer_id'))
            mold = component.get('mold_tool_id', component.get('snapshot_mold_tool_id'))
            if (type(customer) is not int or customer != group['customer_id']
                    or type(mold) is not int or mold != group['mold_tool_id']
                    or component.get('is_die_cut') is not True):
                _fail('共同模切客户或模具身份与冻结子件不一致')
            if component.get('spare_sheet_quantity', 0) != 0:
                _fail('共同模切备用纸张须在组上明确设置，不能隐藏子件加放')
            if row['signature'] in actual_yields:
                _fail('共同模切不能用单个数值覆盖多种产出')
            counts[pid] = row['demand_quantity']
            assigned.add(pid)
        result.append(plan_cutting_group(group, counts))
    for row in demands:
        component = row['component']
        pid = component.get('component_product_id', component.get('product_id'))
        if pid not in assigned:
            result.append(calculate_requisition(row,
                actual_yield_per_sheet=actual_yields.get(row['signature'])))
    return result
