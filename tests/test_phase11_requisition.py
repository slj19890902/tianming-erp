from __future__ import annotations

import sqlite3
from copy import deepcopy
from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def requisition_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.incoming import router as incoming_router
    from app.api.requisition import router as requisition_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserPermissionOverride
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.supplier import Supplier
    from app.models.user import User
    from app.services.supplier_master import normalize_supplier_identity

    engine = create_sqlite_engine(tmp_path / "requisition.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        users = [
            User(
                username=role,
                password_hash=hash_password("RolePass123!"),
                role=role,
                real_name=role,
                display_name=role,
                must_change_password=False,
            )
            for role in ("admin", "finance", "sales", "workshop")
        ]
        customer = Customer(
            customer_number=1,
            customer_code="SME",
            name="苏州思迈尔包装有限公司",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        session.add_all([*users, customer])
        for index, supplier_name in enumerate(
            (
                "苏州纸板供应商",
                "更新后的供应商",
                "昆山鸣明",
                "嘉林亿",
                "鸣朋",
                "苏州嘉林亿",
                "N005测试供应商",
            ),
            start=1,
        ):
            session.add(
                Supplier(
                    standard_name=supplier_name,
                    normalized_name=normalize_supplier_identity(supplier_name),
                    display_name=supplier_name,
                    sort_order=index * 10,
                    is_active=True,
                    version=1,
                )
            )
        session.flush()
        sales = next(user for user in users if user.role == "sales")
        session.add_all(
            [
                UserPermissionOverride(
                    user_id=sales.id,
                    permission_code="requisition.view",
                    is_allowed=True,
                ),
                UserPermissionOverride(
                    user_id=sales.id,
                    permission_code="requisition.execute",
                    is_allowed=True,
                ),
            ]
        )
        product = Product(
            customer_id=customer.id,
            product_code="21301028",
            customer_material_code="SME-028",
            product_name="中性外箱",
            legacy_material_text="K=A-BC",
            length_mm=Decimal("520"),
            width_mm=Decimal("350"),
            height_mm=Decimal("300"),
            box_category="normal",
            box_style="A1",
            flap_mm=30,
        )
        session.add(product)
        session.flush()
        order = Order(
            order_number="PO-20260614-001",
            customer_id=customer.id,
            order_date=date(2026, 6, 14),
            delivery_date=date(2026, 6, 21),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("360"),
        )
        session.add(order)
        session.flush()
        session.add(
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=100,
                unit_price=Decimal("3.60"),
                subtotal=Decimal("360"),
                material_status="pending",
                snapshot_product_name="中性外箱",
                snapshot_spec="520×350×300mm",
                snapshot_material="K=A-BC",
            )
        )
        session.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(requisition_router, prefix="/api/requisition")
    app.include_router(incoming_router, prefix="/api/incoming")

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return app, session_factory


def _login(client: TestClient, role: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": role, "password": "RolePass123!"},
    )
    assert response.status_code == 200


def _batch_payload() -> dict:
    return {
        "supplier_name": "苏州纸板供应商",
        "items": [
            {
                "order_item_id": 1,
                "inventory_deducted_qty": 0,
                "requisition_qty": 27,
                "cardboard_len": "1756",
                "cardboard_width": "1962",
                "special_process": "一开三",
                "remark": "按一开三采购",
            }
        ],
    }


def test_requisition_batch_rejects_disabled_and_unknown_payload_supplier(
    requisition_app,
) -> None:
    from app.models.requisition import Requisition
    from app.models.supplier import Supplier, SupplierAlias
    from app.services.supplier_master import normalize_supplier_identity

    app, session_factory = requisition_app
    with session_factory() as session:
        supplier = Supplier(
            standard_name="苏州佳丰",
            normalized_name=normalize_supplier_identity("苏州佳丰"),
            display_name="佳丰",
            sort_order=900,
            is_active=False,
            version=1,
        )
        session.add(supplier)
        session.flush()
        session.add(
            SupplierAlias(
                supplier_id=supplier.id,
                alias_name="佳丰",
                normalized_alias=normalize_supplier_identity("佳丰"),
            )
        )
        session.commit()

    with TestClient(app) as client:
        _login(client, "admin")
        disabled_payload = _batch_payload()
        disabled_payload["supplier_name"] = "佳丰"
        disabled = client.post(
            "/api/requisition/batches",
            json=disabled_payload,
        )
        unknown_payload = _batch_payload()
        unknown_payload["supplier_name"] = "未建档纸板厂"
        unknown = client.post(
            "/api/requisition/batches",
            json=unknown_payload,
        )

    assert disabled.status_code == 400
    assert "已停用" in disabled.text
    assert unknown.status_code == 400
    assert "尚未建档" in unknown.text
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Requisition)) == 0


def test_material_candidate_and_pending_material_gate_only_actual_changes(
    requisition_app,
) -> None:
    from app.models.material import Material
    from app.models.order import OrderItem
    from app.models.supplier import Supplier, SupplierAlias
    from app.services.supplier_master import normalize_supplier_identity

    app, session_factory = requisition_app
    with session_factory() as session:
        active_material = Material(
            code="CANDIDATE-ACTIVE",
            layer_count=3,
            flute_type="E",
            supplier_name="嘉林亿",
        )
        disabled_supplier = Supplier(
            standard_name="苏州佳丰",
            normalized_name=normalize_supplier_identity("苏州佳丰"),
            display_name="佳丰",
            sort_order=900,
            is_active=False,
            version=1,
        )
        disabled_material = Material(
            code="CANDIDATE-JF",
            layer_count=3,
            flute_type="E",
            supplier_name="佳丰",
        )
        unknown_material = Material(
            code="CANDIDATE-UNKNOWN",
            layer_count=3,
            flute_type="E",
            supplier_name="未建档纸板厂",
        )
        session.add_all(
            [
                active_material,
                disabled_supplier,
                disabled_material,
                unknown_material,
            ]
        )
        session.flush()
        session.add(
            SupplierAlias(
                supplier_id=disabled_supplier.id,
                alias_name="佳丰",
                normalized_alias=normalize_supplier_identity("佳丰"),
            )
        )
        session.commit()
        active_material_id = active_material.id
        disabled_material_id = disabled_material.id
        unknown_material_id = unknown_material.id

    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post(
            "/api/requisition/material-candidates",
            json={
                "customer_id": 1,
                "original_material_code": "客户原材质",
                "material_id": active_material_id,
            },
        )
        assert created.status_code == 201, created.text
        candidate_id = created.json()["id"]

        with session_factory() as session:
            active_supplier = session.scalar(
                select(Supplier).where(Supplier.standard_name == "嘉林亿")
            )
            assert active_supplier is not None
            active_supplier.is_active = False
            item = session.get(OrderItem, 1)
            disabled_material = session.get(Material, disabled_material_id)
            assert item is not None and disabled_material is not None
            item.material_id = disabled_material.id
            item.snapshot_material = disabled_material.code
            item.snapshot_supplier_name = disabled_material.supplier_name
            item.snapshot_weight = disabled_material.basis_weight_description
            item.layer_count = disabled_material.layer_count
            item.flute_type = disabled_material.flute_type
            session.commit()

        unchanged_candidate = client.put(
            f"/api/requisition/material-candidates/{candidate_id}",
            json={
                "material_id": active_material_id,
                "notes": "仅补历史备注",
            },
        )
        disabled_candidate = client.put(
            f"/api/requisition/material-candidates/{candidate_id}",
            json={"material_id": disabled_material_id},
        )
        unknown_candidate = client.put(
            f"/api/requisition/material-candidates/{candidate_id}",
            json={"material_id": unknown_material_id},
        )
        unchanged_pending = client.put(
            "/api/requisition/pending/1/material",
            json={
                "material_id": disabled_material_id,
                "layer_count": 3,
                "flute_type": "E",
            },
        )
        unknown_pending = client.put(
            "/api/requisition/pending/1/material",
            json={
                "material_id": unknown_material_id,
                "layer_count": 3,
                "flute_type": "E",
            },
        )

    assert unchanged_candidate.status_code == 200, unchanged_candidate.text
    assert disabled_candidate.status_code == 400
    assert "已停用" in disabled_candidate.text
    assert unknown_candidate.status_code == 400
    assert "尚未建档" in unknown_candidate.text
    assert unchanged_pending.status_code == 200, unchanged_pending.text
    assert unknown_pending.status_code == 400
    assert "尚未建档" in unknown_pending.text


def test_merge_group_same_historical_supplier_allows_non_supplier_edit(
    requisition_app,
) -> None:
    from app.models.supplier import Supplier, SupplierAlias
    from app.services.supplier_master import normalize_supplier_identity

    app, session_factory = requisition_app
    second_id = _add_second_merge_candidate(session_factory)
    with TestClient(app) as client:
        _login(client, "sales")
        created = _create_merge_group(client, [1, second_id])

        with session_factory() as session:
            current_supplier = session.scalar(
                select(Supplier).where(
                    Supplier.standard_name == "苏州纸板供应商"
                )
            )
            assert current_supplier is not None
            current_supplier.is_active = False
            disabled_supplier = Supplier(
                standard_name="苏州佳丰",
                normalized_name=normalize_supplier_identity("苏州佳丰"),
                display_name="佳丰",
                sort_order=900,
                is_active=False,
                version=1,
            )
            session.add(disabled_supplier)
            session.flush()
            session.add(
                SupplierAlias(
                    supplier_id=disabled_supplier.id,
                    alias_name="佳丰",
                    normalized_alias=normalize_supplier_identity("佳丰"),
                )
            )
            session.commit()

        unchanged = client.put(
            f"/api/requisition/merge-groups/{created['id']}",
            json={
                "supplier_name": "苏州纸板供应商",
                "remark": "只改历史合并组备注",
            },
        )
        disabled_change = client.put(
            f"/api/requisition/merge-groups/{created['id']}",
            json={"supplier_name": "佳丰"},
        )
        unknown_change = client.put(
            f"/api/requisition/merge-groups/{created['id']}",
            json={"supplier_name": "未建档纸板厂"},
        )

    assert unchanged.status_code == 200, unchanged.text
    assert disabled_change.status_code == 400
    assert "已停用" in disabled_change.text
    assert unknown_change.status_code == 400
    assert "尚未建档" in unknown_change.text


def _add_pending_candidate(
    session_factory,
    suffix: int,
    *,
    product_code: str | None = None,
    product_name: str | None = None,
    quantity: int = 80,
    supplier_name: str | None = "苏州纸板供应商",
    material_code: str | None = None,
    layer_count: int | None = None,
    flute_type: str | None = None,
) -> int:
    from app.models.material import Material
    from app.models.order import Order, OrderItem
    from app.models.product import Product

    with session_factory() as session:
        customer_id = 1
        material = None
        if material_code:
            material = session.query(Material).filter(Material.code == material_code).one_or_none()
            if material is None:
                material = Material(
                    code=material_code,
                    paper_composition=material_code,
                    layer_count=layer_count,
                    flute_type=flute_type,
                    supplier_name=supplier_name,
                )
                session.add(material)
                session.flush()
        code = product_code or f"213010{27 + suffix:02d}"
        product = Product(
            customer_id=customer_id,
            product_code=code,
            customer_material_code=f"SME-{suffix:03d}",
            product_name=product_name or f"中性纸箱{suffix}",
            legacy_material_text="K=A-BC",
            length_mm=Decimal("520"),
            width_mm=Decimal("350"),
            height_mm=Decimal("300"),
            box_category="normal",
        )
        session.add(product)
        session.flush()
        order = Order(
            order_number=f"PO-20260614-{suffix:03d}",
            customer_id=customer_id,
            order_date=date(2026, 6, 14),
            delivery_date=date(2026, 6, 22),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal(str(quantity * 3.6)),
        )
        session.add(order)
        session.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=quantity,
            unit_price=Decimal("3.60"),
            subtotal=Decimal(str(quantity * 3.6)),
            material_status="pending",
            requisition_status="未报料",
            snapshot_product_code=code,
            snapshot_product_name=product.product_name,
            snapshot_spec="520×350×300mm",
            snapshot_material=material_code or "K=A-BC",
            snapshot_supplier_name=supplier_name,
            material_id=material.id if material else None,
            layer_count=layer_count,
            flute_type=flute_type,
        )
        session.add(item)
        session.commit()
        return item.id


def _add_second_merge_candidate(session_factory) -> int:
    return _add_pending_candidate(
        session_factory,
        2,
        product_code="21301029",
        product_name="中性内箱",
    )


def _create_merge_group(client: TestClient, item_ids: list[int]) -> dict:
    response = client.post(
        "/api/requisition/merge-groups",
        json={
            "member_item_ids": item_ids,
            "supplier_name": "苏州纸板供应商",
            "report_length_mm": 1000,
            "report_width_mm": 800,
            "cutting_mode": "一开一",
            "remark": "先合并待报料",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _preview_supplier_order_draft(client: TestClient, selections: list[dict]) -> dict:
    response = client.post(
        "/api/requisition/supplier-orders/preview-from-pending-selection",
        json={"selections": selections},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _save_supplier_order_draft(client: TestClient, draft: dict):
    return client.post(
        "/api/requisition/supplier-orders/from-pending-selection",
        json=draft,
    )


def test_frontend_merge_suggestion_confirm_does_not_create_supplier_order() -> None:
    index = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert 'confirmMergeSuggestion(sg, sgIndex)' in index
    assert "async generateSupplierOrder" not in index
    assert "生成供应商报料单" not in index

    start = index.index("async confirmMergeSuggestion")
    end = index.index("async loadSupplierOrders", start)
    confirm_block = index[start:end]
    assert "/api/requisition/merge-groups" in confirm_block
    assert "/api/requisition/supplier-orders" not in confirm_block
    assert "/supplier-order" not in confirm_block
    assert "createSupplierOrderFromMergeGroup" not in confirm_block
    assert 'class="btn success" @click="openSupplierRequisitionDraft()"' in index
    assert "selectedPendingKeys: []" in index
    assert "pendingRowKey(row)" in index
    assert "togglePendingRow(row, checked)" in index
    assert "selectedPendingRows(rows = null)" in index
    open_start = index.index("async openSupplierRequisitionDraft")
    open_end = index.index("async openRequisition", open_start)
    open_block = index[open_start:open_end]
    assert "Array.isArray(rows)" in open_block
    assert "this.selectedPendingRows()" in open_block
    assert "this.requisitionPending.filter(row => this.requisitionSelected" not in open_block
    assert "row.order_item_id || row.item_id || row.id" in index
    assert "row.merge_group_id || row.id" in index
    assert "/api/requisition/supplier-orders/preview-from-pending-selection" in open_block
    assert "/api/requisition/supplier-orders/from-pending-selection" not in open_block
    save_start = index.index("async saveSupplierRequisitionDraft")
    save_end = index.index("async openSupplierRequisitionDraft", save_start)
    save_block = index[save_start:save_end]
    assert "/api/requisition/supplier-orders/from-pending-selection" in index
    assert "/api/requisition/supplier-orders/from-pending-selection" in save_block
    assert "`/api/requisition/merge-groups/${row.merge_group_id}/supplier-order`" not in index
    assert "merge-size-input" in index
    assert "merge-supplier-select" in index


def test_frontend_supplier_draft_shows_duplicate_dimension_and_quantity_checks() -> None:
    index = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    for expected in (
        "报料防错检查",
        "疑似长宽填反",
        "按人工尺寸继续",
        "确认超量报料",
        "supplierDraftLineIsSwapped(line)",
        "supplierDraftQuantityAfter(line)",
        "request_key: group.request_key || null",
        "dimension_override_acknowledged",
        "quantity_override_acknowledged",
    ):
        assert expected in index
    assert "/api/requisition/reported-documents" in index
    assert "draftGroupLines(group)" in index
    assert "line.source_items || []" in index
    assert "supplierOrderPrintLines(modal.data)" in index
    assert "reported-compact-table" in index
    assert "reported-source-cell" in index
    assert "reported-code-cell" in index


def test_preview_supplier_order_draft_supports_single_regular_pending_item(
    requisition_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    app, session_factory = requisition_app
    with TestClient(app) as client:
        _login(client, "sales")
        draft = _preview_supplier_order_draft(
            client,
            [
                {
                    "type": "order_item",
                    "order_item_id": 1,
                    "report_length_mm": 1000,
                    "report_width_mm": 800,
                }
            ],
    )

    assert draft["supplier_groups"]
    line = draft["supplier_groups"][0]["lines"][0]
    assert line["source_type"] == "normal"
    assert line["source_items"][0]["source_type"] == "order_item"
    assert line["source_items"][0]["order_item_id"] == 1
    with session_factory() as session:
        assert session.query(SupplierRequisitionOrder).count() == 0
        assert session.query(SupplierRequisitionOrderItem).count() == 0
        assert session.get(OrderItem, 1).requisition_status == "未报料"


def test_a3_supplier_draft_treats_400_sheets_as_two_200_sheet_components(
    requisition_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    app, session_factory = requisition_app
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        product = session.get(Product, item.product_id)
        product.box_style = "A3 天地盖"
        item.quantity = 200
        item.snapshot_product_name = "A3 订单 200 只"
        item.snapshot_report_length_mm = 2145
        item.snapshot_report_width_mm = 1055
        item.snapshot_base_report_length_mm = 2120
        item.snapshot_base_report_width_mm = 1035
        item.snapshot_splice_mode = "single"
        item.snapshot_pieces_per_box = 1
        session.commit()

    selection = {
        "type": "order_item",
        "order_item_id": 1,
        "supplier_name": "苏州纸板供应商",
        "report_length_mm": 2145,
        "report_width_mm": 1055,
        "cutting_mode": "一开一",
    }
    with TestClient(app) as client:
        _login(client, "sales")
        draft = _preview_supplier_order_draft(client, [selection])
        lines = draft["supplier_groups"][0]["lines"]
        saved = _save_supplier_order_draft(client, draft)

    assert len(lines) == 2
    by_component = {
        line["source_items"][0]["component_type"]: line for line in lines
    }
    assert set(by_component) == {"cover", "base"}
    assert by_component["cover"]["requisition_qty"] == 200
    assert by_component["cover"]["remaining_requisition_qty"] == 200
    assert by_component["base"]["requisition_qty"] == 200
    assert by_component["base"]["remaining_requisition_qty"] == 200
    assert sum(line["requisition_qty"] for line in lines) == 400
    assert all(not line.get("quantity_override_acknowledged", False) for line in lines)
    assert saved.status_code == 201, saved.text

    with session_factory() as session:
        item = session.get(OrderItem, 1)
        supplier_order = session.query(SupplierRequisitionOrder).one()
        supplier_lines = (
            session.query(SupplierRequisitionOrderItem)
            .order_by(SupplierRequisitionOrderItem.id)
            .all()
        )
        assert item.quantity == 200
        assert item.requisition_qty == 400
        assert item.delivered_quantity == 0
        assert supplier_order.total_quantity == 400
        assert supplier_order.requisition_qty == 400
        assert [row.requisition_qty for row in supplier_lines] == [200, 200]
        assert [row.source_key for row in supplier_lines] == [
            "order_item:1:cover",
            "order_item:1:base",
        ]
        assert [row.product_name for row in supplier_lines] == [
            "A3 订单 200 只-盖",
            "A3 订单 200 只-底",
        ]


def test_a3_supplier_draft_allows_cover_and_base_partial_100_of_200(
    requisition_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    app, session_factory = requisition_app
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        product = session.get(Product, item.product_id)
        product.box_style = "A3 天地盖"
        item.quantity = 200
        item.snapshot_product_name = "TD010 订单 200 套"
        item.snapshot_report_length_mm = 2145
        item.snapshot_report_width_mm = 1055
        item.snapshot_base_report_length_mm = 2120
        item.snapshot_base_report_width_mm = 1035
        item.snapshot_splice_mode = "single"
        item.snapshot_pieces_per_box = 1
        session.commit()

    selection = {
        "type": "order_item",
        "order_item_id": 1,
        "supplier_name": "苏州纸板供应商",
        "report_length_mm": 2145,
        "report_width_mm": 1055,
        "cutting_mode": "一开一",
    }
    with TestClient(app) as client:
        _login(client, "sales")
        draft = _preview_supplier_order_draft(client, [selection])
        for line in draft["supplier_groups"][0]["lines"]:
            line["requisition_qty"] = 100
        saved = _save_supplier_order_draft(client, draft)
        pending = client.get("/api/requisition/pending")
        next_draft = _preview_supplier_order_draft(client, [selection])

    assert saved.status_code == 201, saved.text
    next_by_component = {
        line["source_items"][0]["component_type"]: line
        for line in next_draft["supplier_groups"][0]["lines"]
    }
    assert set(next_by_component) == {"cover", "base"}
    for component in ("cover", "base"):
        assert next_by_component[component]["already_requisitioned_qty"] == 100
        assert next_by_component[component]["remaining_requisition_qty"] == 100
        assert next_by_component[component]["requisition_qty"] == 100
    pending_row = next(
        row for row in pending.json()["items"] if row.get("item_id") == 1
    )
    assert pending_row["already_requisitioned_qty"] == 200
    assert pending_row["remaining_requisition_qty"] == 200

    with session_factory() as session:
        item = session.get(OrderItem, 1)
        supplier_order = session.query(SupplierRequisitionOrder).one()
        supplier_lines = (
            session.query(SupplierRequisitionOrderItem)
            .order_by(SupplierRequisitionOrderItem.id)
            .all()
        )
        assert item.quantity == 200
        assert item.requisition_qty == 200
        assert supplier_order.requisition_qty == 200
        assert [row.requisition_qty for row in supplier_lines] == [100, 100]
        assert [row.source_key for row in supplier_lines] == [
            "order_item:1:cover",
            "order_item:1:base",
        ]


def test_preview_supplier_order_draft_supports_multiple_regular_pending_items(
    requisition_app,
) -> None:
    from app.models.supplier_requisition_order import SupplierRequisitionOrder

    app, session_factory = requisition_app
    second_id = _add_pending_candidate(session_factory, 3, product_code="21301030")
    with TestClient(app) as client:
        _login(client, "sales")
        draft = _preview_supplier_order_draft(
            client,
            [
                {
                    "type": "order_item",
                    "order_item_id": 1,
                    "report_length_mm": 1000,
                    "report_width_mm": 800,
                },
                {
                    "type": "order_item",
                    "order_item_id": second_id,
                    "report_length_mm": 1000,
                    "report_width_mm": 800,
                },
            ],
        )

    draft_item_ids = {
        source["order_item_id"]
        for group in draft["supplier_groups"]
        for row in group["lines"]
        for source in row["source_items"]
    }
    assert draft_item_ids == {
        1,
        second_id,
    }
    with session_factory() as session:
        assert session.query(SupplierRequisitionOrder).count() == 0


def test_preview_supplier_order_draft_supports_regular_item_and_merge_group(
    requisition_app,
) -> None:
    from app.models.requisition import Requisition
    from app.models.supplier_requisition_order import SupplierRequisitionOrder

    app, session_factory = requisition_app
    second_id = _add_second_merge_candidate(session_factory)
    regular_id = _add_pending_candidate(session_factory, 3, product_code="21301030")
    with TestClient(app) as client:
        _login(client, "sales")
        created = _create_merge_group(client, [1, second_id])
        draft = _preview_supplier_order_draft(
            client,
            [
                {"type": "merge_group", "merge_group_id": created["id"]},
                {
                    "type": "order_item",
                    "order_item_id": regular_id,
                    "report_length_mm": 1000,
                    "report_width_mm": 800,
                },
            ],
        )

    assert len(draft["supplier_groups"]) == 1
    lines = draft["supplier_groups"][0]["lines"]
    assert {row["source_type"] for row in lines} == {"merge_group", "normal"}
    assert {
        source["order_item_id"]
        for row in lines
        for source in row["source_items"]
    } == {1, second_id, regular_id}
    with session_factory() as session:
        assert session.query(SupplierRequisitionOrder).count() == 0
        assert session.get(Requisition, created["id"]).status == "merged_pending"


def test_merge_group_lifecycle_creates_supplier_order_only_at_final_step(
    requisition_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    app, session_factory = requisition_app
    second_id = _add_second_merge_candidate(session_factory)
    with TestClient(app) as client:
        _login(client, "sales")
        created = _create_merge_group(client, [1, second_id])
        with session_factory() as session:
            assert session.query(SupplierRequisitionOrder).count() == 0
            assert session.query(SupplierRequisitionOrderItem).count() == 0
            assert session.get(OrderItem, 1).requisition_status == "未报料"
            assert session.get(OrderItem, second_id).requisition_status == "未报料"
        pending = client.get("/api/requisition/pending")
        updated = client.put(
            f"/api/requisition/merge-groups/{created['id']}",
            json={
                "supplier_name": "更新后的供应商",
                "report_length_mm": 1100,
                "report_width_mm": 900,
                "cutting_mode": "一开二",
                "remark": "更新合并组备注",
            },
        )
        pending_after_update = client.get("/api/requisition/pending")
        supplier_created = client.post(
            f"/api/requisition/merge-groups/{created['id']}/supplier-order"
        )
        supplier_orders = client.get("/api/requisition/supplier-orders")
        duplicate = client.post(
            f"/api/requisition/merge-groups/{created['id']}/supplier-order"
        )
        pending_after_supplier_order = client.get("/api/requisition/pending")

    assert created["status"] == "merged_pending"
    assert len(created["members"]) == 2
    assert sorted(created["product_codes"]) == ["21301028", "21301029"]
    with session_factory() as session:
        assert session.query(SupplierRequisitionOrder).count() == 1
        assert session.query(SupplierRequisitionOrderItem).count() == 2
        assert session.get(OrderItem, 1).requisition_status == "已报料"
        assert session.get(OrderItem, second_id).requisition_status == "已报料"

    assert pending.status_code == 200
    merge_rows = [row for row in pending.json()["items"] if row.get("is_merge_group")]
    assert len(merge_rows) == 1
    merge_row = merge_rows[0]
    assert merge_row["merge_group_id"] == created["id"]
    assert len(merge_row["members"]) == 2
    assert sorted(merge_row["product_codes"]) == ["21301028", "21301029"]
    assert sorted(merge_row["order_numbers"]) == [
        "PO-20260614-001",
        "PO-20260614-002",
    ]
    regular_ids = {
        row["item_id"]
        for row in pending.json()["items"]
        if not row.get("is_merge_group")
    }
    assert 1 not in regular_ids
    assert second_id not in regular_ids

    assert updated.status_code == 200, updated.text
    assert pending_after_update.status_code == 200
    updated_row = next(
        row for row in pending_after_update.json()["items"] if row.get("is_merge_group")
    )
    assert updated_row["supplier_name"] == "更新后的供应商"
    assert Decimal(str(updated_row["report_length_mm"])) == Decimal("1100")
    assert Decimal(str(updated_row["report_width_mm"])) == Decimal("900")
    assert updated_row["cutting_mode"] == "一开二"
    assert updated_row["remark"] == "更新合并组备注"

    assert supplier_created.status_code == 201, supplier_created.text
    assert supplier_created.json()["supplier_order_id"]
    assert supplier_orders.status_code == 200
    assert supplier_orders.json()["total"] == 1
    assert supplier_orders.json()["items"][0]["supplier_name"] == "更新后的供应商"
    assert duplicate.status_code == 409
    assert not [
        row for row in pending_after_supplier_order.json()["items"]
        if row.get("merge_group_id") == created["id"]
    ]


def test_pending_selection_creates_one_supplier_order_for_merge_group_and_regular_item(
    requisition_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.order import OrderItem
    from app.models.requisition import Requisition
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    app, session_factory = requisition_app
    second_id = _add_second_merge_candidate(session_factory)
    regular_id = _add_pending_candidate(session_factory, 3, product_code="21301030")
    with TestClient(app) as client:
        _login(client, "sales")
        created = _create_merge_group(client, [1, second_id])
        draft = _preview_supplier_order_draft(
            client,
            [
                {
                    "type": "merge_group",
                    "merge_group_id": created["id"],
                    "supplier_name": "苏州纸板供应商",
                    "report_length_mm": 1000,
                    "report_width_mm": 800,
                    "cutting_mode": "一开一",
                },
                {
                    "type": "order_item",
                    "order_item_id": regular_id,
                    "supplier_name": "苏州纸板供应商",
                    "report_length_mm": 1000,
                    "report_width_mm": 800,
                    "cutting_mode": "一开一",
                },
            ],
        )
        with session_factory() as session:
            assert session.query(SupplierRequisitionOrder).count() == 0
            assert session.query(SupplierRequisitionOrderItem).count() == 0
            assert session.get(Requisition, created["id"]).status == "merged_pending"
            assert session.get(OrderItem, 1).requisition_status == "未报料"
        response = _save_supplier_order_draft(client, draft)

    assert response.status_code == 201, response.text
    created_orders = response.json()["created_orders"]
    assert len(created_orders) == 1
    assert created_orders[0]["supplier_name"] == "苏州纸板供应商"
    assert created_orders[0]["item_count"] == 3
    with session_factory() as session:
        assert session.query(SupplierRequisitionOrder).count() == 1
        assert session.query(SupplierRequisitionOrderItem).count() == 3
        assert session.get(Requisition, created["id"]).status == "supplier_requisition_created"
        assert session.get(OrderItem, 1).requisition_status == "已报料"
        assert session.get(OrderItem, second_id).requisition_status == "已报料"
        assert session.get(OrderItem, regular_id).requisition_status == "已报料"
        audit_rows = session.scalars(
            select(OperationLog)
            .where(OperationLog.module_code == "requisition")
            .order_by(OperationLog.id)
        ).all()
        created_events = [
            row
            for row in audit_rows
            if row.action_code == "requisition.supplier_order.create"
        ]
        batch_events = [
            row
            for row in audit_rows
            if row.action_code
            == "requisition.supplier_orders.create_batch"
        ]
        assert len(created_events) == 1
        assert len(batch_events) == 1
        assert created_events[0].batch_id == batch_events[0].batch_id
        assert created_events[0].customer_id_snapshot == 1
        assert created_events[0].object_ref.startswith("SRO-")


def test_supplier_order_create_and_void_audit_failures_roll_back_business(
    requisition_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import requisition as requisition_api
    from app.models.audit import OperationLog
    from app.models.order import OrderItem
    from app.models.supplier_requisition_order import SupplierRequisitionOrder

    app, session_factory = requisition_app
    pending_id = _add_pending_candidate(
        session_factory,
        30,
        product_code="Q1-AUDIT-REPLAY",
    )
    with TestClient(app) as client:
        _login(client, "sales")
        draft = _preview_supplier_order_draft(
            client,
            [
                {
                    "type": "order_item",
                    "order_item_id": pending_id,
                    "supplier_name": "苏州纸板供应商",
                    "report_length_mm": 1000,
                    "report_width_mm": 800,
                    "cutting_mode": "一开一",
                }
            ],
        )

        original_append = requisition_api.append_audit_event

        def reject_audit(*args, **kwargs):
            original_append(*args, **kwargs)
            raise RuntimeError("forced requisition audit failure")

        monkeypatch.setattr(
            requisition_api,
            "append_audit_event",
            reject_audit,
        )
        client.post("/api/auth/logout")
        _login(client, "admin")
        with pytest.raises(
            RuntimeError,
            match="forced requisition audit failure",
        ):
            _save_supplier_order_draft(client, draft)

        with session_factory() as session:
            assert session.query(SupplierRequisitionOrder).count() == 0
            assert session.get(OrderItem, pending_id).requisition_status == "未报料"

        monkeypatch.undo()
        created = _save_supplier_order_draft(client, draft)
        assert created.status_code == 201, created.text
        supplier_order_id = created.json()["created_orders"][0][
            "supplier_order_id"
        ]

        monkeypatch.setattr(
            requisition_api,
            "append_audit_event",
            reject_audit,
        )
        with pytest.raises(
            RuntimeError,
            match="forced requisition audit failure",
        ):
            client.put(
                f"/api/requisition/supplier-orders/{supplier_order_id}/void"
            )

    with session_factory() as session:
        supplier_order = session.get(
            SupplierRequisitionOrder,
            supplier_order_id,
        )
        assert supplier_order.status != "voided"
        assert session.get(OrderItem, pending_id).requisition_status == "已报料"
        assert session.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action_code
                == "requisition.supplier_order.void"
            )
        ) == 0


def test_supplier_order_void_writes_structured_audit(
    requisition_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.order import OrderItem

    app, session_factory = requisition_app
    pending_id = _add_pending_candidate(
        session_factory,
        31,
        product_code="Q1-AUDIT-VOID",
    )
    with TestClient(app) as client:
        _login(client, "sales")
        draft = _preview_supplier_order_draft(
            client,
            [
                {
                    "type": "order_item",
                    "order_item_id": pending_id,
                    "supplier_name": "苏州纸板供应商",
                    "report_length_mm": 1000,
                    "report_width_mm": 800,
                    "cutting_mode": "一开一",
                }
            ],
        )
        created = _save_supplier_order_draft(client, draft)
        supplier_order_id = created.json()["created_orders"][0][
            "supplier_order_id"
        ]
        denied = client.put(
            f"/api/requisition/supplier-orders/{supplier_order_id}/void"
        )
        assert denied.status_code == 403
        client.post("/api/auth/logout")
        _login(client, "admin")
        response = client.put(
            f"/api/requisition/supplier-orders/{supplier_order_id}/void"
        )

    assert response.status_code == 200, response.text
    with session_factory() as session:
        log = session.scalar(
            select(OperationLog).where(
                OperationLog.action_code
                == "requisition.supplier_order.void"
            )
        )
        assert log is not None
        assert log.action == "VOID_SUPPLIER_ORDER"
        assert log.event_category == "business"
        assert log.result == "success"
        assert log.customer_id_snapshot == 1
        assert log.object_ref.startswith("SRO-")
        assert session.get(OrderItem, pending_id).requisition_status == "未报料"


def test_supplier_order_void_requires_receipt_reversal_first(
    requisition_app,
) -> None:
    from app.models.supplier_requisition_order import SupplierRequisitionOrder

    app, session_factory = requisition_app
    pending_id = _add_pending_candidate(
        session_factory,
        32,
        product_code="Q1-ROLLBACK-BLOCK",
        quantity=80,
    )
    with TestClient(app) as client:
        _login(client, "sales")
        draft = _preview_supplier_order_draft(
            client,
            [
                {
                    "type": "order_item",
                    "order_item_id": pending_id,
                    "supplier_name": "苏州纸板供应商",
                    "report_length_mm": 1000,
                    "report_width_mm": 800,
                    "cutting_mode": "一开一",
                }
            ],
        )
        created = _save_supplier_order_draft(client, draft)
        assert created.status_code == 201, created.text
        supplier_order_id = created.json()["created_orders"][0][
            "supplier_order_id"
        ]
        client.post("/api/auth/logout")
        _login(client, "admin")
        received = client.put(
            f"/api/incoming/receive/{pending_id}",
            json={"received_quantity": 80},
        )
        blocked = client.put(
            f"/api/requisition/supplier-orders/{supplier_order_id}/void"
        )

    assert received.status_code == 200, received.text
    assert blocked.status_code == 409
    assert "先撤销来料实收" in blocked.text
    with session_factory() as session:
        assert (
            session.get(SupplierRequisitionOrder, supplier_order_id).status
            != "voided"
        )


def test_pending_selection_aggregates_supplier_draft_by_purchase_spec(
    requisition_app,
) -> None:
    from app.models.supplier_requisition_order import SupplierRequisitionOrder

    app, session_factory = requisition_app
    supplier = "昆山鸣明"
    item_f616a = _add_pending_candidate(
        session_factory,
        10,
        product_code="21301051",
        product_name="中性外箱51*41",
        quantity=50,
        supplier_name=supplier,
        material_code="F616A",
        layer_count=5,
        flute_type="AB",
    )
    item_k616a = _add_pending_candidate(
        session_factory,
        11,
        product_code="20600037",
        product_name="手套外箱48*45*47",
        quantity=1,
        supplier_name=supplier,
        material_code="K616A",
        layer_count=5,
        flute_type="AB",
    )
    item_n7n_a = _add_pending_candidate(
        session_factory,
        12,
        product_code="21301464",
        product_name="A356专用包装箱25*45WW",
        quantity=22,
        supplier_name=supplier,
        material_code="N7N",
        layer_count=3,
        flute_type="B",
    )
    item_n7n_b = _add_pending_candidate(
        session_factory,
        13,
        product_code="21301435",
        product_name="A356专用包装箱25*45BB",
        quantity=10,
        supplier_name=supplier,
        material_code="N7N",
        layer_count=3,
        flute_type="B",
    )

    selections = [
        {
            "type": "order_item",
            "order_item_id": item_f616a,
            "supplier_name": supplier,
            "report_length_mm": 1976,
            "report_width_mm": 594,
            "cutting_mode": "一开一",
        },
        {
            "type": "order_item",
            "order_item_id": item_k616a,
            "supplier_name": supplier,
            "report_length_mm": 1876,
            "report_width_mm": 924,
            "cutting_mode": "一开一",
        },
        {
            "type": "order_item",
            "order_item_id": item_n7n_a,
            "supplier_name": supplier,
            "report_length_mm": 1345,
            "report_width_mm": 1300,
            "cutting_mode": "一开一",
        },
        {
            "type": "order_item",
            "order_item_id": item_n7n_b,
            "supplier_name": supplier,
            "report_length_mm": 1345,
            "report_width_mm": 1300,
            "cutting_mode": "一开一",
        },
    ]

    with TestClient(app) as client:
        _login(client, "sales")
        draft = _preview_supplier_order_draft(client, selections)
        with session_factory() as session:
            assert session.query(SupplierRequisitionOrder).count() == 0
        group = draft["supplier_groups"][0]
        lines = group["lines"]

        assert len(draft["supplier_groups"]) == 1
        assert len(lines) == 3
        line_by_spec = {
            (line["material_code"], line["flute_type"], line["report_length_mm"], line["report_width_mm"]): line
            for line in lines
        }
        assert line_by_spec[("F616A", "AB", 1976, 594)]["requisition_qty"] == 50
        assert line_by_spec[("K616A", "AB", 1876, 924)]["requisition_qty"] == 1
        n7n_line = line_by_spec[("N7N", "B", 1345, 1300)]
        assert n7n_line["requisition_qty"] == 32
        assert {source["product_code"] for source in n7n_line["source_items"]} == {
            "21301464",
            "21301435",
        }

        saved = _save_supplier_order_draft(client, draft)
        assert saved.status_code == 201, saved.text
        created = saved.json()["created_orders"]
        assert len(created) == 1
        detail = client.get(f"/api/requisition/supplier-orders/{created[0]['supplier_order_id']}")
        assert detail.status_code == 200, detail.text
        saved_lines = detail.json()["items"]

    assert len(saved_lines) == 3
    saved_by_spec = {
        (line["material_code"], line["flute_type"], line["report_length_mm"], line["report_width_mm"]): line
        for line in saved_lines
    }
    assert saved_by_spec[("F616A", "AB", 1976, 594)]["requisition_qty"] == 50
    assert saved_by_spec[("K616A", "AB", 1876, 924)]["requisition_qty"] == 1
    assert saved_by_spec[("N7N", "B", 1345, 1300)]["requisition_qty"] == 32
    assert not any(
        line["material_code"] == "N7N"
        and line["report_length_mm"] == 1345
        and line["report_width_mm"] == 1300
        and line["requisition_qty"] == 83
        for line in saved_lines
    )


def test_pending_selection_groups_multiple_suppliers_separately(
    requisition_app,
) -> None:
    from app.models.supplier_requisition_order import SupplierRequisitionOrder

    app, session_factory = requisition_app
    second_id = _add_second_merge_candidate(session_factory)
    supplier_a_item_id = _add_pending_candidate(session_factory, 3, product_code="21301030")
    supplier_b_item_id = _add_pending_candidate(
        session_factory,
        4,
        product_code="21301031",
        supplier_name="昆山鸣明",
    )
    with TestClient(app) as client:
        _login(client, "sales")
        created = _create_merge_group(client, [1, second_id])
        draft = _preview_supplier_order_draft(
            client,
            [
                {
                    "type": "merge_group",
                    "merge_group_id": created["id"],
                    "supplier_name": "苏州纸板供应商",
                    "report_length_mm": 1000,
                    "report_width_mm": 800,
                    "cutting_mode": "一开一",
                },
                {
                    "type": "order_item",
                    "order_item_id": supplier_a_item_id,
                    "supplier_name": "苏州纸板供应商",
                    "report_length_mm": 1000,
                    "report_width_mm": 800,
                    "cutting_mode": "一开一",
                },
                {
                    "type": "order_item",
                    "order_item_id": supplier_b_item_id,
                    "supplier_name": "昆山鸣明",
                    "report_length_mm": 1200,
                    "report_width_mm": 900,
                    "cutting_mode": "一开二",
                },
            ],
        )
        response = _save_supplier_order_draft(client, draft)

    assert response.status_code == 201, response.text
    created_orders = response.json()["created_orders"]
    assert {row["supplier_name"] for row in created_orders} == {"苏州纸板供应商", "昆山鸣明"}
    assert {row["supplier_name"]: row["item_count"] for row in created_orders} == {
        "苏州纸板供应商": 3,
        "昆山鸣明": 1,
    }
    with session_factory() as session:
        assert session.query(SupplierRequisitionOrder).count() == 2


def test_pending_selection_rejects_missing_supplier_and_duplicate_generation(
    requisition_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.order import OrderItem
    from app.models.supplier_requisition_order import SupplierRequisitionOrder

    app, session_factory = requisition_app
    regular_id = _add_pending_candidate(
        session_factory,
        3,
        product_code="21301030",
        supplier_name=None,
    )
    with TestClient(app) as client:
        _login(client, "sales")
        draft_without_supplier = _preview_supplier_order_draft(
            client,
            [
                {
                    "type": "order_item",
                    "order_item_id": regular_id,
                    "report_length_mm": 1000,
                    "report_width_mm": 800,
                    "cutting_mode": "一开一",
                }
            ],
        )
        missing_supplier = _save_supplier_order_draft(client, draft_without_supplier)
        draft = _preview_supplier_order_draft(
            client,
            [
                {
                    "type": "order_item",
                    "order_item_id": regular_id,
                    "supplier_name": "苏州纸板供应商",
                    "report_length_mm": 1000,
                    "report_width_mm": 800,
                    "cutting_mode": "一开一",
                }
            ],
        )
        created = _save_supplier_order_draft(client, draft)
        duplicate = _save_supplier_order_draft(client, draft)
        fresh_duplicate = client.post(
            "/api/requisition/supplier-orders/preview-from-pending-selection",
            json={
                "selections": [
                    {
                        "type": "order_item",
                        "order_item_id": regular_id,
                        "supplier_name": "苏州纸板供应商",
                        "report_length_mm": 1000,
                        "report_width_mm": 800,
                        "cutting_mode": "一开一",
                    }
                ]
            },
        )

    assert missing_supplier.status_code == 400
    assert created.status_code == 201, created.text
    assert duplicate.status_code == 201
    assert duplicate.json()["idempotent_replay"] is True
    assert fresh_duplicate.status_code == 409
    assert "已有报料" in fresh_duplicate.json()["detail"]
    with session_factory() as session:
        assert session.query(SupplierRequisitionOrder).count() == 1
        assert session.get(OrderItem, regular_id).requisition_status == "已报料"
        assert session.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action_code
                == "requisition.supplier_order.create"
            )
        ) == 1
        assert session.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action_code
                == "requisition.supplier_orders.create_batch"
            )
        ) == 1


def test_pending_selection_rejects_fabricated_deduction_and_invalid_requisition_qty(
    requisition_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.supplier_requisition_order import SupplierRequisitionOrder

    app, session_factory = requisition_app
    with TestClient(app) as client:
        _login(client, "sales")
        draft = _preview_supplier_order_draft(
            client,
            [
                {
                    "type": "order_item",
                    "order_item_id": 1,
                    "supplier_name": "苏州纸板供应商",
                    "report_length_mm": 1000,
                    "report_width_mm": 800,
                    "cutting_mode": "一开一",
                }
            ],
        )
        negative_deduction = deepcopy(draft)
        negative_deduction["supplier_groups"][0]["lines"][0]["inventory_deducted_qty"] = -1
        fabricated_deduction = deepcopy(draft)
        fabricated_deduction["supplier_groups"][0]["lines"][0]["inventory_deducted_qty"] = 1
        fabricated_source = deepcopy(draft)
        fabricated_source["supplier_groups"][0]["lines"][0]["source_items"][0][
            "inventory_deducted_qty"
        ] = 1
        zero_requisition_qty = deepcopy(draft)
        zero_requisition_qty["supplier_groups"][0]["lines"][0]["requisition_qty"] = 0

        negative = _save_supplier_order_draft(client, negative_deduction)
        fabricated = _save_supplier_order_draft(client, fabricated_deduction)
        fabricated_source_response = _save_supplier_order_draft(
            client, fabricated_source
        )
        zero_qty = _save_supplier_order_draft(client, zero_requisition_qty)

    assert negative.status_code == 400
    assert fabricated.status_code == 400
    assert "真实库存预占" in fabricated.json()["detail"]
    assert fabricated_source_response.status_code == 400
    assert "真实库存预占" in fabricated_source_response.json()["detail"]
    assert zero_qty.status_code == 400
    with session_factory() as session:
        assert session.query(SupplierRequisitionOrder).count() == 0
        assert session.get(OrderItem, 1).requisition_status == "未报料"


def test_supplier_draft_partial_quantity_stays_pending_and_retry_is_idempotent(
    requisition_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.supplier_requisition_order import SupplierRequisitionOrder

    app, session_factory = requisition_app
    with TestClient(app) as client:
        _login(client, "sales")
        selection = {
            "type": "order_item",
            "order_item_id": 1,
            "supplier_name": "苏州纸板供应商",
            "report_length_mm": 1000,
            "report_width_mm": 800,
            "cutting_mode": "一开一",
        }
        draft = _preview_supplier_order_draft(client, [selection])
        line = draft["supplier_groups"][0]["lines"][0]
        assert line["theoretical_requisition_qty"] == 100
        assert line["already_requisitioned_qty"] == 0
        assert line["remaining_requisition_qty"] == 100
        line["requisition_qty"] = 40

        first = _save_supplier_order_draft(client, draft)
        retry = _save_supplier_order_draft(client, draft)
        pending = client.get("/api/requisition/pending")
        second_draft = _preview_supplier_order_draft(client, [selection])
        second_line = second_draft["supplier_groups"][0]["lines"][0]
        second = _save_supplier_order_draft(client, second_draft)
        pending_after = client.get("/api/requisition/pending")

    assert first.status_code == 201, first.text
    assert retry.status_code == 201, retry.text
    assert retry.json()["idempotent_replay"] is True
    assert second_line["already_requisitioned_qty"] == 40
    assert second_line["remaining_requisition_qty"] == 60
    assert second_line["requisition_qty"] == 60
    assert second.status_code == 201, second.text
    pending_row = next(row for row in pending.json()["items"] if row["item_id"] == 1)
    assert pending_row["already_requisitioned_qty"] == 40
    assert pending_row["remaining_requisition_qty"] == 60
    assert not [row for row in pending_after.json()["items"] if row.get("item_id") == 1]
    first_order_id = first.json()["created_orders"][0]["supplier_order_id"]
    with TestClient(app) as admin_client:
        _login(admin_client, "admin")
        voided = admin_client.put(
            f"/api/requisition/supplier-orders/{first_order_id}/void"
        )
        pending_after_void = admin_client.get("/api/requisition/pending")
    assert voided.status_code == 200, voided.text
    reopened = next(
        row for row in pending_after_void.json()["items"] if row.get("item_id") == 1
    )
    assert reopened["already_requisitioned_qty"] == 60
    assert reopened["remaining_requisition_qty"] == 40
    with session_factory() as session:
        assert session.query(SupplierRequisitionOrder).count() == 2
        assert session.get(OrderItem, 1).requisition_qty == 60
        assert session.get(OrderItem, 1).requisition_status == "已报料"


def test_pending_reconciles_effective_legacy_material_requisition_facts(
    requisition_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.requisition import Requisition, RequisitionItem

    app, session_factory = requisition_app
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        legacy_batch = Requisition(
            requisition_number="BL-LEGACY-PENDING-001",
            requisition_date=date(2026, 7, 8),
            supplier_name="苏州纸板供应商",
            status="已报料",
        )
        session.add(legacy_batch)
        session.flush()
        legacy_item = RequisitionItem(
            requisition_id=legacy_batch.id,
            order_item_id=item.id,
            inventory_deducted_qty=0,
            requisition_qty=100,
            cardboard_len=Decimal("1000"),
            cardboard_width=Decimal("800"),
            pieces_per_box=1,
            required_piece_qty=100,
            special_process="一开一",
            material_snapshot=item.snapshot_material,
            product_code_snapshot=item.snapshot_product_code,
            product_name_snapshot=item.snapshot_product_name,
            specification_snapshot=item.snapshot_spec,
            status="已入库",
        )
        session.add(legacy_item)
        item.requisition_status = "已报料"
        item.requisition_qty = 100
        session.commit()

    with TestClient(app) as client:
        _login(client, "sales")
        pending_with_effective_fact = client.get("/api/requisition/pending")
        assert pending_with_effective_fact.status_code == 200
        assert not [
            row
            for row in pending_with_effective_fact.json()["items"]
            if row.get("item_id") == 1
        ]
        duplicate_preview = client.post(
            "/api/requisition/supplier-orders/preview-from-pending-selection",
            json={
                "selections": [
                    {
                        "type": "order_item",
                        "order_item_id": 1,
                        "supplier_name": "苏州纸板供应商",
                        "report_length_mm": 1000,
                        "report_width_mm": 800,
                        "cutting_mode": "一开一",
                    }
                ]
            },
        )
        assert duplicate_preview.status_code == 409
        assert "BL-LEGACY-PENDING-001" in duplicate_preview.json()["detail"]

        with session_factory() as session:
            session.get(Requisition, legacy_batch.id).status = "已取消"
            session.get(RequisitionItem, legacy_item.id).status = "已取消"
            session.commit()

        pending_after_cancel = client.get("/api/requisition/pending")

    reopened = next(
        row
        for row in pending_after_cancel.json()["items"]
        if row.get("item_id") == 1
    )
    assert reopened["already_requisitioned_qty"] == 0
    assert reopened["remaining_requisition_qty"] == 100


def test_pending_hides_fully_reported_legacy_telescoping_lid_components(
    requisition_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.requisition import Requisition, RequisitionItem

    app, session_factory = requisition_app
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        product = session.get(Product, item.product_id)
        product.box_style = "A3 天地盖"
        item.quantity = 30
        item.snapshot_product_name = "思迈尔天地盖"
        item.snapshot_report_length_mm = 400
        item.snapshot_report_width_mm = 300
        item.snapshot_base_report_length_mm = 375
        item.snapshot_base_report_width_mm = 275
        item.requisition_status = "已报料"
        item.requisition_qty = 60
        legacy_batch = Requisition(
            requisition_number="BL-20260708-006",
            requisition_date=date(2026, 7, 8),
            supplier_name="苏州纸板供应商",
            status="已报料",
        )
        session.add(legacy_batch)
        session.flush()
        session.add_all(
            [
                RequisitionItem(
                    requisition_id=legacy_batch.id,
                    order_item_id=item.id,
                    inventory_deducted_qty=0,
                    requisition_qty=30,
                    cardboard_len=Decimal("400"),
                    cardboard_width=Decimal("300"),
                    pieces_per_box=1,
                    required_piece_qty=30,
                    special_process="一开一",
                    material_snapshot=item.snapshot_material,
                    product_code_snapshot=item.snapshot_product_code,
                    product_name_snapshot="思迈尔天地盖-盖",
                    specification_snapshot=item.snapshot_spec,
                    status="已入库",
                ),
                RequisitionItem(
                    requisition_id=legacy_batch.id,
                    order_item_id=item.id,
                    inventory_deducted_qty=0,
                    requisition_qty=30,
                    cardboard_len=Decimal("375"),
                    cardboard_width=Decimal("275"),
                    pieces_per_box=1,
                    required_piece_qty=30,
                    special_process="一开一",
                    material_snapshot=item.snapshot_material,
                    product_code_snapshot=item.snapshot_product_code,
                    product_name_snapshot="思迈尔天地盖-底",
                    specification_snapshot=item.snapshot_spec,
                    status="已入库",
                ),
            ]
        )
        session.commit()

    with TestClient(app) as client:
        _login(client, "sales")
        pending = client.get("/api/requisition/pending")

    assert pending.status_code == 200
    assert not [row for row in pending.json()["items"] if row.get("item_id") == 1]


def test_supplier_draft_blocks_swapped_dimensions_and_controls_overage(
    requisition_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.order import OrderItem
    from app.models.supplier_requisition_order import SupplierRequisitionOrder

    app, session_factory = requisition_app
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        item.snapshot_report_length_mm = 1000
        item.snapshot_report_width_mm = 800
        session.commit()

    selection = {
        "type": "order_item",
        "order_item_id": 1,
        "supplier_name": "苏州纸板供应商",
        "report_length_mm": 1000,
        "report_width_mm": 800,
        "cutting_mode": "一开一",
    }
    with TestClient(app) as sales_client:
        _login(sales_client, "sales")
        swapped_draft = _preview_supplier_order_draft(sales_client, [selection])
        swapped_line = swapped_draft["supplier_groups"][0]["lines"][0]
        swapped_line["report_length_mm"] = 800
        swapped_line["report_width_mm"] = 1000
        swapped_line["dimension_override_acknowledged"] = True
        sales_swapped = _save_supplier_order_draft(sales_client, swapped_draft)

        over_draft = _preview_supplier_order_draft(sales_client, [selection])
        over_line = over_draft["supplier_groups"][0]["lines"][0]
        over_line["requisition_qty"] = over_line["remaining_requisition_qty"] + 1
        over_line["quantity_override_acknowledged"] = True
        sales_over = _save_supplier_order_draft(sales_client, over_draft)

    assert sales_swapped.status_code == 403
    assert "长宽颠倒" in sales_swapped.json()["detail"]
    assert sales_over.status_code == 403
    assert "只有管理员" in sales_over.json()["detail"]

    with TestClient(app) as admin_client:
        _login(admin_client, "admin")
        admin_draft = _preview_supplier_order_draft(admin_client, [selection])
        admin_line = admin_draft["supplier_groups"][0]["lines"][0]
        admin_line["report_length_mm"] = 800
        admin_line["report_width_mm"] = 1000
        admin_line["requisition_qty"] = admin_line["remaining_requisition_qty"] + 5
        missing_ack = _save_supplier_order_draft(admin_client, admin_draft)
        admin_line["quantity_override_acknowledged"] = True
        missing_dimension_ack = _save_supplier_order_draft(admin_client, admin_draft)
        admin_line["dimension_override_acknowledged"] = True
        accepted = _save_supplier_order_draft(admin_client, admin_draft)

    assert missing_ack.status_code == 409
    assert "确认超量报料" in missing_ack.json()["detail"]
    assert missing_dimension_ack.status_code == 409
    assert "按人工尺寸继续" in missing_dimension_ack.json()["detail"]
    assert accepted.status_code == 201, accepted.text
    with session_factory() as session:
        order = session.query(SupplierRequisitionOrder).one()
        assert order.requisition_qty == 105
        assert order.request_key
        event = session.scalar(
            select(OperationLog).where(
                OperationLog.action_code == "requisition.supplier_order.create"
            )
        )
        assert event is not None
        assert '"dimension_override": true' in event.details
        assert '"quantity_override": true' in event.details


def test_reported_documents_unifies_supplier_orders_and_legacy_requisitions(
    requisition_app,
) -> None:
    from app.models.requisition import Requisition
    from app.models.supplier_requisition_order import SupplierRequisitionOrder

    app, session_factory = requisition_app
    second_id = _add_second_merge_candidate(session_factory)
    regular_id = _add_pending_candidate(session_factory, 3, product_code="21301030")
    with TestClient(app) as client:
        _login(client, "sales")
        created = _create_merge_group(client, [1, second_id])
        draft = _preview_supplier_order_draft(
            client,
            [
                {
                    "type": "merge_group",
                    "merge_group_id": created["id"],
                    "supplier_name": "苏州纸板供应商",
                    "report_length_mm": 1000,
                    "report_width_mm": 800,
                    "cutting_mode": "一开一",
                },
                {
                    "type": "order_item",
                    "order_item_id": regular_id,
                    "supplier_name": "苏州纸板供应商",
                    "report_length_mm": 1000,
                    "report_width_mm": 800,
                    "cutting_mode": "一开一",
                },
            ],
        )
        saved = _save_supplier_order_draft(client, draft)
        with session_factory() as session:
            legacy = Requisition(
                requisition_number="BL-LEGACY-001",
                requisition_date=date(2026, 6, 20),
                supplier_name="旧供应商",
                status="已报料",
            )
            session.add(legacy)
            session.commit()
        documents = client.get("/api/requisition/reported-documents")

    assert saved.status_code == 201, saved.text
    assert documents.status_code == 200, documents.text
    rows = documents.json()["items"]
    source_types = {row["source_type"] for row in rows}
    assert "supplier_order" in source_types
    assert "legacy_material_requisition" in source_types
    assert all(row["document_number"] for row in rows)
    assert all("pdf_url" in row for row in rows)
    assert not any(
        row["source_type"] == "legacy_material_requisition"
        and row["id"] == created["id"]
        for row in rows
    )
    with session_factory() as session:
        assert session.query(SupplierRequisitionOrder).count() == 1


def test_merged_pending_group_is_not_formal_requisition_flow(
    requisition_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    app, session_factory = requisition_app
    second_id = _add_second_merge_candidate(session_factory)
    with TestClient(app) as client:
        _login(client, "sales")
        created = _create_merge_group(client, [1, second_id])
        printed = client.get(f"/api/requisition/batches/{created['id']}/print")
        submitted = client.get("/api/requisition/items")
        client.post("/api/auth/logout")
        _login(client, "workshop")
        incoming = client.get("/api/incoming/pending")

    assert printed.status_code == 409
    assert "不是正式报料单" in printed.json()["detail"]
    assert submitted.status_code == 200
    assert submitted.json()["items"] == []
    assert incoming.status_code == 200
    assert incoming.json()["items"] == []
    with session_factory() as session:
        assert session.query(SupplierRequisitionOrder).count() == 0
        assert session.query(SupplierRequisitionOrderItem).count() == 0
        assert session.get(OrderItem, 1).requisition_status == "未报料"
        assert session.get(OrderItem, second_id).requisition_status == "未报料"


def test_pending_supplier_counts_and_material_change(requisition_app) -> None:
    from app.models.material import Material
    from app.models.order import Order, OrderItem
    from app.models.product import Product

    app, session_factory = requisition_app
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        product = session.get(Product, item.product_id)
        supplier_a = Material(
            code="A6A",
            layer_count=3,
            flute_type="E",
            supplier_name="嘉林亿",
        )
        supplier_b = Material(
            code="CCC-B",
            layer_count=3,
            flute_type="E",
            supplier_name="鸣朋",
        )
        supplier_a_alt = Material(
            code="BC14C",
            layer_count=5,
            flute_type="AB",
            supplier_name="嘉林亿",
        )
        session.add_all([supplier_a, supplier_b, supplier_a_alt])
        session.flush()
        item.material_id = supplier_a.id
        item.snapshot_material = supplier_a.code
        item.snapshot_supplier_name = supplier_a.supplier_name
        item.layer_count = 3
        item.flute_type = "E"
        order = session.get(Order, item.order_id)
        for index in range(9):
            session.add(
                OrderItem(
                    order_id=order.id,
                    product_id=product.id,
                    quantity=10,
                    unit_price=Decimal("1"),
                    subtotal=Decimal("10"),
                    material_status="pending",
                    requisition_status="未报料",
                    snapshot_product_name=f"嘉林亿产品{index}",
                    snapshot_material=supplier_a.code,
                    snapshot_supplier_name=supplier_a.supplier_name,
                    material_id=supplier_a.id,
                    layer_count=3,
                    flute_type="E",
                )
            )
        supplier_b_item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=10,
            unit_price=Decimal("1"),
            subtotal=Decimal("10"),
            material_status="pending",
            requisition_status="未报料",
            snapshot_product_name="鸣朋产品",
            snapshot_material=supplier_b.code,
            snapshot_supplier_name=supplier_b.supplier_name,
            material_id=supplier_b.id,
            layer_count=3,
            flute_type="E",
        )
        session.add(supplier_b_item)
        session.commit()
        supplier_a_id = supplier_a.id
        supplier_a_alt_id = supplier_a_alt.id
        supplier_b_item_id = supplier_b_item.id
        product_id = product.id
        product_version = product.version

    with TestClient(app) as client:
        _login(client, "sales")
        pending = client.get("/api/requisition/pending")
        assert pending.status_code == 200
        counts = {
            row["supplier_name"]: row["count"]
            for row in pending.json()["supplier_counts"]
        }
        assert counts == {"嘉林亿": 10, "鸣朋": 1}
        change_payload = {
            "material_id": supplier_a_id,
            "layer_count": 3,
            "flute_type": "E",
            "sync_product": True,
            "product_expected_version": product_version,
            "product_change_reason": "报料材质调整同步常用箱",
        }
        confirmation = client.put(
            f"/api/requisition/pending/{supplier_b_item_id}/material",
            json=change_payload,
        )
        assert confirmation.status_code == 409, confirmation.text
        assert (
            confirmation.json()["detail"]["code"]
            == "MASTER_CHANGE_CONFIRMATION_REQUIRED"
        )
        change_payload["product_confirmation_token"] = confirmation.json()["detail"][
            "confirmation_token"
        ]
        changed = client.put(
            f"/api/requisition/pending/{supplier_b_item_id}/material",
            json=change_payload,
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["supplier_name"] == "嘉林亿"
        assert changed.json()["material_code"] == "A6A"
        pending_after_supplier_change = client.get("/api/requisition/pending")
        assert pending_after_supplier_change.status_code == 200
        counts_after_supplier_change = {
            row["supplier_name"]: row["count"]
            for row in pending_after_supplier_change.json()["supplier_counts"]
        }
        assert counts_after_supplier_change == {"嘉林亿": 11}
        changed_row = next(
            row
            for row in pending_after_supplier_change.json()["items"]
            if row["item_id"] == supplier_b_item_id
        )
        assert changed_row["requisition_status"] == "未报料"
        assert changed_row["material_display"] == "A6A / E"

        with session_factory() as session:
            product_version = session.get(Product, product_id).version
        same_supplier_payload = {
            "material_id": supplier_a_alt_id,
            "layer_count": 5,
            "flute_type": "AB",
            "sync_product": True,
            "product_expected_version": product_version,
            "product_change_reason": "报料材质调整同步常用箱",
        }
        confirmation = client.put(
            f"/api/requisition/pending/{supplier_b_item_id}/material",
            json=same_supplier_payload,
        )
        assert confirmation.status_code == 409, confirmation.text
        assert (
            confirmation.json()["detail"]["code"]
            == "MASTER_CHANGE_CONFIRMATION_REQUIRED"
        )
        same_supplier_payload["product_confirmation_token"] = confirmation.json()[
            "detail"
        ]["confirmation_token"]
        same_supplier_change = client.put(
            f"/api/requisition/pending/{supplier_b_item_id}/material",
            json=same_supplier_payload,
        )
        assert same_supplier_change.status_code == 200, same_supplier_change.text
        assert same_supplier_change.json()["supplier_name"] == "嘉林亿"
        assert same_supplier_change.json()["material_code"] == "BC14C"
        pending_after_material_change = client.get("/api/requisition/pending")
        assert pending_after_material_change.status_code == 200
        same_supplier_row = next(
            row
            for row in pending_after_material_change.json()["items"]
            if row["item_id"] == supplier_b_item_id
        )
        assert same_supplier_row["snapshot_supplier_name"] == "嘉林亿"
        assert same_supplier_row["material_display"] == "BC14C / AB"
        assert {
            row["supplier_name"]: row["count"]
            for row in pending_after_material_change.json()["supplier_counts"]
        } == {"嘉林亿": 11}

    with session_factory() as session:
        changed_item = session.get(OrderItem, supplier_b_item_id)
        changed_product = session.get(Product, changed_item.product_id)
        assert changed_item.snapshot_supplier_name == "嘉林亿"
        assert changed_item.snapshot_material == "BC14C"
        assert changed_item.flute_type == "AB"
        assert changed_item.requisition_status == "未报料"
        assert changed_item.material_status == "pending"
        assert changed_item.material_id == supplier_a_alt_id
        assert changed_product.material_id == supplier_a_alt_id


def test_pending_requisition_sorts_newest_record_first(requisition_app) -> None:
    from app.models.order import OrderItem

    app, session_factory = requisition_app
    with session_factory() as session:
        first = session.get(OrderItem, 1)
        first.created_at = datetime(2026, 6, 1, 8, 0, 0)
        newest = OrderItem(
            order_id=first.order_id,
            product_id=first.product_id,
            quantity=10,
            unit_price=Decimal("1"),
            subtotal=Decimal("10"),
            material_status="pending",
            requisition_status="未报料",
            snapshot_product_name="最近新增报料明细",
            snapshot_material=first.snapshot_material,
            created_at=datetime(2026, 6, 30, 8, 0, 0),
        )
        session.add(newest)
        session.commit()
        newest_id = newest.id

    with TestClient(app) as client:
        _login(client, "sales")
        response = client.get("/api/requisition/pending")

    assert response.status_code == 200
    assert response.json()["items"][0]["item_id"] == newest_id


def test_dead_or_force_closed_order_items_are_excluded_from_pending_requisition(
    requisition_app,
) -> None:
    from app.models.order import Order, OrderItem

    app, session_factory = requisition_app
    with session_factory() as session:
        order = session.get(Order, 1)
        item = session.get(OrderItem, 1)
        order.status = "dead"
        item.is_force_closed = True
        item.snapshot_report_length_mm = 800
        item.snapshot_report_width_mm = 300
        session.commit()

    with TestClient(app) as client:
        _login(client, "sales")
        pending = client.get("/api/requisition/pending")
        suggestions = client.get("/api/requisition/merge-suggestions")

    assert pending.status_code == 200
    assert pending.json()["items"] == []
    assert suggestions.status_code == 200
    assert suggestions.json()["suggestions"] == []


def test_merge_suggestions_never_merge_different_flute_types(requisition_app) -> None:
    from app.models.material import Material
    from app.models.order import OrderItem

    app, session_factory = requisition_app
    with session_factory() as session:
        source = session.get(OrderItem, 1)
        material = Material(
            code="A416D",
            layer_count=5,
            supplier_name="苏州嘉林亿",
            is_active=True,
        )
        session.add(material)
        session.flush()
        common = {
            "order_id": source.order_id,
            "product_id": source.product_id,
            "quantity": 10,
            "unit_price": Decimal("1"),
            "subtotal": Decimal("10"),
            "material_status": "pending",
            "requisition_status": "未报料",
            "snapshot_material": "A416D",
            "snapshot_supplier_name": "苏州嘉林亿",
            "material_id": material.id,
            "layer_count": 5,
            "snapshot_report_length_mm": 800,
            "snapshot_report_width_mm": 300,
            "snapshot_splice_mode": "single",
            "snapshot_pieces_per_box": 1,
        }
        session.add_all(
            [
                OrderItem(
                    **common,
                    snapshot_product_name="AB楞产品1",
                    flute_type="AB",
                ),
                OrderItem(
                    **common,
                    snapshot_product_name="AB楞产品2",
                    flute_type="AB",
                ),
                OrderItem(
                    **common,
                    snapshot_product_name="BE楞产品",
                    flute_type="BE",
                ),
            ]
        )
        session.commit()

    with TestClient(app) as client:
        _login(client, "sales")
        response = client.get("/api/requisition/merge-suggestions")

    assert response.status_code == 200
    matching = [
        row for row in response.json()["suggestions"]
        if row["material_display"].startswith("A416D")
    ]
    assert len(matching) == 1
    assert matching[0]["flute_type"] == "AB"
    assert matching[0]["member_count"] == 2
    assert all(member["flute_type"] == "AB" for member in matching[0]["members"])



def test_mobile_incoming_exposes_latest_pdf_drawing(requisition_app) -> None:
    from app.models.order import OrderItem
    from app.models.product_drawing import ProductDrawing

    app, session_factory = requisition_app
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        item.requisition_status = "已报料"
        item.requisition_qty = 27
        item.requisition_date = date(2026, 6, 22)
        item.cardboard_len = Decimal("1756")
        item.cardboard_width = Decimal("1962")
        drawing = ProductDrawing(
            product_id=item.product_id,
            image_path="/static/uploads/drawings/customer-order.pdf",
            thumbnail_path="/static/uploads/drawings/customer-order.pdf",
        )
        session.add(drawing)
        session.flush()
        drawing_id = drawing.id
        session.commit()

    with TestClient(app) as client:
        _login(client, "workshop")
        response = client.get("/api/incoming/pending")

    assert response.status_code == 200
    row = response.json()["items"][0]
    assert row["drawing_is_pdf"] is True
    assert row["drawing_path"] == (
        f"/api/master/products/drawings/{drawing_id}/content/original.pdf"
    )
    assert row["product_code"] == "21301028"
    assert row["incoming_quantity"] == row["requisition_qty"]
    assert row["requisition_date"] is not None


def test_incoming_quantity_can_be_changed_when_receiving(requisition_app) -> None:
    from app.models.order import OrderItem
    from app.models.requisition import RequisitionItem

    app, session_factory = requisition_app
    with TestClient(app) as client:
        _login(client, "sales")
        batch = client.post("/api/requisition/batches", json=_batch_payload())
        assert batch.status_code == 201, batch.text
        with session_factory() as session:
            original_order_qty = session.get(OrderItem, 1).requisition_qty
            original_requisition_qty = session.query(RequisitionItem).filter_by(
                order_item_id=1,
                status="有效",
            ).one().requisition_qty
        client.post("/api/auth/logout")
        _login(client, "workshop")
        received = client.put(
            "/api/incoming/receive/1",
            json={
                "received_quantity": 92,
                "resolution_action": "all_to_production",
            },
        )

    assert received.status_code == 200, received.text
    assert received.json()["incoming_quantity"] == 92
    with session_factory() as session:
        assert session.get(OrderItem, 1).requisition_qty == original_order_qty
        requisition_item = session.query(RequisitionItem).filter_by(
            order_item_id=1,
            status="有效",
        ).one()
        assert requisition_item.requisition_qty == original_requisition_qty


def test_mobile_entry_returns_lan_url_and_qr_code(requisition_app) -> None:
    app, _ = requisition_app
    with TestClient(app) as client:
        _login(client, "workshop")
        response = client.get("/api/incoming/mobile-entry")

    assert response.status_code == 200
    assert response.json()["url"].endswith(":8000/incoming.html")
    assert response.json()["qr_data_url"].startswith("data:image/png;base64,")


def test_pending_defaults_dimensions_and_batch_submission(requisition_app) -> None:
    from app.api.requisition import _suggested_dimensions
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.requisition import Requisition, RequisitionItem

    app, session_factory = requisition_app
    with TestClient(app) as client:
        _login(client, "sales")
        pending = client.get("/api/requisition/pending")
        created = client.post("/api/requisition/batches", json=_batch_payload())

    assert pending.status_code == 200
    row = pending.json()["items"][0]
    assert row["requisition_status"] == "未报料"
    assert Decimal(str(row["suggested_cardboard_len"])) == Decimal("1770")
    assert Decimal(str(row["suggested_cardboard_width"])) == Decimal("650")
    assert created.status_code == 201, created.text
    assert created.json()["requisition_number"].startswith(
        f"BL-{date.today():%Y%m%d}-"
    )
    assert created.json()["items"][0]["requisition_qty"] == 34
    assert created.json()["items"][0]["cutting_mode"] == "一开三"
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        assert item.inventory_deducted_qty == 0
        assert item.requisition_qty == 34
        assert item.requisition_status == "已报料"
        assert item.special_process == "一开三"
        assert session.scalar(select(Requisition)) is not None
        assert session.scalar(select(RequisitionItem)) is not None
    unknown = Product(
        customer_id=1,
        product_code="UNKNOWN-BOX",
        product_name="未知箱型",
        box_category="normal",
        box_style="未来新箱型",
        length_mm=Decimal("520"),
        width_mm=Decimal("350"),
        height_mm=Decimal("300"),
    )
    assert _suggested_dimensions(unknown) == (None, None)


def test_supplier_schedule_drives_incoming_priority_and_can_cancel_before_receive(
    requisition_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.requisition import Requisition, RequisitionItem

    app, session_factory = requisition_app
    with TestClient(app) as client:
        _login(client, "admin")
        created = client.post("/api/requisition/batches", json=_batch_payload())
        assert created.status_code == 201, created.text
        batch_id = created.json()["id"]
        scheduled = client.put(
            "/api/requisition/items/1/supplier-schedule",
            json={
                "supplier_delivery_time": "2026-06-15T08:30:00",
                "supplier_order_number": "SUP-8899",
            },
        )
        incoming = client.get("/api/incoming/pending")
        cancelled = client.put(
            "/api/requisition/items/1/cancel",
            json={},
        )

    assert scheduled.status_code == 200
    assert scheduled.json()["requisition_status"] == "供应商已排单"
    assert incoming.status_code == 200
    assert incoming.json()["items"][0]["requisition_status"] == "供应商已排单"
    assert incoming.json()["items"][0]["supplier_delivery_time"].startswith(
        "2026-06-15T08:30"
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["requisition_status"] == "未报料"
    with session_factory() as session:
        batch = session.get(Requisition, batch_id)
        assert batch is not None
        assert batch.status == "已取消"
        assert {
            row.status
            for row in session.scalars(
                select(RequisitionItem).where(
                    RequisitionItem.requisition_id == batch_id
                )
            ).all()
        } == {"已取消"}
        audit = session.scalar(
            select(OperationLog).where(
                OperationLog.action == "CANCEL_REQUISITION",
                OperationLog.entity_id == 1,
            )
        )
    assert audit is not None
    assert '"before"' in audit.details and '"after"' in audit.details
    assert '"reason": "取消报料并退回待报料（系统记录）"' in audit.details
    assert f'"cancelled_requisition_batch_ids": [{batch_id}]' in audit.details
    assert '"requisition_status": "供应商已排单"' in audit.details
    assert '"requisition_status": "未报料"' in audit.details


def test_sales_execute_permission_cannot_cancel_requisition(requisition_app) -> None:
    app, _ = requisition_app
    with TestClient(app) as client:
        _login(client, "sales")
        created = client.post("/api/requisition/batches", json=_batch_payload())
        assert created.status_code == 201, created.text
        response = client.put(
            "/api/requisition/items/1/cancel",
            json={"reason": "普通账号不得执行逐级回退"},
        )

    assert response.status_code == 403


def test_submitted_requisition_items_can_be_listed(requisition_app) -> None:
    app, _ = requisition_app
    with TestClient(app) as client:
        _login(client, "admin")
        client.post("/api/requisition/batches", json=_batch_payload())
        client.put(
            "/api/requisition/items/1/supplier-schedule",
            json={"supplier_delivery_time": "2026-06-15T08:30:00"},
        )
        response = client.get("/api/requisition/items", params={"include_history": "true"})

    assert response.status_code == 200
    row = response.json()["items"][0]
    assert row["item_id"] == 1
    assert row["order_number"] == "PO-20260614-001"
    assert row["customer_name"] == "苏州思迈尔包装有限公司"
    assert row["requisition_status"] == "供应商已排单"
    assert row["supplier_delivery_time"].startswith("2026-06-15T08:30")


def test_requisition_api_hides_legacy_history_prefix_in_order_number(
    requisition_app,
) -> None:
    from app.models.order import Order

    app, session_factory = requisition_app
    with session_factory() as session:
        session.get(Order, 1).order_number = "RUIDA-42838"
        session.commit()
    with TestClient(app) as client:
        _login(client, "admin")
        client.post("/api/requisition/batches", json=_batch_payload())
        response = client.get(
            "/api/requisition/items",
            params={"include_history": "true"},
        )

    assert response.status_code == 200
    row = response.json()["items"][0]
    assert row["display_order_number"] == "TM20260614-0001"
    assert row["order_number"] == "TM20260614-0001"
    assert "RUIDA" not in str(response.json())


def test_requisition_cannot_change_or_cancel_after_material_received(
    requisition_app,
) -> None:
    app, _ = requisition_app
    with TestClient(app) as client:
        _login(client, "admin")
        client.post("/api/requisition/batches", json=_batch_payload())
        received = client.put("/api/incoming/receive/1")
        cancelled = client.put(
            "/api/requisition/items/1/cancel",
            json={"reason": "错误操作"},
        )
        edited = client.put(
            "/api/requisition/items/1",
            json={
                "inventory_deducted_qty": 0,
                "requisition_qty": 100,
                "cardboard_len": "1756",
                "cardboard_width": "654",
                "special_process": "一开一",
                "remark": None,
            },
        )

    assert received.status_code == 200
    assert cancelled.status_code == 409
    assert edited.status_code == 409


def test_history_search_returns_latest_successful_requisition(requisition_app) -> None:
    app, _ = requisition_app
    with TestClient(app) as client:
        _login(client, "sales")
        client.post("/api/requisition/batches", json=_batch_payload())
        response = client.get(
            "/api/requisition/search_history",
            params={"keyword": "思迈尔 21301028 520"},
        )

    assert response.status_code == 200
    result = response.json()["items"][0]
    assert result["product_code"] == "21301028"
    assert Decimal(str(result["cardboard_len"])) == Decimal("1756")
    assert result["special_process"] == "一开三"
    assert result["material"] == "K=A-BC"


def test_requisition_print_contract_has_no_financial_fields(requisition_app) -> None:
    from app.models.company_config import CompanyConfig

    app, session_factory = requisition_app
    with session_factory() as session:
        session.add(
            CompanyConfig(
                id=1,
                company_name="测试纸品包装厂",
                address="测试路88号",
                phone="0512-12345678",
            )
        )
        session.commit()
    with TestClient(app) as client:
        _login(client, "admin")
        payload = _batch_payload()
        payload["items"][0]["special_process"] = "一开12"
        payload["items"][0]["requisition_qty"] = 9
        payload["items"][0]["remark"] = "供应商只看人工备注"
        created = client.post("/api/requisition/batches", json=payload)
        response = client.get(
            f"/api/requisition/batches/{created.json()['id']}/print"
        )

    assert response.status_code == 200
    assert response.json()["sender"] == {
        "company_name": "测试纸品包装厂",
        "address": "测试路88号",
        "phone": "0512-12345678",
    }
    print_line = response.json()["items"][0]
    assert print_line["special_process"] == "一开12"
    assert print_line["report_remark"] == "供应商只看人工备注"
    assert "一开12" not in print_line["report_remark"]
    serialized = str(response.json()).lower()
    for forbidden in ("unit_price", "subtotal", "cost", "amount"):
        assert forbidden not in serialized


def test_double_splice_with_one_to_three_uses_piece_count(requisition_app) -> None:
    from app.models.order import OrderItem

    app, session_factory = requisition_app
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        item.snapshot_splice_mode = "double"
        item.snapshot_pieces_per_box = 2
        item.snapshot_report_length_mm = 800
        item.snapshot_report_width_mm = 200
        item.snapshot_crease_type = "压线"
        item.snapshot_crease_left_mm = 50
        item.snapshot_crease_middle_mm = 100
        item.snapshot_crease_right_mm = 50
        session.commit()

    payload = {
        "supplier_name": "苏州纸板供应商",
        "items": [
            {
                "order_item_id": 1,
                "inventory_deducted_qty": 0,
                "requisition_qty": 4,
                "cardboard_len": "800",
                "cardboard_width": "600",
                "special_process": "一开三",
                "remark": "双拼一开三",
            }
        ],
    }

    with TestClient(app) as client:
        _login(client, "sales")
        pending = client.get("/api/requisition/pending")
        created = client.post("/api/requisition/batches", json=payload)

    assert pending.status_code == 200
    row = pending.json()["items"][0]
    assert row["pieces_per_box"] == 2
    assert row["required_piece_qty"] == 200
    assert created.status_code == 201, created.text
    assert created.json()["items"][0]["requisition_qty"] == 67
    assert created.json()["items"][0]["cardboard_len"] == "800"
    assert created.json()["items"][0]["cardboard_width"] == "600"

    with session_factory() as session:
        item = session.get(OrderItem, 1)
        assert item.requisition_qty == 67
        assert item.special_process == "一开三"


def test_requisition_blocks_mismatched_unit_crease_but_not_purchase_width_factor(
    requisition_app,
) -> None:
    from app.models.order import OrderItem

    app, session_factory = requisition_app
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        item.snapshot_report_length_mm = 800
        item.snapshot_report_width_mm = 205
        item.snapshot_crease_type = "压线"
        item.snapshot_crease_left_mm = 50
        item.snapshot_crease_middle_mm = 100
        item.snapshot_crease_right_mm = 50
        session.commit()

    payload = {
        "supplier_name": "苏州纸板供应商",
        "items": [
            {
                "order_item_id": 1,
                "inventory_deducted_qty": 0,
                "requisition_qty": 50,
                "cardboard_len": "800",
                "cardboard_width": "400",
                "special_process": "一开二",
                "remark": "采购宽可以按开料倍数放大",
            }
        ],
    }
    with TestClient(app) as client:
        _login(client, "sales")
        rejected = client.post("/api/requisition/batches", json=payload)

    assert rejected.status_code == 409
    assert "订单明细压线三段合计 200mm" in rejected.json()["detail"]
    assert "采购宽" not in rejected.json()["detail"]


def _seed_n005_component_rows(session_factory, names: list[str]) -> list[int]:
    from app.models.order import OrderItem
    from app.models.requisition import Requisition, RequisitionItem

    with session_factory() as session:
        item = session.get(OrderItem, 1)
        requisition = Requisition(
            requisition_number="N005-COMPONENT-PARENT",
            requisition_date=date(2026, 7, 14),
            supplier_name="N005测试供应商",
            status="已报料",
        )
        session.add(requisition)
        session.flush()
        rows = [
            RequisitionItem(
                requisition_id=requisition.id,
                order_item_id=item.id,
                requisition_qty=100,
                cardboard_len=Decimal("400"),
                cardboard_width=Decimal("300"),
                special_process="无",
                product_name_snapshot=name,
                status="有效",
            )
            for name in names
        ]
        session.add_all(rows)
        item.material_status = "pending"
        item.requisition_status = "已报料"
        item.requisition_qty = 100 * len(rows)
        session.commit()
        return [row.id for row in rows]


def test_telescoping_parent_path_rejected_after_cover_received_for_single_and_batch(
    requisition_app,
) -> None:
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.order import OrderItem
    from app.models.requisition import RequisitionItem

    app, session_factory = requisition_app
    cover_id, base_id = _seed_n005_component_rows(
        session_factory,
        ["天地盖测试箱-盖", "天地盖测试箱-底"],
    )
    with TestClient(app) as client:
        _login(client, "workshop")
        cover = client.put(f"/api/incoming/receive/r{cover_id}")
        parent = client.put(
            "/api/incoming/receive/1",
            json={"received_quantity": 100},
        )
        batch = client.put(
            "/api/incoming/batch-receive",
            json={"items": [{"item_id": 1, "received_quantity": 100}]},
        )

    assert cover.status_code == 200, cover.text
    assert parent.status_code == 409
    assert "天地盖来料必须分别" in parent.json()["detail"]
    assert batch.status_code == 200
    assert batch.json()["succeeded"] == 0
    assert "天地盖来料必须分别" in batch.json()["results"][0]["message"]
    with session_factory() as session:
        assert session.get(RequisitionItem, cover_id).status == "已入库"
        assert session.get(RequisitionItem, base_id).status == "有效"
        assert session.get(OrderItem, 1).material_status == "pending"
        assert session.query(IncomingReceiptItem).count() == 1


def test_telescoping_parent_path_rejected_for_single_side_anomaly(
    requisition_app,
) -> None:
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.requisition import RequisitionItem

    app, session_factory = requisition_app
    (cover_id,) = _seed_n005_component_rows(
        session_factory,
        ["天地盖异常数据-盖"],
    )
    with TestClient(app) as client:
        _login(client, "workshop")
        parent = client.put("/api/incoming/receive/1")
        batch = client.put(
            "/api/incoming/batch-receive",
            json={"items": [{"item_id": 1, "received_quantity": 100}]},
        )

    assert parent.status_code == 409
    assert "天地盖来料必须分别" in parent.json()["detail"]
    assert batch.status_code == 200
    assert batch.json()["failed"] == 1
    assert "天地盖来料必须分别" in batch.json()["results"][0]["message"]
    with session_factory() as session:
        assert session.get(RequisitionItem, cover_id).status == "有效"
        assert session.query(IncomingReceiptItem).count() == 0


def test_telescoping_lid_requisition_splits_cover_and_base_rows(
    requisition_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.requisition import RequisitionItem

    app, session_factory = requisition_app
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        product = session.get(Product, item.product_id)
        product.box_style = "A3 天地盖"
        item.snapshot_product_name = "天地盖测试箱"
        item.snapshot_report_length_mm = 400
        item.snapshot_report_width_mm = 300
        item.snapshot_crease_type = "压线"
        item.snapshot_crease_left_mm = 50
        item.snapshot_crease_middle_mm = 200
        item.snapshot_crease_right_mm = 50
        item.snapshot_report_notes = "盖料备注"
        item.snapshot_base_report_length_mm = 375
        item.snapshot_base_report_width_mm = 275
        item.snapshot_base_crease_type = "压线"
        item.snapshot_base_crease_left_mm = 50
        item.snapshot_base_crease_middle_mm = 175
        item.snapshot_base_crease_right_mm = 50
        item.snapshot_base_report_notes = "底料备注"
        item.snapshot_splice_mode = "single"
        item.snapshot_pieces_per_box = 1
        session.commit()

    payload = {
        "supplier_name": "苏州纸板供应商",
        "items": [
            {
                "order_item_id": 1,
                "inventory_deducted_qty": 0,
                "requisition_qty": 100,
                "cardboard_len": "400",
                "cardboard_width": "300",
                "special_process": "一开一",
                "remark": None,
            }
        ],
    }

    with TestClient(app) as client:
        _login(client, "sales")
        created = client.post("/api/requisition/batches", json=payload)
        assert created.status_code == 201, created.text
        batch_id = created.json()["id"]
        printed = client.get(f"/api/requisition/batches/{batch_id}/print")
        with session_factory() as session:
            component_ids = [
                row.id
                for row in session.query(RequisitionItem)
                .filter_by(order_item_id=1, status="有效")
                .order_by(RequisitionItem.id)
                .all()
            ]
        client.post("/api/auth/logout")
        _login(client, "workshop")
        received_cover = client.put(f"/api/incoming/receive/r{component_ids[0]}")
        received_base = client.put(f"/api/incoming/receive/r{component_ids[1]}")

    assert printed.status_code == 200, printed.text
    print_rows = printed.json()["items"]
    assert [row["product_name"] for row in print_rows] == [
        "天地盖测试箱-盖",
        "天地盖测试箱-底",
    ]
    assert [row["specification"] for row in print_rows] == ["400×300", "375×275"]
    assert [row["crease_display"] for row in print_rows] == ["50+200+50", "50+175+50"]
    assert print_rows[0]["report_remark"] == "盖料备注"
    assert print_rows[1]["report_remark"] == "底料备注"
    assert received_cover.status_code == 200, received_cover.text
    assert received_base.status_code == 200, received_base.text
    assert received_cover.json()["incoming_quantity"] == 100
    assert received_base.json()["incoming_quantity"] == 100

    with session_factory() as session:
        item = session.get(OrderItem, 1)
        rows = (
            session.query(RequisitionItem)
            .filter_by(order_item_id=1, status="已入库")
            .order_by(RequisitionItem.id)
            .all()
        )
        assert item.requisition_qty == 200
        assert item.requisition_spec == "盖:400×300；底:375×275"
        assert [row.product_name_snapshot for row in rows] == [
            "天地盖测试箱-盖",
            "天地盖测试箱-底",
        ]
        assert [int(row.cardboard_len) for row in rows] == [400, 375]
        assert [int(row.cardboard_width) for row in rows] == [300, 275]
        assert [row.requisition_qty for row in rows] == [100, 100]


def test_telescoping_lid_incoming_keeps_cover_and_base_as_separate_rows(
    requisition_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product import Product

    app, session_factory = requisition_app
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        product = session.get(Product, item.product_id)
        product.box_style = "A3 天地盖"
        item.quantity = 1
        item.snapshot_product_name = "天地盖测试箱"
        item.snapshot_report_length_mm = 400
        item.snapshot_report_width_mm = 300
        item.snapshot_crease_type = "压线"
        item.snapshot_crease_left_mm = 50
        item.snapshot_crease_middle_mm = 200
        item.snapshot_crease_right_mm = 50
        item.snapshot_base_report_length_mm = 375
        item.snapshot_base_report_width_mm = 275
        item.snapshot_base_crease_type = "压线"
        item.snapshot_base_crease_left_mm = 50
        item.snapshot_base_crease_middle_mm = 175
        item.snapshot_base_crease_right_mm = 50
        item.snapshot_splice_mode = "single"
        item.snapshot_pieces_per_box = 1
        session.commit()

    payload = {
        "supplier_name": "苏州纸板供应商",
        "items": [
            {
                "order_item_id": 1,
                "inventory_deducted_qty": 0,
                "requisition_qty": 1,
                "cardboard_len": "400",
                "cardboard_width": "300",
                "special_process": "一开一",
                "remark": None,
            }
        ],
    }

    with TestClient(app) as client:
        _login(client, "sales")
        created = client.post("/api/requisition/batches", json=payload)
        assert created.status_code == 201, created.text
        client.post("/api/auth/logout")
        _login(client, "workshop")
        pending = client.get("/api/incoming/pending")
        rows = pending.json()["items"]
        cover_id = next(row["item_id"] for row in rows if row["component_type"] == "cover")
        base_id = next(row["item_id"] for row in rows if row["component_type"] == "base")
        received_cover = client.put(f"/api/incoming/receive/{cover_id}")
        pending_after_cover = client.get("/api/incoming/pending")
        received_base = client.put(f"/api/incoming/receive/{base_id}")
        pending_after_all = client.get("/api/incoming/pending")
        with session_factory() as stale_session:
            stale_item = stale_session.get(OrderItem, 1)
            stale_item.material_status = "pending"
            stale_item.material_received_at = None
            stale_session.commit()
        received_today = client.get("/api/incoming/received")
        received_history = client.get("/api/incoming/history")
        received_rows = received_history.json()["items"]
        client.post("/api/auth/logout")
        _login(client, "admin")
        revert_cover = client.put(
            f"/api/incoming/revert/{cover_id}",
            json={"reason": "撤销盖入库"},
        )

    assert pending.status_code == 200
    assert [row["product_name"] for row in rows] == ["天地盖测试箱-盖", "天地盖测试箱-底"]
    assert [row["component_type"] for row in rows] == ["cover", "base"]
    assert [row["incoming_quantity"] for row in rows] == [1, 1]
    assert [f"{row['cardboard_len']}×{row['cardboard_width']}" for row in rows] == [
        "400.00×300.00",
        "375.00×275.00",
    ]
    assert received_cover.status_code == 200, received_cover.text
    assert received_cover.json()["component_status"] == "已入库"
    assert [row["component_type"] for row in pending_after_cover.json()["items"]] == ["base"]
    assert [row["item_id"] for row in pending_after_cover.json()["items"]] == [base_id]
    assert received_base.status_code == 200, received_base.text
    assert pending_after_all.status_code == 200
    assert pending_after_all.json()["items"] == []
    assert received_today.status_code == 200
    assert [row["product_name"] for row in received_today.json()["items"]] == [
        "天地盖测试箱-底",
        "天地盖测试箱-盖",
    ]
    assert received_history.status_code == 200
    assert [row["product_name"] for row in received_rows] == [
        "天地盖测试箱-底",
        "天地盖测试箱-盖",
    ]
    assert [row["item_id"] for row in received_rows] == [base_id, cover_id]
    assert revert_cover.status_code == 200, revert_cover.text
    assert revert_cover.json()["component_status"] == "有效"
    with session_factory() as session:
        from app.models.requisition import RequisitionItem

        item = session.get(OrderItem, 1)
        assert item.material_status == "pending"
        assert item.requisition_status == "已报料"
        assert item.requisition_qty == 2
        cover_req = session.get(RequisitionItem, int(cover_id[1:]))
        base_req = session.get(RequisitionItem, int(base_id[1:]))
        assert cover_req.status == "有效"
        assert base_req.status == "已入库"


def test_telescoping_lid_batch_accepts_separate_cover_and_base_quantities(
    requisition_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.requisition import RequisitionItem

    app, session_factory = requisition_app
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        product = session.get(Product, item.product_id)
        product.box_style = "A3 天地盖"
        item.snapshot_product_name = "天地盖测试箱"
        item.snapshot_report_length_mm = 400
        item.snapshot_report_width_mm = 300
        item.snapshot_base_report_length_mm = 375
        item.snapshot_base_report_width_mm = 275
        session.commit()

    payload = {
        "supplier_name": "苏州纸板供应商",
        "items": [
            {
                "order_item_id": 1,
                "component_type": "cover",
                "inventory_deducted_qty": 0,
                "requisition_qty": 100,
                "cardboard_len": "400",
                "cardboard_width": "300",
                "special_process": "一开一",
                "remark": None,
            },
            {
                "order_item_id": 1,
                "component_type": "base",
                "inventory_deducted_qty": 0,
                "requisition_qty": 99,
                "cardboard_len": "375",
                "cardboard_width": "275",
                "special_process": "一开一",
                "remark": None,
            },
        ],
    }

    with TestClient(app) as client:
        _login(client, "sales")
        created = client.post("/api/requisition/batches", json=payload)

    assert created.status_code == 201, created.text
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        rows = (
            session.query(RequisitionItem)
            .filter_by(order_item_id=1, status="有效")
            .order_by(RequisitionItem.id)
            .all()
        )
        assert item.requisition_qty == 200
        assert item.requisition_spec == "盖:400×300；底:375×275"
        assert [row.product_name_snapshot for row in rows] == [
            "天地盖测试箱-盖",
            "天地盖测试箱-底",
        ]
        assert [row.requisition_qty for row in rows] == [100, 100]



def test_phase11_migration_is_additive_and_preserves_order_items(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "phase11.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "phase11-migration-test")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "f4b2c9d7a110")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO users (
                id, username, password_hash, role, real_name, display_name,
                is_active, must_change_password
            ) VALUES (1, 'admin', 'hash', 'admin', 'admin', 'admin', 1, 1)
            """
        )
        connection.execute(
            """
            INSERT INTO customers (
                id, customer_number, customer_code, name, payment_term_days,
                credit_limit, default_tax_rate, status, is_active
            ) VALUES (1, 1, 'T', '测试客户', 30, 0, 0.13, 'active', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO products (
                id, customer_id, product_code, customer_material_code,
                product_name, box_category, unit, is_active
            ) VALUES (1, 1, 'P1', 'M1', '测试箱', 'normal', '只', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO sales_orders (
                id, order_number, customer_id, order_date, status,
                payment_status, total_amount
            ) VALUES (1, 'PO-OLD-001', 1, '2026-06-01',
                      'pending_production', 'unpaid', 10)
            """
        )
        connection.execute(
            """
            INSERT INTO sales_order_items (
                id, order_id, product_id, quantity, delivered_quantity,
                is_force_closed, unit_price, subtotal, material_status,
                snapshot_product_name
            ) VALUES (9, 1, 1, 10, 0, 0, 1, 10, 'pending', '历史纸箱')
            """
        )
        connection.commit()

    command.upgrade(config, "head")
    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "b71c4a9e2d10_phase11_requisition_workflow.py"
    ).read_text(encoding="utf-8")
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT id, quantity, requisition_status FROM sales_order_items WHERE id=9"
        ).fetchone()
        tables = {
            value[0]
            for value in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

    assert row == (9, 10, "未报料")
    assert {"material_requisitions", "material_requisition_items"} <= tables
    assert "drop_table" not in migration.lower()
    assert "drop_column" not in migration.lower()
