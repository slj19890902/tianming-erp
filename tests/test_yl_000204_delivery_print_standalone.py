"""The production graph's self root must not become a delivered kit line."""

from types import SimpleNamespace

from app.api import deliveries
from app.services.composite_bom_workflow import ComponentDemand
from app.services.delivery_goods_projection import is_standalone_graph_root


def _demand(product_id: int, *, root: bool) -> ComponentDemand:
    return ComponentDemand(
        snapshot_id=57 if root else 58,
        order_item_id=10592,
        component_product_id=product_id,
        component_code=f"GRAPH-{product_id}" if root else "YL-CHILD",
        component_name="30入装风机纸箱" if root else "真实子件",
        specification=None,
        quantity_per_set=1,
        is_required=True,
        effective_sets=50,
        required_piece_quantity=50,
        unit="只",
        is_graph_root=root,
    )


def test_single_graph_root_prints_one_parent_with_frozen_unit(monkeypatch) -> None:
    root = _demand(3494, root=True)
    monkeypatch.setattr(deliveries, "delivery_component_demands", lambda _db, _id: [root])
    item = SimpleNamespace(id=10592, product_id=3494)
    metadata = deliveries._delivery_kit_metadata(object(), item, planned_delivery_quantity=50)
    assert metadata["is_composite_bom"] is False
    assert metadata["component_lines"] == []

    lines = deliveries._delivery_document_goods_lines(
        order_item_id=item.id,
        product_code="Z.001.000204",
        product_name="30入装风机纸箱",
        specification="1150×795×576",
        parent_quantity=50,
        kit_metadata=metadata,
        parent_unit="只",
    )
    assert [(line["line_type"], line["product_code"], line["quantity"], line["unit"])
            for line in lines] == [("parent", "Z.001.000204", 50, "只")]


def test_real_bom_with_graph_root_retains_components() -> None:
    root = _demand(3494, root=True)
    child = _demand(3771, root=False)
    assert is_standalone_graph_root([root], 3494)
    assert not is_standalone_graph_root([root, child], 3494)
    assert not is_standalone_graph_root([child], 3494)
    assert not is_standalone_graph_root([root], 9999)


def test_list_projection_omits_standalone_root() -> None:
    order_item = SimpleNamespace(id=10592, product_id=3494)
    context = {"component_demands_by_order_item": {order_item.id: [_demand(3494, root=True)]}}
    assert deliveries._delivery_list_kit_metadata(
        context, order_item=order_item, planned_delivery_quantity=50,
        delivery_item_id=1210, dispatched=True,
    ) == deliveries._empty_delivery_kit_metadata()
