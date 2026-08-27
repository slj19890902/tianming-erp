from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from tests.test_mold_tool_workflow import _login, mold_app


ROOT = Path(__file__).resolve().parents[1]
LABEL_PAGE = (ROOT / "static" / "mold-label.html").read_text(encoding="utf-8")
WAREHOUSE_PAGE = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")
LAYOUT_JS = (ROOT / "static" / "assets" / "mold-label-layout.js").read_text(
    encoding="utf-8"
)


def _legacy_v3_layout() -> dict:
    """Return the final p1-103-v3 geometry for frozen-history tests."""

    return {
        "catalog_version": "p1-103-v3",
        "paper": {"width_mm": 80.0, "height_mm": 40.0},
        "elements": [
            {"id": "board_specification", "kind": "text", "x_mm": 1.2, "y_mm": .8, "width_mm": 61.8, "height_mm": 7.0, "font_size_mm": 5.6, "font_weight": 900, "text_align": "left", "visible": True},
            {"id": "inventory_code", "kind": "text", "x_mm": 1.2, "y_mm": 8.4, "width_mm": 61.8, "height_mm": 7.0, "font_size_mm": 4.8, "font_weight": 900, "text_align": "left", "visible": True},
            {"id": "flute_type", "kind": "text", "x_mm": 1.2, "y_mm": 15.7, "width_mm": 12.0, "height_mm": 7.0, "font_size_mm": 4.0, "font_weight": 800, "text_align": "left", "visible": True},
            {"id": "cutting_mode", "kind": "text", "x_mm": 13.6, "y_mm": 15.7, "width_mm": 49.4, "height_mm": 7.0, "font_size_mm": 3.8, "font_weight": 800, "text_align": "left", "visible": True},
            {"id": "customer_name", "kind": "text", "x_mm": 1.2, "y_mm": 24.6, "width_mm": 21.8, "height_mm": 6.4, "font_size_mm": 4.0, "font_weight": 900, "text_align": "left", "visible": True},
            {"id": "mold_label_name", "kind": "text", "x_mm": 1.2, "y_mm": 31.2, "width_mm": 61.8, "height_mm": 6.8, "font_size_mm": 5.0, "font_weight": 900, "text_align": "left", "visible": True},
            {"id": "mold_chinese_short_name", "kind": "text", "x_mm": 23.4, "y_mm": 24.6, "width_mm": 39.6, "height_mm": 6.4, "font_size_mm": 4.0, "font_weight": 800, "text_align": "left", "visible": True},
            {"id": "product_specification", "kind": "text", "x_mm": 24.6, "y_mm": 30.6, "width_mm": 38.4, "height_mm": 8.0, "font_size_mm": 4.4, "font_weight": 800, "text_align": "left", "visible": False},
            {"id": "mold_qr", "kind": "qr", "x_mm": 64.4, "y_mm": 24.6, "width_mm": 14.2, "height_mm": 14.2, "visible": True},
        ],
    }


def _frozen_v1_layout() -> dict:
    """Build the pre-P1-103E geometry instead of mutating the new default."""

    legacy = deepcopy(_legacy_v3_layout())
    legacy["catalog_version"] = "p1-103-v1"
    geometry = {
        "customer_name": (1.2, 23.2, 21.8, 7.0, 4.2),
        "mold_label_name": (23.4, 23.2, 39.6, 7.0, 5.2),
        "mold_chinese_short_name": (1.2, 30.6, 23.0, 8.0, 4.0),
        "product_specification": (24.6, 30.6, 38.4, 8.0, 4.4),
    }
    for element in legacy["elements"]:
        values = geometry.get(element["id"])
        if values is not None:
            (
                element["x_mm"],
                element["y_mm"],
                element["width_mm"],
                element["height_mm"],
                element["font_size_mm"],
            ) = values
        if element["id"] == "product_specification":
            element["visible"] = True
    return legacy


def _complete_named_mold(
    factory,
    *,
    label_name: str | None = "现场手写A17",
    mold_code: str = "P1103-INTERNAL-01",
    chinese_short_name: str | None = "短侧板",
    product_code: str = "P1103-BOX-001",
) -> int:
    from app.models.customer import Customer
    from app.models.mold_tool import MoldTool
    from app.models.product import Product

    with factory() as db:
        customer = db.get(Customer, 1)
        assert customer is not None
        customer.chinese_short_name = "联测"
        mold = MoldTool(
            mold_code=mold_code,
            mold_name="联测现场模具",
            label_name=label_name,
            chinese_short_name=chinese_short_name,
            identity_status="frozen" if label_name else "legacy_unset",
            rack_location="1F-M-R01-L1-G01",
        )
        db.add(mold)
        db.flush()
        db.add(
            Product(
                customer_id=customer.id,
                product_code=product_code,
                customer_material_code=product_code,
                product_name="布局测试纸箱",
                length_mm=430,
                width_mm=280,
                height_mm=160,
                report_length_mm=920,
                report_width_mm=610,
                flute_type="AB",
                default_cutting_mode="一开二",
                mold_tool_id=mold.id,
            )
        )
        db.commit()
        return int(mold.id)


def test_default_layout_matches_single_label_content_and_fills_80x40_paper() -> None:
    from app.services.mold_label_layout import default_layout, normalize_layout

    layout = normalize_layout(default_layout())
    assert layout["catalog_version"] == "p1-117-v1"
    assert layout["paper"] == {"width_mm": 80.0, "height_mm": 40.0}
    assert [item["id"] for item in layout["elements"]] == [
        "board_specification",
        "product_specification",
        "flute_type",
        "mold_identity",
        "mold_chinese_short_name",
        "mold_qr",
    ]
    qr = next(item for item in layout["elements"] if item["id"] == "mold_qr")
    assert (qr["width_mm"], qr["height_mm"]) == (14.2, 14.2)
    customer = next(
        item for item in layout["elements"] if item["id"] == "mold_identity"
    )
    mold_number = next(
        item
        for item in layout["elements"]
        if item["id"] == "mold_chinese_short_name"
    )
    product_size = next(
        item
        for item in layout["elements"]
        if item["id"] == "product_specification"
    )
    board = next(
        item for item in layout["elements"] if item["id"] == "board_specification"
    )
    assert board["width_mm"] >= 61.5
    assert product_size["visible"] is True
    assert customer["width_mm"] >= 61.5
    assert mold_number["x_mm"] == customer["x_mm"]
    assert customer["y_mm"] == qr["y_mm"]
    assert mold_number["y_mm"] - (
        customer["y_mm"] + customer["height_mm"]
    ) <= 0.3
    assert mold_number["width_mm"] >= 61.5
    assert mold_number["font_size_mm"] >= 4.3
    assert mold_number["y_mm"] + mold_number["height_mm"] <= 40.0
    assert qr["y_mm"] >= 24.0


def test_historical_wide_job_offers_explicit_current_layout_reregistration() -> None:
    assert 'CURRENT_WIDE_CATALOG="p1-117-v1"' in LABEL_PAGE
    assert 'id="recreateCurrentLayout"' in LABEL_PAGE
    assert "按当前统一版式重新登记" in LABEL_PAGE
    assert "历史作业不会被改写" in LABEL_PAGE
    assert 'method:"POST"' in LABEL_PAGE
    assert '"/api/warehouse/molds/label-prints"' in LABEL_PAGE
    assert 'next.searchParams.set("print_job_id",registered.print_job_id)' in LABEL_PAGE


def test_v1_snapshot_hash_and_prefix_catalog_remain_frozen_after_v2_default() -> None:
    from app.services.mold_label_layout import (
        canonical_json,
        default_layout,
        layout_hash,
        load_snapshot,
    )

    legacy = _frozen_v1_layout()
    frozen_hash = layout_hash(legacy)

    snapshot = load_snapshot(
        version=7,
        payload_json=canonical_json(legacy),
        payload_hash=frozen_hash,
    )

    assert snapshot["version"] == 7
    assert snapshot["layout"]["catalog_version"] == "p1-103-v1"
    assert snapshot["layout_hash"] == frozen_hash


def test_current_v1_release_is_projected_to_single_parity_without_mutating_history(
    mold_app,
) -> None:
    from app.models.mold_tool import MoldLabelLayoutRevision
    from app.services.mold_label_layout import (
        canonical_json,
        default_layout,
        effective_layout,
        layout_hash,
    )

    _app, factory = mold_app
    legacy = _frozen_v1_layout()
    with factory() as db:
        db.add(
            MoldLabelLayoutRevision(
                version=1,
                catalog_version="p1-103-v1",
                payload_json=canonical_json(legacy),
                payload_hash=layout_hash(legacy),
                operation_kind="save_and_publish",
                operation_key="p1-103-current-v1-upgrade",
                request_hash="1" * 64,
                created_by=1,
            )
        )
        db.commit()

        current = effective_layout(db)
        stored = db.scalar(select(MoldLabelLayoutRevision))

        assert current["version"] == 1
        assert current["layout"]["catalog_version"] == "p1-117-v1"
        assert [item["id"] for item in current["layout"]["elements"]] == [
            "board_specification",
            "product_specification",
            "flute_type",
            "mold_identity",
            "mold_chinese_short_name",
            "mold_qr",
        ]
        assert stored is not None
        assert stored.catalog_version == "p1-103-v1"
        assert '"catalog_version":"p1-103-v1"' in stored.payload_json


def test_layout_validator_rejects_overlap_qr_resize_and_unknown_fields() -> None:
    from app.services.mold_label_layout import (
        MoldLabelLayoutError,
        default_layout,
        normalize_layout,
    )

    overlap = default_layout()
    overlap["elements"][1]["x_mm"] = overlap["elements"][0]["x_mm"]
    overlap["elements"][1]["y_mm"] = overlap["elements"][0]["y_mm"]
    with pytest.raises(MoldLabelLayoutError, match="发生重叠"):
        normalize_layout(overlap)

    resized = default_layout()
    resized["elements"][-1]["width_mm"] = 14.0
    resized["elements"][-1]["height_mm"] = 14.0
    with pytest.raises(MoldLabelLayoutError, match="14.2"):
        normalize_layout(resized)

    outside = default_layout()
    outside["elements"][0]["x_mm"] = 79
    with pytest.raises(MoldLabelLayoutError, match="超出80×40"):
        normalize_layout(outside)

    unknown = default_layout()
    unknown["elements"][0]["id"] = "unregistered_business_text"
    with pytest.raises(MoldLabelLayoutError, match="未登记元素"):
        normalize_layout(unknown)


def test_admin_publish_is_append_only_idempotent_and_rejects_stale_version(
    mold_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.mold_tool import MoldLabelLayoutRevision
    from app.services.mold_label_layout import default_layout

    app, factory = mold_app
    with TestClient(app) as client:
        assert client.get("/api/warehouse/molds/label-layout").status_code == 401
        anonymous_write = client.post(
            "/api/warehouse/molds/label-layout/admin/publish",
            json={
                "operation_key": "p1-103-anonymous-layout",
                "expected_release_version": 0,
                "layout": default_layout(),
            },
        )
        assert anonymous_write.status_code == 401

        _login(client, "workshop")
        visible = client.get("/api/warehouse/molds/label-layout")
        assert visible.status_code == 200
        assert visible.json()["version"] == 0
        forbidden = client.post(
            "/api/warehouse/molds/label-layout/admin/publish",
            json={
                "operation_key": "p1-103-forbidden-layout",
                "expected_release_version": 0,
                "layout": visible.json()["layout"],
            },
        )
        assert forbidden.status_code == 403

        _login(client, "admin")
        state = client.get("/api/warehouse/molds/label-layout/admin").json()
        layout = state["published"]["layout"]
        layout["elements"][0]["y_mm"] = 1.3
        payload = {
            "operation_key": "p1-103-save-layout-0001",
            "expected_release_version": 0,
            "layout": layout,
        }
        created = client.post(
            "/api/warehouse/molds/label-layout/admin/publish", json=payload
        )
        assert created.status_code == 200, created.text
        assert created.json()["published"]["version"] == 1
        replay = client.post(
            "/api/warehouse/molds/label-layout/admin/publish", json=payload
        )
        assert replay.status_code == 200
        assert replay.json()["replayed"] is True

        same_key_conflict = client.post(
            "/api/warehouse/molds/label-layout/admin/publish",
            json={
                **payload,
                "layout": {**layout, "elements": clone_elements(layout, y_mm=1.4)},
            },
        )
        assert same_key_conflict.status_code == 409
        assert "操作编号已用于不同" in same_key_conflict.json()["detail"]

        stale = client.post(
            "/api/warehouse/molds/label-layout/admin/publish",
            json={
                **payload,
                "operation_key": "p1-103-save-layout-stale",
                "layout": {**layout, "elements": clone_elements(layout, y_mm=1.4)},
            },
        )
        assert stale.status_code == 409

        extra = client.post(
            "/api/warehouse/molds/label-layout/admin/publish",
            json={
                **payload,
                "operation_key": "p1-103-layout-extra",
                "expected_release_version": 1,
                "layout": {**layout, "html": "<script>alert(1)</script>"},
            },
        )
        assert extra.status_code == 422

    with factory() as db:
        assert db.scalar(select(func.count(MoldLabelLayoutRevision.id))) == 1
        assert db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action_code
                == "warehouse.mold_label_layout.save_and_publish"
            )
        ) == 1


def clone_elements(layout: dict, *, y_mm: float) -> list[dict]:
    elements = [dict(item) for item in layout["elements"]]
    elements[0]["y_mm"] = y_mm
    return elements


def test_new_print_job_freezes_layout_and_replay_never_uses_new_release(
    mold_app,
) -> None:
    from app.models.mold_tool import MoldLabelPrintJob

    app, factory = mold_app
    mold_id = _complete_named_mold(factory)
    with TestClient(app) as client:
        _login(client, "admin")
        state = client.get("/api/warehouse/molds/label-layout/admin").json()
        first_layout = state["published"]["layout"]
        first_layout["elements"][0]["y_mm"] = 1.3
        first_release = client.post(
            "/api/warehouse/molds/label-layout/admin/publish",
            json={
                "operation_key": "p1-103-freeze-layout-v1",
                "expected_release_version": 0,
                "layout": first_layout,
            },
        )
        assert first_release.status_code == 200, first_release.text

        _login(client, "workshop")
        print_payload = {
            "mold_ids": [mold_id],
            "source": "single",
            "template_version": "mold_80x40_v1",
            "idempotency_key": "p1-103-freeze-print-0001",
        }
        created = client.post("/api/warehouse/molds/label-prints", json=print_payload)
        assert created.status_code == 200, created.text
        job_id = created.json()["print_job_id"]
        assert created.json()["label_layout"]["version"] == 1

        _login(client, "admin")
        second_layout = deepcopy(first_layout)
        second_layout["elements"][0]["y_mm"] = 1.4
        second_release = client.post(
            "/api/warehouse/molds/label-layout/admin/publish",
            json={
                "operation_key": "p1-103-freeze-layout-v2",
                "expected_release_version": 1,
                "layout": second_layout,
            },
        )
        assert second_release.status_code == 200, second_release.text

        _login(client, "workshop")
        replay = client.post("/api/warehouse/molds/label-prints", json=print_payload)
        assert replay.status_code == 200
        assert replay.json()["print_job_id"] == job_id
        assert replay.json()["label_layout"]["version"] == 1

        frozen = client.get(
            f"/api/warehouse/molds/{mold_id}/label",
            params={
                "template_version": "mold_80x40_v1",
                "print_job_id": job_id,
            },
        )
        assert frozen.status_code == 200, frozen.text
        assert frozen.json()["label_layout"]["version"] == 1
        assert frozen.json()["label_layout"]["layout"]["elements"][0]["y_mm"] == 1.3
        assert frozen.json()["label_mold_name"] == "现场手写A17"
        assert frozen.json()["label_mold_chinese_short_name"] == "短侧板"

        unregistered = client.get(
            f"/api/warehouse/molds/{mold_id}/label",
            params={"template_version": "mold_80x40_v1"},
        )
        assert unregistered.status_code == 409
        assert "冻结打印任务" in unregistered.json()["detail"]
        unregistered_batch = client.get(
            "/api/warehouse/molds/labels",
            params={
                "mold_ids": str(mold_id),
                "template_version": "mold_80x40_v1",
            },
        )
        assert unregistered_batch.status_code == 409
        assert "冻结打印任务" in unregistered_batch.json()["detail"]
        current = client.get("/api/warehouse/molds/label-layout")
        assert current.status_code == 200
        assert current.json()["version"] == 2

    with factory() as db:
        job = db.get(MoldLabelPrintJob, job_id)
        assert job is not None
        assert job.label_layout_version == 1
        assert job.label_layout_payload_hash == created.json()["label_layout"]["layout_hash"]
        assert job.label_layout_payload_json


def test_true_two_mold_batch_keeps_order_and_frozen_layout_across_release(
    mold_app,
) -> None:
    app, factory = mold_app
    first_id = _complete_named_mold(factory)
    second_id = _complete_named_mold(
        factory,
        label_name="现场手写B09",
        mold_code="P1103-INTERNAL-02",
        chinese_short_name=None,
        product_code="P1103-BOX-002",
    )
    with TestClient(app) as client:
        _login(client, "admin")
        first_layout = client.get("/api/warehouse/molds/label-layout/admin").json()[
            "published"
        ]["layout"]
        first_layout["elements"][0]["y_mm"] = 1.3
        published = client.post(
            "/api/warehouse/molds/label-layout/admin/publish",
            json={
                "operation_key": "p1-103-batch-layout-v1",
                "expected_release_version": 0,
                "layout": first_layout,
            },
        )
        assert published.status_code == 200, published.text

        _login(client, "workshop")
        payload = {
            "mold_ids": [second_id, first_id],
            "source": "batch",
            "template_version": "mold_80x40_v1",
            "idempotency_key": "p1-103-true-batch-print-0001",
        }
        created = client.post("/api/warehouse/molds/label-prints", json=payload)
        assert created.status_code == 200, created.text
        assert created.json()["source"] == "batch"
        assert created.json()["count"] == 2
        assert created.json()["mold_ids"] == [second_id, first_id]
        assert created.json()["label_layout"]["version"] == 1
        created_layout = deepcopy(created.json()["label_layout"])
        assert created_layout["layout"]["elements"][0]["y_mm"] == 1.3

        _login(client, "admin")
        second_layout = deepcopy(first_layout)
        second_layout["elements"][0]["y_mm"] = 1.4
        second_release = client.post(
            "/api/warehouse/molds/label-layout/admin/publish",
            json={
                "operation_key": "p1-103-batch-layout-v2",
                "expected_release_version": 1,
                "layout": second_layout,
            },
        )
        assert second_release.status_code == 200, second_release.text
        assert (
            second_release.json()["published"]["layout_hash"]
            != created_layout["layout_hash"]
        )

        _login(client, "workshop")
        replay = client.post("/api/warehouse/molds/label-prints", json=payload)
        assert replay.status_code == 200, replay.text
        assert replay.json()["replayed"] is True
        assert replay.json()["label_layout"]["version"] == 1
        batch = client.get(
            "/api/warehouse/molds/labels",
            params={
                "mold_ids": f"{second_id},{first_id}",
                "template_version": "mold_80x40_v1",
                "print_job_id": created.json()["print_job_id"],
            },
        )
        assert batch.status_code == 200, batch.text
        frozen_layout = batch.json()["label_layout"]
        assert frozen_layout["version"] == 1
        assert frozen_layout["layout_hash"] == created_layout["layout_hash"]
        assert frozen_layout["layout"] == created_layout["layout"]
        assert frozen_layout["layout"]["elements"][0]["y_mm"] == 1.3
        assert [item["mold_code"] for item in batch.json()["items"]] == [
            "P1103-INTERNAL-02",
            "P1103-INTERNAL-01",
        ]
        assert batch.json()["items"][0]["label_mold_chinese_short_name"] == ""


def test_corrupted_frozen_layout_hash_fails_closed(mold_app) -> None:
    from app.models.mold_tool import MoldLabelPrintJob

    app, factory = mold_app
    mold_id = _complete_named_mold(factory)
    with TestClient(app) as client:
        _login(client, "workshop")
        created = client.post(
            "/api/warehouse/molds/label-prints",
            json={
                "mold_ids": [mold_id],
                "source": "single",
                "template_version": "mold_80x40_v1",
                "idempotency_key": "p1-103-corrupt-hash-print",
            },
        )
        assert created.status_code == 200, created.text
        job_id = created.json()["print_job_id"]

        with factory() as db:
            job = db.get(MoldLabelPrintJob, job_id)
            assert job is not None
            job.label_layout_payload_hash = "0" * 64
            db.commit()

        replay = client.get(
            f"/api/warehouse/molds/{mold_id}/label",
            params={
                "template_version": "mold_80x40_v1",
                "print_job_id": job_id,
            },
        )
        assert replay.status_code == 409
        assert "没有可验证的冻结布局" in replay.json()["detail"]


def test_layout_audit_failure_rolls_back_revision(mold_app, monkeypatch) -> None:
    from app.api import warehouse
    from app.models.mold_tool import MoldLabelLayoutRevision

    app, factory = mold_app

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("p1-103 forced audit failure")

    monkeypatch.setattr(warehouse, "append_audit_event", fail_audit)
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client, "admin")
        layout = client.get("/api/warehouse/molds/label-layout/admin").json()[
            "published"
        ]["layout"]
        layout["elements"][0]["y_mm"] = 1.3
        failed = client.post(
            "/api/warehouse/molds/label-layout/admin/publish",
            json={
                "operation_key": "p1-103-audit-failure",
                "expected_release_version": 0,
                "layout": layout,
            },
        )
        assert failed.status_code == 500

    with factory() as db:
        assert db.scalar(select(func.count(MoldLabelLayoutRevision.id))) == 0


def test_wide_label_uses_single_label_facts_without_extra_handwritten_name_gate(
    mold_app,
) -> None:
    app, factory = mold_app
    mold_id = _complete_named_mold(factory, label_name=None, mold_code="INT-01")
    with TestClient(app) as client:
        _login(client, "workshop")
        legacy = client.post(
            "/api/warehouse/molds/label-prints",
            json={
                "mold_ids": [mold_id],
                "source": "single",
                "template_version": "mold_40x30_v1",
                "idempotency_key": "p1-103-missing-label-40",
            },
        )
        assert legacy.status_code == 200, legacy.text

        wide = client.post(
            "/api/warehouse/molds/label-prints",
            json={
                "mold_ids": [mold_id],
                "source": "single",
                "template_version": "mold_80x40_v1",
                "idempotency_key": "p1-103-missing-label-80",
            },
        )
        assert wide.status_code == 200, wide.text

        legacy_label = client.get(
            f"/api/warehouse/molds/{mold_id}/label",
            params={
                "template_version": "mold_40x30_v1",
                "print_job_id": legacy.json()["print_job_id"],
            },
        ).json()
        wide_label = client.get(
            f"/api/warehouse/molds/{mold_id}/label",
            params={
                "template_version": "mold_80x40_v1",
                "print_job_id": wide.json()["print_job_id"],
            },
        ).json()
        for field in (
            "label_customer_name",
            "label_mold_number",
            "label_product_specification",
            "label_report_specification",
            "label_flute_type",
        ):
            assert wide_label[field] == legacy_label[field]


def test_rollback_replay_keeps_the_original_source_and_adds_no_duplicate(
    mold_app,
) -> None:
    from app.models.audit import OperationLog
    from app.models.mold_tool import MoldLabelLayoutRevision

    app, factory = mold_app
    with TestClient(app) as client:
        _login(client, "admin")
        layout = client.get("/api/warehouse/molds/label-layout/admin").json()[
            "published"
        ]["layout"]
        first = deepcopy(layout)
        first["elements"][0]["y_mm"] = 1.3
        assert client.post(
            "/api/warehouse/molds/label-layout/admin/publish",
            json={
                "operation_key": "p1-103-rollback-v1",
                "expected_release_version": 0,
                "layout": first,
            },
        ).status_code == 200
        second = deepcopy(first)
        second["elements"][0]["y_mm"] = 1.4
        assert client.post(
            "/api/warehouse/molds/label-layout/admin/publish",
            json={
                "operation_key": "p1-103-rollback-v2",
                "expected_release_version": 1,
                "layout": second,
            },
        ).status_code == 200
        payload = {
            "operation_key": "p1-103-rollback-replay",
            "expected_release_version": 2,
        }
        rolled_back = client.post(
            "/api/warehouse/molds/label-layout/admin/rollback", json=payload
        )
        assert rolled_back.status_code == 200, rolled_back.text
        assert rolled_back.json()["published"]["version"] == 3
        assert rolled_back.json()["published"]["layout"]["elements"][0]["y_mm"] == 1.3
        replay = client.post(
            "/api/warehouse/molds/label-layout/admin/rollback", json=payload
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["replayed"] is True
        assert replay.json()["published"]["version"] == 3

    with factory() as db:
        assert db.scalar(select(func.count(MoldLabelLayoutRevision.id))) == 3
        assert db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action_code.like("warehouse.mold_label_layout.%")
            )
        ) == 3


def test_layout_frontend_uses_millimetres_drag_save_and_frozen_job_url() -> None:
    for marker in (
        "mold-label-layout.css",
        "mold-label-layout.js",
        "调整40×80标签布局",
        "保存并用于以后打印",
        'data-mold-layout-field="x_mm"',
        'data-mold-layout-field="font_size_mm"',
        "TmMoldLabelLayout.labelHtml",
        "print_job_id",
        "translateX(40mm) rotate(90deg)",
    ):
        assert marker in LABEL_PAGE
    for marker in (
        "board_specification",
        "inventory_code",
        "customer_name",
        "mold_label_name",
        "mold_chinese_short_name",
        "product_specification",
        "14.2毫米",
        "/api/warehouse/molds/label-layout/admin/publish",
        "pointerdown",
        "pointermove",
        "fitAndValidate",
        "preflightLayout",
        "renderPublishedResult",
    ):
        assert marker in LAYOUT_JS
    assert "registered?.print_job_id" in WAREHOUSE_PAGE
    assert "&print_job_id=" in WAREHOUSE_PAGE
