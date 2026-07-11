from datetime import date
from decimal import Decimal

from sqlalchemy.orm import sessionmaker


def _order(db, customer_id, product_id, number, delivery_date, quantity, *, status="pending_delivery", customer_po=None, delivered=0):
    from app.models.order import Order, OrderItem

    order = Order(
        order_number=number,
        customer_id=customer_id,
        customer_po=customer_po,
        order_date=date(2026, 6, 20),
        delivery_date=delivery_date,
        status=status,
        payment_status="unpaid",
        total_amount=Decimal("0"),
    )
    db.add(order)
    db.flush()
    item = OrderItem(
        order_id=order.id,
        product_id=product_id,
        quantity=quantity,
        delivered_quantity=delivered,
        unit_price=Decimal("0"),
        subtotal=Decimal("0"),
        material_status="received",
        snapshot_product_name="测试产品",
        snapshot_product_code="21301877",
    )
    db.add(item)
    db.flush()
    return order, item


def test_tianhua_candidate_scoring_and_invalid_order_filters(tmp_path):
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.models.product import Product
    from app.services.tianhua_pre_delivery import RecognizedRow, preprocess_row

    engine = create_sqlite_engine(tmp_path / "matching.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        customer = Customer(
            customer_number=1,
            customer_code="天华",
            name="苏州天华超净科技股份有限公司",
            credit_limit=Decimal("0"),
        )
        db.add(customer)
        db.flush()
        product = Product(
            customer_id=customer.id,
            product_code="21301877",
            customer_material_code="21301877",
            product_name="测试产品",
            box_category="normal",
        )
        mismatch_product = Product(
            customer_id=customer.id,
            product_code="21302001",
            customer_material_code="21302001",
            product_name="数量差异产品",
            box_category="normal",
        )
        db.add_all([product, mismatch_product])
        db.flush()

        order_a, item_a = _order(
            db, customer.id, product.id, "TH-A", date(2026, 7, 1), 200,
            customer_po="PO-A",
        )
        order_b, item_b = _order(
            db, customer.id, product.id, "TH-B", date(2026, 7, 5), 200,
            customer_po="PO-B",
        )
        _order(
            db, customer.id, product.id, "RUIDA-OLD", date(2026, 7, 5), 200
        )
        _order(
            db, customer.id, product.id, "TH-COMPLETED",
            date(2026, 7, 5), 200, status="completed",
        )
        _order(
            db, customer.id, product.id, "TH-RECONCILED",
            date(2026, 7, 5), 200, status="pending_reconciliation",
        )
        _order(
            db, customer.id, product.id, "TH-DELIVERED",
            date(2026, 7, 5), 200, delivered=200,
        )
        mismatch_order, mismatch_item = _order(
            db, customer.id, mismatch_product.id, "TH-MISMATCH",
            date(2026, 7, 2), 31,
        )
        db.commit()

        by_order = preprocess_row(
            db,
            RecognizedRow(
                1,
                "21301877 TH-A 200",
                "21301877",
                200,
                "TH-A",
            ),
            date(2026, 7, 5),
        )
        assert by_order["order_item_id"] == item_a.id
        assert by_order["order_id"] == order_a.id
        assert by_order["customer_order_no"] == "PO-A"
        assert by_order["match_reason"].startswith("按订单号+数量完全匹配")

        by_date = preprocess_row(
            db,
            RecognizedRow(2, "21301877 200", "21301877", 200),
            date(2026, 7, 6),
        )
        assert by_date["order_item_id"] == item_b.id
        assert by_date["order_id"] == order_b.id
        assert by_date["candidate_count"] == 2
        assert "按预送货日期最近匹配" in by_date["match_reason"]
        assert "已排除 RUIDA" in by_date["match_reason"]

        mismatch = preprocess_row(
            db,
            RecognizedRow(3, "21302001 30", "21302001", 30),
            date(2026, 7, 2),
        )
        assert mismatch["order_item_id"] == mismatch_item.id
        assert mismatch["order_id"] == mismatch_order.id
        assert mismatch["status"] == "qty_mismatch"
        assert "系统未送数量 31" in mismatch["warning"]


def test_tianhua_frontends_show_matching_context():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    desktop = (root / "static" / "index.html").read_text(encoding="utf-8")
    mobile = (root / "static" / "mobile_tianhua_pick.html").read_text(
        encoding="utf-8"
    )
    for text in ("预送货日期", "候选数", "匹配说明", "candidate_count"):
        assert text in desktop
    assert "订单：" not in mobile
    assert "预送货日期：" not in mobile
    assert "specification" in mobile
    assert "finished_inventory_sources" in mobile
