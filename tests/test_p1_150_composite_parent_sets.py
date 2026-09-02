from __future__ import annotations

from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
import json
from pathlib import Path
from threading import Barrier

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tests.test_common_box_low_stock_alert import _add_finished_lot, _factory


def _seed_composite_parent_case(
    factory: sessionmaker[Session], ids: dict[str, int]
) -> dict[str, int]:
    from app.models.material import Material
    from app.models.product import Product
    from app.models.product_bom import ProductBomComponent
    from app.models.stock_replenishment import InventoryStockPolicy
    from app.models.warehouse_inventory import (
        InventoryLot,
        SemiFinishedInventoryDetail,
        WarehouseLocation,
    )

    with factory() as db:
        material = db.get(Material, ids["material_a"])
        finished_location = db.get(WarehouseLocation, ids["standard_location"])
        assert material is not None and finished_location is not None

        parent = Product(
            customer_id=ids["customer_a"],
            product_code="KIT-P1-150",
            customer_material_code="KIT-P1-150",
            product_name="组合父件800套",
            box_category="normal",
            is_composite=True,
            is_virtual_composite_parent=True,
            is_active=True,
        )
        component = Product(
            customer_id=ids["customer_a"],
            product_code="KIT-P1-150-A",
            customer_material_code="KIT-P1-150-A",
            product_name="组合组件A",
            box_style="模切内盒",
            box_category="normal",
            material_id=material.id,
            layer_count=3,
            flute_type="B",
            report_length_mm=600,
            report_width_mm=470,
            crease_type="净料",
            default_cutting_mode="一开四",
            pieces_per_box=1,
            is_internal_component=True,
            is_active=True,
        )
        semi_location = WarehouseLocation(
            location_code="SF-P1-150",
            location_name="半成品原料暂存区",
            warehouse_type="semi_finished",
            is_active=True,
        )
        db.add_all([parent, component, semi_location])
        db.flush()

        relation = ProductBomComponent(
            parent_product_id=parent.id,
            component_product_id=component.id,
            quantity_per_set=Decimal("3"),
            display_order=1,
            internal_component_code="P1-150-A",
            is_die_cut=False,
            spare_sheet_quantity=0,
            display_mode="internal_only",
            is_required=True,
        )
        policy = InventoryStockPolicy(
            policy_name="组合父件1000套目标",
            target_inventory_type="finished",
            product_id=parent.id,
            customer_id=parent.customer_id,
            warning_quantity=800,
            target_quantity=1000,
            active=True,
            created_by=ids["admin"],
            updated_by=ids["admin"],
        )
        db.add_all([relation, policy])
        db.flush()
        _add_finished_lot(
            db,
            lot_number="FG-P1-150-PARENT-200",
            product=parent,
            location=finished_location,
            quantity_available=200,
        )

        semi_lot = InventoryLot(
            lot_number="SF-P1-150-A-50",
            inventory_type="semi_finished",
            warehouse_location_id=semi_location.id,
            quantity_available=50,
            quantity_reserved=0,
            quantity_consumed=0,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit="sheets",
            status="active",
            source_type="stocktake",
            stock_date=date.today(),
            stock_date_accuracy="exact",
            last_movement_at=datetime.now(),
            version=1,
        )
        db.add(semi_lot)
        db.flush()
        db.add(
            SemiFinishedInventoryDetail(
                inventory_lot_id=semi_lot.id,
                supplier_name="匿名纸板供应商",
                owner_customer_id=parent.customer_id,
                material_id=material.id,
                owner_customer_name_snapshot="匿名客户A",
                customer_generic_eligible=True,
                internal_name="组件A通用片料",
                material_code_snapshot=material.code,
                normalized_material_code="A/K/B",
                layer_count=3,
                flute_type="B",
                board_length_mm=600,
                board_width_mm=470,
                component_type="whole",
                pieces_per_box=1,
                stock_yield_per_sheet=4,
                sheet_type="net_sheet",
                crease_type="净料",
            )
        )
        db.commit()
        return {
            **ids,
            "parent": parent.id,
            "component": component.id,
            "relation": relation.id,
            "policy": policy.id,
            "semi_lot": semi_lot.id,
            "semi_location": semi_location.id,
        }


def _scenario(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.incoming import router as incoming_router
    from app.api.production import router as production_router
    from app.api.requisition import router as requisition_router
    from app.api.warehouse import router as warehouse_router

    _engine, factory, base_ids = _factory(tmp_path)
    ids = _seed_composite_parent_case(factory, base_ids)
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(incoming_router, prefix="/api/incoming")
    app.include_router(production_router, prefix="/api/production")
    app.include_router(requisition_router, prefix="/api/requisition")
    app.include_router(warehouse_router, prefix="/api/warehouse")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    return app, factory, ids


def _login_and_expand(
    client: TestClient,
    ids: dict[str, int],
    *,
    username: str = "admin",
    expected_available_sheets: int = 50,
    expected_cutting_mode: str = "一开四",
    expected_is_die_cut: bool = False,
    expected_mold_max_yield_per_sheet: int | None = None,
) -> dict:
    assert client.post(
        "/api/auth/login",
        json={"username": username, "password": "123456"},
    ).status_code == 200
    parent_step = client.get(
        f"/api/requisition/stock-policies/{ids['policy']}/replenishment-draft"
    )
    assert parent_step.status_code == 200, parent_step.text
    parent_body = parent_step.json()
    assert parent_body["requires_parent_set_confirmation"] is True
    assert parent_body["items"] == []
    assert parent_body["composite_parent_plan"]["components"] == []
    assert parent_body["composite_parent_plan"]["current_complete_set_quantity"] == 200
    assert parent_body["composite_parent_plan"]["warning_set_quantity"] == 800
    assert parent_body["composite_parent_plan"]["target_set_quantity"] == 1000
    assert parent_body["composite_parent_plan"]["suggested_parent_set_quantity"] == 800
    assert parent_body["composite_parent_plan"]["parent_set_quantity"] == 800

    expanded = client.get(
        f"/api/requisition/stock-policies/{ids['policy']}/replenishment-draft",
        params={"parent_set_quantity": 800},
    )
    assert expanded.status_code == 200, expanded.text
    draft = expanded.json()
    assert draft["requires_parent_set_confirmation"] is False
    assert len(draft["items"]) == 1
    line = draft["items"][0]
    assert line["component_product_id"] == ids["component"]
    assert line["parent_set_quantity"] == 800
    assert line["bom_quantity_per_set"] == 3
    assert line["required_piece_quantity"] == 2400
    assert line["incoming_covered_piece_quantity"] == 0
    assert line["inventory_deducted_piece_quantity"] == 0
    assert line["net_required_piece_quantity"] == 2400
    assert line["yield_per_sheet"] == 4
    assert line["cutting_mode"] == expected_cutting_mode
    assert line["is_die_cut"] is expected_is_die_cut
    assert (
        line["mold_max_yield_per_sheet"]
        == expected_mold_max_yield_per_sheet
    )
    assert line["purchase_sheet_quantity"] == 600
    assert len(line["inventory_candidates"]) == 1
    candidate = line["inventory_candidates"][0]
    assert candidate["lot_id"] == ids["semi_lot"]
    assert candidate["version"] == 1
    assert candidate["available_stock_quantity"] == expected_available_sheets
    assert candidate["deductible_requirement_quantity"] == min(
        expected_available_sheets * 4, 2400
    )
    assert candidate["direct_deduction_eligible"] is True
    return draft


def _payload(
    draft: dict,
    ids: dict[str, int],
    *,
    idempotency_key: str,
    expected_version: int = 1,
    purchase_sheet_quantity: int = 550,
) -> dict:
    plan = deepcopy(draft["composite_parent_plan"])
    plan["components"][0].update(
        {
            "requested_offset_piece_quantity": 200,
            "selected_lots": [
                {"lot_id": ids["semi_lot"], "expected_version": expected_version}
            ],
            "purchase_sheet_quantity": purchase_sheet_quantity,
        }
    )
    item = deepcopy(draft["items"][0])
    item.update(
        {
            "quantity": purchase_sheet_quantity,
            "purchase_sheet_quantity": purchase_sheet_quantity,
            "inventory_deducted_piece_quantity": 200,
            "net_required_piece_quantity": 2200,
            "reference_product_id": ids["component"],
            "product_id": None,
        }
    )
    return {
        "source_type": "stock_warning",
        "idempotency_key": idempotency_key,
        "supplier_name": draft["supplier_name"],
        "customer_id": draft["customer_id"],
        "stock_now": False,
        "composite_parent_plan": plan,
        "items": [item],
    }


def _seed_later_composite_order(
    factory: sessionmaker[Session],
    ids: dict[str, int],
    *,
    set_quantity: int,
    order_number: str,
) -> tuple[int, int]:
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.product_bom import ProductBomComponent, SalesOrderItemBomComponent

    with factory() as db:
        parent = db.get(Product, ids["parent"])
        component = db.get(Product, ids["component"])
        relation = db.get(ProductBomComponent, ids["relation"])
        assert parent is not None and component is not None and relation is not None
        order = Order(
            order_number=order_number,
            customer_id=ids["customer_a"],
            order_date=date.today(),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal(set_quantity),
        )
        db.add(order)
        db.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=parent.id,
            quantity=set_quantity,
            unit_price=Decimal("1"),
            subtotal=Decimal(set_quantity),
            material_status="pending",
            requisition_status="未报料",
            snapshot_product_code=parent.product_code,
            snapshot_product_name=parent.product_name,
            snapshot_spec=f"组合父件{set_quantity}套",
            snapshot_material=None,
            snapshot_splice_mode="single",
            snapshot_pieces_per_box=1,
        )
        db.add(item)
        db.flush()
        required_pieces = set_quantity * 3
        snapshot = SalesOrderItemBomComponent(
            sales_order_item_id=item.id,
            product_bom_component_id=ids["relation"],
            component_product_id=component.id,
            parent_product_version=int(parent.version or 1),
            component_product_version=int(component.version or 1),
            order_set_quantity=set_quantity,
            quantity_per_set=Decimal("3"),
            required_piece_quantity=Decimal(required_pieces),
            display_order=1,
            internal_component_code="P1-150-A",
            is_die_cut=False,
            snapshot_mold_tool_id=None,
            mold_max_yield_per_sheet=None,
            spare_sheet_quantity=int(relation.spare_sheet_quantity or 0),
            display_mode="internal_only",
            is_required=True,
            snapshot_component_product_code=component.product_code,
            snapshot_component_product_name=component.product_name,
            snapshot_component_spec="600×470",
            snapshot_component_material="A/K/B",
            snapshot_component_material_id=component.material_id,
            snapshot_component_supplier_name="匿名纸板供应商",
            snapshot_component_layer_count=3,
            snapshot_component_flute_type="B",
            snapshot_component_box_category="normal",
            snapshot_component_box_style=component.box_style,
            snapshot_component_default_cutting_mode="一开四",
            snapshot_component_splice_mode="single",
            snapshot_component_pieces_per_box=1,
            snapshot_component_report_length_mm=600,
            snapshot_component_report_width_mm=470,
            snapshot_component_crease_type="净料",
        )
        db.add(snapshot)
        db.flush()
        db.commit()
        return int(item.id), int(snapshot.id)


def _formal_counts(factory: sessionmaker[Session]) -> dict[str, int]:
    from app.models.audit import OperationLog
    from app.models.stock_replenishment import (
        StockReplenishmentBomComponentPlan,
        StockReplenishmentOrder,
        StockReplenishmentOrderItem,
    )
    from app.models.warehouse_inventory import InventoryMovement, InventoryReservation

    with factory() as db:
        return {
            "orders": int(db.scalar(select(func.count(StockReplenishmentOrder.id))) or 0),
            "items": int(
                db.scalar(select(func.count(StockReplenishmentOrderItem.id))) or 0
            ),
            "plans": int(
                db.scalar(select(func.count(StockReplenishmentBomComponentPlan.id)))
                or 0
            ),
            "reservations": int(
                db.scalar(select(func.count(InventoryReservation.id))) or 0
            ),
            "movements": int(db.scalar(select(func.count(InventoryMovement.id))) or 0),
            "create_logs": int(
                db.scalar(
                    select(func.count(OperationLog.id)).where(
                        OperationLog.action == "CREATE_COMPOSITE_STOCK_REPLENISHMENT"
                    )
                )
                or 0
            ),
        }


def test_parent_sets_submit_reserves_exact_component_inventory_idempotently_and_voids(
    tmp_path: Path,
) -> None:
    from app.models.audit import OperationLog
    from app.models.stock_replenishment import (
        StockReplenishmentBomComponentPlan,
        StockReplenishmentOrder,
        StockReplenishmentOrderItem,
    )
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryMovement,
        InventoryReservation,
    )

    app, factory, ids = _scenario(tmp_path)
    with TestClient(app) as client:
        draft = _login_and_expand(client, ids)
        assert _formal_counts(factory) == {
            "orders": 0,
            "items": 0,
            "plans": 0,
            "reservations": 0,
            "movements": 0,
            "create_logs": 0,
        }
        payload = _payload(draft, ids, idempotency_key="p1-150-success")
        created = client.post(
            "/api/requisition/stock-replenishment/orders", json=payload
        )
        assert created.status_code == 201, created.text
        order_id = created.json()["id"]

        with factory() as db:
            order = db.get(StockReplenishmentOrder, order_id)
            items = list(
                db.scalars(
                    select(StockReplenishmentOrderItem).where(
                        StockReplenishmentOrderItem.replenishment_order_id == order_id
                    )
                ).all()
            )
            plans = list(
                db.scalars(
                    select(StockReplenishmentBomComponentPlan).where(
                        StockReplenishmentBomComponentPlan.replenishment_order_id
                        == order_id
                    )
                ).all()
            )
            reservations = list(db.scalars(select(InventoryReservation)).all())
            lot = db.get(InventoryLot, ids["semi_lot"])
            assert order is not None and order.status == "confirmed"
            assert len(items) == 1
            assert items[0].reference_product_id == ids["component"]
            assert items[0].reference_product_id != ids["parent"]
            assert items[0].quantity == 550
            assert len(plans) == 1
            assert plans[0].parent_product_id == ids["parent"]
            assert plans[0].component_product_id == ids["component"]
            assert plans[0].parent_set_quantity == 800
            assert plans[0].quantity_per_set == 3
            assert plans[0].required_piece_quantity == 2400
            assert plans[0].incoming_covered_piece_quantity == 0
            assert plans[0].reserved_piece_quantity == 200
            assert plans[0].net_required_piece_quantity == 2200
            assert plans[0].yield_per_sheet == 4
            assert plans[0].cutting_mode_snapshot == "一开四"
            assert plans[0].is_die_cut_snapshot is False
            assert plans[0].mold_max_yield_per_sheet_snapshot is None
            assert plans[0].purchase_sheet_quantity == 550
            assert plans[0].cutting_remainder_piece_quantity == 0
            assert plans[0].replenishment_item_id == items[0].id
            assert len(reservations) == 1
            assert reservations[0].reservation_type == "semi_requisition"
            assert reservations[0].stock_replenishment_bom_component_plan_id == plans[0].id
            assert reservations[0].reserved_stock_quantity == 50
            assert reservations[0].credited_requirement_quantity == 200
            assert reservations[0].yield_factor == 4
            assert reservations[0].status == "active"
            assert lot is not None
            assert (lot.quantity_available, lot.quantity_reserved, lot.version) == (0, 50, 2)
            assert db.scalar(
                select(func.count(InventoryMovement.id)).where(
                    InventoryMovement.inventory_lot_id == lot.id,
                    InventoryMovement.movement_type == "reserve",
                )
            ) == 1
            assert db.scalar(
                select(func.count(OperationLog.id)).where(
                    OperationLog.action == "CREATE_COMPOSITE_STOCK_REPLENISHMENT"
                )
            ) == 1
            reservation_id = reservations[0].id

        blocked_direct_release = client.post(
            f"/api/warehouse/semi-finished/reservations/{reservation_id}/release",
            json={
                "expected_version": 2,
                "release_reason": "不得绕过补库单作废",
                "idempotency_key": "p1-150-direct-release-blocked",
            },
        )
        assert blocked_direct_release.status_code == 409
        assert "只能随补库单作废统一释放" in blocked_direct_release.text
        assert _formal_counts(factory)["movements"] == 1

        replay = client.post(
            "/api/requisition/stock-replenishment/orders", json=payload
        )
        assert replay.status_code == 201, replay.text
        assert replay.json()["id"] == order_id
        assert _formal_counts(factory) == {
            "orders": 1,
            "items": 1,
            "plans": 1,
            "reservations": 1,
            "movements": 1,
            "create_logs": 1,
        }

        changed = deepcopy(payload)
        changed["composite_parent_plan"]["parent_set_quantity"] = 799
        changed_replay = client.post(
            "/api/requisition/stock-replenishment/orders", json=changed
        )
        assert changed_replay.status_code == 409
        assert "同一防重复标识" in changed_replay.text
        assert _formal_counts(factory)["orders"] == 1

        duplicate = deepcopy(payload)
        duplicate["idempotency_key"] = "p1-150-second-active-plan"
        duplicate_response = client.post(
            "/api/requisition/stock-replenishment/orders", json=duplicate
        )
        assert duplicate_response.status_code == 409
        assert "已有未完成补库单" in duplicate_response.text
        assert _formal_counts(factory) == {
            "orders": 1,
            "items": 1,
            "plans": 1,
            "reservations": 1,
            "movements": 1,
            "create_logs": 1,
        }

        voided = client.put(
            f"/api/requisition/stock-replenishment/orders/{order_id}/void"
        )
        assert voided.status_code == 200, voided.text
        assert voided.json()["status"] == "voided"
        with factory() as db:
            lot = db.get(InventoryLot, ids["semi_lot"])
            reservation = db.scalar(select(InventoryReservation))
            assert lot is not None and reservation is not None
            assert (lot.quantity_available, lot.quantity_reserved, lot.version) == (50, 0, 3)
            assert reservation.status == "released"
            assert reservation.released_stock_quantity == 50
            assert reservation.released_requirement_quantity == 200
            assert db.scalar(
                select(func.count(InventoryMovement.id)).where(
                    InventoryMovement.inventory_lot_id == lot.id,
                    InventoryMovement.movement_type == "release_reserve",
                )
            ) == 1

        repeated_void = client.put(
            f"/api/requisition/stock-replenishment/orders/{order_id}/void"
        )
        assert repeated_void.status_code == 200, repeated_void.text
        assert _formal_counts(factory)["movements"] == 2


def test_tampered_component_purchase_quantity_is_rejected_without_writes(
    tmp_path: Path,
) -> None:
    app, factory, ids = _scenario(tmp_path)
    with TestClient(app) as client:
        draft = _login_and_expand(client, ids)
        payload = _payload(
            draft,
            ids,
            idempotency_key="p1-150-tampered",
            purchase_sheet_quantity=549,
        )
        response = client.post(
            "/api/requisition/stock-replenishment/orders", json=payload
        )
        assert response.status_code == 409, response.text
        assert "应采购550张" in response.text
        assert _formal_counts(factory) == {
            "orders": 0,
            "items": 0,
            "plans": 0,
            "reservations": 0,
            "movements": 0,
            "create_logs": 0,
        }


def test_two_components_use_independent_yields_and_preserve_spare_sheets(
    tmp_path: Path,
) -> None:
    from app.models.product import Product
    from app.models.product_bom import ProductBomComponent
    from app.models.stock_replenishment import (
        StockReplenishmentBomComponentPlan,
        StockReplenishmentOrderItem,
    )

    app, factory, ids = _scenario(tmp_path)
    with factory() as db:
        component_a = db.get(Product, ids["component"])
        assert component_a is not None
        component_b = Product(
            customer_id=ids["customer_a"],
            product_code="KIT-P1-150-B",
            customer_material_code="KIT-P1-150-B",
            product_name="组合组件B",
            box_style="模切内盒",
            box_category="normal",
            material_id=component_a.material_id,
            layer_count=3,
            flute_type="B",
            report_length_mm=700,
            report_width_mm=480,
            crease_type="净料",
            default_cutting_mode="一开五",
            pieces_per_box=1,
            is_internal_component=True,
            is_active=True,
        )
        db.add(component_b)
        db.flush()
        db.add(
            ProductBomComponent(
                parent_product_id=ids["parent"],
                component_product_id=component_b.id,
                quantity_per_set=Decimal("2"),
                display_order=2,
                internal_component_code="P1-150-B",
                is_die_cut=False,
                spare_sheet_quantity=5,
                display_mode="internal_only",
                is_required=True,
            )
        )
        db.commit()
        component_b_id = component_b.id

    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "123456"},
        ).status_code == 200
        response = client.get(
            f"/api/requisition/stock-policies/{ids['policy']}/replenishment-draft",
            params={"parent_set_quantity": 800},
        )
        assert response.status_code == 200, response.text
        draft = response.json()
        lines = {row["component_product_id"]: row for row in draft["items"]}
        assert set(lines) == {ids["component"], component_b_id}
        assert (
            lines[ids["component"]]["required_piece_quantity"],
            lines[ids["component"]]["yield_per_sheet"],
            lines[ids["component"]]["purchase_sheet_quantity"],
        ) == (2400, 4, 600)
        assert (
            lines[component_b_id]["required_piece_quantity"],
            lines[component_b_id]["yield_per_sheet"],
            lines[component_b_id]["spare_sheet_quantity"],
            lines[component_b_id]["purchase_sheet_quantity"],
        ) == (1600, 5, 5, 325)

        plan = deepcopy(draft["composite_parent_plan"])
        plan_a = next(
            row
            for row in plan["components"]
            if row["component_product_id"] == ids["component"]
        )
        plan_a.update(
            {
                "requested_offset_piece_quantity": 200,
                "selected_lots": [
                    {"lot_id": ids["semi_lot"], "expected_version": 1}
                ],
                "purchase_sheet_quantity": 550,
            }
        )
        items = []
        for source in draft["items"]:
            item = deepcopy(source)
            item["reference_product_id"] = source["component_product_id"]
            item["product_id"] = None
            if source["component_product_id"] == ids["component"]:
                item.update(
                    quantity=550,
                    purchase_sheet_quantity=550,
                    inventory_deducted_piece_quantity=200,
                    net_required_piece_quantity=2200,
                )
            items.append(item)
        created = client.post(
            "/api/requisition/stock-replenishment/orders",
            json={
                "source_type": "stock_warning",
                "idempotency_key": "p1-150-two-components",
                "supplier_name": draft["supplier_name"],
                "customer_id": ids["customer_a"],
                "stock_now": False,
                "composite_parent_plan": plan,
                "items": items,
            },
        )
        assert created.status_code == 201, created.text
        order_id = created.json()["id"]

    with factory() as db:
        plans = list(
            db.scalars(
                select(StockReplenishmentBomComponentPlan)
                .where(
                    StockReplenishmentBomComponentPlan.replenishment_order_id
                    == order_id
                )
                .order_by(StockReplenishmentBomComponentPlan.display_order)
            )
        )
        items = list(
            db.scalars(
                select(StockReplenishmentOrderItem).where(
                    StockReplenishmentOrderItem.replenishment_order_id == order_id
                )
            )
        )
        assert len(plans) == len(items) == 2
        assert {item.reference_product_id for item in items} == {
            ids["component"],
            component_b_id,
        }
        assert all(item.reference_product_id != ids["parent"] for item in items)
        assert (
            plans[0].required_piece_quantity,
            plans[0].reserved_piece_quantity,
            plans[0].purchase_sheet_quantity,
        ) == (2400, 200, 550)
        assert (
            plans[1].required_piece_quantity,
            plans[1].yield_per_sheet,
            plans[1].spare_sheet_quantity,
            plans[1].purchase_sheet_quantity,
            plans[1].cutting_remainder_piece_quantity,
        ) == (1600, 5, 5, 325, 0)


def test_concurrent_different_keys_create_only_one_active_policy_plan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import app.api.requisition as requisition_api

    app, factory, ids = _scenario(tmp_path)
    with TestClient(app) as client:
        draft = _login_and_expand(client, ids)
    barrier = Barrier(2)
    original_claim = requisition_api._claim_stock_policy_replenishment_slot

    def synchronized_claim(db: Session, *, policy_id: int):
        barrier.wait(timeout=10)
        return original_claim(db, policy_id=policy_id)

    monkeypatch.setattr(
        requisition_api,
        "_claim_stock_policy_replenishment_slot",
        synchronized_claim,
    )

    def submit(key: str):
        plan = deepcopy(draft["composite_parent_plan"])
        item = deepcopy(draft["items"][0])
        item.update(
            reference_product_id=ids["component"],
            product_id=None,
        )
        with TestClient(app) as client:
            assert client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "123456"},
            ).status_code == 200
            return client.post(
                "/api/requisition/stock-replenishment/orders",
                json={
                    "source_type": "stock_warning",
                    "idempotency_key": key,
                    "supplier_name": draft["supplier_name"],
                    "customer_id": ids["customer_a"],
                    "stock_now": False,
                    "composite_parent_plan": plan,
                    "items": [item],
                },
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(submit, ("p1-150-concurrent-a", "p1-150-concurrent-b"))
        )
    assert sorted(response.status_code for response in responses) == [201, 409]
    assert _formal_counts(factory) == {
        "orders": 1,
        "items": 1,
        "plans": 1,
        "reservations": 0,
        "movements": 0,
        "create_logs": 1,
    }


def test_composite_parent_submit_requires_idempotency_key_without_writes(
    tmp_path: Path,
) -> None:
    app, factory, ids = _scenario(tmp_path)
    with TestClient(app) as client:
        draft = _login_and_expand(client, ids)
        payload = _payload(draft, ids, idempotency_key="discarded")
        payload.pop("idempotency_key")
        payload["source_type"] = "customer_request"
        payload["composite_parent_plan"]["stock_policy_id"] = None
        response = client.post(
            "/api/requisition/stock-replenishment/orders", json=payload
        )
        assert response.status_code == 409
        assert "缺少防重复标识" in response.text
        assert _formal_counts(factory) == {
            "orders": 0,
            "items": 0,
            "plans": 0,
            "reservations": 0,
            "movements": 0,
            "create_logs": 0,
        }


def test_stale_component_lot_version_rolls_back_order_plan_and_reservation(
    tmp_path: Path,
) -> None:
    from app.models.warehouse_inventory import InventoryLot

    app, factory, ids = _scenario(tmp_path)
    with TestClient(app) as client:
        draft = _login_and_expand(client, ids)
        payload = _payload(draft, ids, idempotency_key="p1-150-stale-lot")
        with factory() as db:
            lot = db.get(InventoryLot, ids["semi_lot"])
            assert lot is not None
            lot.version = 2
            db.commit()

        response = client.post(
            "/api/requisition/stock-replenishment/orders", json=payload
        )
        assert response.status_code == 409
        assert "库存已被其他人修改" in response.text
        assert _formal_counts(factory) == {
            "orders": 0,
            "items": 0,
            "plans": 0,
            "reservations": 0,
            "movements": 0,
            "create_logs": 0,
        }
        with factory() as db:
            lot = db.get(InventoryLot, ids["semi_lot"])
            assert lot is not None
            assert (lot.quantity_available, lot.quantity_reserved, lot.version) == (50, 0, 2)


def test_virtual_composite_parent_cannot_bypass_plan_as_physical_item(
    tmp_path: Path,
) -> None:
    app, factory, ids = _scenario(tmp_path)
    with TestClient(app) as client:
        draft = _login_and_expand(client, ids)
        item = deepcopy(draft["items"][0])
        item.update(
            {
                "reference_product_id": ids["parent"],
                "product_id": None,
                "stock_policy_id": None,
                "quantity": 1,
                "purchase_sheet_quantity": 1,
            }
        )
        response = client.post(
            "/api/requisition/stock-replenishment/orders",
            json={
                "source_type": "customer_request",
                "idempotency_key": "p1-150-parent-bypass",
                "supplier_name": draft["supplier_name"],
                "customer_id": ids["customer_a"],
                "stock_now": False,
                "items": [item],
            },
        )
        assert response.status_code == 409, response.text
        assert "虚拟组合父件" in response.text
        assert _formal_counts(factory) == {
            "orders": 0,
            "items": 0,
            "plans": 0,
            "reservations": 0,
            "movements": 0,
            "create_logs": 0,
        }


def test_fully_offset_zero_purchase_plan_remains_customer_scoped_and_voidable(
    tmp_path: Path,
) -> None:
    from app.models.warehouse_inventory import InventoryLot

    app, factory, ids = _scenario(tmp_path)
    with factory() as db:
        lot = db.get(InventoryLot, ids["semi_lot"])
        assert lot is not None
        lot.quantity_available = 600
        db.commit()

    with TestClient(app) as customer_a_client:
        draft = _login_and_expand(
            customer_a_client,
            ids,
            username="no-warehouse",
            expected_available_sheets=600,
        )
        candidate = draft["items"][0]["inventory_candidates"][0]
        assert candidate["deductible_requirement_quantity"] == 2400
        plan = deepcopy(draft["composite_parent_plan"])
        plan["components"][0].update(
            {
                "requested_offset_piece_quantity": 2400,
                "selected_lots": [
                    {
                        "lot_id": ids["semi_lot"],
                        "expected_version": candidate["version"],
                    }
                ],
                "purchase_sheet_quantity": 0,
            }
        )
        created = customer_a_client.post(
            "/api/requisition/stock-replenishment/orders",
            json={
                "source_type": "stock_warning",
                "idempotency_key": "p1-150-full-offset",
                "supplier_name": draft["supplier_name"],
                "customer_id": ids["customer_a"],
                "stock_now": False,
                "composite_parent_plan": plan,
                "items": [],
            },
        )
        assert created.status_code == 201, created.text
        order_id = created.json()["id"]
        assert created.json()["items"] == []
        assert customer_a_client.get(
            f"/api/requisition/stock-replenishment/orders/{order_id}"
        ).status_code == 200
        visible = customer_a_client.get(
            "/api/requisition/stock-replenishment/orders"
        )
        assert [row["id"] for row in visible.json()["items"]] == [order_id]

        with TestClient(app) as customer_b_client:
            assert customer_b_client.post(
                "/api/auth/login",
                json={"username": "other-customer", "password": "123456"},
            ).status_code == 200
            hidden = customer_b_client.get(
                "/api/requisition/stock-replenishment/orders"
            )
            assert hidden.status_code == 200
            assert hidden.json()["items"] == []
            assert customer_b_client.get(
                f"/api/requisition/stock-replenishment/orders/{order_id}"
            ).status_code == 403

        assert customer_a_client.put(
            f"/api/requisition/stock-replenishment/orders/{order_id}/void"
        ).status_code == 200

    assert _formal_counts(factory) == {
        "orders": 1,
        "items": 0,
        "plans": 1,
        "reservations": 1,
        "movements": 2,
        "create_logs": 1,
    }


def test_void_inventory_version_conflict_returns_409_and_rolls_back(
    tmp_path: Path, monkeypatch,
) -> None:
    import app.api.requisition as requisition_api
    from app.models.stock_replenishment import StockReplenishmentOrder
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation
    from app.services.warehouse_inventory import WarehouseInventoryError

    app, factory, ids = _scenario(tmp_path)
    with TestClient(app) as client:
        draft = _login_and_expand(client, ids)
        created = client.post(
            "/api/requisition/stock-replenishment/orders",
            json=_payload(draft, ids, idempotency_key="p1-150-void-conflict"),
        )
        assert created.status_code == 201, created.text
        order_id = created.json()["id"]

        def fail_after_pending_mutation(db: Session, **kwargs):
            reservation = db.get(InventoryReservation, kwargs["reservation_id"])
            assert reservation is not None
            reservation.status = "released"
            raise WarehouseInventoryError("库存已被其他人修改，请刷新后重试。", 409)

        monkeypatch.setattr(
            requisition_api,
            "release_semi_finished_reservation",
            fail_after_pending_mutation,
        )
        response = client.put(
            f"/api/requisition/stock-replenishment/orders/{order_id}/void"
        )
        assert response.status_code == 409
        assert "刷新后重试" in response.text

    with factory() as db:
        order = db.get(StockReplenishmentOrder, order_id)
        reservation = db.scalar(select(InventoryReservation))
        lot = db.get(InventoryLot, ids["semi_lot"])
        assert order is not None and order.status == "confirmed"
        assert reservation is not None and reservation.status == "active"
        assert lot is not None
        assert (lot.quantity_available, lot.quantity_reserved, lot.version) == (0, 50, 2)


def test_plan_reserved_component_can_be_manually_transferred_to_later_order(
    tmp_path: Path,
) -> None:
    from app.models.mold_tool import MoldTool
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.product_bom import (
        ProductBomComponent,
        SalesOrderItemBomComponent,
    )
    from app.models.stock_replenishment import StockReplenishmentOrder
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation
    from app.services.warehouse_inventory import component_inventory_coverage

    app, factory, ids = _scenario(tmp_path)
    with factory() as db:
        lot = db.get(InventoryLot, ids["semi_lot"])
        component = db.get(Product, ids["component"])
        relation = db.get(ProductBomComponent, ids["relation"])
        assert lot is not None and component is not None and relation is not None
        mold = MoldTool(
            mold_code="P1-150-MOLD-4",
            mold_name="P1-150 一模四",
            rack_location="测试模具位",
            is_active=True,
        )
        db.add(mold)
        db.flush()
        lot.quantity_available = 600
        component.default_cutting_mode = "一开一"
        component.mold_tool_id = mold.id
        relation.is_die_cut = True
        relation.mold_tool_id = mold.id
        relation.mold_max_yield_per_sheet = 4
        db.commit()
        mold_id = mold.id

    with TestClient(app) as client:
        draft = _login_and_expand(
            client,
            ids,
            expected_available_sheets=600,
            expected_cutting_mode="一开一",
            expected_is_die_cut=True,
            expected_mold_max_yield_per_sheet=4,
        )
        candidate = draft["items"][0]["inventory_candidates"][0]
        plan = deepcopy(draft["composite_parent_plan"])
        plan["components"][0].update(
            {
                "requested_offset_piece_quantity": 2400,
                "selected_lots": [
                    {
                        "lot_id": ids["semi_lot"],
                        "expected_version": candidate["version"],
                    }
                ],
                "purchase_sheet_quantity": 0,
            }
        )
        created = client.post(
            "/api/requisition/stock-replenishment/orders",
            json={
                "source_type": "stock_warning",
                "idempotency_key": "p1-150-transfer-source",
                "supplier_name": draft["supplier_name"],
                "customer_id": ids["customer_a"],
                "stock_now": False,
                "composite_parent_plan": plan,
                "items": [],
            },
        )
        assert created.status_code == 201, created.text
        source_order_id = created.json()["id"]
        source_order_number = created.json()["order_number"]

    with TestClient(app) as scoped_client:
        assert scoped_client.post(
            "/api/auth/login",
            json={"username": "scoped", "password": "123456"},
        ).status_code == 200
        scoped_documents = scoped_client.get(
            "/api/requisition/reported-documents",
            params={"page": 1, "page_size": 20},
        )
        scoped_items = scoped_client.get(
            "/api/requisition/reported-items",
            params={"source_type": "stock_replenishment", "page_size": 20},
        )
        assert scoped_documents.status_code == 200, scoped_documents.text
        assert scoped_items.status_code == 200, scoped_items.text
        visible_document = next(
            row
            for row in scoped_documents.json()["items"]
            if row["document_number"] == source_order_number
        )
        visible_item = next(
            row
            for row in scoped_items.json()["items"]
            if row["document_number"] == source_order_number
        )
        assert visible_document["source_type"] == "stock_replenishment"
        assert visible_document["can_void"] is True
        assert visible_item["customer_id"] == ids["customer_a"]
        assert visible_item["is_plan_only"] is True
        assert visible_item["item_id"] < 0
        assert visible_item["can_print_task"] is False
        assert visible_item["can_view_supplier_order"] is False
        zero_purchase_print = scoped_client.get(
            f"/api/requisition/stock-replenishment/orders/{source_order_id}/print"
        )
        assert zero_purchase_print.status_code == 409, zero_purchase_print.text

    with TestClient(app) as other_customer_client:
        assert other_customer_client.post(
            "/api/auth/login",
            json={"username": "other-customer", "password": "123456"},
        ).status_code == 200
        other_documents = other_customer_client.get(
            "/api/requisition/reported-documents",
            params={"page": 1, "page_size": 20},
        )
        other_items = other_customer_client.get(
            "/api/requisition/reported-items",
            params={"source_type": "stock_replenishment", "page_size": 20},
        )
        assert other_documents.status_code == 200, other_documents.text
        assert other_items.status_code == 200, other_items.text
        assert all(
            row["document_number"] != source_order_number
            for row in other_documents.json()["items"]
        )
        assert all(
            row["document_number"] != source_order_number
            for row in other_items.json()["items"]
        )

    with factory() as db:
        component = db.get(Product, ids["component"])
        assert component is not None
        component.customer_id = ids["customer_b"]
        db.commit()

    with TestClient(app) as scoped_client:
        assert scoped_client.post(
            "/api/auth/login",
            json={"username": "scoped", "password": "123456"},
        ).status_code == 200
        hidden_documents = scoped_client.get(
            "/api/requisition/reported-documents",
            params={"page": 1, "page_size": 20},
        )
        hidden_items = scoped_client.get(
            "/api/requisition/reported-items",
            params={"source_type": "stock_replenishment", "page_size": 20},
        )
        hidden_detail = scoped_client.get(
            f"/api/requisition/stock-replenishment/orders/{source_order_id}"
        )
        assert hidden_documents.status_code == 200, hidden_documents.text
        assert hidden_items.status_code == 200, hidden_items.text
        assert hidden_detail.status_code == 403, hidden_detail.text
        assert all(
            row["document_number"] != source_order_number
            for row in hidden_documents.json()["items"]
        )
        assert all(
            row["document_number"] != source_order_number
            for row in hidden_items.json()["items"]
        )

    with factory() as db:
        component = db.get(Product, ids["component"])
        assert component is not None
        component.customer_id = ids["customer_a"]
        db.commit()

    with factory() as db:
        component = db.get(Product, ids["component"])
        parent = db.get(Product, ids["parent"])
        assert component is not None and parent is not None
        alternate_component = Product(
            customer_id=ids["customer_a"],
            product_code="KIT-P1-150-B",
            customer_material_code="KIT-P1-150-B",
            product_name="同客户同规格组件B",
            box_style="模切内盒",
            box_category="normal",
            material_id=component.material_id,
            layer_count=3,
            flute_type="B",
            report_length_mm=600,
            report_width_mm=470,
            crease_type="净料",
            default_cutting_mode="一开一",
            pieces_per_box=2,
            mold_tool_id=mold_id,
            is_internal_component=True,
            is_active=True,
        )
        db.add(alternate_component)
        db.flush()
        source_order = db.get(StockReplenishmentOrder, source_order_id)
        assert source_order is not None
        source_order.status = "stocked"
        source_order.stocked_at = datetime.now()
        order = Order(
            order_number="SO-P1-150-TRANSFER",
            customer_id=ids["customer_a"],
            order_date=date.today(),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("800"),
        )
        db.add(order)
        db.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=parent.id,
            quantity=800,
            unit_price=Decimal("1"),
            subtotal=Decimal("800"),
            material_status="pending",
            requisition_status="未报料",
            snapshot_product_code=parent.product_code,
            snapshot_product_name=parent.product_name,
            snapshot_spec="组合父件800套",
            snapshot_material=None,
            snapshot_splice_mode="single",
            snapshot_pieces_per_box=1,
        )
        db.add(item)
        db.flush()
        snapshot = SalesOrderItemBomComponent(
            sales_order_item_id=item.id,
            product_bom_component_id=None,
            component_product_id=alternate_component.id,
            parent_product_version=int(parent.version or 1),
            component_product_version=int(alternate_component.version or 1),
            order_set_quantity=800,
            quantity_per_set=Decimal("3"),
            required_piece_quantity=Decimal("2400"),
            display_order=1,
            internal_component_code="P1-150-A",
            is_die_cut=True,
            snapshot_mold_tool_id=mold_id,
            mold_max_yield_per_sheet=4,
            spare_sheet_quantity=0,
            display_mode="internal_only",
            is_required=True,
            snapshot_component_product_code=alternate_component.product_code,
            snapshot_component_product_name=alternate_component.product_name,
            snapshot_component_spec="600×470",
            snapshot_component_material="A/K/B",
            snapshot_component_material_id=alternate_component.material_id,
            snapshot_component_supplier_name="匿名纸板供应商",
            snapshot_component_layer_count=3,
            snapshot_component_flute_type="B",
            snapshot_component_box_category="normal",
            snapshot_component_box_style=component.box_style,
            snapshot_component_default_cutting_mode="一开一",
            snapshot_component_splice_mode="single",
            snapshot_component_pieces_per_box=1,
            snapshot_component_report_length_mm=600,
            snapshot_component_report_width_mm=470,
            snapshot_component_crease_type="净料",
        )
        db.add(snapshot)
        db.flush()
        db.commit()
        alternate_component_id = alternate_component.id
        item_id = item.id
        snapshot_id = snapshot.id

    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "123456"},
        ).status_code == 200
        candidate_response = client.post(
            f"/api/warehouse/semi-finished/products/{alternate_component_id}/candidates",
            json={
                "customer_id": ids["customer_a"],
                "board_length_mm": 600,
                "board_width_mm": 470,
                "material_code": "A/K/B",
                "flute_type": "B",
                "component_type": "whole",
                "pieces_per_box": 1,
                "stock_yield_per_sheet": 4,
                "layer_count": 3,
                "crease_type": "净料",
            },
        )
        assert candidate_response.status_code == 200, candidate_response.text
        # The public product endpoint must not trust forged conversion values;
        # current product facts (pieces=2, yield=1) therefore cannot expose the
        # frozen pieces=1/yield=4 lot as a direct candidate.
        assert candidate_response.json()["items"] == []
        transfer_key = "t" * 60
        transferred = client.post(
            f"/api/warehouse/finished/bom-components/{snapshot_id}/auto-cover",
            json={
                "order_item_id": item_id,
                "idempotency_key": transfer_key,
                "component_type": "whole",
            },
        )
        assert transferred.status_code == 200, transferred.text
        replay = client.post(
            f"/api/warehouse/finished/bom-components/{snapshot_id}/auto-cover",
            json={
                "order_item_id": item_id,
                "idempotency_key": transfer_key,
                "component_type": "whole",
            },
        )
        assert replay.status_code == 200, replay.text

    with factory() as db:
        from app.models.warehouse_inventory import OrderItemSemiRequirement

        source_order = db.get(StockReplenishmentOrder, source_order_id)
        lot = db.get(InventoryLot, ids["semi_lot"])
        reservations = list(
            db.scalars(select(InventoryReservation).order_by(InventoryReservation.id))
        )
        from app.models.audit import OperationLog

        transfer_log = db.scalar(
            select(OperationLog).where(
                OperationLog.action == "TRANSFER_STOCK_PLAN_TO_ORDER"
            )
        )
        assert source_order is not None and source_order.status == "stocked"
        assert lot is not None
        assert (lot.quantity_available, lot.quantity_reserved, lot.version) == (0, 600, 4)
        assert [row.status for row in reservations] == ["released", "active"]
        assert reservations[1].reservation_type == "semi_order"
        assert reservations[1].order_item_id == item_id
        assert reservations[1].sales_order_item_bom_component_id == snapshot_id
        assert reservations[1].credited_requirement_quantity == 2400
        requirement = db.scalar(
            select(OrderItemSemiRequirement).where(
                OrderItemSemiRequirement.sales_order_item_bom_component_id
                == snapshot_id
            )
        )
        assert requirement is not None
        assert (requirement.pieces_per_box, requirement.stock_yield_per_sheet) == (
            1,
            4,
        )
        assert component_inventory_coverage(db, snapshot_id)["total_piece_quantity"] == 2400
        assert transfer_log is not None
        transfer_details = json.loads(transfer_log.details)
        assert transfer_details["sources"][0]["source_reservation_id"] == reservations[0].id


def test_purchase_only_receipt_reserves_plan_then_transfers_before_free_spare(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import app.services.stock_replenishment as stock_replenishment_service
    from app.models.material import Material
    from app.models.product_bom import ProductBomComponent
    from app.models.stock_replenishment import (
        InventoryStockPolicy,
        StockReplenishmentOrder,
        StockReplenishmentOrderItem,
    )
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryReservation,
        WarehouseLocation,
    )
    from app.services.stock_replenishment import (
        active_stock_policy_replenishment_order_id,
        stock_policy_dict,
    )

    app, factory, ids = _scenario(tmp_path)
    with factory() as db:
        existing_lot = db.get(InventoryLot, ids["semi_lot"])
        relation = db.get(ProductBomComponent, ids["relation"])
        material = db.get(Material, ids["material_a"])
        assert existing_lot is not None and relation is not None and material is not None
        existing_lot.quantity_available = 0
        existing_lot.status = "closed"
        relation.spare_sheet_quantity = 2
        material.quote_price = Decimal("2.80")
        material.price_unit = "元/㎡"
        material.purchase_currency = "CNY"
        material.purchase_tax_included = True
        material.purchase_tax_rate = Decimal("0.13")
        db.commit()

    def staging_location(db: Session, **_kwargs) -> WarehouseLocation:
        location = db.get(WarehouseLocation, ids["semi_location"])
        assert location is not None
        return location

    monkeypatch.setattr(
        stock_replenishment_service,
        "automatic_raw_material_staging_location",
        staging_location,
    )

    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "123456"},
        ).status_code == 200
        expanded = client.get(
            f"/api/requisition/stock-policies/{ids['policy']}/replenishment-draft",
            params={"parent_set_quantity": 800},
        )
        assert expanded.status_code == 200, expanded.text
        draft = expanded.json()
        line = deepcopy(draft["items"][0])
        component_plan = deepcopy(draft["composite_parent_plan"])
        assert component_plan["components"][0]["required_piece_quantity"] == 2400
        assert component_plan["components"][0]["yield_per_sheet"] == 4
        assert component_plan["components"][0]["spare_sheet_quantity"] == 2
        assert component_plan["components"][0]["purchase_sheet_quantity"] == 602
        assert line["inventory_candidates"] == []
        line.update(
            {
                "product_id": None,
                "reference_product_id": ids["component"],
                "quantity": 602,
                "purchase_sheet_quantity": 602,
            }
        )
        payload = {
            "source_type": "stock_warning",
            "idempotency_key": "p1-150-purchase-only-source",
            "supplier_name": draft["supplier_name"],
            "customer_id": ids["customer_a"],
            "stock_now": False,
            "composite_parent_plan": component_plan,
            "items": [line],
        }
        created = client.post(
            "/api/requisition/stock-replenishment/orders",
            json=payload,
        )
        assert created.status_code == 201, created.text
        source_order_id = created.json()["id"]
        source_item_id = created.json()["items"][0]["id"]

        with factory() as db:
            policy = db.get(InventoryStockPolicy, ids["policy"])
            assert policy is not None
            pending_summary = stock_policy_dict(db, policy)
            assert pending_summary["replenishment_state"] == "already_ordered"

        received = client.put(
            f"/api/incoming/receive/sr{source_item_id}",
            json={
                "received_quantity": 602,
                "idempotency_key": "p1-150-purchase-only-receipt",
            },
        )
        assert received.status_code == 200, received.text
        received_lot_id = received.json()["received_inventory_lot_id"]
        replayed_receipt = client.put(
            f"/api/incoming/receive/sr{source_item_id}",
            json={
                "received_quantity": 602,
                "idempotency_key": "p1-150-purchase-only-receipt",
            },
        )
        assert replayed_receipt.status_code == 200, replayed_receipt.text
        assert replayed_receipt.json()["received_inventory_lot_id"] == received_lot_id

        duplicate_payload = deepcopy(payload)
        duplicate_payload["idempotency_key"] = "p1-150-purchase-only-duplicate"
        duplicate = client.post(
            "/api/requisition/stock-replenishment/orders",
            json=duplicate_payload,
        )
        assert duplicate.status_code == 409, duplicate.text
        assert "已有未完成补库单" in duplicate.text

    with factory() as db:
        source_order = db.get(StockReplenishmentOrder, source_order_id)
        source_item = db.get(StockReplenishmentOrderItem, source_item_id)
        lot = db.get(InventoryLot, received_lot_id)
        reservations = list(
            db.scalars(
                select(InventoryReservation).where(
                    InventoryReservation.stock_replenishment_bom_component_plan_id
                    .is_not(None)
                )
            )
        )
        assert source_order is not None and source_order.status == "stocked"
        assert source_item is not None and source_item.stocked_quantity == 602
        assert lot is not None
        assert (lot.quantity_available, lot.quantity_reserved, lot.version) == (2, 600, 2)
        assert len(reservations) == 1
        plan_reservation = reservations[0]
        assert plan_reservation.reservation_type == "semi_requisition"
        assert plan_reservation.reserved_stock_quantity == 600
        assert plan_reservation.credited_requirement_quantity == 2400
        assert plan_reservation.status == "active"
        assert (
            active_stock_policy_replenishment_order_id(db, policy_id=ids["policy"])
            == source_order_id
        )
        policy = db.get(InventoryStockPolicy, ids["policy"])
        assert policy is not None
        summary = stock_policy_dict(db, policy)
        assert summary["suggested_new_requisition_finished_quantity"] == 0
        assert summary["suggested_new_requisition_sheet_quantity"] == 0
        assert summary["replenishment_state"] == "inventory_reserved"
        plan_reservation_id = int(plan_reservation.id)

    order_item_id, snapshot_id = _seed_later_composite_order(
        factory,
        ids,
        set_quantity=800,
        order_number="SO-P1-150-PURCHASE-TRANSFER",
    )
    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "123456"},
        ).status_code == 200
        transfer_key = "p1-150-purchase-to-order-transfer"
        transferred = client.post(
            f"/api/warehouse/finished/bom-components/{snapshot_id}/auto-cover",
            json={
                "order_item_id": order_item_id,
                "idempotency_key": transfer_key,
                "component_type": "whole",
            },
        )
        assert transferred.status_code == 200, transferred.text
        replayed = client.post(
            f"/api/warehouse/finished/bom-components/{snapshot_id}/auto-cover",
            json={
                "order_item_id": order_item_id,
                "idempotency_key": transfer_key,
                "component_type": "whole",
            },
        )
        assert replayed.status_code == 200, replayed.text

    with factory() as db:
        lot = db.get(InventoryLot, received_lot_id)
        source_reservation = db.get(InventoryReservation, plan_reservation_id)
        target_reservation = db.scalar(
            select(InventoryReservation).where(
                InventoryReservation.reservation_type == "semi_order",
                InventoryReservation.order_item_id == order_item_id,
                InventoryReservation.sales_order_item_bom_component_id == snapshot_id,
            )
        )
        assert lot is not None and source_reservation is not None
        assert target_reservation is not None
        assert (lot.quantity_available, lot.quantity_reserved, lot.version) == (2, 600, 4)
        assert source_reservation.status == "released"
        assert source_reservation.released_stock_quantity == 600
        assert source_reservation.released_requirement_quantity == 2400
        assert target_reservation.status == "active"
        assert target_reservation.reserved_stock_quantity == 600
        assert target_reservation.credited_requirement_quantity == 2400
        assert (
            active_stock_policy_replenishment_order_id(db, policy_id=ids["policy"])
            is None
        )
        policy = db.get(InventoryStockPolicy, ids["policy"])
        assert policy is not None
        refreshed_summary = stock_policy_dict(db, policy)
        assert refreshed_summary["suggested_new_requisition_finished_quantity"] == 800
        assert refreshed_summary["suggested_new_requisition_sheet_quantity"] == 602


def test_plan_release_idempotency_rejects_same_key_with_different_quantity(
    tmp_path: Path,
) -> None:
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryMovement,
        InventoryReservation,
    )
    from app.services.semi_finished_inventory import (
        release_semi_finished_reservation,
    )
    from app.services.warehouse_inventory import WarehouseInventoryError

    app, factory, ids = _scenario(tmp_path)
    with TestClient(app) as client:
        draft = _login_and_expand(client, ids)
        created = client.post(
            "/api/requisition/stock-replenishment/orders",
            json=_payload(
                draft,
                ids,
                idempotency_key="p1-150-release-idempotency-source",
            ),
        )
        assert created.status_code == 201, created.text

    with factory() as db:
        reservation = db.scalar(select(InventoryReservation))
        assert reservation is not None
        lot = db.get(InventoryLot, reservation.inventory_lot_id)
        assert lot is not None
        first = release_semi_finished_reservation(
            db,
            reservation_id=reservation.id,
            expected_version=lot.version,
            operator_id=ids["admin"],
            release_reason="测试计划预占部分转接",
            idempotency_key="p1-150-release-two-sheets",
            stock_quantity=2,
            allow_stock_replenishment_plan=True,
        )
        db.commit()
        reservation_id = int(reservation.id)
        first_movement_id = int(first.movement.id)

    with factory() as db:
        lot = db.get(InventoryLot, ids["semi_lot"])
        assert lot is not None
        replayed = release_semi_finished_reservation(
            db,
            reservation_id=reservation_id,
            expected_version=lot.version,
            operator_id=ids["admin"],
            release_reason="测试计划预占部分转接",
            idempotency_key="p1-150-release-two-sheets",
            stock_quantity=2,
            allow_stock_replenishment_plan=True,
        )
        assert replayed.movement.id == first_movement_id
        with pytest.raises(WarehouseInventoryError, match="释放数量已变化") as error:
            release_semi_finished_reservation(
                db,
                reservation_id=reservation_id,
                expected_version=lot.version,
                operator_id=ids["admin"],
                release_reason="测试计划预占部分转接",
                idempotency_key="p1-150-release-two-sheets",
                stock_quantity=5,
                allow_stock_replenishment_plan=True,
            )
        assert error.value.status_code == 409
        reservation = db.get(InventoryReservation, reservation_id)
        assert reservation is not None
        assert (lot.quantity_available, lot.quantity_reserved, lot.version) == (2, 48, 3)
        assert reservation.released_stock_quantity == 2
        assert int(
            db.scalar(
                select(func.count(InventoryMovement.id)).where(
                    InventoryMovement.idempotency_key
                    == "p1-150-release-two-sheets"
                )
            )
            or 0
        ) == 1


def test_partial_plan_transfer_blocks_source_replenishment_void(
    tmp_path: Path,
) -> None:
    from app.models.stock_replenishment import (
        InventoryStockPolicy,
        StockReplenishmentOrder,
    )
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation
    from app.services.stock_replenishment import stock_policy_dict

    app, factory, ids = _scenario(tmp_path)
    with factory() as db:
        lot = db.get(InventoryLot, ids["semi_lot"])
        assert lot is not None
        lot.quantity_available = 600
        db.commit()

    with TestClient(app) as client:
        draft = _login_and_expand(client, ids, expected_available_sheets=600)
        candidate = draft["items"][0]["inventory_candidates"][0]
        plan = deepcopy(draft["composite_parent_plan"])
        plan["components"][0].update(
            {
                "requested_offset_piece_quantity": 2400,
                "selected_lots": [
                    {
                        "lot_id": ids["semi_lot"],
                        "expected_version": candidate["version"],
                    }
                ],
                "purchase_sheet_quantity": 0,
            }
        )
        created = client.post(
            "/api/requisition/stock-replenishment/orders",
            json={
                "source_type": "stock_warning",
                "idempotency_key": "p1-150-partial-transfer-source",
                "supplier_name": draft["supplier_name"],
                "customer_id": ids["customer_a"],
                "stock_now": False,
                "composite_parent_plan": plan,
                "items": [],
            },
        )
        assert created.status_code == 201, created.text
        source_order_id = created.json()["id"]

    with factory() as db:
        policy = db.get(InventoryStockPolicy, ids["policy"])
        assert policy is not None
        zero_purchase_summary = stock_policy_dict(db, policy)
        assert zero_purchase_summary["suggested_new_requisition_sheet_quantity"] == 0
        assert zero_purchase_summary["replenishment_state"] == "inventory_reserved"

    order_item_id, snapshot_id = _seed_later_composite_order(
        factory,
        ids,
        set_quantity=332,
        order_number="SO-P1-150-PARTIAL-TRANSFER",
    )
    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "123456"},
        ).status_code == 200
        transferred = client.post(
            f"/api/warehouse/finished/bom-components/{snapshot_id}/auto-cover",
            json={
                "order_item_id": order_item_id,
                "idempotency_key": "p1-150-partial-transfer-target",
                "component_type": "whole",
            },
        )
        assert transferred.status_code == 200, transferred.text
        blocked_void = client.put(
            f"/api/requisition/stock-replenishment/orders/{source_order_id}/void"
        )
        assert blocked_void.status_code == 409, blocked_void.text
        assert "转接或使用" in blocked_void.text

    with factory() as db:
        source_order = db.get(StockReplenishmentOrder, source_order_id)
        lot = db.get(InventoryLot, ids["semi_lot"])
        source_reservation = db.scalar(
            select(InventoryReservation).where(
                InventoryReservation.stock_replenishment_bom_component_plan_id
                .is_not(None)
            )
        )
        target_reservation = db.scalar(
            select(InventoryReservation).where(
                InventoryReservation.reservation_type == "semi_order",
                InventoryReservation.order_item_id == order_item_id,
            )
        )
        assert source_order is not None and source_order.status == "confirmed"
        assert lot is not None
        assert (lot.quantity_available, lot.quantity_reserved, lot.version) == (0, 600, 4)
        assert source_reservation is not None and source_reservation.status == "partial"
        assert source_reservation.released_stock_quantity == 249
        assert source_reservation.released_requirement_quantity == 996
        assert target_reservation is not None and target_reservation.status == "active"
        assert target_reservation.reserved_stock_quantity == 249
        assert target_reservation.credited_requirement_quantity == 996


def test_component_rounding_surplus_stays_free_and_completion_reverts_cleanly(
    tmp_path: Path,
) -> None:
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.product_bom import (
        ProductBomComponent,
        SalesOrderItemBomComponent,
    )
    from app.models.production import ProductionCompletion, ProductionTask
    from app.models.warehouse_inventory import (
        FinishedGoodsInventoryDetail,
        InventoryLot,
        InventoryMovement,
        InventoryReservation,
        OrderItemSemiRequirement,
        SemiFinishedLotAllowedProduct,
        WarehouseLocation,
    )
    from app.services.production_workflow import create_or_refresh_production_task

    app, factory, ids = _scenario(tmp_path)
    with factory() as db:
        parent = db.get(Product, ids["parent"])
        component = db.get(Product, ids["component"])
        relation = db.get(ProductBomComponent, ids["relation"])
        semi_lot = db.get(InventoryLot, ids["semi_lot"])
        assert (
            parent is not None
            and component is not None
            and relation is not None
            and semi_lot is not None
            and semi_lot.semi_finished_detail is not None
        )
        relation.quantity_per_set = Decimal("1")
        semi_lot.quantity_available = 49
        semi_lot.quantity_reserved = 1
        finished_location = WarehouseLocation(
            location_code="FIN-P1-150-ROUNDING",
            location_name="组合组件余片测试位",
            warehouse_type="finished",
            warehouse_floor=3,
            source_version="V11",
            placement_status="placed",
            is_active=True,
        )
        order = Order(
            order_number="SO-P1-150-ROUNDING",
            customer_id=ids["customer_a"],
            order_date=date.today(),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("1"),
        )
        db.add_all([finished_location, order])
        db.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=parent.id,
            quantity=1,
            unit_price=Decimal("1"),
            subtotal=Decimal("1"),
            material_status="pending",
            requisition_status="未报料",
            is_virtual_composite_parent_snapshot=True,
            snapshot_product_code=parent.product_code,
            snapshot_product_name=parent.product_name,
            snapshot_spec="组合父件1套",
            snapshot_splice_mode="single",
            snapshot_pieces_per_box=1,
        )
        db.add(item)
        db.flush()
        snapshot = SalesOrderItemBomComponent(
            sales_order_item_id=item.id,
            product_bom_component_id=relation.id,
            component_product_id=component.id,
            parent_product_version=int(parent.version or 1),
            component_product_version=int(component.version or 1),
            order_set_quantity=1,
            quantity_per_set=Decimal("1"),
            required_piece_quantity=Decimal("1"),
            display_order=1,
            internal_component_code="P1-150-A",
            is_die_cut=False,
            snapshot_mold_tool_id=None,
            mold_max_yield_per_sheet=None,
            spare_sheet_quantity=0,
            display_mode="internal_only",
            is_required=True,
            snapshot_component_product_code=component.product_code,
            snapshot_component_product_name=component.product_name,
            snapshot_component_spec="600×470",
            snapshot_component_material="A/K/B",
            snapshot_component_material_id=component.material_id,
            snapshot_component_supplier_name="匿名纸板供应商",
            snapshot_component_layer_count=3,
            snapshot_component_flute_type="B",
            snapshot_component_box_category="normal",
            snapshot_component_box_style=component.box_style,
            snapshot_component_default_cutting_mode="一开四",
            snapshot_component_splice_mode="single",
            snapshot_component_pieces_per_box=1,
            snapshot_component_report_length_mm=600,
            snapshot_component_report_width_mm=470,
            snapshot_component_crease_type="净料",
        )
        db.add(snapshot)
        db.flush()
        detail = semi_lot.semi_finished_detail
        requirement = OrderItemSemiRequirement(
            order_item_id=item.id,
            sales_order_item_bom_component_id=snapshot.id,
            customer_id=ids["customer_a"],
            component_type="whole",
            board_length_mm=int(detail.board_length_mm),
            board_width_mm=int(detail.board_width_mm),
            material_code_snapshot=str(detail.material_code_snapshot),
            normalized_material_code=str(detail.normalized_material_code),
            flute_type=str(detail.flute_type),
            pieces_per_box=1,
            stock_yield_per_sheet=4,
            required_piece_quantity=1,
            created_by=ids["admin"],
            updated_by=ids["admin"],
        )
        db.add(requirement)
        db.flush()
        db.add_all(
            [
                SemiFinishedLotAllowedProduct(
                    inventory_lot_id=semi_lot.id,
                    product_id=component.id,
                    confirmed_by=ids["admin"],
                    confirmed_at=datetime.now(),
                ),
                InventoryReservation(
                    reservation_number="SRS-P1-150-ROUNDING",
                    inventory_lot_id=semi_lot.id,
                    reservation_type="semi_order",
                    order_id=order.id,
                    order_item_id=item.id,
                    sales_order_item_bom_component_id=snapshot.id,
                    semi_requirement_id=requirement.id,
                    reserved_stock_quantity=1,
                    credited_requirement_quantity=1,
                    yield_factor=4,
                    consumed_stock_quantity=0,
                    released_stock_quantity=0,
                    consumed_requirement_quantity=0,
                    released_requirement_quantity=0,
                    status="active",
                    warning_codes="[]",
                    reserved_by=ids["admin"],
                    reserved_at=datetime.now(),
                    idempotency_key="p1-150-rounding-semi-reserve",
                ),
            ]
        )
        task = create_or_refresh_production_task(db, item.id)
        assert task is not None
        component_task = db.scalar(
            select(ProductionTask).where(
                ProductionTask.sales_order_item_bom_component_id == snapshot.id
            )
        )
        assert component_task is not None
        assert (
            component_task.status,
            component_task.planned_quantity,
            component_task.material_input_quantity,
            component_task.output_factor,
        ) == ("pending", 1, 1, 4)
        db.commit()
        task_id = int(component_task.id)
        task_version = int(component_task.version)
        item_id = int(item.id)
        snapshot_id = int(snapshot.id)
        finished_location_id = int(finished_location.id)

    completion_payload = {
        "idempotency_key": "p1-150-rounding-completion",
        "items": [
            {
                "task_id": task_id,
                "expected_version": task_version,
                "disposition": "stock",
                "material_input_quantity": 1,
                "actual_output_quantity": 4,
                "defective_quantity": 0,
                "location_id": finished_location_id,
            }
        ],
    }
    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "123456"},
        ).status_code == 200
        completed = client.post(
            "/api/production/completion-batches",
            json=completion_payload,
        )
        assert completed.status_code == 200, completed.text
        replayed = client.post(
            "/api/production/completion-batches",
            json=completion_payload,
        )
        assert replayed.status_code == 200, replayed.text
        assert replayed.json()["replayed"] is True
        completion_id = int(completed.json()["items"][0]["id"])

    with factory() as db:
        completion = db.get(ProductionCompletion, completion_id)
        finished_lot = db.scalar(
            select(InventoryLot).where(
                InventoryLot.source_ref_type == "production_completion",
                InventoryLot.source_ref_id == completion_id,
            )
        )
        assert completion is not None and finished_lot is not None
        assert (
            completion.actual_output_quantity,
            completion.order_reserved_quantity,
            completion.surplus_finished_quantity,
        ) == (4, 1, 3)
        assert (
            finished_lot.quantity_available,
            finished_lot.quantity_reserved,
            finished_lot.quantity_consumed,
        ) == (3, 1, 0)
        finished_detail = db.scalar(
            select(FinishedGoodsInventoryDetail).where(
                FinishedGoodsInventoryDetail.inventory_lot_id == finished_lot.id
            )
        )
        assert finished_detail is not None
        assert finished_detail.product_id == ids["component"]
        finished_reservation = db.scalar(
            select(InventoryReservation).where(
                InventoryReservation.inventory_lot_id == finished_lot.id,
                InventoryReservation.reservation_type == "finished_order",
            )
        )
        assert finished_reservation is not None
        assert (
            finished_reservation.reserved_stock_quantity,
            finished_reservation.credited_requirement_quantity,
            finished_reservation.sales_order_item_bom_component_id,
        ) == (1, 1, snapshot_id)
        semi_lot = db.get(InventoryLot, ids["semi_lot"])
        semi_reservation = db.scalar(
            select(InventoryReservation).where(
                InventoryReservation.order_item_id == item_id,
                InventoryReservation.reservation_type == "semi_order",
            )
        )
        assert semi_lot is not None and semi_reservation is not None
        assert (
            semi_lot.quantity_available,
            semi_lot.quantity_reserved,
            semi_lot.quantity_consumed,
            semi_lot.version,
        ) == (49, 0, 1, 2)
        assert (
            semi_reservation.consumed_stock_quantity,
            semi_reservation.consumed_requirement_quantity,
            semi_reservation.status,
        ) == (1, 1, "consumed")
        assert int(
            db.scalar(
                select(func.count(ProductionCompletion.id)).where(
                    ProductionCompletion.task_id == task_id
                )
            )
            or 0
        ) == 1
        assert int(
            db.scalar(
                select(func.count(InventoryMovement.id)).where(
                    InventoryMovement.inventory_lot_id == finished_lot.id,
                    InventoryMovement.movement_type == "reserve",
                )
            )
            or 0
        ) == 1
        finished_lot_id = int(finished_lot.id)
        finished_reservation_id = int(finished_reservation.id)
        semi_reservation_id = int(semi_reservation.id)

    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "123456"},
        ).status_code == 200
        reverted = client.post(
            f"/api/production/completions/{completion_id}/revert",
            json={"reason": "验证非整除组件余片反向闭环"},
        )
        assert reverted.status_code == 200, reverted.text

    with factory() as db:
        completion = db.get(ProductionCompletion, completion_id)
        task = db.get(ProductionTask, task_id)
        finished_lot = db.get(InventoryLot, finished_lot_id)
        finished_reservation = db.get(InventoryReservation, finished_reservation_id)
        semi_lot = db.get(InventoryLot, ids["semi_lot"])
        semi_reservation = db.get(InventoryReservation, semi_reservation_id)
        assert completion is not None and completion.status == "reversed"
        assert task is not None and task.status == "pending"
        assert finished_lot is not None
        assert (
            finished_lot.quantity_available,
            finished_lot.quantity_reserved,
            finished_lot.status,
        ) == (0, 0, "closed")
        assert finished_reservation is not None
        assert (
            finished_reservation.released_stock_quantity,
            finished_reservation.released_requirement_quantity,
            finished_reservation.status,
        ) == (1, 1, "released")
        assert semi_lot is not None and semi_reservation is not None
        assert (
            semi_lot.quantity_available,
            semi_lot.quantity_reserved,
            semi_lot.quantity_consumed,
            semi_lot.version,
        ) == (49, 1, 0, 3)
        assert (
            semi_reservation.consumed_stock_quantity,
            semi_reservation.consumed_requirement_quantity,
            semi_reservation.status,
        ) == (0, 0, "active")
