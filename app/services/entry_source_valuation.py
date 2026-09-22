"""Read frozen purchase/processing entry costs, without consulting current prices."""
from decimal import Decimal


SOURCES = {'direct_external_receipt', 'external_bom_receipt', 'raw_purchase_receipt',
           'component_processing_actual', 'component_processing_estimate', 'semi_finished_estimate'}


def source_entry_cost(db, lot, detail, visited):
    from app.services.inventory_valuation import frozen_cost, positive
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation
    from app.models.purchase_receipt import IncomingReceiptPurposeAllocation, PurchaseReceiptFact
    unit = positive(lot.estimated_unit_cost_snapshot)
    source = lot.cost_snapshot_source
    result = dict(detail)
    try:
        if unit is None or lot.id in visited or len(visited) > 30:
            raise ValueError('成本来源循环或单价无效')
        visited = set(visited) | {lot.id}
        if source in {'direct_external_receipt', 'external_bom_receipt'}:
            quantity = (Decimal(detail['capitalized_material_cost']) / unit if source == 'direct_external_receipt'
                        else Decimal(detail['quantity']))
            if not detail.get('actual') or not detail.get('external_receipt_item_id') or not detail.get('external_purchase_item_id') or not detail.get('stock_unit'):
                raise ValueError('外购冻结来源不完整')
            expected = Decimal(detail['purchase_line_amount']) / Decimal(detail['purchase_quantity'])
            if source == 'direct_external_receipt':
                expected *= Decimal(detail['purchase_per_finished'])
            else:
                expected = Decimal(detail['capitalized_material_cost']) / quantity
            if quantity <= 0 or abs(expected-unit) > Decimal('.0001'):
                raise ValueError('外购成本与冻结数量不一致')
            result.update(product_unit=detail['stock_unit'], cost_label='冻结外购成本（原采购税口径）')
        elif source == 'raw_purchase_receipt':
            quantity = Decimal(detail['quantity'])
            if not detail.get('supplier_receipt_price_fact_id') or not detail.get('raw_purchase_plan_id') or quantity <= 0 or abs(Decimal(detail['total_cost']) / quantity-unit) > Decimal('.0001'):
                raise ValueError('原片成本与冻结采购来源不一致')
            result.update(product_unit='张', cost_label='冻结原片采购成本')
        else:
            if db is None or lot.source_ref_type != 'production_completion':
                raise ValueError('加工成本需核对实际完工来源')
            from app.models.production import ProductionCompletion
            completion = db.get(ProductionCompletion, lot.source_ref_id)
            original = db.get(InventoryLot, completion.inventory_lot_id) if completion else None
            quantity = Decimal(detail['finished_quantity'])
            if not completion or not original or original.cost_snapshot_detail_json != lot.cost_snapshot_detail_json or completion.actual_output_quantity != quantity or quantity <= 0:
                raise ValueError('加工成本与完工数量不一致')
            inputs = detail['bom_material_inputs']
            total = sum((Decimal(row['total_cost']) for row in inputs), Decimal(0))
            if not inputs or abs(total-Decimal(detail['capitalized_material_cost'])) > Decimal('.0001') or abs(total/quantity-unit) > Decimal('.0001'):
                raise ValueError('加工冻结成本不守恒')
            currencies = set()
            for row in inputs:
                if row['kind'] == 'reservation':
                    reservation = db.get(InventoryReservation, row['id'])
                    origin = db.get(InventoryLot, row['lot_id'])
                    if not reservation or not origin or reservation.inventory_lot_id != origin.id:
                        raise ValueError('加工投入库存身份不一致')
                    origin_unit, basis = frozen_cost(origin, db, visited)
                    if origin_unit is None:
                        raise ValueError('加工投入成本依据不完整')
                    currencies.add(basis.get('currency') or 'CNY')
                elif row['kind'] == 'allocation':
                    allocation = db.get(IncomingReceiptPurposeAllocation, row['id'])
                    fact = db.get(PurchaseReceiptFact, allocation.purchase_receipt_fact_id) if allocation else None
                    if not fact:
                        raise ValueError('加工采购来源不完整')
                    currencies.add(fact.currency)
                else:
                    raise ValueError('加工来源需核对')
            if currencies != {'CNY'} or detail.get('currency') not in (None, '', 'CNY'):
                raise ValueError('加工币种不一致或非人民币')
            result.update(currency='CNY', temporary=not bool(detail.get('actual')),
                          cost_label='加工继承材料成本')
        if result.get('currency') != 'CNY':
            raise ValueError('非人民币成本需单独核对，不自动换算')
        return unit, result
    except (ValueError, TypeError, KeyError, ArithmeticError) as error:
        return None, dict(detail, validation_issue=str(error))
