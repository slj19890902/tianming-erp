from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from tests.test_mold_tool_workflow import _login, mold_app


ROOT = Path(__file__).resolve().parents[1]
LIVE_PAGE = (ROOT / "static" / "mobile_mold_live.html").read_text(
    encoding="utf-8"
)


def test_reported_gaotai_shape_prints_frozen_shared_mold_identity(mold_app) -> None:
    from app.models.customer import Customer
    from app.models.mold_tool import MoldTool, MoldToolCustomer
    from app.models.product import Product

    app, factory = mold_app
    with factory() as db:
        customer = db.get(Customer, 1)
        assert customer is not None
        customer.chinese_short_name = "高泰"
        mold = MoldTool(
            mold_code="P198-GAOTAI-SHARED",
            mold_name="高泰 3D30268 19*19*16加强",
            label_name="3D30268",
            chinese_short_name="19*19*16加强",
            identity_status="frozen",
            rack_location="1F-M-R03-L2-G02",
        )
        db.add(mold)
        db.flush()
        db.add(
            MoldToolCustomer(
                mold_tool_id=mold.id,
                customer_id=customer.id,
                display_order=1,
            )
        )
        for product_code, report_length in (("3D30151", 800), ("3D30268", 795)):
            db.add(
                Product(
                    customer_id=customer.id,
                    product_code=product_code,
                    customer_material_code=f"3.{product_code}",
                    product_name="纸箱190*190*160",
                    length_mm=190,
                    width_mm=190,
                    height_mm=160,
                    report_length_mm=report_length,
                    report_width_mm=375,
                    flute_type="AB",
                    default_cutting_mode="一开一",
                    mold_tool_id=mold.id,
                )
            )
        db.commit()
        mold_id = mold.id

    with TestClient(app) as client:
        _login(client, "workshop")
        created = client.post(
            "/api/warehouse/molds/label-prints",
            json={
                "mold_ids": [mold_id],
                "source": "single",
                "template_version": "mold_80x40_v1",
                "idempotency_key": "p1-98-gaotai-label-0001",
            },
        )
        assert created.status_code == 200, created.text
        response = client.get(
            f"/api/warehouse/molds/{mold_id}/label",
            params={
                "template_version": "mold_80x40_v1",
                "print_job_id": created.json()["print_job_id"],
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["label_projection_mode"] == "shared_mold"
    assert body["label_customer_name"] == "高泰"
    assert body["label_customer_names"] == ["高泰"]
    assert body["label_mold_number"] == "3D30268 19*19*16加强"
    assert body["label_inventory_code"] == "3D30151 等2款"
    assert body["label_inventory_codes"] == ["3D30151", "3D30268"]
    assert body["label_shared_summary"] == "共用 2 款"
    assert body["label_products"] == [
        {"product_code": "3D30151", "product_name": "纸箱190*190*160"},
        {"product_code": "3D30268", "product_name": "纸箱190*190*160"},
    ]
    assert body["label_product_specification"] == "190 × 190 × 160"
    assert body["label_report_specifications"] == ["800 × 375", "795 × 375"]
    assert body["label_report_specification"] == "多款见扫码"
    assert body["label_flute_type"] == "AB"
    assert body["label_cutting_mode"] == "一开一"


def _create_shared_mold_tasks(factory) -> tuple[int, list[int]]:
    from app.core.time_contract import utc_now_naive
    from app.models.master_data_object_version import MasterDataObjectVersion
    from app.models.mold_tool import MoldTool
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.production import ProductionTask

    now = utc_now_naive()
    with factory() as db:
        mold = MoldTool(
            mold_code="P198-SHARED-MOLD",
            mold_name="共用模具任务定向测试",
            rack_location="1F-M-R03-L2-G02",
        )
        db.add(mold)
        db.flush()
        task_ids: list[int] = []
        for index, product_code in enumerate(("P198-BOX-A", "P198-BOX-B"), start=1):
            product = Product(
                customer_id=1,
                product_code=product_code,
                customer_material_code=product_code,
                product_name=f"共用模具纸箱{index}",
                box_category="die_cut",
                production_process="模切",
                length_mm=190,
                width_mm=190,
                height_mm=160,
                report_length_mm=705,
                report_width_mm=700,
                flute_type="B",
                default_cutting_mode="一开二",
                mold_tool_id=mold.id,
            )
            db.add(product)
            db.flush()
            snapshot = json.dumps(
                {"mold_tool_id": mold.id},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            db.add(
                MasterDataObjectVersion(
                    object_type="product",
                    object_id=product.id,
                    version=1,
                    action="create",
                    snapshot_schema_version=1,
                    snapshot_json=snapshot,
                    snapshot_sha256=hashlib.sha256(snapshot.encode()).hexdigest(),
                    changed_fields_json="{}",
                    change_set_id=f"p1-98-shared-{index}",
                    source="test",
                    created_at=now - timedelta(minutes=1),
                )
            )
            order = Order(
                order_number=f"SO-P198-{index:03d}",
                customer_id=1,
                order_date=date.today(),
                status="pending_production",
                total_amount=Decimal("0"),
            )
            db.add(order)
            db.flush()
            item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=30,
                delivered_quantity=0,
                unit_price=Decimal("0"),
                subtotal=Decimal("0"),
                material_status="pending",
                requisition_qty=30,
                snapshot_product_name=product.product_name,
                snapshot_product_code=product.product_code,
                snapshot_spec="190 × 190 × 160",
                special_process="一开二",
            )
            db.add(item)
            db.flush()
            task = ProductionTask(
                order_item_id=item.id,
                status="pending",
                planned_quantity=30,
                ordered_quantity_snapshot=30,
                material_received_quantity=0,
                material_input_quantity=0,
                created_at=now,
            )
            db.add(task)
            db.flush()
            task_ids.append(task.id)
        db.commit()
        return mold.id, task_ids


def test_task_context_projects_only_the_selected_shared_mold_product(mold_app) -> None:
    app, factory = mold_app
    mold_id, task_ids = _create_shared_mold_tasks(factory)

    with TestClient(app) as client:
        _login(client, "workshop")
        unscoped = client.get(f"/api/warehouse/molds/live/{mold_id}")
        assert unscoped.status_code == 200, unscoped.text
        assert unscoped.json()["task_context"] is None
        assert unscoped.json()["bindings"]["total"] == 2
        assert unscoped.json()["current_orders"]["total"] == 2

        for task_id, expected_code, hidden_code in (
            (task_ids[0], "P198-BOX-A", "P198-BOX-B"),
            (task_ids[1], "P198-BOX-B", "P198-BOX-A"),
        ):
            scoped = client.get(
                f"/api/warehouse/molds/live/{mold_id}",
                params={"production_task_id": task_id},
            )
            assert scoped.status_code == 200, scoped.text
            body = scoped.json()
            assert body["schema_version"] == "mold-live-v4"
            assert body["mode"] == "task_context"
            assert body["task_context"] == {
                "production_task_id": task_id,
                "product_id": body["bindings"]["items"][0]["product_id"],
                "product_code": expected_code,
            }
            assert body["current_orders"]["total"] == 1
            assert body["current_orders"]["items"][0]["product_code"] == expected_code
            assert body["bindings"]["total"] == 1
            assert body["bindings"]["items"][0]["product_code"] == expected_code
            assert hidden_code not in json.dumps(body, ensure_ascii=False)

        mismatch = client.get(
            f"/api/warehouse/molds/live/{mold_id}",
            params={"production_task_id": max(task_ids) + 999},
        )
        assert mismatch.status_code == 409
        assert "当前模具" in mismatch.json()["detail"]

        for index, task_id in enumerate(task_ids, start=1):
            recorded = client.post(
                f"/api/warehouse/molds/live/{mold_id}/scan-events",
                json={
                    "production_task_id": task_id,
                    "expected_mold_location_version": 1,
                    "idempotency_key": f"p1-98-shared-scan-{index:04d}",
                },
            )
            assert recorded.status_code == 200, recorded.text

        first_history = client.get(
            f"/api/warehouse/molds/live/{mold_id}",
            params={"production_task_id": task_ids[0]},
        ).json()["scan_history"]
        second_history = client.get(
            f"/api/warehouse/molds/live/{mold_id}",
            params={"production_task_id": task_ids[1]},
        ).json()["scan_history"]
        assert first_history["total"] == second_history["total"] == 1
        assert first_history["items"][0]["production_task_id"] == task_ids[0]
        assert second_history["items"][0]["production_task_id"] == task_ids[1]


def test_completed_mold_task_remains_until_its_order_item_is_fully_delivered(
    mold_app,
) -> None:
    app, factory = mold_app
    mold_id, task_ids = _create_shared_mold_tasks(factory)
    from app.models.order import OrderItem
    from app.models.production import ProductionTask

    with factory() as db:
        task = db.get(ProductionTask, task_ids[0])
        assert task is not None
        task.status = "completed"
        db.commit()

    with TestClient(app) as client:
        _login(client, "workshop")
        visible = client.get(f"/api/warehouse/molds/live/{mold_id}")
        assert visible.status_code == 200, visible.text
        completed = next(
            item
            for item in visible.json()["current_orders"]["items"]
            if item["production_task_id"] == task_ids[0]
        )
        assert completed["task_status"] == "completed"
        assert completed["task_status_label"] == "已完工待送"

    with factory() as db:
        task = db.get(ProductionTask, task_ids[0])
        assert task is not None
        item = db.get(OrderItem, task.order_item_id)
        assert item is not None
        item.delivered_quantity = item.quantity
        db.commit()

    with TestClient(app) as client:
        _login(client, "workshop")
        delivered = client.get(f"/api/warehouse/molds/live/{mold_id}")
        assert delivered.status_code == 200, delivered.text
        remaining_ids = {
            row["production_task_id"]
            for row in delivered.json()["current_orders"]["items"]
        }
        assert task_ids[0] not in remaining_ids
        assert task_ids[1] in remaining_ids


def test_mobile_scan_refetches_selected_task_context_and_hides_other_bindings() -> None:
    for marker in (
        "selectedTaskId",
        "production_task_id=${encodeURIComponent(selectedTaskId)}",
        "data.task_context",
        "当前订单存货已锁定，其他共用产品已隐藏",
        "scanEvent=result.event",
    ):
        assert marker in LIVE_PAGE
