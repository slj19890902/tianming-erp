from datetime import date, datetime
from types import SimpleNamespace


class NullObject(SimpleNamespace):
    def __getattr__(self, _name: str):
        return None


def test_beijing_midnight_bounds_map_to_utc_naive_interval():
    from app.core.time_contract import beijing_date_bounds_utc_naive

    start_at, end_at = beijing_date_bounds_utc_naive(date(2026, 7, 17))

    assert start_at == datetime(2026, 7, 16, 16, 0)
    assert end_at == datetime(2026, 7, 17, 16, 0)


def test_warehouse_movement_date_filters_use_beijing_utc_bounds(monkeypatch):
    from app.api import warehouse
    from app.core.time_contract import beijing_date_bounds_utc_naive

    class EmptyRows:
        def offset(self, _value):
            return self

        def limit(self, _value):
            return self

        def all(self):
            return []

    class CaptureSession:
        def scalar(self, statement):
            self.count_statement = statement
            return 0

        def scalars(self, _statement):
            return EmptyRows()

    monkeypatch.setattr(warehouse, "_visible_customer_ids", lambda *_args: None)
    db = CaptureSession()
    start_date = date(2026, 7, 17)
    end_date = date(2026, 7, 18)

    response = warehouse.list_movements(
        date_from=start_date,
        date_to=end_date,
        page=1,
        page_size=50,
        db=db,
        user=object(),
    )

    start_at, _ = beijing_date_bounds_utc_naive(start_date)
    _, end_at = beijing_date_bounds_utc_naive(end_date)
    bound_values = set(db.count_statement.compile().params.values())
    assert response == {"items": [], "total": 0}
    assert start_at in bound_values
    assert end_at in bound_values


def test_warehouse_timestamp_is_explicit_utc_z():
    from app.api.warehouse import _movement_dict

    movement = SimpleNamespace(
        id=1,
        movement_number="MV-1",
        inventory_lot_id=2,
        lot=None,
        movement_type="manual_in",
        quantity=1,
        unit="pcs",
        before_available=0,
        after_available=1,
        before_reserved=0,
        after_reserved=0,
        before_consumed=0,
        after_consumed=0,
        before_damaged=0,
        after_damaged=0,
        before_scrapped=0,
        after_scrapped=0,
        reason=None,
        operator_id=1,
        created_at=datetime(2026, 7, 16, 16, 1, 2),
    )

    assert _movement_dict(movement)["created_at"] == "2026-07-16T16:01:02Z"


def test_remaining_frontend_datetime_sources_are_explicit_utc_z():
    from app.core.time_contract import utc_naive_to_api
    from app.services.master_data_versioning import serialize_revision

    revision = SimpleNamespace(
        id=1,
        object_type="product",
        object_id=2,
        version=3,
        action="update",
        snapshot_schema_version=1,
        snapshot_sha256="hash",
        changed_fields_json="{}",
        restored_from_version=None,
        change_set_id="change",
        operation_log_id=None,
        actor_user_id=1,
        actor_username_snapshot="admin",
        reason="test",
        source="test",
        created_at=datetime(2026, 7, 16, 16, 1, 2),
    )

    assert serialize_revision(revision)["created_at"] == "2026-07-16T16:01:02Z"
    assert utc_naive_to_api(datetime(2026, 7, 16, 16, 1, 2)).endswith("Z")


def test_tianhua_mobile_response_keeps_business_date_date_only(monkeypatch):
    from app.api import tianhua_pre_delivery

    batch = SimpleNamespace(
        id=1,
        customer_name="天华",
        pre_delivery_date=date(2026, 7, 17),
    )
    draft = SimpleNamespace(id=2, draft_number="TH-1", delivery_id=None)
    monkeypatch.setattr(tianhua_pre_delivery, "_token_scope", lambda *_args: (batch, draft))
    monkeypatch.setattr(tianhua_pre_delivery, "_mobile_items", lambda *_args: [])

    response = tianhua_pre_delivery.get_mobile_pick("token", db=None)

    assert response["pre_delivery_date"] == "2026-07-17"


def test_pdf_training_response_models_emit_explicit_utc_timestamps():
    from app.api.pdf_training import BatchOut, CorrectionOut, SampleSummary

    batch = BatchOut(
        id=1,
        batch_name="UAT",
        description=None,
        created_by="admin",
        created_at=datetime(2026, 7, 16, 16, 1, 2),
        sample_count=1,
    )
    sample = SampleSummary(
        id=1,
        batch_id=1,
        customer_id=None,
        file_name="sample.pdf",
        file_sha256="sha",
        parse_status="pending",
        parse_method="text",
        score=None,
        created_at=datetime(2026, 7, 16, 16, 1, 2),
        labeled_at=None,
    )
    correction = CorrectionOut(
        id=1,
        sample_id=1,
        field_path="items[0].quantity",
        parser_value="1",
        corrected_value="2",
        corrected_by="admin",
        corrected_at=datetime(2026, 7, 16, 16, 1, 2),
        note=None,
    )

    assert batch.model_dump(mode="json")["created_at"] == "2026-07-16T16:01:02Z"
    assert sample.model_dump(mode="json")["created_at"] == "2026-07-16T16:01:02Z"
    assert sample.model_dump(mode="json")["labeled_at"] is None
    assert correction.model_dump(mode="json")["corrected_at"] == "2026-07-16T16:01:02Z"


def test_dashboard_sort_date_is_a_beijing_business_date_string():
    from app.api.dashboard import _business_date_string

    assert _business_date_string(date(2026, 7, 17)) == "2026-07-17"
    assert _business_date_string(datetime(2026, 7, 16, 15, 59)) == "2026-07-16"
    assert _business_date_string(datetime(2026, 7, 16, 16, 0)) == "2026-07-17"
    assert _business_date_string(None) is None


def test_requisition_business_datetime_has_explicit_beijing_offset():
    from app.api import requisition

    item = NullObject(
        id=1,
        quantity=10,
        snapshot_pieces_per_box=1,
        special_process=requisition.DEFAULT_CUTTING_MODE,
        supplier_delivery_time=datetime(2026, 7, 17, 8, 30),
    )

    payload = requisition._item_response(item)

    assert payload["supplier_delivery_time"] == "2026-07-17T08:30:00+08:00"
    item.supplier_delivery_time = None
    assert requisition._item_response(item)["supplier_delivery_time"] is None


def test_order_response_business_datetime_has_explicit_beijing_offset(monkeypatch):
    from app.api import orders

    monkeypatch.setattr(orders, "has_permission", lambda *_args: False)
    item = NullObject(
        id=1,
        product_id=None,
        quantity=10,
        delivered_quantity=0,
        unit_price=1,
        subtotal=10,
        supplier_delivery_time=datetime(2026, 7, 17, 8, 30),
    )
    order = NullObject(id=1, items=[item])

    payload = orders._order_response(order, NullObject(role="admin"))

    assert payload["items"][0]["supplier_delivery_time"] == "2026-07-17T08:30:00+08:00"
    item.supplier_delivery_time = None
    assert orders._order_response(order, NullObject(role="admin"))["items"][0]["supplier_delivery_time"] is None


def test_supplier_order_timestamps_keep_distinct_utc_and_beijing_contracts(
    monkeypatch,
):
    from app.api import requisition

    monkeypatch.setattr(requisition, "_source_items_from_supplier_order", lambda _order: [])
    monkeypatch.setattr(
        requisition,
        "_supplier_order_purchase_lines",
        lambda _order, _db: [],
    )
    monkeypatch.setattr(requisition, "_company_sender", lambda _db: "Tianming")
    order = NullObject(
        id=1,
        items=[],
        created_at=datetime(2026, 7, 17, 0, 1, 2),
        voided_at=datetime(2026, 7, 17, 8, 1, 2),
    )
    db = NullObject(get=lambda _model, _key: None)

    payload = requisition._supplier_order_dict(order, db)

    assert payload["created_at"] == "2026-07-17T00:01:02Z"
    assert payload["voided_at"] == "2026-07-17T08:01:02+08:00"
    order.created_at = None
    order.voided_at = None
    nullable_payload = requisition._supplier_order_dict(order, db)
    assert nullable_payload["created_at"] is None
    assert nullable_payload["voided_at"] is None


def test_reported_documents_allows_nullable_supplier_created_at(monkeypatch):
    from app.api import requisition

    class Rows:
        def __init__(self, values):
            self.values = values

        def all(self):
            return self.values

    class QueueSession:
        def __init__(self):
            self.results = iter(
                [
                    [
                        NullObject(
                            id=1,
                            order_number="SRO-1",
                            supplier_name="supplier",
                            status="confirmed",
                            created_at=None,
                            requisition_qty=1,
                            items=[],
                        )
                    ],
                    [],
                    [],
                ]
            )

        def scalars(self, _statement):
            return Rows(next(self.results))

    monkeypatch.setattr(requisition, "build_display_registry", lambda _db: object())

    response = requisition.list_reported_documents(
        db=QueueSession(),
        _user=object(),
    )

    assert response["items"][0]["created_at"] is None


def test_void_supplier_order_writes_beijing_naive_time(monkeypatch):
    from app.api import requisition

    expected = datetime(2026, 7, 17, 8, 1, 2)
    order = NullObject(status="confirmed", items=[])

    class Session:
        def get(self, _model, _key):
            return order

        def commit(self):
            return None

        def refresh(self, _order):
            return None

    monkeypatch.setattr(requisition, "beijing_now_naive", lambda: expected)
    monkeypatch.setattr(
        requisition,
        "_supplier_order_dict",
        lambda current, _db: {"voided_at": current.voided_at},
    )

    response = requisition.void_supplier_order(
        order_id=1,
        db=Session(),
        user=object(),
    )

    assert order.voided_at == expected
    assert response["voided_at"] == expected


def test_order_workflow_rollback_voids_supplier_order_at_beijing_naive_time(
    monkeypatch,
):
    from app.api import orders

    expected = datetime(2026, 7, 17, 8, 1, 2)
    source_item = NullObject(
        id=10,
        order_item_id=20,
        order_number="ORD-1",
        product_code="P-1",
        quantity=1,
        stock_deduction_qty=1,
        requisition_qty=1,
        required_piece_qty=1,
    )
    supplier_order = NullObject(
        id=30,
        order_number="SRO-1",
        status="confirmed",
        voided_at=None,
    )

    class Rows:
        def __init__(self, values):
            self.values = values

        def all(self):
            return self.values

    class Session:
        def execute(self, _statement):
            return Rows([(source_item, supplier_order)])

        def scalars(self, _statement):
            return Rows([source_item])

    monkeypatch.setattr(orders, "beijing_now_naive", lambda: expected)

    changes = orders._rollback_supplier_requisition_items(
        Session(),
        order_item_ids=[source_item.order_item_id],
    )

    assert supplier_order.status == "voided"
    assert supplier_order.voided_at == expected
    assert changes[0]["action"] == "void_supplier_order"
