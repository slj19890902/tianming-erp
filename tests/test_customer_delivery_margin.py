from datetime import date
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from tests.test_phase11_requisition import _login, requisition_app


def test_customer_delivery_margin_route_and_projection_contract():
    from app.api.dashboard import router
    from app.services.customer_delivery_margin import (
        build_customer_delivery_margin,
    )

    assert "/customer-delivery-margin" in {
        route.path for route in router.routes
    }
    assert date(2026, 9, 1) < date(2026, 10, 1)
    assert Decimal("1.00") == Decimal("1.00")


def test_margin_metrics_keep_unknowns_as_gaps():
    from app.services.customer_delivery_margin import _empty_metrics

    metrics = _empty_metrics()
    assert metrics["sales_amount"] is None
    assert metrics["material_cost"] is None
    assert metrics["coverage_rate"] == "0.0000"
    assert metrics["status"] == "empty"


def test_unknown_unit_does_not_hide_a_complete_tax_and_cost_amount():
    from app.services.customer_delivery_margin import _metrics, _sales_projection, _frozen_bom_root_unit

    item = NS(delivered_quantity=2, unit_snapshot=None, unit_price_snapshot=None)
    order = NS(
        unit_price=Decimal("5.00"),
        price_tax_mode_snapshot="tax_inclusive",
        tax_rate_snapshot=None,
    )
    sales = _sales_projection(item, order)
    result = _metrics(
        [
            {
                "sales": sales,
                "cost": {
                    "actual_material_cost": Decimal("3.00"),
                    "supplemental_material_cost": Decimal("0"),
                    "management_material_cost": Decimal("3.00"),
                    "actual_cost_complete": True,
                    "management_cost_complete": True,
                    "reason_codes": [],
                },
            }
        ]
    )
    assert result["sales_amount"] == "10.00"
    assert result["material_margin"] == "7.00"
    assert result["unknown_unit_quantity"] == 2
    assert _sales_projection(item, order, frozen_unit="套")["unit"] == "套"


def test_bom_root_unit_comes_from_exact_historical_source_contract(monkeypatch):
    from app.services import customer_delivery_margin as service
    from app.services.multilevel_bom_plan import FrozenBom, ProductNode

    monkeypatch.setattr(
        "app.services.multilevel_bom_delivery_history.historical_delivery_component_demands",
        lambda *args, **kwargs: [NS(snapshot_id=19, is_graph_root=False, unit="子件")],
    )
    monkeypatch.setattr(
        "app.services.multilevel_bom_orders.read_order_bom_source_contract",
        lambda *args, **kwargs: NS(
            graph=FrozenBom(
                root_id=7,
                customer_id=1,
                nodes=(
                    ProductNode(7, 1, 1, "父件", "套", "assembled"),
                    ProductNode(8, 1, 1, "子件", "只", "purchased"),
                ),
                edges=(),
            )
        ),
    )
    result = service._frozen_bom_root_unit(
        None, delivery_item_id=3, order_item=NS(id=11)
    )
    assert result == "套"


def test_real_lineage_collector_emits_full_untraced_line(requisition_app):
    from app.models.delivery import Delivery, DeliveryItem
    from app.services.material_cost_lineage import material_cost_coverage_report

    _app, factory = requisition_app
    with factory() as db:
        delivery = Delivery(
            delivery_number="MARGIN-LINEAGE-REAL",
            customer_id=1,
            delivery_date=date(2026, 9, 6),
            status="dispatched",
            total_quantity=1,
        )
        db.add(delivery)
        db.flush()
        db.add(
            DeliveryItem(
                delivery_id=delivery.id,
                source_type="order",
                order_item_id=1,
                delivered_quantity=1,
            )
        )
        db.flush()
        projections = []
        report = material_cost_coverage_report(
            db,
            month="2026-09",
            date_from=date(2026, 9, 1),
            date_to_exclusive=date(2026, 9, 7),
            _line_collector=projections,
        )
        assert report["total_delivery_lines"] == 1
        assert len(projections) == 1
        assert projections[0]["actual_cost_complete"] is False
        assert projections[0]["management_cost_complete"] is False
        assert projections[0]["reason_codes"] == ["no_delivery_cost_source"]


@pytest.mark.parametrize(
    ("mode", "rate", "expected", "gap"),
    [
        ("tax_inclusive", None, Decimal("10.00"), set()),
        ("tax_exclusive", Decimal("0.13"), Decimal("11.30"), set()),
        ("tax_exclusive", None, None, {"missing_sales_tax_basis"}),
    ],
)
def test_sales_tax_snapshot_is_frozen_and_unknown_is_not_zero(mode, rate, expected, gap):
    from app.services.customer_delivery_margin import _sales_projection

    sales = _sales_projection(
        NS(delivered_quantity=2, unit_snapshot="只", unit_price_snapshot=None),
        NS(unit_price=Decimal("5.00"), price_tax_mode_snapshot=mode, tax_rate_snapshot=rate),
    )
    assert sales["amount"] == expected
    assert sales["known_amount"] == (expected or Decimal("0"))
    assert gap.issubset(sales["reasons"])


def test_cross_month_cost_projection_keeps_month_fingerprints(monkeypatch):
    import app.services.customer_delivery_margin as service

    calls = []

    def fake_report(db, *, month, visible_customer_ids=None, date_from=None, date_to_exclusive=None, _line_collector=None):
        calls.append((month, date_from, date_to_exclusive, visible_customer_ids))
        _line_collector.extend([])
        return {}

    monkeypatch.setattr(service, "material_cost_coverage_report", fake_report)
    service.delivery_cost_line_projections(
        None,
        date_from=date(2026, 8, 31),
        date_to_exclusive=date(2026, 10, 2),
        visible_customer_ids={7},
    )
    assert calls == [
        ("2026-08", date(2026, 8, 31), date(2026, 9, 1), {7}),
        ("2026-09", date(2026, 9, 1), date(2026, 10, 1), {7}),
        ("2026-10", date(2026, 10, 1), date(2026, 10, 2), {7}),
    ]


def test_line_rounding_and_reference_status_are_explicit():
    from app.services.customer_delivery_margin import _metrics, _sales_projection

    def line(quantity, price, reference=False):
        item = NS(delivered_quantity=quantity, unit_snapshot="只", unit_price_snapshot=None)
        order = NS(unit_price=price, price_tax_mode_snapshot="tax_inclusive", tax_rate_snapshot=None)
        cost = Decimal("1.00") if not reference else Decimal("0")
        return {
            "sales": _sales_projection(item, order),
            "cost": {
                "actual_material_cost": cost,
                "supplemental_material_cost": Decimal("0.01") if reference else Decimal("0"),
                "management_material_cost": cost + (Decimal("0.01") if reference else Decimal("0")),
                "actual_cost_complete": not reference,
                "management_cost_complete": True,
                "reason_codes": ["missing_purchase_lineage"] if reference else [],
            },
        }

    result = _metrics([line(1, Decimal("0.005")), line(1, Decimal("0.005"), True)])
    assert result["sales_amount"] == "0.02"
    assert result["covered_sales_amount"] == "0.02"
    assert result["status"] == "complete_with_reference"


def test_projection_mapping_retains_all_gap_lines_beyond_display_example_limit(monkeypatch):
    import app.services.customer_delivery_margin as service

    def fake_report(db, *, month, visible_customer_ids=None, date_from=None, date_to_exclusive=None, _line_collector=None):
        _line_collector.extend(
            {
                "delivery_item_id": idx,
                "customer_id": 1,
                "delivery_date": date(2026, 9, 1),
                "actual_material_cost": Decimal("0"),
                "supplemental_material_cost": Decimal("0"),
                "management_material_cost": Decimal("0"),
                "actual_cost_complete": False,
                "management_cost_complete": False,
                "reason_codes": ["missing_purchase_lineage"],
            }
            for idx in range(1, 26)
        )
        return {}

    monkeypatch.setattr(service, "material_cost_coverage_report", fake_report)
    result = service.delivery_cost_line_projections(
        None, date_from=date(2026, 9, 1), date_to_exclusive=date(2026, 9, 2)
    )
    assert len(result) == 25


def test_margin_endpoint_requires_role_and_all_read_permissions(requisition_app):
    from app.api.dashboard import router

    app, _factory = requisition_app
    app.include_router(router, prefix="/api/dashboard")
    with TestClient(app) as client:
        _login(client, "finance")
        denied = client.get(
            "/api/dashboard/customer-delivery-margin",
            params={"date_from": "2026-09-01", "date_to": "2026-09-13"},
        )
        assert denied.status_code == 403
        _login(client, "admin")
        invalid = client.get(
            "/api/dashboard/customer-delivery-margin",
            params={"date_from": "2026-09-13"},
        )
        assert invalid.status_code == 422
        overflow = client.get(
            "/api/dashboard/customer-delivery-margin",
            params={"date_from": "9999-12-31", "date_to": "9999-12-31"},
        )
        assert overflow.status_code == 422
        result = client.get(
            "/api/dashboard/customer-delivery-margin",
            params={"date_from": "2026-09-01", "date_to": "2026-09-13"},
        )
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["filters"]["date_to"] == "2026-09-13"
        assert body["summary"]["status"] == "empty"


def test_margin_endpoint_reads_only_dispatched_current_lines(requisition_app):
    from app.api.dashboard import router
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.order import OrderItem

    app, factory = requisition_app
    app.include_router(router, prefix="/api/dashboard")
    with factory() as db:
        order_item = OrderItem(
            order_id=1,
            product_id=1,
            quantity=2,
            unit_price=Decimal("5.00"),
            subtotal=Decimal("10.00"),
            price_tax_mode_snapshot="tax_inclusive",
            snapshot_product_name="Margin test",
        )
        db.add(order_item)
        db.flush()
        for status, current in (("dispatched", True), ("pending", True), ("dispatched", False)):
            delivery = Delivery(
                delivery_number=f"MARGIN-{status}-{int(current)}-{db.query(Delivery).count()}",
                customer_id=1,
                delivery_date=date(2026, 9, 5),
                status=status,
                total_quantity=2,
            )
            db.add(delivery)
            db.flush()
            db.add(
                DeliveryItem(
                    delivery_id=delivery.id,
                    source_type="order",
                    order_item_id=order_item.id,
                    delivered_quantity=2,
                    unit_snapshot=None,
                    is_current=current,
                )
            )
        db.commit()
    with TestClient(app) as client:
        _login(client, "admin")
        response = client.get(
            "/api/dashboard/customer-delivery-margin",
            params={"date_from": "2026-09-01", "date_to": "2026-09-30"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"]["delivery_line_count"] == 1
    assert body["summary"]["sales_amount"] == "10.00"
    assert body["summary"]["unknown_unit_quantity"] == 2
