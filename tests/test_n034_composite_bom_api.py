from __future__ import annotations

import inspect
from decimal import Decimal

import pytest

from app.api import orders, products
from app.models.material import Material
from app.models.mold_tool import MoldTool
from app.models.order import OrderItem
from app.models.product import Product
from app.models.product_bom import ProductBomComponent, SalesOrderItemBomComponent
from app.services.composite_bom import (
    CompositeBOMError,
    _models,
    _relation_kwargs,
    _snapshot_kwargs,
    _snapshot_response,
    _validate_die_cut_mold,
    create_order_item_bom_snapshots,
    get_order_item_bom_components_by_item_ids,
    internal_component_code,
    validate_component_graph,
)


def _route_methods(router, path: str) -> set[str]:
    for route in router.routes:
        if route.path == path:
            return set(route.methods or ())
    return set()


def test_n034_bom_routes_and_product_flags_are_exposed() -> None:
    assert _route_methods(products.router, "/{product_id}/bom") == {"GET"}
    assert any(
        route.path == "/{product_id}/bom" and "PUT" in (route.methods or ())
        for route in products.router.routes
    )
    assert _route_methods(orders.router, "/items/{item_id}/bom") == {"GET"}
    assert {"is_composite", "is_internal_component"} <= set(
        products.ProductResponse.model_fields
    )
    assert _models() == (ProductBomComponent, SalesOrderItemBomComponent)


def test_n034_payload_accepts_and_validates_canonical_relation_fields() -> None:
    assert {
        "is_die_cut",
        "mold_tool_id",
        "mold_max_yield_per_sheet",
        "display_mode",
        "is_required",
    } <= set(products.ProductBOMComponentPayload.model_fields)
    payload = products.ProductBOMComponentPayload(
        component_product_id=2,
        quantity_per_set=Decimal("1.5"),
        is_die_cut=True,
        mold_tool_id=41,
        mold_max_yield_per_sheet=6,
        spare_sheet_quantity=3,
        display_mode="show_on_delivery",
        is_required=False,
    )
    assert payload.model_dump()["mold_tool_id"] == 41
    assert payload.display_mode == "show_on_delivery"
    assert payload.is_required is False

    with pytest.raises(ValueError):
        products.ProductBOMComponentPayload(
            component_product_id=2,
            quantity_per_set=1,
            display_mode="production",
        )
    with pytest.raises(ValueError, match="非模切组件"):
        products.ProductBOMComponentPayload(
            component_product_id=2,
            quantity_per_set=1,
            is_die_cut=False,
            mold_tool_id=41,
        )


def test_n034_internal_codes_and_graph_guards() -> None:
    assert internal_component_code("PARENT", 1) == "PARENT-S01"
    assert internal_component_code("PARENT", 12) == "PARENT-S12"

    with pytest.raises(CompositeBOMError, match="自身"):
        validate_component_graph(
            parent_product_id=10,
            component_product_ids=[10],
            adjacency={},
        )
    with pytest.raises(CompositeBOMError, match="重复"):
        validate_component_graph(
            parent_product_id=10,
            component_product_ids=[11, 11],
            adjacency={},
        )
    with pytest.raises(CompositeBOMError, match="嵌套"):
        validate_component_graph(
            parent_product_id=10,
            component_product_ids=[11],
            adjacency={11: {12}},
        )


def test_n034_relation_persists_canonical_fields_and_non_die_cut_spares() -> None:
    values = _relation_kwargs(
        ProductBomComponent,
        parent_product_id=1,
        component_product_id=2,
        quantity_per_set=Decimal("2"),
        position=1,
        internal_code="PARENT-S01",
        actor_id=7,
        relation={
            "is_die_cut": False,
            "die_cut_path": None,
            "mold_tool_id": None,
            "mold_max_yield_per_sheet": None,
            "spare_sheet_quantity": 3,
            "display_mode": "show_on_all_docs",
            "is_required": False,
            "remark": None,
        },
    )

    assert values["parent_product_id"] == 1
    assert values["component_product_id"] == 2
    assert values["display_order"] == 1
    assert values["internal_component_code"] == "PARENT-S01"
    assert values["spare_sheet_quantity"] == 3
    assert values["display_mode"] == "show_on_all_docs"
    assert values["is_required"] is False


def test_n034_die_cut_relation_uses_requested_active_mold_or_product_default() -> None:
    default_mold = MoldTool(id=40, mold_code="M-040", mold_name="默认", is_active=True)
    requested_mold = MoldTool(id=41, mold_code="M-041", mold_name="指定", is_active=True)
    inactive_mold = MoldTool(id=42, mold_code="M-042", mold_name="停用", is_active=False)
    component = Product(id=2, mold_tool_id=default_mold.id, mold_tool=default_mold)

    class MoldLookup:
        def get(self, _model, mold_id):
            return {
                default_mold.id: default_mold,
                requested_mold.id: requested_mold,
                inactive_mold.id: inactive_mold,
            }.get(mold_id)

    lookup = MoldLookup()
    assert _validate_die_cut_mold(
        lookup,
        component,
        position=1,
        is_die_cut=True,
        mold_tool_id=requested_mold.id,
    ) is requested_mold
    assert _validate_die_cut_mold(
        lookup,
        component,
        position=1,
        is_die_cut=True,
        mold_tool_id=None,
    ) is default_mold
    assert _validate_die_cut_mold(
        lookup,
        component,
        position=1,
        is_die_cut=False,
        mold_tool_id=None,
    ) is None
    with pytest.raises(CompositeBOMError, match="启用中的模具"):
        _validate_die_cut_mold(
            lookup,
            component,
            position=1,
            is_die_cut=True,
            mold_tool_id=inactive_mold.id,
        )


def test_n034_order_snapshot_keeps_parent_sets_and_captures_component_material() -> None:
    material = Material(
        id=30,
        code="K=A",
        supplier_name="测试纸板供应商",
        layer_count=5,
        flute_type="BC",
        is_active=True,
    )
    mold = MoldTool(
        id=40,
        mold_code="M-040",
        mold_name="组件模具",
        rack_location="3F-M-P01",
        is_active=True,
    )
    requested_mold = MoldTool(
        id=41,
        mold_code="M-041",
        mold_name="订单关系指定模具",
        rack_location="3F-M-P02",
        is_active=True,
    )
    parent = Product(
        id=1,
        customer_id=9,
        product_code="PARENT",
        customer_material_code="PARENT",
        product_name="组合品",
        box_category="normal",
        unit="套",
        is_composite=True,
        is_active=True,
        version=4,
    )
    component = Product(
        id=2,
        customer_id=9,
        product_code="COMPONENT",
        customer_material_code="COMPONENT",
        product_name="内部组件",
        material_id=material.id,
        material=material,
        mold_tool_id=mold.id,
        mold_tool=mold,
        box_category="die_cut",
        production_process="模切",
        unit="片",
        is_internal_component=True,
        is_active=True,
        version=3,
    )
    order_item = OrderItem(id=50, product_id=parent.id, quantity=12)
    values = _snapshot_kwargs(
        SalesOrderItemBomComponent,
        order_item=order_item,
        parent=parent,
        component=component,
        relation={
            "id": 60,
            "quantity_per_set": Decimal("2"),
            "display_order": 1,
            "internal_component_code": "PARENT-S01",
            "is_die_cut": True,
            "die_cut_path": "drawings/component.png",
            "mold_tool_id": requested_mold.id,
            "_mold_tool": requested_mold,
            "mold_max_yield_per_sheet": 4,
            "spare_sheet_quantity": 2,
            "display_mode": "show_on_delivery",
            "is_required": False,
            "remark": "内部生产",
        },
    )

    assert order_item.quantity == 12
    assert values["order_set_quantity"] == 12
    assert values["quantity_per_set"] == Decimal("2")
    assert values["required_piece_quantity"] == Decimal("24")
    assert values["snapshot_component_supplier_name"] == "测试纸板供应商"
    assert values["snapshot_component_layer_count"] == 5
    assert values["snapshot_component_flute_type"] == "BC"
    assert values["snapshot_component_material"] == "K=A"
    assert values["snapshot_mold_tool_id"] == requested_mold.id
    assert values["snapshot_mold_tool_code"] == requested_mold.mold_code
    assert values["snapshot_mold_tool_name"] == requested_mold.mold_name
    assert values["display_mode"] == "show_on_delivery"
    assert values["is_required"] is False
    missing_required = [
        column.name
        for column in SalesOrderItemBomComponent.__table__.columns
        if not column.primary_key
        and not column.nullable
        and column.default is None
        and column.server_default is None
        and values.get(column.name) is None
    ]
    assert missing_required == []


def test_n034_unlinked_snapshot_facts_serialize_independently_and_bulk_once() -> None:
    rows = [
        SalesOrderItemBomComponent(
            id=70,
            sales_order_item_id=50,
            product_bom_component_id=None,
            component_product_id=2,
            order_set_quantity=12,
            quantity_per_set=Decimal("2"),
            required_piece_quantity=Decimal("24"),
            display_order=1,
            internal_component_code="PARENT-S01",
            is_die_cut=False,
            snapshot_mold_tool_id=None,
            mold_max_yield_per_sheet=None,
            spare_sheet_quantity=5,
            display_mode="show_on_all_docs",
            is_required=False,
            snapshot_component_product_code="HIST-CODE",
            snapshot_component_product_name="历史组件名称",
            snapshot_component_box_category="normal",
        ),
        SalesOrderItemBomComponent(
            id=71,
            sales_order_item_id=51,
            product_bom_component_id=None,
            component_product_id=3,
            order_set_quantity=4,
            quantity_per_set=Decimal("1.5"),
            required_piece_quantity=Decimal("6"),
            display_order=1,
            internal_component_code="PARENT2-S01",
            is_die_cut=True,
            snapshot_mold_tool_id=41,
            snapshot_mold_tool_code="M-041",
            snapshot_mold_tool_name="历史模具",
            mold_max_yield_per_sheet=6,
            spare_sheet_quantity=1,
            display_mode="show_on_delivery",
            is_required=True,
            snapshot_component_product_code="HIST-2",
            snapshot_component_product_name="历史组件二",
            snapshot_component_box_category="die_cut",
        ),
    ]

    class ScalarRows:
        def all(self):
            return rows

    class OneQuerySession:
        def __init__(self):
            self.scalar_calls = 0

        def scalars(self, _statement):
            self.scalar_calls += 1
            return ScalarRows()

    session = OneQuerySession()
    grouped = get_order_item_bom_components_by_item_ids(session, {50, 51})
    assert session.scalar_calls == 1
    assert grouped[50][0]["product_bom_component_id"] is None
    assert grouped[50][0]["product_code"] == "HIST-CODE"
    assert grouped[50][0]["required_piece_quantity"] == Decimal("24")
    assert grouped[50][0]["spare_sheet_quantity"] == 5
    assert grouped[50][0]["display_mode"] == "show_on_all_docs"
    assert grouped[50][0]["is_required"] is False
    assert grouped[51][0]["mold_tool_code"] == "M-041"
    assert _snapshot_response(rows[0], fallback_position=1)["name"] == "历史组件名称"


def test_n034_order_responses_bulk_attach_snapshots_and_phase_a_has_no_fact_writes() -> None:
    response_source = inspect.getsource(orders._order_response)
    list_source = inspect.getsource(orders.list_orders)
    phase_a_source = inspect.getsource(create_order_item_bom_snapshots)
    create_order_source = inspect.getsource(orders.create_order)

    assert '"bom_components": bom_components_by_item_id.get(item.id, [])' in response_source
    assert "get_order_item_bom_components_by_item_ids" in list_source
    assert "bom_components_by_item_id=bom_components_by_item_id" in list_source
    assert "db.add(OrderItem(" not in phase_a_source
    for forbidden_write in (
        "Requisition(",
        "ProductionTask(",
        "InventoryLot(",
        "Delivery(",
        "Statement(",
        "Invoice(",
    ):
        assert forbidden_write not in phase_a_source
    assert "create_order_item_bom_snapshots" in create_order_source
    assert "if is_composite_product(product):" in create_order_source
    assert "continue\n            create_or_refresh_production_task" in create_order_source
