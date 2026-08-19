from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from test_p1_21d_mobile_production_materials import (
    _login,
    mobile_production_app,
)


ROOT = Path(__file__).resolve().parents[1]
MOBILE_HTML = (ROOT / "static" / "mobile_erp.html").read_text(encoding="utf-8")
MOLD_HTML = (ROOT / "static" / "mobile_mold_lookup.html").read_text(encoding="utf-8")


def _prepare_station_facts(factory) -> None:
    from app.models.mold_tool import MoldTool
    from app.models.product import Product
    from app.models.production import ProductionTask

    with factory() as db:
        product = db.scalar(
            select(Product).where(Product.product_code == "MOBILE-PROD-01")
        )
        mold = db.scalar(select(MoldTool).where(MoldTool.mold_code == "MOBILE-MOLD-01"))
        assert product is not None and mold is not None
        product.length_mm = 420
        product.width_mm = 310
        product.height_mm = 120
        product.printing_colors = "红,黑"
        mold.is_active = False
        tasks = list(
            db.scalars(
                select(ProductionTask).where(ProductionTask.status == "pending")
            ).all()
        )
        for task in tasks:
            task.printing_plate_mode_snapshot = "plate"
            task.print_content_snapshot = "匿名品牌标识"
            task.printing_plate_codes_snapshot = json.dumps(
                ["PLATE-RED", "PLATE-BLACK"], ensure_ascii=False
            )
            task.printing_plate_details_snapshot = json.dumps(
                [
                    {"plate_code": "PLATE-RED", "color_name": "红"},
                    {"plate_code": "PLATE-BLACK", "color_name": "黑"},
                ],
                ensure_ascii=False,
            )
        db.commit()


def test_station_pages_are_exactly_authorized_scoped_and_read_only(
    mobile_production_app,
) -> None:
    app, factory, _ids = mobile_production_app
    from app.models.audit import OperationLog
    from app.models.production import ProductionTask

    _prepare_station_facts(factory)
    with factory() as db:
        before_tasks = db.scalar(select(func.count(ProductionTask.id))) or 0

    with TestClient(app) as client:
        assert client.get(
            "/api/mobile/erp/production/tasks",
            params={"station": "printing"},
        ).status_code == 401
        _login(client, "mobile-workshop")
        with factory() as db:
            before_logs = db.scalar(select(func.count(OperationLog.id))) or 0

        printing = client.get(
            "/api/mobile/erp/production/tasks",
            params={"station": "printing", "page": 1, "page_size": 20},
        )
        assert printing.status_code == 200, printing.text
        assert printing.headers["cache-control"] == "private, no-store"
        print_payload = printing.json()
        assert print_payload["station"] == "printing"
        assert print_payload["read_only"] is True
        assert print_payload["page_size"] == 20
        assert print_payload["total"] == 2
        assert len(print_payload["items"]) == 2
        print_task = print_payload["items"][0]
        assert print_task["customer_name"] == "匿名车间客户"
        assert print_task["carton_length_mm"] == "420"
        assert print_task["carton_width_mm"] == "310"
        assert print_task["carton_height_mm"] == "120"
        assert print_task["report_length_mm"] == "800"
        assert print_task["report_width_mm"] == "600"
        assert print_task["crease_values_mm"] == ["120", "310", "120"]
        assert print_task["print_content"] == "匿名品牌标识"
        assert print_task["printing_colors"] == ["红", "黑"]
        assert print_task["drawing_path"].startswith(
            "/api/mobile/erp/production/tasks/"
        )
        assert print_task["drawing_path"].endswith("/drawing")
        assert "material" not in print_task
        assert "mold_location" not in print_task

        die_cut = client.get(
            "/api/mobile/erp/production/tasks",
            params={"station": "die_cut", "page": 99, "page_size": 20},
        )
        assert die_cut.status_code == 200, die_cut.text
        die_payload = die_cut.json()
        assert die_payload["page"] == 1
        assert die_payload["total"] == 2
        die_task = die_payload["items"][0]
        assert die_task["material"] == "SECRET-SUPPLIER-MATERIAL"
        assert die_task["flute_type"] == "B"
        assert die_task["mold_code"] == "MOBILE-MOLD-01"
        assert die_task["mold_location"] == "M1-R02"
        assert die_task["mold_is_active"] is False
        assert "禁止直接生产" in die_task["mold_warning"]
        assert die_task["mold_map_url"].startswith("/mobile/mold-lookup?q=")
        assert die_task["mold_map_url"].endswith("&readonly=1")
        assert "print_content" not in die_task
        assert "snapshot_supplier_name" not in die_cut.text
        assert "unit_price" not in die_cut.text
        assert "cost" not in die_cut.text

    with factory() as db:
        assert (db.scalar(select(func.count(ProductionTask.id))) or 0) == before_tasks
        assert (db.scalar(select(func.count(OperationLog.id))) or 0) == before_logs


def test_printing_and_die_cut_endpoints_do_not_share_permission(
    mobile_production_app,
) -> None:
    app, factory, _ids = mobile_production_app
    from app.models.access_control import UserPermissionOverride
    from app.models.user import User

    with factory() as db:
        employee = db.scalar(select(User).where(User.username == "mobile-workshop"))
        assert employee is not None
        db.add(
            UserPermissionOverride(
                user_id=employee.id,
                permission_code="production.printing.view",
                is_allowed=False,
                granted_by=employee.id,
            )
        )
        db.commit()

    with TestClient(app) as client:
        _login(client, "mobile-workshop")
        printing = client.get(
            "/api/mobile/erp/production/tasks",
            params={"station": "printing"},
        )
        assert printing.status_code == 403
        assert "印刷工位" in printing.json()["detail"]
        die_cut = client.get(
            "/api/mobile/erp/production/tasks",
            params={"station": "die_cut"},
        )
        assert die_cut.status_code == 200


def test_mobile_station_ui_is_paged_latest_wins_and_read_only() -> None:
    for marker in (
        "印刷工位",
        "模切工位",
        "每页 20 条",
        'page_size: "20"',
        "/api/mobile/erp/production/tasks?",
        "productionStationGeneration",
        "仍保留上次成功结果",
        "查看模具位置",
        "查看图纸",
    ):
        assert marker in MOBILE_HTML
    assert "generation !== state.productionStationGeneration" in MOBILE_HTML
    assert 'button.addEventListener("click", () => retry());' in MOBILE_HTML
    assert (
        'showStatus("productionStationState", "当前没有待生产任务。", "empty", '
        "() => loadProductionStation(1));"
    ) in MOBILE_HTML
    assert '"empty", loadProductionStation);' not in MOBILE_HTML
    assert "const parsedPage = Number(page);" in MOBILE_HTML
    assert "page: String(safePage)" in MOBILE_HTML
    assert MOBILE_HTML.count('method: "POST"') == 2
    assert "/api/mobile/erp/production/tasks/" not in MOBILE_HTML.split("apiPost", 1)[-1]
    assert 'fetch("/api/auth/logout"' in MOBILE_HTML
    assert 'method: "PUT"' not in MOBILE_HTML
    assert 'method: "DELETE"' not in MOBILE_HTML
    assert 'get("readonly")==="1"' in MOLD_HTML
    assert '$("movePanel").hidden=readOnly' in MOLD_HTML
    assert "if(!readOnly)await loadOneFloorLocationOptions()" in MOLD_HTML
