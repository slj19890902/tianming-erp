"""Read verified conversion costs without promoting reference prices to purchases."""
import json
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from sqlalchemy import select
from app.services.inventory_transfer_lineage import is_original_or_location_split
from app.models.warehouse_inventory import InventoryLot, InventoryLotTransfer, InventoryMovement, InventoryReservation


def _assembly_input_cost_matches(origin, unit, evidence, quantity, amount):
    """Validate the same exact total that source_cost apportions at posting.

    Display unit costs have four decimals. Multiplying them back by a large
    quantity loses the original purchase total. A cumulative four-decimal
    slice can only be the floor or ceiling of the exact proportional amount;
    consuming the whole source must preserve its exact total.
    """
    stored = (Decimal(str(origin.estimated_unit_cost_snapshot))
              if origin.estimated_unit_cost_snapshot is not None else None)
    if unit is None or stored is None or abs(unit - stored) > Decimal('.0001'):
        return False
    total, count = None, None
    if origin.source_ref_type == 'production_completion' and 'bom_material_product_id' in evidence:
        total, count = evidence.get('capitalized_material_cost'), evidence.get('finished_quantity')
    elif origin.source_ref_type in {'bom_assembly', 'subkit_conversion'}:
        total, count = evidence.get('total_cost'), evidence.get('quantity')
    elif origin.source_ref_type == 'bom_external_receipt':
        total, count = evidence.get('capitalized_material_cost'), evidence.get('quantity')
    if total is not None and count is not None and Decimal(str(count)) >= quantity > 0:
        exact = Decimal(str(total)) * quantity / Decimal(str(count))
        if Decimal(str(count)) == quantity:
            return amount == Decimal(str(total))
        quantum = Decimal('.0001')
        return amount in {exact.quantize(quantum, rounding=ROUND_FLOOR),
                          exact.quantize(quantum, rounding=ROUND_CEILING)}
    return abs(stored * quantity - amount) <= Decimal('.01')


def derived_cost(db, lot, visited):
    from app.services.inventory_valuation import frozen_cost
    if lot.id in visited or len(visited) > 30:
        return None, {'validation_issue':'成本来源循环或层数异常'}
    visited=set(visited)|{lot.id}
    try:
        detail=json.loads(lot.cost_snapshot_detail_json or '{}')
        if lot.cost_snapshot_source == 'sheet_cut_production':
            from app.models.production import ProductionCompletion
            from app.services.production_workflow import _stable_key
            completion = db.get(ProductionCompletion, detail['completion_id'])
            qty = int(detail['output_quantity'])
            if not completion or completion.status != 'posted' or not is_original_or_location_split(db, lot, completion.inventory_lot_id) or completion.actual_output_quantity != qty or qty <= 0:
                return None, {'validation_issue':'裁切完工来源不一致'}
            total = Decimal(0)
            evidence = []
            seen = set()
            for row in detail['inputs']:
                reservation = db.get(InventoryReservation, row['reservation_id'])
                origin = db.get(InventoryLot, row['lot_id'])
                movement = db.scalar(select(InventoryMovement).where(InventoryMovement.idempotency_key ==
                    _stable_key('production-completion', completion.id, 'semi', row['reservation_id'])))
                if row['reservation_id'] in seen or not reservation or not origin or reservation.inventory_lot_id != origin.id or not movement or movement.inventory_lot_id != origin.id or movement.quantity != row['quantity'] or movement.movement_type != 'consume':
                    return None, {'validation_issue':'裁切投入缺少一致的实际消耗记录'}
                seen.add(row['reservation_id'])
                unit, basis = frozen_cost(origin, db, visited)
                if unit is None or abs(unit - Decimal(row['unit_cost'])) > Decimal('.0001'):
                    return None, {'validation_issue':'裁切投入冻结成本待核对'}
                total += Decimal(row['unit_cost']) * row['quantity']
                evidence.append(dict(lot_id=origin.id, quantity=row['quantity'], basis=basis))
            if not seen or abs(total-Decimal(detail['total_cost'])) > Decimal('.01') or abs(total / qty - Decimal(str(lot.estimated_unit_cost_snapshot))) > Decimal('.0001'):
                return None, {'validation_issue':'裁切产出成本不守恒'}
            return total / qty, dict(currency='CNY', cost_label='裁切继承材料成本', inputs=evidence,
                quantity=qty, total_cost=str(total), basis='inherited_entry_cost_not_new_purchase')
        if lot.cost_snapshot_source=='stock_preparation':
            from app.models.stock_preparation import StockPreparationJob
            if detail.get('automatic_batch'):
                qty=int(detail['output_quantity']);total=Decimal(detail['total_cost'])
                anchor=db.get(StockPreparationJob,lot.source_ref_id)
                if (not anchor or anchor.status!='completed' or not is_original_or_location_split(db, lot, anchor.output_lot_id)
                        or anchor.actual_output!=qty or qty<=0 or not isinstance(detail.get('inputs'),list)):
                    return None, {'validation_issue':'补库自动成品来源不一致'}
                calculated=Decimal(0);evidence=[];seen=set()
                for row in detail['inputs']:
                    job=db.get(StockPreparationJob,row['job_id']);reservation=db.get(InventoryReservation,row['reservation_id'])
                    origin=db.get(InventoryLot,row['lot_id']);movement=db.scalar(select(InventoryMovement).where(
                        InventoryMovement.idempotency_key==row['movement_key']))
                    if (row['job_id'] in seen or not job or not reservation or not origin or not movement
                            or job.receipt_item_id!=row['receipt_item_id'] or job.reservation_id!=reservation.id
                            or job.product_id!=row['product_id'] or job.status!='completed'
                            or job.input_quantity!=row['quantity'] or job.output_lot_id not in (None,anchor.output_lot_id)
                            or reservation.inventory_lot_id!=origin.id or reservation.status!='consumed'
                            or reservation.reserved_stock_quantity!=row['quantity']
                            or reservation.consumed_stock_quantity!=row['quantity']
                            or movement.inventory_lot_id!=origin.id or movement.reservation_id!=reservation.id
                            or movement.movement_type!='consume' or movement.quantity!=row['quantity']):
                        return None, {'validation_issue':'补库自动成品投入缺少一致的消耗记录'}
                    seen.add(row['job_id']);unit,basis=frozen_cost(origin,db,visited)
                    stored=Decimal(str(origin.estimated_unit_cost_snapshot)) if origin.estimated_unit_cost_snapshot is not None else None
                    if unit is None or stored is None or abs(unit-stored)>Decimal('.0001') or abs(stored-Decimal(row['unit_cost']))>Decimal('.0001'):
                        return None, {'validation_issue':'补库自动成品投入成本待核对'}
                    calculated+=stored*row['quantity'];evidence.append(dict(lot_id=origin.id,quantity=row['quantity'],basis=basis))
                if (not seen or sum(int(r['quantity']) for r in detail['inputs'])!=int(detail['input_quantity'])
                        or abs(calculated-total)>Decimal('.01')
                        or abs(total/qty-Decimal(str(lot.estimated_unit_cost_snapshot)))>Decimal('.0001')):
                    return None, {'validation_issue':'补库自动成品数量或成本不守恒'}
                return total/qty,dict(currency='CNY',cost_label='补库实收自动成品继承成本',inputs=evidence,
                    quantity=qty,total_cost=str(total),basis='inherited_entry_cost_not_new_purchase')
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
            consumed_spec=detail.get('consumed_assembly_spec')
            if consumed_spec is not None:
                from app.services.bom_entry_cost import consumed_assembly_spec_issues
                issues=consumed_assembly_spec_issues(consumed_spec,lot.finished_detail.physical_basis_json,
                    [dict(lot_id=r['lot_id'],product_id=r['product_id'],quantity=r['quantity']) for r in command['inputs']])
                if issues:return None, {'validation_issue':'；'.join(issues)}
            return total/qty,dict(**{k:detail[k] for k in ('assembly_standard','standard_labour_unit_cost','standard_labour_missing','product_unit','consumed_assembly_spec') if k in detail},currency='CNY',cost_label='备库组套继承成本',quantity=qty,total_cost=str(total),inputs=evidence,basis='inherited_entry_cost_not_new_purchase')
        if lot.cost_snapshot_source == 'stock_preparation_finish':
            from app.models.stock_preparation import StockPreparationCommand, StockPreparationJob
            from app.services.finished_stock_identity import matches_stock_identity
            from app.services.warehouse_inventory import _transfer_key
            origin = db.get(InventoryLot, detail['source_lot_id'])
            qty = int(detail['output_quantity'])
            # A partial location move creates a new lot, while the employee's
            # conversion command correctly continues to name the original lot.
            # Follow only recorded transfer edges; never infer ancestry from
            # copied cost JSON or product codes alone.
            output = lot
            lineage = set()
            pairs = []
            for _ in range(30):
                if output.id in lineage:
                    break
                lineage.add(output.id)
                candidates = db.scalars(select(StockPreparationCommand).where(
                    StockPreparationCommand.result_json.contains('"output_lot_id":'+str(output.id)))).all()
                pairs = [(c, json.loads(c.result_json)) for c in candidates]
                pairs = [(c, r) for c, r in pairs if r.get('action') == 'finish_output' and r.get('output_lot_id') == output.id]
                if pairs:
                    break
                edges = db.scalars(select(InventoryLotTransfer).where(
                    InventoryLotTransfer.target_lot_id == output.id,
                    InventoryLotTransfer.source_lot_id != output.id)).all()
                if len(edges) != 1 or output.source_type != 'transfer':
                    break
                edge = edges[0]
                parent = db.get(InventoryLot, edge.source_lot_id)
                target_move = db.scalar(select(InventoryMovement).where(
                    InventoryMovement.idempotency_key == _transfer_key('location-transfer', edge.idempotency_key, 'target')))
                source_move = db.scalar(select(InventoryMovement).where(
                    InventoryMovement.idempotency_key == _transfer_key('location-transfer', edge.idempotency_key, 'source')))
                if (not parent or not parent.finished_detail or not output.finished_detail
                        or edge.quantity <= 0 or edge.available_quantity + edge.reserved_quantity != edge.quantity
                        or not source_move or source_move.inventory_lot_id != parent.id
                        or source_move.movement_type != 'location_transfer' or source_move.quantity != edge.quantity
                        or (source_move.before_available + source_move.before_reserved
                            - source_move.after_available - source_move.after_reserved) != edge.quantity
                        or not target_move or target_move.inventory_lot_id != output.id
                        or target_move.movement_type != 'location_transfer' or target_move.quantity != edge.quantity
                        or target_move.before_available != 0 or target_move.before_reserved != 0
                        or target_move.after_available != edge.available_quantity
                        or target_move.after_reserved != edge.reserved_quantity
                        or output.source_ref_type != parent.source_ref_type
                        or output.source_ref_id != parent.source_ref_id
                        or output.cost_snapshot_source != parent.cost_snapshot_source
                        or output.cost_snapshot_detail_json != parent.cost_snapshot_detail_json
                        or output.estimated_unit_cost_snapshot != parent.estimated_unit_cost_snapshot
                        or output.cost_snapshot_at != parent.cost_snapshot_at
                        or output.unit != parent.unit
                        or output.finished_detail.product_id != parent.finished_detail.product_id
                        or output.finished_detail.owner_customer_id != parent.finished_detail.owner_customer_id
                        or output.finished_detail.physical_basis_json != parent.finished_detail.physical_basis_json):
                    break
                output = parent
            if len(pairs) != 1 or not origin or not lot.finished_detail or qty <= 0:
                return None, {'validation_issue':'半成品转成品来源不唯一'}
            command, result = pairs[0]
            request = json.loads(command.request_json)
            job = db.get(StockPreparationJob, result['job_id'])
            movement = db.scalar(select(InventoryMovement).where(
                InventoryMovement.idempotency_key == detail['movement_key']))
            if (not job or job.output_lot_id != origin.id or job.status != 'completed'
                    or origin.id != lot.source_ref_id or origin.source_ref_type != 'stock_preparation'
                    or output.id != result['output_lot_id']
                    or result['source_lot_id'] != origin.id or result['quantity'] != qty
                    or result['product_id'] != lot.finished_detail.product_id
                    or result['customer_id'] != lot.finished_detail.owner_customer_id
                    or request.get('quantity') != qty or request.get('output_lot_id') != origin.id
                    or request.get('operation_key') != command.operation_key
                    or not movement or movement.inventory_lot_id != origin.id
                    or movement.movement_type != 'consume' or movement.quantity != qty
                    or not matches_stock_identity(lot.finished_detail.physical_basis_json, detail['physical_basis'])):
                return None, {'validation_issue':'半成品转成品身份或消耗流水不一致'}
            unit, evidence = frozen_cost(origin, db, visited)
            stored = Decimal(str(origin.estimated_unit_cost_snapshot)) if origin.estimated_unit_cost_snapshot is not None else None
            total = Decimal(detail['total_cost'])
            if (unit is None or stored is None or abs(unit - stored) > Decimal('.0001')
                    or abs(stored * qty - total) > Decimal('.01')
                    or abs(total / qty - Decimal(str(lot.estimated_unit_cost_snapshot))) > Decimal('.0001')):
                return None, {'validation_issue':'半成品转成品成本不守恒'}
            return total / qty, dict(currency='CNY', cost_label='半成品转成品继承成本',
                source_lot_id=origin.id, source_evidence=evidence, quantity=qty,
                total_cost=str(total), basis='inherited_entry_cost_not_new_purchase')
        if lot.cost_snapshot_source in {'bom_assembly', 'subkit_conversion'}:
            if lot.cost_snapshot_source == 'bom_assembly':
                from app.models.multilevel_bom import BomAssembly as Assembly, BomAssemblyInput as AssemblyInput
            else:
                from app.models.bom_subkit import SubkitConversion as Assembly, SubkitConversionInput as AssemblyInput
            assembly=db.get(Assembly,lot.source_ref_id)
            if lot.cost_snapshot_source == 'subkit_conversion' and assembly:
                from app.models.bom_subkit import OrderSubkit
                group = db.get(OrderSubkit, assembly.order_item_id)
                product_id = group.kit_product_id if group else None
            else:
                product_id = assembly.output_product_id if assembly else None
            if not assembly or assembly.status!='posted' or assembly.quantity<=0 or not lot.finished_detail or product_id!=lot.finished_detail.product_id:
                return None, {'validation_issue':'组套来源身份不完整'}
            inputs=list(db.scalars(select(AssemblyInput).where(AssemblyInput.conversion_id==assembly.id)))
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
                if not _assembly_input_cost_matches(origin, unit, basis, row.quantity, row.total_cost):
                    return None, {'validation_issue':'组套投入未能核对原批次成本'}
                evidence.append(dict(input_id=row.id,lot_id=row.lot_id,quantity=row.quantity,cost=str(row.total_cost),basis=basis))
            result=assembly.total_cost/assembly.quantity
            if lot.estimated_unit_cost_snapshot is None or abs(result-Decimal(str(lot.estimated_unit_cost_snapshot)))>Decimal('.0001'):
                return None, {'validation_issue':'组套产出单价与来源不一致'}
            consumed_spec=detail.get('consumed_assembly_spec')
            if consumed_spec is not None:
                from app.services.bom_entry_cost import consumed_assembly_spec_issues
                issues=consumed_assembly_spec_issues(consumed_spec,lot.finished_detail.physical_basis_json,
                    [dict(lot_id=r.lot_id,product_id=r.product_id,quantity=r.quantity) for r in inputs])
                if issues:return None, {'validation_issue':'；'.join(issues)}
            return result,dict(**{k:detail[k] for k in ('assembly_standard','standard_labour_unit_cost','standard_labour_missing','product_unit','consumed_assembly_spec') if k in detail},currency='CNY',cost_label='组套继承成本',assembly_id=assembly.id,
                total_cost=str(assembly.total_cost),quantity=assembly.quantity,inputs=evidence,basis='inherited_entry_cost_not_new_purchase')
    except (ValueError,TypeError,KeyError,ArithmeticError):
        return None, {'validation_issue':'加工或组套成本记录格式不完整'}
    return None, {}
