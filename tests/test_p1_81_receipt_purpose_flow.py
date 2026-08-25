from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from tests.test_p1_80_purchase_purpose_allocation import (
    _created_order_id,
    _prepare_order_item,
    _preview_supplier_order_draft,
    _save_draft,
    _selection,
    _set_purpose_plan,
)
from tests.test_phase11_requisition import (
    _add_pending_candidate,
    _login,
    requisition_app,
)


@dataclass(frozen=True)
class FrozenSource:
    source_key: str
    route_key: str
    supplier_item_id: int
    source_version: int
    purpose_snapshot_id: int
    purpose_snapshot_version: int
    receipt_plan_fingerprint: str
    component_type: str
    material_id: int


def _error_code(response) -> str:
    detail = response.json().get("detail")
    assert isinstance(detail, dict), response.text
    code = detail.get("code")
    assert isinstance(code, str) and code, response.text
    return code


def _use_p181_published_map_identity(monkeypatch) -> None:
    import app.services.location_candidates as location_candidates

    identity = {
        "revision": "p181-anonymous-map-v1",
        "zones_by_id": {
            "zone-p181-1f-dispatch": "DISPATCH",
            "zone-p181-1f-fin-001": "FIN-001",
            "zone-p181-1f-a1": "A1",
        },
        "zone_ids_by_area": {
            "DISPATCH": ("zone-p181-1f-dispatch",),
            "FIN-001": ("zone-p181-1f-fin-001",),
            "A1": ("zone-p181-1f-a1",),
        },
    }
    monkeypatch.setattr(
        location_candidates,
        "load_warehouse_twin_published_floor_identity",
        lambda _floor_number: identity,
    )


@pytest.fixture(autouse=True)
def _p181_published_map_identity(monkeypatch) -> None:
    """Keep every isolated P1-81 receipt on one anonymous current map."""

    _use_p181_published_map_identity(monkeypatch)


def _seed_material_and_staging(session_factory) -> int:
    from app.models.material import Material
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.warehouse_inventory import (
        Floor3LocationLayout,
        WarehouseArea,
        WarehouseAreaStoragePolicy,
        WarehouseFloor,
        WarehouseGroundLayoutPlan,
        WarehouseGroundLayoutSlot,
        WarehouseLocation,
    )

    with session_factory() as session:
        material = Material(
            code="KAKAK",
            paper_composition="P1-81 匿名正式材质",
            layer_count=5,
            flute_type="AB",
            # Normal receipt freezes this material-master purchase contract.
            # Later master edits must not rewrite the frozen receipt fact.
            quote_price=Decimal("99.9900"),
            price_unit="per_sheet",
            purchase_currency="CNY",
            purchase_tax_included=True,
            purchase_tax_rate=Decimal("0.13"),
            supplier_name="苏州纸板供应商",
            is_active=True,
            version=1,
        )
        session.add(material)
        session.flush()

        item = session.get(OrderItem, 1)
        assert item is not None
        product = session.get(Product, item.product_id)
        assert product is not None
        item.material_id = material.id
        item.snapshot_material = material.code
        item.layer_count = 5
        item.flute_type = "AB"
        product.material_id = material.id

        floor = WarehouseFloor(
            floor_code="P181-F1",
            floor_name="P1-81 匿名一楼",
            floor_number=1,
            construction_status="enabled",
        )
        session.add(floor)
        session.flush()
        dispatch_area = WarehouseArea(
            floor_id=floor.id,
            area_code="DISPATCH",
            area_name="匿名成品暂存区",
            construction_status="enabled",
        )
        dispatch_area.storage_policy = WarehouseAreaStoragePolicy(
            map_feature_id="zone-p181-1f-dispatch",
            allowed_inventory_types_json='["finished"]',
            storage_layout="pallet_ground",
            status="published",
            published_map_revision="p181-anonymous-map-v1",
            version=1,
        )
        fin_area = WarehouseArea(
            floor_id=floor.id,
            area_code="FIN-001",
            area_name="匿名真实成品待送区",
            construction_status="enabled",
        )
        fin_area.storage_policy = WarehouseAreaStoragePolicy(
            map_feature_id="zone-p181-1f-fin-001",
            allowed_inventory_types_json='["finished"]',
            storage_layout="pallet_ground",
            status="published",
            published_map_revision="p181-anonymous-map-v1",
            version=1,
        )
        raw_area = WarehouseArea(
            floor_id=floor.id,
            area_code="A1",
            area_name="匿名原料暂存区",
            construction_status="enabled",
        )
        raw_area.storage_policy = WarehouseAreaStoragePolicy(
            map_feature_id="zone-p181-1f-a1",
            allowed_inventory_types_json='["raw_material"]',
            storage_layout="pallet_ground",
            status="published",
            published_map_revision="p181-anonymous-map-v1",
            version=1,
        )
        dispatch_location = WarehouseLocation(
            location_code="F1-DISPATCH-01",
            location_name="合并一楼成品暂存区",
            warehouse_type="finished",
            is_active=True,
            warehouse_floor=1,
            area_code="DISPATCH",
            storage_type="temporary_aisle",
            placement_status="placed",
            is_temporary=True,
            source_version="P1-25C",
        )
        dispatch_location.floor3_layout = Floor3LocationLayout(
            left_pct=Decimal("4"),
            top_pct=Decimal("4"),
            width_pct=Decimal("12"),
            height_pct=Decimal("12"),
            z_index=0,
            version=1,
            source_type="manual",
            layout_kind="logical_anchor",
        )
        fin_locations: list[WarehouseLocation] = []
        for index in range(1, 9):
            fin_location = WarehouseLocation(
                location_code=f"F1-FIN-001-L{index:03d}",
                location_name=f"成品待送堆放区 {index:03d} 号位",
                warehouse_type="finished",
                is_active=True,
                warehouse_floor=1,
                area_code="FIN-001",
                storage_type="ground",
                placement_status="placed",
                source_version="TWIN_V1",
            )
            fin_location.floor3_layout = Floor3LocationLayout(
                left_pct=Decimal(str(24 + index * 7)),
                top_pct=Decimal("4"),
                width_pct=Decimal("6"),
                height_pct=Decimal("12"),
                z_index=index,
                version=2,
                source_type="manual",
                layout_kind="physical_pallet",
            )
            fin_locations.append(fin_location)
        raw_location = WarehouseLocation(
            location_code="P181-RAW-STAGE",
            location_name="一楼半成品原料暂存区",
            warehouse_type="semi_finished",
            is_active=True,
            warehouse_floor=1,
            area_code="A1",
            storage_type="ground",
            placement_status="placed",
            is_temporary=True,
            source_version="P1-81",
        )
        raw_location.floor3_layout = Floor3LocationLayout(
            left_pct=Decimal("20"),
            top_pct=Decimal("4"),
            width_pct=Decimal("12"),
            height_pct=Decimal("12"),
            z_index=0,
            version=1,
            source_type="manual",
            layout_kind="logical_anchor",
        )
        session.add_all(
            [
                dispatch_area,
                fin_area,
                raw_area,
                dispatch_location,
                *fin_locations,
                raw_location,
            ]
        )
        session.flush()
        fin_plan = WarehouseGroundLayoutPlan(
            area_id=fin_area.id,
            status="published",
            target_slot_count=len(fin_locations),
            numbering_origin="south",
            row_direction="from_aisle_inward",
            slot_direction="left_to_right",
            row_start_no=1,
            slot_start_no=1,
            draft_map_revision="p181-anonymous-map-v1",
            published_map_revision="p181-anonymous-map-v1",
            preview_fingerprint="a" * 64,
            version=1,
            publish_idempotency_key="p181-fin-ground-publish",
            publish_request_hash="b" * 64,
            updated_by=1,
            published_by=1,
            published_at=datetime.now(),
        )
        session.add(fin_plan)
        session.flush()
        for index, fin_location in enumerate(fin_locations, start=1):
            session.add(
                WarehouseGroundLayoutSlot(
                    plan_id=fin_plan.id,
                    location_id=fin_location.id,
                    route_sequence=index,
                    row_no=1,
                    slot_no=index,
                    x_mm=Decimal(str(1000 + (index - 1) * 1200)),
                    y_mm=Decimal("1000"),
                    width_mm=1200,
                    depth_mm=1000,
                )
            )
        session.commit()
        return material.id


def _create_frozen_sources(
    client: TestClient,
    session_factory,
    *,
    order_quantity: int,
    purchase_total: int,
    order_purpose: int,
    stock_purpose: int,
    cutting_mode: str = "一开一",
    composite: bool = False,
    composite_reserve_purpose: int = 0,
) -> list[FrozenSource]:
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.supplier_requisition_order import (
        PurchasePurposeSourceSnapshot,
        SupplierRequisitionOrderItem,
    )

    _prepare_order_item(
        session_factory,
        quantity=order_quantity,
        cutting_mode=cutting_mode,
        pieces_per_box=1,
    )
    if composite:
        with session_factory() as session:
            item = session.get(OrderItem, 1)
            assert item is not None
            product = session.get(Product, item.product_id)
            assert product is not None
            product.box_style = "A3 天地盖"
            item.snapshot_product_name = "P1-81 匿名天地盖"
            item.snapshot_base_report_length_mm = 780
            item.snapshot_base_report_width_mm = 190
            session.commit()

    draft = _preview_supplier_order_draft(
        client,
        [_selection(cutting_mode=cutting_mode)],
    )
    lines = draft["supplier_groups"][0]["lines"]
    if composite:
        assert len(lines) == 2
        for line in lines:
            component = line["source_items"][0]["component_type"]
            reserve = composite_reserve_purpose if component == "base" else 0
            _set_purpose_plan(
                line,
                purchase_total=order_purpose + reserve,
                order_purpose=order_purpose,
                stock_purpose=reserve,
            )
    else:
        assert len(lines) == 1
        _set_purpose_plan(
            lines[0],
            purchase_total=purchase_total,
            order_purpose=order_purpose,
            stock_purpose=stock_purpose,
        )
    saved = _save_draft(client, draft)
    assert saved.status_code == 201, saved.text
    order_id = _created_order_id(saved)

    with session_factory() as session:
        snapshots = list(
            session.scalars(
                select(PurchasePurposeSourceSnapshot)
                .join(
                    SupplierRequisitionOrderItem,
                    SupplierRequisitionOrderItem.id
                    == PurchasePurposeSourceSnapshot.supplier_requisition_order_item_id,
                )
                .where(SupplierRequisitionOrderItem.supplier_order_id == order_id)
                .order_by(PurchasePurposeSourceSnapshot.id)
            )
        )
        result: list[FrozenSource] = []
        for snapshot in snapshots:
            supplier_item = session.get(
                SupplierRequisitionOrderItem,
                snapshot.supplier_requisition_order_item_id,
            )
            assert supplier_item is not None
            assert supplier_item.material_id is not None
            result.append(
                FrozenSource(
                    source_key=snapshot.source_key,
                    route_key=f"so{supplier_item.id}",
                    supplier_item_id=supplier_item.id,
                    source_version=supplier_item.version,
                    purpose_snapshot_id=snapshot.id,
                    purpose_snapshot_version=snapshot.snapshot_version,
                    receipt_plan_fingerprint=snapshot.preview_fingerprint,
                    component_type=snapshot.component_type,
                    material_id=supplier_item.material_id,
                )
            )
    assert len(result) == (2 if composite else 1)
    return result


def _create_frozen_source_batch(
    client: TestClient,
    session_factory,
    *,
    count: int,
) -> list[FrozenSource]:
    """Create distinct physical lines so batch-query tests cannot collapse them."""

    from app.models.supplier_requisition_order import (
        PurchasePurposeSourceSnapshot,
        SupplierRequisitionOrderItem,
    )

    assert count >= 1
    item_ids = [1]
    for index in range(1, count):
        item_ids.append(
            _add_pending_candidate(
                session_factory,
                300 + index,
                product_code=f"P181-BATCH-{index:02d}",
                product_name=f"匿名批量收料纸箱{index:02d}",
                quantity=10,
                material_code="KAKAK",
                layer_count=5,
                flute_type="AB",
            )
        )
    for index, item_id in enumerate(item_ids):
        _prepare_order_item(
            session_factory,
            item_id=item_id,
            quantity=10,
            report_length_mm=800 + index,
            report_width_mm=200,
        )
    selections = [
        _selection(
            item_id=item_id,
            report_length_mm=800 + index,
            report_width_mm=200,
        )
        for index, item_id in enumerate(item_ids)
    ]
    draft = _preview_supplier_order_draft(client, selections)
    lines = [
        line
        for group in draft["supplier_groups"]
        for line in group["lines"]
    ]
    assert len(lines) == count
    for line in lines:
        _set_purpose_plan(
            line,
            purchase_total=10,
            order_purpose=10,
            stock_purpose=0,
        )
    saved = _save_draft(client, draft)
    assert saved.status_code == 201, saved.text
    order_id = _created_order_id(saved)

    with session_factory() as session:
        snapshots = list(
            session.scalars(
                select(PurchasePurposeSourceSnapshot)
                .join(
                    SupplierRequisitionOrderItem,
                    SupplierRequisitionOrderItem.id
                    == PurchasePurposeSourceSnapshot.supplier_requisition_order_item_id,
                )
                .where(SupplierRequisitionOrderItem.supplier_order_id == order_id)
                .order_by(PurchasePurposeSourceSnapshot.id)
            )
        )
        result: list[FrozenSource] = []
        for snapshot in snapshots:
            supplier_item = session.get(
                SupplierRequisitionOrderItem,
                snapshot.supplier_requisition_order_item_id,
            )
            assert supplier_item is not None and supplier_item.material_id is not None
            result.append(
                FrozenSource(
                    source_key=snapshot.source_key,
                    route_key=f"so{supplier_item.id}",
                    supplier_item_id=supplier_item.id,
                    source_version=supplier_item.version,
                    purpose_snapshot_id=snapshot.id,
                    purpose_snapshot_version=snapshot.snapshot_version,
                    receipt_plan_fingerprint=snapshot.preview_fingerprint,
                    component_type=snapshot.component_type,
                    material_id=supplier_item.material_id,
                )
            )
    assert len(result) == count
    return result


def _freeze_receipt_fact(
    client: TestClient,
    source: FrozenSource,
    *,
    idempotency_key: str,
    unit_price: str = "2.5000",
    price_unit: str = "per_sheet",
    currency: str = "CNY",
    tax_included: bool = True,
    tax_rate: str = "0.13",
    actual_material_id: int | None = None,
    material_change_confirmed: bool = False,
    material_variance_approval_id: int | None = None,
    expected_source_version: int | None = None,
    expected_latest_receipt_fact_version: int = 0,
):
    response = client.put(
        "/api/requisition/purchase-sources/"
        f"{quote(source.source_key, safe='')}/receipt-facts",
        json={
            "actual_material_id": actual_material_id or source.material_id,
            "unit_price": unit_price,
            "currency": currency,
            "price_unit": price_unit,
            "tax_included": tax_included,
            "tax_rate": tax_rate,
            "purchase_purpose_source_snapshot_id": source.purpose_snapshot_id,
            "purpose_snapshot_version": source.purpose_snapshot_version,
            "receipt_plan_fingerprint": source.receipt_plan_fingerprint,
            "expected_source_version": (
                source.source_version
                if expected_source_version is None
                else expected_source_version
            ),
            "expected_latest_receipt_fact_version": (
                expected_latest_receipt_fact_version
            ),
            "idempotency_key": idempotency_key,
            **(
                {"material_variance_approval_id": material_variance_approval_id}
                if material_variance_approval_id is not None
                else {}
            ),
        },
    )
    return response


def _receive(
    client: TestClient,
    source: FrozenSource,
    receipt_fact: dict,
    *,
    quantity: int,
    idempotency_key: str,
    overrides: dict | None = None,
):
    payload = {
        "received_quantity": quantity,
        "idempotency_key": idempotency_key,
        "expected_receipt_fact_version": receipt_fact["receipt_fact_version"],
        "purchase_purpose_source_snapshot_id": source.purpose_snapshot_id,
        "expected_purpose_snapshot_version": source.purpose_snapshot_version,
        "receipt_plan_fingerprint": receipt_fact["receipt_plan_fingerprint"],
        "expected_actual_material_version": receipt_fact["actual_material_version"],
        "actual_material_fingerprint": receipt_fact["actual_material_fingerprint"],
    }
    payload.update(overrides or {})
    return client.put(f"/api/incoming/receive/{source.route_key}", json=payload)


def _business_counts(session_factory) -> dict[str, int]:
    from app.models.audit import OperationLog
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.production import ProductionCompletion, ProductionTask
    from app.models.warehouse_inventory import InventoryLot, InventoryMovement

    models = {
        "receipt": IncomingReceipt,
        "receipt_item": IncomingReceiptItem,
        "task": ProductionTask,
        "completion": ProductionCompletion,
        "lot": InventoryLot,
        "movement": InventoryMovement,
        "audit": OperationLog,
    }
    with session_factory() as session:
        return {
            key: int(session.scalar(select(func.count(model.id))) or 0)
            for key, model in models.items()
        }


def _add_changed_material(session_factory) -> int:
    from app.models.material import Material

    with session_factory() as session:
        material = Material(
            code="ABABA",
            paper_composition="P1-81 匿名实际替代材质",
            layer_count=5,
            flute_type="AB",
            quote_price=Decimal("88.8800"),
            price_unit="per_sheet",
            purchase_currency="USD",
            purchase_tax_included=False,
            purchase_tax_rate=Decimal("0.06"),
            supplier_name="苏州纸板供应商",
            is_active=True,
            version=1,
        )
        session.add(material)
        session.commit()
        return material.id


def _posted_finished_quantity(session_factory, *, order_item_id: int = 1) -> int:
    from app.models.production import ProductionCompletion

    with session_factory() as session:
        return int(
            session.scalar(
                select(func.sum(ProductionCompletion.actual_output_quantity)).where(
                    ProductionCompletion.order_item_id == order_item_id,
                    ProductionCompletion.status == "posted",
                )
            )
            or 0
        )


def _active_semi_quantity(session_factory) -> int:
    from app.models.warehouse_inventory import InventoryLot

    with session_factory() as session:
        return int(
            session.scalar(
                select(
                    func.sum(
                        InventoryLot.quantity_available
                        + InventoryLot.quantity_reserved
                        + InventoryLot.quantity_consumed
                    )
                ).where(
                    InventoryLot.inventory_type == "semi_finished",
                    InventoryLot.status.in_(("active", "frozen")),
                )
            )
            or 0
        )


def _assert_allocation(
    response,
    *,
    order_delta: int,
    reserve_delta: int,
    order_cumulative: int,
    reserve_cumulative: int,
    finished_delta: int,
    finished_cumulative: int,
) -> dict:
    assert response.status_code == 200, response.text
    allocation = response.json()["purpose_allocation"]
    assert allocation["order_sheet_delta"] == order_delta
    assert allocation["reserve_sheet_delta"] == reserve_delta
    assert allocation["order_sheet_cumulative"] == order_cumulative
    assert allocation["reserve_sheet_cumulative"] == reserve_cumulative
    assert allocation["theoretical_finished_delta"] == finished_delta
    assert allocation["theoretical_finished_cumulative"] == finished_cumulative
    assert Decimal(str(allocation["order_cost"])) + Decimal(
        str(allocation["reserve_cost"])
    ) == Decimal(str(allocation["receipt_total_cost"]))
    assert allocation["currency"] == "CNY"
    return allocation


def test_pending_projection_refreshes_frozen_receipt_tokens_and_keeps_legacy_open(
    requisition_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=500,
            purchase_total=600,
            order_purpose=500,
            stock_purpose=100,
        )[0]

        legacy_order_item_id = _add_pending_candidate(
            session_factory,
            181,
            product_code="P181-LEGACY-PENDING",
            product_name="匿名历史待收纸箱",
            quantity=10,
        )
        with session_factory() as session:
            legacy_order = SupplierRequisitionOrder(
                order_number="SRO-P181-LEGACY-PENDING",
                supplier_name="匿名历史供应商",
                total_quantity=10,
                stock_deduction_qty=0,
                requisition_qty=10,
                status="confirmed",
                created_by=1,
            )
            session.add(legacy_order)
            session.flush()
            legacy_source = SupplierRequisitionOrderItem(
                supplier_order_id=legacy_order.id,
                order_item_id=legacy_order_item_id,
                source_key=f"order_item:{legacy_order_item_id}:whole",
                product_id=None,
                material_id=None,
                product_code="P181-LEGACY-PENDING",
                product_name="匿名历史待收纸箱",
                quantity=10,
                stock_deduction_qty=0,
                requisition_qty=10,
                cutting_mode="一开一",
                pieces_per_box=1,
                required_piece_qty=10,
                customer_name="苏州思迈尔包装有限公司",
                status="active",
                version=1,
            )
            session.add(legacy_source)
            legacy_order_item = session.get(OrderItem, legacy_order_item_id)
            assert legacy_order_item is not None
            legacy_order_item.supplier_order_number = legacy_order.order_number
            legacy_order_item.requisition_status = "供应商已排单"
            legacy_order_item.material_status = "pending"
            session.commit()
            legacy_route_key = f"so{legacy_source.id}"

        pending_before = client.get("/api/incoming/pending")
        assert pending_before.status_code == 200, pending_before.text
        before_rows = {
            str(row["item_id"]): row for row in pending_before.json()["items"]
        }
        frozen_before = before_rows[source.route_key]
        assert frozen_before["purpose_status"] == "frozen"
        assert frozen_before["receipt_fact_ready"] is False
        assert frozen_before["expected_receipt_fact_version"] is None
        assert frozen_before["expected_purpose_snapshot_version"] == 1
        assert frozen_before["receipt_plan_fingerprint"] == (
            source.receipt_plan_fingerprint
        )
        assert frozen_before["expected_order_purpose_sheet_qty"] == 500
        assert frozen_before["expected_reserve_purpose_sheet_qty"] == 100
        assert frozen_before["expected_finished_output_qty"] == 500
        assert frozen_before["finished_location_name"] == "成品待送堆放区 001 号位"
        assert frozen_before["reserve_location_name"] == "一楼半成品原料暂存区"
        assert frozen_before["purpose_issue"]

        legacy_before = before_rows[legacy_route_key]
        assert legacy_before["purpose_status"] == "legacy_unset"
        assert legacy_before["receipt_fact_ready"] is True
        assert "purpose_issue" not in legacy_before
        assert legacy_before.get("expected_receipt_fact_version") is None
        assert legacy_before.get("receipt_plan_fingerprint") is None

        frozen_fact = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p181-price-pending-projection",
        )
        assert frozen_fact.status_code == 200, frozen_fact.text
        fact = frozen_fact.json()

        pending_after = client.get("/api/incoming/pending")
        assert pending_after.status_code == 200, pending_after.text
        after_rows = {
            str(row["item_id"]): row for row in pending_after.json()["items"]
        }
        frozen_after = after_rows[source.route_key]
        assert frozen_after["purpose_status"] == "frozen"
        assert frozen_after["receipt_fact_ready"] is True
        assert frozen_after["expected_receipt_fact_version"] == fact[
            "receipt_fact_version"
        ]
        assert frozen_after["expected_purpose_snapshot_version"] == fact[
            "purpose_snapshot_version"
        ]
        assert frozen_after["receipt_plan_fingerprint"] == fact[
            "receipt_plan_fingerprint"
        ]
        assert frozen_after["expected_order_purpose_sheet_qty"] == 500
        assert frozen_after["expected_reserve_purpose_sheet_qty"] == 100
        assert frozen_after["expected_finished_output_qty"] == 500
        assert frozen_after["finished_location_name"] == "成品待送堆放区 001 号位"
        assert frozen_after["reserve_location_name"] == "一楼半成品原料暂存区"
        assert "purpose_issue" not in frozen_after

        legacy_after = after_rows[legacy_route_key]
        assert legacy_after["purpose_status"] == "legacy_unset"
        assert legacy_after["receipt_fact_ready"] is True
        assert "purpose_issue" not in legacy_after


@pytest.mark.parametrize("invalid_fact", ("unpublished", "missing_geometry"))
def test_pending_frozen_preview_uses_live_published_location_facts(
    requisition_app,
    invalid_fact: str,
) -> None:
    from app.models.warehouse_inventory import (
        Floor3LocationLayout,
        WarehouseArea,
        WarehouseAreaStoragePolicy,
        WarehouseFloor,
        WarehouseLocation,
    )

    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=500,
            purchase_total=600,
            order_purpose=500,
            stock_purpose=100,
        )[0]
        frozen = _freeze_receipt_fact(
            client,
            source,
            idempotency_key=f"p181-price-location-preview-{invalid_fact}",
        )
        assert frozen.status_code == 200, frozen.text

        with session_factory() as session:
            if invalid_fact == "unpublished":
                floor = session.scalar(
                    select(WarehouseFloor).where(WarehouseFloor.floor_number == 1)
                )
                assert floor is not None
                area = session.scalar(
                    select(WarehouseArea).where(
                        WarehouseArea.floor_id == floor.id,
                        WarehouseArea.area_code == "FIN-001",
                    )
                )
                assert area is not None
                policy = session.scalar(
                    select(WarehouseAreaStoragePolicy).where(
                        WarehouseAreaStoragePolicy.area_id == area.id
                    )
                )
                assert policy is not None
                policy.status = "draft"
                policy.published_map_revision = None
            else:
                fin_location_ids = list(
                    session.scalars(
                        select(WarehouseLocation.id).where(
                            WarehouseLocation.area_code == "FIN-001"
                        )
                    )
                )
                geometries = list(
                    session.scalars(
                        select(Floor3LocationLayout).where(
                            Floor3LocationLayout.location_id.in_(fin_location_ids)
                        )
                    )
                )
                assert geometries
                for geometry in geometries:
                    session.delete(geometry)
            session.commit()

        pending = client.get("/api/incoming/pending")
        assert pending.status_code == 200, pending.text
        row = next(
            item
            for item in pending.json()["items"]
            if str(item["item_id"]) == source.route_key
        )
        assert row["purpose_status"] == "frozen"
        assert row["receipt_fact_ready"] is False
        assert row["finished_location_ready"] is False
        assert row["finished_location_issue"]


def test_pending_frozen_preview_reports_capacity_warning_without_blocking(
    requisition_app,
) -> None:
    from app.models.warehouse_inventory import (
        InventoryPallet,
        WarehouseArea,
        WarehouseFloor,
        WarehouseLocation,
    )

    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=500,
            purchase_total=600,
            order_purpose=500,
            stock_purpose=100,
        )[0]
        frozen = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p181-price-capacity-preview",
        )
        assert frozen.status_code == 200, frozen.text

        with session_factory() as session:
            floor = session.scalar(
                select(WarehouseFloor).where(WarehouseFloor.floor_number == 1)
            )
            assert floor is not None
            area = session.scalar(
                select(WarehouseArea).where(
                    WarehouseArea.floor_id == floor.id,
                    WarehouseArea.area_code == "FIN-001",
                )
            )
            location = session.scalar(
                select(WarehouseLocation).where(
                    WarehouseLocation.location_code == "F1-FIN-001-L001"
                )
            )
            assert area is not None and location is not None
            area.capacity_review_status = "confirmed"
            area.capacity_eligible = True
            area.confirmed_pallet_capacity = 1
            area.capacity_reviewed_by = "匿名容量复核员"
            area.capacity_reviewed_at = datetime.now()
            session.add(
                InventoryPallet(
                    pallet_code="PLT-P181-CAPACITY-FULL",
                    location_id=location.id,
                    location_occupancy_key="P181-CAPACITY-FULL",
                    status="active",
                    is_current=True,
                    needs_relocation=False,
                    version=1,
                )
            )
            session.commit()

        pending = client.get("/api/incoming/pending")
        assert pending.status_code == 200, pending.text
        row = next(
            item
            for item in pending.json()["items"]
            if str(item["item_id"]) == source.route_key
        )
        assert row["receipt_fact_ready"] is True
        assert row["finished_location_name"] == "成品待送堆放区 002 号位"
        assert row["finished_location_ready"] is True
        assert row["finished_capacity_warning"]


def test_frozen_500_600_receipts_split_450_580_600_620_and_cost_exactly(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)

    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=500,
            purchase_total=600,
            order_purpose=500,
            stock_purpose=100,
        )[0]
        frozen = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p181-price-500-600",
        )
        assert frozen.status_code == 200, frozen.text
        receipt_fact = frozen.json()
        assert receipt_fact["unit_price"] == "2.5000"
        assert receipt_fact["actual_material_id"] == source.material_id
        assert receipt_fact["purpose_snapshot_version"] == 1
        assert len(receipt_fact["receipt_plan_fingerprint"]) == 64

        first = _assert_allocation(
            _receive(
                client,
                source,
                receipt_fact,
                quantity=450,
                idempotency_key="p181-receive-450",
            ),
            order_delta=450,
            reserve_delta=0,
            order_cumulative=450,
            reserve_cumulative=0,
            finished_delta=450,
            finished_cumulative=450,
        )
        second = _assert_allocation(
            _receive(
                client,
                source,
                receipt_fact,
                quantity=130,
                idempotency_key="p181-receive-580",
            ),
            order_delta=50,
            reserve_delta=80,
            order_cumulative=500,
            reserve_cumulative=80,
            finished_delta=50,
            finished_cumulative=500,
        )
        third = _assert_allocation(
            _receive(
                client,
                source,
                receipt_fact,
                quantity=20,
                idempotency_key="p181-receive-600",
            ),
            order_delta=0,
            reserve_delta=20,
            order_cumulative=500,
            reserve_cumulative=100,
            finished_delta=0,
            finished_cumulative=500,
        )
        fourth = _assert_allocation(
            _receive(
                client,
                source,
                receipt_fact,
                quantity=20,
                idempotency_key="p181-receive-620",
            ),
            order_delta=0,
            reserve_delta=20,
            order_cumulative=500,
            reserve_cumulative=120,
            finished_delta=0,
            finished_cumulative=500,
        )

    assert Decimal(str(first["receipt_total_cost"])) == Decimal("1125.0000")
    assert Decimal(str(second["order_cost"])) == Decimal("125.0000")
    assert Decimal(str(second["reserve_cost"])) == Decimal("200.0000")
    assert Decimal(str(third["reserve_cost"])) == Decimal("50.0000")
    assert Decimal(str(fourth["reserve_cost"])) == Decimal("50.0000")
    assert fourth["reserve_planned_sheet_qty"] == 100
    assert fourth["reserve_actual_sheet_qty"] == 120
    assert fourth["reserve_variance_sheet_qty"] == 20
    assert fourth["finished_location_name"].startswith("成品待送堆放区 ")
    assert fourth["reserve_location_name"] == "一楼半成品原料暂存区"
    assert "F1-DISPATCH-01" not in fourth["finished_location_name"]

    from app.models.production import ProductionTask
    from app.models.warehouse_inventory import InventoryLot, WarehouseLocation

    with session_factory() as session:
        tasks = list(
            session.scalars(
                select(ProductionTask).where(ProductionTask.order_item_id == 1)
            )
        )
        assert len(tasks) == 1
        assert tasks[0].planned_quantity == 500
        fin_location_ids = set(
            session.scalars(
                select(WarehouseLocation.id).where(
                    WarehouseLocation.area_code == "FIN-001"
                )
            )
        )
        finished_lots = list(
            session.scalars(
                select(InventoryLot).where(
                    InventoryLot.inventory_type == "finished",
                    InventoryLot.status == "active",
                )
            )
        )
        assert finished_lots
        assert {lot.warehouse_location_id for lot in finished_lots}.issubset(
            fin_location_ids
        )
    assert _posted_finished_quantity(session_factory) == 500
    assert _active_semi_quantity(session_factory) == 120


def test_received_and_history_project_purpose_reversal_with_cost_permission(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=500,
            purchase_total=600,
            order_purpose=500,
            stock_purpose=100,
        )[0]
        frozen = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p181-price-received-history",
        )
        assert frozen.status_code == 200, frozen.text
        received = _receive(
            client,
            source,
            frozen.json(),
            quantity=580,
            idempotency_key="p181-received-history",
        )
        assert received.status_code == 200, received.text
        receipt_item_id = int(received.json()["receipt_item_id"])

        admin_recent = client.get("/api/incoming/received")
        assert admin_recent.status_code == 200, admin_recent.text
        admin_row = next(
            row
            for row in admin_recent.json()["items"]
            if row.get("receipt_item_id") == receipt_item_id
        )
        assert admin_row["purpose_status"] == "frozen"
        assert admin_row["purpose_allocation"]["order_sheet_delta"] == 500
        assert admin_row["purpose_allocation"]["reserve_sheet_delta"] == 80
        for key in ("sheet_cost", "order_cost", "reserve_cost", "receipt_total_cost"):
            assert key in admin_row["purpose_allocation"]

        _login(client, "workshop")
        workshop_recent = client.get("/api/incoming/received")
        assert workshop_recent.status_code == 200, workshop_recent.text
        workshop_row = next(
            row
            for row in workshop_recent.json()["items"]
            if row.get("receipt_item_id") == receipt_item_id
        )
        assert workshop_row["purpose_allocation"]["order_sheet_delta"] == 500
        assert workshop_row["purpose_allocation"]["reserve_sheet_delta"] == 80
        for key in ("sheet_cost", "order_cost", "reserve_cost", "receipt_total_cost"):
            assert key not in workshop_row["purpose_allocation"]

        _login(client, "admin")
        reverted = client.put(
            f"/api/incoming/receipt-items/{receipt_item_id}/revert",
            json={"reason": "匿名历史投影验证"},
        )
        assert reverted.status_code == 200, reverted.text
        admin_history = client.get("/api/incoming/history")
        assert admin_history.status_code == 200, admin_history.text
        history_row = next(
            row
            for row in admin_history.json()["items"]
            if row.get("receipt_item_id") == receipt_item_id
        )
        assert history_row["purpose_allocation"]["order_sheet_delta"] == 500
        assert history_row["purpose_allocation"]["reserve_sheet_delta"] == 80
        assert history_row["purpose_reversal"]["order_sheet_cumulative"] == 0
        assert history_row["purpose_reversal"]["reserve_sheet_cumulative"] == 0
        assert "order_cost" in history_row["purpose_allocation"]

        _login(client, "workshop")
        workshop_history = client.get("/api/incoming/history")
        assert workshop_history.status_code == 200, workshop_history.text
        workshop_history_row = next(
            row
            for row in workshop_history.json()["items"]
            if row.get("receipt_item_id") == receipt_item_id
        )
        assert workshop_history_row["purpose_reversal"][
            "order_sheet_cumulative"
        ] == 0
        assert "order_cost" not in workshop_history_row["purpose_allocation"]


def test_bom_same_stable_component_partial_sources_sum_before_required_minimum(
    requisition_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot
    from app.services import receipt_purpose_distribution

    first_partial = PurchasePurposeSourceSnapshot(
        id=18101,
        source_bom_requisition_source_id=9001,
        component_type="whole",
        yield_per_sheet_snapshot=1,
        pieces_per_finished_snapshot=1,
    )
    second_partial = PurchasePurposeSourceSnapshot(
        id=18102,
        source_bom_requisition_source_id=9001,
        component_type="whole",
        yield_per_sheet_snapshot=1,
        pieces_per_finished_snapshot=1,
    )
    other_required_component = PurchasePurposeSourceSnapshot(
        id=18103,
        source_bom_requisition_source_id=9002,
        component_type="whole",
        yield_per_sheet_snapshot=1,
        pieces_per_finished_snapshot=1,
    )
    snapshots = [first_partial, second_partial, other_required_component]
    monkeypatch.setattr(
        receipt_purpose_distribution,
        "_component_key",
        lambda _db, snapshot: (
            "bom:stable-component-a:whole"
            if snapshot.id in {18101, 18102}
            else "bom:stable-component-b:whole"
        ),
    )

    _, session_factory = requisition_app
    with session_factory() as session:
        assert receipt_purpose_distribution._finished_capacity(
            session,
            snapshots,
            {18101: 4, 18102: 6, 18103: 10},
        ) == 10


def test_one_sheet_two_forms_500_finished_and_only_50_reserve_sheets(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=500,
            purchase_total=300,
            order_purpose=250,
            stock_purpose=50,
            cutting_mode="一开二",
        )[0]
        frozen = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p181-price-yield-two",
        )
        assert frozen.status_code == 200, frozen.text
        result = _assert_allocation(
            _receive(
                client,
                source,
                frozen.json(),
                quantity=300,
                idempotency_key="p181-receive-yield-two",
            ),
            order_delta=250,
            reserve_delta=50,
            order_cumulative=250,
            reserve_cumulative=50,
            finished_delta=500,
            finished_cumulative=500,
        )
    assert Decimal(str(result["receipt_total_cost"])) == Decimal(
        str(result["order_cost"])
    ) + Decimal(str(result["reserve_cost"]))
    assert _posted_finished_quantity(session_factory) == 500
    assert _active_semi_quantity(session_factory) == 50


def test_a3_cover_and_base_use_min_component_capacity_not_sum(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        sources = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=500,
            purchase_total=1000,
            order_purpose=500,
            stock_purpose=0,
            composite=True,
        )
        by_component = {source.component_type: source for source in sources}
        assert set(by_component) == {"cover", "base"}
        facts = {}
        for component, source in by_component.items():
            response = _freeze_receipt_fact(
                client,
                source,
                idempotency_key=f"p181-price-a3-{component}",
            )
            assert response.status_code == 200, response.text
            facts[component] = response.json()

        cover = _assert_allocation(
            _receive(
                client,
                by_component["cover"],
                facts["cover"],
                quantity=500,
                idempotency_key="p181-receive-a3-cover",
            ),
            order_delta=500,
            reserve_delta=0,
            order_cumulative=500,
            reserve_cumulative=0,
            finished_delta=0,
            finished_cumulative=0,
        )
        base = _assert_allocation(
            _receive(
                client,
                by_component["base"],
                facts["base"],
                quantity=500,
                idempotency_key="p181-receive-a3-base",
            ),
            order_delta=500,
            reserve_delta=0,
            order_cumulative=500,
            reserve_cumulative=0,
            finished_delta=500,
            finished_cumulative=500,
        )
    assert cover["theoretical_finished_cumulative"] == 0
    assert base["theoretical_finished_cumulative"] == 500
    assert _posted_finished_quantity(session_factory) == 500


def test_a3_remaining_item_can_finish_receipt_after_sibling_was_dispatched(
    requisition_app,
) -> None:
    """An order-level partial delivery must not close untouched sibling lines."""

    from datetime import date

    from app.models.delivery import Delivery, DeliveryItem
    from app.models.order import Order, OrderItem
    from app.models.production import ProductionCompletion

    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        sources = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=100,
            purchase_total=200,
            order_purpose=100,
            stock_purpose=0,
            composite=True,
        )
        by_component = {source.component_type: source for source in sources}
        facts = {
            component: _freeze_receipt_fact(
                client,
                source,
                idempotency_key=f"p1101-partial-price-{component}",
            ).json()
            for component, source in by_component.items()
        }

        with session_factory() as session:
            target = session.get(OrderItem, 1)
            assert target is not None
            order = session.get(Order, target.order_id)
            assert order is not None
            sibling = OrderItem(
                order_id=order.id,
                product_id=target.product_id,
                quantity=7,
                unit_price=target.unit_price,
                subtotal=target.unit_price * 7,
                material_status="received",
                requisition_status="已入库",
                delivered_quantity=7,
                snapshot_product_name="匿名已送兄弟明细",
                snapshot_spec=target.snapshot_spec,
                snapshot_material=target.snapshot_material,
            )
            session.add(sibling)
            session.flush()
            delivery = Delivery(
                delivery_number="P1-101-ANON-DISPATCH",
                customer_id=order.customer_id,
                delivery_date=date(2026, 8, 24),
                status="dispatched",
                total_quantity=7,
            )
            session.add(delivery)
            session.flush()
            session.add(
                DeliveryItem(
                    delivery_id=delivery.id,
                    order_item_id=sibling.id,
                    delivered_quantity=7,
                    ordered_quantity_snapshot=7,
                    order_remaining_snapshot=7,
                )
            )
            order.status = "partially_delivered"
            session.commit()

        cover = _receive(
            client,
            by_component["cover"],
            facts["cover"],
            quantity=100,
            idempotency_key="p1101-partial-receive-cover",
        )
        assert cover.status_code == 200, cover.text
        assert cover.json()["purpose_allocation"]["theoretical_finished_delta"] == 0

        base = _receive(
            client,
            by_component["base"],
            facts["base"],
            quantity=100,
            idempotency_key="p1101-partial-receive-base",
        )
        assert base.status_code == 200, base.text
        assert base.json()["purpose_allocation"]["theoretical_finished_delta"] == 100

    with session_factory() as session:
        target = session.get(OrderItem, 1)
        assert target is not None
        order = session.get(Order, target.order_id)
        assert order is not None
        assert order.status == "partially_delivered"
        assert target.delivered_quantity == 0
        assert (
            session.scalar(
                select(func.sum(ProductionCompletion.actual_output_quantity)).where(
                    ProductionCompletion.order_item_id == target.id,
                    ProductionCompletion.status == "posted",
                )
            )
            == 100
        )


@pytest.mark.parametrize(
    ("order_status", "delivered_quantity", "expected_message"),
    (
        ("closed", 0, "订单当前状态不允许继续收料"),
        ("partially_delivered", 10, "订单明细已全部送货"),
    ),
)
def test_pending_list_and_receipt_execution_share_the_same_item_gate(
    requisition_app,
    order_status: str,
    delivered_quantity: int,
    expected_message: str,
) -> None:
    from app.models.order import Order, OrderItem

    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=10,
            purchase_total=10,
            order_purpose=10,
            stock_purpose=0,
        )[0]
        frozen = _freeze_receipt_fact(
            client,
            source,
            idempotency_key=f"p1101-blocked-price-{order_status}",
        )
        assert frozen.status_code == 200, frozen.text

        with session_factory() as session:
            item = session.get(OrderItem, 1)
            assert item is not None
            order = session.get(Order, item.order_id)
            assert order is not None
            order.status = order_status
            item.delivered_quantity = delivered_quantity
            item.is_force_closed = False
            session.commit()

        pending = client.get("/api/incoming/pending")
        assert pending.status_code == 200, pending.text
        assert source.route_key not in {
            str(row["item_id"]) for row in pending.json()["items"]
        }
        before = _business_counts(session_factory)
        blocked = _receive(
            client,
            source,
            frozen.json(),
            quantity=10,
            idempotency_key=f"p1101-blocked-receive-{order_status}",
        )
        assert blocked.status_code == 409, blocked.text
        assert _error_code(blocked) == "ORDER_ITEM_RECEIPT_BLOCKED"
        assert expected_message in blocked.text
        assert _business_counts(session_factory) == before


def test_receipt_auto_composite_finished_stock_can_dispatch_and_cancel(
    requisition_app,
) -> None:
    from app.api.deliveries import (
        _delivery_remaining_quantity,
        router as deliveries_router,
    )
    from app.models.order import Order, OrderItem
    from app.models.production import ProductionTask
    from app.models.purchase_receipt import IncomingReceiptPurposeAllocation
    from app.models.warehouse_inventory import InventoryLot
    from app.services.production_workflow import production_ready_quantity

    app, session_factory = requisition_app
    app.include_router(deliveries_router, prefix="/api/deliveries")
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        sources = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=500,
            purchase_total=600,
            order_purpose=500,
            stock_purpose=100,
            composite=True,
            composite_reserve_purpose=100,
        )
        by_component = {source.component_type: source for source in sources}
        facts = {}
        for component, source in by_component.items():
            fact = _freeze_receipt_fact(
                client,
                source,
                idempotency_key=f"p181-composite-delivery-price-{component}",
            )
            assert fact.status_code == 200, fact.text
            facts[component] = fact.json()
        cover = _receive(
            client,
            by_component["cover"],
            facts["cover"],
            quantity=500,
            idempotency_key="p181-composite-delivery-cover",
        )
        assert cover.status_code == 200, cover.text
        base = _receive(
            client,
            by_component["base"],
            facts["base"],
            quantity=580,
            idempotency_key="p181-composite-delivery-base",
        )
        assert base.status_code == 200, base.text
        assert base.json()["purpose_allocation"]["order_sheet_delta"] == 500
        assert base.json()["purpose_allocation"]["reserve_sheet_delta"] == 80

        with session_factory() as session:
            item = session.get(OrderItem, 1)
            assert item is not None
            order = session.get(Order, item.order_id)
            main_task = session.scalar(
                select(ProductionTask).where(
                    ProductionTask.order_item_id == item.id,
                    ProductionTask.task_role == "order_main",
                )
            )
            allocation = session.scalar(
                select(IncomingReceiptPurposeAllocation)
                .where(
                    IncomingReceiptPurposeAllocation.finished_inventory_lot_id.is_not(
                        None
                    ),
                )
                .order_by(IncomingReceiptPurposeAllocation.id.desc())
            )
            assert order is not None and main_task is not None and allocation is not None
            lot_id = int(allocation.finished_inventory_lot_id)
            lot = session.get(InventoryLot, lot_id)
            assert lot is not None
            assert main_task.status == "completed"
            assert order.status == "pending_delivery"
            assert production_ready_quantity(session, item) == 500
            assert _delivery_remaining_quantity(session, item) == 500
            assert int(lot.quantity_available) + int(lot.quantity_reserved) == 500

        created = client.post(
            "/api/deliveries",
            json={
                "customer_id": 1,
                "items": [{"order_item_id": 1, "delivered_quantity": 500}],
            },
        )
        assert created.status_code == 201, created.text
        delivery_id = int(created.json()["id"])
        dispatched = client.put(f"/api/deliveries/{delivery_id}/dispatch")
        assert dispatched.status_code == 200, dispatched.text
        with session_factory() as session:
            item = session.get(OrderItem, 1)
            lot = session.get(InventoryLot, lot_id)
            assert item is not None and lot is not None
            assert int(item.delivered_quantity) == 500
            assert int(lot.quantity_consumed) == 500
            assert int(lot.quantity_available) + int(lot.quantity_reserved) == 0

        cancelled = client.put(f"/api/deliveries/{delivery_id}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        with session_factory() as session:
            item = session.get(OrderItem, 1)
            lot = session.get(InventoryLot, lot_id)
            assert item is not None and lot is not None
            assert int(item.delivered_quantity) == 0
            assert int(lot.quantity_consumed) == 0
            assert int(lot.quantity_available) + int(lot.quantity_reserved) == 500


def test_composite_reversal_removes_immutable_allocations_from_production_summary(
    requisition_app,
) -> None:
    from app.services.production_workflow import list_production_tasks

    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        sources = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=10,
            purchase_total=10,
            order_purpose=10,
            stock_purpose=0,
            composite=True,
        )
        by_component = {source.component_type: source for source in sources}
        receipt_item_ids = {}
        for component in ("cover", "base"):
            source = by_component[component]
            fact = _freeze_receipt_fact(
                client,
                source,
                idempotency_key=f"p181-summary-price-{component}",
            )
            assert fact.status_code == 200, fact.text
            received = _receive(
                client,
                source,
                fact.json(),
                quantity=10,
                idempotency_key=f"p181-summary-receive-{component}",
            )
            assert received.status_code == 200, received.text
            receipt_item_ids[component] = int(received.json()["receipt_item_id"])

        for component in ("base", "cover"):
            reverted = client.put(
                "/api/incoming/receipt-items/"
                f"{receipt_item_ids[component]}/revert",
                json={
                    "reason": "匿名组合用途汇总逆序撤销",
                    "idempotency_key": f"p181-summary-revert-{component}",
                },
            )
            assert reverted.status_code == 200, reverted.text

    with session_factory() as session:
        row = next(
            item
            for item in list_production_tasks(
                session,
                allowed_customer_ids=None,
            )
            if int(item["order_item_id"]) == 1
        )
    summary = row["receipt_purpose_summary"]
    assert row["receipt_purpose_managed"] is True
    assert row["completion_actionable"] is False
    assert summary["order_purpose_received_sheet_qty"] == 0
    assert summary["reserve_purpose_received_sheet_qty"] == 0
    assert summary["automatic_finished_output_qty"] == 0
    assert summary["current_theoretical_finished_capacity_qty"] == 0
    assert {item["component_type"] for item in summary["component_progress"]} == {
        "cover",
        "base",
    }


def test_normal_receipt_auto_freezes_material_master_price_for_incoming_operator(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=10,
            purchase_total=10,
            order_purpose=10,
            stock_purpose=0,
        )[0]

        _login(client, "workshop")
        frozen = client.put(
            "/api/requisition/purchase-sources/"
            f"{quote(source.source_key, safe='')}/receipt-facts/auto",
            json={
                "actual_material_id": source.material_id,
                "purchase_purpose_source_snapshot_id": source.purpose_snapshot_id,
                "purpose_snapshot_version": source.purpose_snapshot_version,
                "receipt_plan_fingerprint": source.receipt_plan_fingerprint,
                "expected_source_version": source.source_version,
                "expected_latest_receipt_fact_version": 0,
                "idempotency_key": "p181-auto-master-price",
            },
        )
        assert frozen.status_code == 200, frozen.text
        fact = frozen.json()
        assert fact["actual_material_id"] == source.material_id
        assert fact["unit_price"] == "99.9900"
        assert fact["currency"] == "CNY"
        assert fact["price_unit"] == "per_sheet"
        assert fact["tax_included"] is True
        assert fact["tax_rate"] == "0.1300"

        received = _receive(
            client,
            source,
            fact,
            quantity=10,
            idempotency_key="p181-auto-master-price-receive",
        )
        assert received.status_code == 200, received.text
        assert received.json()["material_status"] == "received"


def test_frozen_missing_receipt_fact_or_stale_plan_fails_closed(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=500,
            purchase_total=600,
            order_purpose=500,
            stock_purpose=100,
        )[0]
        baseline = _business_counts(session_factory)
        missing = client.put(
            f"/api/incoming/receive/{source.route_key}",
            json={
                "received_quantity": 1,
                "idempotency_key": "p181-missing-price",
                "expected_receipt_fact_version": 1,
                "purchase_purpose_source_snapshot_id": source.purpose_snapshot_id,
                "expected_purpose_snapshot_version": source.purpose_snapshot_version,
                "receipt_plan_fingerprint": source.receipt_plan_fingerprint,
                "expected_actual_material_version": 1,
                "actual_material_fingerprint": "a" * 64,
            },
        )
        assert missing.status_code == 409, missing.text
        assert _error_code(missing) == "PURCHASE_RECEIPT_FACT_REQUIRED"
        assert _business_counts(session_factory) == baseline

        frozen = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p181-price-stale",
        )
        assert frozen.status_code == 200, frozen.text
        fact = frozen.json()
        # Freezing the authoritative receipt-price fact is a successful formal
        # operation and must retain its own audit row.  Zero-write assertions
        # below therefore start after that expected commit.
        baseline = _business_counts(session_factory)
        stale = _receive(
            client,
            source,
            fact,
            quantity=1,
            idempotency_key="p181-stale-plan",
            overrides={"expected_receipt_fact_version": fact["receipt_fact_version"] + 1},
        )
        assert stale.status_code == 409, stale.text
        assert _error_code(stale) == "PURCHASE_RECEIPT_FACT_STALE"
        assert _business_counts(session_factory) == baseline

        tampered = _receive(
            client,
            source,
            fact,
            quantity=1,
            idempotency_key="p181-tampered-plan",
            overrides={"receipt_plan_fingerprint": "f" * 64},
        )
        assert tampered.status_code == 409, tampered.text
        assert _error_code(tampered) == "INCOMING_RECEIPT_PLAN_TAMPERED"
        assert _business_counts(session_factory) == baseline


@pytest.mark.parametrize(
    ("missing_field", "expected_code"),
    (
        (
            "purchase_purpose_source_snapshot_id",
            "PURCHASE_PURPOSE_SNAPSHOT_INVALID",
        ),
        ("expected_receipt_fact_version", "PURCHASE_RECEIPT_FACT_STALE"),
        (
            "expected_purpose_snapshot_version",
            "PURCHASE_PURPOSE_SNAPSHOT_STALE",
        ),
        ("receipt_plan_fingerprint", "INCOMING_RECEIPT_PLAN_TAMPERED"),
        ("expected_actual_material_version", "ACTUAL_MATERIAL_FACT_STALE"),
        ("actual_material_fingerprint", "ACTUAL_MATERIAL_FACT_STALE"),
    ),
)
def test_frozen_receive_requires_every_versioned_contract_field_without_writes(
    requisition_app,
    missing_field: str,
    expected_code: str,
) -> None:
    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=10,
            purchase_total=10,
            order_purpose=10,
            stock_purpose=0,
        )[0]
        frozen = _freeze_receipt_fact(
            client,
            source,
            idempotency_key=f"p181-price-missing-{missing_field}",
        )
        assert frozen.status_code == 200, frozen.text
        fact = frozen.json()
        payload = {
            "received_quantity": 10,
            "idempotency_key": f"p181-receive-missing-{missing_field}",
            "expected_receipt_fact_version": fact["receipt_fact_version"],
            "purchase_purpose_source_snapshot_id": source.purpose_snapshot_id,
            "expected_purpose_snapshot_version": source.purpose_snapshot_version,
            "receipt_plan_fingerprint": fact["receipt_plan_fingerprint"],
            "expected_actual_material_version": fact["actual_material_version"],
            "actual_material_fingerprint": fact["actual_material_fingerprint"],
        }
        payload.pop(missing_field)
        baseline = _business_counts(session_factory)
        blocked = client.put(
            f"/api/incoming/receive/{source.route_key}",
            json=payload,
        )
        assert blocked.status_code == 409, blocked.text
        assert _error_code(blocked) == expected_code
        assert _business_counts(session_factory) == baseline


def test_client_cannot_submit_purpose_split_or_request_hash_override(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=10,
            purchase_total=12,
            order_purpose=10,
            stock_purpose=2,
        )[0]
        price_override = client.put(
            "/api/requisition/purchase-sources/"
            f"{quote(source.source_key, safe='')}/receipt-facts",
            json={
                "actual_material_id": source.material_id,
                "unit_price": "2.5000",
                "currency": "CNY",
                "price_unit": "per_sheet",
                "tax_included": True,
                "tax_rate": "0.13",
                "expected_source_version": source.source_version,
                "idempotency_key": "p181-price-client-hash",
                "material_change_confirmed": False,
                "request_hash": "0" * 64,
            },
        )
        assert price_override.status_code == 422, price_override.text

        frozen = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p181-price-no-client-split",
        )
        assert frozen.status_code == 200, frozen.text
        baseline = _business_counts(session_factory)
        tampered = _receive(
            client,
            source,
            frozen.json(),
            quantity=12,
            idempotency_key="p181-receive-client-split",
            overrides={
                "order_purpose_sheet_qty": 0,
                "stock_purpose_sheet_qty": 12,
                "request_hash": "0" * 64,
            },
        )
        assert tampered.status_code == 422, tampered.text
        assert _business_counts(session_factory) == baseline


def test_receipt_fact_uses_formal_price_not_master_quote_and_binds_idempotency(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=10,
            purchase_total=10,
            order_purpose=10,
            stock_purpose=0,
        )[0]
        first = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p181-price-idempotent",
            unit_price="2.5000",
        )
        assert first.status_code == 200, first.text
        replay = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p181-price-idempotent",
            unit_price="2.5000",
        )
        assert replay.status_code == 200, replay.text
        assert replay.json() == first.json()

        changed_payload = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p181-price-idempotent",
            unit_price="3.0000",
        )
        assert changed_payload.status_code == 409, changed_payload.text
        assert _error_code(changed_payload) == "PURCHASE_RECEIPT_FACT_IDEMPOTENCY_CONFLICT"

        stale = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p181-price-stale-source",
            expected_source_version=source.source_version + 1,
        )
        assert stale.status_code == 409, stale.text
        assert _error_code(stale) == "PURCHASE_SOURCE_STALE"

        received = _receive(
            client,
            source,
            first.json(),
            quantity=10,
            idempotency_key="p181-formal-price-receive",
        )
        allocation = _assert_allocation(
            received,
            order_delta=10,
            reserve_delta=0,
            order_cumulative=10,
            reserve_cumulative=0,
            finished_delta=10,
            finished_cumulative=10,
        )
    assert Decimal(str(allocation["receipt_total_cost"])) == Decimal("25.0000")
    assert Decimal(str(allocation["receipt_total_cost"])) != Decimal("999.9000")


def test_square_meter_price_uses_frozen_dimensions_and_cost_conserves(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=10,
            purchase_total=12,
            order_purpose=10,
            stock_purpose=2,
        )[0]
        frozen = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p181-price-square-meter",
            unit_price="10.0000",
            price_unit="per_square_meter",
        )
        assert frozen.status_code == 200, frozen.text
        result = _assert_allocation(
            _receive(
                client,
                source,
                frozen.json(),
                quantity=12,
                idempotency_key="p181-receive-square-meter",
            ),
            order_delta=10,
            reserve_delta=2,
            order_cumulative=10,
            reserve_cumulative=2,
            finished_delta=10,
            finished_cumulative=10,
        )
    # Frozen board size is 800 x 200 mm: 0.16 m2 x 10 CNY x 12 sheets.
    assert Decimal(str(result["receipt_total_cost"])) == Decimal("19.2000")
    assert Decimal(str(result["order_cost"])) == Decimal("16.0000")
    assert Decimal(str(result["reserve_cost"])) == Decimal("3.2000")


def test_receipt_fact_binds_actual_material_version_with_cas(
    requisition_app,
) -> None:
    from app.models.material import Material

    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=10,
            purchase_total=10,
            order_purpose=10,
            stock_purpose=0,
        )[0]
        with session_factory() as session:
            actual_material = session.get(Material, source.material_id)
            assert actual_material is not None
            actual_material_version = int(actual_material.version)

        first = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p181-price-material-cas-first",
        )
        assert first.status_code == 200, first.text
        fact = first.json()
        assert fact["actual_material_version"] == actual_material_version
        assert len(fact["actual_material_fingerprint"]) == 64

        with session_factory() as session:
            actual_material = session.get(Material, source.material_id)
            assert actual_material is not None
            actual_material.version += 1
            session.commit()

        stale = _receive(
            client,
            source,
            fact,
            quantity=1,
            idempotency_key="p181-receive-material-cas-stale",
        )
        assert stale.status_code == 409, stale.text
        assert _error_code(stale) == "ACTUAL_MATERIAL_FACT_STALE"


def test_material_change_requires_independent_request_and_confirmed_approval(
    requisition_app,
) -> None:
    from app.core.security import hash_password
    from app.models.access_control import UserCustomerScope, UserPermissionOverride
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.user import User

    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    changed_material_id = _add_changed_material(session_factory)
    with session_factory() as session:
        finance = session.scalar(select(User).where(User.username == "finance"))
        assert finance is not None
        other_customer = Customer(
            customer_number=18182,
            customer_code="P181-SCOPE-OTHER",
            name="匿名其他客户",
        )
        restricted = User(
            username="restricted-office",
            password_hash=hash_password("RolePass123!"),
            role="sales",
            real_name="匿名受限审批员",
            display_name="匿名受限审批员",
            customer_access_mode="selected",
            must_change_password=False,
        )
        session.add_all([other_customer, restricted])
        session.flush()
        session.add_all(
            [
                UserPermissionOverride(
                    user_id=finance.id,
                    permission_code=permission,
                    is_allowed=True,
                )
                for permission in (
                    "requisition.view",
                    "requisition.execute",
                    "requisition.purchase_price.confirm",
                )
            ]
        )
        session.add_all(
            [
                UserPermissionOverride(
                    user_id=restricted.id,
                    permission_code=permission,
                    is_allowed=True,
                )
                for permission in (
                    "requisition.view",
                    "requisition.execute",
                    "incoming.material_variance.confirm",
                )
            ]
        )
        session.add(
            UserCustomerScope(
                user_id=restricted.id,
                customer_id=other_customer.id,
            )
        )
        session.commit()
    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=10,
            purchase_total=10,
            order_purpose=10,
            stock_purpose=0,
        )[0]
        with session_factory() as session:
            changed_material = session.get(Material, changed_material_id)
            assert changed_material is not None
            changed_version = int(changed_material.version)

        _login(client, "workshop")
        requested = client.put(
            "/api/requisition/purchase-sources/"
            f"{quote(source.source_key, safe='')}/material-variances",
            json={
                "purchase_purpose_source_snapshot_id": source.purpose_snapshot_id,
                "purpose_snapshot_version": source.purpose_snapshot_version,
                "receipt_plan_fingerprint": source.receipt_plan_fingerprint,
                "expected_source_version": source.source_version,
                "actual_material_id": changed_material_id,
                "reason": "供应商实际来料材质与采购快照不同",
                "idempotency_key": "p181-material-variance-request",
            },
        )
        assert requested.status_code == 200, requested.text
        request_fact = requested.json()
        assert request_fact["material_variance_id"] > 0
        assert request_fact["actual_material_version"] == changed_version

        _login(client, "restricted-office")
        hidden = client.put(
            "/api/requisition/purchase-material-variances/"
            f"{request_fact['material_variance_id']}/confirm",
            json={
                "idempotency_key": "p181-material-variance-cross-scope",
            },
        )
        assert hidden.status_code == 404, hidden.text

        _login(client, "admin")
        approved = client.put(
            "/api/requisition/purchase-material-variances/"
            f"{request_fact['material_variance_id']}/confirm",
            json={
                "idempotency_key": "p181-material-variance-confirm",
            },
        )
        assert approved.status_code == 200, approved.text
        approval = approved.json()
        assert approval["material_variance_id"] == request_fact[
            "material_variance_id"
        ]
        assert approval["confirmed_by"] != request_fact["requested_by"]

        _login(client, "workshop")
        frozen = client.put(
            "/api/requisition/purchase-sources/"
            f"{quote(source.source_key, safe='')}/receipt-facts/auto",
            json={
                "actual_material_id": changed_material_id,
                "material_variance_approval_id": approval[
                    "material_variance_approval_id"
                ],
                "purchase_purpose_source_snapshot_id": source.purpose_snapshot_id,
                "purpose_snapshot_version": source.purpose_snapshot_version,
                "receipt_plan_fingerprint": source.receipt_plan_fingerprint,
                "expected_source_version": source.source_version,
                "expected_latest_receipt_fact_version": 0,
                "idempotency_key": "p181-price-approved-material-change",
            },
        )
        assert frozen.status_code == 200, frozen.text
        assert frozen.json()["material_changed"] is True
        assert frozen.json()["actual_material_version"] == changed_version
        assert frozen.json()["unit_price"] == "88.8800"
        assert frozen.json()["currency"] == "USD"
        assert frozen.json()["price_unit"] == "per_sheet"
        assert frozen.json()["tax_included"] is False
        assert frozen.json()["tax_rate"] == "0.0600"


def test_actual_material_change_cannot_use_legacy_boolean_or_bypass_permission(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    changed_material_id = _add_changed_material(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=10,
            purchase_total=10,
            order_purpose=10,
            stock_purpose=0,
        )[0]
        unconfirmed = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p181-material-unconfirmed",
            actual_material_id=changed_material_id,
            material_change_confirmed=False,
        )
        assert unconfirmed.status_code == 409, unconfirmed.text
        assert _error_code(unconfirmed) == "ACTUAL_MATERIAL_CONFIRMATION_REQUIRED"

        _login(client, "workshop")
        unauthorized = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p181-material-unauthorized",
            actual_material_id=changed_material_id,
            material_change_confirmed=True,
        )
        assert unauthorized.status_code == 403, unauthorized.text

        _login(client, "admin")
        confirmed = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p181-material-confirmed",
            actual_material_id=changed_material_id,
            material_change_confirmed=True,
        )
        assert confirmed.status_code == 409, confirmed.text
        assert _error_code(confirmed) == "ACTUAL_MATERIAL_CONFIRMATION_REQUIRED"
        from app.models.purchase_receipt import PurchaseReceiptFact

        with session_factory() as session:
            stored = session.scalar(
                select(PurchaseReceiptFact).where(
                    PurchaseReceiptFact.idempotency_key
                    == "p181-material-confirmed"
                )
            )
            assert stored is None


@pytest.mark.parametrize(
    ("location_code", "expected_code"),
    [
        ("F1-FIN-001-L001", "AUTO_FINISHED_LOCATION_UNAVAILABLE"),
        ("P181-RAW-STAGE", "RESERVE_STAGING_LOCATION_UNAVAILABLE"),
    ],
)
def test_unavailable_required_location_fails_entire_receipt_without_fallback(
    requisition_app,
    location_code: str,
    expected_code: str,
) -> None:
    from app.models.warehouse_inventory import WarehouseLocation

    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=500,
            purchase_total=600,
            order_purpose=500,
            stock_purpose=100,
        )[0]
        frozen = _freeze_receipt_fact(
            client,
            source,
            idempotency_key=f"p181-location-price-{location_code}",
        )
        assert frozen.status_code == 200, frozen.text
        baseline = _business_counts(session_factory)
        with session_factory() as session:
            location = session.scalar(
                select(WarehouseLocation).where(
                    WarehouseLocation.location_code == location_code
                )
            )
            assert location is not None
            if location_code.startswith("F1-FIN-"):
                fin_locations = list(
                    session.scalars(
                        select(WarehouseLocation).where(
                            WarehouseLocation.area_code == "FIN-001"
                        )
                    )
                )
                assert fin_locations
                for fin_location in fin_locations:
                    fin_location.is_active = False
            else:
                location.is_active = False
            session.commit()

        failed = _receive(
            client,
            source,
            frozen.json(),
            quantity=580,
            idempotency_key=f"p181-location-receive-{location_code}",
        )
        assert failed.status_code == 409, failed.text
        assert _error_code(failed) == expected_code
        assert _business_counts(session_factory) == baseline
    assert _posted_finished_quantity(session_factory) == 0
    assert _active_semi_quantity(session_factory) == 0


def test_legacy_unset_keeps_old_receive_contract_without_new_auto_finished(
    requisition_app,
) -> None:
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with session_factory() as session:
        from app.models.order import OrderItem

        order = SupplierRequisitionOrder(
            order_number="SRO-P181-LEGACY",
            supplier_name="匿名历史供应商",
            total_quantity=500,
            stock_deduction_qty=0,
            requisition_qty=500,
            status="confirmed",
            created_by=1,
        )
        session.add(order)
        session.flush()
        item = SupplierRequisitionOrderItem(
            supplier_order_id=order.id,
            order_item_id=1,
            source_key="order_item:1:whole",
            product_id=1,
            material_id=None,
            product_code="P181-LEGACY",
            product_name="匿名历史纸箱",
            quantity=500,
            stock_deduction_qty=0,
            requisition_qty=500,
            cutting_mode="一开一",
            pieces_per_box=1,
            required_piece_qty=500,
            customer_name="苏州思迈尔包装有限公司",
            status="active",
            version=1,
        )
        session.add(item)
        order_item = session.get(OrderItem, 1)
        assert order_item is not None
        order_item.supplier_order_number = order.order_number
        order_item.requisition_status = "供应商已排单"
        order_item.material_status = "pending"
        session.commit()
        route_key = f"so{item.id}"

    with TestClient(app) as client:
        _login(client, "admin")
        response = client.put(
            f"/api/incoming/receive/{route_key}",
            json={
                "received_quantity": 500,
                "idempotency_key": "p181-legacy-receive",
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["purpose_status"] == "legacy_unset"
    assert response.json().get("purpose_allocation") is None
    assert _posted_finished_quantity(session_factory) == 0
    assert _active_semi_quantity(session_factory) == 0


def test_receive_idempotency_binds_payload_and_actor_without_double_counting(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=500,
            purchase_total=600,
            order_purpose=500,
            stock_purpose=100,
        )[0]
        frozen = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p181-price-idem",
        )
        assert frozen.status_code == 200, frozen.text
        fact = frozen.json()
        first = _receive(
            client,
            source,
            fact,
            quantity=450,
            idempotency_key="p181-receive-idem",
        )
        assert first.status_code == 200, first.text
        counts = _business_counts(session_factory)
        replay = _receive(
            client,
            source,
            fact,
            quantity=450,
            idempotency_key="p181-receive-idem",
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["receipt_item_id"] == first.json()["receipt_item_id"]
        assert replay.json()["purpose_allocation"] == first.json()["purpose_allocation"]
        assert _business_counts(session_factory) == counts

        from app.models.order import Order, OrderItem

        with session_factory() as session:
            item = session.get(OrderItem, 1)
            assert item is not None
            order = session.get(Order, item.order_id)
            assert order is not None
            order.status = "closed"
            item.is_force_closed = True
            session.commit()
        terminal_replay = _receive(
            client,
            source,
            fact,
            quantity=450,
            idempotency_key="p181-receive-idem",
        )
        assert terminal_replay.status_code == 200, terminal_replay.text
        assert terminal_replay.json()["receipt_item_id"] == first.json()[
            "receipt_item_id"
        ]
        assert _business_counts(session_factory) == counts

        changed = _receive(
            client,
            source,
            fact,
            quantity=451,
            idempotency_key="p181-receive-idem",
        )
        assert changed.status_code == 409, changed.text
        assert _error_code(changed) == "INCOMING_IDEMPOTENCY_CONFLICT"
        assert _business_counts(session_factory) == counts

        _login(client, "workshop")
        # Login is an intentional security audit event.  Rebase after it so
        # the actor-conflict assertion covers only the rejected receipt call.
        counts = _business_counts(session_factory)
        other_actor = _receive(
            client,
            source,
            fact,
            quantity=450,
            idempotency_key="p181-receive-idem",
        )
        assert other_actor.status_code == 409, other_actor.text
        assert _error_code(other_actor) == "INCOMING_IDEMPOTENCY_ACTOR_MISMATCH"
        assert _business_counts(session_factory) == counts


def test_frozen_partial_receipt_cannot_use_legacy_accept_short(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=500,
            purchase_total=600,
            order_purpose=500,
            stock_purpose=100,
        )[0]
        frozen = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p181-price-accept-short-block",
        )
        assert frozen.status_code == 200, frozen.text
        received = _receive(
            client,
            source,
            frozen.json(),
            quantity=450,
            idempotency_key="p181-receive-accept-short-block",
        )
        assert received.status_code == 200, received.text
        baseline = _business_counts(session_factory)
        blocked = client.put(
            "/api/incoming/receipt-items/"
            f"{received.json()['receipt_item_id']}/accept-short",
            json={},
        )
        assert blocked.status_code == 409, blocked.text
        assert _error_code(blocked) == "FROZEN_PURCHASE_SHORT_ACCEPT_FORBIDDEN"
        after = _business_counts(session_factory)
        assert {key: value for key, value in after.items() if key != "audit"} == {
            key: value for key, value in baseline.items() if key != "audit"
        }


def test_frozen_receipt_cannot_be_reverted_through_legacy_order_item_route(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=500,
            purchase_total=600,
            order_purpose=500,
            stock_purpose=100,
        )[0]
        frozen = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p181-price-legacy-revert-block",
        )
        assert frozen.status_code == 200, frozen.text
        received = _receive(
            client,
            source,
            frozen.json(),
            quantity=450,
            idempotency_key="p181-receive-legacy-revert-block",
        )
        assert received.status_code == 200, received.text
        baseline = _business_counts(session_factory)
        blocked = client.put(
            "/api/incoming/revert/1",
            json={"reason": "不得绕过用途反冲"},
        )
        assert blocked.status_code == 409, blocked.text
        assert _error_code(blocked) == "FROZEN_RECEIPT_LEGACY_REVERT_FORBIDDEN"
        after = _business_counts(session_factory)
        assert {key: value for key, value in after.items() if key != "audit"} == {
            key: value for key, value in baseline.items() if key != "audit"
        }


def test_frozen_receipt_revert_replays_same_actor_payload_and_key(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=500,
            purchase_total=600,
            order_purpose=500,
            stock_purpose=100,
        )[0]
        frozen = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p181-price-revert-idempotent",
        )
        assert frozen.status_code == 200, frozen.text
        received = _receive(
            client,
            source,
            frozen.json(),
            quantity=450,
            idempotency_key="p181-receive-revert-idempotent",
        )
        assert received.status_code == 200, received.text
        receipt_item_id = int(received.json()["receipt_item_id"])
        payload = {
            "reason": "匿名撤销幂等验证",
            "idempotency_key": "p181-revert-idempotent",
        }
        first = client.put(
            f"/api/incoming/receipt-items/{receipt_item_id}/revert",
            json=payload,
        )
        assert first.status_code == 200, first.text
        counts = _business_counts(session_factory)
        replay = client.put(
            f"/api/incoming/receipt-items/{receipt_item_id}/revert",
            json=payload,
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["purpose_reversal"] == first.json()["purpose_reversal"]
        assert _business_counts(session_factory) == counts


def test_latest_reversal_exactly_unwinds_620_600_580_450_zero(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    receipt_item_ids: list[int] = []
    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=500,
            purchase_total=600,
            order_purpose=500,
            stock_purpose=100,
        )[0]
        frozen = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p181-price-revert",
        )
        assert frozen.status_code == 200, frozen.text
        fact = frozen.json()
        for quantity, key in (
            (450, "450"),
            (130, "580"),
            (20, "600"),
            (20, "620"),
        ):
            response = _receive(
                client,
                source,
                fact,
                quantity=quantity,
                idempotency_key=f"p181-revert-source-{key}",
            )
            assert response.status_code == 200, response.text
            receipt_item_ids.append(response.json()["receipt_item_id"])

        expected = (
            (600, 500, 100, 500),
            (580, 500, 80, 500),
            (450, 450, 0, 450),
            (0, 0, 0, 0),
        )
        for receipt_item_id, (
            total_received,
            order_cumulative,
            reserve_cumulative,
            finished_cumulative,
        ) in zip(reversed(receipt_item_ids), expected, strict=True):
            response = client.put(
                f"/api/incoming/receipt-items/{receipt_item_id}/revert",
                json={},
            )
            assert response.status_code == 200, response.text
            reversal = response.json()["purpose_reversal"]
            assert reversal["valid_received_cumulative"] == total_received
            assert reversal["order_sheet_cumulative"] == order_cumulative
            assert reversal["reserve_sheet_cumulative"] == reserve_cumulative
            assert reversal["theoretical_finished_cumulative"] == finished_cumulative
            assert _posted_finished_quantity(session_factory) == finished_cumulative
            assert _active_semi_quantity(session_factory) == reserve_cumulative

    from app.models.production import ProductionTask

    with session_factory() as session:
        task = session.scalar(
            select(ProductionTask).where(ProductionTask.order_item_id == 1)
        )
        assert task is not None
        assert task.status == "waiting_material"
        assert task.planned_quantity == 0


def test_reversal_blocks_when_reserve_lot_is_used_by_a_later_order(
    requisition_app,
) -> None:
    from datetime import datetime, timezone

    from app.models.order import OrderItem
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation

    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=500,
            purchase_total=600,
            order_purpose=500,
            stock_purpose=100,
        )[0]
        frozen = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p181-price-downstream",
        )
        assert frozen.status_code == 200, frozen.text
        fact = frozen.json()
        first = _receive(
            client,
            source,
            fact,
            quantity=450,
            idempotency_key="p181-downstream-450",
        )
        second = _receive(
            client,
            source,
            fact,
            quantity=130,
            idempotency_key="p181-downstream-580",
        )
        assert first.status_code == second.status_code == 200
        second_receipt_item_id = second.json()["receipt_item_id"]

        with session_factory() as session:
            reserve_lot = session.scalar(
                select(InventoryLot).where(
                    InventoryLot.inventory_type == "semi_finished",
                    InventoryLot.status == "active",
                )
            )
            item = session.get(OrderItem, 1)
            assert reserve_lot is not None and item is not None
            assert reserve_lot.quantity_available >= 1
            reserve_lot.quantity_available -= 1
            reserve_lot.quantity_reserved += 1
            reserve_lot.version += 1
            session.add(
                InventoryReservation(
                    reservation_number="RSV-P181-DOWNSTREAM",
                    inventory_lot_id=reserve_lot.id,
                    reservation_type="semi_order",
                    order_id=item.order_id,
                    order_item_id=item.id,
                    reserved_stock_quantity=1,
                    credited_requirement_quantity=1,
                    yield_factor=1,
                    consumed_stock_quantity=0,
                    released_stock_quantity=0,
                    consumed_requirement_quantity=0,
                    released_requirement_quantity=0,
                    status="active",
                    reservation_group_key="P181-DOWNSTREAM-GROUP",
                    reservation_group_requested_quantity=1,
                    idempotency_key="p181-downstream-reservation",
                    reserved_at=datetime.now(timezone.utc).replace(tzinfo=None),
                )
            )
            session.commit()

        before = _business_counts(session_factory)
        blocked = client.put(
            f"/api/incoming/receipt-items/{second_receipt_item_id}/revert",
            json={},
        )
        assert blocked.status_code == 409, blocked.text
        assert _error_code(blocked) == "RESERVE_INVENTORY_ALREADY_USED"
        assert _business_counts(session_factory) == before
    assert _posted_finished_quantity(session_factory) == 500
    assert _active_semi_quantity(session_factory) == 80


def test_composite_internal_tasks_never_escape_employee_lists_or_search(
    requisition_app,
) -> None:
    from app.api.mobile_erp import router as mobile_router
    from app.api.production import router as production_router
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.production import ProductionTask
    from app.services.production_workflow import list_production_task_dashboard_rows
    from tests.test_n039_composite_bom_production import _snapshot

    app, session_factory = requisition_app
    app.include_router(production_router, prefix="/api/production")
    app.include_router(mobile_router, prefix="/api/mobile/erp")
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        assert item is not None
        order = session.get(Order, item.order_id)
        parent = session.get(Product, item.product_id)
        assert order is not None and parent is not None
        order.order_number = "P181-COMPOSITE-VISIBLE"
        item.material_status = "received"
        item.requisition_status = "已入库"
        item.snapshot_product_code = "P181-COMPOSITE"
        item.snapshot_product_name = "匿名组合纸箱"
        item.snapshot_production_notes = "印刷后组合"
        parent.is_composite = True
        parent.box_style = "A1"
        parent.production_process = "印刷后组合"
        component_products = [
            Product(
                customer_id=parent.customer_id,
                product_code=f"P181-COMP-{index}",
                customer_material_code=f"P181-COMP-{index}",
                product_name=f"匿名内部组件{index}",
                box_style="组件",
                is_internal_component=True,
            )
            for index in (1, 2)
        ]
        session.add_all(component_products)
        session.flush()
        snapshots = [
            _snapshot(
                item=item,
                product=product,
                display_order=index,
                quantity_per_set=1,
                is_required=True,
            )
            for index, product in enumerate(component_products, start=1)
        ]
        session.add_all(snapshots)
        session.flush()
        main = ProductionTask(
            order_item_id=item.id,
            sales_order_item_bom_component_id=None,
            task_role="order_main",
            status="pending",
            planned_quantity=100,
            finished_coverage_snapshot=0,
            ordered_quantity_snapshot=100,
            material_received_quantity=100,
            material_input_quantity=100,
            output_factor=1,
            readiness_basis="automatic_receipt",
            print_content_snapshot="匿名主任务印刷",
            version=1,
        )
        internals = [
            ProductionTask(
                order_item_id=item.id,
                sales_order_item_bom_component_id=snapshot.id,
                task_role="component_internal",
                status="pending",
                planned_quantity=100,
                finished_coverage_snapshot=0,
                ordered_quantity_snapshot=100,
                material_received_quantity=100,
                material_input_quantity=100,
                output_factor=1,
                readiness_basis="automatic_receipt",
                print_content_snapshot=f"内部组件{index}",
                version=1,
            )
            for index, snapshot in enumerate(snapshots, start=1)
        ]
        session.add_all([main, *internals])
        session.commit()
        main_id = int(main.id)
        internal_ids = {int(task.id) for task in internals}

        dashboard = list_production_task_dashboard_rows(
            session,
            allowed_customer_ids=None,
            status="pending",
        )
        assert [int(row["id"]) for row in dashboard] == [main_id]

    with TestClient(app) as client:
        _login(client, "admin")
        desktop = client.get("/api/production/tasks", params={"status": "pending"})
        assert desktop.status_code == 200, desktop.text
        assert [int(row["id"]) for row in desktop.json()["items"]] == [main_id]

        station = client.get(
            "/api/mobile/erp/production/tasks",
            params={"station": "printing", "page_size": 20},
        )
        assert station.status_code == 200, station.text
        assert station.json()["total"] == 1
        assert [int(row["task_id"]) for row in station.json()["items"]] == [main_id]

        lookup = client.get(
            "/api/mobile/erp/production/pending/lookup",
            params={"q": "P181-COMPOSITE"},
        )
        assert lookup.status_code == 200, lookup.text
        assert lookup.json()["total"] == 1
        assert [int(row["task_id"]) for row in lookup.json()["items"]] == [main_id]

        search = client.get(
            "/api/mobile/erp/search",
            params={"q": "P181-COMPOSITE", "category": "production"},
        )
        assert search.status_code == 200, search.text
        group = search.json()["groups"][0]
        assert group["total"] == 1
        assert [int(row["task_id"]) for row in group["items"]] == [main_id]
        assert not internal_ids.intersection(
            {
                int(row.get("task_id") or row.get("id"))
                for row in [
                    *desktop.json()["items"],
                    *station.json()["items"],
                    *lookup.json()["items"],
                    *group["items"],
                ]
            }
        )


def test_partial_composite_receipt_has_no_manual_remaining_capacity_or_duplicate_post(
    requisition_app,
) -> None:
    from app.api.production import router as production_router
    from app.models.production import ProductionCompletion
    from app.models.warehouse_inventory import InventoryLot
    from app.services.production_workflow import ensure_receipt_auto_main_task

    app, session_factory = requisition_app
    app.include_router(production_router, prefix="/api/production")
    _seed_material_and_staging(session_factory)

    with TestClient(app) as client:
        _login(client, "admin")
        sources = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=30,
            purchase_total=30,
            order_purpose=30,
            stock_purpose=0,
            composite=True,
        )
        facts: dict[str, dict] = {}
        for source in sources:
            frozen = _freeze_receipt_fact(
                client,
                source,
                idempotency_key=f"p1102-partial-price-{source.component_type}",
            )
            assert frozen.status_code == 200, frozen.text
            facts[source.component_type] = frozen.json()

        # Reproduce an already-existing legacy pending task.  Freezing the
        # purpose source must remove this row from the ordinary fast path before
        # the first receipt posts any allocation.
        with session_factory() as session:
            task = ensure_receipt_auto_main_task(session, order_item_id=1)
            task.status = "pending"
            task.planned_quantity = 30
            task.material_received_quantity = 30
            task.material_input_quantity = 30
            task.readiness_basis = "legacy_material_received"
            session.commit()

        before_first_receipt = client.get(
            "/api/production/tasks",
            params={"status": "pending", "page": 1, "page_size": 25},
        )
        assert before_first_receipt.status_code == 200, before_first_receipt.text
        before_first_row = next(
            item
            for item in before_first_receipt.json()["items"]
            if int(item["order_item_id"]) == 1
        )
        assert before_first_row["receipt_purpose_managed"] is True
        assert before_first_row["completion_actionable"] is False
        assert before_first_row["actual_output_quantity"] == 0
        before_first_duplicate = client.post(
            "/api/production/completion-batches",
            json={
                "idempotency_key": "p1102-manual-before-first-receipt",
                "items": [
                    {
                        "task_id": before_first_row["id"],
                        "expected_version": before_first_row["version"],
                        "disposition": "direct",
                        "material_input_quantity": 1,
                        "actual_output_quantity": 1,
                        "defective_quantity": 0,
                        "direct_delivery_quantity": 1,
                    }
                ],
            },
        )
        assert before_first_duplicate.status_code == 409, before_first_duplicate.text

        by_component = {source.component_type: source for source in sources}
        cover_received = _receive(
            client,
            by_component["cover"],
            facts["cover"],
            quantity=10,
            idempotency_key="p1102-partial-receive-cover",
        )
        assert cover_received.status_code == 200, cover_received.text

        zero_output_pending = client.get(
            "/api/production/tasks",
            params={"page": 1, "page_size": 25},
        )
        assert zero_output_pending.status_code == 200, zero_output_pending.text
        zero_output_row = next(
            item
            for item in zero_output_pending.json()["items"]
            if int(item["order_item_id"]) == 1
        )
        assert zero_output_row["receipt_purpose_managed"] is True
        assert zero_output_row["actual_output_quantity"] == 0
        assert zero_output_row["receipt_purpose_summary"][
            "waiting_component_labels"
        ] == ["底片"]
        assert zero_output_row["receipt_purpose_summary"][
            "waiting_component_gap_quantity"
        ] == 10
        assert "底片" in zero_output_row["completion_block_message"]
        zero_output_duplicate = client.post(
            "/api/production/completion-batches",
            json={
                "idempotency_key": "p1102-manual-before-kit-complete",
                "items": [
                    {
                        "task_id": zero_output_row["id"],
                        "expected_version": zero_output_row["version"],
                        "disposition": "direct",
                        "material_input_quantity": 1,
                        "actual_output_quantity": 1,
                        "defective_quantity": 0,
                        "direct_delivery_quantity": 1,
                    }
                ],
            },
        )
        assert zero_output_duplicate.status_code == 409, zero_output_duplicate.text
        assert "冻结收料用途自动形成成品" in zero_output_duplicate.json()["detail"]

        base_received = _receive(
            client,
            by_component["base"],
            facts["base"],
            quantity=10,
            idempotency_key="p1102-partial-receive-base",
        )
        assert base_received.status_code == 200, base_received.text

        pending = client.get(
            "/api/production/tasks",
            params={"status": "pending", "page": 1, "page_size": 25},
        )
        assert pending.status_code == 200, pending.text
        row = next(
            item
            for item in pending.json()["items"]
            if int(item["order_item_id"]) == 1
        )
        assert row["receipt_purpose_managed"] is True
        assert row["completion_actionable"] is False
        assert row["completion_block_code"] == "receipt_auto_managed"
        assert "自动形成" in row["completion_block_message"]
        assert row["available_material_input_quantity"] == 0
        assert row["planned_output_quantity"] == 0
        assert row["actual_output_quantity"] == 10
        summary = row["receipt_purpose_summary"]
        assert summary["order_purpose_received_sheet_qty"] == 20
        assert summary["reserve_purpose_received_sheet_qty"] == 0
        assert summary["automatic_finished_output_qty"] == 10
        assert summary["current_theoretical_finished_capacity_qty"] == 10
        assert summary["currently_unposted_finished_capacity_qty"] == 0
        assert summary["waiting_component_labels"] == ["盖片", "底片"]
        assert "盖片、底片" in row["completion_block_message"]
        assert {
            item["component_type"]: item["current_finished_capacity_qty"]
            for item in summary["component_progress"]
        } == {"cover": 10, "base": 10}

        with session_factory() as session:
            completion_count = int(
                session.scalar(select(func.count(ProductionCompletion.id))) or 0
            )
            finished_lot_count = int(
                session.scalar(
                    select(func.count(InventoryLot.id)).where(
                        InventoryLot.inventory_type == "finished"
                    )
                )
                or 0
            )

        duplicate = client.post(
            "/api/production/completion-batches",
            json={
                "idempotency_key": "p1102-manual-duplicate",
                "items": [
                    {
                        "task_id": row["id"],
                        "expected_version": row["version"],
                        "disposition": "direct",
                        "material_input_quantity": 10,
                        "actual_output_quantity": 10,
                        "defective_quantity": 0,
                        "direct_delivery_quantity": 10,
                    }
                ],
            },
        )
        assert duplicate.status_code == 409, duplicate.text
        assert "冻结收料用途自动形成成品" in duplicate.json()["detail"]

    with session_factory() as session:
        assert int(session.scalar(select(func.count(ProductionCompletion.id))) or 0) == completion_count
        assert int(
            session.scalar(
                select(func.count(InventoryLot.id)).where(
                    InventoryLot.inventory_type == "finished"
                )
            )
            or 0
        ) == finished_lot_count


def test_receipt_auto_reserves_only_order_quantity_not_already_covered(
    requisition_app,
) -> None:
    from app.models.order import Order, OrderItem
    from app.models.production import ProductionCompletion, ProductionTask
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryReservation,
        WarehouseLocation,
    )
    from app.services.warehouse_inventory import (
        active_finished_reserved_qty,
        manual_finished_in,
    )

    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)

    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=30,
            purchase_total=30,
            order_purpose=30,
            stock_purpose=0,
        )[0]
        frozen = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p1102-existing-coverage-price",
        )
        assert frozen.status_code == 200, frozen.text

        # Reproduce two independent facts that appear after the purchase
        # purpose was frozen but before its receipt posts: five boxes already
        # delivered and ten boxes still reserved.  This deliberately writes
        # only the isolated fixture database.
        with session_factory() as session:
            item = session.get(OrderItem, 1)
            assert item is not None
            order = session.get(Order, item.order_id)
            location = session.scalar(
                select(WarehouseLocation).where(
                    WarehouseLocation.location_code == "F1-FIN-001-L001"
                )
            )
            assert order is not None and location is not None
            item.delivered_quantity = 5
            order.status = "partially_delivered"
            existing_lot = manual_finished_in(
                session,
                customer_id=order.customer_id,
                product_id=item.product_id,
                location_id=location.id,
                quantity=10,
                stock_date=datetime.now().date(),
                source_type="manual",
                remarks="P1-102 existing finished coverage",
                operator_id=1,
                idempotency_key="p1102-existing-finished-in",
                expected_layout_version=int(location.floor3_layout.version),
            )
            existing_lot.quantity_available = 0
            existing_lot.quantity_reserved = 10
            existing_lot.version = int(existing_lot.version or 1) + 1
            session.add(
                InventoryReservation(
                    reservation_number="P1102-EXISTING-COVERAGE",
                    inventory_lot_id=existing_lot.id,
                    reservation_type="finished_order",
                    order_id=order.id,
                    order_item_id=item.id,
                    reserved_stock_quantity=10,
                    credited_requirement_quantity=10,
                    yield_factor=1,
                    status="active",
                    warning_codes="[]",
                    reserved_by=1,
                    reserved_at=datetime.now(),
                    idempotency_key="p1102-existing-finished-reserve",
                )
            )
            session.commit()

        received = _receive(
            client,
            source,
            frozen.json(),
            quantity=30,
            idempotency_key="p1102-existing-coverage-receive",
        )
        assert received.status_code == 200, received.text
        replayed = _receive(
            client,
            source,
            frozen.json(),
            quantity=30,
            idempotency_key="p1102-existing-coverage-receive",
        )
        assert replayed.status_code == 200, replayed.text

    with session_factory() as session:
        from app.services.production_workflow import (
            list_production_tasks,
            production_ready_quantity,
        )
        from app.services.receipt_managed_production import (
            receipt_purpose_summaries_by_order_item_ids,
        )

        completion = session.scalar(
            select(ProductionCompletion).where(
                ProductionCompletion.origin == "receipt_auto",
                ProductionCompletion.status == "posted",
            )
        )
        assert completion is not None
        assert int(completion.actual_output_quantity) == 30
        assert int(completion.order_reserved_quantity) == 15
        assert int(completion.surplus_finished_quantity) == 15
        auto_lot = session.get(InventoryLot, completion.inventory_lot_id)
        task = session.get(ProductionTask, completion.task_id)
        assert auto_lot is not None and task is not None
        assert int(auto_lot.quantity_reserved) == 15
        assert int(auto_lot.quantity_available) == 15
        assert active_finished_reserved_qty(session, 1) == 30
        assert int(task.finished_coverage_snapshot) == 30
        assert task.status == "completed"
        item = session.get(OrderItem, 1)
        assert item is not None
        assert production_ready_quantity(session, item) == 45
        summary = receipt_purpose_summaries_by_order_item_ids(session, [1])[1]
        assert summary["automatic_order_reserved_quantity"] == 15
        assert summary["automatic_surplus_finished_quantity"] == 15
        rows = list_production_tasks(
            session,
            allowed_customer_ids=None,
            status=None,
        )
        row = next(value for value in rows if int(value["order_item_id"]) == 1)
        assert row["order_reserved_quantity"] == 15
        assert row["surplus_finished_quantity"] == 15
        assert int(
            session.scalar(
                select(func.count(ProductionCompletion.id)).where(
                    ProductionCompletion.origin == "receipt_auto"
                )
            )
            or 0
        ) == 1


def test_inactive_frozen_snapshot_keeps_only_received_plan_capacity(
    requisition_app,
) -> None:
    from app.models.purchase_receipt import IncomingReceiptPurposeAllocation
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )
    from app.services.receipt_managed_production import (
        receipt_purpose_summaries_by_order_item_ids,
    )

    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=30,
            purchase_total=30,
            order_purpose=30,
            stock_purpose=0,
        )[0]
        fact = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p1102-inactive-plan-price",
        )
        assert fact.status_code == 200, fact.text
        received = _receive(
            client,
            source,
            fact.json(),
            quantity=10,
            idempotency_key="p1102-inactive-plan-receive",
        )
        assert received.status_code == 200, received.text

    with session_factory() as session:
        supplier_item = session.get(
            SupplierRequisitionOrderItem,
            source.supplier_item_id,
        )
        assert supplier_item is not None
        supplier_order = session.get(
            SupplierRequisitionOrder,
            supplier_item.supplier_order_id,
        )
        assert supplier_order is not None
        supplier_order.status = "voided"
        supplier_order.voided_at = datetime.now()
        session.commit()

    with session_factory() as session:
        summary = receipt_purpose_summaries_by_order_item_ids(session, [1])[1]
    assert summary["automatic_finished_output_qty"] == 10
    assert summary["future_planned_finished_capacity_qty"] == 0
    assert summary["remaining_order_purpose_sheet_qty"] == 0
    assert len(summary["component_progress"]) == 1
    component = summary["component_progress"][0]
    assert component["planned_order_sheet_qty"] == 10
    assert component["received_order_sheet_qty"] == 10
    assert component["remaining_order_sheet_qty"] == 0

    with session_factory() as session:
        allocation = session.scalar(select(IncomingReceiptPurposeAllocation))
        assert allocation is not None
        allocation.finished_output_qty_after = 11
        allocation.finished_output_qty_delta = 11
        session.commit()

    with session_factory() as session:
        inconsistent = receipt_purpose_summaries_by_order_item_ids(session, [1])[1]
    assert inconsistent["automatic_finished_output_qty"] == 11
    assert inconsistent["current_theoretical_finished_capacity_qty"] == 10
    assert inconsistent["projection_inconsistent"] is True


def test_unposted_receipt_auto_capacity_uses_projection_inconsistency_block() -> None:
    from app.services.production_workflow import _receipt_managed_completion_block

    code, message = _receipt_managed_completion_block(
        {
            "currently_unposted_finished_capacity_qty": 7,
            "projection_inconsistent": False,
            "waiting_component_labels": [],
            "waiting_component_gap_quantity": 0,
        },
        automatic_output=10,
    )
    assert code == "receipt_auto_projection_inconsistent"
    assert "7 个配套产能尚未结转" in message
    assert "停止人工完工" in message
    assert "当前没有尚未结转" not in message


def test_three_line_batch_receipt_continues_after_sibling_dispatch(
    requisition_app,
) -> None:
    """Reproduce the SO369/SO371/SO373 state shape without formal data."""

    from datetime import date

    from app.models.delivery import Delivery, DeliveryItem
    from app.models.order import Order, OrderItem
    from app.models.purchase_receipt import IncomingReceiptPurposeAllocation
    from app.models.supplier_requisition_order import SupplierRequisitionOrderItem

    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        sources = _create_frozen_source_batch(
            client,
            session_factory,
            count=3,
        )
        facts: dict[str, dict] = {}
        for index, source in enumerate(sources):
            frozen = _freeze_receipt_fact(
                client,
                source,
                idempotency_key=f"p1101-three-line-price-{index}",
            )
            assert frozen.status_code == 200, frozen.text
            facts[source.route_key] = frozen.json()

        with session_factory() as session:
            delivery = Delivery(
                delivery_number="P1-101-ANON-THREE-LINE-DISPATCH",
                customer_id=1,
                delivery_date=date(2026, 8, 24),
                status="dispatched",
                total_quantity=3,
            )
            session.add(delivery)
            session.flush()
            affected_order_ids: set[int] = set()
            for index, source in enumerate(sources, start=1):
                supplier_item = session.get(
                    SupplierRequisitionOrderItem,
                    source.supplier_item_id,
                )
                assert supplier_item is not None
                assert supplier_item.order_item_id is not None
                target = session.get(OrderItem, supplier_item.order_item_id)
                assert target is not None
                order = session.get(Order, target.order_id)
                assert order is not None
                sibling = OrderItem(
                    order_id=order.id,
                    product_id=target.product_id,
                    quantity=1,
                    unit_price=target.unit_price,
                    subtotal=target.unit_price,
                    material_status="received",
                    requisition_status="已入库",
                    delivered_quantity=1,
                    snapshot_product_name=f"匿名已送兄弟明细{index}",
                    snapshot_spec=target.snapshot_spec,
                    snapshot_material=target.snapshot_material,
                )
                session.add(sibling)
                session.flush()
                session.add(
                    DeliveryItem(
                        delivery_id=delivery.id,
                        order_item_id=sibling.id,
                        delivered_quantity=1,
                        ordered_quantity_snapshot=1,
                        order_remaining_snapshot=1,
                    )
                )
                order.status = "partially_delivered"
                affected_order_ids.add(int(order.id))
            session.commit()

        response = client.put(
            "/api/incoming/batch-receive",
            json={
                "idempotency_key": "p1101-three-line-partial-batch",
                "items": [
                    {
                        "item_id": source.route_key,
                        "received_quantity": 10,
                        "idempotency_key": (
                            f"p1101-three-line-partial-batch:{source.route_key}"
                        ),
                        "expected_receipt_fact_version": facts[source.route_key][
                            "receipt_fact_version"
                        ],
                        "purchase_purpose_source_snapshot_id": (
                            source.purpose_snapshot_id
                        ),
                        "expected_purpose_snapshot_version": (
                            source.purpose_snapshot_version
                        ),
                        "receipt_plan_fingerprint": facts[source.route_key][
                            "receipt_plan_fingerprint"
                        ],
                        "expected_actual_material_version": facts[source.route_key][
                            "actual_material_version"
                        ],
                        "actual_material_fingerprint": facts[source.route_key][
                            "actual_material_fingerprint"
                        ],
                    }
                    for source in sources
                ],
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["succeeded"] == 3, body
        assert body["failed"] == 0, body

    with session_factory() as session:
        assert set(
            session.scalars(
                select(Order.status).where(Order.id.in_(affected_order_ids))
            ).all()
        ) == {"partially_delivered"}
        allocations = list(
            session.scalars(
                select(IncomingReceiptPurposeAllocation).order_by(
                    IncomingReceiptPurposeAllocation.id
                )
            ).all()
        )
        assert len(allocations) == 3
        assert sum(int(row.finished_output_qty_delta) for row in allocations) == 30


def test_batch_receive_select_queries_are_bounded_between_one_and_six_lines(
    requisition_app,
) -> None:
    from app.models.purchase_receipt import IncomingReceiptPurposeAllocation
    from app.services.receipt_purpose_distribution import (
        serialize_receipt_purpose_allocations,
    )

    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    engine = session_factory.kw["bind"]
    with TestClient(app) as client:
        _login(client, "admin")
        sources = _create_frozen_source_batch(
            client,
            session_factory,
            count=6,
        )
        facts: dict[str, dict] = {}
        for index, source in enumerate(sources):
            frozen = _freeze_receipt_fact(
                client,
                source,
                idempotency_key=f"p181-batch-price-{index}",
            )
            assert frozen.status_code == 200, frozen.text
            facts[source.route_key] = frozen.json()

        response = client.put(
            "/api/incoming/batch-receive",
            json={
                "idempotency_key": "p181-batch-six",
                "items": [
                    {
                        "item_id": source.route_key,
                        "received_quantity": 10,
                        "idempotency_key": f"p181-batch-six:{source.route_key}",
                        "expected_receipt_fact_version": facts[source.route_key][
                            "receipt_fact_version"
                        ],
                        "purchase_purpose_source_snapshot_id": (
                            source.purpose_snapshot_id
                        ),
                        "expected_purpose_snapshot_version": (
                            source.purpose_snapshot_version
                        ),
                        "receipt_plan_fingerprint": facts[source.route_key][
                            "receipt_plan_fingerprint"
                        ],
                        "expected_actual_material_version": facts[source.route_key][
                            "actual_material_version"
                        ],
                        "actual_material_fingerprint": facts[source.route_key][
                            "actual_material_fingerprint"
                        ],
                    }
                    for source in sources
                ],
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["failed"] == 0, body
        assert body["succeeded"] == 6, body
        assert all(
            row["item"]["purpose_allocation"]["order_sheet_delta"] == 10
            for row in body["results"]
        )

    with session_factory() as session:
        allocations = list(
            session.scalars(
                select(IncomingReceiptPurposeAllocation).order_by(
                    IncomingReceiptPurposeAllocation.id
                )
            ).all()
        )
        assert len(allocations) == 6
        assert sum(int(row.finished_output_qty_delta) for row in allocations) == 60

        def count_serializer(rows: list[IncomingReceiptPurposeAllocation]) -> int:
            select_count = 0

            def count_selects(
                _connection,
                _cursor,
                statement,
                _parameters,
                _context,
                _executemany,
            ) -> None:
                nonlocal select_count
                if str(statement).lstrip().upper().startswith("SELECT"):
                    select_count += 1

            event.listen(engine, "before_cursor_execute", count_selects)
            try:
                payloads = serialize_receipt_purpose_allocations(session, rows)
            finally:
                event.remove(engine, "before_cursor_execute", count_selects)
            assert len(payloads) == len(rows)
            return select_count

        one_selects = count_serializer(allocations[:1])
        six_selects = count_serializer(allocations)

    # P1-81's allocation response projection must batch-prefetch.  The older
    # per-line receive transaction remains outside this scoped performance gate.
    assert six_selects <= one_selects + 1 and six_selects <= 5, {
        "one_line_selects": one_selects,
        "six_line_selects": six_selects,
    }


def test_customer_scope_is_checked_before_price_or_purpose_details_leak(
    requisition_app,
) -> None:
    from app.models.user import User

    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with TestClient(app) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=500,
            purchase_total=600,
            order_purpose=500,
            stock_purpose=100,
        )[0]
        frozen = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p181-price-scope",
        )
        assert frozen.status_code == 200, frozen.text
        with session_factory() as session:
            workshop = session.scalar(select(User).where(User.username == "workshop"))
            assert workshop is not None
            workshop.customer_access_mode = "selected"
            session.commit()

        _login(client, "workshop")
        baseline = _business_counts(session_factory)
        blocked = _receive(
            client,
            source,
            frozen.json(),
            quantity=1,
            idempotency_key="p181-scope-blocked",
        )
        assert blocked.status_code == 403, blocked.text
        text = str(blocked.json().get("detail", ""))
        for secret in ("2.5000", "500", "100", "KAKAK"):
            assert secret not in text
        after = _business_counts(session_factory)
        assert {key: value for key, value in after.items() if key != "audit"} == {
            key: value for key, value in baseline.items() if key != "audit"
        }
        # A denied customer-scope access is security evidence, not a forbidden
        # purchase/receipt/inventory business write.
        assert after["audit"] == baseline["audit"] + 1


def test_audit_failure_rolls_back_receipt_allocation_completion_and_inventory(
    requisition_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.services.incoming_receipts as incoming_receipts

    app, session_factory = requisition_app
    _seed_material_and_staging(session_factory)
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client, "admin")
        source = _create_frozen_sources(
            client,
            session_factory,
            order_quantity=500,
            purchase_total=600,
            order_purpose=500,
            stock_purpose=100,
        )[0]
        frozen = _freeze_receipt_fact(
            client,
            source,
            idempotency_key="p181-price-rollback",
        )
        assert frozen.status_code == 200, frozen.text
        baseline = _business_counts(session_factory)

        def explode_audit(*args, **kwargs):
            raise RuntimeError("P1-81 injected audit failure")

        monkeypatch.setattr(incoming_receipts, "append_audit_event", explode_audit)
        failed = _receive(
            client,
            source,
            frozen.json(),
            quantity=580,
            idempotency_key="p181-receive-rollback",
        )
    assert failed.status_code == 500
    assert _business_counts(session_factory) == baseline
    assert _posted_finished_quantity(session_factory) == 0
    assert _active_semi_quantity(session_factory) == 0
