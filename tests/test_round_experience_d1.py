from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select

from test_p1_36j_requisition_projection import _fixture


def test_pending_requisition_keeps_order_drawing_frozen_and_projects_common_box_fallback(
    tmp_path: Path,
) -> None:
    """The workbench can read context, but never rewrites order or requisition facts."""
    from app.api.requisition import pending_requisitions
    from app.models.order import OrderItem
    from app.models.product_drawing import ProductDrawing
    from app.models.requisition import Requisition, RequisitionItem
    from app.models.user import User

    fixture = _fixture(tmp_path, suffix="experience-d1", ordinary_count=2)
    with fixture["factory"]() as db:
        first, second = [
            db.get(OrderItem, item_id) for item_id in fixture["ordinary_ids"]
        ]
        assert first is not None and second is not None
        first.drawing_file = "private:order-drawings/frozen-line.pdf"
        db.add_all(
            [
                ProductDrawing(
                    product_id=first.product_id,
                    image_path="private:drawings/older.webp",
                    thumbnail_path="private:drawing_thumbnails/older.webp",
                ),
                ProductDrawing(
                    product_id=first.product_id,
                    image_path="private:drawings/current.webp",
                    thumbnail_path="private:drawing_thumbnails/current.webp",
                ),
            ]
        )
        db.commit()
        expected = {
            "drawing_file": first.drawing_file,
            "requisitions": db.scalar(select(func.count()).select_from(Requisition)),
            "requisition_items": db.scalar(
                select(func.count()).select_from(RequisitionItem)
            ),
        }

    with fixture["factory"]() as db:
        user = db.get(User, fixture["user_id"])
        assert user is not None
        rows = pending_requisitions(db, user)["items"]
        first_row = next(row for row in rows if row.get("item_id") == first.id)
        second_row = next(row for row in rows if row.get("item_id") == second.id)
        merge_row = next(row for row in rows if row.get("is_merge_group"))

        assert first_row["order_drawing_path"] == (
            f"/api/orders/items/{first.id}/drawing/content/file.pdf"
        )
        assert first_row["drawing_path"] == first_row["order_drawing_path"]
        assert first_row["drawing_source"] == "订单图纸"
        assert first_row["drawing_is_pdf"] is True
        assert second_row["order_drawing_path"] is None
        assert second_row["product_drawing_path"].endswith("/original.webp")
        assert second_row["drawing_path"] == second_row["product_drawing_path"]
        assert second_row["drawing_source"] == "常用箱图纸"
        assert second_row["drawing_is_pdf"] is False
        assert isinstance(second_row["common_box_readiness"], dict)
        assert "ready" in second_row["common_box_readiness"]
        assert "drawing_path" not in merge_row

    with fixture["factory"]() as db:
        first_after = db.get(OrderItem, first.id)
        assert first_after is not None
        assert first_after.drawing_file == expected["drawing_file"]
        assert db.scalar(select(func.count()).select_from(Requisition)) == expected[
            "requisitions"
        ]
        assert db.scalar(select(func.count()).select_from(RequisitionItem)) == expected[
            "requisition_items"
        ]
