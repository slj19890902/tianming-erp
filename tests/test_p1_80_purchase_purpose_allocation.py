from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.test_phase11_requisition import (
    _add_pending_candidate,
    _login,
    _preview_supplier_order_draft,
    requisition_app,
)
from tests.test_n039_composite_bom_requisition import (
    _component_payload,
    _login as _login_composite,
    _parent_payload,
    composite_requisition_app,
)


PURCHASE_TOTAL = "purchase_total_sheet_qty"
ORDER_PURPOSE = "order_purpose_sheet_qty"
STOCK_PURPOSE = "stock_purpose_sheet_qty"
PURPOSE_VERSION = "purpose_plan_version"
PURPOSE_FINGERPRINT = "purpose_plan_fingerprint"
PURPOSE_STATUS = "purpose_status"


def _prepare_order_item(
    session_factory,
    *,
    item_id: int = 1,
    quantity: int,
    pieces_per_box: int = 1,
    cutting_mode: str = "一开一",
    report_length_mm: int = 800,
    report_width_mm: int = 200,
) -> None:
    from app.models.order import OrderItem

    with session_factory() as session:
        item = session.get(OrderItem, item_id)
        assert item is not None
        item.quantity = quantity
        item.subtotal = Decimal(str(quantity * 3.6))
        item.requisition_status = "未报料"
        item.material_status = "pending"
        item.snapshot_supplier_name = "苏州纸板供应商"
        item.snapshot_report_length_mm = report_length_mm
        item.snapshot_report_width_mm = report_width_mm
        item.snapshot_pieces_per_box = pieces_per_box
        item.snapshot_splice_mode = "single"
        item.special_process = cutting_mode
        session.commit()


def _selection(
    item_id: int = 1,
    *,
    cutting_mode: str = "一开一",
    report_length_mm: int = 800,
    report_width_mm: int = 200,
) -> dict:
    return {
        "type": "order_item",
        "order_item_id": item_id,
        "supplier_name": "苏州纸板供应商",
        "report_length_mm": report_length_mm,
        "report_width_mm": report_width_mm,
        "cutting_mode": cutting_mode,
    }


def _single_line(draft: dict) -> dict:
    groups = draft["supplier_groups"]
    assert len(groups) == 1
    lines = groups[0]["lines"]
    assert len(lines) == 1
    return lines[0]


def _assert_purpose_plan(
    line: dict,
    *,
    purchase_total: int,
    order_purpose: int,
    stock_purpose: int,
) -> None:
    assert line[PURCHASE_TOTAL] == purchase_total
    assert line[ORDER_PURPOSE] == order_purpose
    assert line[STOCK_PURPOSE] == stock_purpose
    assert line[ORDER_PURPOSE] + line[STOCK_PURPOSE] == line[PURCHASE_TOTAL]
    assert line[PURPOSE_VERSION] >= 1
    assert len(line[PURPOSE_FINGERPRINT]) == 64


def _set_purpose_plan(
    line: dict,
    *,
    purchase_total: int,
    order_purpose: int,
    stock_purpose: int,
) -> None:
    line["requisition_qty"] = purchase_total
    line[PURCHASE_TOTAL] = purchase_total
    line[ORDER_PURPOSE] = order_purpose
    line[STOCK_PURPOSE] = stock_purpose
    sources = list(line.get("source_items") or [])
    if len(sources) == 1:
        sources[0][ORDER_PURPOSE] = order_purpose
        sources[0][STOCK_PURPOSE] = stock_purpose


def _save_draft(client: TestClient, draft: dict):
    return client.post(
        "/api/requisition/supplier-orders/from-pending-selection",
        json=draft,
    )


def _created_order_id(response) -> int:
    body = response.json()
    created = body["created_orders"]
    assert len(created) == 1
    return int(created[0]["supplier_order_id"])


def _formal_line(client: TestClient, order_id: int) -> tuple[dict, dict]:
    response = client.get(f"/api/requisition/supplier-orders/{order_id}")
    assert response.status_code == 200, response.text
    order = response.json()
    lines = order["lines"]
    assert len(lines) == 1
    return order, lines[0]


def _business_fact_counts(session_factory) -> dict[str, int]:
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.production import ProductionCompletion, ProductionTask
    from app.models.warehouse_inventory import InventoryLot, InventoryMovement

    models = {
        "receipt": IncomingReceipt,
        "receipt_item": IncomingReceiptItem,
        "task": ProductionTask,
        "completion": ProductionCompletion,
        "lot": InventoryLot,
        "movement": InventoryMovement,
    }
    with session_factory() as session:
        return {
            name: int(session.scalar(select(func.count(model.id))) or 0)
            for name, model in models.items()
        }


def _formal_write_counts(session_factory) -> dict[str, int]:
    from app.models.audit import OperationLog
    from app.models.supplier_requisition_order import (
        PurchasePurposeSourceSnapshot,
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    models = {
        "order": SupplierRequisitionOrder,
        "item": SupplierRequisitionOrderItem,
        "purpose": PurchasePurposeSourceSnapshot,
        "audit": OperationLog,
    }
    with session_factory() as session:
        return {
            name: int(session.scalar(select(func.count(model.id))) or 0)
            for name, model in models.items()
        }


def _legacy_batch_write_counts(session_factory) -> dict[str, int]:
    from app.models.audit import OperationLog
    from app.models.requisition import Requisition, RequisitionItem
    from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot

    models = {
        "batch": Requisition,
        "item": RequisitionItem,
        "purpose": PurchasePurposeSourceSnapshot,
        "audit": OperationLog,
    }
    with session_factory() as session:
        return {
            name: int(session.scalar(select(func.count(model.id))) or 0)
            for name, model in models.items()
        }


@pytest.mark.parametrize(
    (
        "effective_pieces",
        "yield_per_sheet",
        "purchase_total",
        "expected_order",
        "expected_reserve",
    ),
    [
        (500, 1, 500, 500, 0),
        (500, 1, 600, 500, 100),
        (500, 2, 300, 250, 50),
        (20, 3, 7, 7, 0),
        (20, 3, 8, 7, 1),
    ],
)
def test_pure_allocation_gold_samples_keep_finished_pieces_separate_from_sheets(
    effective_pieces: int,
    yield_per_sheet: int,
    purchase_total: int,
    expected_order: int,
    expected_reserve: int,
) -> None:
    from app.services.purchase_purpose_allocation import (
        PurchasePurposeSourceDemand,
        allocate_purchase_purpose,
    )

    allocation = allocate_purchase_purpose(
        purchase_sheet_qty=purchase_total,
        yield_per_sheet=yield_per_sheet,
        source_demands=[
            PurchasePurposeSourceDemand(
                source_key="order_item:1:whole",
                customer_id=1,
                effective_required_piece_qty=effective_pieces,
            )
        ],
    )
    assert allocation.effective_required_piece_qty == effective_pieces
    assert allocation.purchase_sheet_qty == purchase_total
    assert allocation.authoritative_order_sheet_qty == expected_order
    assert allocation.order_purpose_sheet_qty == expected_order
    assert allocation.reserve_purpose_sheet_qty == expected_reserve
    assert sum(row.purchase_sheet_qty for row in allocation.source_allocations) == (
        purchase_total
    )


def test_pure_allocation_merges_two_tens_before_single_ceiling() -> None:
    from app.services.purchase_purpose_allocation import (
        PurchasePurposeSourceDemand,
        allocate_purchase_purpose,
    )

    allocation = allocate_purchase_purpose(
        purchase_sheet_qty=8,
        yield_per_sheet=3,
        source_demands=[
            PurchasePurposeSourceDemand("order_item:1:whole", 1, 10),
            PurchasePurposeSourceDemand("order_item:2:whole", 1, 10),
        ],
    )
    assert allocation.effective_required_piece_qty == 20
    assert allocation.authoritative_order_sheet_qty == 7
    assert allocation.order_purpose_sheet_qty == 7
    assert allocation.reserve_purpose_sheet_qty == 1
    assert sum(
        row.order_purpose_sheet_qty for row in allocation.source_allocations
    ) == 7
    assert sum(
        row.reserve_purpose_sheet_qty for row in allocation.source_allocations
    ) == 1
    assert {row.source_key for row in allocation.source_allocations} == {
        "order_item:1:whole",
        "order_item:2:whole",
    }


def test_pure_idempotency_guard_binds_hash_and_actor() -> None:
    from app.services.purchase_purpose_allocation import (
        PurchasePurposeIdempotencyConflict,
        assert_purchase_purpose_replay,
        canonical_purchase_purpose_hash,
    )

    request_hash = canonical_purchase_purpose_hash(
        {"purchase": 600, "order": 500, "reserve": 100}
    )
    assert_purchase_purpose_replay(
        stored_request_hash=request_hash,
        stored_actor_id=7,
        submitted_request_hash=request_hash,
        submitted_actor_id=7,
    )
    with pytest.raises(PurchasePurposeIdempotencyConflict):
        assert_purchase_purpose_replay(
            stored_request_hash=request_hash,
            stored_actor_id=7,
            submitted_request_hash=canonical_purchase_purpose_hash(
                {"purchase": 601, "order": 500, "reserve": 101}
            ),
            submitted_actor_id=7,
        )
    with pytest.raises(PurchasePurposeIdempotencyConflict):
        assert_purchase_purpose_replay(
            stored_request_hash=request_hash,
            stored_actor_id=7,
            submitted_request_hash=request_hash,
            submitted_actor_id=8,
        )


def test_preview_and_finalize_500_of_600_freezes_500_order_and_100_stock(
    requisition_app,
) -> None:
    from app.models.order import OrderItem

    app, session_factory = requisition_app
    _prepare_order_item(session_factory, quantity=500)
    before_facts = _business_fact_counts(session_factory)

    with TestClient(app) as client:
        _login(client, "sales")
        draft = _preview_supplier_order_draft(client, [_selection()])
        line = _single_line(draft)
        _assert_purpose_plan(
            line,
            purchase_total=500,
            order_purpose=500,
            stock_purpose=0,
        )
        _set_purpose_plan(
            line,
            purchase_total=600,
            order_purpose=500,
            stock_purpose=100,
        )
        saved = _save_draft(client, draft)
        assert saved.status_code == 201, saved.text
        order, formal = _formal_line(client, _created_order_id(saved))

    assert order["requisition_qty"] == 600
    assert formal["requisition_qty"] == 600
    _assert_purpose_plan(
        formal,
        purchase_total=600,
        order_purpose=500,
        stock_purpose=100,
    )
    assert formal[PURPOSE_STATUS] == "frozen"
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        assert item is not None
        assert item.quantity == 500
        assert item.delivered_quantity == 0
    assert _business_fact_counts(session_factory) == before_facts


def test_one_sheet_outputs_two_finished_units_freezes_250_of_300(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _prepare_order_item(
        session_factory,
        quantity=500,
        pieces_per_box=1,
        cutting_mode="一开二",
    )
    with TestClient(app) as client:
        _login(client, "sales")
        draft = _preview_supplier_order_draft(
            client,
            [_selection(cutting_mode="一开二", report_width_mm=400)],
        )
        line = _single_line(draft)
        assert line["effective_demand_piece_qty"] == 500
        assert line["theoretical_requisition_qty"] == 250
        _assert_purpose_plan(
            line,
            purchase_total=250,
            order_purpose=250,
            stock_purpose=0,
        )
        _set_purpose_plan(
            line,
            purchase_total=300,
            order_purpose=250,
            stock_purpose=50,
        )
        saved = _save_draft(client, draft)
        assert saved.status_code == 201, saved.text
        _order, formal = _formal_line(client, _created_order_id(saved))
    _assert_purpose_plan(
        formal,
        purchase_total=300,
        order_purpose=250,
        stock_purpose=50,
    )


def test_merged_two_tens_rounds_once_then_freezes_seven_of_eight(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    second_id = _add_pending_candidate(
        session_factory,
        80,
        product_code="P180-MERGE-2",
        product_name="P1-80 合并来源二",
        quantity=10,
    )
    for item_id in (1, second_id):
        _prepare_order_item(session_factory, item_id=item_id, quantity=10)

    with TestClient(app) as client:
        _login(client, "sales")
        created = client.post(
            "/api/requisition/merge-groups",
            json={
                "member_item_ids": [1, second_id],
                "supplier_name": "苏州纸板供应商",
                "report_length_mm": 800,
                "report_width_mm": 200,
                "cutting_mode": "一开一",
                "remark": "P1-80 两来源合并",
            },
        )
        assert created.status_code == 201, created.text
        row = created.json()
        factor = 3
        updated = client.put(
            f"/api/requisition/merge-groups/{row['id']}",
            json={
                "supplier_name": row["supplier_name"],
                "report_length_mm": row["original_report_length_mm"],
                "report_width_mm": int(row["original_report_width_mm"]) * factor,
                "cutting_mode": "一开三",
                "remark": "合并后一次取整",
                "expected_cutting_plan_fingerprint": row[
                    "cutting_plan_fingerprint"
                ],
                "calculated_report_length_mm": row[
                    "original_report_length_mm"
                ],
                "calculated_report_width_mm": int(
                    row["original_report_width_mm"]
                )
                * factor,
                "calculated_requisition_qty": 7,
                "calculated_effective_demand_piece_qty": 20,
            },
        )
        assert updated.status_code == 200, updated.text
        row = updated.json()
        preview = client.post(
            "/api/requisition/supplier-orders/preview-from-pending-selection",
            json={
                "selections": [
                    {
                        "type": "merge_group",
                        "merge_group_id": row["id"],
                        "supplier_name": row["supplier_name"],
                        "report_length_mm": row["report_length_mm"],
                        "report_width_mm": row["report_width_mm"],
                        "cutting_mode": row["cutting_mode"],
                    }
                ]
            },
        )
        assert preview.status_code == 200, preview.text
        draft = preview.json()
        line = _single_line(draft)

        assert line["effective_demand_piece_qty"] == 20
        assert line["theoretical_requisition_qty"] == 7
        assert len(line["source_items"]) == 2
        _assert_purpose_plan(
            line,
            purchase_total=7,
            order_purpose=7,
            stock_purpose=0,
        )
        _set_purpose_plan(
            line,
            purchase_total=8,
            order_purpose=7,
            stock_purpose=1,
        )
        saved = _save_draft(client, draft)
        assert saved.status_code == 201, saved.text
        _order, formal = _formal_line(client, _created_order_id(saved))

    _assert_purpose_plan(
        formal,
        purchase_total=8,
        order_purpose=7,
        stock_purpose=1,
    )


def test_partial_purchase_then_overbuy_uses_only_live_remaining_order_demand(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _prepare_order_item(session_factory, quantity=500)
    selection = _selection()

    with TestClient(app) as client:
        _login(client, "sales")
        first = _preview_supplier_order_draft(client, [selection])
        first_line = _single_line(first)
        _set_purpose_plan(
            first_line,
            purchase_total=300,
            order_purpose=300,
            stock_purpose=0,
        )
        first_saved = _save_draft(client, first)
        assert first_saved.status_code == 201, first_saved.text

        second = _preview_supplier_order_draft(client, [selection])
        second_line = _single_line(second)
        assert second_line["already_requisitioned_qty"] == 300
        assert second_line["remaining_requisition_qty"] == 200
        _assert_purpose_plan(
            second_line,
            purchase_total=200,
            order_purpose=200,
            stock_purpose=0,
        )
        _set_purpose_plan(
            second_line,
            purchase_total=250,
            order_purpose=200,
            stock_purpose=50,
        )
        second_saved = _save_draft(client, second)
        assert second_saved.status_code == 201, second_saved.text
        first_id = _created_order_id(first_saved)
        second_id = _created_order_id(second_saved)
        _first_order, first_formal = _formal_line(client, first_id)
        _second_order, second_formal = _formal_line(client, second_id)

    _assert_purpose_plan(
        first_formal,
        purchase_total=300,
        order_purpose=300,
        stock_purpose=0,
    )
    _assert_purpose_plan(
        second_formal,
        purchase_total=250,
        order_purpose=200,
        stock_purpose=50,
    )


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        (PURCHASE_TOTAL, 601, "PURCHASE_PURPOSE_TAMPERED"),
        (ORDER_PURPOSE, 499, "PURCHASE_PURPOSE_SUM_MISMATCH"),
        (STOCK_PURPOSE, 99, "PURCHASE_PURPOSE_SUM_MISMATCH"),
        (PURPOSE_FINGERPRINT, "f" * 64, "PURCHASE_PURPOSE_STALE"),
    ],
)
def test_tamper_or_stale_rejects_without_any_formal_mutation(
    requisition_app,
    field: str,
    value,
    expected_code: str,
) -> None:
    from app.models.order import OrderItem

    app, session_factory = requisition_app
    _prepare_order_item(session_factory, quantity=500)
    with TestClient(app) as client:
        _login(client, "sales")
        draft = _preview_supplier_order_draft(client, [_selection()])
        line = _single_line(draft)
        _set_purpose_plan(
            line,
            purchase_total=600,
            order_purpose=500,
            stock_purpose=100,
        )
        line[field] = value
        before_submit = _formal_write_counts(session_factory)
        response = _save_draft(client, draft)

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["code"] == expected_code
    assert _formal_write_counts(session_factory) == before_submit
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        assert item is not None
        assert item.requisition_status == "未报料"


def test_live_demand_change_invalidates_preview_and_writes_nothing(
    requisition_app,
) -> None:
    from app.models.order import OrderItem

    app, session_factory = requisition_app
    _prepare_order_item(session_factory, quantity=500)
    with TestClient(app) as client:
        _login(client, "sales")
        stale_draft = _preview_supplier_order_draft(client, [_selection()])
        with session_factory() as session:
            item = session.get(OrderItem, 1)
            assert item is not None
            item.quantity = 501
            item.subtotal = Decimal("1803.60")
            session.commit()
        before_submit = _formal_write_counts(session_factory)
        response = _save_draft(client, stale_draft)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "PURCHASE_PURPOSE_STALE"
    assert _formal_write_counts(session_factory) == before_submit


def test_idempotency_replay_binds_payload_hash_and_actor(requisition_app) -> None:
    app, session_factory = requisition_app
    _prepare_order_item(session_factory, quantity=500)
    with TestClient(app) as client:
        _login(client, "sales")
        draft = _preview_supplier_order_draft(client, [_selection()])
        line = _single_line(draft)
        _set_purpose_plan(
            line,
            purchase_total=600,
            order_purpose=500,
            stock_purpose=100,
        )
        original = deepcopy(draft)
        first = _save_draft(client, original)
        assert first.status_code == 201, first.text
        first_id = _created_order_id(first)
        after_first = _formal_write_counts(session_factory)

        replay = _save_draft(client, deepcopy(original))
        assert replay.status_code == 201, replay.text
        assert replay.json()["idempotent_replay"] is True
        assert _created_order_id(replay) == first_id
        assert _formal_write_counts(session_factory) == after_first

        changed = deepcopy(original)
        _single_line(changed)["remark"] = "同键异载荷"
        hash_conflict = _save_draft(client, changed)
        assert hash_conflict.status_code == 409, hash_conflict.text
        assert hash_conflict.json()["detail"]["code"] == (
            "PURCHASE_PURPOSE_IDEMPOTENCY_CONFLICT"
        )

        _login(client, "admin")
        before_actor_conflict = _formal_write_counts(session_factory)
        actor_conflict = _save_draft(client, deepcopy(original))
        assert actor_conflict.status_code == 409, actor_conflict.text
        assert actor_conflict.json()["detail"]["code"] == (
            "PURCHASE_PURPOSE_ACTOR_MISMATCH"
        )
    assert _formal_write_counts(session_factory) == before_actor_conflict


@pytest.mark.parametrize("failure_stage", ["audit", "commit"])
def test_audit_or_commit_failure_rolls_back_purchase_and_purpose_together(
    requisition_app,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    import app.api.requisition as requisition_api

    app, session_factory = requisition_app
    _prepare_order_item(session_factory, quantity=500)
    with TestClient(app, raise_server_exceptions=False) as client:
        _login(client, "sales")
        draft = _preview_supplier_order_draft(client, [_selection()])
        line = _single_line(draft)
        _set_purpose_plan(
            line,
            purchase_total=600,
            order_purpose=500,
            stock_purpose=100,
        )
        before_submit = _formal_write_counts(session_factory)

        def fail(*_args, **_kwargs):
            raise RuntimeError(f"p1-80 forced {failure_stage} failure")

        if failure_stage == "audit":
            monkeypatch.setattr(requisition_api, "append_audit_event", fail)
        else:
            monkeypatch.setattr(session_factory.class_, "commit", fail)
        response = _save_draft(client, draft)
    monkeypatch.undo()

    assert response.status_code == 500
    assert _formal_write_counts(session_factory) == before_submit
    from app.models.order import OrderItem

    with session_factory() as session:
        item = session.get(OrderItem, 1)
        assert item is not None
        assert item.requisition_status == "未报料"


def test_view_permission_sees_purpose_without_execute_permission(
    requisition_app,
) -> None:
    from app.models.access_control import UserPermissionOverride
    from app.models.user import User

    app, session_factory = requisition_app
    _prepare_order_item(session_factory, quantity=500)
    with session_factory() as session:
        finance = session.scalar(select(User).where(User.username == "finance"))
        assert finance is not None
        session.add_all(
            [
                UserPermissionOverride(
                    user_id=finance.id,
                    permission_code="requisition.view",
                    is_allowed=True,
                ),
                UserPermissionOverride(
                    user_id=finance.id,
                    permission_code="requisition.execute",
                    is_allowed=False,
                ),
            ]
        )
        session.commit()

    with TestClient(app) as client:
        _login(client, "finance")
        draft = _preview_supplier_order_draft(client, [_selection()])
        line = _single_line(draft)
        _assert_purpose_plan(
            line,
            purchase_total=500,
            order_purpose=500,
            stock_purpose=0,
        )
        assert "unit_price" not in line
        blocked = _save_draft(client, draft)
    assert blocked.status_code == 403, blocked.text
    assert _formal_write_counts(session_factory)["order"] == 0


def test_customer_scope_blocks_preview_and_finalize_before_master_validation(
    requisition_app,
) -> None:
    from datetime import date

    from app.models.access_control import UserCustomerScope
    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.user import User

    app, session_factory = requisition_app
    _prepare_order_item(session_factory, quantity=500)
    with session_factory() as session:
        sales = session.scalar(select(User).where(User.username == "sales"))
        assert sales is not None
        sales.customer_access_mode = "selected"
        session.add(UserCustomerScope(user_id=sales.id, customer_id=1))
        other_customer = Customer(
            customer_number=180,
            customer_code="P180-SCOPE-B",
            name="匿名客户乙",
        )
        session.add(other_customer)
        session.flush()
        product = Product(
            customer_id=other_customer.id,
            product_code="P180-SCOPE-P",
            customer_material_code="P180-SCOPE-CM",
            product_name="匿名客户乙纸箱",
            length_mm=Decimal("100"),
            width_mm=Decimal("100"),
            height_mm=Decimal("100"),
            box_category="normal",
        )
        order = Order(
            order_number="P180-SCOPE-O",
            customer_id=other_customer.id,
            order_date=date(2026, 8, 20),
            delivery_date=date(2026, 8, 21),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("10"),
        )
        session.add_all([product, order])
        session.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=10,
            unit_price=Decimal("1"),
            subtotal=Decimal("10"),
            material_status="pending",
            requisition_status="未报料",
            snapshot_product_code=product.product_code,
            snapshot_product_name=product.product_name,
            snapshot_supplier_name="苏州纸板供应商",
            snapshot_report_length_mm=800,
            snapshot_report_width_mm=200,
            snapshot_pieces_per_box=1,
        )
        session.add(item)
        session.commit()
        other_item_id = item.id

    with TestClient(app) as client:
        _login(client, "admin")
        admin_draft = _preview_supplier_order_draft(
            client,
            [_selection(other_item_id)],
        )
        _login(client, "sales")
        blocked_preview = client.post(
            "/api/requisition/supplier-orders/preview-from-pending-selection",
            json={
                "selections": [
                    {
                        **_selection(other_item_id),
                        "supplier_name": "未建档供应商也不应先泄漏",
                    }
                ]
            },
        )
        blocked_finalize = _save_draft(client, admin_draft)
    assert blocked_preview.status_code == 403, blocked_preview.text
    assert blocked_finalize.status_code == 403, blocked_finalize.text
    assert "供应商" not in str(blocked_preview.json().get("detail", ""))
    assert _formal_write_counts(session_factory)["order"] == 0


def test_old_client_is_compatible_only_without_overbuy(requisition_app) -> None:
    app, session_factory = requisition_app
    _prepare_order_item(session_factory, quantity=500)
    with TestClient(app) as client:
        _login(client, "sales")
        exact = _preview_supplier_order_draft(client, [_selection()])
        exact_line = _single_line(exact)
        for field in (
            PURCHASE_TOTAL,
            ORDER_PURPOSE,
            STOCK_PURPOSE,
            PURPOSE_VERSION,
            PURPOSE_FINGERPRINT,
            PURPOSE_STATUS,
        ):
            exact_line.pop(field, None)
        for source in exact_line["source_items"]:
            source.pop(ORDER_PURPOSE, None)
            source.pop(STOCK_PURPOSE, None)
        exact_saved = _save_draft(client, exact)
        assert exact_saved.status_code == 201, exact_saved.text
        second_id = _add_pending_candidate(
            session_factory,
            81,
            product_code="P180-OLD-CLIENT",
            product_name="P1-80 旧客户端超量",
            quantity=500,
        )
        _prepare_order_item(
            session_factory,
            item_id=second_id,
            quantity=500,
        )
        overbuy = _preview_supplier_order_draft(
            client,
            [_selection(second_id)],
        )
        line = _single_line(overbuy)
        line["requisition_qty"] = 600
        for field in (
            PURCHASE_TOTAL,
            ORDER_PURPOSE,
            STOCK_PURPOSE,
            PURPOSE_VERSION,
            PURPOSE_FINGERPRINT,
            PURPOSE_STATUS,
        ):
            line.pop(field, None)
        for source in line["source_items"]:
            source.pop(ORDER_PURPOSE, None)
            source.pop(STOCK_PURPOSE, None)
        rejected = _save_draft(client, overbuy)
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"]["code"] == (
        "PURCHASE_PURPOSE_REQUIRED_FOR_OVERBUY"
    )


def test_backend_draft_contract_exposes_versioned_purpose_fields() -> None:
    from app.api.requisition import (
        PendingSupplierOrderDraftLine,
        PendingSupplierOrderDraftSourceItem,
        RequisitionLinePayload,
        SupplierOrderMemberPayload,
    )

    line_fields = set(PendingSupplierOrderDraftLine.model_fields)
    source_fields = set(PendingSupplierOrderDraftSourceItem.model_fields)
    direct_fields = set(SupplierOrderMemberPayload.model_fields)
    batch_fields = set(RequisitionLinePayload.model_fields)
    required = {
        PURCHASE_TOTAL,
        ORDER_PURPOSE,
        STOCK_PURPOSE,
        PURPOSE_VERSION,
        PURPOSE_FINGERPRINT,
    }
    assert required <= line_fields
    assert {ORDER_PURPOSE, STOCK_PURPOSE} <= source_fields
    assert required <= direct_fields
    assert required <= batch_fields


def test_merge_group_direct_adapter_freezes_authoritative_seven_sheets(
    requisition_app,
) -> None:
    from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot

    app, session_factory = requisition_app
    second_id = _add_pending_candidate(
        session_factory,
        82,
        product_code="P180-MERGE-DIRECT",
        product_name="P1-80 合并直连来源二",
        quantity=10,
    )
    for item_id in (1, second_id):
        _prepare_order_item(session_factory, item_id=item_id, quantity=10)

    with TestClient(app) as client:
        _login(client, "sales")
        created = client.post(
            "/api/requisition/merge-groups",
            json={
                "member_item_ids": [1, second_id],
                "supplier_name": "苏州纸板供应商",
                "report_length_mm": 800,
                "report_width_mm": 200,
                "cutting_mode": "一开一",
                "remark": "P1-80 合并直连接口",
            },
        )
        assert created.status_code == 201, created.text
        row = created.json()
        updated = client.put(
            f"/api/requisition/merge-groups/{row['id']}",
            json={
                "supplier_name": row["supplier_name"],
                "report_length_mm": row["original_report_length_mm"],
                "report_width_mm": int(row["original_report_width_mm"]) * 3,
                "cutting_mode": "一开三",
                "remark": "合并后一次取整",
                "expected_cutting_plan_fingerprint": row[
                    "cutting_plan_fingerprint"
                ],
                "calculated_report_length_mm": row[
                    "original_report_length_mm"
                ],
                "calculated_report_width_mm": int(
                    row["original_report_width_mm"]
                )
                * 3,
                "calculated_requisition_qty": 7,
                "calculated_effective_demand_piece_qty": 20,
            },
        )
        assert updated.status_code == 200, updated.text
        finalized = client.post(
            f"/api/requisition/merge-groups/{row['id']}/supplier-order"
        )
        assert finalized.status_code == 201, finalized.text
        formal = finalized.json()["supplier_order"]

    assert formal["purpose_status"] == "frozen"
    assert formal[PURCHASE_TOTAL] == 7
    assert formal[ORDER_PURPOSE] == 7
    assert formal[STOCK_PURPOSE] == 0
    assert formal["requisition_qty"] == 7
    with session_factory() as session:
        snapshots = list(
            session.scalars(
                select(PurchasePurposeSourceSnapshot).order_by(
                    PurchasePurposeSourceSnapshot.source_key
                )
            )
        )
        assert len(snapshots) == 2
        assert sum(row.purchase_sheet_qty for row in snapshots) == 7
        assert sum(row.order_purpose_sheet_qty for row in snapshots) == 7
        assert sum(row.reserve_purpose_sheet_qty for row in snapshots) == 0
        assert {row.source_order_item_id for row in snapshots} == {1, second_id}


def test_legacy_direct_supplier_endpoint_uses_same_purpose_freeze(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _prepare_order_item(session_factory, quantity=500)
    payload = {
        "request_key": "p180-direct-00000001",
        "supplier_name": "苏州纸板供应商",
        "report_length_mm": 800,
        "report_width_mm": 200,
        "cutting_mode": "一开一",
        "members": [
            {
                "item_id": 1,
                "quantity": 500,
                "requisition_qty": 600,
                "purchase_total_sheet_qty": 600,
                "order_purpose_sheet_qty": 500,
                "stock_purpose_sheet_qty": 100,
                "cutting_mode": "一开一",
            }
        ],
    }
    with TestClient(app) as client:
        _login(client, "sales")
        response = client.post("/api/requisition/supplier-orders", json=payload)
        assert response.status_code == 201, response.text
        formal = response.json()

    assert formal[PURPOSE_STATUS] == "frozen"
    assert formal[PURCHASE_TOTAL] == 600
    assert formal[ORDER_PURPOSE] == 500
    assert formal[STOCK_PURPOSE] == 100
    counts = _formal_write_counts(session_factory)
    assert counts["order"] == 1
    assert counts["item"] == 1
    assert counts["purpose"] == 1


def test_unlinked_legacy_direct_supplier_payload_fails_closed(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    with TestClient(app) as client:
        _login(client, "admin")
        response = client.post(
            "/api/requisition/supplier-orders",
            json={
                "request_key": "p180-unlinked-000001",
                "supplier_name": "苏州纸板供应商",
                "members": [
                    {
                        "quantity": 10,
                        "requisition_qty": 10,
                        "product_code": "P180-NO-SOURCE",
                        "product_name": "匿名无来源采购",
                    }
                ],
            },
        )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == (
        "PURCHASE_PURPOSE_SOURCE_REQUIRED"
    )
    assert _formal_write_counts(session_factory)["order"] == 0


def test_legacy_batch_endpoint_freezes_exact_order_purpose_without_guessing(
    requisition_app,
) -> None:
    from app.models.requisition import Requisition
    from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot

    app, session_factory = requisition_app
    _prepare_order_item(session_factory, quantity=500)
    with TestClient(app) as client:
        _login(client, "sales")
        response = client.post(
            "/api/requisition/batches",
            json={
                "request_key": "p180-batch-00000001",
                "supplier_name": "苏州纸板供应商",
                "items": [
                    {
                        "order_item_id": 1,
                        "inventory_deducted_qty": 0,
                        "requisition_qty": 500,
                        "cardboard_len": 800,
                        "cardboard_width": 200,
                        "special_process": "一开一",
                        "remark": "P1-80 旧批次入口",
                    }
                ],
            },
        )
    assert response.status_code == 201, response.text
    with session_factory() as session:
        batch = session.scalar(select(Requisition))
        snapshot = session.scalar(select(PurchasePurposeSourceSnapshot))
        assert batch is not None and snapshot is not None
        assert batch.request_key == "p180-batch-00000001"
        assert len(batch.request_hash or "") == 64
        assert batch.request_actor_id is not None
        assert snapshot.material_requisition_item_id is not None
        assert snapshot.supplier_requisition_order_item_id is None
        assert snapshot.purchase_sheet_qty == 500
        assert snapshot.order_purpose_sheet_qty == 500
        assert snapshot.reserve_purpose_sheet_qty == 0


def test_composite_cover_and_base_keep_distinct_frozen_physical_sources(
    requisition_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot

    app, session_factory = requisition_app
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        assert item is not None
        product = session.get(Product, item.product_id)
        assert product is not None
        product.box_style = "A3 天地盖"
        item.quantity = 20
        item.subtotal = Decimal("72")
        item.requisition_status = "未报料"
        item.material_status = "pending"
        item.snapshot_supplier_name = "苏州纸板供应商"
        item.snapshot_product_name = "P1-80 组合天地盖"
        item.snapshot_report_length_mm = 800
        item.snapshot_report_width_mm = 200
        item.snapshot_base_report_length_mm = 780
        item.snapshot_base_report_width_mm = 190
        item.snapshot_splice_mode = "single"
        item.snapshot_pieces_per_box = 1
        session.commit()

    with TestClient(app) as client:
        _login(client, "sales")
        draft = _preview_supplier_order_draft(client, [_selection()])
        lines = draft["supplier_groups"][0]["lines"]
        assert len(lines) == 2
        by_component = {
            line["source_items"][0]["component_type"]: line for line in lines
        }
        assert set(by_component) == {"cover", "base"}
        _set_purpose_plan(
            by_component["cover"],
            purchase_total=25,
            order_purpose=20,
            stock_purpose=5,
        )
        saved = _save_draft(client, draft)
        assert saved.status_code == 201, saved.text
        order_id = _created_order_id(saved)
        formal_response = client.get(
            f"/api/requisition/supplier-orders/{order_id}"
        )
        assert formal_response.status_code == 200, formal_response.text
        formal = formal_response.json()

    assert len(formal["lines"]) == 2
    assert sum(line[PURCHASE_TOTAL] for line in formal["lines"]) == 45
    assert sum(line[ORDER_PURPOSE] for line in formal["lines"]) == 40
    assert sum(line[STOCK_PURPOSE] for line in formal["lines"]) == 5
    with session_factory() as session:
        snapshots = list(
            session.scalars(
                select(PurchasePurposeSourceSnapshot).order_by(
                    PurchasePurposeSourceSnapshot.component_type
                )
            )
        )
        assert len(snapshots) == 2
        assert {row.component_type for row in snapshots} == {"cover", "base"}
        assert len({row.source_key for row in snapshots}) == 2
        assert sum(row.reserve_purpose_sheet_qty for row in snapshots) == 5


def test_historical_supplier_order_is_explicit_legacy_unset(requisition_app) -> None:
    from app.models.supplier_requisition_order import (
        PurchasePurposeSourceSnapshot,
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    app, session_factory = requisition_app
    with session_factory() as session:
        historical = SupplierRequisitionOrder(
            order_number="SRO-P180-LEGACY",
            supplier_name="匿名历史供应商",
            total_quantity=500,
            stock_deduction_qty=0,
            requisition_qty=600,
            status="confirmed",
            created_by=1,
        )
        session.add(historical)
        session.flush()
        session.add(
            SupplierRequisitionOrderItem(
                supplier_order_id=historical.id,
                order_item_id=1,
                source_key="order_item:1:whole",
                quantity=500,
                stock_deduction_qty=0,
                requisition_qty=600,
                customer_name="苏州思迈尔包装有限公司",
            )
        )
        session.commit()
        historical_id = historical.id

    with TestClient(app) as client:
        _login(client, "admin")
        response = client.get(
            f"/api/requisition/supplier-orders/{historical_id}"
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body[PURPOSE_STATUS] == "legacy_unset"
    assert body[PURCHASE_TOTAL] is None
    assert body[ORDER_PURPOSE] is None
    assert body[STOCK_PURPOSE] is None
    assert body["lines"][0][PURPOSE_STATUS] == "legacy_unset"
    assert body["lines"][0][PURCHASE_TOTAL] is None
    with session_factory() as session:
        assert (
            session.scalar(select(func.count(PurchasePurposeSourceSnapshot.id)))
            == 0
        )


def test_partial_merge_sources_subtract_only_frozen_order_purpose_on_next_draft(
    requisition_app,
) -> None:
    from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot

    app, session_factory = requisition_app
    second_id = _add_pending_candidate(
        session_factory,
        85,
        product_code="P180-PARTIAL-SOURCE-2",
        product_name="P1-80 部分来源二",
        quantity=10,
    )
    for item_id in (1, second_id):
        _prepare_order_item(session_factory, item_id=item_id, quantity=10)

    with TestClient(app) as client:
        _login(client, "sales")
        created = client.post(
            "/api/requisition/merge-groups",
            json={
                "member_item_ids": [1, second_id],
                "supplier_name": "苏州纸板供应商",
                "report_length_mm": 800,
                "report_width_mm": 200,
                "cutting_mode": "一开一",
                "remark": "P1-80 部分采购用途",
            },
        )
        assert created.status_code == 201, created.text
        group = created.json()
        selection = {
            "type": "merge_group",
            "merge_group_id": group["id"],
            "supplier_name": group["supplier_name"],
            "report_length_mm": group["report_length_mm"],
            "report_width_mm": group["report_width_mm"],
            "cutting_mode": group["cutting_mode"],
        }
        first_draft = _preview_supplier_order_draft(client, [selection])
        first_line = _single_line(first_draft)
        _assert_purpose_plan(
            first_line,
            purchase_total=20,
            order_purpose=20,
            stock_purpose=0,
        )
        _set_purpose_plan(
            first_line,
            purchase_total=12,
            order_purpose=10,
            stock_purpose=2,
        )
        first_saved = _save_draft(client, first_draft)
        assert first_saved.status_code == 201, first_saved.text

        second_draft = _preview_supplier_order_draft(client, [selection])
        second_line = _single_line(second_draft)

    assert second_line["theoretical_requisition_qty"] == 10
    assert second_line["already_requisitioned_qty"] == 10
    assert second_line["remaining_requisition_qty"] == 10
    _assert_purpose_plan(
        second_line,
        purchase_total=10,
        order_purpose=10,
        stock_purpose=0,
    )
    assert sum(
        int(source["already_requisitioned_qty"])
        for source in second_line["source_items"]
    ) == 10
    assert sum(
        int(source["remaining_requisition_qty"])
        for source in second_line["source_items"]
    ) == 10
    with session_factory() as session:
        snapshots = list(
            session.scalars(
                select(PurchasePurposeSourceSnapshot).order_by(
                    PurchasePurposeSourceSnapshot.source_requisition_item_id
                )
            )
        )
        assert len(snapshots) == 2
        assert {row.source_kind for row in snapshots} == {"requisition_item"}
        assert sum(row.purchase_sheet_qty for row in snapshots) == 12
        assert sum(row.order_purpose_sheet_qty for row in snapshots) == 10
        assert sum(row.reserve_purpose_sheet_qty for row in snapshots) == 2


@pytest.mark.parametrize(
    ("missing_field", "request_key"),
    [
        (PURPOSE_VERSION, "p180-missing-version"),
        (PURPOSE_FINGERPRINT, "p180-missing-fingerprint"),
    ],
)
def test_legacy_batch_explicit_purpose_requires_version_and_fingerprint_atomically(
    requisition_app,
    missing_field: str,
    request_key: str,
) -> None:
    from app.models.order import OrderItem

    app, session_factory = requisition_app
    _prepare_order_item(session_factory, quantity=500)
    line = {
        "order_item_id": 1,
        "inventory_deducted_qty": 0,
        "requisition_qty": 500,
        PURCHASE_TOTAL: 500,
        ORDER_PURPOSE: 500,
        STOCK_PURPOSE: 0,
        PURPOSE_VERSION: 1,
        PURPOSE_FINGERPRINT: "a" * 64,
        "cardboard_len": 800,
        "cardboard_width": 200,
        "special_process": "一开一",
    }
    line.pop(missing_field)
    with TestClient(app) as client:
        _login(client, "sales")
        before_submit = _legacy_batch_write_counts(session_factory)
        response = client.post(
            "/api/requisition/batches",
            json={
                "request_key": request_key,
                "supplier_name": "苏州纸板供应商",
                "items": [line],
            },
        )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "PURCHASE_PURPOSE_TAMPERED"
    assert _legacy_batch_write_counts(session_factory) == before_submit
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        assert item is not None
        assert item.requisition_status == "未报料"
        assert item.requisition_qty is None
        assert item.cardboard_len is None
        assert item.cardboard_width is None


def test_frozen_legacy_batch_rejects_old_item_edit_without_any_mutation(
    requisition_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.requisition import RequisitionItem
    from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot

    app, session_factory = requisition_app
    _prepare_order_item(session_factory, quantity=500)
    with TestClient(app) as client:
        _login(client, "sales")
        created = client.post(
            "/api/requisition/batches",
            json={
                "request_key": "p180-frozen-edit-guard",
                "supplier_name": "苏州纸板供应商",
                "items": [
                    {
                        "order_item_id": 1,
                        "inventory_deducted_qty": 0,
                        "requisition_qty": 500,
                        "cardboard_len": 800,
                        "cardboard_width": 200,
                        "special_process": "一开一",
                    }
                ],
            },
        )
        assert created.status_code == 201, created.text
        before_edit_counts = _legacy_batch_write_counts(session_factory)
        with session_factory() as session:
            order_item = session.get(OrderItem, 1)
            requisition_item = session.scalar(select(RequisitionItem))
            snapshot = session.scalar(select(PurchasePurposeSourceSnapshot))
            assert order_item is not None
            assert requisition_item is not None
            assert snapshot is not None
            before_order_item = (
                order_item.requisition_qty,
                order_item.cardboard_len,
                order_item.cardboard_width,
                order_item.special_process,
                order_item.requisition_remark,
            )
            before_requisition_item = (
                requisition_item.requisition_qty,
                requisition_item.cardboard_len,
                requisition_item.cardboard_width,
                requisition_item.special_process,
                requisition_item.remark,
            )
            before_snapshot = (
                snapshot.purchase_sheet_qty,
                snapshot.order_purpose_sheet_qty,
                snapshot.reserve_purpose_sheet_qty,
                snapshot.preview_fingerprint,
            )

        rejected = client.put(
            "/api/requisition/items/1",
            json={
                "inventory_deducted_qty": 0,
                "requisition_qty": 499,
                "cardboard_len": 999,
                "cardboard_width": 333,
                "special_process": "一开二",
                "remark": "不得覆盖冻结事实",
            },
        )

    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"]["code"] == "PURCHASE_PURPOSE_FROZEN"
    assert _legacy_batch_write_counts(session_factory) == before_edit_counts
    with session_factory() as session:
        order_item = session.get(OrderItem, 1)
        requisition_item = session.scalar(select(RequisitionItem))
        snapshot = session.scalar(select(PurchasePurposeSourceSnapshot))
        assert order_item is not None
        assert requisition_item is not None
        assert snapshot is not None
        assert (
            order_item.requisition_qty,
            order_item.cardboard_len,
            order_item.cardboard_width,
            order_item.special_process,
            order_item.requisition_remark,
        ) == before_order_item
        assert (
            requisition_item.requisition_qty,
            requisition_item.cardboard_len,
            requisition_item.cardboard_width,
            requisition_item.special_process,
            requisition_item.remark,
        ) == before_requisition_item
        assert (
            snapshot.purchase_sheet_qty,
            snapshot.order_purpose_sheet_qty,
            snapshot.reserve_purpose_sheet_qty,
            snapshot.preview_fingerprint,
        ) == before_snapshot


def test_weighted_multi_source_formal_snapshots_are_each_locally_balanced(
    requisition_app,
) -> None:
    from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot

    app, session_factory = requisition_app
    second_id = _add_pending_candidate(
        session_factory,
        86,
        product_code="P180-WEIGHT-2",
        product_name="P1-80 权重来源二",
        quantity=1,
    )
    third_id = _add_pending_candidate(
        session_factory,
        87,
        product_code="P180-WEIGHT-3",
        product_name="P1-80 权重来源三",
        quantity=4,
    )
    for item_id, quantity in ((1, 1), (second_id, 1), (third_id, 4)):
        _prepare_order_item(session_factory, item_id=item_id, quantity=quantity)

    with TestClient(app) as client:
        _login(client, "sales")
        created = client.post(
            "/api/requisition/merge-groups",
            json={
                "member_item_ids": [1, second_id, third_id],
                "supplier_name": "苏州纸板供应商",
                "report_length_mm": 800,
                "report_width_mm": 200,
                "cutting_mode": "一开一",
                "remark": "P1-80 1:1:4 权重分配",
            },
        )
        assert created.status_code == 201, created.text
        group = created.json()
        draft = _preview_supplier_order_draft(
            client,
            [
                {
                    "type": "merge_group",
                    "merge_group_id": group["id"],
                    "supplier_name": group["supplier_name"],
                    "report_length_mm": group["report_length_mm"],
                    "report_width_mm": group["report_width_mm"],
                    "cutting_mode": group["cutting_mode"],
                }
            ],
        )
        line = _single_line(draft)
        _assert_purpose_plan(
            line,
            purchase_total=6,
            order_purpose=6,
            stock_purpose=0,
        )
        _set_purpose_plan(
            line,
            purchase_total=10,
            order_purpose=6,
            stock_purpose=4,
        )
        saved = _save_draft(client, draft)
        assert saved.status_code == 201, saved.text
        order, formal = _formal_line(client, _created_order_id(saved))

    assert order["requisition_qty"] == 10
    _assert_purpose_plan(
        formal,
        purchase_total=10,
        order_purpose=6,
        stock_purpose=4,
    )
    with session_factory() as session:
        snapshots = list(
            session.scalars(
                select(PurchasePurposeSourceSnapshot).order_by(
                    PurchasePurposeSourceSnapshot.source_order_item_id
                )
            )
        )
        assert len(snapshots) == 3
        assert all(
            row.purchase_sheet_qty
            == row.order_purpose_sheet_qty + row.reserve_purpose_sheet_qty
            for row in snapshots
        )
        by_source = {
            row.source_order_item_id: (
                row.purchase_sheet_qty,
                row.order_purpose_sheet_qty,
                row.reserve_purpose_sheet_qty,
            )
            for row in snapshots
        }
        assert by_source == {
            1: (2, 1, 1),
            second_id: (2, 1, 1),
            third_id: (6, 4, 2),
        }


@pytest.mark.parametrize("missing_mode", ["one", "all"])
def test_pending_formal_requires_every_supplier_group_request_key_atomically(
    requisition_app,
    missing_mode: str,
) -> None:
    app, session_factory = requisition_app
    second_id = _add_pending_candidate(
        session_factory,
        88,
        product_code="P180-REQUEST-KEY-B",
        product_name="P1-80 第二供应商请求键",
        quantity=10,
        supplier_name="昆山鸣明",
    )
    _prepare_order_item(session_factory, quantity=10)
    _prepare_order_item(session_factory, item_id=second_id, quantity=10)

    with TestClient(app) as client:
        _login(client, "sales")
        draft = _preview_supplier_order_draft(
            client,
            [
                _selection(),
                {
                    **_selection(second_id),
                    "supplier_name": "昆山鸣明",
                    "report_length_mm": 900,
                    "report_width_mm": 300,
                },
            ],
        )
        assert len(draft["supplier_groups"]) == 2
        if missing_mode == "one":
            draft["supplier_groups"][0]["request_key"] = None
        else:
            for group in draft["supplier_groups"]:
                group["request_key"] = None
        before_submit = _formal_write_counts(session_factory)
        rejected = _save_draft(client, draft)

    assert rejected.status_code == 400, rejected.text
    assert "请求编号不完整" in rejected.json()["detail"]
    assert _formal_write_counts(session_factory) == before_submit


def test_non_merge_same_spec_sources_round_once_and_keep_zero_sheet_identity(
    requisition_app,
) -> None:
    from app.models.supplier_requisition_order import (
        PurchasePurposeSourceSnapshot,
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    app, session_factory = requisition_app
    second_id = _add_pending_candidate(
        session_factory,
        89,
        product_code="P180-ROUND-ONCE-2",
        product_name="P1-80 单次取整来源二",
        quantity=1,
    )
    third_id = _add_pending_candidate(
        session_factory,
        90,
        product_code="P180-ROUND-ONCE-3",
        product_name="P1-80 单次取整来源三",
        quantity=1,
    )
    for item_id in (1, second_id, third_id):
        _prepare_order_item(
            session_factory,
            item_id=item_id,
            quantity=1,
            cutting_mode="一开三",
        )

    selections = [
        _selection(
            item_id,
            cutting_mode="一开三",
            report_width_mm=600,
        )
        for item_id in (1, second_id, third_id)
    ]
    with TestClient(app) as client:
        _login(client, "sales")
        draft = _preview_supplier_order_draft(client, selections)
        line = _single_line(draft)
        assert line["source_type"] == "normal"
        assert len(line["source_items"]) == 3
        assert line["effective_demand_piece_qty"] == 3
        _assert_purpose_plan(
            line,
            purchase_total=1,
            order_purpose=1,
            stock_purpose=0,
        )
        saved = _save_draft(client, draft)

    assert saved.status_code == 201, saved.text
    assert saved.json()["created_orders"][0]["item_count"] == 3
    with session_factory() as session:
        order = session.scalar(select(SupplierRequisitionOrder))
        supplier_items = list(
            session.scalars(
                select(SupplierRequisitionOrderItem).order_by(
                    SupplierRequisitionOrderItem.id
                )
            )
        )
        snapshots = list(
            session.scalars(
                select(PurchasePurposeSourceSnapshot).order_by(
                    PurchasePurposeSourceSnapshot.source_order_item_id
                )
            )
        )
        assert order is not None
        assert order.requisition_qty == 1
        assert len(supplier_items) == 3
        assert len(snapshots) == 3
        assert {row.source_order_item_id for row in snapshots} == {
            1,
            second_id,
            third_id,
        }
        assert sorted(row.purchase_sheet_qty for row in snapshots) == [0, 0, 1]
        assert all(
            row.purchase_sheet_qty
            == row.order_purpose_sheet_qty + row.reserve_purpose_sheet_qty
            for row in snapshots
        )


def test_stock_only_source_can_be_purchased_again_under_a_new_formal_group(
    requisition_app,
) -> None:
    from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot

    app, session_factory = requisition_app
    second_id = _add_pending_candidate(
        session_factory,
        91,
        product_code="P180-STOCK-ONLY-2",
        product_name="P1-80 备库来源二",
        quantity=1,
    )
    third_id = _add_pending_candidate(
        session_factory,
        92,
        product_code="P180-STOCK-ONLY-3",
        product_name="P1-80 备库来源三",
        quantity=4,
    )
    for item_id, quantity in ((1, 1), (second_id, 1), (third_id, 4)):
        _prepare_order_item(session_factory, item_id=item_id, quantity=quantity)

    with TestClient(app) as client:
        _login(client, "sales")
        first_draft = _preview_supplier_order_draft(
            client,
            [_selection(item_id) for item_id in (1, second_id, third_id)],
        )
        first_line = _single_line(first_draft)
        _assert_purpose_plan(
            first_line,
            purchase_total=6,
            order_purpose=6,
            stock_purpose=0,
        )
        _set_purpose_plan(
            first_line,
            purchase_total=3,
            order_purpose=1,
            stock_purpose=2,
        )
        first_saved = _save_draft(client, first_draft)
        assert first_saved.status_code == 201, first_saved.text

        with session_factory() as session:
            first_source_snapshot = session.scalar(
                select(PurchasePurposeSourceSnapshot).where(
                    PurchasePurposeSourceSnapshot.source_order_item_id == 1
                )
            )
            assert first_source_snapshot is not None
            assert (
                first_source_snapshot.purchase_sheet_qty,
                first_source_snapshot.order_purpose_sheet_qty,
                first_source_snapshot.reserve_purpose_sheet_qty,
            ) == (1, 0, 1)
            first_group_key = first_source_snapshot.allocation_group_key

        second_draft = _preview_supplier_order_draft(client, [_selection(1)])
        second_line = _single_line(second_draft)
        _assert_purpose_plan(
            second_line,
            purchase_total=1,
            order_purpose=1,
            stock_purpose=0,
        )
        second_saved = _save_draft(client, second_draft)

    assert second_saved.status_code == 201, second_saved.text
    with session_factory() as session:
        snapshots = list(
            session.scalars(
                select(PurchasePurposeSourceSnapshot)
                .where(PurchasePurposeSourceSnapshot.source_order_item_id == 1)
                .order_by(PurchasePurposeSourceSnapshot.id)
            )
        )
        assert len(snapshots) == 2
        assert [row.order_purpose_sheet_qty for row in snapshots] == [0, 1]
        assert snapshots[0].allocation_group_key == first_group_key
        assert snapshots[1].allocation_group_key != first_group_key


def test_legacy_batch_replay_conflicts_and_ordinary_partial_purpose_are_atomic(
    requisition_app,
) -> None:
    from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot
    from app.services.purchase_purpose_allocation import (
        canonical_purchase_purpose_hash,
    )

    app, session_factory = requisition_app
    _prepare_order_item(session_factory, quantity=500)
    payload = {
        "request_key": "p180-batch-replay-0001",
        "supplier_name": "苏州纸板供应商",
        "items": [
            {
                "order_item_id": 1,
                "inventory_deducted_qty": 0,
                "requisition_qty": 500,
                "cardboard_len": 800,
                "cardboard_width": 200,
                "special_process": "一开一",
                "remark": "P1-80 旧入口幂等重放",
            }
        ],
    }
    with TestClient(app) as client:
        _login(client, "sales")
        created = client.post("/api/requisition/batches", json=payload)
        assert created.status_code == 201, created.text
        created_items = created.json()["items"]
        assert created_items
        after_created = _legacy_batch_write_counts(session_factory)

        replay = client.post("/api/requisition/batches", json=payload)
        assert replay.status_code == 201, replay.text
        assert replay.json()["idempotent_replay"] is True
        assert replay.json()["id"] == created.json()["id"]
        assert replay.json()["requisition_number"] == created.json()[
            "requisition_number"
        ]
        assert replay.json()["items"]
        assert [row["item_id"] for row in replay.json()["items"]] == [
            row["item_id"] for row in created_items
        ]
        assert _legacy_batch_write_counts(session_factory) == after_created

        changed = deepcopy(payload)
        changed["items"][0]["remark"] = "同键异载荷必须冲突"
        payload_conflict = client.post("/api/requisition/batches", json=changed)
        assert payload_conflict.status_code == 409, payload_conflict.text
        assert payload_conflict.json()["detail"]["code"] == (
            "PURCHASE_PURPOSE_IDEMPOTENCY_CONFLICT"
        )
        assert _legacy_batch_write_counts(session_factory) == after_created

        client.post("/api/auth/logout")
        _login(client, "admin")
        after_actor_login = _legacy_batch_write_counts(session_factory)
        actor_conflict = client.post("/api/requisition/batches", json=payload)
        assert actor_conflict.status_code == 409, actor_conflict.text
        assert actor_conflict.json()["detail"]["code"] == (
            "PURCHASE_PURPOSE_ACTOR_MISMATCH"
        )
        assert _legacy_batch_write_counts(session_factory) == after_actor_login

        partial_item_id = _add_pending_candidate(
            session_factory,
            93,
            product_code="P180-LEGACY-PARTIAL",
            product_name="P1-80 旧入口禁止分批",
            quantity=500,
        )
        _prepare_order_item(
            session_factory,
            item_id=partial_item_id,
            quantity=500,
        )
        source_key = f"order_item:{partial_item_id}"
        fingerprint = canonical_purchase_purpose_hash(
            {
                "version": 1,
                "source_key": source_key,
                "customer_id": 1,
                "effective_piece_qty": 500,
                "yield_per_sheet": 1,
                "authoritative_order_sheet_qty": 500,
            }
        )
        before_partial = _legacy_batch_write_counts(session_factory)
        partial = client.post(
            "/api/requisition/batches",
            json={
                "request_key": "p180-batch-partial-01",
                "supplier_name": "苏州纸板供应商",
                "items": [
                    {
                        "order_item_id": partial_item_id,
                        "inventory_deducted_qty": 0,
                        "requisition_qty": 500,
                        PURCHASE_TOTAL: 500,
                        ORDER_PURPOSE: 400,
                        STOCK_PURPOSE: 100,
                        PURPOSE_VERSION: 1,
                        PURPOSE_FINGERPRINT: fingerprint,
                        "cardboard_len": 800,
                        "cardboard_width": 200,
                        "special_process": "一开一",
                    }
                ],
            },
        )
        pending_after_partial = client.get("/api/requisition/pending")

    assert partial.status_code == 201, partial.text
    after_partial = _legacy_batch_write_counts(session_factory)
    assert after_partial == {
        "batch": before_partial["batch"] + 1,
        "item": before_partial["item"] + 1,
        "purpose": before_partial["purpose"] + 1,
        "audit": before_partial["audit"] + 1,
    }
    pending_row = next(
        row
        for row in pending_after_partial.json()["items"]
        if row.get("item_id") == partial_item_id
    )
    assert pending_row["already_requisitioned_qty"] == 400
    assert pending_row["remaining_requisition_qty"] == 100
    with session_factory() as session:
        partial_snapshot = session.scalar(
            select(PurchasePurposeSourceSnapshot).where(
                PurchasePurposeSourceSnapshot.source_order_item_id
                == partial_item_id
            )
        )
        assert partial_snapshot is not None
        assert partial_snapshot.purchase_sheet_qty == 500
        assert partial_snapshot.order_purpose_sheet_qty == 400
        assert partial_snapshot.reserve_purpose_sheet_qty == 100


def test_supplier_order_void_refreshes_order_item_from_order_purpose_only(
    requisition_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.supplier_requisition_order import (
        PurchasePurposeSourceSnapshot,
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )

    app, session_factory = requisition_app
    _prepare_order_item(session_factory, quantity=500)
    with TestClient(app) as client:
        _login(client, "sales")
        first_draft = _preview_supplier_order_draft(client, [_selection()])
        first_line = _single_line(first_draft)
        _set_purpose_plan(
            first_line,
            purchase_total=600,
            order_purpose=300,
            stock_purpose=300,
        )
        first = _save_draft(client, first_draft)
        assert first.status_code == 201, first.text

        second_draft = _preview_supplier_order_draft(client, [_selection()])
        second_line = _single_line(second_draft)
        _assert_purpose_plan(
            second_line,
            purchase_total=200,
            order_purpose=200,
            stock_purpose=0,
        )
        second = _save_draft(client, second_draft)
        assert second.status_code == 201, second.text
        second_order_id = _created_order_id(second)

        client.post("/api/auth/logout")
        _login(client, "admin")
        voided = client.put(
            f"/api/requisition/supplier-orders/{second_order_id}/void"
        )
        pending_after_void = client.get("/api/requisition/pending")

    assert voided.status_code == 200, voided.text
    assert pending_after_void.status_code == 200, pending_after_void.text
    pending_row = next(
        row
        for row in pending_after_void.json()["items"]
        if row.get("item_id") == 1
    )
    assert pending_row["already_requisitioned_qty"] == 300
    assert pending_row["remaining_requisition_qty"] == 200
    with session_factory() as session:
        order_item = session.get(OrderItem, 1)
        assert order_item is not None
        assert order_item.requisition_qty == 300
        # Preserve the established partial-procurement projection: the row is
        # still a reported business fact, while the pending API exposes the
        # remaining 200 sheets separately.
        assert order_item.requisition_status == "已报料"
        active_rows = list(
            session.scalars(
                select(SupplierRequisitionOrderItem)
                .join(
                    SupplierRequisitionOrder,
                    SupplierRequisitionOrder.id
                    == SupplierRequisitionOrderItem.supplier_order_id,
                )
                .where(
                    SupplierRequisitionOrder.status != "voided",
                    SupplierRequisitionOrderItem.status == "active"
                )
            )
        )
        assert len(active_rows) == 1
        active_snapshot = session.scalar(
            select(PurchasePurposeSourceSnapshot).where(
                PurchasePurposeSourceSnapshot.supplier_requisition_order_item_id
                == active_rows[0].id
            )
        )
        assert active_snapshot is not None
        assert active_snapshot.purchase_sheet_qty == 600
        assert active_snapshot.order_purpose_sheet_qty == 300
        assert active_snapshot.reserve_purpose_sheet_qty == 300


def test_composite_bom_void_refreshes_parent_from_order_purpose_only(
    composite_requisition_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product_bom import RequisitionItemBomSource
    from app.models.requisition import RequisitionItem
    from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot
    from app.services.purchase_purpose_allocation import (
        canonical_purchase_purpose_hash,
    )

    app, session_factory = composite_requisition_app
    component_fingerprint = canonical_purchase_purpose_hash(
        {
            "version": 1,
            "source_key": "bom_component:1:whole",
            "customer_id": 1,
            "effective_piece_qty": 20,
            "yield_per_sheet": 1,
            "authoritative_order_sheet_qty": 20,
        }
    )
    component_a = {
        **_component_payload(1, requisition_qty=25),
        PURCHASE_TOTAL: 25,
        ORDER_PURPOSE: 20,
        STOCK_PURPOSE: 5,
        PURPOSE_VERSION: 1,
        PURPOSE_FINGERPRINT: component_fingerprint,
    }
    with TestClient(app) as client:
        _login_composite(client)
        first = client.post(
            "/api/requisition/batches",
            json={
                "request_key": "p180-bom-void-first",
                "supplier_name": "N039 供应商",
                "items": [_parent_payload(), component_a],
            },
        )
        assert first.status_code == 201, first.text
        second = client.post(
            "/api/requisition/batches",
            json={
                "request_key": "p180-bom-void-second",
                "supplier_name": "N039 供应商",
                "items": [_component_payload(2)],
            },
        )
        assert second.status_code == 201, second.text
        second_batch_id = second.json()["id"]
        voided = client.put(
            f"/api/requisition/batches/{second_batch_id}/void",
            json={"reason": "验证作废只回算订单用途"},
        )

    assert voided.status_code == 200, voided.text
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        assert item is not None
        assert item.requisition_qty == 30
        assert item.requisition_status == "未报料"
        active_purpose_rows = list(
            session.execute(
                select(PurchasePurposeSourceSnapshot, RequisitionItem)
                .join(
                    RequisitionItem,
                    RequisitionItem.id
                    == PurchasePurposeSourceSnapshot.material_requisition_item_id,
                )
                .where(RequisitionItem.status == "有效")
            )
        )
        assert len(active_purpose_rows) == 2
        assert sum(
            snapshot.purchase_sheet_qty
            for snapshot, _requisition_item in active_purpose_rows
        ) == 35
        assert sum(
            snapshot.order_purpose_sheet_qty
            for snapshot, _requisition_item in active_purpose_rows
        ) == 30
        assert sum(
            snapshot.reserve_purpose_sheet_qty
            for snapshot, _requisition_item in active_purpose_rows
        ) == 5
        assert session.scalar(
            select(func.count(RequisitionItemBomSource.id)).where(
                RequisitionItemBomSource.active_guard == 1
            )
        ) == 1


def test_external_request_hash_override_is_ignored_and_cannot_bypass_replay(
    requisition_app,
) -> None:
    from app.models.supplier_requisition_order import SupplierRequisitionOrder

    app, session_factory = requisition_app
    _prepare_order_item(session_factory, quantity=10)
    forged_hash = "0" * 64
    with TestClient(app) as client:
        _login(client, "sales")
        draft = _preview_supplier_order_draft(client, [_selection()])
        draft["supplier_groups"][0]["request_hash_override"] = forged_hash
        created = _save_draft(client, draft)
        assert created.status_code == 201, created.text
        with session_factory() as session:
            order = session.scalar(select(SupplierRequisitionOrder))
            assert order is not None
            stored_hash = order.request_hash
            created_order_id = order.id
        assert stored_hash
        assert stored_hash != forged_hash
        after_created = _formal_write_counts(session_factory)

        replay = _save_draft(client, draft)
        assert replay.status_code == 201, replay.text
        assert replay.json()["idempotent_replay"] is True
        assert _created_order_id(replay) == created_order_id
        assert _formal_write_counts(session_factory) == after_created

        forged_changed = deepcopy(draft)
        forged_changed["supplier_groups"][0]["request_hash_override"] = stored_hash
        forged_changed["supplier_groups"][0]["lines"][0][
            "remark"
        ] = "外部字段不得覆盖服务端请求哈希"
        conflict = _save_draft(client, forged_changed)

    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["code"] == (
        "PURCHASE_PURPOSE_IDEMPOTENCY_CONFLICT"
    )
    assert _formal_write_counts(session_factory) == after_created


def test_same_purchase_spec_cannot_be_split_into_two_lines_to_round_twice(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    second_id = _add_pending_candidate(
        session_factory,
        94,
        product_code="P180-SPLIT-LINE-2",
        product_name="P1-80 恶意拆行来源二",
        quantity=10,
    )
    for item_id in (1, second_id):
        _prepare_order_item(
            session_factory,
            item_id=item_id,
            quantity=10,
            cutting_mode="一开三",
        )

    with TestClient(app) as client:
        _login(client, "sales")
        first_draft = _preview_supplier_order_draft(
            client,
            [
                _selection(
                    1,
                    cutting_mode="一开三",
                    report_width_mm=600,
                )
            ],
        )
        second_draft = _preview_supplier_order_draft(
            client,
            [
                _selection(
                    second_id,
                    cutting_mode="一开三",
                    report_width_mm=600,
                )
            ],
        )
        first_line = _single_line(first_draft)
        second_line = _single_line(second_draft)
        assert first_line["line_key"] == second_line["line_key"]
        assert first_line["requisition_qty"] == 4
        assert second_line["requisition_qty"] == 4
        malicious = deepcopy(first_draft)
        malicious["supplier_groups"][0]["lines"] = [
            first_line,
            second_line,
        ]
        before_submit = _formal_write_counts(session_factory)
        rejected = _save_draft(client, malicious)

    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"]["code"] == "PURCHASE_PURPOSE_TAMPERED"
    assert "同一采购规格被拆成多行" in rejected.json()["detail"]["message"]
    assert _formal_write_counts(session_factory) == before_submit


def test_legacy_direct_supplier_replay_and_conflicts_use_original_payload_hash(
    requisition_app,
) -> None:
    app, session_factory = requisition_app
    _prepare_order_item(session_factory, quantity=500)
    payload = {
        "request_key": "p180-direct-replay-01",
        "supplier_name": "苏州纸板供应商",
        "report_length_mm": 800,
        "report_width_mm": 200,
        "cutting_mode": "一开一",
        "remark": "P1-80 旧直连幂等",
        "members": [
            {
                "item_id": 1,
                "quantity": 500,
                "requisition_qty": 600,
                PURCHASE_TOTAL: 600,
                ORDER_PURPOSE: 500,
                STOCK_PURPOSE: 100,
                "cutting_mode": "一开一",
            }
        ],
    }
    with TestClient(app) as client:
        _login(client, "sales")
        created = client.post("/api/requisition/supplier-orders", json=payload)
        assert created.status_code == 201, created.text
        created_id = created.json()["id"]
        after_created = _formal_write_counts(session_factory)

        replay = client.post("/api/requisition/supplier-orders", json=payload)
        assert replay.status_code == 201, replay.text
        assert replay.json()["id"] == created_id
        assert _formal_write_counts(session_factory) == after_created

        changed = deepcopy(payload)
        changed["remark"] = "同键异载荷"
        payload_conflict = client.post(
            "/api/requisition/supplier-orders",
            json=changed,
        )
        assert payload_conflict.status_code == 409, payload_conflict.text
        assert payload_conflict.json()["detail"]["code"] == (
            "PURCHASE_PURPOSE_IDEMPOTENCY_CONFLICT"
        )
        assert _formal_write_counts(session_factory) == after_created

        client.post("/api/auth/logout")
        _login(client, "admin")
        after_actor_login = _formal_write_counts(session_factory)
        actor_conflict = client.post(
            "/api/requisition/supplier-orders",
            json=payload,
        )

    assert actor_conflict.status_code == 409, actor_conflict.text
    assert actor_conflict.json()["detail"]["code"] == (
        "PURCHASE_PURPOSE_ACTOR_MISMATCH"
    )
    assert _formal_write_counts(session_factory) == after_actor_login


def test_a3_legacy_direct_rejects_ambiguous_overrides_but_defaults_authoritatively(
    requisition_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.supplier_requisition_order import (
        PurchasePurposeSourceSnapshot,
        SupplierRequisitionOrderItem,
    )

    app, session_factory = requisition_app
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        assert item is not None
        product = session.get(Product, item.product_id)
        assert product is not None
        product.box_style = "A3 天地盖"
        item.quantity = 20
        item.subtotal = Decimal("72")
        item.snapshot_supplier_name = "苏州纸板供应商"
        item.snapshot_report_length_mm = 800
        item.snapshot_report_width_mm = 200
        item.snapshot_base_report_length_mm = 780
        item.snapshot_base_report_width_mm = 190
        item.snapshot_pieces_per_box = 1
        item.snapshot_splice_mode = "single"
        item.special_process = "一开一"
        session.commit()

    base_payload = {
        "supplier_name": "苏州纸板供应商",
        "report_length_mm": 800,
        "report_width_mm": 200,
        "cutting_mode": "一开一",
        "members": [{"item_id": 1, "cutting_mode": "一开一"}],
    }
    overrides = [
        {"requisition_qty": 20},
        {
            "requisition_qty": 20,
            PURCHASE_TOTAL: 20,
            ORDER_PURPOSE: 20,
            STOCK_PURPOSE: 0,
        },
    ]
    with TestClient(app) as client:
        _login(client, "sales")
        for index, override in enumerate(overrides, start=1):
            rejected_payload = deepcopy(base_payload)
            rejected_payload["request_key"] = f"p180-a3-override-{index:02d}"
            rejected_payload["members"][0].update(override)
            before_rejected = _formal_write_counts(session_factory)
            rejected = client.post(
                "/api/requisition/supplier-orders",
                json=rejected_payload,
            )
            assert rejected.status_code == 409, rejected.text
            assert rejected.json()["detail"]["code"] == (
                "PURCHASE_PURPOSE_TAMPERED"
            )
            assert "多组件订单不能通过旧直连接口覆盖" in rejected.json()[
                "detail"
            ]["message"]
            assert _formal_write_counts(session_factory) == before_rejected

        default_payload = deepcopy(base_payload)
        default_payload["request_key"] = "p180-a3-default-0001"
        created = client.post(
            "/api/requisition/supplier-orders",
            json=default_payload,
        )

    assert created.status_code == 201, created.text
    assert created.json()[PURCHASE_TOTAL] == 40
    assert created.json()[ORDER_PURPOSE] == 40
    assert created.json()[STOCK_PURPOSE] == 0
    with session_factory() as session:
        supplier_items = list(
            session.scalars(
                select(SupplierRequisitionOrderItem).order_by(
                    SupplierRequisitionOrderItem.id
                )
            )
        )
        snapshots = list(
            session.scalars(
                select(PurchasePurposeSourceSnapshot).order_by(
                    PurchasePurposeSourceSnapshot.component_type
                )
            )
        )
        assert len(supplier_items) == 2
        assert {row.source_key for row in supplier_items} == {
            "order_item:1:cover",
            "order_item:1:base",
        }
        assert len(snapshots) == 2
        assert sum(row.purchase_sheet_qty for row in snapshots) == 40
        assert sum(row.order_purpose_sheet_qty for row in snapshots) == 40


def test_composite_bom_partial_order_purpose_is_rejected_without_writes(
    composite_requisition_app,
) -> None:
    from app.services.purchase_purpose_allocation import (
        canonical_purchase_purpose_hash,
    )

    app, session_factory = composite_requisition_app
    fingerprint = canonical_purchase_purpose_hash(
        {
            "version": 1,
            "source_key": "bom_component:1:whole",
            "customer_id": 1,
            "effective_piece_qty": 20,
            "yield_per_sheet": 1,
            "authoritative_order_sheet_qty": 20,
        }
    )
    partial_component = {
        **_component_payload(1, requisition_qty=19),
        PURCHASE_TOTAL: 19,
        ORDER_PURPOSE: 19,
        STOCK_PURPOSE: 0,
        PURPOSE_VERSION: 1,
        PURPOSE_FINGERPRINT: fingerprint,
    }
    with TestClient(app) as client:
        _login_composite(client)
        before_submit = _legacy_batch_write_counts(session_factory)
        rejected = client.post(
            "/api/requisition/batches",
            json={
                "request_key": "p180-bom-partial-001",
                "supplier_name": "N039 供应商",
                "items": [_parent_payload(), partial_component],
            },
        )

    assert rejected.status_code == 400, rejected.text
    assert "不能少于库存抵扣后的系统最低 20 张" in rejected.json()["detail"]
    assert _legacy_batch_write_counts(session_factory) == before_submit


def test_composite_bom_snapshot_freezes_real_semi_reservation_quantity(
    composite_requisition_app,
) -> None:
    from datetime import date, datetime

    from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot
    from app.models.warehouse_inventory import (
        InventoryLot,
        InventoryReservation,
        WarehouseLocation,
    )

    app, session_factory = composite_requisition_app
    with session_factory() as session:
        location = WarehouseLocation(
            location_code="P180-SEMI-01",
            location_name="P1-80 匿名半成品暂存",
            warehouse_type="semi_finished",
        )
        session.add(location)
        session.flush()
        lot = InventoryLot(
            lot_number="P180-SEMI-LOT-01",
            inventory_type="semi_finished",
            warehouse_location_id=location.id,
            quantity_available=7,
            quantity_reserved=7,
            quantity_consumed=0,
            quantity_damaged=0,
            quantity_scrapped=0,
            unit="sheets",
            status="active",
            source_type="manual",
            stock_date=date(2026, 8, 20),
            last_movement_at=datetime.now(),
        )
        session.add(lot)
        session.flush()
        session.add(
            InventoryReservation(
                reservation_number="P180-SEMI-RES-01",
                inventory_lot_id=lot.id,
                reservation_type="semi_order",
                order_item_id=1,
                sales_order_item_bom_component_id=1,
                reserved_stock_quantity=7,
                credited_requirement_quantity=7,
                yield_factor=1,
                consumed_stock_quantity=0,
                released_stock_quantity=0,
                consumed_requirement_quantity=0,
                released_requirement_quantity=0,
                status="active",
            )
        )
        session.commit()

    with TestClient(app) as client:
        _login_composite(client)
        created = client.post(
            "/api/requisition/batches",
            json={
                "request_key": "p180-bom-semi-snapshot",
                "supplier_name": "N039 供应商",
                "items": [_parent_payload(), _component_payload(1)],
            },
        )

    assert created.status_code == 201, created.text
    with session_factory() as session:
        component_snapshot = session.scalar(
            select(PurchasePurposeSourceSnapshot).where(
                PurchasePurposeSourceSnapshot.source_key
                == "bom_component:1:whole"
            )
        )
        assert component_snapshot is not None
        assert component_snapshot.source_required_piece_qty_snapshot == 20
        assert component_snapshot.source_semi_reserved_piece_qty_snapshot == 7
        assert component_snapshot.source_effective_piece_qty_snapshot == 13
        assert component_snapshot.group_authoritative_order_sheet_qty_snapshot == 13
        assert component_snapshot.purchase_sheet_qty == 13
        assert component_snapshot.order_purpose_sheet_qty == 13
        assert component_snapshot.reserve_purpose_sheet_qty == 0


def test_bom_item_cannot_use_ordinary_pending_finalize_or_merge_paths(
    composite_requisition_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product import Product

    app, session_factory = composite_requisition_app
    with session_factory() as session:
        normal_product = Product(
            customer_id=1,
            product_code="P180-NORMAL-FOR-GUARD",
            customer_material_code="P180-NORMAL-FOR-GUARD",
            product_name="P1-80 普通报料对照项",
            box_category="normal",
            box_style="A1",
        )
        session.add(normal_product)
        session.flush()
        normal_item = OrderItem(
            order_id=1,
            product_id=normal_product.id,
            quantity=10,
            unit_price=Decimal("1"),
            subtotal=Decimal("10"),
            material_status="pending",
            requisition_status="未报料",
            snapshot_product_code=normal_product.product_code,
            snapshot_product_name=normal_product.product_name,
            snapshot_spec="800×200",
            snapshot_material="K616K",
            snapshot_supplier_name="N039 供应商",
            snapshot_report_length_mm=800,
            snapshot_report_width_mm=200,
            snapshot_pieces_per_box=1,
            snapshot_splice_mode="single",
            special_process="一开一",
        )
        session.add(normal_item)
        session.commit()
        normal_item_id = normal_item.id

    selection = {
        "type": "order_item",
        "order_item_id": 1,
        "supplier_name": "N039 供应商",
        "report_length_mm": 900,
        "report_width_mm": 600,
        "cutting_mode": "一开一",
    }
    with TestClient(app) as client:
        _login_composite(client)
        before_rejected = _formal_write_counts(session_factory)
        before_merge = _legacy_batch_write_counts(session_factory)
        preview_rejected = client.post(
            "/api/requisition/supplier-orders/preview-from-pending-selection",
            json={"selections": [selection]},
        )
        assert preview_rejected.status_code == 409, preview_rejected.text
        assert "组合/BOM订单必须按冻结物理组件报料" in preview_rejected.json()[
            "detail"
        ]

        normal_draft = _preview_supplier_order_draft(
            client,
            [
                {
                    **selection,
                    "order_item_id": normal_item_id,
                    "report_length_mm": 800,
                    "report_width_mm": 200,
                }
            ],
        )
        malicious = deepcopy(normal_draft)
        malicious["supplier_groups"][0]["lines"][0]["source_items"][0][
            "order_item_id"
        ] = 1
        finalize_rejected = _save_draft(client, malicious)
        assert finalize_rejected.status_code == 409, finalize_rejected.text

        merge_rejected = client.post(
            "/api/requisition/merge-groups",
            json={
                "member_item_ids": [1, normal_item_id],
                "supplier_name": "N039 供应商",
                "report_length_mm": 900,
                "report_width_mm": 600,
                "cutting_mode": "一开一",
                "remark": "BOM父件不得走普通合并",
            },
        )

    assert merge_rejected.status_code == 409, merge_rejected.text
    assert "多物理组件订单不能加入普通合并组" in merge_rejected.json()[
        "detail"
    ]
    assert _formal_write_counts(session_factory) == before_rejected
    assert _legacy_batch_write_counts(session_factory) == before_merge


def test_a3_multi_physical_item_cannot_enter_plain_merge_group(
    requisition_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product import Product

    app, session_factory = requisition_app
    second_id = _add_pending_candidate(
        session_factory,
        95,
        product_code="P180-A3-MERGE-NORMAL",
        product_name="P1-80 A3合并对照项",
        quantity=10,
    )
    _prepare_order_item(session_factory, item_id=second_id, quantity=10)
    with session_factory() as session:
        item = session.get(OrderItem, 1)
        assert item is not None
        product = session.get(Product, item.product_id)
        assert product is not None
        product.box_style = "A3 天地盖"
        item.quantity = 10
        item.snapshot_report_length_mm = 800
        item.snapshot_report_width_mm = 200
        item.snapshot_base_report_length_mm = 780
        item.snapshot_base_report_width_mm = 190
        item.snapshot_pieces_per_box = 1
        item.snapshot_splice_mode = "single"
        session.commit()

    with TestClient(app) as client:
        _login(client, "sales")
        before_merge = _legacy_batch_write_counts(session_factory)
        rejected = client.post(
            "/api/requisition/merge-groups",
            json={
                "member_item_ids": [1, second_id],
                "supplier_name": "苏州纸板供应商",
                "report_length_mm": 800,
                "report_width_mm": 200,
                "cutting_mode": "一开一",
                "remark": "天地盖不得走普通合并",
            },
        )

    assert rejected.status_code == 409, rejected.text
    assert "多物理组件订单不能加入普通合并组" in rejected.json()["detail"]
    assert _legacy_batch_write_counts(session_factory) == before_merge


def test_cross_customer_merge_is_rejected_at_create_preview_and_finalize(
    requisition_app,
) -> None:
    from datetime import date

    from app.models.customer import Customer
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.requisition import Requisition, RequisitionItem

    app, session_factory = requisition_app
    same_a_id = _add_pending_candidate(
        session_factory,
        96,
        product_code="P180-MERGE-SAME-A",
        product_name="P1-80 同客户来源A",
        quantity=10,
    )
    same_b_id = _add_pending_candidate(
        session_factory,
        97,
        product_code="P180-MERGE-SAME-B",
        product_name="P1-80 同客户来源B",
        quantity=10,
    )
    for item_id in (1, same_a_id, same_b_id):
        _prepare_order_item(session_factory, item_id=item_id, quantity=10)

    with session_factory() as session:
        other_customer = Customer(
            customer_number=2,
            customer_code="P180-CROSS",
            name="P1-80 匿名跨客户",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        session.add(other_customer)
        session.flush()
        other_product = Product(
            customer_id=other_customer.id,
            product_code="P180-CROSS-PRODUCT",
            customer_material_code="P180-CROSS-PRODUCT",
            product_name="P1-80 跨客户来源",
            box_category="normal",
            box_style="A1",
        )
        session.add(other_product)
        session.flush()
        other_order = Order(
            order_number="P180-CROSS-ORDER",
            customer_id=other_customer.id,
            order_date=date(2026, 8, 20),
            delivery_date=date(2026, 8, 30),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("10"),
        )
        session.add(other_order)
        session.flush()
        other_item = OrderItem(
            order_id=other_order.id,
            product_id=other_product.id,
            quantity=10,
            unit_price=Decimal("1"),
            subtotal=Decimal("10"),
            material_status="pending",
            requisition_status="未报料",
            snapshot_product_code=other_product.product_code,
            snapshot_product_name=other_product.product_name,
            snapshot_spec="800×200",
            snapshot_material="K=A-BC",
            snapshot_supplier_name="苏州纸板供应商",
            snapshot_report_length_mm=800,
            snapshot_report_width_mm=200,
            snapshot_pieces_per_box=1,
            snapshot_splice_mode="single",
            special_process="一开一",
        )
        session.add(other_item)
        session.commit()
        other_customer_id = other_customer.id
        other_item_id = other_item.id

    def seed_merged_group(number: str, item_ids: list[int]) -> int:
        with session_factory() as session:
            group = Requisition(
                requisition_number=number,
                requisition_date=date(2026, 8, 20),
                supplier_name="苏州纸板供应商",
                status="merged_pending",
                created_by=1,
            )
            session.add(group)
            session.flush()
            for item_id in item_ids:
                item = session.get(OrderItem, item_id)
                assert item is not None
                session.add(
                    RequisitionItem(
                        requisition_id=group.id,
                        order_item_id=item.id,
                        inventory_deducted_qty=0,
                        requisition_qty=10,
                        cardboard_len=800,
                        cardboard_width=200,
                        pieces_per_box=1,
                        required_piece_qty=10,
                        special_process="一开一",
                        material_snapshot=item.snapshot_material,
                        product_code_snapshot=item.snapshot_product_code,
                        product_name_snapshot=item.snapshot_product_name,
                        specification_snapshot=item.snapshot_spec,
                        status="merged_pending",
                    )
                )
            session.commit()
            return group.id

    with TestClient(app) as client:
        _login(client, "sales")
        before_create = _legacy_batch_write_counts(session_factory)
        create_rejected = client.post(
            "/api/requisition/merge-groups",
            json={
                "member_item_ids": [1, other_item_id],
                "supplier_name": "苏州纸板供应商",
                "report_length_mm": 800,
                "report_width_mm": 200,
                "cutting_mode": "一开一",
                "remark": "跨客户不得合并",
            },
        )
        assert create_rejected.status_code == 409, create_rejected.text
        assert "不能跨客户" in create_rejected.json()["detail"]
        assert _legacy_batch_write_counts(session_factory) == before_create

        historical_cross_id = seed_merged_group(
            "P180-HIST-CROSS",
            [1, other_item_id],
        )
        before_historical_preview = _formal_write_counts(session_factory)
        preview_rejected = client.post(
            "/api/requisition/supplier-orders/preview-from-pending-selection",
            json={
                "selections": [
                    {
                        "type": "merge_group",
                        "merge_group_id": historical_cross_id,
                        "supplier_name": "苏州纸板供应商",
                        "report_length_mm": 800,
                        "report_width_mm": 200,
                        "cutting_mode": "一开一",
                    }
                ]
            },
        )
        assert preview_rejected.status_code == 409, preview_rejected.text
        assert "不能跨客户" in preview_rejected.json()["detail"]
        assert _formal_write_counts(session_factory) == before_historical_preview

        same_group_id = seed_merged_group(
            "P180-HIST-FINALIZE",
            [same_a_id, same_b_id],
        )
        valid_draft = _preview_supplier_order_draft(
            client,
            [
                {
                    "type": "merge_group",
                    "merge_group_id": same_group_id,
                    "supplier_name": "苏州纸板供应商",
                    "report_length_mm": 800,
                    "report_width_mm": 200,
                    "cutting_mode": "一开一",
                }
            ],
        )
        with session_factory() as session:
            same_b = session.get(OrderItem, same_b_id)
            assert same_b is not None
            same_b_order = session.get(Order, same_b.order_id)
            assert same_b_order is not None
            same_b_order.customer_id = other_customer_id
            session.commit()
        before_finalize = _formal_write_counts(session_factory)
        finalize_rejected = _save_draft(client, valid_draft)

    assert finalize_rejected.status_code == 409, finalize_rejected.text
    assert "不能跨客户" in finalize_rejected.json()["detail"]
    assert _formal_write_counts(session_factory) == before_finalize
