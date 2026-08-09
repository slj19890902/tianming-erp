from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.orm import sessionmaker


def _fixture(tmp_path: Path, *, suffix: str, ordinary_count: int = 1):
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.requisition import Requisition, RequisitionHold, RequisitionItem
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / f"p1-36j-{suffix}.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        visible = Customer(
            customer_number=1,
            customer_code="P136J-V",
            name="P1-36J 可见客户",
            payment_term_days=0,
            credit_limit=Decimal("0"),
        )
        hidden = Customer(
            customer_number=2,
            customer_code="P136J-H",
            name="P1-36J 隐藏客户",
            payment_term_days=0,
            credit_limit=Decimal("0"),
        )
        user = User(
            username=f"p1-36j-{suffix}",
            password_hash="not-used",
            role="sales",
            real_name="P1-36J 范围用户",
            must_change_password=False,
            customer_access_mode="selected",
        )
        db.add_all([visible, hidden, user])
        db.flush()
        db.add(UserCustomerScope(user_id=user.id, customer_id=visible.id))
        visible_product = Product(
            customer_id=visible.id,
            product_code="P136J-V",
            customer_material_code="P136J-V",
            product_name="可见纸箱",
            box_category="normal",
            box_style="A1",
        )
        hidden_product = Product(
            customer_id=hidden.id,
            product_code="P136J-H",
            customer_material_code="P136J-H",
            product_name="隐藏纸箱",
            box_category="normal",
            box_style="A1",
        )
        db.add_all([visible_product, hidden_product])
        db.flush()

        def add_item(
            *,
            sequence: int,
            customer: Customer = visible,
            product: Product = visible_product,
            code: str | None = None,
        ) -> OrderItem:
            order = Order(
                order_number=f"P1-36J-{suffix}-{sequence:03d}",
                customer_id=customer.id,
                customer_po=f"PO-{sequence:03d}",
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
                material_status="pending",
                requisition_status="未报料",
                snapshot_product_name=product.product_name,
                snapshot_product_code=code or product.product_code,
                snapshot_material="A=B",
                snapshot_report_length_mm=500,
                snapshot_report_width_mm=300,
                special_process="一开一",
            )
            db.add(item)
            db.flush()
            return item

        ordinary_items = [
            add_item(sequence=index + 1) for index in range(ordinary_count)
        ]
        merge_members = [
            add_item(sequence=ordinary_count + index + 1, code=f"P136J-M{index + 1}")
            for index in range(2)
        ]
        held_item = add_item(sequence=ordinary_count + 3, code="P136J-HOLD")
        hidden_item = add_item(
            sequence=ordinary_count + 4,
            customer=hidden,
            product=hidden_product,
            code="P136J-HIDDEN",
        )

        merge_group = Requisition(
            requisition_number=f"P1-36J-MERGE-{suffix}",
            requisition_date=date(2026, 8, 10),
            supplier_name="P1-36J 供应商",
            status="merged_pending",
            created_by=user.id,
        )
        db.add(merge_group)
        db.flush()
        for member in merge_members:
            db.add(
                RequisitionItem(
                    requisition_id=merge_group.id,
                    order_item_id=member.id,
                    inventory_deducted_qty=0,
                    requisition_qty=100,
                    cardboard_len=Decimal("500"),
                    cardboard_width=Decimal("300"),
                    pieces_per_box=1,
                    required_piece_qty=100,
                    special_process="一开一",
                    material_snapshot="A=B",
                    product_code_snapshot=member.snapshot_product_code,
                    product_name_snapshot=member.snapshot_product_name,
                    specification_snapshot=member.snapshot_spec,
                    status="merged_pending",
                )
            )
        db.add(
            RequisitionHold(
                order_item_id=held_item.id,
                order_item_id_snapshot=held_item.id,
                customer_id_snapshot=visible.id,
                customer_name_snapshot=visible.name,
                order_number_snapshot=held_item.order.order_number,
                order_item_sequence_snapshot=None,
                product_code_snapshot=held_item.snapshot_product_code,
                product_name_snapshot=held_item.snapshot_product_name,
                specification_snapshot=held_item.snapshot_spec,
                quantity_snapshot=held_item.quantity,
                release_mode="expected_date",
                expected_requisition_date=date(2026, 9, 1),
                status="active",
                version=1,
                created_by=user.id,
                updated_by=user.id,
            )
        )
        db.commit()
        return {
            "engine": engine,
            "factory": factory,
            "user_id": user.id,
            "ordinary_ids": [item.id for item in ordinary_items],
            "merge_group_id": merge_group.id,
            "held_id": held_item.id,
            "hidden_id": hidden_item.id,
        }


def _dashboard_data(rows: list[dict]) -> dict:
    from app.services.dashboard_metric_contracts import build_authoritative_snapshot

    snapshot = build_authoritative_snapshot(
        pending_material_rows=rows,
        pending_incoming_rows=[],
        pending_production_rows=[],
        pending_delivery_rows=[],
        pending_receipt_rows=[],
        pending_reconciliation_rows=[],
        statement_rows=[],
        statement_month="2026-08",
        as_of="2026-08-10T12:00:00+08:00",
    )
    return {"snapshot": snapshot, "rows": {"pending_material": rows}}


def _dashboard_row_contract(row: dict) -> dict:
    return {
        "is_merge_group": bool(row.get("is_merge_group")),
        "merge_group_id": row.get("merge_group_id") or row.get("requisition_id"),
        "item_id": row.get("item_id") or row.get("order_item_id"),
        "customer_id": row.get("customer_id"),
        "customer_name": row.get("customer_name"),
        "order_number": row.get("order_number"),
        "product_code": row.get("product_code"),
        "delivery_date": row.get("delivery_date"),
        "created_at": row.get("created_at"),
    }


def test_projection_matches_public_pending_identity_and_todo_fields(
    tmp_path: Path,
) -> None:
    from app.api.dashboard import _authoritative_dashboard_todos
    from app.api.requisition import (
        dashboard_pending_requisition_rows,
        pending_requisitions,
    )
    from app.models.user import User
    from app.services.dashboard_metric_contracts import stable_identities

    fixture = _fixture(tmp_path, suffix="equivalence")
    with fixture["factory"]() as db:
        user = db.get(User, fixture["user_id"])
        assert user is not None
        full_rows = pending_requisitions(db=db, _user=user)["items"]
        projected_rows = dashboard_pending_requisition_rows(db, user)

    assert stable_identities("pending_material", full_rows) == stable_identities(
        "pending_material", projected_rows
    )
    assert [_dashboard_row_contract(row) for row in projected_rows] == [
        _dashboard_row_contract(row) for row in full_rows
    ]
    assert _authoritative_dashboard_todos(
        _dashboard_data(projected_rows)
    ) == _authoritative_dashboard_todos(_dashboard_data(full_rows))


def test_projection_skips_heavy_display_builders_and_preserves_scope_and_holds(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import app.api.requisition as requisition_api
    from app.models.user import User

    fixture = _fixture(tmp_path, suffix="scope")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("dashboard projection must not build full display payloads")

    monkeypatch.setattr(requisition_api, "build_display_registry", forbidden)
    monkeypatch.setattr(requisition_api, "_merge_group_dict", forbidden)
    monkeypatch.setattr(
        requisition_api._PendingRequisitionReadContext,
        "late_finished_inventory_preview",
        forbidden,
    )
    monkeypatch.setattr(
        requisition_api._PendingRequisitionReadContext,
        "customer_board_preparation_summary",
        forbidden,
    )

    with fixture["factory"]() as db:
        user = db.get(User, fixture["user_id"])
        assert user is not None
        rows = requisition_api.dashboard_pending_requisition_rows(db, user)

    normal_ids = {
        int(row["item_id"]) for row in rows if not row["is_merge_group"]
    }
    assert normal_ids == set(fixture["ordinary_ids"])
    assert fixture["held_id"] not in normal_ids
    assert fixture["hidden_id"] not in normal_ids
    assert [
        row["merge_group_id"] for row in rows if row["is_merge_group"]
    ] == [fixture["merge_group_id"]]


def test_projection_query_growth_is_bounded_and_read_only(tmp_path: Path) -> None:
    from app.api.requisition import dashboard_pending_requisition_rows
    from app.models.user import User

    query_counts: dict[int, int] = {}
    for count in (1, 20, 100):
        fixture = _fixture(
            tmp_path,
            suffix=f"scale-{count}",
            ordinary_count=count,
        )
        statements: list[str] = []

        def record_sql(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement.lstrip().lower())

        event.listen(fixture["engine"], "before_cursor_execute", record_sql)
        try:
            with fixture["factory"]() as db:
                user = db.get(User, fixture["user_id"])
                assert user is not None
                rows = dashboard_pending_requisition_rows(db, user)
                assert len(rows) == count + 1
        finally:
            event.remove(
                fixture["engine"],
                "before_cursor_execute",
                record_sql,
            )
            fixture["engine"].dispose()

        verbs = [statement.split(None, 1)[0] for statement in statements]
        assert not {"insert", "update", "delete"}.intersection(verbs)
        query_counts[count] = sum(verb == "select" for verb in verbs)

    assert query_counts[20] <= query_counts[1] + 2
    assert query_counts[100] <= query_counts[1] + 2
