from types import SimpleNamespace

from app.api.incoming import _supplier_order_item_overlay
from app.models.order import OrderItem


class _FakeDb:
    def __init__(self, order_item):
        self.order_item = order_item

    def get(self, model, identifier):
        if model is OrderItem:
            return self.order_item
        return SimpleNamespace(order_number="SRO-TEST", supplier_name="供应商")


def _supplier_item(**overrides):
    values = {
        "id": 77,
        "supplier_order_id": 30,
        "order_item_id": 9750,
        "source_key": None,
        "product_id": None,
        "product_code": "21301852",
        "product_name": "纸箱",
        "report_length_mm": None,
        "report_width_mm": None,
        "material_code_snapshot": "W618K",
        "flute_type_snapshot": "BE",
        "requisition_qty": 5,
        "supplier_name_snapshot": None,
        "cutting_mode": "一开一",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_supplier_overlay_falls_back_to_order_snapshot_when_legacy_line_is_blank():
    order_item = SimpleNamespace(
        cardboard_len=992,
        cardboard_width=1061,
        snapshot_report_length_mm=992,
        snapshot_report_width_mm=1061,
    )

    row = _supplier_order_item_overlay(_FakeDb(order_item), _supplier_item())

    assert row["cardboard_len"] == 992
    assert row["cardboard_width"] == 1061
    assert "992" in row["specification"]
    assert "1061" in row["specification"]


def test_supplier_overlay_preserves_explicit_one_sided_supplier_dimension():
    order_item = SimpleNamespace(
        cardboard_len=992,
        cardboard_width=1061,
        snapshot_report_length_mm=992,
        snapshot_report_width_mm=1061,
    )

    row = _supplier_order_item_overlay(
        _FakeDb(order_item),
        _supplier_item(report_length_mm=1000),
    )

    assert row["cardboard_len"] == 1000
    assert row["cardboard_width"] == 1061
