from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import func, select

from test_p1_40a_packaging_masterdata import _honeycomb_payload, p1_40a_app


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PARENT_REVISION = "jh67v8x9z56"
TARGET_REVISION = "ji68v8x9z57"


@pytest.fixture()
def external_stock_app(p1_40a_app: FastAPI) -> FastAPI:
    from app.api.external_packaging_purchases import router as purchase_router
    from app.api.requisition import router as requisition_router

    p1_40a_app.include_router(requisition_router, prefix="/api/requisition")
    p1_40a_app.include_router(purchase_router, prefix="/api")
    return p1_40a_app


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "p1-40a-admin", "password": "123456"},
    )
    assert response.status_code == 200


def _seed_external_warning(app: FastAPI) -> tuple[int, int]:
    from app.models.external_packaging_price import ExternalPackagingPriceVersion
    from app.models.product import Product
    from app.models.stock_replenishment import InventoryStockPolicy
    from app.models.supplier import ExternalPackagingProduct
    from app.models.user import User

    ids = app.state.fixture
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/master/products", json=_honeycomb_payload(ids)
        )
        assert created.status_code == 201, created.text
        product_id = int(created.json()["id"])

    with app.state.factory() as db:
        product = db.get(Product, product_id)
        external_product = db.get(
            ExternalPackagingProduct, ids["HC-GENERAL"]
        )
        admin = db.scalar(select(User).where(User.username == "p1-40a-admin"))
        assert product is not None and external_product is not None and admin is not None
        db.add(
            ExternalPackagingPriceVersion(
                external_product_id=external_product.id,
                version_number=1,
                product_version=external_product.version,
                specification_snapshot_json=external_product.specification_json,
                quote_unit="片",
                unit_conversion_basis="采购单位直接计价",
                currency="CNY",
                tax_mode="tax_inclusive",
                tax_rate=Decimal("0.13"),
                unit_price=Decimal("8.50"),
                effective_from=date(2026, 1, 1),
                moq_quantity=Decimal("1"),
                moq_unit="片",
                packaging_multiple=Decimal("1"),
                tier_prices_json="[]",
                shipping_fee_mode="not_provided",
                evidence_reference="P1-140-UAT",
                quote_fingerprint="p1-140-honeycomb",
                created_by=admin.id,
            )
        )
        policy = InventoryStockPolicy(
            policy_name="蜂窝板提前备库",
            target_inventory_type="finished",
            product_id=product.id,
            customer_id=product.customer_id,
            warning_quantity=1000,
            target_quantity=4000,
            active=True,
            created_by=admin.id,
            updated_by=admin.id,
        )
        db.add(policy)
        db.commit()
        return int(policy.id), int(product.id)


def test_external_warning_draft_uses_common_box_ratio_without_paper_fields(
    external_stock_app: FastAPI,
) -> None:
    policy_id, product_id = _seed_external_warning(external_stock_app)
    from app.api.dashboard import _common_box_low_stock_warnings

    with external_stock_app.state.factory() as db:
        warning = next(
            row
            for row in _common_box_low_stock_warnings(db, None)
            if row["policy_id"] == policy_id
        )
        assert warning["procurement_mode"] == "external_purchase"
        assert warning["draft_ready"] is True
        assert warning["missing_fields"] == []

    with TestClient(external_stock_app) as client:
        _login(client)
        response = client.get(
            f"/api/requisition/stock-policies/{policy_id}/replenishment-draft"
        )
        assert response.status_code == 200, response.text
        draft = response.json()
        assert draft["procurement_mode"] == "external_purchase"
        assert draft["draft_ready"] is True
        assert draft["missing_fields"] == []
        assert draft["supplier_name"] == "蜂窝板供应商"
        line = draft["items"][0]
        assert line["target_inventory_type"] == "finished"
        assert line["product_id"] == product_id
        assert line["quantity"] == 4000
        assert line["suggested_finished_quantity"] == 4000
        assert line["external_purchase_quantity"] == "8000"
        assert line["external_purchase_unit"] == "片"
        assert line["external_order_quantity_basis"] == "1"
        assert line["external_purchase_quantity_basis"] == "2"
        assert line["material_code"] is None
        assert line["layer_count"] is None
        assert line["flute_type"] is None
        assert line["report_length_mm"] is None
        assert line["report_width_mm"] is None


def test_external_warning_confirm_creates_no_sales_order_and_is_idempotent(
    external_stock_app: FastAPI,
) -> None:
    from app.models.external_packaging_purchase import (
        ExternalPackagingPurchaseBatch,
        ExternalPackagingPurchaseItem,
        ExternalPackagingPurchaseOrder,
    )
    from app.models.order import Order
    from app.models.stock_replenishment import (
        InventoryStockPolicy,
        StockReplenishmentOrder,
        StockReplenishmentOrderItem,
    )
    from app.services.stock_replenishment import stock_policy_dict

    policy_id, _product_id = _seed_external_warning(external_stock_app)
    with TestClient(external_stock_app) as client:
        _login(client)
        draft = client.get(
            f"/api/requisition/stock-policies/{policy_id}/replenishment-draft"
        ).json()
        payload = {
            "source_type": "stock_warning",
            "idempotency_key": "p1-140-external-stock-warning",
            "customer_id": draft["customer_id"],
            "supplier_name": draft["supplier_name"],
            "stock_now": False,
            "items": [draft["items"][0]],
        }
        first = client.post(
            "/api/requisition/stock-replenishment/orders", json=payload
        )
        assert first.status_code == 201, first.text
        body = first.json()
        assert body["procurement_mode"] == "external_purchase"
        assert body["external_purchase_orders"][0]["purchase_number"].startswith(
            "EP-"
        )
        assert body["items"][0]["quantity"] == 4000
        assert body["external_purchase_orders"][0]["items"][0][
            "purchase_quantity"
        ] == "8000"
        repeated = client.post(
            "/api/requisition/stock-replenishment/orders", json=payload
        )
        assert repeated.status_code == 201, repeated.text
        assert repeated.json()["id"] == body["id"]

    with external_stock_app.state.factory() as db:
        assert int(db.scalar(select(func.count(Order.id))) or 0) == 0
        assert int(
            db.scalar(select(func.count(StockReplenishmentOrder.id))) or 0
        ) == 1
        assert int(
            db.scalar(select(func.count(StockReplenishmentOrderItem.id))) or 0
        ) == 1
        assert int(
            db.scalar(select(func.count(ExternalPackagingPurchaseBatch.id))) or 0
        ) == 1
        assert int(
            db.scalar(select(func.count(ExternalPackagingPurchaseOrder.id))) or 0
        ) == 1
        purchase_item = db.scalar(select(ExternalPackagingPurchaseItem))
        policy = db.get(InventoryStockPolicy, policy_id)
        assert purchase_item is not None
        assert policy is not None
        assert purchase_item.sales_order_id is None
        assert purchase_item.sales_order_item_id is None
        assert purchase_item.stock_replenishment_item_id is not None
        assert Decimal(purchase_item.order_quantity_basis_snapshot) == Decimal("1")
        assert Decimal(purchase_item.purchase_quantity_basis_snapshot) == Decimal("2")
        summary = stock_policy_dict(db, policy)
        assert summary["external_purchase_incoming_quantity"] == 4000
        assert summary["suggested_new_requisition_finished_quantity"] == 0
        assert summary["replenishment_state"] == "already_ordered"


def test_external_warning_purchase_confirmation_requires_admin_cost_authority(
    external_stock_app: FastAPI,
) -> None:
    from app.core.security import hash_password
    from app.models.user import User

    policy_id, _product_id = _seed_external_warning(external_stock_app)
    with external_stock_app.state.factory() as db:
        db.add(
            User(
                username="p1-140-boss",
                password_hash=hash_password("123456"),
                role="boss",
                real_name="只读负责人",
                must_change_password=False,
            )
        )
        db.commit()

    with TestClient(external_stock_app) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": "p1-140-boss", "password": "123456"},
        )
        assert login.status_code == 200, login.text
        draft = client.get(
            f"/api/requisition/stock-policies/{policy_id}/replenishment-draft"
        ).json()
        denied = client.post(
            "/api/requisition/stock-replenishment/orders",
            json={
                "source_type": "stock_warning",
                "idempotency_key": "p1-140-non-admin-denied",
                "customer_id": draft["customer_id"],
                "supplier_name": draft["supplier_name"],
                "stock_now": False,
                "items": [draft["items"][0]],
            },
        )
        assert denied.status_code == 403, denied.text
        assert "仅管理员可确认" in denied.json()["detail"]


def test_external_stock_partial_receipts_keep_loose_units_until_ratio_is_complete(
    external_stock_app: FastAPI,
) -> None:
    from app.models.external_packaging_purchase import ExternalPackagingPurchaseItem
    from app.models.stock_replenishment import StockReplenishmentOrderItem
    from app.models.warehouse_inventory import (
        FinishedGoodsInventoryDetail,
        InventoryLot,
        WarehouseLocation,
    )

    policy_id, product_id = _seed_external_warning(external_stock_app)
    with external_stock_app.state.factory() as db:
        db.add(
            WarehouseLocation(
                location_code="F1-DISPATCH-01",
                location_name="一楼成品待送区",
                warehouse_type="finished",
                warehouse_floor=1,
                area_code="DISPATCH",
                storage_type="temporary_aisle",
                source_version="P1-25C",
                placement_status="placed",
                is_active=True,
            )
        )
        db.commit()

    with TestClient(external_stock_app) as client:
        _login(client)
        draft = client.get(
            f"/api/requisition/stock-policies/{policy_id}/replenishment-draft"
        ).json()
        created = client.post(
            "/api/requisition/stock-replenishment/orders",
            json={
                "source_type": "stock_warning",
                "idempotency_key": "p1-140-partial-receipt",
                "customer_id": draft["customer_id"],
                "supplier_name": draft["supplier_name"],
                "stock_now": False,
                "items": [draft["items"][0]],
            },
        )
        assert created.status_code == 201, created.text
        purchase_order = created.json()["external_purchase_orders"][0]
        purchase_order_id = int(purchase_order["id"])
        purchase_item_id = int(purchase_order["items"][0]["id"])
        pending = client.get("/api/external-packaging-purchases/pending-receipts")
        assert pending.status_code == 200, pending.text
        pending_row = next(
            row
            for row in pending.json()["purchase_orders"]
            if row["id"] == purchase_order_id
        )
        assert pending_row["source_type"] == "stock_replenishment"
        assert pending_row["sales_order_id"] is None
        assert pending_row["stock_replenishment_order_id"] == created.json()["id"]

        first = client.post(
            f"/api/external-packaging-purchases/{purchase_order_id}/receipts",
            json={
                "idempotency_key": "p1-140-receive-one",
                "lines": [
                    {
                        "purchase_item_id": purchase_item_id,
                        "received_quantity": "1",
                    }
                ],
            },
        )
        assert first.status_code == 200, first.text
        first_line = first.json()["receipt"]["items"][0]
        assert first_line["converted_finished_quantity"] == 0
        assert first_line["loose_remainder_quantity_after"] == "1"

        with external_stock_app.state.factory() as db:
            assert int(
                db.scalar(
                    select(func.count(InventoryLot.id)).where(
                        InventoryLot.inventory_type == "finished"
                    )
                )
                or 0
            ) == 0
            stock_item = db.scalar(select(StockReplenishmentOrderItem))
            assert stock_item is not None
            assert stock_item.stocked_quantity == 0

        second = client.post(
            f"/api/external-packaging-purchases/{purchase_order_id}/receipts",
            json={
                "idempotency_key": "p1-140-receive-two",
                "lines": [
                    {
                        "purchase_item_id": purchase_item_id,
                        "received_quantity": "1",
                    }
                ],
            },
        )
        assert second.status_code == 200, second.text
        second_line = second.json()["receipt"]["items"][0]
        assert second_line["converted_finished_quantity"] == 1
        assert second_line["loose_remainder_quantity_after"] == "0"
        repeated = client.post(
            f"/api/external-packaging-purchases/{purchase_order_id}/receipts",
            json={
                "idempotency_key": "p1-140-receive-two",
                "lines": [
                    {
                        "purchase_item_id": purchase_item_id,
                        "received_quantity": "1",
                    }
                ],
            },
        )
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["created"] is False
        history = client.get("/api/external-packaging-purchases/history")
        assert history.status_code == 200, history.text
        history_batch = next(
            row
            for row in history.json()["items"]
            if row["batch_id"]
            == created.json()["external_purchase_batch_id"]
        )
        assert history_batch["source_type"] == "stock_replenishment"
        assert history_batch["order_number"] == created.json()["order_number"]
        assert history_batch["customer_order_number"] is None
        assert history_batch["can_cancel"] is False
        blocked_cancel = client.post(
            f"/api/external-packaging-purchases/{purchase_order_id}/cancel",
            json={
                "expected_batch_id": created.json()[
                    "external_purchase_batch_id"
                ],
                "confirmed": True,
                "reason": "已有实收后验证撤销门禁",
            },
        )
        assert blocked_cancel.status_code == 409, blocked_cancel.text
        printed = client.get(
            f"/api/external-packaging-purchases/{purchase_order_id}/print"
        )
        assert printed.status_code == 200, printed.text
        assert printed.json()["purchase_number"] == purchase_order["purchase_number"]
        assert "source_type" not in printed.json()
        assert "source_number" not in printed.json()

    with external_stock_app.state.factory() as db:
        stock_item = db.scalar(select(StockReplenishmentOrderItem))
        purchase_item = db.scalar(select(ExternalPackagingPurchaseItem))
        lot = db.scalar(
            select(InventoryLot)
            .join(
                FinishedGoodsInventoryDetail,
                FinishedGoodsInventoryDetail.inventory_lot_id == InventoryLot.id,
            )
            .where(FinishedGoodsInventoryDetail.product_id == product_id)
        )
        assert stock_item is not None and purchase_item is not None and lot is not None
        assert stock_item.stocked_quantity == 1
        assert lot.quantity_available == 1


def test_unreceived_external_stock_purchase_can_cancel_and_reopen_warning(
    external_stock_app: FastAPI,
) -> None:
    policy_id, _product_id = _seed_external_warning(external_stock_app)
    with TestClient(external_stock_app) as client:
        _login(client)
        draft = client.get(
            f"/api/requisition/stock-policies/{policy_id}/replenishment-draft"
        ).json()
        created = client.post(
            "/api/requisition/stock-replenishment/orders",
            json={
                "source_type": "stock_warning",
                "idempotency_key": "p1-140-cancellable",
                "customer_id": draft["customer_id"],
                "supplier_name": draft["supplier_name"],
                "stock_now": False,
                "items": [draft["items"][0]],
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        purchase_order_id = int(body["external_purchase_orders"][0]["id"])
        cancelled = client.post(
            f"/api/external-packaging-purchases/{purchase_order_id}/cancel",
            json={
                "expected_batch_id": body["external_purchase_batch_id"],
                "confirmed": True,
                "reason": "测试撤销未实收备库采购",
            },
        )
        assert cancelled.status_code == 200, cancelled.text
        refreshed = client.get(
            f"/api/requisition/stock-policies/{policy_id}/replenishment-draft"
        )
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["draft_ready"] is True
        assert refreshed.json()["items"][0]["quantity"] == 4000
        history = client.get("/api/external-packaging-purchases/history")
        assert history.status_code == 200, history.text
        row = next(
            item
            for item in history.json()["items"]
            if item["batch_id"] == body["external_purchase_batch_id"]
        )
        assert row["lifecycle_status"] == "cancelled"
        assert row["purchase_orders"][0]["lifecycle_status"] == "cancelled"


def test_p1_140_migration_is_linear_and_round_trips_isolated_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.core.database import create_sqlite_engine
    from app.models import Base

    database = tmp_path / "p1-140-migration.sqlite3"
    engine = create_sqlite_engine(database)
    Base.metadata.create_all(engine)
    engine.dispose()
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    monkeypatch.setenv("ERP_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("ERP_SECRET_KEY", "p1-140-isolated-migration")
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    assert script.get_revision(TARGET_REVISION).down_revision == PARENT_REVISION
    assert script.get_heads() == [TARGET_REVISION]
    command.stamp(config, TARGET_REVISION)

    def assert_health(revision: str) -> None:
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            assert connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone() == (revision,)
            assert connection.execute("PRAGMA integrity_check").fetchone() == (
                "ok",
            )
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    assert_health(TARGET_REVISION)
    command.downgrade(config, PARENT_REVISION)
    assert_health(PARENT_REVISION)
    with sqlite3.connect(database) as connection:
        batch_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(external_packaging_purchase_batches)"
            )
        }
        assert "stock_replenishment_order_id" not in batch_columns

    command.upgrade(config, TARGET_REVISION)
    assert_health(TARGET_REVISION)
    with sqlite3.connect(database) as connection:
        batch_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(external_packaging_purchase_batches)"
            )
        }
        item_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(external_packaging_purchase_items)"
            )
        }
        receipt_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(external_packaging_receipt_items)"
            )
        }
        assert "stock_replenishment_order_id" in batch_columns
        assert {
            "stock_replenishment_item_id",
            "customer_product_id_snapshot",
            "order_quantity_basis_snapshot",
            "purchase_quantity_basis_snapshot",
        } <= item_columns
        assert {
            "converted_finished_quantity",
            "loose_remainder_quantity_after",
        } <= receipt_columns

    command.downgrade(config, PARENT_REVISION)
    assert_health(PARENT_REVISION)
    command.upgrade(config, TARGET_REVISION)
    assert_health(TARGET_REVISION)


def test_external_stock_warning_frontend_uses_purchase_units_and_history_actions() -> None:
    html = (PROJECT_ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert "建议外购备库" in html
    assert "常用箱比例" in html
    assert "供应商采购数量" in html
    assert "包材采购历史/来料待入库" in html
    assert "/api/external-packaging-purchases/${purchase.id}/cancel" in html
    assert (
        "item.procurement_mode==='external_purchase' "
        "? item.suggested_new_requisition_finished_quantity" in html
    )
