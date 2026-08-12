from __future__ import annotations

from collections.abc import Generator
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture()
def plate_app(tmp_path: Path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.products import router as products_router
    from app.api.warehouse import router as warehouse_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "printing-plates.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add_all(
            [
                User(
                    username=role,
                    password_hash=hash_password("RolePass123!"),
                    role=role,
                    real_name=role,
                    display_name=role,
                    must_change_password=False,
                )
                for role in ("admin", "workshop")
            ]
        )
        db.add_all(
            [
                Customer(
                    customer_number=9961,
                    customer_code="PLATE-A",
                    name="挂板测试客户甲",
                    payment_term_days=30,
                    credit_limit=Decimal("100000"),
                ),
                Customer(
                    customer_number=9962,
                    customer_code="PLATE-B",
                    name="挂板测试客户乙",
                    payment_term_days=30,
                    credit_limit=Decimal("100000"),
                ),
            ]
        )
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(products_router, prefix="/api/master/products")
    app.include_router(warehouse_router, prefix="/api/warehouse")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    yield app, factory
    engine.dispose()


def _login(client: TestClient, role: str = "admin") -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": role, "password": "RolePass123!"},
    )
    assert response.status_code == 200, response.text


def _create_plate(
    client: TestClient,
    *,
    customer_id: int = 1,
    color: str,
    position: int,
) -> dict:
    response = client.post(
        "/api/warehouse/printing-plates",
        json={
            "customer_id": customer_id,
            "plate_name": f"{color}测试挂板",
            "color_name": color,
            "rack_location": f"1f-pl-r1-l1-p{position}",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _product_payload(**overrides) -> dict:
    payload = {
        "customer_id": 1,
        "product_code": "PLATE-PRODUCT-001",
        "customer_material_code": "PLATE-MATERIAL-001",
        "product_name": "挂板测试纸箱",
        "box_category": "normal",
        "print_content": "单色印刷",
        "printing_plate_mode": "plate",
    }
    payload.update(overrides)
    return payload


def test_plate_codes_locations_and_product_colour_count_are_fail_closed(
    plate_app,
) -> None:
    app, _factory = plate_app
    with TestClient(app) as client:
        _login(client)
        red = _create_plate(client, color="红色", position=1)
        blue = _create_plate(client, color="蓝色", position=2)
        foreign = _create_plate(client, customer_id=2, color="黑色", position=3)
        green = _create_plate(client, color="绿色", position=4)

        assert red["plate_code"] == "PL000001"
        assert blue["plate_code"] == "PL000002"
        assert red["rack_location"] == "1F-PL-R01-L1-P01"
        assert "第1层" in red["location_guide"]["prompt"]

        missing_second = client.post(
            "/api/master/products",
            json=_product_payload(
                print_content="双色印刷",
                printing_plate_1_id=red["id"],
            ),
        )
        assert missing_second.status_code == 422
        assert "选择 2 块挂板" in missing_second.text

        wrong_customer = client.post(
            "/api/master/products",
            json=_product_payload(printing_plate_1_id=foreign["id"]),
        )
        assert wrong_customer.status_code == 400
        assert "不属于当前客户" in wrong_customer.text

        created = client.post(
            "/api/master/products",
            json=_product_payload(
                print_content="双色印刷",
                printing_plate_1_id=red["id"],
                printing_plate_2_id=blue["id"],
                plate_alignment_value_mm="1.25",
                plate_mount_value_mm="2.50",
                machine_set_length_mm="500.00",
                machine_set_width_mm="320.00",
                machine_set_height_mm="280.50",
            ),
        )
        assert created.status_code == 201, created.text
        product = created.json()
        assert [row["plate_code"] for row in product["printing_plates"]] == [
            "PL000001",
            "PL000002",
        ]
        assert product["machine_set_height_mm"] == "280.50"
        reopened = client.get(f"/api/master/products/{product['id']}")
        assert reopened.status_code == 200, reopened.text
        assert [row["plate_code"] for row in reopened.json()["printing_plates"]] == [
            "PL000001",
            "PL000002",
        ]

        three_colour = client.post(
            "/api/master/products",
            json=_product_payload(
                product_code="PLATE-PRODUCT-003",
                customer_material_code="PLATE-MATERIAL-003",
                print_content="三色印刷",
                printing_plate_1_id=red["id"],
                printing_plate_2_id=blue["id"],
                printing_plate_3_id=green["id"],
            ),
        )
        assert three_colour.status_code == 201, three_colour.text
        assert len(three_colour.json()["printing_plates"]) == 3

        listed = client.get("/api/warehouse/printing-plates", params={"q": "PLATE-PRODUCT-001"})
        assert listed.status_code == 200, listed.text
        assert {row["plate_code"] for row in listed.json()["items"]} == {
            "PL000001",
            "PL000002",
        }

        located = client.get(
            "/api/warehouse/twin-operations/locate",
            params={"keyword": "PL000001", "search_type": "printing_plate"},
        )
        assert located.status_code == 200, located.text
        resources = located.json()["resources"]
        plate_resource = next(
            row for row in resources if row["resource_id"] == f"printing-plate:{red['id']}"
        )
        assert plate_resource["primary_code"] == "PL000001"
        assert plate_resource["location_code"] == "1F-PL-R01-L1-P01"
        assert plate_resource["feature_codes"] == ["ZONE-1F-PLATE-002"]
        assert plate_resource["map_status"] == "mapped"
        assert "PLATE-PRODUCT-001" in plate_resource["subtitle"]


def test_no_plate_is_default_and_clears_plate_only_settings(plate_app) -> None:
    app, _factory = plate_app
    with TestClient(app) as client:
        _login(client)
        red = _create_plate(client, color="红色", position=1)
        created = client.post(
            "/api/master/products",
            json=_product_payload(
                printing_colors="黑色",
                printing_plate_mode="no_plate",
                printing_plate_1_id=red["id"],
                plate_alignment_value_mm=3,
                machine_set_length_mm=510,
            ),
        )
        assert created.status_code == 201, created.text
        product = created.json()
        assert product["printing_plate_mode"] == "no_plate"
        assert product["printing_plate_1_id"] is None
        assert product["plate_alignment_value_mm"] is None
        assert product["machine_set_length_mm"] is None


def test_plate_move_is_previewed_versioned_idempotent_and_occupancy_safe(
    plate_app,
) -> None:
    app, factory = plate_app
    from app.models.printing_plate import PrintingPlateLocationMovement

    with TestClient(app) as client:
        _login(client)
        red = _create_plate(client, color="红色", position=1)
        blue = _create_plate(client, color="蓝色", position=2)

        occupied = client.post(
            "/api/warehouse/printing-plates/location-movement/preview",
            json={
                "plate_code": red["plate_code"],
                "target_location": blue["rack_location"],
            },
        )
        assert occupied.status_code == 200, occupied.text
        assert occupied.json()["can_confirm"] is False
        assert occupied.json()["occupancy_conflict"]["plate_code"] == blue["plate_code"]

        preview = client.post(
            "/api/warehouse/printing-plates/location-movement/preview",
            json={
                "plate_code": red["plate_code"],
                "target_location": "1F-PL-R01-L2-P37",
            },
        )
        assert preview.status_code == 200, preview.text
        data = preview.json()
        assert data["can_confirm"] is True
        assert data["expected_version"] == 1
        assert data["target_location"] == "1F-PL-R01-L2-P37"

        payload = {
            "plate_code": red["plate_code"],
            "target_location": data["target_location"],
            "expected_version": data["expected_version"],
            "idempotency_key": "plate-move-idempotent-001",
            "source": "manual_input",
        }
        moved = client.post(
            "/api/warehouse/printing-plates/location-movement/confirm", json=payload
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["plate"]["rack_location"] == "1F-PL-R01-L2-P37"
        assert moved.json()["plate"]["location_version"] == 2
        assert moved.json()["idempotent_replay"] is False

        replayed = client.post(
            "/api/warehouse/printing-plates/location-movement/confirm", json=payload
        )
        assert replayed.status_code == 200, replayed.text
        assert replayed.json()["idempotent_replay"] is True

        stale = client.post(
            "/api/warehouse/printing-plates/location-movement/confirm",
            json={**payload, "target_location": "1F-PL-R01-L2-P38", "idempotency_key": "plate-move-stale-002"},
        )
        assert stale.status_code == 409
        assert "版本已变化" in stale.text

    with factory() as db:
        assert db.scalar(select(func.count(PrintingPlateLocationMovement.id))) == 1


def test_bound_plate_cannot_be_disabled_and_workshop_cannot_manage_master(
    plate_app,
) -> None:
    app, _factory = plate_app
    with TestClient(app) as admin:
        _login(admin)
        plate = _create_plate(admin, color="红色", position=1)
        created = admin.post(
            "/api/master/products",
            json=_product_payload(printing_plate_1_id=plate["id"]),
        )
        assert created.status_code == 201, created.text
        disabled = admin.put(
            f"/api/warehouse/printing-plates/{plate['id']}/status",
            json={"expected_version": plate["version"], "status": "inactive"},
        )
        assert disabled.status_code == 409
        assert "仍绑定常用箱" in disabled.text

    with TestClient(app) as workshop:
        _login(workshop, "workshop")
        listed = workshop.get("/api/warehouse/printing-plates")
        assert listed.status_code == 200, listed.text
        denied = workshop.post(
            "/api/warehouse/printing-plates",
            json={
                "customer_id": 1,
                "plate_name": "无权限",
                "color_name": "红色",
                "rack_location": "1F-PL-R01-L1-P09",
            },
        )
        assert denied.status_code == 403


def test_production_task_freezes_plate_codes_and_machine_values(plate_app) -> None:
    _app, factory = plate_app
    from app.models.order import Order, OrderItem
    from app.models.printing_plate import PrintingPlate
    from app.models.product import Product
    from app.services.production_workflow import (
        _task_printing_snapshot,
        create_or_refresh_production_task,
        list_production_tasks,
    )

    with factory() as db:
        red = PrintingPlate(
            plate_code="PL000101",
            customer_id=1,
            plate_name="任务红版",
            color_name="红色",
            rack_location="1F-PL-R01-L1-P21",
        )
        blue = PrintingPlate(
            plate_code="PL000102",
            customer_id=1,
            plate_name="任务蓝版",
            color_name="蓝色",
            rack_location="1F-PL-R01-L1-P22",
        )
        db.add_all([red, blue])
        db.flush()
        product = Product(
            customer_id=1,
            product_code="TASK-PLATE-001",
            customer_material_code="TASK-PLATE-001",
            product_name="生产挂板快照纸箱",
            box_category="normal",
            print_content="双色印刷",
            printing_plate_mode="plate",
            printing_plate_1_id=red.id,
            printing_plate_2_id=blue.id,
            plate_alignment_value_mm=Decimal("1.25"),
            plate_mount_value_mm=Decimal("2.50"),
            machine_set_length_mm=Decimal("500.00"),
            machine_set_width_mm=Decimal("320.00"),
            machine_set_height_mm=Decimal("280.50"),
        )
        db.add(product)
        db.flush()
        order = Order(
            order_number="P1-16D4-TASK-001",
            customer_id=1,
            order_date=date.today(),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("10"),
        )
        db.add(order)
        db.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            item_order_number="P1-16D4-TASK-001-001",
            item_sequence=1,
            quantity=10,
            delivered_quantity=0,
            unit_price=Decimal("1"),
            subtotal=Decimal("10"),
            material_status="received",
            snapshot_product_name=product.product_name,
            snapshot_product_code=product.product_code,
            requisition_status="未报料",
            special_process="无",
        )
        db.add(item)
        db.flush()

        task = create_or_refresh_production_task(db, item.id)
        snapshot = _task_printing_snapshot(task)
        assert snapshot["printing_plate_codes"] == ["PL000101", "PL000102"]
        assert snapshot["plate_alignment_value_mm"] == Decimal("1.25")
        assert snapshot["plate_mount_value_mm"] == Decimal("2.50")
        assert snapshot["machine_set_height_mm"] == Decimal("280.50")
        task_rows = list_production_tasks(db, allowed_customer_ids=None)
        assert task_rows[0]["printing_plate_codes"] == ["PL000101", "PL000102"]
        assert task_rows[0]["printing_plates"] == [
            {"plate_code": "PL000101", "color_name": "红色"},
            {"plate_code": "PL000102", "color_name": "蓝色"},
        ]
        assert task_rows[0]["printing_instruction"] == "按挂板编号安装并核对机器设定值"

        product.printing_plate_1_id = blue.id
        product.printing_plate_2_id = red.id
        product.machine_set_height_mm = Decimal("999.00")
        refreshed = create_or_refresh_production_task(db, item.id)
        unchanged = _task_printing_snapshot(refreshed)
        assert unchanged["printing_plate_codes"] == ["PL000101", "PL000102"]
        assert unchanged["machine_set_height_mm"] == Decimal("280.50")


def test_product_history_restore_cannot_rebind_inactive_or_foreign_plate(
    plate_app,
) -> None:
    _app, factory = plate_app
    from app.api.master_data_versions import _validate_product_printing_plate_restore
    from app.models.printing_plate import PrintingPlate
    from app.models.product import Product

    with factory() as db:
        inactive = PrintingPlate(
            plate_code="PL000201",
            customer_id=1,
            plate_name="已停用红版",
            color_name="红色",
            rack_location="1F-PL-R01-L1-P31",
            status="inactive",
        )
        foreign = PrintingPlate(
            plate_code="PL000202",
            customer_id=2,
            plate_name="其他客户蓝版",
            color_name="蓝色",
            rack_location="1F-PL-R01-L1-P32",
        )
        product = Product(
            customer_id=1,
            product_code="RESTORE-PLATE-001",
            customer_material_code="RESTORE-PLATE-001",
            product_name="历史恢复门禁产品",
            box_category="normal",
        )
        db.add_all([inactive, foreign, product])
        db.flush()

        with pytest.raises(ValueError, match="不是启用状态"):
            _validate_product_printing_plate_restore(
                db,
                product=product,
                updates={
                    "print_content": "单色印刷",
                    "printing_plate_mode": "plate",
                    "printing_plate_1_id": inactive.id,
                },
            )
        with pytest.raises(ValueError, match="不属于当前客户"):
            _validate_product_printing_plate_restore(
                db,
                product=product,
                updates={
                    "print_content": "单色印刷",
                    "printing_plate_mode": "plate",
                    "printing_plate_1_id": foreign.id,
                },
            )


def test_printing_plate_frontend_and_migration_contract() -> None:
    index = Path("static/index.html").read_text(encoding="utf-8")
    warehouse = Path("static/warehouse.html").read_text(encoding="utf-8")
    mobile = Path("static/mobile_erp.html").read_text(encoding="utf-8")
    migration = Path(
        "alembic/versions/dm95v8x9z84_printing_plate_ledger.py"
    ).read_text(encoding="utf-8")

    for marker in (
        '<option value="挂板印刷">挂板印刷</option>',
        "productForm.printing_plate_mode==='plate'",
        "printing_plate_1_id",
        "printing_plate_2_id",
        "printing_plate_3_id",
        "productPrintingPlateError",
        "对版值(mm)",
        "挂版值(mm)",
        "机器设定箱长(mm)",
        "productionPrintingSetup(row)",
    ):
        assert marker in index
    for marker in (
        'data-tab="printing_plates"',
        "新增 / 编辑印刷挂板",
        "/api/warehouse/printing-plates/location-movement/preview",
        "/api/warehouse/printing-plates/location-movement/confirm",
        "1F-PL-R01-L1-P01",
    ):
        assert marker in warehouse
    assert "printing_plate_codes" in mobile
    assert 'revision: str = "dm95v8x9z84"' in migration
    assert 'down_revision: Union[str, Sequence[str], None] = "dl94v8x9z83"' in migration


def test_desktop_printing_plate_form_builds_confirmed_one_floor_codes() -> None:
    warehouse = Path("static/warehouse.html").read_text(encoding="utf-8")
    for marker in (
        'id="printingPlateRackSelect"',
        'id="printingPlateLevelSelect"',
        'id="printingPlatePositionNumber"',
        "function buildPrintingPlateLocation()",
        "1F-PL-R01-L${level}-P${positionCode(position)}",
    ):
        assert marker in warehouse
    assert 'id="printingPlateLocation" required' in warehouse


@pytest.mark.parametrize(
    "page_name",
    ("index.html", "mobile_erp.html", "warehouse.html"),
)
def test_printing_plate_pages_have_valid_inline_javascript(
    tmp_path: Path,
    page_name: str,
) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for frontend syntax validation"
    page = Path("static", page_name).read_text(encoding="utf-8")
    scripts = "\n".join(
        re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", page, flags=re.DOTALL)
    )
    target = tmp_path / f"p1-16d4-{page_name}.js"
    target.write_text(scripts, encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(target)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
