"""Synthetic starting facts for the round UAT; never a production data loader.

These fixtures make the three changed pages reviewable. They do not stand in
for the API workflow regression tests or represent historical factory data.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import os


def seed(session, shared: Path, customer, actor):
    boundary = Path(os.environ["ERP_ROUND_TEST_ROOT"]).resolve()
    database = Path(session.bind.url.database).resolve()
    if (os.environ.get("ERP_ENVIRONMENT") != "test"
            or boundary.parent != Path("D:/tm-uat").resolve()
            or not boundary.name.startswith("round-upgrade-")
            or not database.is_relative_to(boundary)
            or not shared.resolve().is_relative_to(boundary)):
        raise RuntimeError("Synthetic round fixture boundary rejected")
    from app.models.material import Material
    from app.models.product import Product
    from app.models.order import Order, OrderItem
    from app.models.production import ProductionTask
    from app.models.delivery_backlog import DeliveryBacklog
    from app.models.warehouse_inventory import (InventoryLot, InventoryReservation,
        WarehouseLocation, SemiFinishedInventoryDetail, SemiFinishedLotAllowedProduct,
        OrderItemSemiRequirement, FinishedGoodsInventoryDetail)

    material = Material(code="ROUND-M", layer_count=3, quote_price=Decimal("2"),
        price_unit="元/㎡", supplier_name="隔离测试纸板供应商", is_active=True,
        purchase_currency="CNY", purchase_tax_included=True, purchase_tax_rate=Decimal("0.13"))
    session.add(material)
    session.flush()
    pending = Product(customer_id=customer.id, product_code="UAT-PENDING", customer_material_code="UAT-PENDING",
        product_name="待报料图纸与资料体验", box_category="normal", box_style="A1", unit="只",
        length_mm=300, width_mm=200, height_mm=100, flute_type="B")
    production = Product(customer_id=customer.id, product_code="UAT-THREE-SOURCES", customer_material_code="UAT-THREE-SOURCES",
        product_name="三批纸板取用体验", box_category="normal", box_style="A1", unit="只",
        length_mm=300, width_mm=200, height_mm=100, flute_type="B", layer_count=3,
        material_id=material.id, report_length_mm=1020, report_width_mm=310)
    external = Product(customer_id=customer.id, product_code="UAT-2-TO-1", customer_material_code="UAT-2-TO-1",
        product_name="双数量待补送体验", box_category="normal", box_style="A1", unit="只",
        supply_mode="external_purchase", external_packaging_category_code="other_packaging",
        external_packaging_specification_json='{"summary":"合成演示包材"}',
        external_packaging_specification_summary="合成演示包材", external_packaging_purchase_unit="片",
        external_packaging_candidate_snapshot_json="[]",
        external_packaging_default_order_quantity_basis=2, external_packaging_default_purchase_quantity_basis=1,
        length_mm=300, width_mm=200, height_mm=100)
    session.add_all([pending, production, external])
    session.flush()
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    def order_item(code, product, quantity, **values):
        order = Order(order_number=code, customer_id=customer.id, customer_po=code,
            order_date=date.today(), delivery_date=date.today()+timedelta(days=7),
            status="pending_production", payment_status="unpaid", total_amount=Decimal(quantity)*Decimal("2.5"))
        session.add(order)
        session.flush()
        item = OrderItem(order_id=order.id, product_id=product.id, quantity=quantity,
            unit_price=Decimal("2.5"), subtotal=Decimal(quantity)*Decimal("2.5"),
            snapshot_product_code=product.product_code, snapshot_product_name=product.product_name,
            snapshot_spec="300×200×100", snapshot_material="ROUND-M", snapshot_report_length_mm=1020,
            snapshot_report_width_mm=310, special_process="一开一", sales_unit_snapshot="只", **values)
        session.add(item)
        session.flush()
        return order, item

    _, pending_item = order_item("UAT-PENDING-001", pending, 100, material_status="pending", requisition_status="未报料")
    # A genuine PDF in the isolated attachment directory, not a broken link.
    from reportlab.pdfgen import canvas
    pdf = shared / "data/private_uploads/order-drawings/round-frozen-a.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    page = canvas.Canvas(str(pdf), pagesize=(420, 300))
    page.drawString(30, 265, "ROUND UAT - synthetic frozen drawing A")
    page.rect(50, 70, 300, 150)
    page.drawString(100, 35, "300 x 200 x 100 mm - test data only")
    page.save()
    pending_item.drawing_file = "private:order-drawings/round-frozen-a.pdf"

    order, item = order_item("UAT-PRODUCTION-001", production, 60, material_status="received", requisition_status="已报料")
    task = ProductionTask(order_item_id=item.id, status="pending", planned_quantity=60,
        material_input_quantity=0, finished_coverage_snapshot=0, readiness_basis="inventory", version=1)
    session.add(task)
    requirement = OrderItemSemiRequirement(order_item_id=item.id, customer_id=customer.id,
        component_type="whole", board_length_mm=1020, board_width_mm=310,
        material_code_snapshot="ROUND-M", normalized_material_code="ROUND-M", flute_type="B",
        pieces_per_box=1, stock_yield_per_sheet=1, required_piece_quantity=60)
    session.add(requirement)
    session.flush()
    locations = []
    for number, quantity in enumerate((10, 20, 30), start=1):
        location = WarehouseLocation(location_code=f"UAT-MAT-{number}", location_name=f"隔离纸板位 {number}",
            warehouse_type="semi_finished", warehouse_floor=3)
        session.add(location)
        session.flush()
        locations.append(location)
        lot = InventoryLot(lot_number=f"UAT-BOARD-{number}", inventory_type="semi_finished",
            warehouse_location_id=location.id, quantity_available=0, quantity_reserved=quantity,
            unit="sheets", status="active", source_type="manual", stock_date=date.today(), last_movement_at=now, version=1)
        session.add(lot)
        session.flush()
        session.add_all([
            SemiFinishedInventoryDetail(inventory_lot_id=lot.id, owner_customer_id=customer.id,
                owner_customer_name_snapshot=customer.name, material_code_snapshot="ROUND-M",
                normalized_material_code="ROUND-M", layer_count=3, flute_type="B", board_length_mm=1020,
                board_width_mm=310, component_type="whole", pieces_per_box=1, stock_yield_per_sheet=1, sheet_type="raw_board"),
            SemiFinishedLotAllowedProduct(inventory_lot_id=lot.id, product_id=production.id, confirmed_at=now),
            InventoryReservation(reservation_number=f"UAT-SR-{number}", inventory_lot_id=lot.id,
                reservation_type="semi_order", order_id=order.id, order_item_id=item.id,
                semi_requirement_id=requirement.id, reserved_stock_quantity=quantity,
                credited_requirement_quantity=quantity, yield_factor=1, status="active", reserved_at=now, idempotency_key=f"uat-sr-{number}")])
    order, item = order_item("UAT-BACKLOG-001", external, 1000, material_status="received", requisition_status="已报料",
        supply_mode_snapshot="external_purchase", external_packaging_category_code_snapshot="other_packaging",
        external_packaging_specification_json_snapshot='{"summary":"合成演示包材"}',
        external_packaging_specification_summary_snapshot="合成演示包材", external_packaging_purchase_unit_snapshot="片",
        external_packaging_candidate_snapshot_json="[]", external_packaging_product_version_snapshot=1,
        external_packaging_order_quantity_basis_snapshot=2, external_packaging_purchase_quantity_basis_snapshot=1,
        external_packaging_quantity_per_finished_unit_snapshot=Decimal("0.5"))
    location = WarehouseLocation(location_code="UAT-FIN", location_name="隔离成品位", warehouse_type="finished", warehouse_floor=3)
    session.add(location)
    session.flush()
    lot = InventoryLot(lot_number="UAT-FIN-500", inventory_type="finished", warehouse_location_id=location.id,
        quantity_reserved=500, quantity_available=0, unit="boxes", status="active", source_type="manual", stock_date=date.today(), last_movement_at=now, version=1)
    session.add(lot)
    session.flush()
    session.add_all([
        FinishedGoodsInventoryDetail(inventory_lot_id=lot.id, owner_customer_id=customer.id,
            owner_customer_name_snapshot=customer.name, product_id=external.id,
            inventory_code_snapshot=external.product_code, product_name_snapshot=external.product_name, is_general=False),
        InventoryReservation(reservation_number="UAT-FR-500", inventory_lot_id=lot.id, reservation_type="finished_order",
            order_id=order.id, order_item_id=item.id, reserved_stock_quantity=500, credited_requirement_quantity=1000,
            yield_factor=1, status="active", reserved_at=now, idempotency_key="uat-fr-500"),
        DeliveryBacklog(customer_id=customer.id, product_id=external.id, order_item_id=item.id,
            stock_code=external.product_code, product_name=external.product_name, customer_order_no=order.customer_po,
            due_date=order.delivery_date, original_quantity=1000, target_quantity=1000,
            reason="隔离合成体验：客户 1000 只对应实物 500 片", created_by=actor.id)])
    session.flush()
    return {"pending_item_id": pending_item.id, "production_task_id": task.id,
            "backlog_item_id": item.id, "fixture_type": "synthetic starting facts; not a factory snapshot"}
