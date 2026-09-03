from __future__ import annotations

from collections.abc import Generator
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.requisition import (
    _aggregate_composite_physical_group,
    _composite_physical_group_fingerprint,
    _composite_physical_group_storage_keys,
    _current_purchase_purpose_source,
    _expand_composite_requisition_void_scope,
)


def _physical_source(sequence: int, *, pieces: int = 1, spare: int = 0) -> dict:
    return {
        "order_item_id": sequence,
        "bom_snapshot_id": 100 + sequence,
        "component_type": "whole",
        "physical_group_key": "a" * 64,
        "source_fingerprint": f"{sequence:064x}",
        "required_piece_quantity": pieces,
        "inventory_reserved_piece_quantity": 0,
        "net_required_piece_quantity": pieces,
        "spare_sheet_quantity": spare,
        "yield_per_sheet": 4,
    }


def test_pure_group_ceil_happens_once_and_zero_sheet_sources_remain() -> None:
    plan = _aggregate_composite_physical_group(
        [_physical_source(3), _physical_source(1), _physical_source(2)]
    )

    assert plan["net_required_piece_quantity"] == 3
    assert plan["order_purpose_sheet_quantity"] == 1
    assert plan["purchase_sheet_quantity"] == 1
    assert plan["source_purchase_allocations"] == [0, 0, 1]
    assert [row["order_item_id"] for row in plan["sources"]] == [1, 2, 3]
    assert plan["source_count"] == 3


def test_pure_group_adds_each_frozen_spare_after_the_single_ceiling() -> None:
    plan = _aggregate_composite_physical_group(
        [_physical_source(1, pieces=1, spare=1), _physical_source(2, pieces=1, spare=2)],
        purchase_sheet_quantity=5,
    )

    assert plan["net_order_sheet_quantity"] == 1
    assert plan["spare_sheet_quantity"] == 3
    assert plan["order_purpose_sheet_quantity"] == 4
    assert plan["reserve_sheet_quantity"] == 1
    assert plan["purchase_sheet_quantity"] == 5
    assert plan["cutting_remainder_piece_quantity"] == 2
    assert plan["source_order_purpose_allocations"] == [1, 3]
    assert plan["source_purchase_allocations"] == [1, 4]


def test_group_storage_keys_stay_bounded_for_long_request_key() -> None:
    request_key = "r" * 120
    physical_group_key = "a" * 64

    group_key, idempotency_key = _composite_physical_group_storage_keys(
        request_key,
        physical_group_key,
    )

    assert len(group_key) <= 160
    assert len(idempotency_key) <= 160
    assert (group_key, idempotency_key) == (
        *_composite_physical_group_storage_keys(
            request_key,
            physical_group_key,
        ),
    )
    assert _composite_physical_group_storage_keys(
        request_key[:-1] + "x",
        physical_group_key,
    ) != (group_key, idempotency_key)


def test_void_scope_closes_over_shared_and_sibling_physical_groups() -> None:
    order_item_ids, group_ids = _expand_composite_requisition_void_scope(
        seed_order_item_ids=[1],
        group_source_links=[
            (10, 1),
            (10, 2),
            (20, 2),
            (20, 3),
            (30, 99),
        ],
    )

    assert order_item_ids == [1, 2, 3]
    assert group_ids == [10, 20]


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("physical_group_key", "b" * 64, "冻结物理事实不一致"),
        ("yield_per_sheet", 3, "一张出几不一致"),
        ("net_required_piece_quantity", 0, "组件片需求不守恒"),
    ],
)
def test_pure_group_rejects_mixed_or_nonconserving_sources(
    field: str,
    replacement: object,
    message: str,
) -> None:
    sources = [_physical_source(1), _physical_source(2)]
    tampered = deepcopy(sources)
    tampered[1][field] = replacement

    with pytest.raises(ValueError, match=message):
        _aggregate_composite_physical_group(tampered)


@pytest.fixture()
def physical_group_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.requisition import router as requisition_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.models.supplier import Supplier
    from app.models.user import User
    from app.services.supplier_master import normalize_supplier_identity

    engine = create_sqlite_engine(tmp_path / "p1-150b.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="admin",
            password_hash=hash_password("123456"),
            role="admin",
            real_name="P1-150B",
            display_name="P1-150B",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=1,
            customer_code="P1150B",
            name="P1-150B 测试客户",
        )
        supplier = Supplier(
            standard_name="P1-150B 供应商",
            normalized_name=normalize_supplier_identity("P1-150B 供应商"),
            display_name="P1-150B 供应商",
            sort_order=1,
            is_active=True,
            version=1,
        )
        material = Material(
            code="K=A",
            layer_count=3,
            flute_type="B",
            supplier_name=supplier.standard_name,
            is_active=True,
            version=1,
        )
        parent = Product(
            customer_id=1,
            product_code="SET-P1-150B",
            customer_material_code="SET-P1-150B",
            product_name="组合父件",
            box_category="normal",
            is_composite=True,
            unit="套",
        )
        component = Product(
            customer_id=1,
            product_code="PHYSICAL-COMPONENT",
            customer_material_code="PHYSICAL-COMPONENT",
            product_name="同物理组件",
            box_category="normal",
            is_internal_component=True,
            unit="片",
        )
        db.add_all([admin, customer, supplier, material, parent, component])
        db.flush()
        for sequence in range(1, 4):
            order = Order(
                order_number=f"P1-150B-{sequence:03d}",
                customer_id=customer.id,
                order_date=date(2026, 9, 3),
                delivery_date=date(2026, 9, 8),
                status="pending_production",
                payment_status="unpaid",
                total_amount=Decimal("10"),
            )
            db.add(order)
            db.flush()
            item = OrderItem(
                order_id=order.id,
                product_id=parent.id,
                quantity=1,
                unit_price=Decimal("10"),
                subtotal=Decimal("10"),
                material_status="pending",
                requisition_status="未报料",
                is_virtual_composite_parent_snapshot=True,
                snapshot_product_code=parent.product_code,
                snapshot_product_name=parent.product_name,
                snapshot_spec="1 套",
                snapshot_material="虚拟父件",
                snapshot_supplier_name=supplier.standard_name,
                snapshot_report_length_mm=700,
                snapshot_report_width_mm=500,
                snapshot_crease_type="毛片",
                special_process="一开一",
            )
            db.add(item)
            db.flush()
            db.add(
                SalesOrderItemBomComponent(
                    sales_order_item_id=item.id,
                    component_product_id=component.id,
                    parent_product_version=parent.version,
                    component_product_version=component.version,
                    snapshot_schema_version=3,
                    order_set_quantity=1,
                    quantity_per_set=Decimal("1"),
                    required_piece_quantity=Decimal("1"),
                    display_order=1,
                    internal_component_code="PHYSICAL-COMPONENT",
                    is_die_cut=False,
                    spare_sheet_quantity=0,
                    display_mode="internal_only",
                    is_required=True,
                    snapshot_component_product_code=component.product_code,
                    snapshot_component_product_name=component.product_name,
                    snapshot_component_spec="800×400",
                    snapshot_component_material=material.code,
                    snapshot_component_material_id=material.id,
                    snapshot_component_supplier_name=supplier.standard_name,
                    snapshot_component_layer_count=material.layer_count,
                    snapshot_component_flute_type=material.flute_type,
                    snapshot_component_box_category="normal",
                    snapshot_component_box_style="平卡",
                    snapshot_component_default_cutting_mode="一开四",
                    snapshot_component_report_length_mm=800,
                    snapshot_component_report_width_mm=400,
                    snapshot_component_crease_type="毛片",
                    snapshot_component_splice_mode="single",
                    snapshot_component_pieces_per_box=1,
                )
            )
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(requisition_router, prefix="/api/requisition")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, factory
    finally:
        engine.dispose()


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "123456"},
    )
    assert response.status_code == 200, response.text


def _group_payload_from_pending(rows: list[dict]) -> dict:
    components = [row["component_requirements"][0] for row in rows]
    group_key = components[0]["physical_group_key"]
    assert {row["physical_group_key"] for row in components} == {group_key}
    group_fingerprint = _composite_physical_group_fingerprint(
        group_key,
        [row["physical_source_fingerprint"] for row in components],
    )
    ordered = sorted(
        zip(rows, components, strict=True),
        key=lambda row: (row[0]["item_id"], row[1]["snapshot_id"]),
    )
    items = []
    for index, (pending, component) in enumerate(ordered):
        allocated = 1 if index == len(ordered) - 1 else 0
        items.append(
            {
                "order_item_id": pending["item_id"],
                "bom_snapshot_id": component["snapshot_id"],
                "component_type": component["component_type"],
                "physical_group_key": group_key,
                "group_fingerprint": group_fingerprint,
                "source_fingerprint": component[
                    "physical_source_fingerprint"
                ],
                "group_purchase_sheet_qty": 1,
                "group_order_purpose_sheet_qty": 1,
                "group_stock_purpose_sheet_qty": 0,
                "requisition_qty": allocated,
                "purchase_total_sheet_qty": allocated,
                "order_purpose_sheet_qty": allocated,
                "stock_purpose_sheet_qty": 0,
                "purpose_plan_version": 1,
                "purpose_plan_fingerprint": group_fingerprint,
                "cardboard_len": component["report_length_mm"],
                "cardboard_width": component["report_width_mm"],
                "special_process": component["cutting_mode"],
            }
        )
    return {
        "request_key": "p1-150b-three-sources",
        "supplier_name": "P1-150B 供应商",
        "items": items,
    }


def test_api_groups_three_orders_into_one_sheet_and_keeps_zero_sources(
    physical_group_app,
) -> None:
    from app.models.composite_purchase_group import (
        CompositePhysicalPurchaseGroup,
        CompositePhysicalPurchaseGroupSource,
    )
    from app.models.order import OrderItem
    from app.models.product_bom import RequisitionItemBomSource
    from app.models.requisition import RequisitionItem
    from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot

    app, factory = physical_group_app
    with TestClient(app) as client:
        _login(client)
        pending = client.get("/api/requisition/pending")
        assert pending.status_code == 200, pending.text
        payload = _group_payload_from_pending(pending.json()["items"])
        created = client.post("/api/requisition/batches", json=payload)
        assert created.status_code == 201, created.text
        printed = client.get(
            f"/api/requisition/batches/{created.json()['id']}/print"
        )

    assert printed.status_code == 200, printed.text
    assert printed.json()["total_quantity"] == 1
    assert len(printed.json()["items"]) == 1
    assert printed.json()["items"][0]["quantity"] == 1
    assert printed.json()["items"][0]["source_count"] == 3
    assert printed.json()["items"][0]["physical_group_key"].startswith("cg")
    with factory() as db:
        group = db.scalar(select(CompositePhysicalPurchaseGroup))
        sources = db.scalars(
            select(CompositePhysicalPurchaseGroupSource).order_by(
                CompositePhysicalPurchaseGroupSource.source_sequence
            )
        ).all()
        requisition_items = db.scalars(
            select(RequisitionItem).order_by(RequisitionItem.id)
        ).all()
        order_items = db.scalars(select(OrderItem).order_by(OrderItem.id)).all()
        assert group is not None
        assert group.purchase_sheet_quantity == 1
        assert group.order_purpose_sheet_quantity == 1
        assert group.net_required_piece_quantity == 3
        assert group.material_code_snapshot == "K=A"
        assert group.physical_snapshot_hash == payload["items"][0][
            "physical_group_key"
        ]
        assert len(sources) == 3
        assert [row.allocated_order_purpose_sheet_quantity for row in sources] == [0, 0, 1]
        assert [row.requisition_qty for row in requisition_items] == [0, 0, 1]
        assert all(row.requisition_status == "已报料" for row in order_items), [
            row.requisition_status for row in order_items
        ]
        assert db.scalar(select(func.count(RequisitionItemBomSource.id))) == 3
        purpose_rows = db.scalars(
            select(PurchasePurposeSourceSnapshot).order_by(
                PurchasePurposeSourceSnapshot.id
            )
        ).all()
        assert len(purpose_rows) == 3
        assert [row.pieces_per_finished_snapshot for row in purpose_rows] == [1, 1, 1]
        assert {row.group_authoritative_order_sheet_qty_snapshot for row in purpose_rows} == {1}


def test_api_missing_group_fields_rejects_every_new_bom_source_without_writes(
    physical_group_app,
) -> None:
    from app.models.composite_purchase_group import CompositePhysicalPurchaseGroup
    from app.models.requisition import Requisition, RequisitionItem

    app, factory = physical_group_app
    with TestClient(app) as client:
        _login(client)
        pending = client.get("/api/requisition/pending").json()["items"]
        component = pending[0]["component_requirements"][0]
        rejected = client.post(
            "/api/requisition/batches",
            json={
                "supplier_name": "P1-150B 供应商",
                "items": [
                    {
                        "order_item_id": pending[0]["item_id"],
                        "bom_snapshot_id": component["snapshot_id"],
                        "component_type": component["component_type"],
                        "requisition_qty": 1,
                        "cardboard_len": component["report_length_mm"],
                        "cardboard_width": component["report_width_mm"],
                        "special_process": component["cutting_mode"],
                    }
                ],
            },
        )

    assert rejected.status_code == 409, rejected.text
    with factory() as db:
        assert db.scalar(select(func.count(Requisition.id))) == 0
        assert db.scalar(select(func.count(RequisitionItem.id))) == 0
        assert db.scalar(select(func.count(CompositePhysicalPurchaseGroup.id))) == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("physical_group_key", "b" * 64),
        ("group_fingerprint", "b" * 64),
        ("source_fingerprint", "b" * 64),
        ("group_purchase_sheet_qty", 2),
        ("requisition_qty", 1),
        ("purpose_plan_fingerprint", "b" * 64),
    ],
)
def test_api_rejects_tampered_group_or_source_facts_atomically(
    physical_group_app,
    field: str,
    value: object,
) -> None:
    from app.models.composite_purchase_group import CompositePhysicalPurchaseGroup
    from app.models.requisition import Requisition, RequisitionItem

    app, factory = physical_group_app
    with TestClient(app) as client:
        _login(client)
        pending = client.get("/api/requisition/pending").json()["items"]
        payload = _group_payload_from_pending(pending)
        payload["items"][0][field] = value
        rejected = client.post("/api/requisition/batches", json=payload)

    assert rejected.status_code == 409, rejected.text
    with factory() as db:
        assert db.scalar(select(func.count(Requisition.id))) == 0
        assert db.scalar(select(func.count(RequisitionItem.id))) == 0
        assert db.scalar(select(func.count(CompositePhysicalPurchaseGroup.id))) == 0


def test_api_rejects_stale_material_version_after_preview_without_writes(
    physical_group_app,
) -> None:
    from app.models.composite_purchase_group import CompositePhysicalPurchaseGroup
    from app.models.material import Material
    from app.models.requisition import Requisition

    app, factory = physical_group_app
    with TestClient(app) as client:
        _login(client)
        pending = client.get("/api/requisition/pending").json()["items"]
        payload = _group_payload_from_pending(pending)
        with factory() as db:
            material = db.scalar(select(Material))
            material.version += 1
            db.commit()
        rejected = client.post("/api/requisition/batches", json=payload)

    assert rejected.status_code == 409, rejected.text
    with factory() as db:
        assert db.scalar(select(func.count(Requisition.id))) == 0
        assert db.scalar(select(func.count(CompositePhysicalPurchaseGroup.id))) == 0


def test_api_rejects_stale_bom_demand_after_preview_without_writes(
    physical_group_app,
) -> None:
    from app.models.composite_purchase_group import CompositePhysicalPurchaseGroup
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.models.requisition import Requisition

    app, factory = physical_group_app
    with TestClient(app) as client:
        _login(client)
        pending = client.get("/api/requisition/pending").json()["items"]
        payload = _group_payload_from_pending(pending)
        with factory() as db:
            changed = db.get(
                SalesOrderItemBomComponent,
                payload["items"][0]["bom_snapshot_id"],
            )
            changed.required_piece_quantity = Decimal("2")
            changed.quantity_per_set = Decimal("2")
            db.commit()
        rejected = client.post("/api/requisition/batches", json=payload)

    assert rejected.status_code == 409, rejected.text
    with factory() as db:
        assert db.scalar(select(func.count(Requisition.id))) == 0
        assert db.scalar(select(func.count(CompositePhysicalPurchaseGroup.id))) == 0


def test_preview_never_groups_same_named_component_when_directed_size_differs(
    physical_group_app,
) -> None:
    from app.models.product_bom import SalesOrderItemBomComponent

    app, factory = physical_group_app
    with factory() as db:
        changed = db.scalar(
            select(SalesOrderItemBomComponent).where(
                SalesOrderItemBomComponent.sales_order_item_id == 3
            )
        )
        changed.snapshot_component_report_length_mm = 801
        db.commit()
    with TestClient(app) as client:
        _login(client)
        rows = client.get("/api/requisition/pending").json()["items"]

    keys = [row["component_requirements"][0]["physical_group_key"] for row in rows]
    assert len(set(keys)) == 2


def test_voiding_one_parent_expands_to_whole_physical_group_and_restores_zero_sources(
    physical_group_app,
) -> None:
    from app.models.composite_purchase_group import CompositePhysicalPurchaseGroup
    from app.models.order import OrderItem
    from app.models.requisition import RequisitionItem

    app, factory = physical_group_app
    with TestClient(app) as client:
        _login(client)
        pending = client.get("/api/requisition/pending").json()["items"]
        created = client.post(
            "/api/requisition/batches",
            json=_group_payload_from_pending(pending),
        )
        assert created.status_code == 201, created.text
        legacy_cancel = client.put(
            f"/api/requisition/items/{pending[0]['item_id']}/cancel",
            json={"reason": "must not cancel one zero-sheet source"},
        )
        assert legacy_cancel.status_code == 409, legacy_cancel.text
        voided = client.put(
            f"/api/requisition/batches/{created.json()['id']}/void",
            params={"order_item_id": pending[0]["item_id"]},
            json={"reason": "P1-150B group atomic void"},
        )

    assert voided.status_code == 200, voided.text
    assert voided.json()["voided_item_count"] == 3
    with factory() as db:
        group = db.scalar(select(CompositePhysicalPurchaseGroup))
        assert group is not None
        assert group.status == "voided"
        assert group.version == 2
        assert {
            row.status for row in db.scalars(select(RequisitionItem)).all()
        } == {"已取消"}
        assert {
            row.requisition_status for row in db.scalars(select(OrderItem)).all()
        } == {"未报料"}


def test_cg_price_source_uses_first_positive_allocation_as_stable_anchor(
    physical_group_app,
) -> None:
    from fastapi import HTTPException

    from app.models.composite_purchase_group import (
        CompositePhysicalPurchaseGroup,
        CompositePhysicalPurchaseGroupSource,
    )
    from app.models.user import User

    app, factory = physical_group_app
    with TestClient(app) as client:
        _login(client)
        pending = client.get("/api/requisition/pending").json()["items"]
        created = client.post(
            "/api/requisition/batches",
            json=_group_payload_from_pending(pending),
        )
        assert created.status_code == 201, created.text

    with factory() as db:
        group = db.scalar(select(CompositePhysicalPurchaseGroup))
        user = db.scalar(select(User).where(User.username == "admin"))
        sources = db.scalars(
            select(CompositePhysicalPurchaseGroupSource).order_by(
                CompositePhysicalPurchaseGroupSource.source_sequence
            )
        ).all()
        anchor = sources[-1]
        purpose, source = _current_purchase_purpose_source(
            db,
            source_key=f"cg{group.id}",
            snapshot_id=anchor.purchase_purpose_source_snapshot_id,
            user=user,
        )
        assert purpose.id == anchor.purchase_purpose_source_snapshot_id
        assert source.id == anchor.requisition_item_id
        with pytest.raises(HTTPException) as stale:
            _current_purchase_purpose_source(
                db,
                source_key=f"cg{group.id}",
                snapshot_id=sources[0].purchase_purpose_source_snapshot_id,
                user=user,
            )
        assert stale.value.status_code == 409
