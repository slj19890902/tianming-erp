"""Authenticated paper/receipt HTTP UAT; writes only a fresh explicit replica.

python -B -m scripts.drawing_v2_paper_flow_uat
This uses existing legacy_unset supplier-order compatibility, with real receipt
and price guards. It does not certify frozen-purpose procurement or factory use.
"""
from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal
import base64
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import shutil
import sqlite3
import traceback


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', default='20260922', help='New isolated run suffix; existing targets are never overwritten')
    run_id = parser.parse_args().run_id
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,48}', run_id):
        parser.error('run-id must be 1-48 letters, digits, underscores or hyphens')
    root = Path(__file__).resolve().parents[1]
    uat = root / ".uat"
    source = uat / "delivery_reference_20260922.sqlite3"
    source_files = uat / "delivery_reference_files_20260922"
    target = uat / f"paper_flow_{run_id}.sqlite3"
    storage = uat / f"paper_flow_files_{run_id}"
    output = root / "output/drawing_v2_uat_20260921" / f"paper_flow_{run_id}"
    runtime = uat / f"paper_flow_runtime_{run_id}"
    for path in (target, storage, output, runtime):
        if path.exists() or path.is_symlink():
            raise RuntimeError(f"Refuse overwriting existing UAT target: {path}")
    if not source.is_file() or not source_files.is_dir() or uat.is_symlink():
        raise RuntimeError("Expected isolated delivery replica/storage is unavailable")
    assert target.resolve().parent == uat.resolve() and target.resolve() != source.resolve()
    before = {"database": digest(source), "files": {
        str(path.relative_to(source_files)): digest(path)
        for path in source_files.rglob("*") if path.is_file()}}
    evidence = {"started_utc": datetime.now(timezone.utc).isoformat(), "source": str(source),
        "database": str(target), "storage": str(storage), "source_hashes_before": before,
        "status": "running", "cases": [], "http_checks": [],
        "boundary": "Only fresh isolated copy; no server startup, background jobs, browser or factory write",
        "receipt_flow": "legacy_unset supplier-order compatibility; actual receive route and ReceiptPriceGuardSession",
        "quantity_basis": "Each new UAT order is 2 pieces from 2 sheets, one sheet per piece; not factory demand",
        "site_acceptance": False, "browser_verified": False}
    output.mkdir(parents=True)
    runtime.mkdir()
    engine = None
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
            original.row_factory = sqlite3.Row
            product_rows = [dict(row) for row in original.execute("SELECT * FROM products WHERE id IN (183,97,300,3323) ORDER BY id")]
            historical_tasks = [dict(row) for row in original.execute("SELECT * FROM production_tasks WHERE id IN (222,466) ORDER BY id")]
            with sqlite3.connect(target) as destination:
                original.backup(destination)
        shutil.copytree(source_files, storage)
        os.environ.update(ERP_DATABASE_PATH=str(target), ERP_FILE_STORAGE_DIR=str(storage),
            ERP_BACKUP_DIR=str(runtime / "backups"), ERP_LOG_DIR=str(runtime / "logs"),
            ERP_SECRET_KEY_FILE=str(runtime / "session.key"), ERP_SECRET_KEY=secrets.token_urlsafe(40),
            ERP_ENVIRONMENT="test", ERP_UAT_ROOT="", ERP_DRAWING_V2_ENABLED="1", ERP_WORKERS="1")
        for name, path in {
            "ERP_UPLOAD_TEMP_DIR": "uploads", "ERP_INVOICE_EXPORT_DIR": "invoices", "ERP_INVOICE_ATTACHMENT_DIR": "invoice_attachments",
            "ERP_PDF_TRAINING_DIR": "pdf_training", "ERP_TWIN_LAYOUT_RUNTIME_PATH": "layout/runtime.json",
            "ERP_TWIN_LAYOUT_DRAFT_PATH": "layout/draft.json", "ERP_TWIN_LAYOUT_BACKUP_DIR": "layout/backups",
            "ERP_LEGACY_UPLOAD_DIR": "legacy_uploads", "ERP_DELIVERY_PRINT_SETTINGS_PATH": "print_settings.json",
            "ERP_FACTORY_TWIN_DATABASE_PATH": "twin.sqlite3",
        }.items():
            os.environ[name] = str(runtime / path)

        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from sqlalchemy import select, func
        from sqlalchemy.orm import sessionmaker
        from app.api import auth, incoming, requisition, mobile_erp, drawing_design
        from app.api.deps import get_db
        from app.core.config import load_settings
        from app.core.database import create_sqlite_engine
        from app.core.receipt_price_guard import ReceiptPriceGuardSession
        from app.core.security import hash_password
        from app.models.access_control import UserCustomerScope, UserPermissionOverride
        from app.models.customer import Customer
        from app.models.drawing_design import DrawingRelease, ProductionTaskDrawing
        from app.models.incoming_receipt import IncomingReceiptItem
        from app.models.material import Material
        from app.models.order import Order, OrderItem
        from app.models.product import Product
        from app.models.production import ProductionTask
        from app.models.supplier_requisition_order import SupplierRequisitionOrder, SupplierRequisitionOrderItem
        from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact
        from app.models.user import User
        from app.services.production_workflow import create_or_refresh_production_task

        assert load_settings().database_path == target.resolve()
        engine = create_sqlite_engine(target)
        factory = sessionmaker(bind=engine, class_=ReceiptPriceGuardSession, expire_on_commit=False)
        app = FastAPI()
        app.state.erp_settings = load_settings()
        for router, prefix in [(auth.router, "/api/auth"), (incoming.router, "/api/incoming"),
                (requisition.router, "/api/requisition"), (mobile_erp.router, "/api/mobile"),
                (drawing_design.router, "/api/products")]:
            app.include_router(router, prefix=prefix)
        def isolated_db():
            with factory() as db:
                yield db
        app.dependency_overrides[get_db] = isolated_db

        def record_response(label, response, expected=200):
            entry = {"name": label, "status": response.status_code, "expected": expected}
            if response.status_code != expected:
                entry["response"] = response.text[:6000]
            evidence["http_checks"].append(entry)
            assert response.status_code == expected, f"{label}: {response.status_code} {response.text[:3000]}"
            return response.json() if response.headers.get("content-type", "").startswith("application/json") else response.content

        password = "DrawingPaperUat20260922!"  # Created only in the guarded isolated copy.
        with factory() as db:
            admin = User(username="uat-drawing-paper-admin", password_hash=hash_password(password), role="admin",
                real_name="图纸纸单隔离UAT管理员", display_name="图纸纸单隔离UAT", must_change_password=False)
            restricted = User(username="uat-drawing-paper-scoped", password_hash=hash_password(password), role="sales",
                real_name="图纸纸单范围测试", customer_access_mode="selected", must_change_password=False)
            db.add_all([admin, restricted]); db.flush()
            db.add(UserCustomerScope(user_id=restricted.id, customer_id=5, assigned_by=admin.id))
            for permission in ("incoming.execute", "incoming.view", "requisition.view", "production.printing.view"):
                db.add(UserPermissionOverride(user_id=restricted.id, permission_code=permission, is_allowed=True))
            db.commit()
            admin_id = admin.id

        with TestClient(app, raise_server_exceptions=False) as client:
            record_response("anonymous_receipt_route_denied", client.put("/api/incoming/receive/so999999",
                json={"received_quantity": 2}), 401)
            record_response("real_admin_login", client.post("/api/auth/login", json={"username": "uat-drawing-paper-admin", "password": password}))
            # Previous isolated verification deliberately changed one wing to
            # 35.5. Restore the user-confirmed reference in this new copy only.
            current = record_response("97_read_draft", client.get("/api/products/97/managed-drawing"))
            payload = {key: value for key, value in current["draft"].items() if key in drawing_design.DesignWrite.model_fields}
            payload.update(expected_product_version=current["draft"]["source_product_version"],
                expected_design_version=current["draft"]["version"], idempotency_key="paper-uat-97-confirmed-save")
            payload["parameters"]["left_wing_mm"] = "40"
            saved = record_response("97_restore_confirmed_shape", client.put("/api/products/97/managed-drawing", json=payload))
            record_response("97_publish_confirmed_shape", client.post("/api/products/97/managed-drawing/releases", json={
                "expected_product_version": saved["draft"]["source_product_version"], "expected_design_version": saved["draft"]["version"],
                "idempotency_key": "paper-uat-97-confirmed-publish"}))

            fixtures = []
            with factory() as db:
                for pid in (183, 97, 300, 3323):
                    product = db.get(Product, pid)
                    material = db.get(Material, product.material_id)
                    customer = db.get(Customer, product.customer_id)
                    assert material.quote_price and material.quote_price > 0 and material.supplier_name
                    size = [product.length_mm, product.width_mm] + ([product.height_mm] if product.height_mm is not None else [])
                    spec = "×".join(format(value.normalize(), "f") for value in size) + "mm"
                    order = Order(order_number=f"UAT-PAPER-20260922-{pid}", customer_id=product.customer_id,
                        order_date=date(2026, 9, 22), status="pending_production", total_amount=0,
                        remark="隔离图纸链验证，不是工厂订单", created_by=admin_id)
                    db.add(order); db.flush()
                    item = OrderItem(order_id=order.id, product_id=pid, quantity=2, unit_price=0, subtotal=0,
                        item_order_number=f"UAT-PAPER-20260922-{pid}-001", item_sequence=1,
                        snapshot_product_code=product.product_code, snapshot_product_name=product.product_name,
                        snapshot_spec=spec, snapshot_material=material.code, material_id=material.id,
                        layer_count=product.layer_count, flute_type=product.flute_type,
                        snapshot_report_length_mm=product.report_length_mm, snapshot_report_width_mm=product.report_width_mm,
                        snapshot_crease_type=product.crease_type, snapshot_crease_left_mm=product.crease_left_mm,
                        snapshot_crease_middle_mm=product.crease_middle_mm, snapshot_crease_right_mm=product.crease_right_mm,
                        snapshot_splice_mode="single", snapshot_pieces_per_box=1, snapshot_flap_mm=product.flap_mm,
                        material_status="pending", requisition_status="已报料", requisition_qty=2,
                        special_process="一开一", cardboard_len=product.report_length_mm, cardboard_width=product.report_width_mm)
                    db.add(item); db.flush()
                    supplier = SupplierRequisitionOrder(order_number=f"UAT-SRO-PAPER-20260922-{pid}",
                        supplier_name=material.supplier_name, material_id=material.id, layer_count=product.layer_count,
                        flute_type=product.flute_type, report_length_mm=product.report_length_mm,
                        report_width_mm=product.report_width_mm, crease_type=product.crease_type,
                        crease_left_mm=product.crease_left_mm, crease_middle_mm=product.crease_middle_mm,
                        crease_right_mm=product.crease_right_mm, cutting_mode="一开一", pieces_per_box=1,
                        required_piece_qty=2, total_quantity=2, requisition_qty=2, stock_deduction_qty=0,
                        status="confirmed", created_by=admin_id, remark="仅隔离UAT 2张，沿用真实材质报价验证实收冻结价")
                    db.add(supplier); db.flush()
                    line = SupplierRequisitionOrderItem(supplier_order_id=supplier.id, order_item_id=item.id,
                        source_key=f"order_item:{item.id}", product_id=pid, material_id=material.id,
                        material_code_snapshot=material.code, supplier_name_snapshot=material.supplier_name,
                        layer_count_snapshot=product.layer_count, flute_type_snapshot=product.flute_type,
                        order_number=item.item_order_number, product_code=product.product_code, product_name=product.product_name,
                        report_length_mm=product.report_length_mm, report_width_mm=product.report_width_mm,
                        quantity=2, requisition_qty=2, required_piece_qty=2, stock_deduction_qty=0,
                        pieces_per_box=1, cutting_mode="一开一", customer_name=customer.name)
                    db.add(line); db.flush()
                    item.supplier_order_number = supplier.order_number
                    task = create_or_refresh_production_task(db, item.id, source_is_new=True)
                    assert task.status == "waiting_material" and task.planned_quantity == 0
                    binding = db.get(ProductionTaskDrawing, task.id)
                    assert binding is not None, f"New task {task.id} did not bind explicit {spec}"
                    release = db.get(DrawingRelease, binding.release_id)
                    fixtures.append({"product_id": pid, "order_item_id": item.id, "supplier_order_id": supplier.id,
                        "supplier_item_id": line.id, "task_id": task.id, "release_id": release.id,
                        "pdf_sha256": release.pdf_sha256, "material_id": material.id, "quote_price": str(material.quote_price),
                        "snapshot_spec": spec, "customer_id": product.customer_id})
                db.commit()

            for fixture in fixtures:
                pid = fixture["product_id"]
                case = dict(fixture, status="running")
                evidence["cases"].append(case)
                try:
                    pending = record_response(f"{pid}_pending_paper", client.get(
                        f"/api/requisition/supplier-orders/{fixture['supplier_order_id']}/production-print-package"))
                    (output / f"{pid}_pending_paper.json").write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")
                    drawings = [drawing for card in pending["cards"] for drawing in card.get("managed_drawings", [])]
                    assert len(drawings) == 1 and drawings[0]["release_id"] == fixture["release_id"]
                    assert drawings[0]["svg_urls"]["structure"].startswith("data:image/svg+xml;base64,")
                    expected_print = pid == 3323
                    assert bool(drawings[0]["print_objects"]) == expected_print
                    assert (drawings[0]["svg_urls"]["print"] != drawings[0]["svg_urls"]["structure"]) == expected_print
                    for layer in ("structure", "print"):
                        (output / f"{pid}_pending_{layer}.svg").write_bytes(base64.b64decode(drawings[0]["svg_urls"][layer].split(",", 1)[1]))
                    record_response(f"{pid}_waiting_mobile_denied", client.get(
                        f"/api/mobile/production/tasks/{fixture['task_id']}/drawing"), 404)
                    receive_payload = {"received_quantity": 2, "idempotency_key": f"paper-uat-receive-{pid}-two-sheets"}
                    received = record_response(f"{pid}_actual_receive", client.put(
                        f"/api/incoming/receive/so{fixture['supplier_item_id']}", json=receive_payload))
                    (output / f"{pid}_receive.json").write_text(json.dumps(received, ensure_ascii=False, indent=2), encoding="utf-8")
                    with factory() as db:
                        fact = db.scalar(select(IncomingReceiptItem).where(IncomingReceiptItem.supplier_order_item_id == fixture["supplier_item_id"]))
                        assert fact is not None and fact.status == "posted" and fact.received_quantity == 2
                        price = db.scalar(select(SupplierReceiptSettlementPriceFact).where(
                            SupplierReceiptSettlementPriceFact.incoming_receipt_item_id == fact.id))
                        assert price is not None and price.received_quantity_snapshot == 2 and price.unit_price == Decimal(fixture["quote_price"])
                        task = db.get(ProductionTask, fixture["task_id"])
                        assert task.status == "pending" and task.planned_quantity == 2
                        case.update(receipt_item_id=fact.id, price_fact_id=price.id, frozen_unit_price=str(price.unit_price),
                            status_after_receive=task.status, planned_quantity=task.planned_quantity, receipt_quantity=2)
                    record_response(f"{pid}_receive_retry", client.put(
                        f"/api/incoming/receive/so{fixture['supplier_item_id']}", json=receive_payload))
                    with factory() as db:
                        assert db.scalar(select(func.count()).select_from(IncomingReceiptItem).where(
                            IncomingReceiptItem.supplier_order_item_id == fixture["supplier_item_id"])) == 1
                    actual = record_response(f"{pid}_receipt_paper", client.get(
                        f"/api/incoming/receipt-items/{case['receipt_item_id']}/production-card"))
                    (output / f"{pid}_receipt_paper.json").write_text(json.dumps(actual, ensure_ascii=False, indent=2), encoding="utf-8")
                    actual_drawings = [d for card in actual["cards"] for d in card.get("managed_drawings", [])]
                    assert actual_drawings == drawings, "Receipt paper changed or dropped its fixed drawing"
                    mobile = record_response(f"{pid}_ready_mobile", client.get(
                        f"/api/mobile/production/tasks/{fixture['task_id']}/drawing"))
                    assert hashlib.sha256(mobile).hexdigest() == fixture["pdf_sha256"]
                    (output / f"{pid}_mobile_bound.pdf").write_bytes(mobile)
                    case.update(status="passed", has_print_objects=expected_print,
                        pending_and_receipt_drawings_equal=True, mobile_fixed_pdf=True)
                except Exception as error:
                    case.update(status="failed", error=f"{type(error).__name__}: {error}", traceback=traceback.format_exc())

            successful = [case for case in evidence["cases"] if case["status"] == "passed"]
            if successful:
                fixture = successful[0]
                pid = fixture["product_id"]
                current = record_response("history_read_draft", client.get(f"/api/products/{pid}/managed-drawing"))
                payload = {key: value for key, value in current["draft"].items() if key in drawing_design.DesignWrite.model_fields}
                payload.update(expected_product_version=current["draft"]["source_product_version"], expected_design_version=current["draft"]["version"],
                    paper_color="natural" if current["draft"]["paper_color"] == "white" else "white", idempotency_key="paper-flow-later-draft")
                saved = record_response("later_draft", client.put(f"/api/products/{pid}/managed-drawing", json=payload))
                newer = record_response("later_release", client.post(f"/api/products/{pid}/managed-drawing/releases", json={
                    "expected_product_version": saved["draft"]["source_product_version"], "expected_design_version": saved["draft"]["version"],
                    "idempotency_key": "paper-flow-later-release"}))
                assert newer["id"] != fixture["release_id"]
                again = record_response("historical_receipt_reprint", client.get(f"/api/incoming/receipt-items/{fixture['receipt_item_id']}/production-card"))
                assert [d["release_id"] for card in again["cards"] for d in card["managed_drawings"]] == [fixture["release_id"]]
                mobile = record_response("historical_mobile_after_new_release", client.get(f"/api/mobile/production/tasks/{fixture['task_id']}/drawing"))
                assert hashlib.sha256(mobile).hexdigest() == fixture["pdf_sha256"]
                evidence["historical_release_fixed"] = True

        other = next(fixture for fixture in fixtures if fixture["customer_id"] == 10)
        with TestClient(app, raise_server_exceptions=False) as restricted_client:
            record_response("scoped_real_login", restricted_client.post("/api/auth/login",
                json={"username": "uat-drawing-paper-scoped", "password": password}))
            record_response("cross_customer_receipt_denied", restricted_client.put(
                f"/api/incoming/receive/so{other['supplier_item_id']}", json={"received_quantity": 2}), 403)
            record_response("cross_customer_paper_denied", restricted_client.get(
                f"/api/requisition/supplier-orders/{other['supplier_order_id']}/production-print-package"), 403)
            record_response("cross_customer_mobile_hidden", restricted_client.get(
                f"/api/mobile/production/tasks/{other['task_id']}/drawing"), 404)

        with closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)) as copied:
            copied.row_factory = sqlite3.Row
            assert [dict(row) for row in copied.execute("SELECT * FROM products WHERE id IN (183,97,300,3323) ORDER BY id")] == product_rows
            assert [dict(row) for row in copied.execute("SELECT * FROM production_tasks WHERE id IN (222,466) ORDER BY id")] == historical_tasks
        evidence["original_product_and_historical_tasks_unchanged"] = True
        evidence["status"] = "passed_http_legacy_receipt_flow" if all(case["status"] == "passed" for case in evidence["cases"]) else "partial_failures"
    except Exception as error:
        evidence.update(status="failed", error=f"{type(error).__name__}: {error}", traceback=traceback.format_exc())
        raise
    finally:
        if engine is not None:
            engine.dispose()
        after = {"database": digest(source), "files": {str(path.relative_to(source_files)): digest(path)
                 for path in source_files.rglob("*") if path.is_file()}}
        evidence["source_hashes_after"] = after
        evidence["source_unchanged"] = before == after
        if not evidence["source_unchanged"]:
            evidence["status"] = "failed_source_changed"
        evidence["finished_utc"] = datetime.now(timezone.utc).isoformat()
        (output / "result.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(json.dumps({"status": evidence["status"], "cases": [(c["product_id"], c["status"]) for c in evidence["cases"]],
                          "source_unchanged": evidence["source_unchanged"], "output": str(output)}, ensure_ascii=False))
    if evidence["status"] != "passed_http_legacy_receipt_flow":
        raise RuntimeError("Paper-flow UAT has unpassed checks; see saved evidence")


if __name__ == "__main__":
    main()
