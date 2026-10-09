from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker


def test_finance_cards_use_issued_collectible_balances_instead_of_status(
    tmp_path: Path,
) -> None:
    from app.api.auth import router as auth_router
    from app.api.dashboard import router as dashboard_router
    from app.api.deps import get_db
    from app.api.finance import router as finance_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.finance import Statement
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "q0-05-finance-cards.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    statement_month = date.today().strftime("%Y-%m")
    with factory() as db:
        user = User(
            username="admin",
            password_hash=hash_password("RolePass123!"),
            role="admin",
            real_name="管理员",
            must_change_password=False,
        )
        customer = Customer(name="匿名首页口径客户")
        db.add_all([user, customer])
        db.flush()
        db.add_all(
            [
                Statement(
                    statement_number="ST-Q005-1",
                    customer_id=customer.id,
                    statement_month=statement_month,
                    total_receivable=Decimal("100.00"),
                    invoiced_amount=Decimal("40.00"),
                    settled_amount=Decimal("25.00"),
                    total_gross_profit=Decimal("0.00"),
                    status="settled",
                    confirmation_status="confirmed",
                ),
                Statement(
                    statement_number="ST-Q005-2",
                    customer_id=customer.id,
                    statement_month=statement_month,
                    total_receivable=Decimal("50.00"),
                    invoiced_amount=Decimal("50.00"),
                    settled_amount=Decimal("50.00"),
                    total_gross_profit=Decimal("0.00"),
                    status="unsettled",
                    confirmation_status="confirmed",
                ),
                Statement(
                    statement_number="ST-Q005-3",
                    customer_id=customer.id,
                    statement_month=statement_month,
                    total_receivable=Decimal("30.00"),
                    invoiced_amount=Decimal("10.00"),
                    settled_amount=Decimal("5.00"),
                    total_gross_profit=Decimal("0.00"),
                    status="settled",
                    confirmation_status="confirmed",
                ),
            ]
        )
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(dashboard_router, prefix="/api/dashboard")
    app.include_router(finance_router, prefix="/api/finance")

    def override_get_db() -> Generator[Session, None, None]:
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as client:
        assert client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "RolePass123!"},
        ).status_code == 200
        overview_response = client.get("/api/dashboard/overview")
        payment_response = client.get(
            "/api/finance/statements",
            params={
                "statement_month": statement_month,
                "balance_type": "pending_payment",
            },
        )
        current_payment_response = client.get(
            "/api/finance/current-customer-months",
            params={
                "statement_month": statement_month,
                "balance_type": "pending_payment",
            },
        )

    assert overview_response.status_code == 200
    body = overview_response.json()
    cards = {card["key"]: card for card in body["cards"]}
    assert cards["pending_invoice"]["count"] == 1
    assert Decimal(cards["pending_invoice"]["amount"]) == Decimal("80.00")
    assert cards["pending_payment"]["count"] == 1
    assert Decimal(cards["pending_payment"]["amount"]) == Decimal("20.00")
    assert cards["pending_payment"]["count_unit"] == "客户"
    assert cards["pending_payment"]["identity_key"] == "settlement_entity_id or customer_id"
    assert cards["pending_payment"]["statement_month"] == statement_month
    assert cards["pending_payment"]["target_filter"] == {
        "balance_type": "pending_payment",
        "statement_month": "",
        "all_open": True,
    }
    assert body["statement_month"] == statement_month
    assert body["timezone"] == "Asia/Shanghai"
    assert body["as_of"].endswith("+08:00")

    assert payment_response.status_code == 200
    payment_page = payment_response.json()
    assert payment_page["total"] == 2
    assert payment_page["customer_count"] == 1
    assert [row["statement_number"] for row in payment_page["items"]] == [
        "ST-Q005-3",
        "ST-Q005-1"
    ]
    assert current_payment_response.status_code == 200
    current_payment_page = current_payment_response.json()
    assert current_payment_page["total"] == cards["pending_payment"]["count"]
    assert Decimal(
        str(current_payment_page["summary"]["pending_payment_action_amount"])
    ) == Decimal(cards["pending_payment"]["amount"])
    assert [
        row["customer_id"] for row in current_payment_page["items"]
    ] == [customer.id]


def test_dashboard_frontend_carries_deterministic_filters_and_clears_stale_data() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "static" / "index.html"
    ).read_text(encoding="utf-8")

    workbench = (Path(__file__).resolve().parents[1] / "static/ui/home-workbench.js").read_text(encoding="utf-8")
    assert "vm.homeOpenTask(r)" in workbench
    assert "this.openDashboardTarget(row)" in workbench
    assert 'status:"dispatched", return_status:"waiting_receipt"' in source
    assert 'deliveryDashboardMode = "pending_customers"' in source
    assert "const rawFinanceFilters = {...this.financeFilters}" in source
    assert "balance_type:rawFinanceFilters.balance_type || undefined" in source
    assert "statement_month:rawFinanceFilters.statement_month || undefined" in source
    assert 'axios.get("/api/finance/current-customer-months"' in source
    assert "{{ financeCurrentTotal }} 个结算对象账期" in source
    loading_index = source.index("async loadOverview()")
    request_index = source.index('axios.get("/api/dashboard/overview",', loading_index)
    assert "this.overview = { cards: [], todos: [], summary: {} };" not in source[loading_index:request_index]
    assert "以下保留上次数据" in workbench


def test_pending_delivery_customer_summary_matches_page_without_item_n_plus_one(
    tmp_path: Path,
) -> None:
    from app.api.deliveries import (
        pending_delivery_customer_summaries,
        pending_delivery_items,
    )
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "q0-05-delivery-summary.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        user = User(
            username="admin",
            password_hash="unused",
            role="admin",
            real_name="管理员",
            must_change_password=False,
        )
        customers = [Customer(name="匿名客户甲"), Customer(name="匿名客户乙")]
        db.add_all([user, *customers])
        db.flush()
        for customer_index, customer in enumerate(customers, start=1):
            product = Product(
                customer_id=customer.id,
                product_code=f"Q005-{customer_index}",
                customer_material_code=f"Q005-{customer_index}",
                product_name=f"匿名纸箱{customer_index}",
                box_category="normal",
            )
            db.add(product)
            db.flush()
            for item_index in range(15):
                order = Order(
                    order_number=f"Q005-{customer_index}-{item_index:02d}",
                    customer_id=customer.id,
                    order_date=date.today(),
                    delivery_date=date.today(),
                    status="pending_delivery",
                    payment_status="unpaid",
                    total_amount=Decimal("0"),
                )
                db.add(order)
                db.flush()
                db.add(
                    OrderItem(
                        order_id=order.id,
                        product_id=product.id,
                        quantity=10,
                        delivered_quantity=0,
                        unit_price=Decimal("1.00"),
                        subtotal=Decimal("10.00"),
                        material_status="received",
                        requisition_status="已入库",
                        snapshot_product_name=product.product_name,
                        snapshot_product_code=product.product_code,
                    )
                )
        db.commit()

    query_count = 0

    def count_query(_conn, _cursor, _statement, _params, _context, _many) -> None:
        nonlocal query_count
        query_count += 1

    event.listen(engine, "before_cursor_execute", count_query)
    try:
        with factory() as db:
            user = db.query(User).filter(User.username == "admin").one()
            query_count = 0
            summaries = pending_delivery_customer_summaries(db, user=user)
            summary_query_count = query_count
            page_rows = pending_delivery_items(db=db, user=user)["items"]
    finally:
        event.remove(engine, "before_cursor_execute", count_query)

    # Current delivery readiness includes one fixed, batched lookup for
    # customer/product surplus finished goods.  The budget remains independent
    # of the 30 order-item rows and therefore still guards against N+1 queries.
    assert summary_query_count <= 13
    assert len(summaries) == 2
    assert {row["customer_id"] for row in summaries} == {
        row["customer_id"] for row in page_rows
    }
    assert sum(row["item_count"] for row in summaries) == len(page_rows) == 30


def test_composite_pending_delivery_summary_uses_fixed_query_count(
    tmp_path: Path,
) -> None:
    from app.api.deliveries import pending_delivery_customer_summaries
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.models.production import (
        ProductionCompletion,
        ProductionCompletionBatch,
        ProductionTask,
    )
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "q0-05-composite-summary.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        user = User(
            username="admin",
            password_hash="unused",
            role="admin",
            real_name="管理员",
            must_change_password=False,
        )
        customers = [Customer(name="匿名套件客户甲"), Customer(name="匿名套件客户乙")]
        db.add_all([user, *customers])
        db.flush()
        batch = ProductionCompletionBatch(
            idempotency_key="q0-05-composite-batch",
            request_hash="b" * 64,
            item_count=24,
            completed_at=datetime.now(),
        )
        db.add(batch)
        db.flush()
        for customer_index, customer in enumerate(customers, start=1):
            parent = Product(
                customer_id=customer.id,
                product_code=f"Q005-KIT-{customer_index}",
                customer_material_code=f"Q005-KIT-{customer_index}",
                product_name=f"匿名套件{customer_index}",
                box_category="normal",
            )
            component = Product(
                customer_id=customer.id,
                product_code=f"Q005-COMP-{customer_index}",
                customer_material_code=f"Q005-COMP-{customer_index}",
                product_name=f"匿名组件{customer_index}",
                box_category="die_cut",
            )
            db.add_all([parent, component])
            db.flush()
            for item_index in range(12):
                order = Order(
                    order_number=f"Q005-KIT-{customer_index}-{item_index:02d}",
                    customer_id=customer.id,
                    order_date=date.today(),
                    delivery_date=date.today(),
                    status="pending_delivery",
                    payment_status="unpaid",
                    total_amount=Decimal("10.00"),
                )
                db.add(order)
                db.flush()
                item = OrderItem(
                    order_id=order.id,
                    product_id=parent.id,
                    quantity=10,
                    delivered_quantity=0,
                    unit_price=Decimal("1.00"),
                    subtotal=Decimal("10.00"),
                    material_status="received",
                    requisition_status="已入库",
                    snapshot_product_name=parent.product_name,
                    snapshot_product_code=parent.product_code,
                )
                db.add(item)
                db.flush()
                snapshot = SalesOrderItemBomComponent(
                    sales_order_item_id=item.id,
                    component_product_id=component.id,
                    parent_product_version=1,
                    component_product_version=1,
                    snapshot_schema_version=3,
                    order_set_quantity=10,
                    quantity_per_set=Decimal("1"),
                    required_piece_quantity=Decimal("10"),
                    display_order=1,
                    internal_component_code=f"Q005-COMP-{customer_index}",
                    is_die_cut=False,
                    spare_sheet_quantity=0,
                    display_mode="show_on_delivery",
                    is_required=True,
                    snapshot_component_product_code=component.product_code,
                    snapshot_component_product_name=component.product_name,
                    snapshot_component_box_category="die_cut_inner",
                    snapshot_component_default_cutting_mode="一开一",
                )
                db.add(snapshot)
                db.flush()
                task = ProductionTask(
                    order_item_id=item.id,
                    sales_order_item_bom_component_id=snapshot.id,
                    task_role="component_internal",
                    status="completed",
                    planned_quantity=10,
                    finished_coverage_snapshot=0,
                    ordered_quantity_snapshot=10,
                    material_received_quantity=10,
                    material_input_quantity=10,
                    output_factor=1,
                    version=1,
                )
                db.add(task)
                db.flush()
                db.add(
                    ProductionCompletion(
                        batch_id=batch.id,
                        task_id=task.id,
                        order_item_id=item.id,
                        expected_version=1,
                        quantity=10,
                        completion_type="primary",
                        material_input_quantity=10,
                        planned_output_quantity=10,
                        actual_output_quantity=10,
                        defective_quantity=0,
                        order_reserved_quantity=10,
                        direct_delivery_quantity=10,
                        stock_quantity=0,
                        surplus_finished_quantity=0,
                        initial_disposition="direct",
                        status="posted",
                        completed_at=datetime.now(),
                    )
                )
        db.commit()

    query_count = 0

    def count_query(_conn, _cursor, _statement, _params, _context, _many) -> None:
        nonlocal query_count
        query_count += 1

    event.listen(engine, "before_cursor_execute", count_query)
    try:
        with factory() as db:
            user = db.query(User).filter(User.username == "admin").one()
            query_count = 0
            summaries = pending_delivery_customer_summaries(db, user=user)
            summary_query_count = query_count
    finally:
        event.remove(engine, "before_cursor_execute", count_query)

    assert summary_query_count <= 22
    assert len(summaries) == 2
    assert sum(row["item_count"] for row in summaries) == 24
    assert sum(row["pending_quantity"] for row in summaries) == 240
