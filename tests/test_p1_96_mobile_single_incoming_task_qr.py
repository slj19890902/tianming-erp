from __future__ import annotations

import base64
import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.test_p1_32a2_requisition_production_print import (
    _login,
    production_print_app,
)


ROOT = Path(__file__).resolve().parents[1]
MOBILE = (ROOT / "static" / "mobile_erp.html").read_text(encoding="utf-8")
INCOMING = (ROOT / "static" / "incoming.html").read_text(encoding="utf-8")
PRINT_PAGE = (ROOT / "static" / "requisition-production-print.html").read_text(
    encoding="utf-8"
)
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _decode_qr(data_url: str) -> str:
    import cv2
    import numpy as np

    raw = base64.b64decode(data_url.split(",", 1)[1])
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    decoded, _points, _straight = cv2.QRCodeDetector().detectAndDecode(image)
    return decoded


def test_mobile_receiving_is_one_entry_with_paged_history_and_separate_lookup() -> None:
    assert "车间来料入库" not in MOBILE
    assert "复用正式收料权限和受控确认动作" not in MOBILE
    assert '<iframe id="incomingFrame"' in MOBILE
    assert "待收料" in INCOMING
    assert "历史入库" in INCOMING
    assert "今日已收" not in INCOMING
    assert 'api(`/api/incoming/history?${params.toString()}`' in INCOMING
    assert 'id="receivedPager"' in INCOMING
    assert "receivedPage" in INCOMING and "receivedTotal" in INCOMING

    assert 'id="openMaterialLookup"' in MOBILE
    assert 'id="materialLookupPanel"' in MOBILE
    assert '"/api/mobile/erp/incoming/search"' in MOBILE
    assert "materialLookupController" in MOBILE
    assert "generation !== state.materialLookupGeneration" in MOBILE
    assert 'materials: "待收材料"' not in MOBILE


def test_bottom_navigation_stays_available_in_maps_drawings_and_details() -> None:
    assert "--mobile-nav-height" in MOBILE
    assert ".map-layer" in MOBILE
    assert "bottom: var(--mobile-nav-height)" in MOBILE
    assert ".bottom-nav" in MOBILE
    assert "z-index: 60" in MOBILE
    for label in ("首页", "查询", "收料", "仓库", "生产", "预送货"):
        assert f">{label}</button>" in MOBILE
    assert 'id="productionTaskDetail"' in MOBILE
    assert 'id="productionTaskWorkspace"' in MOBILE


def test_production_paper_qr_targets_one_task_and_opens_read_only_mobile_detail(
    production_print_app,
) -> None:
    from app.api.mobile_erp import router as mobile_router
    from app.models.access_control import UserPermissionOverride
    from app.models.audit import OperationLog
    from app.models.user import User

    app = production_print_app["app"]
    app.include_router(mobile_router, prefix="/api/mobile/erp")
    order_id = production_print_app["supplier_order_id"]

    with TestClient(app) as client:
        _login(client, "p132a2-admin")
        package = client.get(
            f"/api/requisition/supplier-orders/{order_id}/production-print-package"
        )
        assert package.status_code == 200, package.text
        card = next(
            row
            for row in package.json()["cards"]
            if len(row.get("production_task_versions") or []) == 1
        )
        task_id = int(card["production_task_versions"][0]["task_id"])
        qr = card["production_task_qr"]
        assert qr["production_task_id"] == task_id
        assert qr["lookup_url"].endswith(f"/mobile/?task={task_id}#production")
        assert _decode_qr(qr["qr_data_url"]) == qr["lookup_url"]
        serialized_qr = json.dumps(qr, ensure_ascii=False, sort_keys=True)
        for forbidden in ("status", "quantity", "order_id", "token", "version"):
            assert forbidden not in serialized_qr

        with production_print_app["session_factory"]() as db:
            before_logs = int(
                db.scalar(select(func.count()).select_from(OperationLog)) or 0
            )
        detail = client.get(f"/api/mobile/erp/production/tasks/{task_id}")
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["task_id"] == task_id
        assert body["status"] == "waiting_material"
        assert body["status_text"] == "材料未齐"
        assert body["can_complete"] is False
        assert body["read_only"] is True
        assert body["stations"] == ["printing"]
        assert body["drawing_path"].endswith(f"/production/tasks/{task_id}/drawing")
        assert "printing_colors_frozen" in body
        assert body["crease_type"] == "净"
        serialized = json.dumps(body, ensure_ascii=False, sort_keys=True)
        for forbidden in (
            "unit_price",
            "subtotal",
            "amount",
            "cost",
            "margin",
            "supplier_name",
        ):
            assert forbidden not in serialized
        assert client.post(f"/api/mobile/erp/production/tasks/{task_id}").status_code == 405
        with production_print_app["session_factory"]() as db:
            after_logs = int(
                db.scalar(select(func.count()).select_from(OperationLog)) or 0
            )
        assert after_logs == before_logs

    with production_print_app["session_factory"]() as db:
        restricted = db.scalar(select(User).where(User.username == "p132a2-sales"))
        db.add_all(
            [
                UserPermissionOverride(
                    user_id=restricted.id,
                    permission_code="orders.view",
                    is_allowed=True,
                ),
                UserPermissionOverride(
                    user_id=restricted.id,
                    permission_code="production.printing.view",
                    is_allowed=True,
                ),
            ]
        )
        db.commit()
    with TestClient(app) as client:
        _login(client, "p132a2-sales")
        hidden = client.get(f"/api/mobile/erp/production/tasks/{task_id}")
        assert hidden.status_code == 404
        assert hidden.headers["cache-control"] == "private, no-store, max-age=0"

    assert "productionTaskQrHtml(card)" in PRINT_PAGE
    assert 'alt="扫码查看这一项生产任务"' in PRINT_PAGE
    assert "productQrHtml(card)" not in PRINT_PAGE


def test_task_scan_login_round_trip_is_strict_and_mobile_only() -> None:
    assert "requestedProductionTaskId" in MOBILE
    assert "mobile_task" in MOBILE
    assert "openProductionTaskDetail" in MOBILE
    assert 'loginParams.set("mobile_page", "production")' in MOBILE
    assert "mobileTask" in INDEX
    assert "task=${mobileTask}" in INDEX
    assert "#/production" not in MOBILE
    assert "getUserMedia" not in MOBILE
    assert "扫码反查" not in MOBILE
