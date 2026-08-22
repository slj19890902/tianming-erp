from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.services.production_station_routing import (
    PRODUCTION_STATION_ROUTING_RULE_VERSION,
    production_station_memberships,
)
from scripts.audit.p0_15_incomplete_order_chain import _render_markdown
from tests.test_p0_15_incomplete_order_chain import (
    add_order,
    add_task,
    add_traced_receipt,
    build_p0_15_database,
    finding_codes,
    run_audit,
)


def test_station_contract_uses_only_explicit_process_facts() -> None:
    assert production_station_memberships(
        print_content_snapshot="无印刷",
        box_style="A1",
        die_cut_required=False,
    ) == {"printing"}
    assert production_station_memberships(
        print_content_snapshot="双色印刷",
        box_style="衬板",
        die_cut_required=False,
    ) == {"printing"}
    assert production_station_memberships(
        print_content_snapshot="无印刷",
        box_style="衬板",
        die_cut_required=True,
    ) == {"die_cut"}
    assert production_station_memberships(
        print_content_snapshot="黑绿印刷内容",
        box_style="衬板",
        die_cut_required=True,
    ) == {"printing", "die_cut"}
    assert production_station_memberships(
        print_content_snapshot="无印刷",
        box_style="衬板",
        die_cut_required=False,
    ) == frozenset()

    with pytest.raises(ValueError, match="explicit boolean"):
        production_station_memberships(
            print_content_snapshot="无印刷",
            box_style="衬板",
            die_cut_required="模切",  # type: ignore[arg-type]
        )


def test_readonly_audit_reports_real_station_coverage(tmp_path: Path) -> None:
    engine = build_p0_15_database(tmp_path / "p1-84-routing.sqlite3")
    try:
        with Session(engine) as db:
            facts = (
                ("PRINT-STYLE", "A1", "normal", "无印刷"),
                ("PRINT-CONTENT", "衬板", "normal", "单色印刷"),
                ("DIE", "衬板", "die_cut", "无印刷"),
                ("BOTH", "衬板", "die_cut", "双色印刷"),
                ("NONE", "衬板", "normal", "无印刷"),
            )
            for token, box_style, box_category, print_content in facts:
                _customer, product, _order, item = add_order(db, token)
                product.box_style = box_style
                product.box_category = box_category
                task = add_task(db, item)
                task.print_content_snapshot = print_content
            db.commit()

            report = run_audit(db, focus_terms=("ANON-PRODUCT-DIE",))

        assert report["coverage"]["workstation_membership"] == {
            "status": "evaluated",
            "rule_version": PRODUCTION_STATION_ROUTING_RULE_VERSION,
            "eligible_task_count": 5,
            "station_task_counts": {"printing": 3, "die_cut": 2},
            "dual_route_task_count": 1,
            "unrouted_task_count": 1,
        }
        assert len(report["workstation_routes"]) == 5
        assert report["summary"]["focus_route_count"] == 1
        assert sum(
            row["focus_match"] is not None for row in report["workstation_routes"]
        ) == 1
        assert {
            tuple(row["stations"]) for row in report["workstation_routes"]
        } == {(), ("printing",), ("die_cut",), ("die_cut", "printing")}
        serialized_routes = str(report["workstation_routes"])
        assert "ANON-ORDER" not in serialized_routes
        assert "ANON-PRODUCT" not in serialized_routes
        assert report["summary"]["scan_complete"] is True

        report["source"] = {
            "label": "anonymous-test-copy",
            "revision": "test-revision",
            "database_sha256_before": "a" * 64,
            "database_sha256_after": "a" * 64,
            "database_unchanged": True,
            "quick_check": "ok",
            "foreign_key_errors": 0,
            "elapsed_ms": 1,
        }
        markdown = _render_markdown(report)
        assert "### 聚焦对象工位证据" in markdown
        assert "PRODUCTION_TASK-" in markdown
        assert "ANON-PRODUCT-DIE" not in markdown
    finally:
        engine.dispose()


def test_five_anomaly_fact_shapes_are_covered_without_identifier_rules(
    tmp_path: Path,
) -> None:
    engine = build_p0_15_database(tmp_path / "p1-84-five-shapes.sqlite3")
    try:
        with Session(engine) as db:
            _customer, _product, order, missing_task_item = add_order(
                db, "MISSING-TASK"
            )
            add_traced_receipt(db, "MISSING-TASK", order, missing_task_item)

            add_order(db, "TRACE-GAP", material_status="received")

            _customer, color_product, _order, color_item = add_order(
                db, "COLOR-FACT"
            )
            color_product.printing_plate_mode = "no_plate"
            color_product.print_content = "双色印刷"
            color_product.printing_colors = "黑＋绿"
            color_task = add_task(db, color_item)
            color_task.print_content_snapshot = "双色印刷"
            db.commit()

            report = run_audit(db)

        codes = finding_codes(report)
        assert "P015_INCOMING_WITHOUT_PRODUCTION_TASK" in codes
        assert "P015_LEGACY_RECEIVED_STATUS_TRACE_GAP" in codes
        assert "P015_PENDING_PRODUCTION_WITHOUT_INPUT_FACT" in codes
        assert "P015_COMMON_BOX_PRINT_COLOR_INVALID" not in codes

        wrong_die_route = production_station_memberships(
            print_content_snapshot="双色印刷",
            box_style="衬板",
            die_cut_required=False,
        )
        assert wrong_die_route == {"printing"}
        for _anonymous_case in ("REQUIRED-DIE-A", "REQUIRED-DIE-B"):
            assert production_station_memberships(
                print_content_snapshot="无印刷",
                box_style="衬板",
                die_cut_required=True,
            ) == {"die_cut"}
    finally:
        engine.dispose()
