from decimal import Decimal
from pathlib import Path

import pytest

from app.models.material import Material
from app.models.order import OrderItem
from app.models.product import Product
from app.services import order_material_cost
from app.services.order_material_cost import estimate_order_item_material_cost


INDEX = Path("static/index.html").read_text(encoding="utf-8")


class _FakeSession:
    def __init__(self, *materials: Material) -> None:
        self.materials = {int(material.id): material for material in materials}

    def get(self, model, object_id):
        assert model is Material
        return self.materials.get(int(object_id))


def _material(material_id: int, price: str = "2.0000") -> Material:
    return Material(
        id=material_id,
        code=f"MAT-{material_id}",
        supplier_name="匿名供应商",
        layer_count=3,
        quote_price=Decimal(price),
        is_active=True,
    )


def _item(
    *,
    quantity: int,
    material_id: int = 1,
    report_length: int | None = 500,
    report_width: int | None = 400,
    pieces_per_box: int = 1,
    cutting_mode: str = "一开一",
    base_length: int | None = None,
    base_width: int | None = None,
    combination_role: str = "standalone",
) -> OrderItem:
    product = Product(
        id=1,
        customer_id=1,
        product_code="P1-28A",
        customer_material_code="P1-28A",
        product_name="匿名纸箱",
        material_id=material_id,
        box_category="normal",
        box_style="A1",
        layer_count=3,
        flute_type="B",
        pieces_per_box=pieces_per_box,
        default_cutting_mode=cutting_mode,
        is_active=True,
    )
    item = OrderItem(
        product_id=product.id,
        quantity=quantity,
        delivered_quantity=0,
        unit_price=Decimal("1"),
        subtotal=Decimal(quantity),
        material_status="pending",
        snapshot_product_name=product.product_name,
        snapshot_product_code=product.product_code,
        material_id=material_id,
        layer_count=3,
        flute_type="B",
        snapshot_report_length_mm=report_length,
        snapshot_report_width_mm=report_width,
        snapshot_base_report_length_mm=base_length,
        snapshot_base_report_width_mm=base_width,
        snapshot_pieces_per_box=pieces_per_box,
        special_process=cutting_mode,
        combination_role=combination_role,
    )
    item.product = product
    return item


@pytest.fixture(autouse=True)
def _fixed_effective_price(monkeypatch):
    def fake_price(_db, *, material, supplier_name, layer_count, flute_type):
        return {
            "base_price": float(material.quote_price),
            "flute_delta": 0.0,
            "effective_price": float(material.quote_price),
            "rule_id": None,
            "supplier_name": supplier_name,
            "layer_count": layer_count,
            "flute_type": flute_type,
        }

    monkeypatch.setattr(order_material_cost, "get_effective_material_price", fake_price)


def test_double_piece_and_cutting_mode_use_physical_piece_and_sheet_quantities() -> None:
    material = _material(1)
    result = estimate_order_item_material_cost(
        _FakeSession(material),
        _item(quantity=100, pieces_per_box=2, cutting_mode="一开二"),
    )

    assert result["material_cost_status"] == "calculated"
    component = result["material_cost_components"][0]
    assert component["required_piece_quantity"] == 200
    assert component["yield_per_purchase_sheet"] == 2
    assert component["purchase_sheet_quantity"] == 100
    assert component["estimated_material_cost"] == "40.0000"
    assert result["estimated_material_unit_cost"] == "0.4000"
    assert result["estimated_material_total_cost"] == "40.00"


def test_a3_cover_and_base_are_two_physical_components() -> None:
    material = _material(1)
    result = estimate_order_item_material_cost(
        _FakeSession(material),
        _item(
            quantity=10,
            report_length=500,
            report_width=400,
            base_length=450,
            base_width=350,
        ),
    )

    assert result["material_cost_status"] == "calculated"
    assert [row["source_type"] for row in result["material_cost_components"]] == [
        "order_cover",
        "order_base",
    ]
    assert result["estimated_material_total_cost"] == "7.15"
    assert result["estimated_material_unit_cost"] == "0.7150"


def test_parent_3000_and_adjusted_component_2700_one_to_two_cost_separately() -> None:
    parent_material = _material(1, "1.0000")
    component_material = _material(2, "1.0000")
    item = _item(
        quantity=3000,
        material_id=1,
        report_length=470,
        report_width=600,
        combination_role="set_parent",
    )
    result = estimate_order_item_material_cost(
        _FakeSession(parent_material, component_material),
        item,
        bom_components=[
            {
                "snapshot_component_product_name": "匿名内衬",
                "snapshot_component_report_length_mm": 575,
                "snapshot_component_report_width_mm": 550,
                "effective_required_piece_quantity": 2700,
                "snapshot_component_default_cutting_mode": "一开二",
                "snapshot_component_material_id": 2,
                "snapshot_component_supplier_name": "匿名供应商",
                "snapshot_component_layer_count": 3,
                "snapshot_component_flute_type": "B",
            }
        ],
    )

    assert result["material_cost_status"] == "calculated"
    parent, component = result["material_cost_components"]
    assert parent["required_piece_quantity"] == 3000
    assert parent["purchase_sheet_quantity"] == 3000
    assert component["required_piece_quantity"] == 2700
    assert component["purchase_sheet_quantity"] == 1350
    assert component["estimated_material_cost"] == "426.9375"
    assert result["estimated_material_total_cost"] == "1272.94"


def test_missing_dimensions_fail_closed_without_zero_cost() -> None:
    material = _material(1)
    result = estimate_order_item_material_cost(
        _FakeSession(material),
        _item(quantity=100, report_length=None, report_width=None),
    )

    assert result["material_cost_status"] == "missing"
    assert result["estimated_material_total_cost"] is None
    assert result["estimated_cost"] is None
    assert "主片缺少报料长宽" in result["material_cost_missing_items"]


def test_order_detail_shows_compact_material_scope_and_expandable_basis() -> None:
    assert "orderMaterialCostSummary(item)" in INDEX
    assert "查看材料成本依据" in INDEX
    assert "未计生产损耗和加工费" in INDEX
    assert "item.material_cost_components" in INDEX
