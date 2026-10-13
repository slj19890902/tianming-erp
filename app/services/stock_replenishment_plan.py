"""Versioned, authoritative BOM stock plan; persisted in the existing JSON contract."""
import hashlib
import json


def frozen_bom_plan(item):
    value = json.loads(item.quantity_contract_json) if item.quantity_contract_json else None
    return value if isinstance(value, dict) and value.get('kind') == 'bom_stock_plan' else None


def replenishment_supplier_name(item, order=None):
    """BOM components own their frozen supplier; legacy sources keep the header."""
    plan = frozen_bom_plan(item)
    if plan is not None:
        product_id = item.reference_product_id or item.product_id
        component = next((row for row in plan['components'] if row['product_id'] == product_id), None)
        # Never substitute the parent or today's material for missing frozen facts.
        supplier = str((component or {}).get('defaults', {}).get('material_supplier_name') or '').strip()
        if not supplier:
            from fastapi import HTTPException
            raise HTTPException(409, '补库BOM冻结资料缺少该组件供应商，请核对原补库计划')
        return supplier
    header = order if order is not None else item.order
    return header.supplier_name


def composite_plan_contract(policy, product, plan):
    components = []
    for row in plan['component_demands']:
        child = row['product']
        relation = row['relation']
        components.append({
            'product_id': child.id, 'product_version': child.version,
            'bom_relation_id': relation.id, 'quantity_per_set': row['quantity_per_set'],
            'required_piece_quantity': row['required_piece_quantity'],
            'covered_piece_quantity': row['covered_piece_quantity'],
            'sheet_quantity': row['sheet_quantity'], 'spare_sheet_quantity': row['spare_sheet_quantity'],
            'defaults': row['defaults'], 'coverage': row['coverage'],
            'production_process': child.production_process,
            'printing_colors': child.printing_colors,
        })
    body = {'schema_version': 1, 'kind': 'bom_stock_plan', 'policy_id': policy.id,
            'parent_product_id': product.id, 'parent_product_version': product.version,
            'customer_id': product.customer_id, 'finished_quantity': plan['parent_set_quantity'],
            'policy_updated_at': str(policy.updated_at or policy.created_at),
            'components': components}
    # Decimal dimensions are serialized consistently for the API and persisted contract.
    body = json.loads(json.dumps(body, ensure_ascii=False, default=str))
    body['version_fingerprint'] = hashlib.sha256(json.dumps(body, ensure_ascii=False,
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return body
