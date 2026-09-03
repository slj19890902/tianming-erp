from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def a3_surround_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.incoming import router as incoming_router
    from app.api.requisition import router as requisition_router
    from app.api.warehouse import router as warehouse_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import (
        UserCustomerScope,
        UserPermissionOverride,
    )
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.models.supplier import Supplier
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "p1-13c.sqlite3")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    with factory() as db:
        user = User(
            username="admin",
            password_hash=hash_password("123456"),
            role="admin",
            real_name="P1-13C",
            display_name="P1-13C",
            must_change_password=False,
        )
        no_permission_user = User(
            username="p1-13c-no-permission",
            password_hash=hash_password("123456"),
            role="sales",
            real_name="P1-13C 无报料权限",
            display_name="P1-13C 无报料权限",
            must_change_password=False,
            customer_access_mode="all",
        )
        scoped_user = User(
            username="p1-13c-scoped",
            password_hash=hash_password("123456"),
            role="sales",
            real_name="P1-13C 客户范围",
            display_name="P1-13C 客户范围",
            must_change_password=False,
            customer_access_mode="selected",
        )
        customer = Customer(
            customer_number=1,
            customer_code="P113C",
            name="P1-13C 匿名客户",
        )
        other_customer = Customer(
            customer_number=2,
            customer_code="P113C-OTHER",
            name="P1-13C 其他匿名客户",
        )
        supplier = Supplier(
            standard_name="匿名供应商",
            normalized_name="匿名供应商",
            display_name="匿名供应商",
            sort_order=10,
            is_active=True,
            version=1,
        )
        material = Material(
            code="A=A",
            layer_count=5,
            flute_type="AB",
            supplier_name=supplier.standard_name,
            is_active=True,
            version=1,
        )
        db.add_all(
            [
                user,
                no_permission_user,
                scoped_user,
                customer,
                other_customer,
                supplier,
                material,
            ]
        )
        db.flush()
        db.add_all(
            [
                UserPermissionOverride(
                    user_id=scoped_user.id,
                    permission_code="requisition.execute",
                    is_allowed=True,
                    granted_by=user.id,
                ),
                UserCustomerScope(
                    user_id=scoped_user.id,
                    customer_id=other_customer.id,
                    assigned_by=user.id,
                ),
            ]
        )
        parent = Product(
            customer_id=customer.id,
            product_code="SET-A3-SURROUND",
            customer_material_code="SET-A3-SURROUND",
            product_name="天地盖围板套件",
            box_category="normal",
            is_composite=True,
            unit="套",
        )
        a3 = Product(
            customer_id=customer.id,
            product_code="A3-COMPONENT",
            customer_material_code="A3-COMPONENT",
            product_name="A3 天地盖",
            box_category="normal",
            box_style="A3 天地盖",
            is_internal_component=True,
            unit="套",
        )
        surround = Product(
            customer_id=customer.id,
            product_code="SURROUND-COMPONENT",
            customer_material_code="SURROUND-COMPONENT",
            product_name="围板",
            box_category="normal",
            box_style="围板",
            is_internal_component=True,
            unit="套",
        )
        db.add_all([parent, a3, surround])
        db.flush()
        order = Order(
            order_number="P1-13C-001",
            customer_id=customer.id,
            order_date=date(2026, 7, 29),
            delivery_date=date(2026, 8, 1),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("100"),
        )
        db.add(order)
        db.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=parent.id,
            quantity=10,
            unit_price=Decimal("10"),
            subtotal=Decimal("100"),
            material_status="pending",
            requisition_status="未报料",
            snapshot_product_code=parent.product_code,
            snapshot_product_name=parent.product_name,
            snapshot_spec="10 套",
            snapshot_material="A=A",
            snapshot_supplier_name="匿名供应商",
            snapshot_report_length_mm=999,
            snapshot_report_width_mm=999,
            snapshot_crease_type="净料",
            special_process="一开一",
        )
        db.add(item)
        db.flush()
        db.add_all(
            [
                SalesOrderItemBomComponent(
                    sales_order_item_id=item.id,
                    component_product_id=a3.id,
                    parent_product_version=parent.version,
                    component_product_version=a3.version,
                    snapshot_schema_version=3,
                    order_set_quantity=10,
                    quantity_per_set=Decimal("1"),
                    required_piece_quantity=Decimal("10"),
                    display_order=1,
                    internal_component_code="SET-A3",
                    is_die_cut=False,
                    spare_sheet_quantity=0,
                    display_mode="internal_only",
                    is_required=True,
                    snapshot_component_product_code=a3.product_code,
                    snapshot_component_product_name=a3.product_name,
                    snapshot_component_spec="A3",
                    snapshot_component_material="A=A",
                    snapshot_component_material_id=material.id,
                    snapshot_component_supplier_name="匿名供应商",
                    snapshot_component_layer_count=5,
                    snapshot_component_flute_type="AB",
                    snapshot_component_box_category="normal",
                    snapshot_component_box_style="A3 天地盖",
                    snapshot_component_default_cutting_mode="一开一",
                    snapshot_component_report_length_mm=610,
                    snapshot_component_report_width_mm=410,
                    snapshot_component_crease_type="净料",
                    snapshot_component_base_report_length_mm=590,
                    snapshot_component_base_report_width_mm=390,
                    snapshot_component_base_crease_type="净料",
                    snapshot_component_splice_mode="single",
                    snapshot_component_pieces_per_box=1,
                ),
                SalesOrderItemBomComponent(
                    sales_order_item_id=item.id,
                    component_product_id=surround.id,
                    parent_product_version=parent.version,
                    component_product_version=surround.version,
                    snapshot_schema_version=3,
                    order_set_quantity=10,
                    quantity_per_set=Decimal("1"),
                    required_piece_quantity=Decimal("10"),
                    display_order=2,
                    internal_component_code="SET-SURROUND",
                    is_die_cut=False,
                    spare_sheet_quantity=0,
                    display_mode="internal_only",
                    is_required=True,
                    snapshot_component_product_code=surround.product_code,
                    snapshot_component_product_name=surround.product_name,
                    snapshot_component_spec="围板双拼",
                    snapshot_component_material="A=A",
                    snapshot_component_material_id=material.id,
                    snapshot_component_supplier_name="匿名供应商",
                    snapshot_component_layer_count=5,
                    snapshot_component_flute_type="AB",
                    snapshot_component_box_category="normal",
                    snapshot_component_box_style="围板",
                    snapshot_component_default_cutting_mode="一开一",
                    snapshot_component_report_length_mm=800,
                    snapshot_component_report_width_mm=300,
                    snapshot_component_crease_type="毛片",
                    snapshot_component_report_notes="围板按毛片报料",
                    snapshot_component_splice_mode="double",
                    snapshot_component_pieces_per_box=2,
                ),
            ]
        )
        db.commit()

    from tests.test_p1_81_receipt_purpose_flow import (
        _seed_material_and_staging,
        _use_p181_published_map_identity,
    )

    _use_p181_published_map_identity(monkeypatch)
    _seed_material_and_staging(factory)
    with factory() as db:
        material = db.scalar(select(Material).where(Material.code == "A=A"))
        item = db.get(OrderItem, 1)
        parent = db.get(Product, item.product_id)
        assert material is not None and item is not None and parent is not None
        item.material_id = material.id
        item.snapshot_material = material.code
        item.snapshot_supplier_name = material.supplier_name
        parent.material_id = material.id
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(requisition_router, prefix="/api/requisition")
    app.include_router(incoming_router, prefix="/api/incoming")
    app.include_router(warehouse_router, prefix="/api/warehouse")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, factory
    finally:
        engine.dispose()


def _login(
    client: TestClient,
    username: str = "admin",
    password: str = "123456",
) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text


def _source_payload(source: dict) -> dict:
    from app.api.requisition import _composite_physical_group_fingerprint

    group_key = str(source["physical_group_key"])
    source_fingerprint = str(source["physical_source_fingerprint"])
    group_fingerprint = _composite_physical_group_fingerprint(
        group_key,
        [source_fingerprint],
    )
    quantity = int(source["requisition_qty"])
    return {
        "order_item_id": int(source.get("order_item_id") or 1),
        "bom_snapshot_id": int(source["snapshot_id"]),
        "component_type": source["component_type"],
        "physical_group_key": group_key,
        "group_fingerprint": group_fingerprint,
        "source_fingerprint": source_fingerprint,
        "group_purchase_sheet_qty": quantity,
        "group_order_purpose_sheet_qty": quantity,
        "group_stock_purpose_sheet_qty": 0,
        "requisition_qty": quantity,
        "purchase_total_sheet_qty": quantity,
        "order_purpose_sheet_qty": quantity,
        "stock_purpose_sheet_qty": 0,
        "purpose_plan_version": 1,
        "purpose_plan_fingerprint": group_fingerprint,
        "cardboard_len": source["report_length_mm"],
        "cardboard_width": source["report_width_mm"],
        "special_process": source["cutting_mode"],
    }


def _pending_sources(client: TestClient) -> dict[tuple[int, str], dict]:
    response = client.get("/api/requisition/pending")
    assert response.status_code == 200, response.text
    sources = response.json()["items"][0]["component_requirements"]
    return {
        (int(source["snapshot_id"]), str(source["component_type"])): source
        for source in sources
        if source["can_requisition"]
    }


def _source_payloads(
    client: TestClient,
    *source_keys: tuple[int, str],
) -> list[dict]:
    pending = _pending_sources(client)
    return [_source_payload(pending[source_key]) for source_key in source_keys]


def _incoming_groups(client: TestClient) -> dict[str, dict]:
    response = client.get("/api/incoming/pending")
    assert response.status_code == 200, response.text
    groups: dict[str, dict] = {}
    for row in response.json()["items"]:
        if not str(row["item_id"]).startswith("cg"):
            continue
        source_types = {
            str(source["component_type"]) for source in row["source_items"]
        }
        assert len(source_types) == 1
        groups[next(iter(source_types))] = row
    return groups


def _receive_group(
    client: TestClient,
    factory,
    group_row: dict,
    *,
    quantity: int,
    idempotency_key: str,
    receipt_fact: dict | None = None,
):
    from tests.test_p1_150b_composite_physical_group_receipts import (
        _freeze_direct_group_receipt_fact,
        _group_receive_payload,
    )

    if receipt_fact is None:
        receipt_fact = _freeze_direct_group_receipt_fact(
            client,
            factory,
            group_row,
            idempotency_key=f"{idempotency_key}-price",
        )
    refreshed = next(
        row
        for row in client.get("/api/incoming/pending").json()["items"]
        if row["item_id"] == group_row["item_id"]
    )
    response = client.put(
        f"/api/incoming/receive/{refreshed['item_id']}",
        json=_group_receive_payload(
            refreshed,
            receipt_fact,
            quantity=quantity,
            idempotency_key=idempotency_key,
        ),
    )
    return response, receipt_fact


def test_pending_expands_exactly_cover_base_and_double_surround(
    a3_surround_app,
) -> None:
    app, _ = a3_surround_app
    with TestClient(app) as client:
        _login(client)
        response = client.get("/api/requisition/pending")

    assert response.status_code == 200, response.text
    row = response.json()["items"][0]
    assert row["suppress_parent_requisition"] is True
    assert row["parent_requirement"]["can_requisition"] is False
    sources = row["bom_requisition_sources"]
    assert [source["source_key"] for source in sources] == [
        "component:1:cover",
        "component:1:base",
        "component:2:whole",
    ]
    assert [source["component_type"] for source in sources] == [
        "cover",
        "base",
        "whole",
    ]
    assert [source["required_piece_quantity"] for source in sources] == [
        10,
        10,
        20,
    ]
    assert [source["requisition_qty"] for source in sources] == [10, 10, 20]
    assert [
        (source["report_length_mm"], source["report_width_mm"])
        for source in sources
    ] == [(610, 410), (590, 390), (800, 300)]


def test_bom_component_print_and_reported_list_use_component_crease_snapshot(
    a3_surround_app,
) -> None:
    app, _ = a3_surround_app
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/requisition/batches",
            json={
                "supplier_name": "匿名供应商",
                "items": _source_payloads(client, (2, "whole")),
            },
        )
        assert created.status_code == 201, created.text
        batch_id = created.json()["id"]
        printed = client.get(f"/api/requisition/batches/{batch_id}/print")
        reported = client.get("/api/requisition/reported-documents")

    assert printed.status_code == 200, printed.text
    print_line = printed.json()["items"][0]
    assert print_line["product_code"] == "SURROUND-COMPONENT"
    assert print_line["crease_display"] == "毛"
    assert print_line["flute_type"] == "AB"
    assert "围板按毛片报料" in print_line["report_remark"]

    assert reported.status_code == 200, reported.text
    document = next(
        row
        for row in reported.json()["items"]
        if row["id"] == batch_id
        and row["source_type"] == "composite_bom_requisition"
    )
    line = document["line_items"][0]
    assert line["component_type"] == "whole"
    assert line["crease_display"] == "毛片"
    assert line["flute_type"] == "AB"


def test_bom_component_group_freezes_component_physical_snapshot(
    a3_surround_app,
) -> None:
    from app.models.composite_purchase_group import CompositePhysicalPurchaseGroup
    from app.models.product import Product

    app, factory = a3_surround_app
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/requisition/batches",
            json={
                "supplier_name": "匿名供应商",
                "items": _source_payloads(client, (2, "whole")),
            },
        )
    assert created.status_code == 201, created.text

    with factory() as db:
        group = db.scalar(select(CompositePhysicalPurchaseGroup))
        surround_id = db.scalar(
            select(Product.id).where(Product.product_code == "SURROUND-COMPONENT")
        )
        assert group is not None
        assert group.component_product_id == surround_id
        assert group.material_code_snapshot == "A=A"
        assert group.layer_count_snapshot == 5
        assert group.flute_type_snapshot == "AB"
        assert (group.report_length_mm, group.report_width_mm) == (800, 300)
        assert group.crease_type_snapshot == "毛片"
        assert group.sheet_type_snapshot == "raw_board"
        assert group.source_count == 1


@pytest.mark.parametrize(
    ("splice_mode", "pieces_per_box", "expected_physical_quantity"),
    [
        ("single", 1, 10),
        ("double", 2, 20),
    ],
)
def test_surround_physical_quantity_uses_frozen_single_or_double_mode(
    a3_surround_app,
    splice_mode: str,
    pieces_per_box: int,
    expected_physical_quantity: int,
) -> None:
    from app.models.product_bom import SalesOrderItemBomComponent

    app, factory = a3_surround_app
    with factory() as db:
        surround = db.scalar(
            select(SalesOrderItemBomComponent).where(
                SalesOrderItemBomComponent.snapshot_component_box_style
                == "围板"
            )
        )
        assert surround is not None
        surround.snapshot_component_splice_mode = splice_mode
        surround.snapshot_component_pieces_per_box = pieces_per_box
        db.commit()

    with TestClient(app) as client:
        _login(client)
        response = client.get("/api/requisition/pending")

    assert response.status_code == 200, response.text
    sources = response.json()["items"][0]["bom_requisition_sources"]
    surround_source = next(
        source
        for source in sources
        if source["component_type"] == "whole"
    )
    assert surround_source["physical_pieces_per_component"] == pieces_per_box
    assert (
        surround_source["required_piece_quantity"]
        == expected_physical_quantity
    )
    assert surround_source["requisition_qty"] == expected_physical_quantity


def test_report_receive_and_replay_keep_three_physical_sources_independent(
    a3_surround_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product_bom import RequisitionItemBomSource
    from app.models.requisition import RequisitionItem

    app, factory = a3_surround_app
    with TestClient(app) as client:
        _login(client)
        cover_payload = _source_payloads(client, (1, "cover"))
        cover = client.post(
            "/api/requisition/batches",
            json={
                "supplier_name": "匿名供应商",
                "items": cover_payload,
            },
        )
        pending_after_cover = client.get("/api/requisition/pending")
        replay = client.post(
            "/api/requisition/batches",
            json={
                "supplier_name": "匿名供应商",
                "items": cover_payload,
            },
        )
        remaining = client.post(
            "/api/requisition/batches",
            json={
                "supplier_name": "匿名供应商",
                "items": _source_payloads(
                    client,
                    (1, "base"),
                    (2, "whole"),
                ),
            },
        )
        incoming = client.get("/api/incoming/pending")
        reported = client.get("/api/requisition/reported-documents")

    assert cover.status_code == 201, cover.text
    assert replay.status_code == 409, replay.text
    pending_sources = pending_after_cover.json()["items"][0][
        "component_requirements"
    ]
    assert {
        (source["snapshot_id"], source["component_type"])
        for source in pending_sources
        if source["can_requisition"]
    } == {(1, "base"), (2, "whole")}
    assert remaining.status_code == 201, remaining.text
    incoming_ids = {
        row["item_id"] for row in incoming.json()["items"]
    }
    assert len(incoming_ids) == 3
    assert reported.status_code == 200, reported.text
    two_line_document = next(
        row
        for row in reported.json()["items"]
        if row["source_type"] == "composite_bom_requisition"
        and row["item_count"] == 2
    )
    assert [line["component_type"] for line in two_line_document["line_items"]] == [
        "base",
        "whole",
    ]
    assert all(line["can_void"] for line in two_line_document["line_items"])

    with factory() as db:
        req_items = db.scalars(
            select(RequisitionItem).order_by(RequisitionItem.id)
        ).all()
        sources = db.scalars(
            select(RequisitionItemBomSource).order_by(
                RequisitionItemBomSource.id
            )
        ).all()
    assert len(req_items) == 3
    assert [source.component_type for source in sources] == [
        "cover",
        "base",
        "whole",
    ]
    assert [int(source.required_piece_quantity) for source in sources] == [
        10,
        10,
        20,
    ]
    assert [int(source.quantity_per_set) for source in sources] == [1, 1, 2]
    assert [source.demand_basis for source in sources] == [
        "order_sets",
        "order_sets",
        "order_sets",
    ]
    assert [int(row.required_piece_qty) for row in req_items] == [10, 10, 20]
    with factory() as db:
        # Stable source identity must not depend on localized product suffixes.
        db.get(RequisitionItem, 1).product_name_snapshot = "A3 物理料甲"
        db.get(RequisitionItem, 2).product_name_snapshot = "A3 物理料乙"
        db.commit()

    by_component = {
        source.component_type: source.requisition_item_id for source in sources
    }
    with TestClient(app) as client:
        _login(client)
        stable_identity_groups = _incoming_groups(client)
        cancelled_base = client.put(
            f"/api/requisition/batch-items/{by_component['base']}/void",
            json={"reason": "P1-13C 单独撤销底片"},
        )
        cancel_replay = client.put(
            f"/api/requisition/batch-items/{by_component['base']}/void",
            json={"reason": "P1-13C 幂等重放"},
        )
        pending_after_cancel = _incoming_groups(client)
        received_cover, _ = _receive_group(
            client,
            factory,
            stable_identity_groups["cover"],
            quantity=int(stable_identity_groups["cover"]["planned_quantity"]),
            idempotency_key="p1-13c-cover-receipt",
        )
        pending_after_receive_cover = _incoming_groups(client)
        received_surround, _ = _receive_group(
            client,
            factory,
            pending_after_receive_cover["whole"],
            quantity=int(pending_after_receive_cover["whole"]["planned_quantity"]),
            idempotency_key="p1-13c-surround-receipt",
        )
        void_received_cover = client.put(
            f"/api/requisition/batch-items/{by_component['cover']}/void",
            json={"reason": "已收货行应拒绝"},
        )

    assert set(stable_identity_groups) == {"cover", "base", "whole"}
    assert all(
        str(row["item_id"]).startswith("cg")
        for row in stable_identity_groups.values()
    )
    assert cancelled_base.status_code == 409, cancelled_base.text
    assert cancel_replay.status_code == 409, cancel_replay.text
    assert set(pending_after_cancel) == {"cover", "base", "whole"}
    assert received_cover.status_code == 200, received_cover.text
    assert set(pending_after_receive_cover) == {"base", "whole"}
    assert received_surround.status_code == 200, received_surround.text
    assert void_received_cover.status_code == 409, void_received_cover.text
    with factory() as db:
        item = db.get(OrderItem, 1)
        assert item is not None
        assert item.material_status == "pending"
        assert item.requisition_status == "已报料"


@pytest.mark.parametrize(
    ("component_type", "short_quantity"),
    [
        ("cover", 5),
        ("base", 5),
        ("whole", 10),
    ],
)
def test_each_physical_source_can_short_receive_revert_and_receive_again(
    a3_surround_app,
    component_type: str,
    short_quantity: int,
) -> None:
    from app.models.product_bom import RequisitionItemBomSource

    app, factory = a3_surround_app
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/requisition/batches",
            json={
                "supplier_name": "匿名供应商",
                "items": _source_payloads(
                    client,
                    (1, "cover"),
                    (1, "base"),
                    (2, "whole"),
                ),
            },
        )
    assert created.status_code == 201, created.text

    with factory() as db:
        source = db.scalar(
            select(RequisitionItemBomSource).where(
                RequisitionItemBomSource.component_type == component_type
            )
        )
        assert source is not None
        requisition_item_id = source.requisition_item_id

    with TestClient(app) as client:
        _login(client)
        group_row = _incoming_groups(client)[component_type]
        short_received, receipt_fact = _receive_group(
            client,
            factory,
            group_row,
            quantity=short_quantity,
            idempotency_key=f"p1-13c-{component_type}-short",
        )
        blocked_void = client.put(
            f"/api/requisition/batch-items/{requisition_item_id}/void",
            json={"reason": "已有实收必须先撤销"},
        )
        assert short_received.status_code == 200, short_received.text
        reverted = client.put(
            "/api/incoming/receipt-items/"
            f"{short_received.json()['receipt_item_id']}/revert",
            json={
                "idempotency_key": f"p1-13c-{component_type}-revert",
            },
        )
        received_again, _ = _receive_group(
            client,
            factory,
            _incoming_groups(client)[component_type],
            quantity=short_quantity,
            idempotency_key=f"p1-13c-{component_type}-again",
            receipt_fact=receipt_fact,
        )

    assert blocked_void.status_code == 409, blocked_void.text
    assert reverted.status_code == 200, reverted.text
    assert received_again.status_code == 200, received_again.text
    assert (
        received_again.json()["receipt_item_id"]
        != short_received.json()["receipt_item_id"]
    )


def test_component_inventory_coverage_separates_new_physical_board_facts(
    a3_surround_app,
) -> None:
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryReservation,
        OrderItemSemiRequirement,
        WarehouseLocation,
    )
    from app.services.warehouse_inventory import component_inventory_coverage

    _app, factory = a3_surround_app
    with factory() as db:
        location = WarehouseLocation(
            location_code="P1-13C-SEMI",
            location_name="P1-13C 半成品",
            warehouse_type="semi_finished",
            is_active=True,
        )
        db.add(location)
        db.flush()
        cover_requirement = OrderItemSemiRequirement(
            order_item_id=1,
            sales_order_item_bom_component_id=1,
            customer_id=1,
            component_type="cover",
            board_length_mm=610,
            board_width_mm=410,
            material_code_snapshot="A=A",
            normalized_material_code="A=A",
            flute_type="AB",
            pieces_per_box=1,
            stock_yield_per_sheet=1,
            required_piece_quantity=10,
        )
        base_requirement = OrderItemSemiRequirement(
            order_item_id=1,
            sales_order_item_bom_component_id=1,
            customer_id=1,
            component_type="base",
            board_length_mm=590,
            board_width_mm=390,
            material_code_snapshot="A=A",
            normalized_material_code="A=A",
            flute_type="AB",
            pieces_per_box=1,
            stock_yield_per_sheet=1,
            required_piece_quantity=10,
        )
        db.add_all([cover_requirement, base_requirement])
        db.flush()
        lots = []
        for number in ("COVER", "FINISHED", "LEGACY"):
            lot = InventoryLot(
                lot_number=f"P1-13C-{number}",
                inventory_type=(
                    "finished" if number == "FINISHED" else "semi_finished"
                ),
                warehouse_location_id=location.id,
                quantity_available=0,
                quantity_reserved=10,
                quantity_consumed=0,
                quantity_damaged=0,
                quantity_scrapped=0,
                unit="sheets",
                status="active",
                source_type="manual",
                stock_date=date(2026, 7, 29),
                last_movement_at=datetime(2026, 7, 29),
                version=1,
            )
            db.add(lot)
            db.flush()
            lots.append(lot)
        db.add_all(
            [
                InventoryReservation(
                    reservation_number="P1-13C-COVER",
                    inventory_lot_id=lots[0].id,
                    reservation_type="semi_order",
                    order_item_id=1,
                    sales_order_item_bom_component_id=1,
                    semi_requirement_id=cover_requirement.id,
                    reserved_stock_quantity=4,
                    credited_requirement_quantity=4,
                    status="active",
                    idempotency_key="p1-13c-cover",
                ),
                InventoryReservation(
                    reservation_number="P1-13C-FINISHED",
                    inventory_lot_id=lots[1].id,
                    reservation_type="finished_order",
                    order_item_id=1,
                    sales_order_item_bom_component_id=1,
                    reserved_stock_quantity=2,
                    credited_requirement_quantity=2,
                    status="active",
                    idempotency_key="p1-13c-finished",
                ),
                InventoryReservation(
                    reservation_number="P1-13C-LEGACY",
                    inventory_lot_id=lots[2].id,
                    reservation_type="semi_order",
                    order_item_id=1,
                    sales_order_item_bom_component_id=1,
                    semi_requirement_id=None,
                    reserved_stock_quantity=1,
                    credited_requirement_quantity=1,
                    status="active",
                    idempotency_key="p1-13c-legacy",
                ),
            ]
        )
        db.commit()
        cover = component_inventory_coverage(
            db,
            1,
            component_type="cover",
        )
        base = component_inventory_coverage(
            db,
            1,
            component_type="base",
        )

    assert cover == {
        "finished_piece_quantity": 2,
        "semi_piece_quantity": 5,
        "total_piece_quantity": 7,
    }
    assert base == {
        "finished_piece_quantity": 2,
        "semi_piece_quantity": 1,
        "total_piece_quantity": 3,
    }


def test_double_surround_auto_cover_reserves_twenty_physical_pieces(
    a3_surround_app,
) -> None:
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.models.warehouse_inventory import (
        InventoryLot,
        OrderItemSemiRequirement,
        SemiFinishedInventoryDetail,
        SemiFinishedLotAllowedProduct,
        WarehouseLocation,
    )

    app, factory = a3_surround_app
    with factory() as db:
        snapshot = db.get(SalesOrderItemBomComponent, 2)
        assert snapshot is not None
        location = WarehouseLocation(
            location_code="P1-13C-SURROUND",
            location_name="P1-13C 围板备料",
            warehouse_type="semi_finished",
            is_active=True,
        )
        db.add(location)
        db.flush()
        lot = InventoryLot(
            lot_number="P1-13C-SURROUND-20",
            inventory_type="semi_finished",
            warehouse_location_id=location.id,
            quantity_available=20,
            quantity_reserved=0,
            quantity_consumed=0,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit="sheets",
            status="active",
            source_type="manual",
            stock_date=date(2026, 7, 29),
            last_movement_at=datetime(2026, 7, 29),
            version=1,
        )
        db.add(lot)
        db.flush()
        db.add_all(
            [
                SemiFinishedInventoryDetail(
                    inventory_lot_id=lot.id,
                    supplier_name="匿名供应商",
                    owner_customer_id=1,
                    owner_customer_name_snapshot="P1-13C 匿名客户",
                    material_code_snapshot="A=A",
                    normalized_material_code="A=A",
                    layer_count=5,
                    flute_type="AB",
                    board_length_mm=800,
                    board_width_mm=300,
                    component_type="whole",
                    pieces_per_box=2,
                    stock_yield_per_sheet=1,
                    sheet_type="raw_board",
                    crease_type="毛片",
                ),
                SemiFinishedLotAllowedProduct(
                    inventory_lot_id=lot.id,
                    product_id=snapshot.component_product_id,
                    confirmed_by=1,
                    confirmed_at=datetime(2026, 7, 29),
                ),
            ]
        )
        db.commit()

    with TestClient(app) as client:
        _login(client)
        response = client.post(
            "/api/warehouse/finished/bom-components/2/auto-cover",
            json={
                "order_item_id": 1,
                "component_type": "whole",
                "idempotency_key": "p1-13c-surround-auto-cover",
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["semi_finished_reserved_piece_qty"] == 20
    assert response.json()["remaining_required_piece_qty"] == 0
    with factory() as db:
        requirement = db.scalar(
            select(OrderItemSemiRequirement).where(
                OrderItemSemiRequirement.sales_order_item_bom_component_id
                == 2
            )
        )
        assert requirement is not None
        assert requirement.component_type == "whole"
        assert requirement.pieces_per_box == 2
        assert requirement.required_piece_quantity == 20


def test_inventory_covered_cover_needs_only_base_and_surround_receipts(
    a3_surround_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product_bom import RequisitionItemBomSource
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryReservation,
        OrderItemSemiRequirement,
        WarehouseLocation,
    )

    app, factory = a3_surround_app
    with factory() as db:
        location = WarehouseLocation(
            location_code="P1-13C-COVERED",
            location_name="P1-13C 已备盖片",
            warehouse_type="semi_finished",
            is_active=True,
        )
        db.add(location)
        db.flush()
        requirement = OrderItemSemiRequirement(
            order_item_id=1,
            sales_order_item_bom_component_id=1,
            customer_id=1,
            component_type="cover",
            board_length_mm=610,
            board_width_mm=410,
            material_code_snapshot="A=A",
            normalized_material_code="A=A",
            flute_type="AB",
            pieces_per_box=1,
            stock_yield_per_sheet=1,
            required_piece_quantity=10,
        )
        lot = InventoryLot(
            lot_number="P1-13C-COVERED-10",
            inventory_type="semi_finished",
            warehouse_location_id=location.id,
            quantity_available=0,
            quantity_reserved=10,
            quantity_consumed=0,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit="sheets",
            status="active",
            source_type="manual",
            stock_date=date(2026, 7, 29),
            last_movement_at=datetime(2026, 7, 29),
            version=1,
        )
        db.add_all([requirement, lot])
        db.flush()
        db.add(
            InventoryReservation(
                reservation_number="P1-13C-COVERED-R",
                inventory_lot_id=lot.id,
                reservation_type="semi_order",
                order_item_id=1,
                sales_order_item_bom_component_id=1,
                semi_requirement_id=requirement.id,
                reserved_stock_quantity=10,
                credited_requirement_quantity=10,
                status="active",
                idempotency_key="p1-13c-covered-cover",
            )
        )
        db.commit()

    with TestClient(app) as client:
        _login(client)
        pending = client.get("/api/requisition/pending")
        created = client.post(
            "/api/requisition/batches",
            json={
                "supplier_name": "匿名供应商",
                "items": _source_payloads(
                    client,
                    (1, "base"),
                    (2, "whole"),
                ),
            },
        )
        incoming = client.get("/api/incoming/pending")

    assert pending.status_code == 200, pending.text
    assert {
        source["source_key"]
        for source in pending.json()["items"][0][
            "component_requirements"
        ]
        if source["can_requisition"]
    } == {"component:1:base", "component:2:whole"}
    assert created.status_code == 201, created.text
    assert {
        row["component_type"] for row in incoming.json()["items"]
    } == {"base", "whole"}
    with factory() as db:
        item = db.get(OrderItem, 1)
        assert item is not None
        assert item.requisition_status == "已报料"

    with TestClient(app) as client:
        _login(client)
        groups = _incoming_groups(client)
        base, _ = _receive_group(
            client,
            factory,
            groups["base"],
            quantity=int(groups["base"]["planned_quantity"]),
            idempotency_key="p1-13c-covered-base",
        )
        remaining_groups = _incoming_groups(client)
        surround, _ = _receive_group(
            client,
            factory,
            remaining_groups["whole"],
            quantity=int(remaining_groups["whole"]["planned_quantity"]),
            idempotency_key="p1-13c-covered-whole",
        )
    assert base.status_code == 200, base.text
    assert surround.status_code == 200, surround.text
    with factory() as db:
        item = db.get(OrderItem, 1)
        assert item is not None
        assert item.material_status == "received"


def test_database_guard_blocks_duplicate_active_source_and_allows_rereport(
    a3_surround_app,
) -> None:
    from sqlalchemy.exc import IntegrityError

    from app.models.product_bom import RequisitionItemBomSource
    from app.models.requisition import Requisition, RequisitionItem

    app, factory = a3_surround_app
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/requisition/batches",
            json={
                "supplier_name": "匿名供应商",
                "items": _source_payloads(client, (1, "cover")),
            },
        )
    assert created.status_code == 201, created.text

    with factory() as db:
        original = db.scalar(select(RequisitionItemBomSource))
        assert original is not None
        original_item = db.get(RequisitionItem, original.requisition_item_id)
        assert original_item is not None
        duplicate_batch = Requisition(
            requisition_number="P1-13C-DUPLICATE",
            requisition_date=date(2026, 7, 29),
            supplier_name="匿名供应商",
            status="已报料",
            created_by=1,
        )
        db.add(duplicate_batch)
        db.flush()
        duplicate_item = RequisitionItem(
            requisition_id=duplicate_batch.id,
            order_item_id=original_item.order_item_id,
            inventory_deducted_qty=0,
            requisition_qty=original_item.requisition_qty,
            cardboard_len=original_item.cardboard_len,
            cardboard_width=original_item.cardboard_width,
            pieces_per_box=original_item.pieces_per_box,
            required_piece_qty=original_item.required_piece_qty,
            special_process=original_item.special_process,
            material_snapshot=original_item.material_snapshot,
            product_code_snapshot=original_item.product_code_snapshot,
            product_name_snapshot="并发重复盖片",
            status="有效",
        )
        db.add(duplicate_item)
        db.flush()
        db.add(
            RequisitionItemBomSource(
                requisition_item_id=duplicate_item.id,
                sales_order_item_bom_component_id=(
                    original.sales_order_item_bom_component_id
                ),
                component_type="cover",
                active_guard=1,
                order_set_quantity=original.order_set_quantity,
                quantity_per_set=original.quantity_per_set,
                required_piece_quantity=original.required_piece_quantity,
                demand_basis=original.demand_basis,
                mold_max_yield_per_sheet=original.mold_max_yield_per_sheet,
                actual_yield_per_sheet=original.actual_yield_per_sheet,
                spare_sheet_quantity=original.spare_sheet_quantity,
                calculated_purchase_quantity=(
                    original.calculated_purchase_quantity
                ),
                direction_note="并发门禁测试",
                calculation_rule_version=(
                    original.calculation_rule_version
                ),
            )
        )
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()
        original_id = original.requisition_item_id

    with TestClient(app) as client:
        _login(client)
        voided = client.put(
            f"/api/requisition/batches/{created.json()['id']}/void",
            json={"reason": "释放活动来源门禁"},
        )
        rereported = client.post(
            "/api/requisition/batches",
            json={
                "supplier_name": "匿名供应商",
                "items": _source_payloads(client, (1, "cover")),
            },
        )
    assert voided.status_code == 200, voided.text
    assert rereported.status_code == 201, rereported.text
    with factory() as db:
        sources = db.scalars(
            select(RequisitionItemBomSource).order_by(
                RequisitionItemBomSource.id
            )
        ).all()
        assert [source.active_guard for source in sources] == [None, 1]


def test_partial_receipt_blocks_batch_and_order_level_void(
    a3_surround_app,
) -> None:
    from app.models.product_bom import RequisitionItemBomSource

    app, factory = a3_surround_app
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/requisition/batches",
            json={
                "supplier_name": "匿名供应商",
                "items": _source_payloads(
                    client,
                    (1, "cover"),
                    (1, "base"),
                    (2, "whole"),
                ),
            },
        )
    assert created.status_code == 201, created.text
    with factory() as db:
        cover_source = db.scalar(
            select(RequisitionItemBomSource).where(
                RequisitionItemBomSource.component_type == "cover"
            )
        )
        assert cover_source is not None

    with TestClient(app) as client:
        _login(client)
        partial, _ = _receive_group(
            client,
            factory,
            _incoming_groups(client)["cover"],
            quantity=5,
            idempotency_key="p1-13c-partial-cover",
        )
        batch_void = client.put(
            f"/api/requisition/batches/{created.json()['id']}/void",
            json={"reason": "部分实收后不可整批作废"},
        )
        order_void = client.put(
            "/api/requisition/items/1/cancel",
            json={"reason": "部分实收后不可整单撤销"},
        )
        reported = client.get("/api/requisition/reported-documents")

    assert partial.status_code == 200, partial.text
    assert batch_void.status_code == 409, batch_void.text
    assert order_void.status_code == 409, order_void.text
    document = next(
        row
        for row in reported.json()["items"]
        if row["id"] == created.json()["id"]
        and row["source_type"] == "composite_bom_requisition"
    )
    assert document["can_void"] is False
    can_void_by_type = {
        line["component_type"]: line["can_void"]
        for line in document["line_items"]
    }
    assert can_void_by_type == {
        "cover": False,
        "base": True,
        "whole": True,
    }


def test_direct_void_ids_enforce_permission_and_customer_scope(
    a3_surround_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.order import OrderItem
    from app.models.product_bom import RequisitionItemBomSource
    from app.models.requisition import Requisition, RequisitionItem

    app, factory = a3_surround_app
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/requisition/batches",
            json={
                "supplier_name": "匿名供应商",
                "items": _source_payloads(
                    client,
                    (1, "cover"),
                    (1, "base"),
                    (2, "whole"),
                ),
            },
        )
    assert created.status_code == 201, created.text
    batch_id = created.json()["id"]
    with factory() as db:
        line_id = db.scalar(
            select(RequisitionItem.id)
            .where(RequisitionItem.requisition_id == batch_id)
            .order_by(RequisitionItem.id)
        )
        assert line_id is not None
        denied_action_names = {
            "CANCEL_REQUISITION",
            "VOID_COMPOSITE_REQUISITION_ITEM",
            "VOID_COMPOSITE_REQUISITION",
        }
        denied_action_count_before = len(
            db.scalars(
                select(OperationLog.id).where(
                    OperationLog.action.in_(denied_action_names)
                )
            ).all()
        )

    endpoint_calls = [
        (
            f"/api/requisition/batch-items/{line_id}/void",
            {"reason": "直接 ID 权限验证"},
        ),
        (
            f"/api/requisition/batches/{batch_id}/void",
            {"reason": "直接 ID 权限验证"},
        ),
        (
            "/api/requisition/items/1/cancel",
            {"reason": "直接 ID 权限验证"},
        ),
    ]
    results: dict[str, list[tuple[int, str]]] = {}
    for username in ("p1-13c-no-permission", "p1-13c-scoped"):
        with TestClient(app) as client:
            _login(client, username=username)
            responses = [
                client.put(endpoint, json=payload)
                for endpoint, payload in endpoint_calls
            ]
            results[username] = [
                (response.status_code, response.json()["detail"])
                for response in responses
            ]

    assert results == {
        "p1-13c-no-permission": [
            (403, "权限不足"),
            (403, "权限不足"),
            (403, "权限不足"),
        ],
        "p1-13c-scoped": [
            (403, "无客户访问权限"),
            (403, "无客户访问权限"),
            (403, "权限不足"),
        ],
    }
    with factory() as db:
        batch = db.get(Requisition, batch_id)
        assert batch is not None
        assert batch.status == "已报料"
        rows = db.scalars(
            select(RequisitionItem).where(
                RequisitionItem.requisition_id == batch_id
            )
        ).all()
        assert rows
        assert all(row.status == "有效" for row in rows)
        sources = db.scalars(
            select(RequisitionItemBomSource).where(
                RequisitionItemBomSource.requisition_item_id.in_(
                    [row.id for row in rows]
                )
            )
        ).all()
        assert sources
        assert all(source.active_guard == 1 for source in sources)
        order_item = db.get(OrderItem, 1)
        assert order_item is not None
        assert order_item.requisition_status == "已报料"
        assert len(
            db.scalars(
                select(OperationLog.id).where(
                    OperationLog.action.in_(denied_action_names)
                )
            ).all()
        ) == (
            denied_action_count_before
        )


def test_unknown_box_style_does_not_receive_generic_requisition_formula() -> None:
    from app.api.requisition import _suggested_dimensions
    from app.models.product import Product

    product = Product(
        customer_id=1,
        product_code="UNKNOWN",
        product_name="未知箱型",
        box_category="normal",
        box_style="未来新箱型",
        length_mm=Decimal("100"),
        width_mm=Decimal("200"),
        height_mm=Decimal("300"),
    )
    assert _suggested_dimensions(product) == (None, None)


def test_migration_is_linear_and_downgrade_checks_only_unrepresentable_facts() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/cy81v8x9z70_bom_a3_physical_sources.py"
    ).read_text(encoding="utf-8")
    assert 'down_revision: Union[str, Sequence[str], None] = "cw79v8x9z68"' in source
    assert "WHERE component_type <> 'whole'" in source
    assert "HAVING COUNT(*) > 1" in source
    assert "component_type IN ('whole','cover','base')" in source
    assert "duplicate_active_physical_sources" in source
    assert "active_guard = 1" in source


def _run_alembic(
    repository_root: Path,
    database_path: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["ERP_DATABASE_PATH"] = str(database_path)
    environment["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=repository_root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _database_state(database_path: Path) -> dict:
    with sqlite3.connect(database_path) as connection:
        return {
            "revision": connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()[0],
            "integrity": connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0],
            "foreign_key_errors": connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall(),
            "source_columns": [
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(requisition_item_bom_sources)"
                )
            ],
            "active_index_count": connection.execute(
                """
                SELECT COUNT(*)
                  FROM sqlite_master
                 WHERE type = 'index'
                   AND name =
                       'uq_requisition_item_bom_sources_active_physical_source'
                """
            ).fetchone()[0],
        }


def test_migration_roundtrip_and_duplicate_preflight_are_repeatable(
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    cw79_source = tmp_path / "p1-13c-cw79-source.sqlite3"
    normal_target = tmp_path / "p1-13c-normal-roundtrip.sqlite3"
    duplicate_target = tmp_path / "p1-13c-duplicate-preflight.sqlite3"

    base_upgrade = _run_alembic(
        repository_root,
        cw79_source,
        "upgrade",
        "cw79v8x9z68",
    )
    assert base_upgrade.returncode == 0, (
        base_upgrade.stdout + base_upgrade.stderr
    )
    assert _database_state(cw79_source) == {
        "revision": "cw79v8x9z68",
        "integrity": "ok",
        "foreign_key_errors": [],
        "source_columns": [
            "id",
            "requisition_item_id",
            "sales_order_item_bom_component_id",
            "order_set_quantity",
            "quantity_per_set",
            "required_piece_quantity",
            "mold_max_yield_per_sheet",
            "actual_yield_per_sheet",
            "spare_sheet_quantity",
            "calculated_purchase_quantity",
            "direction_note",
            "created_at",
            "calculation_rule_version",
            "demand_basis",
        ],
        "active_index_count": 0,
    }

    shutil.copy2(cw79_source, normal_target)
    for arguments, expected_revision, expected_component_column in (
        (("upgrade", "cy81v8x9z70"), "cy81v8x9z70", True),
        (("downgrade", "cw79v8x9z68"), "cw79v8x9z68", False),
        (("upgrade", "cy81v8x9z70"), "cy81v8x9z70", True),
    ):
        migrated = _run_alembic(
            repository_root,
            normal_target,
            *arguments,
        )
        assert migrated.returncode == 0, migrated.stdout + migrated.stderr
        state = _database_state(normal_target)
        assert state["revision"] == expected_revision
        assert state["integrity"] == "ok"
        assert state["foreign_key_errors"] == []
        assert (
            "component_type" in state["source_columns"]
        ) is expected_component_column
        assert state["active_index_count"] == int(
            expected_component_column
        )

    shutil.copy2(cw79_source, duplicate_target)
    with sqlite3.connect(duplicate_target) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            INSERT INTO customers (id, name)
            VALUES (930001, 'duplicate-customer')
            """
        )
        connection.executemany(
            """
            INSERT INTO products (
                id, customer_id, product_code,
                customer_material_code, product_name
            ) VALUES (?, 930001, ?, ?, ?)
            """,
            [
                (930011, "PARENT", "PARENT", "parent"),
                (930012, "COMP", "COMP", "component"),
            ],
        )
        connection.execute(
            """
            INSERT INTO sales_orders (
                id, order_number, customer_id, order_date
            ) VALUES (920001, 'DUP-ORDER', 930001, '2026-07-29')
            """
        )
        connection.execute(
            """
            INSERT INTO sales_order_items (
                id, order_id, product_id, quantity,
                unit_price, subtotal, snapshot_product_name
            ) VALUES (920001, 920001, 930011, 10, 1, 10, 'parent')
            """
        )
        connection.execute(
            """
            INSERT INTO sales_order_item_bom_components (
                id, sales_order_item_id, component_product_id,
                order_set_quantity, quantity_per_set,
                required_piece_quantity, display_order,
                internal_component_code, is_die_cut,
                spare_sheet_quantity, display_mode, is_required,
                snapshot_component_product_code,
                snapshot_component_product_name,
                snapshot_component_box_category,
                snapshot_schema_version,
                snapshot_component_default_cutting_mode
            ) VALUES (
                930001, 920001, 930012,
                10, 1, 10, 1,
                'COMP', 0, 0, 'internal_only', 1,
                'COMP', 'component', 'normal', 1, ?
            )
            """,
            ("\u4e00\u5f00\u4e00",),
        )
        connection.executemany(
            """
            INSERT INTO material_requisitions (
                id, requisition_number, requisition_date, status
            ) VALUES (?, ?, '2026-07-29', 'active')
            """,
            [
                (910001, "DUP-REQ-1"),
                (910002, "DUP-REQ-2"),
            ],
        )
        connection.executemany(
            """
            INSERT INTO material_requisition_items (
                id, requisition_id, order_item_id,
                inventory_deducted_qty, requisition_qty,
                cardboard_len, cardboard_width, special_process,
                product_name_snapshot, status,
                pieces_per_box, required_piece_qty
            ) VALUES (?, ?, ?, 0, 10, 470, 600, 'test',
                      'duplicate-preflight', 'active', 1, 10)
            """,
            [
                (900001, 910001, 920001),
                (900002, 910002, 920001),
            ],
        )
        connection.executemany(
            """
            INSERT INTO requisition_item_bom_sources (
                id, requisition_item_id,
                sales_order_item_bom_component_id,
                order_set_quantity, quantity_per_set,
                required_piece_quantity,
                mold_max_yield_per_sheet,
                actual_yield_per_sheet,
                spare_sheet_quantity,
                calculated_purchase_quantity,
                direction_note,
                calculation_rule_version,
                demand_basis
            ) VALUES (?, ?, 930001, 10, 1, 10,
                      1, 1, 0, 10, NULL,
                      'duplicate-preflight-v1', 'order_sets')
            """,
            [
                (940001, 900001),
                (940002, 900002),
            ],
        )
        connection.commit()
        assert connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall() == []

    failed_upgrade = _run_alembic(
        repository_root,
        duplicate_target,
        "upgrade",
        "cy81v8x9z70",
    )
    assert failed_upgrade.returncode != 0
    failure_output = failed_upgrade.stdout + failed_upgrade.stderr
    assert "RuntimeError" in failure_output
    assert "任何 DDL 前停止" in failure_output
    failed_state = _database_state(duplicate_target)
    assert failed_state["revision"] == "cw79v8x9z68"
    assert failed_state["integrity"] == "ok"
    assert failed_state["foreign_key_errors"] == []
    assert "component_type" not in failed_state["source_columns"]
    assert "active_guard" not in failed_state["source_columns"]
    assert failed_state["active_index_count"] == 0


def test_reported_composite_history_is_not_routed_through_supplier_item_void() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "static/index.html"
    ).read_text(encoding="utf-8")
    assert 'v-for="row in requisitionItems" :key="row.stable_id"' in source
    assert 'row.source_type === "supplier_order"' in source
    assert "/api/requisition/supplier-order-items/${attempt.itemId}/void" in source
    assert "row.source_type !== \"supplier_order\"" in source
    # The legacy composite endpoint remains available to its existing guarded
    # callers, but the new physical supplier-line button never invokes it.
    assert "const targetId = Number(line?.id || 0)" in source
    assert "/api/requisition/batch-items/${targetId}/void" in source
    assert "executeRequisitionVoidAction" in source
