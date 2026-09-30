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
            or boundary.parent not in {Path("D:/tm-uat").resolve(), Path("C:/ERP-OPT10-20260930").resolve()}
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

    from app.models.supplier import Supplier
    from app.services.supplier_master import normalize_supplier_identity
    supplier_name = "隔离测试纸板供应商"
    supplier = Supplier(standard_name=supplier_name, normalized_name=normalize_supplier_identity(supplier_name),
        display_name=supplier_name, is_active=True, sort_order=1, version=1)
    session.add(supplier)
    material = Material(code="K7A", layer_count=3, flute_type="B", quote_price=Decimal("2"),
        price_unit="元/㎡", supplier_name="隔离测试纸板供应商", is_active=True,
        purchase_currency="CNY", purchase_tax_included=True, purchase_tax_rate=Decimal("0.13"))
    session.add(material)
    session.flush()
    from app.api.materials import MaterialResponse
    MaterialResponse.model_validate(material)
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
            snapshot_spec="300×200×100", snapshot_material="K7A", snapshot_report_length_mm=1020,
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
        material_code_snapshot="K7A", normalized_material_code="K7A", flute_type="B",
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
                owner_customer_name_snapshot=customer.name, material_code_snapshot="K7A",
                normalized_material_code="K7A", layer_count=3, flute_type="B", board_length_mm=1020,
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
    _external_receipt_facts(session, supplier, customer, actor, order, item, lot)
    return {"pending_item_id": pending_item.id, "production_task_id": task.id,
            "backlog_item_id": item.id, "fixture_type": "synthetic starting facts; not a factory snapshot"}


def _external_receipt_facts(db, supplier, customer, actor, order, item, lot):
    """Consistent synthetic purchase/receipt facts for the real delivery entry.

    This is fixture setup, not evidence that the purchase UI was exercised.
    Every FK, frozen unit, price and receipt identity is retained.
    """
    from app.models.supplier import ExternalPackagingProduct, SupplierSupplyCategory
    from app.models.external_packaging_price import ExternalPackagingPriceVersion
    from app.models.order_external_packaging import (SalesOrderItemExternalComponent,
        SalesOrderItemExternalComponentCandidate)
    from app.models.external_packaging_purchase import (ExternalPackagingPurchaseBatch,
        ExternalPackagingPurchaseOrder, ExternalPackagingPurchaseItem,
        ExternalPackagingReceipt, ExternalPackagingReceiptItem)
    from app.services.external_physical_receipt import freeze_purchase_piece_cost
    from app.services.external_packaging_purchase import _amounts

    spec = '{"summary":"合成演示包材"}'
    db.add(SupplierSupplyCategory(supplier_id=supplier.id, category_code="other_packaging"))
    supplied = ExternalPackagingProduct(supplier_id=supplier.id, category_code="other_packaging",
        supplier_product_code="UAT-EXTERNAL", normalized_supplier_product_code="UAT-EXTERNAL",
        product_name="隔离采购包材", purchase_unit="片", specification_summary="合成演示包材",
        specification_json=spec, customer_scope_id=customer.id, is_active=True, version=1)
    db.add(supplied); db.flush()
    price = ExternalPackagingPriceVersion(external_product_id=supplied.id, version_number=1,
        product_version=1, specification_snapshot_json=spec, quote_unit="片",
        unit_conversion_basis="采购单位直接计价", currency="CNY", tax_mode="tax_inclusive",
        tax_rate=Decimal("0.13"), unit_price=Decimal("1"), effective_from=date(2026, 1, 1),
        tier_prices_json="[]", shipping_fee_mode="not_provided", evidence_reference="ROUND-UAT synthetic",
        quote_fingerprint="round-uat-price", created_by=actor.id)
    component = SalesOrderItemExternalComponent(sales_order_item_id=item.id, source_kind="direct_product",
        source_component_set_version=1, display_order=1, purpose="外购成品",
        quantity_per_finished_unit=Decimal("0.5"), consumption_unit="片", is_required=True,
        category_code="other_packaging", specification_json=spec, specification_summary="合成演示包材")
    batch = ExternalPackagingPurchaseBatch(sales_order_id=order.id, idempotency_key="round-uat-purchase",
        request_fingerprint="round-uat-purchase", confirmed_by=actor.id)
    db.add_all([price, component, batch]); db.flush()
    line_amount, tax_amount, total_amount = _amounts(price, quantity=Decimal("500"), unit_price=Decimal("1"))
    candidate = SalesOrderItemExternalComponentCandidate(order_component_id=component.id,
        external_product_id_snapshot=supplied.id, is_default=True, supplier_id_snapshot=supplier.id,
        supplier_name_snapshot=supplier.standard_name, supplier_product_code_snapshot="UAT-EXTERNAL",
        product_name_snapshot=supplied.product_name, purchase_unit_snapshot="片",
        customer_scope_id_snapshot=customer.id, external_product_version_snapshot=1)
    purchase_order = ExternalPackagingPurchaseOrder(batch_id=batch.id, purchase_number="UAT-PO-001",
        supplier_id=supplier.id, supplier_name_snapshot=supplier.standard_name, currency="CNY",
        goods_amount=line_amount, tax_amount=tax_amount, total_amount=total_amount, confirmed_by=actor.id)
    db.add_all([candidate, purchase_order]); db.flush()
    purchase = ExternalPackagingPurchaseItem(purchase_order_id=purchase_order.id, sales_order_id=order.id,
        sales_order_item_id=item.id, order_component_id=component.id, order_candidate_id=candidate.id,
        purpose_snapshot="外购成品", category_code_snapshot="other_packaging",
        specification_summary_snapshot="合成演示包材", specification_json_snapshot=spec,
        external_product_id_snapshot=supplied.id, external_product_version_snapshot=1,
        supplier_product_code_snapshot="UAT-EXTERNAL", product_name_snapshot=supplied.product_name,
        price_version_id=price.id, price_version_number_snapshot=1, purchase_quantity=Decimal("500"),
        purchase_unit="片", unit_price=Decimal("1"), currency="CNY", tax_mode="tax_inclusive",
        tax_rate=Decimal("0.13"), line_amount=line_amount, tax_amount=tax_amount,
        total_amount=total_amount, shipping_fee_mode="not_provided",
        price_evidence_reference_snapshot="ROUND-UAT synthetic")
    receipt = ExternalPackagingReceipt(purchase_order_id=purchase_order.id, receipt_number="UAT-RCPT-001",
        idempotency_key="round-uat-receipt", request_fingerprint="round-uat-receipt", received_by=actor.id)
    db.add_all([purchase, receipt]); db.flush()
    received = ExternalPackagingReceiptItem(receipt_id=receipt.id, purchase_item_id=purchase.id,
        received_quantity=Decimal("500"), purchase_unit_snapshot="片", converted_finished_quantity=1000)
    db.add(received); db.flush()
    lot.source_type = "purchase_reserve"
    lot.source_ref_type = "direct_external_receipt"
    lot.source_ref_id = received.id
    freeze_purchase_piece_cost(lot, purchase=purchase, receipt=received,
        customer_id=customer.id, product_id=item.product_id)
    db.flush()
    from app.api.deliveries import pending_delivery_customer_options, _delivery_remaining_quantity
    assert _delivery_remaining_quantity(db, item) == 1000
    assert any(row["customer_id"] == customer.id for row in pending_delivery_customer_options(db, actor)["items"])
