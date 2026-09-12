from fastapi.testclient import TestClient
from sqlalchemy import select
from app.models.receipt_putaway import ReceiptStagingArea
from app.models.warehouse_inventory import WarehouseArea, WarehouseLocation, InventoryLot
from tests.test_phase11_requisition import requisition_app, _login
from tests.test_p1_81_receipt_purpose_flow import (
    _seed_material_and_staging, _use_p181_published_map_identity, _create_frozen_sources,
    _freeze_receipt_fact, _receive, _assert_allocation, _business_counts)


def test_receipt_exceeds_single_staging_slot_preserves_conversion_cost_and_replay(requisition_app,monkeypatch):
    app,factory=requisition_app
    _use_p181_published_map_identity(monkeypatch)
    _seed_material_and_staging(factory,finished_slot_count=1)
    with factory() as db:
        area=db.scalar(select(WarehouseArea).where(WarehouseArea.area_code=='FIN-001'))
        db.add(ReceiptStagingArea(area_id=area.id))
        area.storage_policy.allowed_inventory_types_json='["finished","semi_finished","raw_material"]'
        loc=db.scalar(select(WarehouseLocation).where(WarehouseLocation.area_code=='FIN-001'))
        loc.warehouse_type='shared'
        location_id=loc.id
        db.commit()
    with TestClient(app) as client:
        _login(client,'admin')
        source=_create_frozen_sources(client,factory,order_quantity=500,purchase_total=600,order_purpose=500,stock_purpose=100)[0]
        frozen=_freeze_receipt_fact(client,source,idempotency_key='putaway-price')
        assert frozen.status_code==200,frozen.text
        fact=frozen.json()
        _assert_allocation(_receive(client,source,fact,quantity=450,idempotency_key='putaway-450'),
            order_delta=450,reserve_delta=0,order_cumulative=450,reserve_cumulative=0,finished_delta=450,finished_cumulative=450)
        response=_receive(client,source,fact,quantity=150,idempotency_key='putaway-150',overrides={"surplus_disposition":"semi_finished_reserve"})
        _assert_allocation(response,order_delta=50,reserve_delta=100,order_cumulative=500,reserve_cumulative=100,
            finished_delta=50,finished_cumulative=500)
        counts=_business_counts(factory)
        assert _receive(client,source,fact,quantity=150,idempotency_key='putaway-150',overrides={"surplus_disposition":"semi_finished_reserve"}).status_code==200
        assert _business_counts(factory)==counts
    with factory() as db:
        lots=db.scalars(select(InventoryLot).where(InventoryLot.warehouse_location_id==location_id,InventoryLot.status=='active')).all()
        assert sum(x.quantity_available+x.quantity_reserved for x in lots if x.inventory_type=='finished')==500
        assert sum(x.quantity_available+x.quantity_reserved for x in lots if x.inventory_type=='semi_finished')==100
        assert all(x.estimated_unit_cost_snapshot is not None for x in lots)
        # Production putaway must move just this completion, not other lots on
        # the shared staging pallet; the second completion may join the same rack cell.
        from tests.test_p1_123_warehouse_region_rack_labels import _layout
        from app.services.warehouse_rack_cells import sync_published_rack_cells
        from app.models.production import ProductionCompletion
        from app.services.production_workflow import transfer_direct_completion_to_stock, StockTransferCommand
        raw=db.scalar(select(WarehouseArea).where(WarehouseArea.area_code=='RAW-001'))
        raw.floor.floor_code='3F'
        raw.storage_policy.storage_layout='rack'
        raw.storage_policy.allowed_inventory_types_json='["finished"]'
        layout=_layout()
        layout['revision']='p181-anonymous-map-v1'
        layout['features'][0]['id']='zone-p181-3f-raw-001'
        layout['racks'][0]['area_feature_id']='zone-p181-3f-raw-001'
        db.flush()
        cells=sync_published_rack_cells(db,floor_layout=layout,operator_id=1).created_location_ids
        target=db.get(WarehouseLocation,cells[0])
        completions=db.scalars(select(ProductionCompletion).where(ProductionCompletion.inventory_lot_id.in_(
            [x.id for x in lots if x.inventory_type=='finished']))).all()
        assert len(completions)==2
        for completion in completions:
            command=StockTransferCommand(location_id=target.id,expected_layout_version=target.floor3_layout.version,
                idempotency_key=f'production-stage-{completion.id}')
            moved=transfer_direct_completion_to_stock(db,completion_id=completion.id,command=command,operator_id=1)
            assert moved.transfer.warehouse_location_id==target.id
            assert transfer_direct_completion_to_stock(db,completion_id=completion.id,command=command,operator_id=1).replayed
        db.flush()
        at_target=db.scalars(select(InventoryLot).where(InventoryLot.warehouse_location_id==target.id)).all()
        assert sum(x.quantity_available+x.quantity_reserved for x in at_target)==500
