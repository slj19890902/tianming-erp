"""Validate new stock at the real transaction boundary, after source costs freeze.

Transfers/returns preserve prior lot facts. This never scans or reprices old lots.
"""
import json
import hashlib
import re
from decimal import Decimal, InvalidOperation
from sqlalchemy import event
from app.core.receipt_price_guard import ReceiptPriceGuardSession
from sqlalchemy.sql.elements import TextClause


def _positive(value):
    try:
        number = Decimal(str(value))
        return number.is_finite() and number > 0
    except (InvalidOperation, ValueError, TypeError):
        return False


def _assembled_specifications(cost, basis):
    """Validate all frozen assembly leaves, never today's mutable master data."""
    if (cost.get('algorithm_version') != 'assembled-entry-v1'
            or cost.get('source') != 'assembled'
            or cost.get('product_id') != basis.get('product_id')):
        return False
    parts, assembly = cost.get('components'), basis.get('assembly')
    if not isinstance(parts, list) or not parts or not isinstance(assembly, list) or len(parts) != len(assembly):
        return False
    def identities(rows, quantity):
        result = {}
        for row in rows:
            if not isinstance(row, dict) or not row.get('product_id') or not _positive(row.get(quantity)):
                return None
            if row['product_id'] in result:
                return None
            result[row['product_id']] = Decimal(str(row[quantity]))
        return result
    actual, expected = identities(parts, 'quantity_per_set'), identities(assembly, 'quantity')
    if actual is None or expected is None or actual != expected:
        return False

    def complete(node, product_id, depth=0):
        if not isinstance(node, dict) or node.get('product_id') != product_id or depth > 64:
            return False
        children = node.get('components')
        if node.get('source') in {'assembled', 'manufactured', 'purchased'}:
            if not isinstance(children, list) or not children:
                return False
            if node['source'] != 'assembled' and not any(
                    isinstance(p, dict) and p.get('product_id') == product_id for p in children):
                return False
            return all(isinstance(p, dict) and _positive(p.get('quantity_per_set'))
                       and complete(p.get('evidence'), p.get('product_id'), depth + 1) for p in children)
        # Material evidence at a leaf must cover every physical sheet.
        if isinstance(children, list) and children:
            return all(isinstance(p, dict) and _positive(p.get('length_mm'))
                       and _positive(p.get('width_mm')) for p in children)
        rule = node.get('cost_rule')
        return (isinstance(rule, dict) and rule.get('mode') == 'material'
                and _positive(rule.get('length_mm')) and _positive(rule.get('width_mm')))

    return complete(cost, basis['product_id'])


def _physical_kit_specifications(cost, basis):
    """Only the verified Finance/consume source may supply these frozen specs."""
    parts=cost.get('frozen_assembly_spec')
    assembly=basis.get('assembly')
    if not isinstance(parts,list) or len(parts)!=2 or not isinstance(assembly,list) or len(assembly)!=2:
        return False
    actual=[]
    for part in parts:
        if not isinstance(part,dict) or part.get('quantity_per_set')!=1:
            return False
        raw=part.get('basis_json');physical=part.get('physical_basis')
        if not isinstance(raw,str) or not isinstance(physical,dict):
            return False
        try:
            if json.loads(raw)!=physical:
                return False
        except (ValueError,TypeError):
            return False
        if (physical.get('product_id')!=part.get('product_id') or physical.get('schema')!=1
                or not isinstance(physical.get('spec'),str) or not physical['spec'].strip()
                or physical.get('assembly')!=[] or not physical.get('unit')):
            return False
        actual.append(dict(product_id=part['product_id'],quantity=1,
            basis_hash=hashlib.sha256(raw.encode()).hexdigest()))
    return (len({p['product_id'] for p in actual})==2 and
        sorted(actual,key=lambda r:r['product_id'])==sorted(assembly,key=lambda r:r.get('product_id',0)))


@event.listens_for(ReceiptPriceGuardSession, 'do_orm_execute')
def reject_untracked_insert(state):
    table = getattr(state.statement, 'table', None)
    raw = state.statement.text if isinstance(state.statement, TextClause) else ''
    if ((state.is_insert and getattr(table, 'name', None) == 'inventory_lots') or
            (re.search(r'\b(?:insert|replace)\b', raw, re.I) and 'inventory_lots' in raw.lower())):
        from fastapi import HTTPException
        raise HTTPException(422, '新入库必须经过实物资料和成本检查，不能直接批量插入库存')


@event.listens_for(ReceiptPriceGuardSession, 'after_flush')
def track_entries(session, _context):
    from app.models.warehouse_inventory import InventoryLot
    ids = session.info.setdefault('new_inventory_entry_ids', set())
    for row in session.new:
        if isinstance(row, InventoryLot) and (row.source_type not in {'transfer', 'delivery_return'}
                or row.source_ref_type in {'inventory_onboarding_transfer', 'bom_assembly', 'subkit_conversion', 'preparation_assembly', 'stock_preparation_assembly', 'stock_preparation_finish', 'physical_kit_correction_20261014'}
                or (row.source_ref_type == 'stock_preparation' and row.inventory_type == 'finished')):
            ids.add(row.id)


@event.listens_for(ReceiptPriceGuardSession, 'before_commit')
def validate_entries(session):
    if session.in_nested_transaction():
        return
    from app.models.warehouse_inventory import InventoryLot
    from fastapi import HTTPException
    session.flush()
    for lid in session.info.get('new_inventory_entry_ids', ()):
        lot = session.get(InventoryLot, lid, populate_existing=True)
        if lot is None:
            continue
        # A split move inherits the assembly's source_ref_type for traceability.
        # It is not a new assembly: require the actual transfer and both ledger
        # movements before accepting unchanged historical entry facts.
        if _verified_location_split(session, lot):
            continue
        if lot.source_ref_type == 'inventory_onboarding_transfer':
            raise HTTPException(422, '盘点导入库存拆分缺少一致的来源与移库流水')
        missing = []
        issues = []
        try:
            cost = Decimal(str(lot.estimated_unit_cost_snapshot or 0))
            if not cost.is_finite() or cost <= 0:
                missing.append('有效材料成本')
            evidence = json.loads(lot.cost_snapshot_detail_json or '{}')
            if not isinstance(evidence, dict) or not evidence or not lot.cost_snapshot_source:
                missing.append('冻结成本来源')
            if not lot.cost_snapshot_at:
                missing.append('成本冻结时间')
        except (InvalidOperation, ValueError, TypeError):
            missing.append('冻结成本来源')
        from app.services.inventory_valuation import frozen_cost
        unit, cost_detail = frozen_cost(lot, session)
        if unit is None:
            missing.append(cost_detail.get('validation_issue') or '可核验的冻结成本')
        detail = lot.finished_detail
        if detail:
            from app.models.product import Product
            product = session.get(Product, detail.product_id) if detail.product_id else None
            try:
                basis = json.loads(detail.physical_basis_json or '{}')
            except (ValueError, TypeError):
                basis = {}
            if not isinstance(basis, dict):
                basis = {}
            if not basis.get('unit'):
                missing.append('实物单位')
                issues.append(dict(field='unit', label='实物单位',
                    reason='本次入库必须冻结实际计量单位', source='entry_snapshot',
                    repairable=False, repair_page=None, repair_field='unit'))
            components = cost_detail.get('components') or []
            has_sheet = any(isinstance(part, dict) and part.get('length_mm') and part.get('width_mm') for part in components)
            rule = cost_detail.get('cost_rule') or {}
            has_sheet = has_sheet or (rule.get('mode') == 'material' and bool(rule.get('length_mm') and rule.get('width_mm')))
            external_spec = cost_detail.get('frozen_specification')
            if isinstance(external_spec, str):
                try:
                    external_spec = json.loads(external_spec)
                except (ValueError, TypeError):
                    external_spec = None
            external_source = lot.cost_snapshot_source == 'direct_external_receipt' or (
                lot.cost_snapshot_source == 'purchase_receipt_actual'
                and lot.source_ref_type in {'external_packaging_receipt_item', 'direct_external_receipt'}
                and cost_detail.get('external_receipt_item_id') == lot.source_ref_id)
            has_external_spec = external_source and isinstance(external_spec, dict) and bool(external_spec)
            has_assembly_spec = _assembled_specifications(cost_detail, basis)
            consumed_spec_issues = []
            if (lot.cost_snapshot_source in {'stock_preparation_assembly','bom_assembly','subkit_conversion'}
                    and cost_detail.get('consumed_assembly_spec') is not None):
                from app.services.bom_entry_cost import consumed_assembly_spec_issues
                consumed_spec_issues = consumed_assembly_spec_issues(
                    cost_detail.get('consumed_assembly_spec'), basis)
                has_assembly_spec = not consumed_spec_issues
            if lot.cost_snapshot_source == 'physical_kit_correction_20261014':
                has_assembly_spec = _physical_kit_specifications(cost_detail, basis)
            # Report/cutting sheet dimensions explain material cost, but do not
            # become a finished product's length/width.  Production receipts may
            # keep their already frozen manufacturing evidence; manual/stocktake
            # entries must use the operation-specific product/assembly/external
            # identity.
            legal_frozen_spec = bool(
                has_external_spec or has_assembly_spec or (
                    has_sheet and lot.source_type not in {'manual', 'stocktake'}
                )
            )
            from app.services.product_readiness import inventory_entry_issues
            physical_issues = inventory_entry_issues(
                product,
                inventory_type='finished',
                basis=basis,
                has_legal_frozen_spec=legal_frozen_spec,
            )
            if product is None and not missing:
                from app.services.unassigned_finished_entry import verified_unassigned_entry
                if verified_unassigned_entry(session, lot, basis, cost_detail):
                    physical_issues = []
            if consumed_spec_issues:
                physical_issues = [dict(field='assembly_evidence', label=issue,
                    reason='组套输出与本次真实子件消耗依据不一致', source='assembly',
                    repairable=False, repair_page=None, repair_field=None,
                    product_id=detail.product_id,
                    product_code=getattr(product, 'product_code', None),
                    product_name=getattr(product, 'product_name', None))
                    for issue in consumed_spec_issues]
            issues.extend(physical_issues)
            missing.extend([str(issue.get('label') or issue.get('field')) for issue in physical_issues])
        elif lot.semi_finished_detail:
            sheet = lot.semi_finished_detail
            from app.models.product import Product
            from app.services.product_readiness import inventory_entry_issues
            product = session.get(Product, sheet.product_id) if getattr(sheet, 'product_id', None) else None
            semi_issues = inventory_entry_issues(product, inventory_type='semi_finished',
                semi_length=sheet.board_length_mm, semi_width=sheet.board_width_mm,
                semi_material=sheet.material_code_snapshot)
            issues.extend(semi_issues)
            missing.extend([str(issue.get('label') or issue.get('field')) for issue in semi_issues])
        elif lot.inventory_type != 'assembly_body':
            missing.append('实物明细')
        if missing:
            # Preserve the legacy ``missing`` labels while giving every client a
            # single actionable list.  Cost/system issues are administrator work,
            # not a reason to send an employee back to an unrelated material page.
            represented = {str(issue.get('label')) for issue in issues}
            for label in missing:
                if label not in represented:
                    issues.append(dict(field='cost_or_entry_evidence', label=label,
                        reason='本次入库缺少可核验的冻结成本或流水依据',
                        source='frozen_source', repairable=False, repair_page=None,
                        repair_field=None, product_id=detail.product_id if detail else None))
            product_issues = [issue for issue in issues if issue.get('repairable')]
            if product_issues:
                message = '本次入库尚未保存：' + '、'.join(
                    str(issue.get('label')) for issue in issues) + '未填写或依据不足'
            else:
                message = ('系统未能完成入库依据校验（' + '、'.join(dict.fromkeys(missing)) +
                           '）。本次未保存，原表单可继续保留；需系统维护人员修复，'
                           '无需重复修改产品资料或提升账号权限')
            raise HTTPException(422, detail=dict(code='INVENTORY_ENTRY_INCOMPLETE', lot_number=lot.lot_number,
                missing=missing, issues=issues, message=message,
                product_id=detail.product_id if detail else None,
                repair_url=(product_issues[0].get('repair_page') if product_issues else None),
                retry_action='refresh_preflight'))


def _verified_location_split(session, lot):
    from app.services.inventory_transfer_lineage import location_split_source
    return location_split_source(session, lot) is not None


@event.listens_for(ReceiptPriceGuardSession, 'after_transaction_end')
def clear_entries(session, transaction):
    if transaction.parent is None:
        session.info.pop('new_inventory_entry_ids', None)
