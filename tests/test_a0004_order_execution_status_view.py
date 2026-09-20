from pathlib import Path
from types import SimpleNamespace

from app.services.order_document_trace import _execution_summary


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _event(key, stage, source_type, source_id, quantity, document_number):
    return {
        "key": key,
        "stage": stage,
        "source_type": source_type,
        "source_id": source_id,
        "document_number": document_number,
        "quantity": quantity,
        "is_effective": True,
    }


def test_execution_summary_keeps_sheet_and_piece_quantities_separate():
    events = [
        _event("order:1", "order", "sales_order", 1, 500, "TM-001"),
        _event("incoming:11", "incoming", "incoming_receipt", 11, 300, "IN-001"),
        _event("incoming:12", "incoming", "incoming_receipt", 12, 300, "IN-002"),
        _event("inventory:21", "inventory", "inventory_lot", 21, None, "LOT-FIN"),
        _event("delivery:31", "delivery", "delivery_dispatch", 31, 300, "TH-001"),
    ]
    inventory = [
        {
            "inventory_type": "finished",
            "quantity_available": 200,
            "quantity_reserved": 0,
            "quantity_consumed": 300,
        },
        {
            "inventory_type": "semi_finished",
            "quantity_available": 80,
            "quantity_reserved": 20,
            "quantity_consumed": 0,
        },
    ]

    summary = _execution_summary(
        events=events,
        current_inventory=inventory,
        item=SimpleNamespace(quantity=500, delivered_quantity=300),
        permissions={"incoming.view", "warehouse.view", "deliveries.view"},
    )
    metrics = {row["key"]: row for row in summary["metrics"]}

    assert (metrics["ordered"]["value"], metrics["ordered"]["unit"]) == (500, "只")
    assert (metrics["received_sheets"]["value"], metrics["received_sheets"]["unit"]) == (600, "张")
    assert metrics["finished_accounted"]["value"] == 500
    assert metrics["finished_available"]["value"] == 200
    assert metrics["delivered"]["value"] == 300
    assert summary["remaining_quantity"] == 200
    assert metrics["deliverable"]["value"] == 200
    assert metrics["reserve_sheets"]["value"] == 100
    assert metrics["deliverable"]["source"]["source_id"] == 21
    assert metrics["received_sheets"]["source"]["source_id"] == 12


def test_execution_summary_marks_zero_deliverable_and_permission_limits():
    events = [_event("order:1", "order", "sales_order", 1, 500, "TM-001")]
    item = SimpleNamespace(quantity=500, delivered_quantity=0)

    visible = _execution_summary(
        events=events,
        current_inventory=[],
        item=item,
        permissions={"incoming.view", "warehouse.view"},
    )
    visible_metrics = {row["key"]: row for row in visible["metrics"]}
    assert visible_metrics["deliverable"]["status"] == "blocked"
    assert visible_metrics["deliverable"]["status_label"] == "暂无可送"

    restricted = _execution_summary(
        events=events,
        current_inventory=[],
        item=item,
        permissions=set(),
    )
    restricted_metrics = {row["key"]: row for row in restricted["metrics"]}
    assert restricted_metrics["received_sheets"]["value"] is None
    assert restricted_metrics["finished_available"]["value"] is None
    assert restricted_metrics["reserve_sheets"]["status"] == "restricted"


def test_trace_frontend_renders_compact_summary_and_exact_source_navigation():
    assert 'class="order-execution-summary"' in INDEX
    assert "orderTrace.execution_summary.metrics" in INDEX
    assert "traceMetricValue(metric)" in INDEX
    assert "openTraceMetricSource(metric)" in INDEX
    assert "row.key === source.event_key" in INDEX
    for label in ("执行数量总览", "来源 {{ metric.source.document_number }}"):
        assert label in INDEX
