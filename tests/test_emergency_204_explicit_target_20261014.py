"""Explicit completion destinations on synthetic FIN slots and frozen semi inputs.

No emergency customer/order IDs or retained business database are loaded. The
map identity fixture is inherited; selection, claims, production and stock are
the real services operating on a disposable SQLite database.
"""
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from tests.test_n039_composite_bom_requisition import composite_requisition_app
from tests.test_p1_81_receipt_purpose_flow import _p181_published_map_identity, _seed_order_semi_reservation
from tests.test_multilevel_bom_receipt_flow import seed_graph


@pytest.fixture
def semi_case(composite_requisition_app, _p181_published_map_identity):
    from app.models.warehouse_inventory import (InventoryLot, InventoryReservation, OrderItemSemiRequirement,
        SemiFinishedLotAllowedProduct, WarehouseLocation)
    _app, factory = composite_requisition_app
    _material, snapshots = seed_graph(factory)
    snapshot_id = next(sid for sid, product_id in snapshots if product_id == 2)
    _seed_order_semi_reservation(factory, credited_piece_quantity=30, pieces_per_box=1)
    with factory() as db:
        reservation=db.scalar(select(InventoryReservation))
        reservation.sales_order_item_bom_component_id=snapshot_id
        requirement=db.get(OrderItemSemiRequirement,reservation.semi_requirement_id)
        requirement.sales_order_item_bom_component_id=snapshot_id
        requirement.required_piece_quantity=30
        requirement.board_length_mm,requirement.board_width_mm=1000,700
        lot=db.get(InventoryLot,reservation.inventory_lot_id)
        lot.estimated_unit_cost_snapshot=Decimal('0.5')
        lot.semi_finished_detail.board_length_mm,lot.semi_finished_detail.board_width_mm=1000,700
        db.scalar(select(SemiFinishedLotAllowedProduct)).product_id=2
        slots=list(db.scalars(select(WarehouseLocation).where(WarehouseLocation.area_code=='FIN-001')
            .order_by(WarehouseLocation.location_code)))
        db.commit()
        return factory, [r.id for r in slots], lot.id, reservation.id


def _facts(db):
    """Read every synthetic table, including ledgers, reservations and audits."""
    from app.models import Base
    return {table.name: [tuple(row) for row in db.execute(select(table).order_by(*table.primary_key.columns))]
            for table in sorted(Base.metadata.tables.values(), key=lambda table: table.name)}


def _confirm(db, review, target, key='synthetic-explicit-second'):
    from app.models.user import User
    from app.services.multilevel_bom_semi_production import confirm_semi_production
    return confirm_semi_production(db,order_item_id=1,product_id=2,target_location_id=target,
        reviewed_hash=review['reviewed_hash'],operation_key=key,actor=db.get(User,1))


@pytest.mark.parametrize('explicit',[True,False])
def test_completion_lot_pallet_and_ground_occupancy_use_same_target_and_replay_is_zero_increment(semi_case,explicit):
    from app.models.production import ProductionCompletion
    from app.models.warehouse_inventory import (InventoryLot,InventoryPallet,InventoryPalletItem,
        WarehouseGroundOccupancy,WarehouseGroundOccupancySlot,InventoryReservation)
    from app.services.multilevel_bom_semi_production import preview_semi_production
    factory, slots, source_lot_id, reservation_id = semi_case
    target=slots[1] if explicit else None
    with factory() as db:
        before=_facts(db)
        _, _, default=preview_semi_production(db,order_item_id=1,product_id=2)
        _, _, none=preview_semi_production(db,order_item_id=1,product_id=2,target_location_id=None)
        # The unified putaway rule reuses this real product's existing formal
        # raw/semi location.  An explicit eligible target still wins for this
        # one operation and never relocates the historical source lot.
        assert default==none
        expected=slots[1] if explicit else default['production_location']['id']
        _, _, review=preview_semi_production(db,order_item_id=1,product_id=2,target_location_id=target)
        assert review['ready'] and review['production_location']['id']==expected
        assert _facts(db)==before
        answer=_confirm(db,review,target)
        completion=db.get(ProductionCompletion,answer['completion_id'])
        lot=db.get(InventoryLot,answer['lot_id'])
        pallet_item=db.scalar(select(InventoryPalletItem).where(InventoryPalletItem.inventory_lot_id==lot.id))
        assert pallet_item is not None
        pallet=db.get(InventoryPallet,pallet_item.pallet_id)
        occupancy=db.scalar(select(WarehouseGroundOccupancy).where(WarehouseGroundOccupancy.pallet_id==pallet.id,
                                                                 WarehouseGroundOccupancy.status=='active'))
        assert occupancy is not None
        occupied=list(db.scalars(select(WarehouseGroundOccupancySlot).where(
            WarehouseGroundOccupancySlot.occupancy_id==occupancy.id,WarehouseGroundOccupancySlot.status=='active')))
        assert completion.warehouse_location_id==lot.warehouse_location_id==pallet.location_id==occupancy.primary_location_id==expected
        assert [(row.location_id,row.slot_sequence) for row in occupied]==[(expected,1)]
        assert answer['quantity']==completion.quantity==pallet_item.quantity==30
        assert pallet.is_current and pallet.status=='active'
        assert lot.quantity_reserved==30 and lot.quantity_available==0
        assert db.get(InventoryLot,source_lot_id).quantity_consumed==30
        assert db.get(InventoryReservation,reservation_id).consumed_stock_quantity==30
        posted=_facts(db)
        assert _confirm(db,review,target)==answer
        assert _facts(db)==posted
        # Existing implicit requests replay identically when target is omitted.
        if not explicit:
            from app.models.user import User
            from app.services.multilevel_bom_semi_production import confirm_semi_production
            assert confirm_semi_production(db,order_item_id=1,product_id=2,
                reviewed_hash=review['reviewed_hash'],operation_key='synthetic-explicit-second',actor=db.get(User,1))==answer
            assert _facts(db)==posted


def test_same_key_with_another_target_conflicts_without_ledger_increment(semi_case):
    from app.services.multilevel_bom_semi_production import preview_semi_production
    from app.services.multilevel_bom_plan import BomPlanError
    factory,slots,_,_=semi_case
    with factory() as db:
        _,_,review=preview_semi_production(db,order_item_id=1,product_id=2,target_location_id=slots[1])
        _confirm(db,review,slots[1]);before=_facts(db)
        with pytest.raises(BomPlanError,match='标识已使用|内容不同'):
            _confirm(db,review,slots[2])
        assert _facts(db)==before


def _disqualify(db,slots,kind):
    from app.models.warehouse_inventory import (InventoryPallet,WarehouseGroundOccupancy,WarehouseGroundOccupancySlot,
        WarehouseArea,WarehouseAreaStoragePolicy,WarehouseGroundLayoutPlan)
    from app.models.customer_finished_storage_preference import CustomerFinishedStoragePreference
    if kind in ('empty_pallet','secondary_slot'):
        pallet=InventoryPallet(pallet_code='synthetic-existing-empty-pallet',location_id=slots[1] if kind=='empty_pallet' else slots[0],
                               status='active',is_current=True,version=1)
        db.add(pallet);db.flush()
        if kind=='secondary_slot':
            occupancy=WarehouseGroundOccupancy(pallet_id=pallet.id,primary_location_id=slots[0],
                footprint_kind='double',capacity_quantity=100,status='active',version=1,created_by=1)
            db.add(occupancy);db.flush()
            for sequence,location_id in enumerate(slots[:2],1):
                db.add(WarehouseGroundOccupancySlot(occupancy_id=occupancy.id,location_id=location_id,slot_sequence=sequence,status='active'))
    elif kind=='unpublished_policy':
        area=db.scalar(select(WarehouseArea).where(WarehouseArea.area_code=='FIN-001'))
        area.storage_policy.status='draft'
    elif kind=='unpublished_plan':
        plan=db.scalar(select(WarehouseGroundLayoutPlan).join(WarehouseArea).where(WarehouseArea.area_code=='FIN-001'))
        plan.status='draft'
        plan.published_map_revision=None
        plan.publish_idempotency_key=None
        plan.publish_request_hash=None
        plan.published_by=None
        plan.published_at=None
    elif kind=='outside_customer_area':
        # A saved customer preference forbids silently falling back to FIN-001,
        # including when that preferred area has no qualified free ground slot.
        area=db.scalar(select(WarehouseArea).where(WarehouseArea.area_code=='DISPATCH'))
        db.add(CustomerFinishedStoragePreference(customer_id=1,warehouse_area_id=area.id,priority=1))
    db.flush()


@pytest.mark.parametrize('kind',['empty_pallet','secondary_slot','unpublished_policy','unpublished_plan','outside_customer_area'])
def test_ineligible_target_is_rejected_by_preview_and_stale_confirmation_without_writes(semi_case,kind):
    from app.services.multilevel_bom_semi_production import preview_semi_production
    from app.services.production_workflow import ProductionWorkflowError
    factory,slots,_,_=semi_case
    with factory() as db:
        _,_,review=preview_semi_production(db,order_item_id=1,product_id=2,target_location_id=slots[1])
        _disqualify(db,slots,kind);db.commit();before=_facts(db)
        with pytest.raises(ProductionWorkflowError):
            preview_semi_production(db,order_item_id=1,product_id=2,target_location_id=slots[1])
        assert _facts(db)==before
        with pytest.raises(ProductionWorkflowError):
            _confirm(db,review,slots[1])
        assert _facts(db)==before


@pytest.mark.parametrize('kind',['policy_before_claim','layout_before_claim','layout_after_claim'])
def test_claim_boundary_rechecks_actual_database_qualification_and_layout_version(semi_case,monkeypatch,kind):
    from app.models.warehouse_inventory import WarehouseArea,WarehouseAreaStoragePolicy,Floor3LocationLayout
    from app.services import production_workflow as workflow
    from app.services.multilevel_bom_semi_production import preview_semi_production
    factory,slots,_,_=semi_case
    claimed=False
    original=workflow.claim_active_placed_location
    def mutate(db,location_id,*,expected_layout_version=None):
        # Execute the real claim and change real rows at its boundary. This is
        # deterministic transaction fault injection, not two-session locking.
        nonlocal claimed
        if location_id!=slots[1]:return original(db,location_id,expected_layout_version=expected_layout_version)
        assert not claimed;claimed=True
        def change():
            if kind=='policy_before_claim':
                area_id=db.scalar(select(WarehouseArea.id).where(WarehouseArea.area_code=='FIN-001'))
                db.execute(update(WarehouseAreaStoragePolicy).where(WarehouseAreaStoragePolicy.area_id==area_id)
                    .values(status='draft'),execution_options={'synchronize_session':False})
            else:
                db.execute(update(Floor3LocationLayout).where(Floor3LocationLayout.location_id==location_id)
                    .values(version=Floor3LocationLayout.version+1),execution_options={'synchronize_session':False})
        if kind!='layout_after_claim':change()
        result=original(db,location_id,expected_layout_version=expected_layout_version)
        if kind=='layout_after_claim':change()
        return result
    with factory() as db:
        _,_,review=preview_semi_production(db,order_item_id=1,product_id=2,target_location_id=slots[1])
        before=_facts(db)
        monkeypatch.setattr(workflow,'claim_active_placed_location',mutate)
        with pytest.raises(workflow.ProductionWorkflowError):
            _confirm(db,review,slots[1])
        assert claimed and _facts(db)==before


def test_non_semi_completion_rejects_explicit_target_and_rolls_back(semi_case):
    from app.services.multilevel_bom_semi_production import preview_semi_production
    from app.services.production_workflow import post_automatic_receipt_completion,ProductionWorkflowError
    from app.services.bom_transactions import atomic_bom
    factory,slots,_,_=semi_case
    with factory() as db:
        context,plan,_=preview_semi_production(db,order_item_id=1,product_id=2,target_location_id=slots[1])
        inputs=plan['detail']['bom_material_inputs'];before=_facts(db)
        with pytest.raises(ProductionWorkflowError,match='仅明确的半成品'):
            with atomic_bom(db):
                post_automatic_receipt_completion(db,order_item_id=1,
                    previous_theoretical_quantity=plan['before'],new_theoretical_quantity=plan['after'],
                    material_input_delta=sum(i['stock_after']-i['stock_before'] for i in inputs),
                    material_input_cumulative=sum(i['stock_after'] for i in inputs),operator_id=1,
                    idempotency_key='synthetic-non-semi-explicit',capitalized_material_cost=plan['total_cost'],
                    cost_detail=plan['detail'],bom_snapshot_id=context.snapshot.id,semi_only=False,target_location_id=slots[1])
        assert _facts(db)==before
