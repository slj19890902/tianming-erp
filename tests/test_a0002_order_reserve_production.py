"""A-0002-R3: frozen order-derived reserve can use the stock-production ledger."""
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.test_phase11_requisition import requisition_app, _login
from tests.test_p1_81_receipt_purpose_flow import (
    _create_frozen_sources,
    _freeze_receipt_fact,
    _p181_published_map_identity,
    _posted_finished_quantity,
    _receive,
    _seed_material_and_staging,
)
from app.api.stock_preparation import router
from app.models.purchase_receipt import IncomingReceiptPurposeAllocation
from app.models.product import Product
from app.models.order import OrderItem
from app.models.stock_preparation import StockPreparationJob
from app.models.warehouse_inventory import (
    InventoryLot,
    InventoryMovement,
    InventoryReservation,
)
from app.services.stock_preparation import plan_product
from app.services.stock_preparation_history import rows as history_rows
from app.services.warehouse_inventory import WarehouseInventoryError


class _ProductDb:
    def __init__(self, product):
        self.product = product

    def get(self, model, identity):
        assert model is Product
        assert identity == self.product.id
        return self.product


def _frozen_item():
    return SimpleNamespace(
        reference_product_id=None,
        product_id=17,
        customer_id=10,
        frozen_order_reserve=True,
    )


def _current_product(**overrides):
    values = dict(
        id=17,
        customer_id=10,
        is_active=True,
        deleted_at=None,
        is_virtual_composite_parent=False,
        flute_type='BC',
        report_length_mm=9999,
        report_width_mm=9999,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_frozen_order_reserve_keeps_frozen_fit_after_current_spec_drift():
    product = _current_product()
    lot = SimpleNamespace(
        allowed_products=[],
        semi_finished_detail=SimpleNamespace(
            flute_type='B', board_length_mm=100, board_width_mm=100
        ),
    )

    assert plan_product(_ProductDb(product), _frozen_item(), lot) is product


@pytest.mark.parametrize(
    ('overrides', 'message'),
    [
        ({'is_active': False}, '缺少有效的同客户目标产品'),
        ({'deleted_at': datetime(2026, 9, 20)}, '缺少有效的同客户目标产品'),
        ({'customer_id': 11}, '缺少有效的同客户目标产品'),
        ({'is_virtual_composite_parent': True}, '虚拟组合母件不能直接入库'),
    ],
)
def test_frozen_order_reserve_rechecks_current_product_identity(overrides, message):
    product = _current_product(**overrides)
    lot = SimpleNamespace(allowed_products=[])

    with pytest.raises(WarehouseInventoryError, match=message):
        plan_product(_ProductDb(product), _frozen_item(), lot)


def test_frozen_order_reserve_obeys_current_explicit_allowed_product_scope():
    product = _current_product()
    lot = SimpleNamespace(
        allowed_products=[SimpleNamespace(product_id=99)]
    )

    with pytest.raises(WarehouseInventoryError, match='适用产品范围不包含目标产品'):
        plan_product(_ProductDb(product), _frozen_item(), lot)


def _row(client, receipt_id):
    response = client.get('/api/production/stock-preparation')
    assert response.status_code == 200, response.text
    return next(row for row in response.json()['items']
                if row['receipt_item_id'] == receipt_id)


def _action(client, row, action, operation_key, **values):
    payload = dict(action=action, operation_key=operation_key,
                   lot_version=row['lot_version'], **values)
    return client.post(
        f"/api/production/stock-preparation/{row['receipt_item_id']}/actions",
        json=payload,
    )


def test_order_reserve_partial_production_replay_reversal_and_scope(
    requisition_app, monkeypatch
):
    app, factory = requisition_app
    app.include_router(router, prefix='/api/production')
    _seed_material_and_staging(factory)
    with TestClient(app) as client:
        _login(client, 'admin')
        source = _create_frozen_sources(
            client, factory, order_quantity=500, purchase_total=600,
            order_purpose=500, stock_purpose=100,
        )[0]
        frozen = _freeze_receipt_fact(
            client, source, idempotency_key='a0002-r3-price'
        )
        assert frozen.status_code == 200, frozen.text
        receipt_ids = []
        for index in (1, 2):
            response = _receive(
                client, source, frozen.json(), quantity=300,
                idempotency_key=f'a0002-r3-receive-{index}',
                overrides={'surplus_disposition': 'semi_finished_reserve'}
                if index == 2 else None,
            )
            assert response.status_code == 200, response.text
            receipt_ids.append(response.json()['receipt_item_id'])
        receipt_id = response.json()['receipt_item_id']

        # A normal order-purpose receipt cannot be mistaken for reserve stock.
        from app.services.stock_preparation import source as preparation_source
        from app.services.warehouse_inventory import WarehouseInventoryError
        with factory() as db:
            try:
                preparation_source(db, receipt_ids[0])
            except WarehouseInventoryError as exc:
                assert '备库收料来源不存在' in str(exc)
            else:
                raise AssertionError('order-purpose receipt must stay outside stock preparation')
            # Later master conversion edits must not rewrite the frozen purchase purpose.
            allocation = db.scalar(select(IncomingReceiptPurposeAllocation).where(
                IncomingReceiptPurposeAllocation.incoming_receipt_item_id == receipt_id
            ))
            order_item = db.get(OrderItem, allocation.source_order_item_id)
            product = db.get(Product, order_item.product_id)
            product.pieces_per_box = 99
            db.commit()

        row = _row(client, receipt_id)
        assert row['source_kind'] == 'order_reserve'
        assert (row['quantity'], row['available'], row['reserved']) == (100, 100, 0)
        assert row['can_plan'] and (row['factor'], row['pieces_per_box']) == (1, 1)

        plan = _action(
            client, row, 'plan', 'a0002-r3-plan', quantity=60
        )
        assert plan.status_code == 200, plan.text
        assert _action(
            client, row, 'plan', 'a0002-r3-plan', quantity=60
        ).json() == plan.json()
        assert _action(
            client, row, 'plan', 'a0002-r3-plan', quantity=59
        ).status_code == 409
        row = _row(client, receipt_id)
        assert (row['available'], row['reserved']) == (40, 60)
        job = row['jobs'][0]

        locations_response = client.get('/api/production/stock-preparation/locations')
        assert locations_response.status_code == 200, locations_response.text
        location_row = next(
            item for item in locations_response.json()['items']
            if item.get('area_code') == 'FIN-001' and item.get('is_empty')
        )
        location_id = location_row['id']
        layout_version = location_row['layout_version']
        complete = _action(
            client, row, 'complete', 'a0002-r3-complete',
            job_id=job['id'], job_version=job['version'], actual_output=60,
            location_id=location_id, layout_version=layout_version,
        )
        assert complete.status_code == 200, complete.text
        assert _action(
            client, row, 'complete', 'a0002-r3-complete',
            job_id=job['id'], job_version=job['version'], actual_output=60,
            location_id=location_id, layout_version=layout_version,
        ).json() == complete.json()

        row = _row(client, receipt_id)
        assert (row['available'], row['reserved']) == (40, 0)
        with factory() as db:
            allocation = db.scalar(select(IncomingReceiptPurposeAllocation).where(
                IncomingReceiptPurposeAllocation.incoming_receipt_item_id == receipt_id
            ))
            source_lot = db.get(InventoryLot, allocation.semi_finished_inventory_lot_id)
            job_record = db.get(StockPreparationJob, job['id'])
            output = db.get(InventoryLot, job_record.output_lot_id)
            assert (source_lot.quantity_available, source_lot.quantity_reserved,
                    source_lot.quantity_consumed) == (40, 0, 60)
            assert output.quantity_available == 60
            assert output.finished_detail.product_id == job_record.product_id
            assert output.estimated_unit_cost_snapshot == source_lot.estimated_unit_cost_snapshot
            assert (output.source_ref_type, output.source_ref_id) == (
                'stock_preparation', job_record.id
            )
            history = history_rows(db)
            assert len(history) == 1 and history[0]['can_revert']
            history_row = history[0]

        blocked = client.put(
            f'/api/incoming/receipt-items/{receipt_id}/revert', json={}
        )
        assert blocked.status_code == 409
        assert blocked.json()['detail']['code'] == 'RESERVE_INVENTORY_ALREADY_USED'

        reverse_payload = dict(
            operation_key='a0002-r3-reverse', confirm_unused=True,
            jobs=history_row['reverse_versions'],
        )
        reverse_url = (
            '/api/production/stock-preparation/completions/'
            f"{history_row['preparation_key']}/revert"
        )
        reversed_response = client.post(reverse_url, json=reverse_payload)
        assert reversed_response.status_code == 200, reversed_response.text
        assert client.post(reverse_url, json=reverse_payload).json() == reversed_response.json()
        with factory() as db:
            allocation = db.scalar(select(IncomingReceiptPurposeAllocation).where(
                IncomingReceiptPurposeAllocation.incoming_receipt_item_id == receipt_id
            ))
            source_lot = db.get(InventoryLot, allocation.semi_finished_inventory_lot_id)
            output = db.get(InventoryLot, db.get(StockPreparationJob, job['id']).output_lot_id)
            assert (source_lot.quantity_available, source_lot.quantity_reserved,
                    source_lot.quantity_consumed) == (100, 0, 0)
            assert output.status == 'closed' and output.quantity_available == 0
            assert db.scalar(select(func.count()).select_from(StockPreparationJob)) == 1

        from app.api import stock_preparation as api
        monkeypatch.setattr(api, 'has_unrestricted_customer_access', lambda *args: False)
        monkeypatch.setattr(api, 'customer_scope_ids', lambda *args: [])
        assert client.get('/api/production/stock-preparation').json()['total'] == 0

    assert _posted_finished_quantity(factory) == 500
