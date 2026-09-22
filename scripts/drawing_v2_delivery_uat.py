"""One-shot drawing V2 UAT on a new, explicitly isolated SQLite backup.

Run from the isolated worktree with: python -B -m scripts.drawing_v2_delivery_uat
The source is always opened mode=ro. Existing target DB/storage/output paths
are refused. This is an API/service UAT, not browser or factory acceptance.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import traceback
from xml.etree import ElementTree


SOURCE = Path("D:/tm-uat/bom-home-v353-runtime-20260911/carton_erp_uat.sqlite3")
PRODUCT_IDS = (183, 97, 300, 3323, 1605)
HISTORICAL_TASK_IDS = (222, 466)


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256")
    return digest.hexdigest()


def source_fingerprints() -> dict:
    return {path.name: sha256(path) for path in (
        SOURCE, Path(str(SOURCE) + "-wal"), Path(str(SOURCE) + "-shm")
    ) if path.is_file()}


def readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def rows(connection, table: str, ids: tuple[int, ...]) -> list[dict]:
    marks = ",".join("?" for _ in ids)
    return [dict(row) for row in connection.execute(
        f"SELECT * FROM {table} WHERE id IN ({marks}) ORDER BY id", ids)]


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    uat_root = root / ".uat"
    target = uat_root / "delivery_reference_20260922.sqlite3"
    storage = uat_root / "delivery_reference_files_20260922"
    output = root / "output/drawing_v2_uat_20260921/delivery_reference_20260922"
    if not SOURCE.is_file():
        raise RuntimeError("Expected local source replica is missing")
    if any(path.exists() or path.is_symlink() for path in (target, storage, output)):
        raise RuntimeError("One-shot UAT refuses an existing target DB/storage/output; nothing overwritten")
    if uat_root.is_symlink() or target.resolve().parent != uat_root.resolve():
        raise RuntimeError("UAT target must stay in this isolated worktree's .uat directory")
    if SOURCE.resolve() == target.resolve():
        raise RuntimeError("Source and target must differ")
    result = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": str(SOURCE), "database": str(target), "storage": str(storage),
        "source_fingerprints_before": source_fingerprints(),
        "browser_verified": False, "factory_deployed": False,
        "slot_width_basis": "5mm仅隔离链路测试值，非现场确认；不能据本测试批准开槽生产轮廓",
        "liner_material_difference": "21302044源产品和历史订单为B三层；任务书AB五层要求另待正式产品核对，本轮未覆盖",
        "checks": [], "status": "running",
    }
    uat_root.mkdir(exist_ok=True)
    output.mkdir(parents=True)
    engine = None
    try:
        with closing(readonly(SOURCE)) as source:
            result["source_alembic_revision"] = source.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            source_products = rows(source, "products", PRODUCT_IDS)
            source_tasks = rows(source, "production_tasks", HISTORICAL_TASK_IDS)
            if len(source_products) != len(PRODUCT_IDS) or len(source_tasks) != len(HISTORICAL_TASK_IDS):
                raise RuntimeError("Expected real reference records are missing")
            fields = ("id", "product_code", "product_name", "customer_id", "version", "length_mm", "width_mm", "height_mm",
                      "flute_type", "layer_count", "material_id", "report_length_mm", "report_width_mm", "crease_left_mm",
                      "crease_middle_mm", "crease_right_mm", "splice_mode", "pieces_per_box", "flap_mm", "mold_tool_id")
            result["reference_product_fields"] = [{key: row.get(key) for key in fields} for row in source_products]
            result["source_reference_rows_sha256"] = hashlib.sha256(json.dumps(
                {"products": source_products, "historical_tasks": source_tasks}, sort_keys=True).encode()).hexdigest()
            with sqlite3.connect(target) as destination:
                source.backup(destination)
        storage.mkdir()
        os.environ["ERP_DATABASE_PATH"] = str(target)
        os.environ["ERP_FILE_STORAGE_DIR"] = str(storage)
        os.environ["ERP_DRAWING_V2_ENABLED"] = "1"

        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy import create_engine, select, text
        from sqlalchemy.orm import Session
        from fastapi import HTTPException
        from app.api.drawing_design import (
            DesignWrite, PublishWrite, download_release, get_design,
            publish_design, release_svg, save_design,
        )
        from app.api.mobile_erp import mobile_production_task_drawing
        from app.models.drawing_design import DrawingRelease
        from app.models.order import Order, OrderItem
        from app.models.product import Product
        from app.models.production import ProductionTask
        from app.models.user import User
        from app.services.drawing_binding import bind_new_task_drawing, bound_task_release
        from app.services.drawing_geometry import build_geometry, custom_parameter_suggestions
        from app.services.drawing_product_rules import slotted_parameter_candidates
        from scripts.drawing_v2_reconcile import audit

        migration = root / "alembic/versions/rt21v8x9z68_drawing_v2_managed_release.py"
        spec = importlib.util.spec_from_file_location("drawing_v2_delivery_schema", migration)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        engine = create_engine(f"sqlite:///{target.as_posix()}")
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                module.upgrade()
            actual_revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert actual_revision == result["source_alembic_revision"]
        result["schema_action"] = {
            "module": str(migration), "sha256": sha256(migration),
            "action": "Only this V2 upgrade applied with Operations to isolated backup; alembic_version unchanged",
            "added_tables": ["drawing_designs", "drawing_number_sequences", "drawing_releases",
                             "production_task_drawings", "production_task_drawing_adoptions"],
        }

        with Session(engine) as db:
            user = db.scalar(select(User).where(User.role == "admin").order_by(User.id))
            if user is None:
                raise RuntimeError("Isolated source has no admin user for permission-preserving calls")
            real_products = {pid: db.get(Product, pid) for pid in PRODUCT_IDS}
            candidates = slotted_parameter_candidates(real_products[1605])
            assert candidates["top_flap_mm"] == candidates["bottom_flap_mm"] == "197.5"
            assert real_products[1605].crease_left_mm == 200 and real_products[1605].report_width_mm == 610
            result["checks"].append({"name": "8116_decimal_candidate_procurement_separate", "passed": True,
                "product_id": 1605, "candidates": candidates, "manual_report_mm": [1645, 610],
                "manual_crease_mm": [200, 210, 200]})

            cases = [
                (183, "liner_v1", {}, ("1070", "580"), "真实B三层衬板；保留原产品和人工报料"),
                (97, "custom_21301634_v1", {
                    **custom_parameter_suggestions(real_products[97].length_mm, real_products[97].width_mm, real_products[97].height_mm),
                    "top_fold_mm": 28, "bottom_fold_mm": 28, "left_wing_mm": 40, "right_wing_mm": 40,
                }, ("752", "996"), "用户确认620×470×26/28、40mm侧翼及四角内收"),
                (300, "custom_21301634_v1", {
                    "top_cover_mm": 308, "bottom_cover_mm": 308,
                    "top_fold_mm": 28, "bottom_fold_mm": 28,
                    "left_fold_mm": 25, "right_fold_mm": 25, "left_wing_mm": 40, "right_wing_mm": 40,
                }, ("1295", "1287"), "21301634原参考稿独立参数"),
                (3323, "slotted_v1", slotted_parameter_candidates(real_products[3323]),
                 ("1690", "480"), result["slot_width_basis"]),
            ]
            initial_releases = {}
            for pid, template, params, expected_dimensions, basis in cases:
                product = real_products[pid]
                saved = save_design(pid, DesignWrite(
                    expected_product_version=product.version, template_key=template, parameters=params,
                    idempotency_key=f"delivery-uat-save-{pid}-initial"), db, user)
                if pid == 3323:
                    assert saved["geometry_ready"] is False
                    try:
                        publish_design(pid, PublishWrite(expected_product_version=product.version,
                            expected_design_version=saved["draft"]["version"],
                            idempotency_key="delivery-uat-incomplete-slot-3323"), db, user)
                    except HTTPException as error:
                        assert error.status_code == 422
                        result["checks"].append({"name": "slot_missing_cannot_publish", "passed": True,
                            "status_code": error.status_code, "detail": error.detail})
                    else:
                        raise AssertionError("Unknown slot width was published")
                    assert db.scalar(select(DrawingRelease).where(DrawingRelease.product_id == pid)) is None
                    params = dict(params, slot_width_mm="5")
                    saved = save_design(pid, DesignWrite(
                        expected_product_version=product.version, expected_design_version=saved["draft"]["version"],
                        template_key=template, parameters=params,
                        print_objects=[dict(kind="text", text="UAT槽宽5mm仅测试，非现场确认", panel_id="panel_1",
                            x_mm=10, y_mm=10, width_mm=200, height_mm=12)],
                        idempotency_key="delivery-uat-slot-3323-test-only"), db, user)
                assert saved["geometry_ready"] is True
                release = publish_design(pid, PublishWrite(expected_product_version=product.version,
                    expected_design_version=saved["draft"]["version"],
                    idempotency_key=f"delivery-uat-publish-{pid}-initial"), db, user)
                row = db.get(DrawingRelease, release["id"])
                manifest = json.loads(row.manifest_json)
                geometry = manifest["geometry"]
                assert (geometry["width_mm"], geometry["height_mm"]) == expected_dimensions
                resolved = dict(manifest["parameters"])
                if template == "liner_v1":
                    resolved.update(length_mm=product.length_mm, width_mm=product.width_mm)
                elif template == "custom_21301634_v1":
                    resolved.update(panel_width_mm=product.length_mm, panel_height_mm=product.width_mm)
                assert build_geometry(template, resolved) == geometry
                if pid == 183:
                    assert manifest["thickness"].startswith("3mm")
                pdf_path = Path(download_release(pid, row.id, db, user).path)
                assert sha256(pdf_path) == row.pdf_sha256
                assert pdf_path.read_bytes().startswith(b"%PDF-")
                label = f"{product.product_code}_{template}_UAT"
                shutil.copyfile(pdf_path, output / f"{label}.pdf")
                for layer in ("structure", "print"):
                    response = release_svg(pid, row.id, layer, db, user)
                    content = response.body
                    ElementTree.fromstring(content)
                    assert hashlib.sha256(content).hexdigest() == manifest["svg_snapshots"][layer]["sha256"]
                    (output / f"{label}_{layer}.svg").write_bytes(content)
                (output / f"{label}_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

                order = Order(order_number=f"UAT-DRAWING-20260922-{pid}", customer_id=product.customer_id,
                    order_date=date(2026, 9, 22), status="pending_production", remark="仅图纸V2隔离UAT，禁止导入正式业务")
                db.add(order)
                db.flush()
                item = OrderItem(order_id=order.id, product_id=pid, quantity=1, unit_price=0, subtotal=0,
                    snapshot_product_name=f"UAT-{product.product_name}", snapshot_product_code=product.product_code,
                    snapshot_spec=(f"{product.length_mm}×{product.width_mm}mm" if template == "liner_v1"
                        else f"{product.length_mm}×{product.width_mm}×{product.height_mm}mm"),
                    material_status="received", item_order_number=f"UAT-DRAWING-20260922-{pid}-001")
                db.add(item)
                db.flush()
                task = ProductionTask(order_item_id=item.id, status="pending", planned_quantity=1,
                    material_received_quantity=1, material_input_quantity=1, ordered_quantity_snapshot=1,
                    version=1, production_label_template_version_snapshot="current_40x30_v2",
                    production_label_product_version_snapshot=product.version)
                db.add(task)
                db.flush()
                bind_new_task_drawing(db, task, product, source_is_new=True)
                db.commit()
                assert bound_task_release(db, task.id).id == row.id
                mobile = mobile_production_task_drawing(task.id, db, user)
                assert sha256(Path(mobile.path)) == row.pdf_sha256
                initial_releases[pid] = {"release_id": row.id, "task_id": task.id,
                    "pdf_sha256": row.pdf_sha256, "manifest_json": row.manifest_json}
                result["checks"].append({"name": f"real_{pid}_{template}", "passed": True,
                    "product_code": product.product_code, "source_product_version": product.version,
                    "release": release, "task_id": task.id, "basis": basis,
                    "net_dimensions_mm": list(expected_dimensions), "pdf_sha256": row.pdf_sha256,
                    "mobile_bound_pdf_verified": True, "production_approved": False,
                    "mold_snapshot": manifest["mold_snapshot"], "thickness": manifest["thickness"]})

            # A subsequent release for 21301204 must not change its already-bound
            # task or the separately published 21301634 parameters/files.
            product = real_products[97]
            draft = get_design(97, db, user)["draft"]
            changed = dict(draft["parameters"], left_wing_mm="35.5")
            saved = save_design(97, DesignWrite(expected_product_version=product.version,
                expected_design_version=draft["version"], template_key="custom_21301634_v1",
                parameters=changed, idempotency_key="delivery-uat-save-97-new-wing"), db, user)
            newer = publish_design(97, PublishWrite(expected_product_version=product.version,
                expected_design_version=saved["draft"]["version"],
                idempotency_key="delivery-uat-publish-97-new-wing"), db, user)
            assert newer["id"] != initial_releases[97]["release_id"]
            assert json.loads(db.get(DrawingRelease, newer["id"]).manifest_json)["geometry"]["width_mm"] == "747.5"
            for pid in (97, 300):
                initial = initial_releases[pid]
                assert bound_task_release(db, initial["task_id"]).id == initial["release_id"]
                assert db.get(DrawingRelease, initial["release_id"]).manifest_json == initial["manifest_json"]
                assert sha256(Path(mobile_production_task_drawing(initial["task_id"], db, user).path)) == initial["pdf_sha256"]
            for tid in HISTORICAL_TASK_IDS:
                assert bound_task_release(db, tid) is None
            result["checks"].append({"name": "same_series_independent_and_history_fixed", "passed": True,
                "new_isolated_97_release": newer, "historical_tasks_without_binding": list(HISTORICAL_TASK_IDS)})

        engine.dispose()
        engine = None
        with closing(readonly(target)) as destination:
            assert rows(destination, "products", PRODUCT_IDS) == source_products
            assert rows(destination, "production_tasks", HISTORICAL_TASK_IDS) == source_tasks
            assert destination.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert destination.execute("SELECT version_num FROM alembic_version").fetchone()[0] == result["source_alembic_revision"]
        reconciliation = audit(target, storage)
        assert not reconciliation["missing"] and not reconciliation["damaged"] and not reconciliation["orphan"]
        result["file_reconciliation"] = reconciliation
        result["checks"].append({"name": "source_product_and_historical_task_rows_unchanged_in_copy", "passed": True})
        result["paper_order_verified"] = False
        result["paper_order_limit"] = "本脚本未构造到料单纸单链；由独立定向用例和后续Chrome验收验证，未算通过"
        result["status"] = "passed_with_explicit_site_acceptance_gaps"
    except Exception as error:
        result["status"] = "failed"
        result["error"] = f"{type(error).__name__}: {error}"
        result["traceback"] = traceback.format_exc()
        raise
    finally:
        if engine is not None:
            engine.dispose()
        result["source_fingerprints_after"] = source_fingerprints()
        result["source_files_unchanged"] = result["source_fingerprints_after"] == result["source_fingerprints_before"]
        if not result["source_files_unchanged"]:
            result["status"] = "failed_source_changed"
        result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(json.dumps({"status": result["status"], "source_files_unchanged": result["source_files_unchanged"],
                          "checks": len(result["checks"]), "output": str(output)}, ensure_ascii=False))
    if not result["source_files_unchanged"]:
        raise RuntimeError("Source fingerprints changed; inspect evidence before any further use")


if __name__ == "__main__":
    main()
