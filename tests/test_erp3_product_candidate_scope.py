"""Order suggestions must obey existing save gates before counting/paging."""
from types import SimpleNamespace

import pytest
from sqlalchemy import event, literal, select
from sqlalchemy.sql import visitors

from tests.test_p1_09c_product_query_scaling import _fixture
from tests.test_customer_sharing import reservation_db


def _rows(db, user, **kwargs):
    from app.api.products import list_products
    return list_products(db=db, user=user, selection_context="order",
                         response_mode="summary", page=1, page_size=1, **kwargs)


@pytest.mark.parametrize("inactive,status", [(True, "active"), (False, "inactive")])
def test_order_candidates_reject_unavailable_customer_but_master_history_remains(tmp_path, inactive, status):
    from app.api.products import list_products
    from app.models.customer import Customer
    from app.models.user import User
    engine, factory, uid = _fixture(tmp_path, visible_count=2)
    try:
        with factory() as db:
            user = db.get(User, uid)
            customer = db.get(Customer, 1)
            customer.is_active = not inactive
            customer.status = status
            db.commit()
            result = _rows(db, user, customer_id=1, include_inactive=True)
            assert result["items"] == [] and result["total"] == 0
            history = list_products(db=db, user=user, selection_context="master_data",
                                    response_mode="summary", page=1, page_size=20,
                                    customer_id=1, include_inactive=True)
            assert history["total"] == 2
    finally:
        engine.dispose()


@pytest.mark.parametrize("enabled", [True, False])
def test_page_drawing_batch_preserves_shared_policy_and_full_payload(reservation_db, enabled):
    from app.api.products import _response
    from app.models.shared_finished_stock import SharedFinishedGroup, SharedFinishedMember
    from app.models.product_drawing import ProductDrawing
    from app.services.product_drawings import list_product_drawings
    from tests.test_shared_catalog_assets import catalog
    db, data = reservation_db
    source, peer = catalog(db, data)
    member = db.get(SharedFinishedMember, source.id)
    db.get(SharedFinishedGroup, member.group_id).enabled = enabled
    db.add_all([
        ProductDrawing(product_id=source.id, image_path="private:drawings/source.pdf", thumbnail_path="private:drawings/source.pdf"),
        ProductDrawing(product_id=peer.id, image_path="private:drawings/peer.pdf", thumbnail_path="private:drawings/peer.pdf"),
        ProductDrawing(product_id=data["other_product"].id, image_path="private:drawings/other.pdf", thumbnail_path="private:drawings/other.pdf"),
    ])
    db.commit()
    rows = [source, peer, data["other_product"]]
    before = [_response(p, data["admin"]) for p in rows]
    batched = list_product_drawings(db, rows)
    after = [_response(p, data["admin"], drawings_by_product=batched) for p in rows]
    assert after == before
    assert len(after[0]["drawings"]) == (2 if enabled else 1)
    assert len(after[2]["drawings"]) == 1
    assert list_product_drawings(db, []) == {}


def test_order_candidate_gates_apply_before_paging_and_cannot_be_bypassed(tmp_path):
    from app.models.product import Product
    from app.models.user import User
    engine, factory, uid = _fixture(tmp_path, visible_count=5)
    statements = []
    try:
        with factory() as db:
            user = db.get(User, uid)
            rows = db.scalars(select(Product).where(Product.customer_id == 1).order_by(Product.id)).all()
            rows[0].is_active = False
            rows[1].box_style = "00001 模具费"
            rows[2].box_style = " ZHJ 纸护角 "
            # An explicitly saved external supply contract remains authoritative.
            rows[3].box_style = "ZHJ 纸护角"
            rows[3].supply_mode = "external_purchase"
            rows[3].external_packaging_category_code = "paper_corner_guard"
            rows[3].external_packaging_specification_json = '{"length_mm":100}'
            rows[3].external_packaging_specification_summary = "合成护角100mm"
            rows[3].external_packaging_purchase_unit = "根"
            rows[3].external_packaging_candidate_snapshot_json = '[{"is_default":true}]'
            db.commit()
            event.listen(engine, "before_cursor_execute", lambda _c, _u, sql, _p, _x, _m: statements.append(sql))
            result = _rows(db, user, customer_id=1, include_inactive=True)
            assert result["total"] == 2 and result["total_pages"] == 2
            assert result["items"][0]["id"] == rows[3].id
            # Ordinary products need not have requisition fields to be found for
            # manual orders; frozen/stock-derived operations have their own gates.
            from app.api.products import list_products
            second = list_products(db=db, user=user, selection_context="order", response_mode="summary",
                                   customer_id=1, page=2, page_size=1, include_inactive=True)
            assert second["items"][0]["id"] == rows[4].id
            assert second["items"][0]["readiness"]["order_save_missing_labels"]
            assert not any(sql.lstrip().lower().startswith(("insert", "update", "delete")) for sql in statements)
    finally:
        engine.dispose()


def test_sql_candidate_classification_matches_existing_save_guard(tmp_path):
    from app.models.product import Product
    from app.services.legacy_product_classification import (
        EXTERNAL_ALIASES, EXPENSE_ALIASES, new_order_issue, new_order_issue_condition,
    )
    engine, factory, _uid = _fixture(tmp_path, visible_count=0)
    try:
        with factory() as db:
            for style in [None, "A1", *EXTERNAL_ALIASES, *EXPENSE_ALIASES]:
                for mode in [None, "corrugated_production", "external_purchase"]:
                    for category in [None, "", "paper_corner_guard", " "]:
                        raw = None if style is None else "\t\u3000" + style + "\n "
                        values = dict(box_style=raw, supply_mode=mode,
                                      external_packaging_category_code=category)
                        # Evaluate the SQL predicate using bound values, including
                        # legacy/null variants that today's write schema forbids.
                        # No invalid product facts are inserted or checks disabled.
                        def bind(column):
                            if getattr(column, "table", None) is Product.__table__:
                                return literal(values[column.name])
                            return None
                        expression = visitors.replacement_traverse(new_order_issue_condition(), {}, bind)
                        actual = db.scalar(select(expression))
                        assert bool(actual) == bool(new_order_issue(SimpleNamespace(**values))), values
    finally:
        engine.dispose()
