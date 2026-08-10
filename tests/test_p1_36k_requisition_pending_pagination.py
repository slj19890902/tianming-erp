from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import event, select


from test_p1_36j_requisition_projection import _fixture as _projection_fixture
from test_phase11_requisition import _login, requisition_app


def _supplier_name(row: dict) -> str:
    return str(
        row.get("supplier_name")
        or row.get("snapshot_supplier_name")
        or "未设置供应商"
    ).strip()


def _set_supplier_names(fixture: dict) -> None:
    from app.models.order import OrderItem
    from app.models.requisition import Requisition

    with fixture["factory"]() as db:
        ordinary = db.scalars(
            select(OrderItem).where(
                OrderItem.id.in_(fixture["ordinary_ids"])
            )
        ).all()
        for index, item in enumerate(sorted(ordinary, key=lambda row: row.id)):
            item.snapshot_supplier_name = (
                "   "
                if index == len(ordinary) - 1
                else ("鸣朋" if index % 2 == 0 else "嘉林亿")
            )
        held = db.get(OrderItem, fixture["held_id"])
        hidden = db.get(OrderItem, fixture["hidden_id"])
        assert held is not None and hidden is not None
        held.snapshot_supplier_name = "暂缓供应商"
        hidden.snapshot_supplier_name = "隐藏供应商"
        merge_group = db.get(Requisition, fixture["merge_group_id"])
        assert merge_group is not None
        merge_group.supplier_name = "   "
        db.commit()


def test_opt_in_pages_reassemble_legacy_rows_and_clamp_without_scope_leak(
    tmp_path: Path,
) -> None:
    from app.api.requisition import pending_requisitions
    from app.models.user import User

    fixture = _projection_fixture(tmp_path, suffix="pages", ordinary_count=5)
    _set_supplier_names(fixture)

    with fixture["factory"]() as db:
        user = db.get(User, fixture["user_id"])
        assert user is not None
        legacy = pending_requisitions(db, user)
        pages = [
            pending_requisitions(db, user, page=page, page_size=2)
            for page in range(1, 4)
        ]
        page_only = pending_requisitions(db, user, page=2)
        page_size_only = pending_requisitions(db, user, page_size=2)
        overlarge = pending_requisitions(db, user, page=999, page_size=2)

    assert set(legacy) == {
        "items",
        "total",
        "auto_released_hold_ids",
        "supplier_counts",
    }
    assert [row for response in pages for row in response["items"]] == legacy[
        "items"
    ]
    assert pages[0]["items"][0]["is_merge_group"] is True
    assert all(response["total"] == legacy["total"] for response in pages)
    assert all(
        response["overall_total"] == legacy["total"] for response in pages
    )
    assert all(
        response["supplier_counts"] == legacy["supplier_counts"]
        for response in pages
    )
    assert overlarge["page"] == 3
    assert overlarge["items"] == pages[-1]["items"]
    assert page_only["page"] == 1 and page_only["page_size"] == 25
    assert page_size_only["page"] == 1
    assert page_size_only["items"] == pages[0]["items"]

    visible_ids = {
        int(row["item_id"])
        for row in legacy["items"]
        if not row.get("is_merge_group")
    }
    assert fixture["held_id"] not in visible_ids
    assert fixture["hidden_id"] not in visible_ids
    supplier_names = {
        row["supplier_name"] for row in legacy["supplier_counts"]
    }
    assert "" in supplier_names
    assert "未设置供应商" in supplier_names
    assert "暂缓供应商" not in supplier_names
    assert "隐藏供应商" not in supplier_names


def test_pending_pagination_http_validation_rejects_invalid_bounds(
    requisition_app,
) -> None:
    from fastapi.testclient import TestClient

    app, _session_factory = requisition_app
    with TestClient(app) as client:
        _login(client, "sales")
        assert client.get("/api/requisition/pending?page=0").status_code == 422
        assert client.get(
            "/api/requisition/pending?page_size=0"
        ).status_code == 422
        assert client.get(
            "/api/requisition/pending?page_size=201"
        ).status_code == 422


def test_supplier_filter_is_exact_global_and_empty_result_stays_on_page_one(
    tmp_path: Path,
) -> None:
    from app.api.requisition import pending_requisitions
    from app.models.user import User

    fixture = _projection_fixture(tmp_path, suffix="supplier", ordinary_count=6)
    _set_supplier_names(fixture)

    with fixture["factory"]() as db:
        user = db.get(User, fixture["user_id"])
        assert user is not None
        legacy = pending_requisitions(db, user)
        expected = [
            row for row in legacy["items"] if _supplier_name(row) == "鸣朋"
        ]
        first = pending_requisitions(
            db,
            user,
            page=1,
            page_size=2,
            supplier_name="鸣朋",
        )
        second = pending_requisitions(
            db,
            user,
            page=2,
            page_size=2,
            supplier_name="鸣朋",
        )
        supplier_only = pending_requisitions(
            db,
            user,
            supplier_name="鸣朋",
        )
        missing = pending_requisitions(
            db,
            user,
            page=99,
            page_size=2,
            supplier_name="不存在的供应商",
        )

    assert first["items"] + second["items"] == expected
    assert supplier_only["items"] == expected
    assert supplier_only["page"] == 1
    assert supplier_only["page_size"] == 25
    assert first["total"] == second["total"] == len(expected)
    assert first["overall_total"] == legacy["total"]
    assert first["supplier_counts"] == legacy["supplier_counts"]
    assert missing["items"] == []
    assert missing["total"] == 0
    assert missing["overall_total"] == legacy["total"]
    assert missing["page"] == 1
    assert missing["supplier_counts"] == legacy["supplier_counts"]


def test_paged_complex_bom_and_inventory_rows_are_deep_equal_to_legacy(
    tmp_path: Path,
) -> None:
    from app.api.requisition import pending_requisitions
    from app.models.user import User
    from test_p1_09c_requisition_query_scaling import (
        _add_visible_bom_snapshots,
        _add_visible_inventory_shape,
        _add_visible_semi_requirements,
        _fixture,
    )

    fixture_path = tmp_path / "complex"
    fixture_path.mkdir()
    _engine, factory, user_id = _fixture(fixture_path, visible_count=3)
    _add_visible_bom_snapshots(factory)
    _add_visible_semi_requirements(factory)
    _add_visible_inventory_shape(factory, matching=True, include_finished=True)

    with factory() as db:
        user = db.get(User, user_id)
        assert user is not None
        legacy = pending_requisitions(db, user)
        paged = pending_requisitions(db, user, page=1, page_size=2)
        next_page = pending_requisitions(db, user, page=2, page_size=2)

    assert paged["items"] + next_page["items"] == legacy["items"]
    assert paged["total"] == paged["overall_total"] == legacy["total"]
    assert any(row.get("is_composite_bom") for row in paged["items"] + next_page["items"])
    assert paged["supplier_counts"] == legacy["supplier_counts"]


def test_inventory_covered_rows_do_not_leave_supplier_count_ghosts(
    tmp_path: Path,
) -> None:
    from app.api.requisition import pending_requisitions
    from app.models.order import Order, OrderItem
    from app.models.user import User
    from app.models.warehouse_inventory import InventoryLot, InventoryReservation
    from test_p1_09c_requisition_query_scaling import (
        _add_visible_inventory_shape,
        _fixture,
    )

    fixture_path = tmp_path / "inventory-covered"
    fixture_path.mkdir()
    _engine, factory, user_id = _fixture(fixture_path, visible_count=1)
    with factory() as db:
        item = db.scalar(
            select(OrderItem)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.order_number.like("P1-09C-V-%"))
        )
        assert item is not None
        item.quantity = 10
        item.snapshot_supplier_name = "鸣朋"
        db.commit()
    _add_visible_inventory_shape(factory, matching=True, include_finished=True)
    with factory() as db:
        item = db.scalar(
            select(OrderItem)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.order_number.like("P1-09C-V-%"))
        )
        lot = db.scalar(
            select(InventoryLot).where(InventoryLot.inventory_type == "finished")
        )
        assert item is not None and lot is not None
        lot.quantity_available = 10
        lot.quantity_consumed = 10
        db.add(
            InventoryReservation(
                reservation_number="P1-36K-CONSUMED",
                inventory_lot_id=lot.id,
                reservation_type="finished_order",
                order_id=item.order_id,
                order_item_id=item.id,
                reserved_stock_quantity=10,
                consumed_stock_quantity=10,
                released_stock_quantity=0,
                credited_requirement_quantity=10,
                released_requirement_quantity=0,
                status="consumed",
                reserved_by=user_id,
                reservation_group_key="p1-36k-consumed",
                idempotency_key="p1-36k-consumed",
            )
        )
        db.commit()

    with factory() as db:
        user = db.get(User, user_id)
        assert user is not None
        legacy = pending_requisitions(db, user)
        paged = pending_requisitions(
            db,
            user,
            page=1,
            page_size=25,
        )
        filtered = pending_requisitions(
            db,
            user,
            page=1,
            page_size=25,
            supplier_name="鸣朋",
        )

    assert legacy["items"] == []
    assert legacy["total"] == 0
    assert legacy["supplier_counts"] == []
    assert paged["items"] == []
    assert paged["total"] == paged["overall_total"] == 0
    assert paged["supplier_counts"] == []
    assert filtered["items"] == []
    assert filtered["total"] == filtered["overall_total"] == 0
    assert filtered["supplier_counts"] == []


def test_fixed_page_has_bounded_queries_bytes_and_zero_writes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import app.api.requisition as requisition_api
    from app.models.user import User
    from test_p1_09c_requisition_query_scaling import (
        _add_visible_bom_snapshots,
        _fixture as _complex_fixture,
    )

    original_format_supplier_material = requisition_api._format_supplier_material
    decoration_calls = 0

    def format_supplier_material_spy(*args, **kwargs):
        nonlocal decoration_calls
        decoration_calls += 1
        return original_format_supplier_material(*args, **kwargs)

    monkeypatch.setattr(
        requisition_api,
        "_format_supplier_material",
        format_supplier_material_spy,
    )

    select_counts: dict[int, int] = {}
    response_sizes: dict[int, int] = {}
    decoration_counts: dict[int, int] = {}
    for count in (1, 20, 100):
        fixture_path = tmp_path / str(count)
        fixture_path.mkdir()
        engine, factory, user_id = _complex_fixture(
            fixture_path,
            visible_count=count,
        )
        _add_visible_bom_snapshots(factory)
        statements: list[str] = []

        def before_cursor_execute(
            _conn,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            statements.append(str(statement).lstrip().lower())

        event.listen(engine, "before_cursor_execute", before_cursor_execute)
        before = decoration_calls
        try:
            with factory() as db:
                user = db.get(User, user_id)
                assert user is not None
                statements.clear()
                response = requisition_api.pending_requisitions(
                    db,
                    user,
                    page=1,
                    page_size=20,
                )
        finally:
            event.remove(
                engine,
                "before_cursor_execute",
                before_cursor_execute,
            )

        assert not any(
            statement.startswith(("insert", "update", "delete"))
            for statement in statements
        )
        select_counts[count] = sum(
            statement.startswith(("select", "with")) for statement in statements
        )
        response_sizes[count] = len(
            json.dumps(response, ensure_ascii=False, default=str).encode("utf-8")
        )
        decoration_counts[count] = decoration_calls - before
        assert len(response["items"]) <= 20
        assert response["overall_total"] == count

    assert select_counts[100] <= select_counts[20] + 3
    assert response_sizes[100] <= response_sizes[20] + 1024
    assert decoration_counts[100] <= 20
