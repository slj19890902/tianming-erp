from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from math import ceil

from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from test_n039_composite_bom_requisition import (
    _component_payload,
    _login as _login_composite,
    _parent_payload,
    composite_requisition_app,
)
from test_p1_06_reported_documents_pagination import (
    _login as _login_p1_06,
    reported_documents_app,
)


__all__ = ["composite_requisition_app", "reported_documents_app"]


@contextmanager
def _app_session(app) -> Iterator[Session]:
    from app.api.deps import get_db

    override = app.dependency_overrides[get_db]
    iterator = override()
    session = next(iterator)
    try:
        yield session
    finally:
        iterator.close()


def _replace_reported_documents(
    app,
    document_count: int,
    *,
    hidden_stock_count: int = 0,
):
    """Replace the P1-06 reported rows with a deterministic mixed-source scale."""
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.requisition import Requisition, RequisitionItem
    from app.models.stock_replenishment import (
        InventoryStockPolicy,
        StockReplenishmentOrder,
        StockReplenishmentOrderItem,
    )
    from app.models.supplier_requisition_order import (
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    with _app_session(app) as db:
        # Remove only isolated test data.  The customer, product, and login users
        # supplied by P1-06 remain the shared permission/scope fixture.
        for model in (
            SupplierRequisitionOrder,
            StockReplenishmentOrder,
            Requisition,
        ):
            for row in db.scalars(select(model)).all():
                db.delete(row)
        db.flush()
        for row in db.scalars(
            select(InventoryStockPolicy).where(
                InventoryStockPolicy.policy_name.like("P1-36O %")
            )
        ).all():
            db.delete(row)
        db.flush()
        for row in db.scalars(select(Order)).all():
            db.delete(row)
        db.flush()

        customer = db.scalar(
            select(Customer).where(Customer.customer_code == "P106-A")
        )
        product = db.scalar(
            select(Product).where(Product.product_code == "P106-A")
        )
        assert customer is not None
        assert product is not None
        hidden_customer = db.scalar(
            select(Customer).where(Customer.customer_code == "P106-B")
        )
        hidden_product = db.scalar(
            select(Product).where(Product.product_code == "P106-B")
        )
        assert hidden_customer is not None
        assert hidden_product is not None

        header_material = db.scalar(
            select(Material).where(
                Material.supplier_name == "P1-36O 供应商",
                Material.code == "P136O-HEADER-MATERIAL",
            )
        )
        if header_material is None:
            header_material = Material(
                supplier_name="P1-36O 供应商",
                code="P136O-HEADER-MATERIAL",
                is_active=True,
                version=1,
            )
            db.add(header_material)
        item_material = db.scalar(
            select(Material).where(
                Material.supplier_name == "P1-36O 供应商",
                Material.code == "P136O-ITEM-MATERIAL",
            )
        )
        if item_material is None:
            item_material = Material(
                supplier_name="P1-36O 供应商",
                code="P136O-ITEM-MATERIAL",
                is_active=True,
                version=1,
            )
            db.add(item_material)
        db.flush()

        visible_policy = InventoryStockPolicy(
            policy_name="P1-36O 可见客户半成品策略",
            target_inventory_type="semi_finished",
            product_id=product.id,
            customer_id=customer.id,
            material_code_snapshot="A+B",
            normalized_material_code="A+B",
            flute_type="B",
            report_length_mm=500,
            report_width_mm=300,
            pieces_per_box=1,
            stock_yield_per_sheet=1,
            warning_quantity=0,
            target_quantity=100,
            active=True,
        )
        hidden_policy = InventoryStockPolicy(
            policy_name="P1-36O 隐藏客户半成品策略",
            target_inventory_type="semi_finished",
            product_id=hidden_product.id,
            customer_id=hidden_customer.id,
            material_code_snapshot="A+B",
            normalized_material_code="A+B",
            flute_type="B",
            report_length_mm=500,
            report_width_mm=300,
            pieces_per_box=1,
            stock_yield_per_sheet=1,
            warning_quantity=0,
            target_quantity=100,
            active=True,
        )
        db.add_all([visible_policy, hidden_policy])
        db.flush()

        created_at = datetime(2026, 8, 10, 8, 0, 0)
        for offset in range(document_count):
            serial = offset + 1
            source_index = offset % 3
            order = None
            order_item = None
            if source_index in {0, 2}:
                order = Order(
                    order_number=f"TM-P136O-{serial:03d}",
                    customer_id=customer.id,
                    order_date=date(2026, 8, 10),
                    status="pending_production",
                    payment_status="unpaid",
                    total_amount=Decimal("10"),
                    created_at=created_at,
                )
                db.add(order)
                db.flush()
                order_item = OrderItem(
                    order_id=order.id,
                    product_id=product.id,
                    item_order_number=f"TM-P136O-{serial:03d}-001",
                    item_sequence=1,
                    quantity=10,
                    unit_price=Decimal("1"),
                    subtotal=Decimal("10"),
                    material_status="pending",
                    requisition_status="已报料",
                    snapshot_product_code=f"P136O-{serial:03d}",
                    snapshot_product_name=f"P1-36O 纸箱 {serial:03d}",
                    snapshot_material="A+B",
                    flute_type="B",
                )
                db.add(order_item)
                db.flush()

            if source_index == 0:
                assert order is not None and order_item is not None
                supplier_order = SupplierRequisitionOrder(
                    order_number=f"SRO-P136O-{serial:03d}",
                    supplier_name="P1-36O 供应商",
                    material_id=header_material.id,
                    requisition_qty=10,
                    status="confirmed",
                    created_at=created_at,
                )
                db.add(supplier_order)
                db.flush()
                db.add(
                    SupplierRequisitionOrderItem(
                        supplier_order_id=supplier_order.id,
                        order_item_id=order_item.id,
                        product_id=product.id,
                        material_id=(
                            item_material.id if serial % 2 == 0 else None
                        ),
                        order_number=order.order_number,
                        product_code=f"P136O-{serial:03d}",
                        product_name=f"P1-36O 纸箱 {serial:03d}",
                        customer_name=customer.name,
                        quantity=10,
                        requisition_qty=10,
                        report_length_mm=500,
                        report_width_mm=300,
                        material_code_snapshot=None,
                        flute_type_snapshot="B",
                    )
                )
            elif source_index == 1:
                stock_order = StockReplenishmentOrder(
                    order_number=f"SR-P136O-{serial:03d}",
                    supplier_name="P1-36O 补库供应商",
                    customer_id=customer.id,
                    source_type="stock_warning",
                    status="confirmed",
                    created_at=created_at,
                )
                db.add(stock_order)
                db.flush()
                db.add(
                    StockReplenishmentOrderItem(
                        replenishment_order_id=stock_order.id,
                        target_inventory_type="semi_finished",
                        stock_policy_id=visible_policy.id,
                        product_id=product.id,
                        customer_id=customer.id,
                        product_code_snapshot=f"P136O-{serial:03d}",
                        product_name_snapshot=f"P1-36O 纸箱 {serial:03d}",
                        pieces_per_box=1,
                        stock_yield_per_sheet=1,
                        quantity=10,
                        stocked_quantity=0,
                        report_length_mm=500,
                        report_width_mm=300,
                        material_code_snapshot="A+B",
                        flute_type="B",
                        created_at=created_at,
                    )
                )
            else:
                assert order_item is not None
                requisition = Requisition(
                    requisition_number=f"REQ-P136O-{serial:03d}",
                    requisition_date=date(2026, 8, 10),
                    supplier_name="P1-36O 旧报料供应商",
                    status="已报料",
                    created_at=created_at,
                )
                db.add(requisition)
                db.flush()
                db.add(
                    RequisitionItem(
                        requisition_id=requisition.id,
                        order_item_id=order_item.id,
                        requisition_qty=10,
                        cardboard_len=Decimal("500"),
                        cardboard_width=Decimal("300"),
                        material_snapshot="A+B",
                        product_code_snapshot=f"P136O-{serial:03d}",
                        product_name_snapshot=f"P1-36O 纸箱 {serial:03d}",
                    )
                )

        for offset in range(hidden_stock_count):
            serial = offset + 1
            hidden_stock_order = StockReplenishmentOrder(
                order_number=f"SR-P136O-HIDDEN-{serial:03d}",
                supplier_name="P1-36O 隐藏补库供应商",
                customer_id=hidden_customer.id,
                source_type="stock_warning",
                status="confirmed",
                created_at=created_at,
            )
            db.add(hidden_stock_order)
            db.flush()
            db.add(
                StockReplenishmentOrderItem(
                    replenishment_order_id=hidden_stock_order.id,
                    target_inventory_type="semi_finished",
                    stock_policy_id=hidden_policy.id,
                    product_id=hidden_product.id,
                    customer_id=hidden_customer.id,
                    product_code_snapshot=f"P136O-HIDDEN-{serial:03d}",
                    product_name_snapshot=f"P1-36O 隐藏纸箱 {serial:03d}",
                    pieces_per_box=1,
                    stock_yield_per_sheet=1,
                    quantity=10,
                    stocked_quantity=0,
                    report_length_mm=500,
                    report_width_mm=300,
                    material_code_snapshot="A+B",
                    flute_type="B",
                    created_at=created_at,
                )
            )
        db.commit()
        return db.get_bind()


def _identity(row: dict) -> tuple[str, int]:
    return row["source_type"], int(row["id"])


def _assert_rich_stock_relationships(
    app,
    *,
    expected_customer_ids: set[int],
) -> None:
    from app.models.stock_replenishment import (
        InventoryStockPolicy,
        StockReplenishmentOrderItem,
    )

    with _app_session(app) as db:
        rows = db.execute(
            select(
                StockReplenishmentOrderItem.customer_id,
                StockReplenishmentOrderItem.product_id,
                StockReplenishmentOrderItem.stock_policy_id,
                InventoryStockPolicy.customer_id,
                InventoryStockPolicy.product_id,
            ).join(
                InventoryStockPolicy,
                InventoryStockPolicy.id
                == StockReplenishmentOrderItem.stock_policy_id,
            )
        ).all()
    assert rows
    assert {int(row[0]) for row in rows} == expected_customer_ids
    assert all(
        item_customer_id == policy_customer_id
        and item_product_id == policy_product_id
        and policy_id is not None
        for (
            item_customer_id,
            item_product_id,
            policy_id,
            policy_customer_id,
            policy_product_id,
        ) in rows
    )


def _get_with_sql(client: TestClient, engine, *, params: dict | None = None):
    statements: list[str] = []

    def record_sql(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lstrip().lower())

    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        response = client.get(
            "/api/requisition/reported-documents",
            params=params,
        )
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)
    return response, statements


def _assert_zero_dml(statements: list[str]) -> None:
    assert not [
        statement
        for statement in statements
        if statement.startswith(("insert", "update", "delete", "replace"))
    ]


def test_old_full_baseline_equals_current_pages_and_decoration_is_bounded(
    reported_documents_app,
    monkeypatch,
) -> None:
    import app.api.requisition as requisition_api

    real_builder = requisition_api._build_reported_documents
    real_decorator = requisition_api._decorate_reported_document_candidates
    decoration_calls: list[
        tuple[list[tuple[str, int]], list[tuple[str, int]]]
    ] = []

    def observed_decorator(candidates: list[dict]):
        result = real_decorator(candidates)
        decoration_calls.append(
            (
                [_identity(candidate) for candidate in candidates],
                [_identity(document) for document in result],
            )
        )
        return result

    monkeypatch.setattr(
        requisition_api,
        "_decorate_reported_document_candidates",
        observed_decorator,
    )
    first_page_selects: dict[int, int] = {}
    first_page_bytes: dict[int, int] = {}
    scoped_first_page_selects: dict[int, int] = {}

    with TestClient(reported_documents_app) as client:
        _login_p1_06(client, "admin", "AdminPass123!")
        for document_count in (0, 1, 20, 21, 100):
            engine = _replace_reported_documents(
                reported_documents_app,
                document_count,
            )
            if document_count == 100:
                from app.models.user import User

                empty_selection_sql: list[str] = []

                def record_empty_selection_sql(
                    _conn,
                    _cursor,
                    statement,
                    _parameters,
                    _context,
                    _many,
                ):
                    empty_selection_sql.append(statement)

                with _app_session(reported_documents_app) as db:
                    admin = db.scalar(
                        select(User).where(User.username == "admin")
                    )
                    assert admin is not None
                    event.listen(
                        engine,
                        "before_cursor_execute",
                        record_empty_selection_sql,
                    )
                    try:
                        assert real_builder(
                            db,
                            admin,
                            selected_identities=set(),
                        ) == []
                    finally:
                        event.remove(
                            engine,
                            "before_cursor_execute",
                            record_empty_selection_sql,
                        )
                assert empty_selection_sql == []

            full, full_sql = _get_with_sql(client, engine)
            assert full.status_code == 200, full.text
            _assert_zero_dml(full_sql)
            full_payload = full.json()
            assert set(full_payload) == {
                "total",
                "matched_line_count",
                "page",
                "page_size",
                "items",
            }
            assert full_payload["total"] == document_count
            assert full_payload["matched_line_count"] == document_count
            assert full_payload["page"] == 1
            assert full_payload["page_size"] == document_count
            assert len(full_payload["items"]) == document_count
            assert "total_pages" not in full_payload
            if document_count == 100:
                assert {
                    line["material_code"]
                    for document in full_payload["items"]
                    if document["source_type"] == "supplier_order"
                    for line in document["line_items"]
                } == {
                    "P136O-HEADER-MATERIAL",
                    "P136O-ITEM-MATERIAL",
                }
                _assert_rich_stock_relationships(
                    reported_documents_app,
                    expected_customer_ids={1},
                )

            decoration_calls.clear()
            page_count = max(1, ceil(document_count / 20))
            page_payloads: list[dict] = []
            for requested_page in range(1, page_count + 1):
                call_start = len(decoration_calls)
                response, statements = _get_with_sql(
                    client,
                    engine,
                    params={"page": requested_page, "page_size": 20},
                )
                assert response.status_code == 200, response.text
                _assert_zero_dml(statements)
                payload = response.json()
                page_payloads.append(payload)
                if requested_page == 1:
                    first_page_selects[document_count] = sum(
                        statement.startswith(("select", "with"))
                        for statement in statements
                    )
                    first_page_bytes[document_count] = len(response.content)
                assert payload["total"] == document_count
                assert payload["matched_line_count"] == document_count
                assert payload["page"] == requested_page
                assert payload["page_size"] == 20
                assert "total_pages" not in payload

                request_calls = decoration_calls[call_start:]
                assert len(request_calls) == 1
                candidates, decorated = request_calls[0]
                expected = [_identity(row) for row in payload["items"]]
                assert candidates == decorated == expected
                assert len(expected) <= 20

            stitched = [
                row
                for payload in page_payloads
                for row in payload["items"]
            ]
            assert stitched == full_payload["items"]

            overflow_page = page_count + 1
            call_start = len(decoration_calls)
            overflow, overflow_sql = _get_with_sql(
                client,
                engine,
                params={"page": overflow_page, "page_size": 20},
            )
            assert overflow.status_code == 200, overflow.text
            _assert_zero_dml(overflow_sql)
            assert overflow.json()["page"] == overflow_page
            assert overflow.json()["items"] == []
            assert decoration_calls[call_start:] == [([], [])]

            if document_count >= 3:
                assert {
                    row["source_type"] for row in full_payload["items"]
                } == {
                    "supplier_order",
                    "stock_replenishment",
                    "legacy_material_requisition",
                }

        _login_p1_06(client, "sales-a", "SalesPass123!")
        for document_count in (20, 100):
            engine = _replace_reported_documents(
                reported_documents_app,
                document_count,
                hidden_stock_count=document_count,
            )
            scoped_full, scoped_full_sql = _get_with_sql(client, engine)
            assert scoped_full.status_code == 200, scoped_full.text
            _assert_zero_dml(scoped_full_sql)
            scoped_full_payload = scoped_full.json()
            decoration_calls.clear()
            scoped, statements = _get_with_sql(
                client,
                engine,
                params={"page": 1, "page_size": 20},
            )
            assert scoped.status_code == 200, scoped.text
            _assert_zero_dml(statements)
            scoped_payload = scoped.json()
            assert scoped_payload["total"] == document_count
            assert scoped_payload["matched_line_count"] == document_count
            assert len(scoped_payload["items"]) == 20
            assert scoped_payload["items"] == scoped_full_payload["items"][:20]
            assert scoped_payload["total"] == scoped_full_payload["total"]
            assert (
                scoped_payload["matched_line_count"]
                == scoped_full_payload["matched_line_count"]
            )
            assert all(
                "P106-B" not in str(row) for row in scoped_payload["items"]
            )
            assert all(
                line["customer_id"] == 1
                for row in scoped_payload["items"]
                for line in row["line_items"]
            )
            assert len(decoration_calls) == 1
            candidates, decorated = decoration_calls[0]
            expected = [_identity(row) for row in scoped_payload["items"]]
            assert candidates == decorated == expected
            assert len(expected) == 20
            scoped_first_page_selects[document_count] = sum(
                statement.startswith(("select", "with"))
                for statement in statements
            )
            if document_count == 100:
                _assert_rich_stock_relationships(
                    reported_documents_app,
                    expected_customer_ids={1, 2},
                )

    # Page-one query count and response size may vary slightly by selected
    # source mix, but adding 80 off-page documents must not grow either with
    # the history size.
    assert first_page_selects[100] <= first_page_selects[20] + 2
    # The SQL listener wraps the whole paged GET, so its count includes the
    # request's authentication/permission SELECTs (but not the earlier login).
    # This rich-relation fixture measures admin=11 and scoped sales=13; the
    # shared budget of 16 keeps explicit headroom without accepting the old 28.
    assert first_page_selects[100] <= 16, first_page_selects
    assert first_page_bytes[100] <= first_page_bytes[20] + 256
    assert scoped_first_page_selects[100] <= (
        scoped_first_page_selects[20] + 2
    )
    assert scoped_first_page_selects[100] <= 16, scoped_first_page_selects


def test_equal_created_at_and_id_keeps_legacy_cross_source_order(
    reported_documents_app,
    monkeypatch,
) -> None:
    import app.api.requisition as requisition_api

    engine = _replace_reported_documents(reported_documents_app, 3)
    with TestClient(reported_documents_app) as client:
        _login_p1_06(client, "admin", "AdminPass123!")
        response, statements = _get_with_sql(client, engine)

        real_decorator = requisition_api._decorate_reported_document_candidates

        def incomplete_decorator(candidates: list[dict]):
            documents = real_decorator(candidates)
            if candidates and documents:
                return documents[1:]
            return documents

        monkeypatch.setattr(
            requisition_api,
            "_decorate_reported_document_candidates",
            incomplete_decorator,
        )
        rejected = client.get(
            "/api/requisition/reported-documents",
            params={"page": 1, "page_size": 2},
        )

    assert response.status_code == 200, response.text
    _assert_zero_dml(statements)
    rows = response.json()["items"]
    assert [row["source_type"] for row in rows] == [
        "supplier_order",
        "stock_replenishment",
        "legacy_material_requisition",
    ]
    assert len({row["id"] for row in rows}) == 1
    assert len({row["created_at"] for row in rows}) == 1
    assert rejected.status_code == 409, rejected.text
    rejected_payload = rejected.json()
    assert "items" not in rejected_payload
    detail = rejected_payload.get("detail")
    assert detail == "已报料列表数据已变化，请刷新重试"


def test_mixed_source_filters_stitch_to_the_full_baseline(
    reported_documents_app,
) -> None:
    cases = [
        ({"source_type": "supplier_order"}, {"SRO-P106-A", "SRO-P106-B"}),
        (
            {"status": "confirmed"},
            {"SRO-P106-A", "SRO-P106-B", "SR-P106-A", "SR-P106-B"},
        ),
        (
            {"date_from": "2026-07-29", "date_to": "2026-07-29"},
            {
                "SRO-P106-A",
                "SRO-P106-B",
                "SR-P106-A",
                "SR-P106-B",
                "REQ-P106-A",
                "REQ-P106-B",
            },
        ),
        ({"document_number": "SRO-P106-A"}, {"SRO-P106-A"}),
        ({"order_number": "TM-P106-A"}, {"SRO-P106-A", "REQ-P106-A"}),
        ({"product_code": "P106-A-ALT"}, {"SR-P106-A"}),
        ({"product_name": "备用"}, {"SR-P106-A"}),
        ({"material_code": "A+B"}, {"SRO-P106-A", "SR-P106-A"}),
        ({"flute_type": "BC"}, {"SRO-P106-B", "SR-P106-B"}),
        (
            {"report_length_mm": 500, "report_width_mm": 300},
            {"SRO-P106-A", "SR-P106-A", "REQ-P106-A"},
        ),
        (
            {
                "report_length_min": 450,
                "report_length_max": 550,
                "report_width_min": 250,
                "report_width_max": 350,
            },
            {"SRO-P106-A", "SR-P106-A", "REQ-P106-A"},
        ),
        ({"supplier_name": "老供应商 A"}, {"REQ-P106-A"}),
        ({"customer_id": 1}, {"SRO-P106-A", "SR-P106-A", "REQ-P106-A"}),
        ({"keyword": "P106 产品 A 备用"}, {"SR-P106-A"}),
        (
            {"product_code": "P106-A-ALT", "report_width_mm": 300},
            set(),
        ),
    ]
    with TestClient(reported_documents_app) as client:
        _login_p1_06(client, "admin", "AdminPass123!")
        for params, expected_numbers in cases:
            full = client.get(
                "/api/requisition/reported-documents",
                params=params,
            )
            assert full.status_code == 200, full.text
            full_payload = full.json()
            page_count = max(1, ceil(full_payload["total"] / 2))
            pages = [
                client.get(
                    "/api/requisition/reported-documents",
                    params={**params, "page": page, "page_size": 2},
                )
                for page in range(1, page_count + 1)
            ]

            assert {
                row["document_number"] for row in full_payload["items"]
            } == expected_numbers
            stitched: list[dict] = []
            for page_number, response in enumerate(pages, start=1):
                assert response.status_code == 200, response.text
                payload = response.json()
                assert payload["page"] == page_number
                assert payload["total"] == full_payload["total"]
                assert (
                    payload["matched_line_count"]
                    == full_payload["matched_line_count"]
                )
                assert "total_pages" not in payload
                stitched.extend(payload["items"])
            assert stitched == full_payload["items"]


def test_match_metadata_covers_off_page_documents_and_all_sibling_lines(
    reported_documents_app,
) -> None:
    with TestClient(reported_documents_app) as client:
        _login_p1_06(client, "admin", "AdminPass123!")
        unfiltered = client.get(
            "/api/requisition/reported-documents",
            params={
                "source_type": "stock_replenishment",
                "document_number": "SR-P106-A",
            },
        )
        material = client.get(
            "/api/requisition/reported-documents",
            params={"material_code": "A+B", "page": 1, "page_size": 1},
        )

    assert unfiltered.status_code == material.status_code == 200
    unfiltered_payload = unfiltered.json()
    assert unfiltered_payload["matched_line_count"] == 2
    assert all(
        line["matched"] is True and line["matched_fields"] == []
        for line in unfiltered_payload["items"][0]["line_items"]
    )

    material_payload = material.json()
    assert material_payload["total"] == 2
    assert material_payload["matched_line_count"] == 2
    document = material_payload["items"][0]
    assert document["document_number"] == "SR-P106-A"
    assert document["matched_line_count"] == 1
    assert len(document["line_items"]) == 2
    assert [line["matched"] for line in document["line_items"]] == [True, False]
    assert [line["matched_fields"] for line in document["line_items"]] == [
        ["material_code"],
        [],
    ]


def test_scope_permission_defaults_bounds_and_empty_page_contract(
    reported_documents_app,
) -> None:
    from app.api.requisition import list_reported_documents
    from app.core.security import hash_password
    from app.models.user import User

    with _app_session(reported_documents_app) as db:
        db.add(
            User(
                username="p136o-no-view",
                password_hash=hash_password("NoViewPass123!"),
                role="workshop",
                real_name="P1-36O no view",
                must_change_password=False,
            )
        )
        db.commit()

    with _app_session(reported_documents_app) as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        assert admin is not None
        direct_unpaged = list_reported_documents(db=db, _user=admin)

    with TestClient(reported_documents_app) as client:
        _login_p1_06(client, "admin", "AdminPass123!")
        unpaged = client.get("/api/requisition/reported-documents")
        page_only = client.get(
            "/api/requisition/reported-documents", params={"page": 1}
        )
        size_only = client.get(
            "/api/requisition/reported-documents", params={"page_size": 2}
        )
        overflow = client.get(
            "/api/requisition/reported-documents", params={"page": 99}
        )
        invalid_responses = [
            client.get("/api/requisition/reported-documents", params={"page": 0}),
            client.get(
                "/api/requisition/reported-documents", params={"page_size": 0}
            ),
            client.get(
                "/api/requisition/reported-documents", params={"page_size": 201}
            ),
            client.get(
                "/api/requisition/reported-documents",
                params={"report_length_mm": 0},
            ),
            client.get(
                "/api/requisition/reported-documents",
                params={"report_length_min": 500, "report_length_max": 499},
            ),
            client.get(
                "/api/requisition/reported-documents",
                params={"source_type": "unknown"},
            ),
        ]

        _login_p1_06(client, "sales-a", "SalesPass123!")
        scoped = client.get(
            "/api/requisition/reported-documents",
            params={"page": 1, "page_size": 1},
        )
        scoped_leak_probe = client.get(
            "/api/requisition/reported-documents",
            params={"product_code": "P106-B", "page": 1, "page_size": 1},
        )
        blocked_customer = client.get(
            "/api/requisition/reported-documents",
            params={"customer_id": 2},
        )

        _login_p1_06(client, "p136o-no-view", "NoViewPass123!")
        forbidden = client.get("/api/requisition/reported-documents")

    assert unpaged.status_code == page_only.status_code == size_only.status_code == 200
    assert direct_unpaged == unpaged.json()
    assert unpaged.json()["page_size"] == 6
    assert page_only.json()["page"] == 1
    assert page_only.json()["page_size"] == 50
    assert size_only.json()["page"] == 1
    assert size_only.json()["page_size"] == 2
    assert overflow.status_code == 200
    assert overflow.json()["page"] == 99
    assert overflow.json()["page_size"] == 50
    assert overflow.json()["items"] == []
    assert all("total_pages" not in response.json() for response in (
        unpaged,
        page_only,
        size_only,
        overflow,
    ))
    assert all(response.status_code == 422 for response in invalid_responses)

    assert scoped.status_code == 200
    assert scoped.json()["total"] == 3
    assert scoped.json()["matched_line_count"] == 4
    assert all("P106-B" not in str(row) for row in scoped.json()["items"])
    assert scoped_leak_probe.status_code == 200
    assert scoped_leak_probe.json()["total"] == 0
    assert scoped_leak_probe.json()["matched_line_count"] == 0
    assert blocked_customer.status_code == 403
    assert forbidden.status_code == 403


def test_composite_bom_current_page_is_deep_equal_and_keeps_all_lines(
    composite_requisition_app,
) -> None:
    app, _session_factory = composite_requisition_app
    with TestClient(app) as client:
        _login_composite(client)
        created = client.post(
            "/api/requisition/batches",
            json={
                "supplier_name": "N039 供应商",
                "items": [
                    _parent_payload(),
                    _component_payload(1),
                    _component_payload(2),
                ],
            },
        )
        assert created.status_code == 201, created.text

        full = client.get(
            "/api/requisition/reported-documents",
            params={"source_type": "composite_bom_requisition"},
        )
        page = client.get(
            "/api/requisition/reported-documents",
            params={
                "source_type": "composite_bom_requisition",
                "page": 1,
                "page_size": 1,
            },
        )
        filtered = client.get(
            "/api/requisition/reported-documents",
            params={
                "source_type": "composite_bom_requisition",
                "product_code": "COMP-A",
                "page": 1,
                "page_size": 1,
            },
        )

    assert full.status_code == page.status_code == filtered.status_code == 200
    assert page.json()["items"] == full.json()["items"]
    document = page.json()["items"][0]
    assert document["source_type"] == "composite_bom_requisition"
    assert document["is_composite_bom"] is True
    assert len(document["line_items"]) == 3
    assert document["can_void"] is True

    filtered_document = filtered.json()["items"][0]
    assert len(filtered_document["line_items"]) == 3
    assert filtered_document["matched_line_count"] == 1
    assert filtered.json()["matched_line_count"] == 1
    assert [
        line["product_code"]
        for line in filtered_document["line_items"]
        if line["matched"]
    ] == ["COMP-A"]


def test_composite_posted_receipt_keeps_reported_print_eligibility_from_source_status(
    composite_requisition_app,
) -> None:
    """A display-only 已收料 overlay must not revoke a valid source line."""

    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.order import OrderItem
    from app.models.requisition import Requisition

    app, session_factory = composite_requisition_app
    with TestClient(app) as client:
        _login_composite(client)
        created = client.post(
            "/api/requisition/batches",
            json={
                "supplier_name": "N039 供应商",
                "items": [
                    _parent_payload(),
                    _component_payload(1),
                    _component_payload(2),
                ],
            },
        )
        assert created.status_code == 201, created.text

    with session_factory() as db:
        requisition = db.get(Requisition, created.json()["id"])
        assert requisition is not None
        received_item = sorted(requisition.items, key=lambda row: int(row.id))[0]
        assert received_item.status == "有效"
        order_item = db.get(OrderItem, received_item.order_item_id)
        assert order_item is not None
        received_quantity = int(received_item.requisition_qty or 0)
        receipt = IncomingReceipt(
            receipt_number="IR-P038-COMPOSITE-POSTED",
            status="posted",
            received_at=datetime(2026, 9, 2, 9, 30),
            received_by=1,
            idempotency_key="p0-38-composite-posted-receipt",
        )
        db.add(receipt)
        db.flush()
        db.add(
            IncomingReceiptItem(
                receipt_id=receipt.id,
                order_id=order_item.order_id,
                order_item_id=order_item.id,
                requisition_id=requisition.id,
                requisition_item_id=received_item.id,
                planned_quantity=received_quantity,
                received_quantity=received_quantity,
                cumulative_received_quantity=received_quantity,
                variance_quantity=0,
                variance_type="matched",
                resolution_status="not_required",
                status="posted",
            )
        )
        db.commit()
        received_item_id = int(received_item.id)

    with TestClient(app) as client:
        _login_composite(client)
        response = client.get(
            "/api/requisition/reported-items",
            params={
                "source_type": "composite_bom_requisition",
                "document_number": created.json()["requisition_number"],
                "page_size": 20,
            },
        )

    assert response.status_code == 200, response.text
    row = next(
        item
        for item in response.json()["items"]
        if item["item_id"] == received_item_id
    )
    assert row["status"] == "已收料"
    assert row["can_print_task"] is True
    assert row["can_print_label"] is True
