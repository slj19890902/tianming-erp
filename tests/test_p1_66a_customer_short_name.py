from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.test_master_data_versioning_p4_writers import writer_app


ROOT = Path(__file__).resolve().parents[1]


def _write_payload(customer: dict, **changes: object) -> dict:
    payload = {
        key: value
        for key, value in customer.items()
        if key not in {"id", "is_active", "version"}
    }
    payload.update(changes)
    payload["expected_version"] = customer["version"]
    return payload


def test_customer_master_round_trips_manual_chinese_short_name(
    writer_app,
) -> None:
    with TestClient(writer_app) as client:
        created = client.post(
            "/api/master/customers",
            json={
                "customer_number": 6691,
                "customer_code": "P166A-MANUAL",
                "name": "苏州思迈尔包装有限公司",
                "chinese_short_name": "  思迈包装  ",
            },
        )
        assert created.status_code == 201, created.text
        customer = created.json()
        assert customer["chinese_short_name"] == "思迈包装"

        updated = client.put(
            f"/api/master/customers/{customer['id']}",
            json=_write_payload(customer, chinese_short_name="思迈新版"),
        )
        assert updated.status_code == 200, updated.text
        customer = updated.json()
        assert customer["chinese_short_name"] == "思迈新版"

        legacy_payload = _write_payload(customer, remark="旧客户端只改其它字段")
        legacy_payload.pop("chinese_short_name")
        legacy_update = client.put(
            f"/api/master/customers/{customer['id']}",
            json=legacy_payload,
        )
        assert legacy_update.status_code == 200, legacy_update.text
        assert legacy_update.json()["chinese_short_name"] == "思迈新版"

        detail = client.get(f"/api/master/customers/{customer['id']}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["chinese_short_name"] == "思迈新版"

        history = client.get(
            f"/api/master-data/customer/{customer['id']}/versions/2"
        )
        assert history.status_code == 200, history.text
        assert history.json()["snapshot"]["chinese_short_name"] == "思迈新版"


def test_customer_master_allows_blank_but_rejects_non_chinese_short_name(
    writer_app,
) -> None:
    with TestClient(writer_app) as client:
        blank = client.post(
            "/api/master/customers",
            json={
                "customer_number": 6692,
                "customer_code": "P166A-BLANK",
                "name": "历史客户可暂不补简称",
                "chinese_short_name": "   ",
            },
        )
        invalid = client.post(
            "/api/master/customers",
            json={
                "customer_number": 6693,
                "customer_code": "P166A-ENGLISH",
                "name": "外文客户",
                "chinese_short_name": "ABC PACKAGING",
            },
        )
        too_long = client.post(
            "/api/master/customers",
            json={
                "customer_number": 6694,
                "customer_code": "P166A-LONG",
                "name": "简称长度门禁客户",
                "chinese_short_name": "简" * 31,
            },
        )

    assert blank.status_code == 201, blank.text
    assert blank.json()["chinese_short_name"] is None
    assert invalid.status_code == 422, invalid.text
    assert too_long.status_code == 422, too_long.text


def test_customer_master_frontend_exposes_manual_label_short_name() -> None:
    source = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert 'v-model.trim="customerForm.chinese_short_name"' in source
    assert "标签中文简称（人工）" in source
    assert '["chinese_short_name","标签中文简称"]' in source
    assert 'chinese_short_name: ""' in source
