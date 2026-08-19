from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from pathlib import Path

from fastapi import Response
import pytest
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker


def test_dashboard_uses_light_projections_without_full_page_payloads(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import app.api.incoming as incoming_api
    import app.api.production as production_api
    import app.api.requisition as requisition_api
    import app.services.production_workflow as production_workflow
    from app.api.dashboard import dashboard_overview
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "p1-36j-dashboard.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        user = User(
            username="p1-36j-admin",
            password_hash=hash_password("RolePass123!"),
            role="admin",
            real_name="P1-36J 管理员",
            must_change_password=False,
        )
        db.add(user)
        db.commit()
        user_id = int(user.id)

    def full_payload_must_not_run(*_args, **_kwargs):
        raise AssertionError("首页不得构造业务页面完整载荷")

    monkeypatch.setattr(
        requisition_api,
        "pending_requisitions",
        full_payload_must_not_run,
    )
    monkeypatch.setattr(incoming_api, "pending_items", full_payload_must_not_run)
    monkeypatch.setattr(
        production_api,
        "get_production_tasks",
        full_payload_must_not_run,
    )

    calls: list[tuple[str, object]] = []

    def pending_material_rows(*, db, user):
        calls.append(("requisition", int(user.id)))
        return [
            {
                "item_id": 101,
                "order_item_id": 101,
                "is_merge_group": False,
                "customer_id": 11,
                "customer_name": "首页轻摘要客户",
                "order_number": "P1-36J-MATERIAL",
                "product_code": "MAT-001",
                "delivery_date": date(2026, 8, 11),
                "created_at": None,
            }
        ]

    def pending_incoming_rows(*, db, user):
        calls.append(("incoming", int(user.id)))
        return [
            {
                "item_id": "r201",
                "order_item_id": 102,
                "requisition_item_id": 201,
                "stock_replenishment_item_id": None,
                "customer_id": 11,
                "customer_name": "首页轻摘要客户",
                "order_number": "P1-36J-INCOMING",
                "product_code": "INC-001",
                "delivery_date": date(2026, 8, 12),
                "created_at": None,
            }
        ]

    def pending_production_rows(
        db,
        *,
        allowed_customer_ids,
        status="pending",
    ):
        calls.append(("production", allowed_customer_ids))
        assert status == "pending"
        return [
            {
                "id": 301,
                "customer_id": 11,
                "customer_name": "首页轻摘要客户",
                "order_number": "P1-36J-PRODUCTION",
                "product_code": "PROD-001",
                "delivery_date": None,
                "created_at": None,
            }
        ]

    monkeypatch.setattr(
        requisition_api,
        "dashboard_pending_requisition_rows",
        pending_material_rows,
    )
    monkeypatch.setattr(
        incoming_api,
        "dashboard_pending_incoming_rows",
        pending_incoming_rows,
    )
    monkeypatch.setattr(
        production_workflow,
        "list_production_task_dashboard_rows",
        pending_production_rows,
    )

    statements: list[str] = []

    def record_sql(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement.lstrip().lower())

    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        with factory() as db:
            user = db.get(User, user_id)
            assert user is not None
            result = dashboard_overview(db=db, user=user)
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)
        engine.dispose()

    assert calls == [
        ("requisition", user_id),
        ("incoming", user_id),
        ("production", None),
    ]
    cards = {card["key"]: card for card in result["cards"]}
    assert cards["pending_material"]["count"] == 1
    assert cards["pending_incoming"]["count"] == 1
    assert cards["pending_production"]["count"] == 1
    todos = {todo["type"]: todo for todo in result["todos"]}
    assert todos["待报料"]["first_order_no"] == "P1-36J-MATERIAL"
    assert todos["待入库"]["first_item_no"] == "INC-001"
    assert todos["待生产"]["first_order_no"] == "P1-36J-PRODUCTION"
    assert not any(
        statement.startswith(("insert", "update", "delete"))
        for statement in statements
    )


def test_dashboard_source_only_imports_light_pending_projections() -> None:
    source = Path("app/api/dashboard.py").read_text(encoding="utf-8")
    start = source.index("def _authoritative_dashboard_data(")
    end = source.index("\ndef _authoritative_dashboard_cards(", start)
    block = source[start:end]

    assert "dashboard_pending_requisition_rows" in block
    assert "dashboard_pending_incoming_rows" in block
    assert "list_production_task_dashboard_rows" in block
    assert "pending_requisitions(" not in block
    assert "pending_incoming_items" not in block
    assert "get_production_tasks(" not in block


@pytest.mark.parametrize(
    ("role", "expected_calls"),
    [
        ("boss", {"requisition", "incoming", "production"}),
        ("workshop", {"incoming", "production"}),
        ("sales", set()),
    ],
)
def test_dashboard_projection_calls_follow_role_permissions(
    tmp_path: Path,
    monkeypatch,
    role: str,
    expected_calls: set[str],
) -> None:
    import app.api.incoming as incoming_api
    import app.api.requisition as requisition_api
    import app.services.production_workflow as production_workflow
    from app.api.dashboard import dashboard_overview
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / f"p1-36j-{role}.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        user = User(
            username=f"p1-36j-{role}",
            password_hash="not-used",
            role=role,
            real_name=f"P1-36J {role}",
            must_change_password=False,
        )
        db.add(user)
        db.commit()
        user_id = int(user.id)

    calls: set[str] = set()

    def requisition_rows(*, db, user):
        calls.add("requisition")
        return []

    def incoming_rows(*, db, user):
        calls.add("incoming")
        return []

    def production_rows(db, *, allowed_customer_ids, status="pending"):
        calls.add("production")
        return []

    monkeypatch.setattr(
        requisition_api,
        "dashboard_pending_requisition_rows",
        requisition_rows,
    )
    monkeypatch.setattr(
        incoming_api,
        "dashboard_pending_incoming_rows",
        incoming_rows,
    )
    monkeypatch.setattr(
        production_workflow,
        "list_production_task_dashboard_rows",
        production_rows,
    )

    with factory() as db:
        user = db.get(User, user_id)
        assert user is not None
        dashboard_overview(db=db, user=user)

    assert calls == expected_calls


def test_combined_dashboard_matches_legacy_full_sources_with_fewer_queries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import app.api.incoming as incoming_api
    import app.api.production as production_api
    import app.api.requisition as requisition_api
    import app.services.production_workflow as production_workflow
    from app.api.dashboard import dashboard_overview
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.production import ProductionTask
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "p1-36j-combined.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        user = User(
            username="p1-36j-combined-admin",
            password_hash="not-used",
            role="admin",
            real_name="P1-36J 综合回归",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=1,
            customer_code="P1-36J-COMBINED",
            name="P1-36J 综合客户",
            payment_term_days=0,
            credit_limit=Decimal("0"),
        )
        db.add_all([user, customer])
        db.flush()
        product = Product(
            customer_id=customer.id,
            product_code="P1-36J-BOX",
            customer_material_code="P1-36J-BOX",
            product_name="P1-36J 纸箱",
            box_category="normal",
        )
        db.add(product)
        db.flush()

        def add_order_item(
            suffix: str,
            *,
            requisition_status: str,
            material_status: str,
        ) -> OrderItem:
            order = Order(
                order_number=f"P1-36J-{suffix}",
                customer_id=customer.id,
                order_date=date(2026, 8, 10),
                delivery_date=date(2026, 8, 20),
                status="pending_production",
                payment_status="unpaid",
                total_amount=Decimal("0"),
            )
            db.add(order)
            db.flush()
            item = OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=100,
                delivered_quantity=0,
                unit_price=Decimal("0"),
                subtotal=Decimal("0"),
                material_status=material_status,
                requisition_status=requisition_status,
                snapshot_product_name="P1-36J 纸箱",
                snapshot_product_code=f"P1-36J-{suffix}",
                snapshot_material="A=B",
                snapshot_report_length_mm=500,
                snapshot_report_width_mm=300,
                special_process="一开一",
            )
            db.add(item)
            db.flush()
            return item

        add_order_item(
            "MATERIAL",
            requisition_status="未报料",
            material_status="pending",
        )
        add_order_item(
            "INCOMING",
            requisition_status="已报料",
            material_status="pending",
        )
        production_item = add_order_item(
            "PRODUCTION",
            requisition_status="已报料",
            material_status="received",
        )
        db.add(
            ProductionTask(
                order_item_id=production_item.id,
                status="pending",
                planned_quantity=100,
                ordered_quantity_snapshot=100,
                material_received_quantity=100,
                material_input_quantity=100,
                output_factor=1,
                printing_plate_mode_snapshot="plate",
                printing_plate_codes_snapshot="[]",
                version=1,
            )
        )
        db.commit()
        user_id = int(user.id)

    original_requisition = requisition_api.dashboard_pending_requisition_rows
    original_incoming = incoming_api.dashboard_pending_incoming_rows
    original_production = production_workflow.list_production_task_dashboard_rows
    active_user: dict[str, User] = {}

    def legacy_requisition(*, db, user):
        return requisition_api.pending_requisitions(db=db, _user=user)["items"]

    def legacy_incoming(*, db, user):
        return incoming_api.pending_items(response=Response(), db=db, user=user)["items"]

    def legacy_production(db, *, allowed_customer_ids, status="pending"):
        return production_api.get_production_tasks(
            task_status=status,
            db=db,
            user=active_user["value"],
        )["items"]

    def run_overview() -> tuple[dict, list[str]]:
        statements: list[str] = []

        def record_sql(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement.lstrip().lower())

        with factory() as db:
            user = db.get(User, user_id)
            assert user is not None
            active_user["value"] = user
            event.listen(engine, "before_cursor_execute", record_sql)
            try:
                result = dashboard_overview(db=db, user=user)
            finally:
                event.remove(engine, "before_cursor_execute", record_sql)
        return result, statements

    monkeypatch.setattr(
        requisition_api,
        "dashboard_pending_requisition_rows",
        legacy_requisition,
    )
    monkeypatch.setattr(
        incoming_api,
        "dashboard_pending_incoming_rows",
        legacy_incoming,
    )
    monkeypatch.setattr(
        production_workflow,
        "list_production_task_dashboard_rows",
        legacy_production,
    )
    legacy, legacy_sql = run_overview()

    monkeypatch.setattr(
        requisition_api,
        "dashboard_pending_requisition_rows",
        original_requisition,
    )
    monkeypatch.setattr(
        incoming_api,
        "dashboard_pending_incoming_rows",
        original_incoming,
    )
    monkeypatch.setattr(
        production_workflow,
        "list_production_task_dashboard_rows",
        original_production,
    )
    optimized, optimized_sql = run_overview()

    def normalize(value):
        if isinstance(value, dict):
            return {
                key: "NORMALIZED" if key == "as_of" else normalize(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value

    legacy_json = json.dumps(
        normalize(legacy),
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    ).encode("utf-8")
    optimized_json = json.dumps(
        normalize(optimized),
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    ).encode("utf-8")

    assert optimized_json == legacy_json
    assert sum(row.startswith("select") for row in optimized_sql) < sum(
        row.startswith("select") for row in legacy_sql
    )
    assert not any(
        row.startswith(("insert", "update", "delete"))
        for row in [*legacy_sql, *optimized_sql]
    )
