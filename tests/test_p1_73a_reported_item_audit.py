from __future__ import annotations

import json
from pathlib import Path

from app.models.supplier_requisition_order import SupplierRequisitionOrderItem


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "p1_73_reported_item_golden_cases.json"
)


def _cases() -> dict[str, dict]:
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "p1-73a-v1"
    return {row["case_id"]: row for row in payload["cases"]}


def test_p1_73a_golden_fixture_covers_the_five_required_business_shapes() -> None:
    cases = _cases()
    assert set(cases) == {
        "same_order_three_lines_void_middle",
        "same_order_item_two_partial_requisitions",
        "a3_cover_base_are_independent",
        "partially_received_line_is_blocked",
        "last_unreceived_line_voids_header",
    }

    middle = cases["same_order_three_lines_void_middle"]
    assert middle["before"]["supplier_order_active_quantity"] == 300
    assert middle["after"]["supplier_order_active_quantity"] == 200
    assert middle["after"]["active_item_ids"] == [1101, 1103]

    partial = cases["same_order_item_two_partial_requisitions"]
    assert partial["after"]["effective_coverage"] == 100
    assert partial["after"]["remaining_demand"] == 100

    a3 = cases["a3_cover_base_are_independent"]
    assert a3["after"]["cover_coverage"] == 0
    assert a3["after"]["base_coverage"] == 100

    received = cases["partially_received_line_is_blocked"]
    assert received["void_allowed"] is False
    assert received["after"] == received["before"]

    last = cases["last_unreceived_line_voids_header"]
    assert last["after"]["active_item_ids"] == []
    assert last["after"]["supplier_order_status"] == "voided"


def test_p1_73a_latest_formal_model_requires_a_minimal_item_state_migration() -> None:
    columns = SupplierRequisitionOrderItem.__table__.columns
    assert "id" in columns
    assert "supplier_order_id" in columns
    assert "source_key" in columns

    # This is an audit finding, not a desired final schema assertion.  P1-73C
    # replaces it with migration/behaviour tests once the line state exists.
    missing = {
        name
        for name in ("status", "version", "voided_at", "voided_by")
        if name not in columns
    }
    assert missing == {"status", "version", "voided_at", "voided_by"}
