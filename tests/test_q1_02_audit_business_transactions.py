from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request


def _request(path: str) -> Request:
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "PUT",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 18179),
            "server": ("127.0.0.1", 18179),
        }
    )
    request.state.request_id = "q1-02-transaction-test"
    return request


@pytest.fixture()
def audit_business_db(tmp_path: Path):
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "q1-02-business.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        user = User(
            username="q1-audit-admin",
            password_hash="test-only",
            role="admin",
            real_name="审计测试管理员",
            display_name="审计测试管理员",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=1,
            customer_code="Q1-AUDIT",
            name="匿名审计测试客户",
            payment_term_days=30,
            credit_limit=Decimal("0"),
        )
        db.add_all([user, customer])
        db.flush()
        product = Product(
            customer_id=customer.id,
            product_code="Q1-AUDIT-001",
            customer_material_code="Q1-AUDIT-001",
            product_name="匿名审计测试纸箱",
            box_category="normal",
        )
        db.add(product)
        db.flush()
        order = Order(
            order_number="TM-Q1-AUDIT-001",
            customer_id=customer.id,
            customer_po="PO-BEFORE",
            order_date=date(2026, 7, 29),
            delivery_date=date(2026, 7, 30),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("10"),
            created_by=user.id,
        )
        db.add(order)
        db.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            item_order_number="TM-Q1-AUDIT-001-001",
            item_sequence=1,
            quantity=10,
            delivered_quantity=0,
            unit_price=Decimal("1"),
            subtotal=Decimal("10"),
            material_status="pending",
            requisition_status="已报料",
            snapshot_product_code="Q1-AUDIT-001",
            snapshot_product_name="匿名审计测试纸箱",
        )
        db.add(item)
        db.commit()
        ids = {
            "user": user.id,
            "customer": customer.id,
            "product": product.id,
            "order": order.id,
            "item": item.id,
        }
    return factory, ids


def _audit_failure(*args, **kwargs):
    raise RuntimeError("forced audit failure")


def test_order_update_rolls_back_when_audit_write_fails(
    audit_business_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import orders as orders_api
    from app.api.orders import OrderUpdate
    from app.models.order import Order
    from app.models.user import User

    factory, ids = audit_business_db
    monkeypatch.setattr(orders_api, "append_audit_event", _audit_failure)
    with factory() as db:
        with pytest.raises(RuntimeError, match="forced audit failure"):
            orders_api.update_order(
                order_id=ids["order"],
                payload=OrderUpdate(
                    customer_po="PO-AFTER",
                    delivery_date=date(2026, 8, 1),
                    remark="不应提交",
                ),
                request=_request(f"/api/orders/{ids['order']}"),
                db=db,
                user=db.get(User, ids["user"]),
            )
        db.rollback()
        db.expire_all()
        order = db.get(Order, ids["order"])
        assert order.customer_po == "PO-BEFORE"
        assert order.delivery_date == date(2026, 7, 30)
        assert order.remark is None


def test_order_update_keeps_legacy_action_and_structured_snapshots(
    audit_business_db,
) -> None:
    from app.api import orders as orders_api
    from app.api.orders import OrderUpdate
    from app.models.audit import OperationLog
    from app.models.user import User

    factory, ids = audit_business_db
    with factory() as db:
        result = orders_api.update_order(
            order_id=ids["order"],
            payload=OrderUpdate(
                customer_po="PO-AFTER",
                delivery_date=date(2026, 8, 1),
                remark="订单资料更新",
            ),
            request=_request(f"/api/orders/{ids['order']}"),
            db=db,
            user=db.get(User, ids["user"]),
        )
        assert result["customer_po"] == "PO-AFTER"
        log = db.scalar(
            select(OperationLog).where(
                OperationLog.entity_type == "order",
                OperationLog.entity_id == ids["order"],
                OperationLog.action_code == "order.update",
            )
        )
        assert log is not None
        assert log.action == "UPDATE"
        assert log.event_category == "business"
        assert log.result == "success"
        assert log.actor_user_id_snapshot == ids["user"]
        assert log.customer_id_snapshot == ids["customer"]
        assert log.customer_name_snapshot == "匿名审计测试客户"
        assert log.object_ref == "TM-Q1-AUDIT-001"
        assert log.request_id == "q1-02-transaction-test"
        assert log.schema_version == 1


def test_incoming_receive_rolls_back_when_audit_write_fails(
    audit_business_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.order import OrderItem
    from app.models.user import User
    from app.services import incoming_receipts

    factory, ids = audit_business_db
    monkeypatch.setattr(
        incoming_receipts,
        "append_audit_event",
        _audit_failure,
    )
    with factory() as db:
        with pytest.raises(RuntimeError, match="forced audit failure"):
            incoming_receipts.receive_one(
                db,
                user=db.get(User, ids["user"]),
                item_key=ids["item"],
                received_quantity=10,
                resolution_action=None,
                resolution_reason=None,
                surplus_location_id=None,
                idempotency_key="q1-audit-receive-fail",
                audit_context={
                    "request": _request(
                        f"/api/incoming/receive/{ids['item']}"
                    )
                },
            )
        db.rollback()
        db.expire_all()
        item = db.get(OrderItem, ids["item"])
        assert item.material_status == "pending"
        assert item.requisition_status == "已报料"
        assert db.scalar(select(func.count(IncomingReceipt.id))) == 0
        assert db.scalar(select(func.count(IncomingReceiptItem.id))) == 0


def test_incoming_idempotent_repeat_does_not_duplicate_structured_audit(
    audit_business_db,
) -> None:
    from app.models.audit import OperationLog
    from app.models.user import User
    from app.services import incoming_receipts

    factory, ids = audit_business_db
    with factory() as db:
        kwargs = {
            "user": db.get(User, ids["user"]),
            "item_key": ids["item"],
            "received_quantity": 10,
            "resolution_action": None,
            "resolution_reason": None,
            "surplus_location_id": None,
            "idempotency_key": "q1-audit-receive-once",
            "audit_context": {
                "request": _request(
                    f"/api/incoming/receive/{ids['item']}"
                ),
                "batch_id": "q1-audit-batch",
            },
        }
        first = incoming_receipts.receive_one(db, **kwargs)
        db.commit()
        repeated = incoming_receipts.receive_one(db, **kwargs)
        db.commit()
        assert repeated.id == first.id
        logs = list(
            db.scalars(
                select(OperationLog).where(
                    OperationLog.action_code == "incoming.receive"
                )
            )
        )
        assert len(logs) == 1
        log = logs[0]
        assert log.action == "RECEIVE_MATERIAL"
        assert log.event_category == "business"
        assert log.result == "success"
        assert log.customer_id_snapshot == ids["customer"]
        assert log.object_ref == f"incoming:{first.id}"
        assert log.batch_id == "q1-audit-batch"
        assert log.schema_version == 1


def test_group_delete_has_batch_summary_and_per_item_trace(
    audit_business_db,
) -> None:
    from app.api.orders import OrderGroupDeleteRequest, delete_order_group
    from app.models.audit import OperationLog
    from app.models.order import Order, OrderItem
    from app.models.user import User

    factory, ids = audit_business_db
    with factory() as db:
        second_order = Order(
            order_number="TM-Q1-AUDIT-002",
            customer_id=ids["customer"],
            customer_po="PO-BEFORE",
            order_date=date(2026, 7, 29),
            delivery_date=date(2026, 7, 30),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("5"),
            created_by=ids["user"],
        )
        db.add(second_order)
        db.flush()
        second_item = OrderItem(
            order_id=second_order.id,
            product_id=ids["product"],
            item_order_number="TM-Q1-AUDIT-002-001",
            item_sequence=1,
            quantity=5,
            delivered_quantity=0,
            unit_price=Decimal("1"),
            subtotal=Decimal("5"),
            material_status="pending",
            requisition_status="pending",
            snapshot_product_code="Q1-AUDIT-001",
            snapshot_product_name="匿名审计测试纸箱",
        )
        db.add(second_item)
        db.commit()
        second_order_id = second_order.id
        second_item_id = second_item.id

        result = delete_order_group(
            payload=OrderGroupDeleteRequest(
                order_ids=[ids["order"], second_order_id],
                confirm=True,
            ),
            request=_request("/api/orders/group-delete"),
            db=db,
            user=db.get(User, ids["user"]),
        )

        assert result["deleted_count"] == 2
        assert len(result["batch_id"]) == 32
        assert db.get(Order, ids["order"]) is None
        assert db.get(Order, second_order_id) is None
        assert db.get(OrderItem, ids["item"]) is None
        assert db.get(OrderItem, second_item_id) is None

        logs = db.scalars(
            select(OperationLog)
            .where(OperationLog.batch_id == result["batch_id"])
            .order_by(OperationLog.id)
        ).all()

    assert [row.action_code for row in logs].count("order.item.delete") == 2
    assert [row.action_code for row in logs].count("order.delete") == 2
    assert [row.action_code for row in logs].count("order.group_delete") == 1
    item_refs = {
        json.loads(row.details)["item_order_number"]
        for row in logs
        if row.action_code == "order.item.delete"
    }
    assert item_refs == {
        "TM-Q1-AUDIT-001-001",
        "TM-Q1-AUDIT-002-001",
    }
    summary = next(
        row for row in logs if row.action_code == "order.group_delete"
    )
    assert summary.object_ref == f"{ids['customer']}::PO-BEFORE"
    assert json.loads(summary.details)["deleted_item_count"] == 2


def test_statement_export_is_security_audited_and_audit_failure_blocks(
    audit_business_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import finance as finance_api
    from app.models.audit import OperationLog
    from app.models.finance import Statement
    from app.models.user import User

    factory, ids = audit_business_db
    with factory() as db:
        statement = Statement(
            statement_number="ST-Q1-AUDIT-001",
            customer_id=ids["customer"],
            statement_month="2026-07",
            total_receivable=Decimal("0"),
            total_gross_profit=Decimal("0"),
            invoiced_amount=Decimal("0"),
            settled_amount=Decimal("0"),
            status="unsettled",
            created_by=ids["user"],
        )
        db.add(statement)
        db.commit()
        statement_id = statement.id

        response = finance_api.export_statement_excel(
            statement_id=statement_id,
            request=_request(
                f"/api/finance/statements/{statement_id}/export"
            ),
            db=db,
            user=db.get(User, ids["user"]),
        )
        assert response.media_type.endswith(
            "spreadsheetml.sheet"
        )
        log = db.scalar(
            select(OperationLog).where(
                OperationLog.action_code
                == "finance.statement.export"
            )
        )
        assert log is not None
        assert log.event_category == "security"
        assert log.result == "success"
        assert log.object_ref == "ST-Q1-AUDIT-001"
        details = json.loads(log.details)
        assert details["line_count"] == 0
        assert details["file_size"] > 0
        assert len(details["file_sha256"]) == 64

        before_count = db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action_code
                == "finance.statement.export"
            )
        )
        monkeypatch.setattr(
            finance_api,
            "append_audit_event",
            _audit_failure,
        )
        with pytest.raises(RuntimeError, match="forced audit failure"):
            finance_api.export_statement_excel(
                statement_id=statement_id,
                request=_request(
                    f"/api/finance/statements/{statement_id}/export"
                ),
                db=db,
                user=db.get(User, ids["user"]),
            )
        db.rollback()
        after_count = db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.action_code
                == "finance.statement.export"
            )
        )
        assert after_count == before_count
