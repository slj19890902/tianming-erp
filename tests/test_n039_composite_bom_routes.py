from __future__ import annotations

import inspect

from app.api import deliveries, orders


def test_order_routes_snapshot_components_and_record_quantity_adjustments() -> None:
    create_source = inspect.getsource(orders.create_order)
    update_source = inspect.getsource(orders.update_order_item)

    assert "create_order_item_bom_snapshots" in create_source
    assert "create_or_refresh_production_task" in create_source
    assert "append_order_quantity_adjustments" in update_source
    assert "quantity_adjustment_idempotency_key" in update_source
    assert "refresh_production_task" in update_source


def test_delivery_routes_use_component_kit_capacity_and_atomic_allocations() -> None:
    remaining_source = inspect.getsource(deliveries._delivery_remaining_quantity)
    dispatch_source = inspect.getsource(deliveries.dispatch_delivery)
    cancel_source = inspect.getsource(deliveries.cancel_delivery)
    inventory_source = inspect.getsource(
        deliveries._composite_inventory_sources_for_order_item
    )

    assert "kit_availability" in remaining_source
    assert "execute_delivery_component_consumption" in dispatch_source
    assert "reverse_delivery_component_allocations" in cancel_source
    assert "component_stock" in inventory_source
    assert "component_direct" in inventory_source

