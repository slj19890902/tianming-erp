"""Read verified conversion costs without promoting reference prices to purchases."""
import json
from decimal import Decimal
from sqlalchemy import select
from app.models.warehouse_inventory import InventoryLot, InventoryMovement, InventoryReservation


def derived_cost(db, lot, visited):
    from app.services.inventory_valuation import frozen_cost
    if lot.id in visited or len(visited) > 30:
        return None, {'validation_issue':'成本来源循环或层数异常'}
    visited=set(visited)|{lot.id}
    try:
        detail=json.loads(lot.cost_snapshot_detail_json or '{}')
        if lot.cost_snapshot_source=='stock_preparation':
            from app.models.stock_preparation import StockPreparationJob
            job=db.get(StockPreparationJob,lot.source_ref_id)
            origin=db.get(InventoryLot,detail.get('source_lot_id'))
            qty=int(detail['output_quantity']); take=int(detail['input_quantity']); total=Decimal(detail['total_cost'])
            reservation=db.get(InventoryReservation,job.reservation_id) if job else None
            if origin is None or qty<=0 or take<=0 or not job or job.status!='completed' or job.actual_output!=qty or job.input_quantity!=take or not reservation or reservation.inventory_lot_id!=origin.id or reservation.consumed_stock_quantity!=take:
                return None, {}
            unit,evidence=frozen_cost(origin,db,visited)
            stored=Decimal(str(origin.estimated_unit_cost_snapshot)) if origin.estimated_unit_cost_snapshot is not None else None
            if unit is None or stored is None or abs(unit-stored)>Decimal('.0001') or abs(stored*take-total)>Decimal('.01'):
                return None, {'validation_issue':'加工投入成本与冻结来源不一致'}
            result=total/qty
            if abs(result-Decimal(str(lot.estimated_unit_cost_snapshot)))>Decimal('.0001'):
                return None, {'validation_issue':'加工产出单价与来源不一致'}
            return result,dict(currency='CNY',cost_label='加工继承成本',source_lot_id=origin.id,source_evidence=evidence,
                total_cost=str(total),quantity=qty,basis='inherited_entry_cost_not_new_purchase')
        if lot.cost_snapshot_source=='stock_preparation_assembly':
            from app.models.stock_preparation import StockPreparationCommand
            # Exact output identity inside an immutable command result; LIKE is
            # only a bounded candidate lookup, every candidate is verified.
            candidates=db.scalars(select(StockPreparationCommand).where(
                StockPreparationCommand.result_json.contains('"output_lot_id":'+str(lot.source_ref_id)))).all()
            results=[json.loads(c.result_json) for c in candidates]
            results=[r for r in results if r.get('action')=='assemble' and r.get('output_lot_id')==lot.source_ref_id]
            if len(results)!=1 or not lot.finished_detail:return None, {'validation_issue':'备库组套来源不唯一'}
            command=results[0];qty=int(command['sets']);total=Decimal(detail['total_cost'])
            if qty<=0 or command['recipe']['parent_id']!=lot.finished_detail.product_id or command['inputs']!=detail['inputs']:
                return None, {'validation_issue':'备库组套身份或投入不一致'}
            calculated=Decimal(0);evidence=[]
            for row in command['inputs']:
                origin=db.get(InventoryLot,row['lot_id'])
                movement=db.scalar(select(InventoryMovement).where(InventoryMovement.idempotency_key==row['movement_key']))
                if not origin or not movement or movement.inventory_lot_id!=origin.id or movement.quantity!=row['quantity'] or movement.movement_type!='consume':
                    return None, {'validation_issue':'备库组套缺少实际消耗记录'}
                unit,basis=frozen_cost(origin,db,visited)
                stored=Decimal(str(origin.estimated_unit_cost_snapshot)) if origin.estimated_unit_cost_snapshot is not None else None
                if unit is None or stored is None or abs(unit-stored)>Decimal('.0001'):return None, {'validation_issue':'备库组套投入成本待核对'}
                calculated+=stored*row['quantity'];evidence.append(dict(lot_id=origin.id,quantity=row['quantity'],basis=basis))
            if abs(calculated-total)>Decimal('.01') or abs(total/qty-Decimal(str(lot.estimated_unit_cost_snapshot)))>Decimal('.0001'):
                return None, {'validation_issue':'备库组套成本不守恒'}
            return total/qty,dict(currency='CNY',cost_label='备库组套继承成本',quantity=qty,total_cost=str(total),inputs=evidence,basis='inherited_entry_cost_not_new_purchase')
        if lot.cost_snapshot_source=='bom_assembly':
            from app.models.multilevel_bom import BomAssembly, BomAssemblyInput
            assembly=db.get(BomAssembly,lot.source_ref_id)
            if not assembly or assembly.status!='posted' or assembly.quantity<=0 or not lot.finished_detail or assembly.output_product_id!=lot.finished_detail.product_id:
                return None, {'validation_issue':'组套来源身份不完整'}
            inputs=list(db.scalars(select(BomAssemblyInput).where(BomAssemblyInput.conversion_id==assembly.id)))
            if not inputs or sum((r.total_cost for r in inputs),Decimal(0))!=assembly.total_cost:
                return None, {'validation_issue':'组套投入成本合计不一致'}
            evidence=[]
            for row in inputs:
                origin=db.get(InventoryLot,row.lot_id)
                movement=db.get(InventoryMovement,row.consume_movement_id)
                if not origin or not movement or movement.inventory_lot_id!=origin.id or movement.quantity!=row.quantity or movement.movement_type!='consume':
                    return None, {'validation_issue':'组套投入缺少一致的实际消耗记录'}
                if origin.finished_detail and origin.finished_detail.product_id!=row.product_id:
                    return None, {'validation_issue':'组套投入产品与原批次不一致'}
                unit,basis=frozen_cost(origin,db,visited)
                # Existing assembly consumes its rounded four-decimal entry unit.
                stored=Decimal(str(origin.estimated_unit_cost_snapshot)) if origin.estimated_unit_cost_snapshot is not None else None
                if unit is None or stored is None or abs(unit-stored)>Decimal('.0001') or abs(stored*row.quantity-row.total_cost)>Decimal('.01'):
                    return None, {'validation_issue':'组套投入未能核对原批次成本'}
                evidence.append(dict(input_id=row.id,lot_id=row.lot_id,quantity=row.quantity,cost=str(row.total_cost),basis=basis))
            result=assembly.total_cost/assembly.quantity
            if lot.estimated_unit_cost_snapshot is None or abs(result-Decimal(str(lot.estimated_unit_cost_snapshot)))>Decimal('.0001'):
                return None, {'validation_issue':'组套产出单价与来源不一致'}
            return result,dict(currency='CNY',cost_label='组套继承成本',assembly_id=assembly.id,
                total_cost=str(assembly.total_cost),quantity=assembly.quantity,inputs=evidence,basis='inherited_entry_cost_not_new_purchase')
    except (ValueError,TypeError,KeyError,ArithmeticError):
        return None, {'validation_issue':'加工或组套成本记录格式不完整'}
    return None, {}
