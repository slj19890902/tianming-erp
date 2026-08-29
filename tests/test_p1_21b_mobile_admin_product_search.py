from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import os
import re
import shutil
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


ROOT = Path(__file__).resolve().parents[1]
MOBILE_HTML = (ROOT / "static" / "mobile_erp.html").read_text(encoding="utf-8")
WAREHOUSE_HTML = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")
MAIN_SOURCE = (ROOT / "app" / "main.py").read_text(encoding="utf-8")


@pytest.fixture()
def mobile_erp_app(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.mobile_erp import router as mobile_erp_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.product import Product
    from app.models.user import User
    from app.models.warehouse_inventory import (
        FinishedGoodsInventoryDetail,
        Floor3LocationLayout,
        InventoryLot,
        InventoryPallet,
        InventoryPalletItem,
        InventoryReservation,
        SemiFinishedInventoryDetail,
        SemiFinishedLotAllowedProduct,
        WarehouseArea,
        WarehouseAreaStoragePolicy,
        WarehouseFloor,
        WarehouseGroundLayoutPlan,
        WarehouseGroundLayoutSlot,
        WarehouseLocation,
    )

    def published_identity(floor_number: int) -> dict:
        if int(floor_number) == 3:
            zones = {"mobile-zone-c1": "C1", "mobile-zone-sf": "SF"}
        else:
            zones = {"mobile-zone-fg": "FG"}
        return {
            "floor_code": f"{int(floor_number)}F",
            "revision": "mobile-map-v1",
            "feature_ids": frozenset(zones),
            "erp_area_codes": frozenset(zones.values()),
            "zones_by_id": zones,
            "zone_ids_by_area": {
                area_code: (feature_id,)
                for feature_id, area_code in zones.items()
            },
        }

    monkeypatch.setattr(
        "app.services.location_candidates.load_warehouse_twin_published_floor_identity",
        published_identity,
    )

    engine = create_sqlite_engine(tmp_path / "mobile-erp.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime(2026, 8, 2, 2, 0, 0)
    with factory() as db:
        admin = User(
            username="mobile-admin",
            password_hash=hash_password("123456"),
            role="admin",
            real_name="手机管理员",
            must_change_password=False,
        )
        scoped = User(
            username="mobile-scoped",
            password_hash=hash_password("123456"),
            role="workshop",
            real_name="客户范围员工",
            must_change_password=False,
            customer_access_mode="selected",
        )
        picker = User(
            username="mobile-picker",
            password_hash=hash_password("123456"),
            role="delivery_picker",
            real_name="送货拿货员",
            must_change_password=False,
        )
        customer = Customer(name="匿名客户甲", customer_code="NMJ")
        other_customer = Customer(name="匿名客户乙", customer_code="NMY")
        db.add_all([admin, scoped, picker, customer, other_customer])
        db.flush()
        db.add(UserCustomerScope(user_id=scoped.id, customer_id=customer.id))
        product = Product(
            customer_id=customer.id,
            product_code="MOBILE-BOX-001",
            customer_material_code="MB001",
            product_name="匿名三层外箱",
            length_mm=Decimal("420"),
            width_mm=Decimal("310"),
            height_mm=Decimal("260"),
        )
        product_two = Product(
            customer_id=customer.id,
            product_code="MOBILE-BOX-002",
            customer_material_code="MB002",
            product_name="匿名三层内盒",
        )
        other_product = Product(
            customer_id=other_customer.id,
            product_code="OTHER-MOBILE-001",
            customer_material_code="OM001",
            product_name="其他客户产品",
        )
        db.add_all([product, product_two, other_product])
        db.flush()
        floor1 = WarehouseFloor(
            floor_code="1F",
            floor_name="一楼成品区",
            floor_number=1,
            construction_status="enabled",
        )
        floor3 = WarehouseFloor(
            floor_code="3F",
            floor_name="三楼成品仓",
            floor_number=3,
            construction_status="enabled",
        )
        db.add_all([floor1, floor3])
        db.flush()
        finished_area = WarehouseArea(
            floor_id=floor3.id,
            area_code="C1",
            area_name="C1成品区",
            construction_status="enabled",
        )
        semi_area = WarehouseArea(
            floor_id=floor3.id,
            area_code="SF",
            area_name="纸板暂存区",
            construction_status="enabled",
        )
        ledger_area = WarehouseArea(
            floor_id=floor1.id,
            area_code="FG",
            area_name="成品台账区",
            construction_status="enabled",
        )
        db.add_all([finished_area, semi_area, ledger_area])
        db.flush()
        db.add(
            WarehouseAreaStoragePolicy(
                area_id=finished_area.id,
                map_feature_id="mobile-zone-c1",
                allowed_inventory_types_json='["finished"]',
                storage_layout="pallet_ground",
                status="published",
                published_map_revision="mobile-map-v1",
                version=1,
            )
        )
        mapped_location = WarehouseLocation(
            location_code="C1-L01",
            location_name="三楼 C1 第一货位",
            warehouse_type="finished",
            warehouse_floor=3,
            area_code="C1",
            storage_type="ground",
            placement_status="placed",
            source_version="TWIN_V1",
        )
        ledger_location = WarehouseLocation(
            location_code="FG-L01",
            location_name="一楼成品台账货位",
            warehouse_type="finished",
            warehouse_floor=1,
            area_code="FG",
            storage_type="rack",
            placement_status="placed",
        )
        semi_location = WarehouseLocation(
            location_code="SF-TEMP",
            location_name="纸板待归位",
            warehouse_type="semi_finished",
            warehouse_floor=3,
            area_code="SF",
            storage_type="temporary_aisle",
            placement_status="unplaced",
            is_temporary=True,
        )
        db.add_all([mapped_location, ledger_location, semi_location])
        db.flush()
        db.add(
            Floor3LocationLayout(
                location_id=mapped_location.id,
                left_pct=Decimal("10"),
                top_pct=Decimal("10"),
                width_pct=Decimal("10"),
                height_pct=Decimal("10"),
                source_type="manual",
            )
        )
        ground_plan = WarehouseGroundLayoutPlan(
            area_id=finished_area.id,
            status="published",
            target_slot_count=1,
            numbering_origin="south",
            row_direction="from_aisle_inward",
            slot_direction="left_to_right",
            row_start_no=1,
            slot_start_no=1,
            draft_map_revision="mobile-map-v1",
            published_map_revision="mobile-map-v1",
            preview_fingerprint="a" * 64,
            version=1,
            publish_idempotency_key="mobile-ground-publish",
            publish_request_hash="b" * 64,
            updated_by=admin.id,
            published_by=admin.id,
            published_at=now,
        )
        db.add(ground_plan)
        db.flush()
        db.add(
            WarehouseGroundLayoutSlot(
                plan_id=ground_plan.id,
                location_id=mapped_location.id,
                route_sequence=1,
                row_no=1,
                slot_no=1,
                x_mm=Decimal("1000"),
                y_mm=Decimal("1000"),
                width_mm=1200,
                depth_mm=1000,
            )
        )

        finished_one = InventoryLot(
            lot_number="FG-MOBILE-001",
            inventory_type="finished",
            warehouse_location_id=mapped_location.id,
            quantity_available=7,
            quantity_reserved=3,
            unit="boxes",
            status="active",
            source_type="manual",
            stock_date=date(2026, 8, 1),
            last_movement_at=now,
        )
        finished_two = InventoryLot(
            lot_number="FG-MOBILE-002",
            inventory_type="finished",
            warehouse_location_id=ledger_location.id,
            quantity_available=4,
            quantity_reserved=0,
            unit="boxes",
            status="active",
            source_type="manual",
            stock_date=date(2026, 8, 2),
            last_movement_at=now,
        )
        general_finished = InventoryLot(
            lot_number="FG-MOBILE-GENERAL",
            inventory_type="finished",
            warehouse_location_id=ledger_location.id,
            quantity_available=99,
            unit="boxes",
            status="active",
            source_type="manual",
            stock_date=date(2026, 8, 2),
            last_movement_at=now,
        )
        semi = InventoryLot(
            lot_number="SF-MOBILE-001",
            inventory_type="semi_finished",
            warehouse_location_id=semi_location.id,
            quantity_available=8,
            quantity_reserved=2,
            unit="sheets",
            status="active",
            source_type="replenishment",
            stock_date=date(2026, 8, 2),
            last_movement_at=now,
        )
        unbound_semi = InventoryLot(
            lot_number="SF-MOBILE-UNBOUND",
            inventory_type="semi_finished",
            warehouse_location_id=semi_location.id,
            quantity_available=88,
            unit="sheets",
            status="active",
            source_type="manual",
            stock_date=date(2026, 8, 2),
            last_movement_at=now,
        )
        db.add_all(
            [finished_one, finished_two, general_finished, semi, unbound_semi]
        )
        db.flush()
        for lot, is_general in (
            (finished_one, False),
            (finished_two, False),
            (general_finished, True),
        ):
            db.add(
                FinishedGoodsInventoryDetail(
                    inventory_lot_id=lot.id,
                    owner_customer_id=None if is_general else customer.id,
                    owner_customer_name_snapshot=None if is_general else customer.name,
                    is_general=is_general,
                    product_id=product.id,
                    inventory_code_snapshot=product.product_code,
                    product_name_snapshot=product.product_name,
                )
            )
        for lot in (semi, unbound_semi):
            db.add(
                SemiFinishedInventoryDetail(
                    inventory_lot_id=lot.id,
                    owner_customer_id=customer.id,
                    owner_customer_name_snapshot=customer.name,
                    material_code_snapshot="K=A",
                    normalized_material_code="K=A",
                    layer_count=3,
                    flute_type="B",
                    board_length_mm=800,
                    board_width_mm=600,
                    component_type="whole",
                    pieces_per_box=1,
                    stock_yield_per_sheet=1,
                    sheet_type="raw_board",
                )
            )
        db.add(
            SemiFinishedLotAllowedProduct(
                inventory_lot_id=semi.id,
                product_id=product.id,
                confirmed_at=now,
            )
        )
        pallet = InventoryPallet(
            pallet_code="PLT-MOBILE-001",
            location_id=mapped_location.id,
            status="active",
            is_current=True,
        )
        db.add(pallet)
        db.flush()
        db.add(
            InventoryPalletItem(
                pallet_id=pallet.id,
                inventory_lot_id=finished_one.id,
                customer_id=customer.id,
                product_id=product.id,
                inventory_code=product.product_code,
                product_name=product.product_name,
                item_type="finished",
                quantity=Decimal("10"),
                unit="boxes",
                match_status="matched",
            )
        )
        db.add(
            InventoryReservation(
                reservation_number="RSV-MOBILE-001",
                inventory_lot_id=finished_one.id,
                reservation_type="finished_order",
                reserved_stock_quantity=3,
                credited_requirement_quantity=3,
                consumed_stock_quantity=0,
                released_stock_quantity=0,
                consumed_requirement_quantity=0,
                released_requirement_quantity=0,
                status="active",
                idempotency_key="mobile-reservation-001",
            )
        )
        db.commit()
        ids = {
            "product": product.id,
            "product_two": product_two.id,
            "other_product": other_product.id,
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(mobile_erp_router, prefix="/api/mobile/erp")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app, ids, factory
    finally:
        engine.dispose()


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "123456"},
    )
    assert response.status_code == 200, response.text


def test_product_search_requires_explicit_selection_and_preserves_units(
    mobile_erp_app,
) -> None:
    app, ids, _factory = mobile_erp_app
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        search = client.get(
            "/api/mobile/erp/products",
            params={"q": "匿名三层", "include_zero": "true"},
        )
        assert search.status_code == 200, search.text
        payload = search.json()
        assert payload["count"] == 2
        assert payload["requires_selection"] is True
        assert payload["auto_selected"] is False
        assert search.headers["cache-control"] == "private, no-store"

        detail = client.get(
            f"/api/mobile/erp/products/{ids['product']}/inventory"
        )
        assert detail.status_code == 200, detail.text
        data = detail.json()
        assert data["read_only"] is True
        assert data["inventory"]["finished"] == {
            "unit": "只",
            "quantity_total": 14,
            "quantity_available": 11,
            "quantity_reserved": 3,
            "quantity_pending_pick": 3,
            "position_count": 2,
            "positions": data["inventory"]["finished"]["positions"],
        }
        assert data["inventory"]["semi_finished"]["unit"] == "张"
        assert data["inventory"]["semi_finished"]["quantity_total"] == 10
        assert data["inventory"]["semi_finished"]["quantity_available"] == 8
        assert data["inventory"]["semi_finished"]["quantity_reserved"] == 2
        assert data["inventory"]["semi_finished"]["position_count"] == 1
        assert data["inventory"]["raw_material"]["state"] == "not_configured"
        assert "quantity_total" not in data["inventory"]["raw_material"]


def test_product_search_defaults_to_real_stock_and_returns_authoritative_summary(
    mobile_erp_app,
) -> None:
    app, ids, _factory = mobile_erp_app
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        search = client.get("/api/mobile/erp/products", params={"q": "NMJ"})
        assert search.status_code == 200, search.text
        payload = search.json()
        assert payload["matched_product_count"] == 2
        assert payload["zero_stock_match_count"] == 1
        assert [row["id"] for row in payload["items"]] == [ids["product"]]
        summary = payload["items"][0]["inventory_summary"]
        assert summary == {
            "has_stock": True,
            "position_count": 3,
            "finished": {
                "unit": "只",
                "quantity_total": 14,
                "quantity_available": 11,
                "quantity_reserved": 3,
                "quantity_pending_pick": 3,
            },
            "semi_finished": {
                "unit": "张",
                "quantity_total": 10,
                "quantity_available": 8,
                "quantity_reserved": 2,
                "quantity_pending_pick": 0,
            },
        }


def test_admin_can_include_zero_stock_but_scoped_employee_cannot_expand_search(
    mobile_erp_app,
) -> None:
    app, ids, _factory = mobile_erp_app
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        search = client.get(
            "/api/mobile/erp/products",
            params={"q": "NMJ", "include_zero": "true"},
        )
        assert search.status_code == 200, search.text
        rows = {row["id"]: row for row in search.json()["items"]}
        assert set(rows) == {ids["product"], ids["product_two"]}
        assert rows[ids["product_two"]]["inventory_summary"]["has_stock"] is False
        assert rows[ids["product_two"]]["inventory_summary"]["position_count"] == 0

    with TestClient(app) as client:
        _login(client, "mobile-scoped")
        denied = client.get(
            "/api/mobile/erp/products",
            params={"q": "NMJ", "include_zero": "true"},
        )
        assert denied.status_code == 403


def test_dimension_search_accepts_common_x_separators_without_mutating_product(
    mobile_erp_app,
) -> None:
    app, ids, factory = mobile_erp_app
    from app.models.product import Product

    with TestClient(app) as client:
        _login(client, "mobile-admin")
        for keyword in ("420x310x260", "420X310X260", "420×310×260"):
            response = client.get("/api/mobile/erp/products", params={"q": keyword})
            assert response.status_code == 200, response.text
            assert [row["id"] for row in response.json()["items"]] == [ids["product"]]
    with factory() as db:
        product = db.get(Product, ids["product"])
        assert (product.length_mm, product.width_mm, product.height_mm) == (
            Decimal("420"),
            Decimal("310"),
            Decimal("260"),
        )


def test_inventory_returns_all_real_positions_and_only_real_map_links(
    mobile_erp_app,
) -> None:
    app, ids, _factory = mobile_erp_app
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        data = client.get(
            f"/api/mobile/erp/products/{ids['product']}/inventory"
        ).json()
    finished_positions = data["inventory"]["finished"]["positions"]
    mapped = next(row for row in finished_positions if row["location_code"] == "C1-L01")
    ledger = next(row for row in finished_positions if row["location_code"] == "FG-L01")
    semi = data["inventory"]["semi_finished"]["positions"][0]
    assert mapped["pallet_code"] == "PLT-MOBILE-001"
    assert mapped["map_status"] == "mapped"
    assert "readonly=1" in mapped["map_url"]
    assert f"location_id={mapped['location_id']}" in mapped["map_url"]
    assert f"lot_id={mapped['lot_id']}" in mapped["map_url"]
    assert ledger["map_status"] == "ledger_only"
    assert ledger["map_url"] is None
    assert semi["map_status"] == "unplaced"
    assert semi["map_url"] is None
    assert data["mapped_position_count"] == 1
    assert "source=mobile-product-all" in data["map_url"]
    assert f"customer_id={data['product']['customer_id']}" in data["map_url"]
    assert "keyword=MB001" in data["map_url"]


def test_customer_scope_and_warehouse_permission_fail_closed(mobile_erp_app) -> None:
    app, ids, _factory = mobile_erp_app
    with TestClient(app) as client:
        _login(client, "mobile-scoped")
        visible = client.get("/api/mobile/erp/products", params={"q": "MB"})
        assert visible.status_code == 200
        assert {row["id"] for row in visible.json()["items"]} == {ids["product"]}
        hidden = client.get("/api/mobile/erp/products", params={"q": "OTHER"})
        assert hidden.status_code == 200
        assert hidden.json()["items"] == []
        direct = client.get(
            f"/api/mobile/erp/products/{ids['other_product']}/inventory"
        )
        assert direct.status_code == 404

    with TestClient(app) as client:
        _login(client, "mobile-picker")
        denied = client.get("/api/mobile/erp/products", params={"q": "MB"})
        assert denied.status_code == 403


def test_read_only_queries_do_not_change_inventory(mobile_erp_app) -> None:
    app, ids, factory = mobile_erp_app
    from app.models.warehouse_inventory import InventoryLot

    with factory() as db:
        before = (
            db.scalar(select(func.count(InventoryLot.id))),
            db.scalar(select(func.sum(InventoryLot.quantity_available))),
            db.scalar(select(func.sum(InventoryLot.quantity_reserved))),
        )
    with TestClient(app) as client:
        _login(client, "mobile-admin")
        assert client.get("/api/mobile/erp/products", params={"q": "MB"}).status_code == 200
        assert client.get(
            f"/api/mobile/erp/products/{ids['product']}/inventory"
        ).status_code == 200
    with factory() as db:
        after = (
            db.scalar(select(func.count(InventoryLot.id))),
            db.scalar(select(func.sum(InventoryLot.quantity_available))),
            db.scalar(select(func.sum(InventoryLot.quantity_reserved))),
        )
    assert after == before


def test_mobile_page_is_compact_read_only_and_keeps_map_return_state() -> None:
    for text in (
        "天明 ERP 手机版",
        "今天先做什么",
        "仓库",
        "成品库存",
        "客户专用纸板备料",
        "日常先按客户、存货编码、产品名称或规格查货",
        "只有需要浏览整仓时再打开地图",
        "返回产品",
        "输入客户、存货编码、产品名称或规格",
        "包含零库存产品",
        "查看位置",
        "当前无在库数量",
        "地图定位全部",
        "更多功能",
    ):
        assert text in MOBILE_HTML
    assert "overflow-x: hidden" in MOBILE_HTML
    assert "width: min(100%, 480px)" in MOBILE_HTML
    assert "new AbortController()" in MOBILE_HTML
    assert "generation !== state.searchGeneration" in MOBILE_HTML
    assert "generation !== state.detailGeneration" in MOBILE_HTML
    assert "include_zero" in MOBILE_HTML
    assert "product.inventory_summary" in MOBILE_HTML
    assert "openProductMap(data)" in MOBILE_HTML
    assert 'urlCustomerId=Number(params.get("customer_id"))' in WAREHOUSE_HTML
    assert 'await loadFloor3Locations(true)' in WAREHOUSE_HTML
    assert "Search text, candidates and selected product remain in memory" in MOBILE_HTML
    assert "readonly=1" not in MOBILE_HTML  # map URLs come only from the trusted API.
    assert MOBILE_HTML.count('method: "POST"') == 2
    assert 'fetch("/api/auth/logout"' in MOBILE_HTML
    assert 'method: "PUT"' not in MOBILE_HTML
    assert 'method: "DELETE"' not in MOBILE_HTML


def test_mobile_page_route_and_api_router_are_registered() -> None:
    assert 'from app.api.mobile_erp import router as mobile_erp_router' in MAIN_SOURCE
    assert 'route.path == "/mobile/erp.html"' in MAIN_SOURCE
    assert '"/api/mobile/erp"' in MAIN_SOURCE
    assert '"mobile_erp.html"' in MAIN_SOURCE


def test_mobile_inline_javascript_syntax() -> None:
    node_binary = shutil.which("node")
    assert node_binary, "Node.js is required for inline JavaScript syntax checks"
    scripts = [
        script
        for script in re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>",
            MOBILE_HTML,
            flags=re.DOTALL,
        )
        if script.strip()
    ]
    assert scripts
    for script in scripts:
        result = subprocess.run(
            [node_binary, "--check"],
            input=script,
            text=True,
            encoding="utf-8",
            capture_output=True,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            check=False,
        )
        assert result.returncode == 0, result.stderr
