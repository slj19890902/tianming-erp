from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.test_mold_tool_workflow import (
    _login,
    _protected_business_state,
    mold_app,
)


def _printable_mold(factory, *, suffix: str):
    from app.models.mold_tool import MoldTool
    from app.models.product import Product

    with factory() as db:
        mold = MoldTool(
            mold_code=f"PRINT-{suffix}",
            mold_name=f"模具联动测试客户PRINT{suffix}",
            rack_location=f"1F-M-R01-L1-G{int(suffix):02d}",
        )
        db.add(mold)
        db.flush()
        db.add(
            Product(
                customer_id=1,
                product_code=f"PRINT-P-{suffix}",
                customer_material_code=f"PRINT-P-{suffix}",
                product_name=f"模具标签产品 {suffix}",
                length_mm=430,
                width_mm=68,
                report_length_mm=880,
                report_width_mm=425,
                mold_tool_id=mold.id,
            )
        )
        db.commit()
        return mold.id, mold.mold_code, mold.rack_location


def test_single_and_batch_print_registration_drives_list_status(mold_app) -> None:
    from app.models.audit import OperationLog
    from app.models.mold_tool import MoldLabelPrintJob, MoldLabelPrintJobItem, MoldTool

    app, factory = mold_app
    first_id, first_code, first_location = _printable_mold(factory, suffix="1")
    second_id, _second_code, _second_location = _printable_mold(factory, suffix="2")
    third_id, _third_code, _third_location = _printable_mold(factory, suffix="3")
    protected_before = _protected_business_state(factory)

    with TestClient(app) as client:
        _login(client, "workshop")
        listed = client.get("/api/warehouse/molds", params={"q": "PRINT-"})
        assert listed.status_code == 200, listed.text
        initial = {row["id"]: row for row in listed.json()["items"]}
        assert initial[first_id]["label_print_status"] == {
            "printed": False,
            "label": "未打印",
            "last_printed_at": None,
            "last_printed_by": None,
            "print_count": 0,
        }

        single_payload = {
            "mold_ids": [first_id],
            "source": "single",
            "idempotency_key": "p1-58-single-print-0001",
        }
        single = client.post("/api/warehouse/molds/label-prints", json=single_payload)
        assert single.status_code == 200, single.text
        assert single.json()["replayed"] is False
        replay = client.post("/api/warehouse/molds/label-prints", json=single_payload)
        assert replay.status_code == 200, replay.text
        assert replay.json()["print_job_id"] == single.json()["print_job_id"]
        assert replay.json()["replayed"] is True

        batch_payload = {
            "mold_ids": [third_id, second_id],
            "source": "batch",
            "idempotency_key": "p1-58-batch-print-0001",
        }
        batch = client.post("/api/warehouse/molds/label-prints", json=batch_payload)
        assert batch.status_code == 200, batch.text
        assert batch.json()["mold_ids"] == [third_id, second_id]

        listed = client.get("/api/warehouse/molds", params={"q": "PRINT-"})
        assert listed.status_code == 200, listed.text
        current = {row["id"]: row for row in listed.json()["items"]}
        assert all(current[mold_id]["label_print_status"]["printed"] for mold_id in (first_id, second_id, third_id))
        assert current[first_id]["label_print_status"]["label"] == "已打印"
        assert current[first_id]["label_print_status"]["last_printed_by"] == "workshop"
        assert current[first_id]["label_print_status"]["print_count"] == 1

    assert _protected_business_state(factory) == protected_before
    with factory() as db:
        assert db.scalar(select(func.count(MoldLabelPrintJob.id))) == 2
        assert db.scalar(select(func.count(MoldLabelPrintJobItem.id))) == 3
        assert db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action_code == "mold.label.print"
            )
        ) == 2
        first_item = db.scalar(
            select(MoldLabelPrintJobItem).where(
                MoldLabelPrintJobItem.mold_tool_id == first_id
            )
        )
        assert first_item is not None
        assert first_item.mold_code_snapshot == first_code
        assert first_item.rack_location_snapshot == first_location
        mold = db.get(MoldTool, first_id)
        assert mold is not None
        assert mold.rack_location == first_location
        assert mold.location_version == 1


def test_print_registration_fails_closed_and_rejects_key_reuse(mold_app) -> None:
    from app.models.audit import OperationLog
    from app.models.mold_tool import MoldLabelPrintJob, MoldLabelPrintJobItem

    app, factory = mold_app
    valid_id, _code, _location = _printable_mold(factory, suffix="4")
    invalid_id, _code, _location = _printable_mold(factory, suffix="5")
    with factory() as db:
        from app.models.mold_tool import MoldTool

        invalid = db.get(MoldTool, invalid_id)
        assert invalid is not None
        invalid.is_active = False
        db.commit()

    before = _protected_business_state(factory)
    with TestClient(app) as client:
        _login(client, "workshop")
        rejected = client.post(
            "/api/warehouse/molds/label-prints",
            json={
                "mold_ids": [valid_id, invalid_id],
                "source": "batch",
                "idempotency_key": "p1-58-invalid-batch-0001",
            },
        )
        assert rejected.status_code == 409, rejected.text

        payload = {
            "mold_ids": [valid_id],
            "source": "single",
            "idempotency_key": "p1-58-key-conflict-0001",
        }
        created = client.post("/api/warehouse/molds/label-prints", json=payload)
        assert created.status_code == 200, created.text
        conflict = client.post(
            "/api/warehouse/molds/label-prints",
            json={**payload, "mold_ids": [invalid_id]},
        )
        assert conflict.status_code == 409, conflict.text
        assert "另一组模具" in conflict.json()["detail"]

    assert _protected_business_state(factory) == before
    with factory() as db:
        assert db.scalar(select(func.count(MoldLabelPrintJob.id))) == 1
        assert db.scalar(select(func.count(MoldLabelPrintJobItem.id))) == 1
        assert db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action_code == "mold.label.print"
            )
        ) == 1


def test_mold_print_status_frontend_uses_registered_single_and_batch_actions() -> None:
    warehouse = (Path(__file__).resolve().parents[1] / "static/warehouse.html").read_text(
        encoding="utf-8"
    )
    for marker in (
        "未打印",
        "label_print_status",
        "registerAndOpenMoldLabels",
        'registerAndOpenMoldLabels("single"',
        'registerAndOpenMoldLabels("batch"',
        'api("/api/warehouse/molds/label-prints"',
        "上一次标签打印结果尚未核对",
        "复用同一凭证核对",
        "attempt?.committed",
        "只会打开页面，不会重复登记",
    ):
        assert marker in warehouse
    assert "正常启用与已打印" not in warehouse
    assert re.search(r'onclick="openMoldLabel\(\$\{row\.id\}\)"', warehouse)
    assert "window.open('/mold-label.html?mold_id=${row.id}'" not in warehouse
