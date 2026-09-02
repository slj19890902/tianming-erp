from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.test_p1_32a2_requisition_production_print import (
    _login,
    production_print_app,
)


ROOT = Path(__file__).resolve().parents[1]
PRINT_PAGE = (ROOT / "static" / "requisition-production-print.html").read_text(
    encoding="utf-8"
)


@pytest.fixture()
def stock_replenishment_print_app(production_print_app):
    """Add stock-plan sources without inventing sales orders or tasks."""

    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.product import Product
    from app.models.stock_replenishment import (
        StockReplenishmentOrder,
        StockReplenishmentOrderItem,
    )

    factory = production_print_app["session_factory"]
    with factory() as db:
        customer = db.scalar(
            select(Customer).where(Customer.customer_code == "P132A2-A")
        )
        other_customer = db.scalar(
            select(Customer).where(Customer.customer_code == "P132A2-B")
        )
        material = db.scalar(select(Material).where(Material.code == "K=A"))
        reference_product = db.get(Product, production_print_app["product_id"])
        assert customer is not None
        assert other_customer is not None
        assert material is not None
        assert reference_product is not None

        liner = Product(
            customer_id=customer.id,
            product_code="P038-LINER",
            customer_material_code="P038-LINER",
            product_name="P0-38 衬板成品",
            material_id=material.id,
            legacy_material_text="K=A",
            length_mm=Decimal("600"),
            width_mm=Decimal("400"),
            box_category="normal",
            box_style="衬板",
        )
        db.add(liner)
        db.flush()

        semi_order = StockReplenishmentOrder(
            order_number="SR-P038-SEMI",
            supplier_name="P0-38 纸板厂",
            customer_id=customer.id,
            source_type="stock_warning",
            status="confirmed",
        )
        finished_order = StockReplenishmentOrder(
            order_number="SR-P038-FINISHED",
            supplier_name="P0-38 纸板厂",
            customer_id=customer.id,
            source_type="stock_warning",
            status="confirmed",
        )
        voided_order = StockReplenishmentOrder(
            order_number="SR-P038-VOIDED",
            supplier_name="P0-38 纸板厂",
            customer_id=customer.id,
            source_type="stock_warning",
            status="voided",
        )
        missing_product_order = StockReplenishmentOrder(
            order_number="SR-P038-NO-PRODUCT",
            supplier_name="P0-38 纸板厂",
            customer_id=customer.id,
            source_type="manual_history",
            status="confirmed",
        )
        missing_customer_order = StockReplenishmentOrder(
            order_number="SR-P038-NO-CUSTOMER",
            supplier_name="P0-38 纸板厂",
            customer_id=None,
            source_type="manual_history",
            status="confirmed",
        )
        dirty_cross_customer_order = StockReplenishmentOrder(
            order_number="SR-P038-DIRTY-CUSTOMER",
            supplier_name="P0-38 纸板厂",
            customer_id=other_customer.id,
            source_type="manual_history",
            status="confirmed",
        )
        db.add_all(
            [
                semi_order,
                finished_order,
                voided_order,
                missing_product_order,
                missing_customer_order,
                dirty_cross_customer_order,
            ]
        )
        db.flush()

        def semi_item(
            order: StockReplenishmentOrder,
            *,
            product: Product | None = reference_product,
            item_customer_id: int | None = customer.id,
            quantity: int = 40,
        ) -> StockReplenishmentOrderItem:
            return StockReplenishmentOrderItem(
                replenishment_order_id=order.id,
                target_inventory_type="semi_finished",
                product_id=None,
                reference_product_id=product.id if product is not None else None,
                customer_id=item_customer_id,
                material_id=material.id,
                product_code_snapshot=(product.product_code if product else "P038-ORPHAN"),
                product_name_snapshot=(product.product_name if product else "缺失常用箱"),
                material_code_snapshot="K=A",
                normalized_material_code="K=A",
                layer_count=3,
                flute_type="B",
                report_length_mm=720,
                report_width_mm=520,
                crease_type="净",
                sheet_type="raw_board",
                component_type="whole",
                pieces_per_box=2,
                stock_yield_per_sheet=2,
                quantity=quantity,
                stocked_quantity=0,
            )

        semi = semi_item(semi_order)
        voided = semi_item(voided_order, quantity=12)
        missing_product = semi_item(missing_product_order, product=None, quantity=9)
        missing_customer = semi_item(
            missing_customer_order,
            item_customer_id=None,
            quantity=7,
        )
        dirty_cross_customer = semi_item(
            dirty_cross_customer_order,
            item_customer_id=other_customer.id,
            quantity=11,
        )
        finished = StockReplenishmentOrderItem(
            replenishment_order_id=finished_order.id,
            target_inventory_type="finished",
            product_id=liner.id,
            reference_product_id=liner.id,
            customer_id=customer.id,
            material_id=material.id,
            product_code_snapshot=liner.product_code,
            product_name_snapshot=liner.product_name,
            material_code_snapshot="K=A",
            normalized_material_code="K=A",
            layer_count=3,
            flute_type="B",
            report_length_mm=600,
            report_width_mm=400,
            sheet_type="raw_board",
            component_type="whole",
            pieces_per_box=1,
            # Deliberately not one: quantity is already finished-product units.
            stock_yield_per_sheet=5,
            quantity=18,
            stocked_quantity=0,
        )
        db.add_all(
            [
                semi,
                finished,
                voided,
                missing_product,
                missing_customer,
                dirty_cross_customer,
            ]
        )
        db.commit()

        ids = {
            "semi_order_id": semi_order.id,
            "semi_item_id": semi.id,
            "finished_order_id": finished_order.id,
            "finished_item_id": finished.id,
            "voided_order_id": voided_order.id,
            "voided_item_id": voided.id,
            "missing_product_order_id": missing_product_order.id,
            "missing_product_item_id": missing_product.id,
            "missing_customer_order_id": missing_customer_order.id,
            "missing_customer_item_id": missing_customer.id,
            "dirty_cross_customer_order_id": dirty_cross_customer_order.id,
            "dirty_cross_customer_item_id": dirty_cross_customer.id,
        }

    return {**production_print_app, **ids}


def _package_url(order_id: int, item_id: int | None = None) -> str:
    url = (
        f"/api/requisition/stock-replenishment/orders/{order_id}"
        "/production-print-package"
    )
    return f"{url}?item_ids={item_id}" if item_id is not None else url


def _stock_selection(package: dict, order_id: int) -> dict:
    card = package["cards"][0]
    return {
        "source_type": "stock_replenishment",
        "document_id": order_id,
        "source_identity": card["source_identity"],
        "selection_fingerprint": card["selection_fingerprint"],
        "task_versions": [],
    }


def test_stock_plan_cards_keep_frozen_inventory_units_and_half_a4_layout(
    stock_replenishment_print_app,
) -> None:
    from app.models.production import ProductionTask

    fixture = stock_replenishment_print_app
    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        before = client.get(
            _package_url(fixture["semi_order_id"], fixture["semi_item_id"])
        )
        finished = client.get(
            _package_url(fixture["finished_order_id"], fixture["finished_item_id"])
        )

    assert before.status_code == finished.status_code == 200
    semi_body = before.json()
    finished_body = finished.json()
    assert semi_body["source_type"] == finished_body["source_type"] == "stock_replenishment"
    assert semi_body["card_count"] == semi_body["page_count"] == 1
    assert semi_body["pages"][0]["top"] == semi_body["cards"][0]
    assert semi_body["pages"][0]["bottom"] is None

    semi_card = semi_body["cards"][0]
    assert semi_card["paper_phase"] == "planned"
    assert semi_card["paper_phase_label"] == "库存补库计划版"
    assert semi_card["output_unit"] == "张"
    assert semi_card["planned_finished_quantity"] == 40
    assert semi_card["requisition_quantity"] == 40
    assert semi_card["components"][0]["output_factor"] == 2
    assert semi_card["production_task_versions"] == []
    assert semi_card["components"][0]["production_task_id"] is None
    assert semi_card["components"][0]["production_task_version"] is None

    finished_card = finished_body["cards"][0]
    assert finished_card["output_unit"] == "只"
    assert finished_card["planned_finished_quantity"] == 18
    # Finished stock is planned in pieces; the material line still shows the
    # frozen board-sheet requirement derived from its output factor.
    assert finished_card["requisition_quantity"] == 4
    assert finished_card["components"][0]["required_piece_quantity"] == 18
    assert finished_card["components"][0]["output_factor"] == 5

    with fixture["session_factory"]() as db:
        # The projection must not create a fake sales-order production task.
        assert db.scalar(select(func.count()).select_from(ProductionTask)) == 4


def test_finished_stock_template_uses_output_unit_instead_of_hard_coded_sheets() -> None:
    detail_html = PRINT_PAGE.split("function detailHtml(card) {", 1)[1].split(
        "function receiptFactsHtml", 1
    )[0]

    assert "const quantityUnit = card.output_unit" in detail_html
    assert "`${card.production_label_units_per_bundle} ${quantityUnit}`" in detail_html
    assert '`${card.production_label_units_per_bundle} 张`' not in detail_html


def test_reported_stock_line_exposes_task_print_and_explicit_label_boundary(
    stock_replenishment_print_app,
) -> None:
    fixture = stock_replenishment_print_app
    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        response = client.get(
            "/api/requisition/reported-items",
            params={
                "source_type": "stock_replenishment",
                "document_number": "SR-P038-SEMI",
                "page_size": 20,
            },
        )

    assert response.status_code == 200, response.text
    rows = response.json()["items"]
    assert len(rows) == 1
    assert rows[0]["can_print_task"] is True
    assert rows[0]["can_print_label"] is False
    assert "库存补库" in rows[0]["label_print_block_reason"]
    assert "形成具体成品" in rows[0]["label_print_resolution"]


def test_stocked_and_remaining_facts_change_card_and_plan_fingerprints(
    stock_replenishment_print_app,
) -> None:
    from app.models.stock_replenishment import (
        StockReplenishmentOrder,
        StockReplenishmentOrderItem,
    )
    from app.services.requisition_production_print import (
        build_stock_replenishment_production_package,
    )

    fixture = stock_replenishment_print_app
    with fixture["session_factory"]() as db:
        order = db.get(StockReplenishmentOrder, fixture["semi_order_id"])
        item = db.get(StockReplenishmentOrderItem, fixture["semi_item_id"])
        assert order is not None and item is not None

        pending = build_stock_replenishment_production_package(db, order)
        item.stocked_quantity = 10
        order.status = "partially_stocked"
        db.flush()
        partial = build_stock_replenishment_production_package(db, order)
        item.stocked_quantity = 20
        db.flush()
        later_partial = build_stock_replenishment_production_package(db, order)
        item.stocked_quantity = 40
        order.status = "stocked"
        db.flush()
        stocked = build_stock_replenishment_production_package(db, order)

    cards = [
        pending["cards"][0],
        partial["cards"][0],
        later_partial["cards"][0],
        stocked["cards"][0],
    ]
    assert [card["received_quantity"] for card in cards] == [0, 10, 20, 40]
    assert [card["remaining_quantity"] for card in cards] == [40, 30, 20, 0]
    assert len({package["plan_fingerprint"] for package in [pending, partial, later_partial, stocked]}) == 4
    assert len({card["selection_fingerprint"] for card in cards}) == 4
    assert [package["status"] for package in [pending, partial, later_partial, stocked]] == [
        "confirmed",
        "partially_stocked",
        "partially_stocked",
        "stocked",
    ]


def test_voided_and_incomplete_stock_sources_fail_closed_with_reasons(
    stock_replenishment_print_app,
) -> None:
    fixture = stock_replenishment_print_app
    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        voided = client.get(
            _package_url(fixture["voided_order_id"], fixture["voided_item_id"])
        )
        missing_product = client.get(
            _package_url(
                fixture["missing_product_order_id"],
                fixture["missing_product_item_id"],
            )
        )
        missing_customer = client.get(
            _package_url(
                fixture["missing_customer_order_id"],
                fixture["missing_customer_item_id"],
            )
        )

    assert voided.status_code == 409
    assert any(word in voided.text for word in ("作废", "撤销", "正式确认"))
    assert missing_product.status_code == missing_customer.status_code == 409
    product_detail = missing_product.json()["detail"]
    customer_detail = missing_customer.json()["detail"]
    assert product_detail["code"] == customer_detail["code"] == (
        "stock_replenishment_production_print_review_required"
    )
    assert any(
        "常用箱" in reason or "产品" in reason
        for reason in product_detail["items"][0]["reasons"]
    )
    assert any(
        "客户" in reason for reason in customer_detail["items"][0]["reasons"]
    )
    assert product_detail["items"][0]["resolution"]
    assert customer_detail["items"][0]["resolution"]


def test_stock_batch_accepts_empty_task_versions_and_rechecks_source_cas(
    stock_replenishment_print_app,
) -> None:
    from app.models.production import ProductionTask
    from app.models.stock_replenishment import (
        StockReplenishmentOrder,
        StockReplenishmentOrderItem,
    )

    fixture = stock_replenishment_print_app
    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        package_response = client.get(
            _package_url(fixture["semi_order_id"], fixture["semi_item_id"])
        )
        assert package_response.status_code == 200, package_response.text
        selection = _stock_selection(package_response.json(), fixture["semi_order_id"])
        payload = {
            "idempotency_key": "p0-38-stock-plan-batch-001",
            "confirmed": True,
            "items": [selection],
        }
        prepared = client.post(
            "/api/requisition/production-print-batches/prepare",
            json=payload,
        )
        assert prepared.status_code == 200, prepared.text
        prepared_body = prepared.json()
        assert prepared_body["replayed"] is False
        assert prepared_body["cards"][0]["source_identity"] == selection["source_identity"]
        assert prepared_body["cards"][0]["production_task_versions"] == []

        replay = client.post(
            "/api/requisition/production-print-batches/prepare",
            json=payload,
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["replayed"] is True

        fake_task = {
            **payload,
            "idempotency_key": "p0-38-stock-plan-batch-fake-task",
            "items": [{**selection, "task_versions": [{"task_id": 1, "version": 1}]}],
        }
        rejected_fake_task = client.post(
            "/api/requisition/production-print-batches/prepare",
            json=fake_task,
        )
        assert rejected_fake_task.status_code == 422

        with fixture["session_factory"]() as db:
            item = db.get(StockReplenishmentOrderItem, fixture["semi_item_id"])
            order = db.get(StockReplenishmentOrder, fixture["semi_order_id"])
            assert item is not None and order is not None
            item.stocked_quantity = 1
            order.status = "partially_stocked"
            db.commit()

        stale = client.get(
            f"/api/requisition/production-print-batches/{prepared_body['batch_id']}"
        )
        assert stale.status_code == 409
        assert "变化" in stale.text

    with fixture["session_factory"]() as db:
        assert db.scalar(select(func.count()).select_from(ProductionTask)) == 4


def test_stock_print_package_and_batch_remain_customer_scoped(
    stock_replenishment_print_app,
) -> None:
    fixture = stock_replenishment_print_app
    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-admin")
        source = client.get(
            _package_url(fixture["semi_order_id"], fixture["semi_item_id"])
        )
        assert source.status_code == 200, source.text
        selection = _stock_selection(source.json(), fixture["semi_order_id"])
        client.post("/api/auth/logout")

        _login(client, "p132a2-sales")
        blocked_package = client.get(
            _package_url(fixture["semi_order_id"], fixture["semi_item_id"])
        )
        blocked_batch = client.post(
            "/api/requisition/production-print-batches/prepare",
            json={
                "idempotency_key": "p0-38-cross-customer-stock-plan",
                "confirmed": True,
                "items": [selection],
            },
        )

    assert blocked_package.status_code == 403
    assert blocked_batch.status_code == 403


def test_cross_customer_reference_product_dirty_row_fails_closed_for_scoped_user(
    stock_replenishment_print_app,
) -> None:
    from app.models.stock_replenishment import StockReplenishmentOrder
    from app.services.requisition_production_print import (
        build_stock_replenishment_production_package,
    )

    fixture = stock_replenishment_print_app
    with fixture["session_factory"]() as db:
        order = db.get(
            StockReplenishmentOrder,
            fixture["dirty_cross_customer_order_id"],
        )
        assert order is not None
        package = build_stock_replenishment_production_package(
            db,
            order,
            selected_item_ids={fixture["dirty_cross_customer_item_id"]},
        )
        card = package["cards"][0]
        assert card["selection_eligible"] is False
        assert any("客户不一致" in reason for reason in card["selection_block_reasons"])
        selection = _stock_selection(
            package,
            fixture["dirty_cross_customer_order_id"],
        )

    with TestClient(fixture["app"]) as client:
        _login(client, "p132a2-sales")
        blocked_package = client.get(
            _package_url(
                fixture["dirty_cross_customer_order_id"],
                fixture["dirty_cross_customer_item_id"],
            )
        )
        blocked_batch = client.post(
            "/api/requisition/production-print-batches/prepare",
            json={
                "idempotency_key": "p0-38-dirty-cross-customer-stock-plan",
                "confirmed": True,
                "items": [selection],
            },
        )

    assert blocked_package.status_code == 403, blocked_package.text
    assert blocked_batch.status_code == 403, blocked_batch.text
    assert "无客户补库单访问权限" in blocked_package.text
    assert "无客户补库单访问权限" in blocked_batch.text
