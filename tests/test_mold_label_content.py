from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.middleware.mold_private import MoldPrivateNoStoreMiddleware
from app.services.mold_label_content import canonical_label_overrides
from tests.test_mold_tool_workflow import _login, mold_app
from tests.test_p1_103_mold_label_layout import _complete_named_mold


def _formal_update_payload(
    *,
    version: int,
    key: str,
    overrides: dict[str, str | None] | None = None,
) -> dict:
    payload = {
        "label_name": "现场手写A17",
        "chinese_short_name": "短侧板",
        "customers": [{"customer_id": 1, "display_order": 1}],
        "expected_version": version,
        "idempotency_key": key,
        "rack_location": "1F-M-R01-L1-G01",
    }
    if overrides is not None:
        payload["label_overrides"] = overrides
    return payload


def test_preview_projects_shared_report_dimensions_without_printing(mold_app) -> None:
    app, factory = mold_app
    mold_id = _complete_named_mold(factory)
    with factory() as db:
        from app.models.customer import Customer
        from app.models.mold_tool import MoldLabelPrintJob
        from app.models.product import Product

        first = db.scalar(select(Product).where(Product.mold_tool_id == mold_id))
        assert first is not None
        first.product_code = "80011946"
        first.product_name = "APS4"
        first.report_length_mm = 772
        first.report_width_mm = 336
        second_customer = Customer(
            customer_number=9902,
            customer_code="MOLD-C2",
            name="模具标签第二客户",
            payment_term_days=30,
            credit_limit=100000,
        )
        db.add(second_customer)
        db.flush()
        db.add(
            Product(
                customer_id=second_customer.id,
                product_code="80011946",
                customer_material_code="80011946",
                product_name="APS4",
                report_length_mm=876,
                report_width_mm=336,
                default_cutting_mode="一开四",
                flute_type="B",
                mold_tool_id=mold_id,
            )
        )
        first.default_cutting_mode = "一开四"
        db.commit()
        assert db.scalar(select(MoldLabelPrintJob.id).limit(1)) is None

    with TestClient(app) as client:
        _login(client, "admin")
        response = client.get(f"/api/warehouse/molds/{mold_id}/label-preview")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["preview_only"] is True
    assert body["print_job"] is None
    assert body["label_auto_fields"]["product_name"] == "APS4"
    assert set(body["label_auto_fields"]["report_specification"].split(" / ")) == {
        "772 × 336",
        "876 × 336",
    }
    assert body["label_display_report_specification"] == body["label_auto_fields"]["report_specification"]
    assert body["label_layout"]
    with factory() as db:
        from app.models.mold_tool import MoldLabelPrintJob

        assert db.scalar(select(MoldLabelPrintJob.id).limit(1)) is None


def test_label_overrides_save_replay_and_clear_without_product_rewrite(mold_app) -> None:
    app, factory = mold_app
    mold_id = _complete_named_mold(factory)
    with factory() as db:
        from app.models.product import Product

        product = db.scalar(select(Product).where(Product.mold_tool_id == mold_id))
        assert product is not None
        before_product = (product.report_length_mm, product.report_width_mm, product.version)

    overrides = {
        "display_identity": "联测 80011946 APS4",
        "product_name": "APS4 标签名",
        "report_specification": "772 × 336 / 876 × 336",
        "cutting_mode": "一开四",
        "remarks": "仅用于模具标签",
    }
    payload = _formal_update_payload(
        version=1,
        key="mold-label-content-save-0001",
        overrides=overrides,
    )
    with TestClient(app) as client:
        _login(client, "admin")
        saved = client.put(f"/api/warehouse/molds/{mold_id}", json=payload)
        replay = client.put(f"/api/warehouse/molds/{mold_id}", json=payload)
        preview = client.get(f"/api/warehouse/molds/{mold_id}/label-preview")
        stale = client.put(
            f"/api/warehouse/molds/{mold_id}",
            json=_formal_update_payload(
                version=1,
                key="mold-label-content-stale-0001",
                overrides=overrides,
            ),
        )

    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == 2
    assert replay.status_code == 200, replay.text
    assert replay.json()["idempotent_replay"] is True
    assert stale.status_code == 409, stale.text
    assert preview.status_code == 200, preview.text
    assert "label_display_identity" in preview.json(), preview.json()
    assert preview.json()["label_display_identity"] == overrides["display_identity"]
    assert preview.json()["label_display_product_name"] == overrides["product_name"]
    assert preview.json()["label_display_remarks"] == overrides["remarks"]
    with factory() as db:
        from app.models.mold_tool import MoldMasterMutation, MoldTool
        from app.models.product import Product

        mold = db.get(MoldTool, mold_id)
        product = db.scalar(select(Product).where(Product.mold_tool_id == mold_id))
        assert mold is not None and mold.label_overrides_json is not None
        assert product is not None
        assert (product.report_length_mm, product.report_width_mm, product.version) == before_product
        assert db.scalar(select(MoldMasterMutation).where(MoldMasterMutation.mold_tool_id == mold_id))

    preserve_payload = _formal_update_payload(
        version=2,
        key="mold-label-content-legacy-omit-0001",
    )
    with TestClient(app) as client:
        _login(client, "admin")
        preserved = client.put(f"/api/warehouse/molds/{mold_id}", json=preserve_payload)

    assert preserved.status_code == 200, preserved.text
    assert preserved.json()["version"] == 3
    assert preserved.json()["label_overrides"] == overrides

    clear_payload = _formal_update_payload(
        version=3,
        key="mold-label-content-clear-0001",
        overrides={
            "display_identity": None,
            "product_name": None,
            "report_specification": None,
            "cutting_mode": None,
            "remarks": None,
        },
    )
    with TestClient(app) as client:
        _login(client, "admin")
        cleared = client.put(f"/api/warehouse/molds/{mold_id}", json=clear_payload)

    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["version"] == 4
    assert all(value is None for value in cleared.json()["label_overrides"].values())
    with factory() as db:
        from app.models.mold_tool import MoldTool

        mold = db.get(MoldTool, mold_id)
        assert mold is not None and mold.label_overrides_json is None


def test_label_preview_keeps_full_customer_scope_gate(mold_app) -> None:
    app, factory = mold_app
    mold_id = _complete_named_mold(factory)
    with factory() as db:
        from app.models.access_control import UserCustomerScope
        from app.models.user import User

        sales = db.scalar(select(User).where(User.username == "sales"))
        assert sales is not None
        sales.customer_access_mode = "selected"
        db.add(UserCustomerScope(user_id=sales.id, customer_id=1))
        db.commit()

    with TestClient(app) as client:
        _login(client, "sales")
        response = client.get(f"/api/warehouse/molds/{mold_id}/label-preview")

    assert response.status_code == 403, response.text
    assert "全客户范围" in response.json()["detail"]


def test_label_preview_allows_unbound_or_inactive_mold_but_keeps_print_gate(mold_app) -> None:
    app, factory = mold_app
    mold_id = _complete_named_mold(factory)
    with factory() as db:
        from app.models.mold_tool import MoldTool
        from app.models.product import Product

        mold = db.get(MoldTool, mold_id)
        product = db.scalar(select(Product).where(Product.mold_tool_id == mold_id))
        assert mold is not None and product is not None
        product.mold_tool_id = None
        mold.is_active = False
        db.commit()

    with TestClient(app) as client:
        _login(client, "admin")
        preview = client.get(f"/api/warehouse/molds/{mold_id}/label-preview")
        printed = client.get(f"/api/warehouse/molds/{mold_id}/label")

    assert preview.status_code == 200, preview.text
    assert preview.json()["printable"] is False
    assert "不能打印使用标签" in preview.json()["printability_error"]
    assert printed.status_code == 409, printed.text


def test_label_auto_fields_mark_partial_dimensions_and_cutting_mode() -> None:
    from app.api.warehouse import (
        _label_display_cutting_mode,
        _label_display_report_specification,
    )

    products = [
        SimpleNamespace(report_length_mm=772, report_width_mm=336, default_cutting_mode="一开四"),
        SimpleNamespace(report_length_mm=None, report_width_mm=336, default_cutting_mode=None),
    ]
    assert _label_display_report_specification(products) == "772 × 336 / 部分尺寸未填"
    assert _label_display_cutting_mode(products) == "一开四 / 部分开料未填"


def test_label_overrides_forbid_unknown_fields(mold_app) -> None:
    app, factory = mold_app
    mold_id = _complete_named_mold(factory)
    payload = _formal_update_payload(
        version=1,
        key="mold-label-content-extra-field-0001",
        overrides={
            "display_identity": "仅标签用",
            "product_name": None,
            "report_specification": None,
            "cutting_mode": None,
            "remarks": None,
            "not_allowed": "nope",
        },
    )
    with TestClient(app) as client:
        _login(client, "admin")
        response = client.put(f"/api/warehouse/molds/{mold_id}", json=payload)
    assert response.status_code == 422, response.text


def test_restricted_mold_list_does_not_disclose_label_free_text(mold_app) -> None:
    app, factory = mold_app
    mold_id = _complete_named_mold(factory)
    with factory() as db:
        from app.models.access_control import UserCustomerScope
        from app.models.mold_tool import MoldTool
        from app.models.user import User

        mold = db.get(MoldTool, mold_id)
        sales = db.scalar(select(User).where(User.username == "sales"))
        assert mold is not None and sales is not None
        mold.label_overrides_json = canonical_label_overrides(
            {"remarks": "仅其他关联客户可见的人工标签文本"}
        )
        sales.customer_access_mode = "selected"
        db.add(UserCustomerScope(user_id=sales.id, customer_id=1))
        db.commit()

    with TestClient(app) as client:
        _login(client, "sales")
        response = client.get("/api/warehouse/molds")

    assert response.status_code == 200, response.text
    row = next(item for item in response.json()["items"] if item["id"] == mold_id)
    assert all(value is None for value in row["label_overrides"].values())


def test_mold_private_no_store_covers_label_preview_outcomes() -> None:
    app = FastAPI()
    app.add_middleware(MoldPrivateNoStoreMiddleware)

    @app.get("/api/warehouse/molds/{mold_id}/label-preview")
    def preview(mold_id: int) -> dict:
        if mold_id == 401:
            raise HTTPException(status_code=401, detail="unauthorized")
        if mold_id == 404:
            raise HTTPException(status_code=404, detail="missing")
        if mold_id == 500:
            raise RuntimeError("unexpected")
        return {"id": mold_id}

    with TestClient(app, raise_server_exceptions=False) as client:
        responses = [
            client.get("/api/warehouse/molds/1/label-preview"),
            client.get("/api/warehouse/molds/401/label-preview"),
            client.get("/api/warehouse/molds/404/label-preview"),
            client.get("/api/warehouse/molds/not-an-id/label-preview"),
            client.get("/api/warehouse/molds/500/label-preview"),
        ]

    assert [response.status_code for response in responses] == [200, 401, 404, 422, 500]
    for response in responses:
        assert response.headers["cache-control"] == "private, no-store, max-age=0"
        assert response.headers["pragma"] == "no-cache"
        assert "Cookie" in response.headers["vary"]


def test_legacy_master_request_hash_omits_absent_label_overrides() -> None:
    from app.api.warehouse import MoldToolPayload, _mold_master_request_hash

    payload = MoldToolPayload.model_validate(
        _formal_update_payload(version=7, key="legacy-master-save-0001")
    )
    expected_canonical = {
        "action": "update",
        "mold_id": 242,
        "expected_version": 7,
        "label_name": "现场手写A17",
        "chinese_short_name": "短侧板",
        "customers": [{"customer_id": 1, "display_order": 1}],
        "rack_location": "1F-M-R01-L1-G01",
        "remarks": None,
    }
    expected = hashlib.sha256(
        json.dumps(
            expected_canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert _mold_master_request_hash(
        action="update", mold_id=242, payload=payload
    ) == expected


def test_formal_create_accepts_two_primary_label_customers(mold_app) -> None:
    app, factory = mold_app
    with factory() as db:
        from app.models.customer import Customer

        first = db.get(Customer, 1)
        assert first is not None
        first.chinese_short_name = "甲方"
        second = Customer(
            customer_number=9903,
            customer_code="YIFANG",
            name="乙方包装",
            chinese_short_name="乙方",
            payment_term_days=30,
            credit_limit=100000,
        )
        db.add(second)
        db.commit()
        second_id = second.id

    payload = {
        "label_name": "80011946",
        "chinese_short_name": "APS4",
        "customers": [
            {"customer_id": 1, "display_order": 1},
            {"customer_id": second_id, "display_order": 2},
        ],
        "expected_version": 1,
        "idempotency_key": "two-primary-customers-create-0001",
        "rack_location": "1F-M-R01-L1-G01",
    }
    with TestClient(app) as client:
        _login(client, "admin")
        response = client.post("/api/warehouse/molds", json=payload)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["mold_name"] == "甲方/乙方 80011946 APS4"
    assert [item["display_order"] for item in body["primary_customers"]] == [1, 2]
    assert {item["customer_id"] for item in body["associated_customers"]} == {1, second_id}


def test_bound_products_endpoint_and_single_box_search_terms(mold_app) -> None:
    app, factory = mold_app
    mold_id = _complete_named_mold(factory, mold_code="SEARCH-MOLD-001")
    with factory() as db:
        from app.models.customer import Customer
        from app.models.mold_tool import MoldTool
        from app.models.product import Product

        customer = db.get(Customer, 1)
        mold = db.get(MoldTool, mold_id)
        product = db.scalar(select(Product).where(Product.mold_tool_id == mold_id))
        assert customer is not None and mold is not None and product is not None
        customer.name = "光洋包装"
        customer.chinese_short_name = "光洋"
        customer.customer_code = "GUANGYANG"
        mold.label_name = "80011946"
        mold.chinese_short_name = "APS4"
        mold.rack_location = "1F-M-R04-L2-G01"
        product.product_code = "80011946"
        product.customer_material_code = "80011946"
        product.product_name = "APS4"
        db.commit()

    with TestClient(app) as client:
        _login(client, "admin")
        bound = client.get(f"/api/warehouse/molds/{mold_id}/bound-products")
        searches = [
            client.get("/api/warehouse/molds", params={"q": term})
            for term in (
                "光洋包装",
                "光洋",
                "GUANGYANG",
                "80011946",
                "APS4",
                "R04-L2",
                "光洋 80011946",
                "80011946 APS4",
            )
        ]
        filtered = client.get(
            "/api/warehouse/molds",
            params={
                "customer_keyword": "光洋",
                "product_code": "APS4",
                "rack_location": "R04-L2",
            },
        )
        no_match = client.get(
            "/api/warehouse/molds",
            params={
                "customer_keyword": "光洋",
                "product_code": "不存在",
                "rack_location": "R04-L2",
            },
        )

    assert bound.status_code == 200, bound.text
    assert bound.json()["mold"]["id"] == mold_id
    assert bound.json()["items"] == bound.json()["mold"]["products"]
    assert bound.json()["items"][0]["version"] >= 1
    for response in searches:
        assert response.status_code == 200, response.text
        assert mold_id in {item["id"] for item in response.json()["items"]}
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["total"] == 1
    assert no_match.status_code == 200, no_match.text
    assert no_match.json()["total"] == 0
