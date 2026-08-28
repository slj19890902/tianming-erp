from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace


def test_product_legacy_local_timestamps_keep_beijing_offset(monkeypatch):
    from app.api import products

    legacy_local = datetime(2026, 7, 7, 15, 17, 41)
    values = {name: None for name in products.ProductPayload.model_fields}
    product = SimpleNamespace(
        **values,
        id=1,
        manual_modified=True,
        manual_modified_at=legacy_local,
        deleted_at=legacy_local,
        deleted_by=1,
        purged_at=None,
        version=1,
        drawings=[],
        material=None,
        mold_tool=None,
        printing_plate_1=None,
        printing_plate_2=None,
        printing_plate_3=None,
        external_packaging_candidate_snapshot_json=None,
        external_packaging_specification_json=None,
    )
    monkeypatch.setattr(products, "beijing_now_naive", lambda: legacy_local)

    payload = products._response(product, SimpleNamespace(role="admin"))

    assert payload["manual_modified_at"] == "2026-07-07T15:17:41+08:00"
    assert payload["deleted_at"] == "2026-07-07T15:17:41+08:00"
    assert payload["deleted_expires_at"] == "2026-08-06T15:17:41+08:00"


def test_floor3_legacy_local_timestamps_keep_beijing_offset():
    from app.api.warehouse import _floor3_layout_dict, _floor3_pallet_dict

    legacy_local = datetime(2026, 7, 16, 16, 23, 44)
    layout = SimpleNamespace(
        location_id=1,
        left_pct=1,
        top_pct=2,
        width_pct=3,
        height_pct=4,
        z_index=0,
        version=1,
        source_type="manual",
        layout_kind="physical_pallet",
        updated_at=legacy_local,
    )
    pallet = SimpleNamespace(
        id=1,
        pallet_code="PLT-1",
        location_id=1,
        status="closed",
        is_current=False,
        needs_relocation=False,
        remarks=None,
        version=1,
        items=[],
        created_at=datetime(2026, 7, 16, 8, 23, 32),
        updated_at=datetime(2026, 7, 16, 8, 23, 44),
        closed_at=legacy_local,
    )

    assert _floor3_layout_dict(layout)["updated_at"].endswith("+08:00")
    payload = _floor3_pallet_dict(
        pallet,
        visible_customer_ids=None,
        customer_names={},
    )
    assert payload["created_at"].endswith("Z")
    assert payload["updated_at"].endswith("Z")
    assert payload["closed_at"] == "2026-07-16T16:23:44+08:00"


def test_company_config_legacy_updated_at_is_not_relabelled_as_utc():
    from app.api.system import _company_dict

    payload = _company_dict(
        SimpleNamespace(
            company_name="天明",
            short_name=None,
            address=None,
            phone=None,
            fax=None,
            tax_number=None,
            bank_name=None,
            bank_account=None,
            contact_person=None,
            contact_phone=None,
            updated_at=datetime(2026, 6, 29, 12, 53, 33),
        )
    )

    assert payload["updated_at"] == "2026-06-29T12:53:33+08:00"
