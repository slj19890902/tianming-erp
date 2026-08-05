from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any


BATCH_ID = "YKE_KEW_SAMPLE_DRAWINGS_20260805"
CUSTOMER_CODES = {"YKE", "KEW"}
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _readonly(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{database.as_posix()}?mode=ro",
        uri=True,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _database_checks(connection: sqlite3.Connection) -> dict[str, Any]:
    return {
        "integrity_check": str(
            connection.execute("PRAGMA integrity_check").fetchone()[0]
        ),
        "foreign_key_violations": len(
            connection.execute("PRAGMA foreign_key_check").fetchall()
        ),
        "alembic_revision": connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()[0],
        "product_drawings": connection.execute(
            "SELECT count(*) FROM product_drawings"
        ).fetchone()[0],
        "operation_logs": connection.execute(
            "SELECT count(*) FROM operation_logs"
        ).fetchone()[0],
        "sales_orders": connection.execute(
            "SELECT count(*) FROM sales_orders"
        ).fetchone()[0],
        "inventory_lots": connection.execute(
            "SELECT count(*) FROM inventory_lots"
        ).fetchone()[0],
    }


def _build_plan(
    *,
    database: Path,
    image_manifest: Path,
    source_matches: Path,
    allowed_missing_codes: set[str],
) -> dict[str, Any]:
    images = json.loads(image_manifest.read_text(encoding="utf-8"))
    matches_payload = json.loads(source_matches.read_text(encoding="utf-8"))
    matches_by_sample = {
        str(row["sampleNo"]): row for row in matches_payload["matches"]
    }

    with closing(_readonly(database)) as connection:
        customers = {
            str(row["customer_code"]).strip().upper(): dict(row)
            for row in connection.execute(
                """
                SELECT id, customer_number, customer_code, name, is_active
                FROM customers
                WHERE upper(trim(customer_code)) IN ('YKE', 'KEW')
                """
            ).fetchall()
        }
        products = {
            (str(row["customer_code"]).strip().upper(), str(row["product_code"]).strip()): dict(row)
            for row in connection.execute(
                """
                SELECT p.id, p.customer_id, c.customer_code, c.name AS customer_name,
                       p.product_code, p.customer_material_code, p.product_name,
                       p.print_content, p.production_process, p.is_active,
                       p.deleted_at, p.version
                FROM products p
                JOIN customers c ON c.id = p.customer_id
                WHERE upper(trim(c.customer_code)) IN ('YKE', 'KEW')
                """
            ).fetchall()
        }

        assignments: list[dict[str, Any]] = []
        skipped_outside_scope: list[dict[str, Any]] = []
        skipped_missing: list[dict[str, Any]] = []
        errors: list[str] = []

        for image in images:
            sample_no = str(image["sample_no"]).strip()
            product_code = str(image["product_code"]).strip()
            source_match = matches_by_sample.get(sample_no)
            if source_match is None:
                errors.append(f"{sample_no}: 缺少原始匹配证据")
                continue
            source_sheets = sorted(
                {
                    str(row.get("sheet") or "").strip().upper()
                    for row in source_match.get("matches", [])
                    if str(row.get("sheet") or "").strip().upper()
                    in CUSTOMER_CODES
                }
            )
            if not source_sheets:
                skipped_outside_scope.append(
                    {
                        "sample_no": sample_no,
                        "product_code": product_code,
                        "reason": "原始资料未确认属于 YKE 或 KEW",
                    }
                )
                continue

            image_path = Path(image["path"]).resolve()
            if not image_path.is_file():
                errors.append(f"{sample_no}: 图片不存在 {image_path}")
                continue
            image_sha256 = _sha256(image_path)
            for customer_code in source_sheets:
                product = products.get((customer_code, product_code))
                if product is None:
                    if product_code in allowed_missing_codes:
                        skipped_missing.append(
                            {
                                "sample_no": sample_no,
                                "customer_code": customer_code,
                                "product_code": product_code,
                                "reason": "已明确允许跳过：正式常用箱不存在",
                            }
                        )
                    else:
                        errors.append(
                            f"{sample_no}: {customer_code}/{product_code} "
                            "在正式常用箱中不存在"
                        )
                    continue
                if product["deleted_at"] is not None:
                    errors.append(
                        f"{sample_no}: {customer_code}/{product_code} 已在垃圾站"
                    )
                    continue
                assignments.append(
                    {
                        "sample_no": sample_no,
                        "excel_row": int(image["excel_row"]),
                        "customer_code": customer_code,
                        "customer_id": int(product["customer_id"]),
                        "customer_name": product["customer_name"],
                        "product_id": int(product["id"]),
                        "product_code": product_code,
                        "customer_material_code": product[
                            "customer_material_code"
                        ],
                        "product_name": product["product_name"],
                        "print_content": product["print_content"],
                        "source_image_count": int(image["source_image_count"]),
                        "source_path": str(image_path),
                        "source_sha256": image_sha256,
                    }
                )

        assignments.sort(
            key=lambda row: (
                row["customer_code"],
                row["product_code"],
                row["excel_row"],
            )
        )
        target_product_ids = sorted({row["product_id"] for row in assignments})
        existing_drawings: list[dict[str, Any]] = []
        if target_product_ids:
            placeholders = ",".join("?" for _ in target_product_ids)
            existing_drawings = [
                dict(row)
                for row in connection.execute(
                    f"""
                    SELECT id, product_id, image_path, thumbnail_path, uploaded_at
                    FROM product_drawings
                    WHERE product_id IN ({placeholders})
                    ORDER BY product_id, id
                    """,
                    target_product_ids,
                ).fetchall()
            ]
        existing_batch_logs = connection.execute(
            "SELECT count(*) FROM operation_logs WHERE batch_id = ?",
            (BATCH_ID,),
        ).fetchone()[0]
        checks = _database_checks(connection)

    assignment_counts = Counter(row["customer_code"] for row in assignments)
    product_counts = Counter(
        row["customer_code"]
        for row in {
            (entry["customer_code"], entry["product_id"]): entry
            for entry in assignments
        }.values()
    )
    duplicate_product_images = {
        str(product_id): count
        for product_id, count in Counter(
            row["product_id"] for row in assignments
        ).items()
        if count > 1
    }
    return {
        "batch_id": BATCH_ID,
        "database": str(database),
        "database_sha256": _sha256(database),
        "image_manifest": str(image_manifest),
        "image_manifest_sha256": _sha256(image_manifest),
        "source_matches": str(source_matches),
        "source_matches_sha256": _sha256(source_matches),
        "customers": customers,
        "assignment_count": len(assignments),
        "target_product_count": len(target_product_ids),
        "assignment_counts_by_customer": dict(assignment_counts),
        "target_product_counts_by_customer": dict(product_counts),
        "duplicate_product_images": duplicate_product_images,
        "skipped_outside_scope": skipped_outside_scope,
        "skipped_missing": skipped_missing,
        "existing_drawings": existing_drawings,
        "existing_batch_logs": existing_batch_logs,
        "errors": errors,
        "checks_before": checks,
        "assignments": assignments,
    }


def _backup_database(database: Path, backup_dir: Path) -> dict[str, Any]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    target = backup_dir / f"carton_erp_{timestamp}_before_YKE_KEW_drawings.sqlite3"
    temporary = target.with_suffix(".sqlite3.tmp")
    try:
        with closing(sqlite3.connect(database, timeout=30)) as source:
            source.execute("PRAGMA busy_timeout = 30000")
            with closing(sqlite3.connect(temporary, timeout=30)) as destination:
                source.backup(destination)
                destination.commit()
        with closing(sqlite3.connect(temporary, timeout=30)) as check:
            integrity = str(check.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_keys = len(check.execute("PRAGMA foreign_key_check").fetchall())
        if integrity.lower() != "ok" or foreign_keys:
            raise RuntimeError(
                f"备份校验失败 integrity={integrity}, foreign_keys={foreign_keys}"
            )
        temporary.replace(target)
        return {
            "path": str(target),
            "sha256": _sha256(target),
            "size": target.stat().st_size,
            "integrity_check": integrity,
            "foreign_key_violations": foreign_keys,
        }
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _apply(
    *,
    plan: dict[str, Any],
    database: Path,
    upload_root: Path,
    backup_dir: Path,
    expected_database_sha256: str,
) -> dict[str, Any]:
    if plan["errors"]:
        raise RuntimeError("导入计划存在错误，禁止写入")
    if plan["checks_before"]["integrity_check"].lower() != "ok":
        raise RuntimeError("正式数据库完整性检查未通过")
    if plan["checks_before"]["foreign_key_violations"]:
        raise RuntimeError("正式数据库存在外键异常")
    if plan["existing_drawings"]:
        raise RuntimeError("目标常用箱已有图片/图纸，禁止离线叠加写入")
    if plan["existing_batch_logs"]:
        raise RuntimeError("已存在同批次导入日志，禁止重复写入")
    if plan["database_sha256"] != expected_database_sha256:
        raise RuntimeError("正式数据库 SHA-256 与授权前置值不一致")

    backup = _backup_database(database, backup_dir)
    if _sha256(database) != expected_database_sha256:
        raise RuntimeError("备份后正式数据库已变化，已停止写入")

    os.environ["ERP_FILE_STORAGE_DIR"] = str(upload_root.resolve())
    from app.services.product_drawings import (
        remove_drawing_files,
        save_product_drawing_files,
    )
    from app.services.secure_uploads import ValidatedUpload

    stored_rows: list[dict[str, Any]] = []
    try:
        for assignment in plan["assignments"]:
            source = Path(assignment["source_path"])
            content = source.read_bytes()
            upload = ValidatedUpload(
                content=content,
                original_filename=source.name,
                extension=".jpg",
                content_type="image/jpeg",
                size=len(content),
                sha256=assignment["source_sha256"],
            )
            saved = save_product_drawing_files(
                product_id=assignment["product_id"],
                upload=upload,
            )
            stored_rows.append(
                {
                    **assignment,
                    "image_path": saved.image_path,
                    "thumbnail_path": saved.thumbnail_path,
                }
            )

        if _sha256(database) != expected_database_sha256:
            raise RuntimeError("生成私有图片期间正式数据库已变化，已停止写入")

        with closing(sqlite3.connect(database, timeout=30)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("BEGIN IMMEDIATE")
            target_ids = sorted({row["product_id"] for row in stored_rows})
            placeholders = ",".join("?" for _ in target_ids)
            existing = connection.execute(
                f"SELECT count(*) FROM product_drawings WHERE product_id IN ({placeholders})",
                target_ids,
            ).fetchone()[0]
            if existing:
                raise RuntimeError("加锁复核发现目标常用箱已有图片/图纸")

            for row in stored_rows:
                cursor = connection.execute(
                    """
                    INSERT INTO product_drawings
                        (product_id, image_path, thumbnail_path, uploaded_by)
                    VALUES (?, ?, ?, NULL)
                    """,
                    (
                        row["product_id"],
                        row["image_path"],
                        row["thumbnail_path"],
                    ),
                )
                drawing_id = int(cursor.lastrowid)
                details = json.dumps(
                    {
                        "drawing_id": drawing_id,
                        "batch_id": BATCH_ID,
                        "sample_no": row["sample_no"],
                        "source_path": row["source_path"],
                        "source_sha256": row["source_sha256"],
                        "source_image_count": row["source_image_count"],
                        "customer_code": row["customer_code"],
                        "product_code": row["product_code"],
                        "printing_independent": True,
                    },
                    ensure_ascii=False,
                )
                connection.execute(
                    """
                    INSERT INTO operation_logs (
                        user_id, action, resource, details, username, role,
                        entity_type, entity_id, description,
                        event_category, result, source, module_code, action_code,
                        actor_user_id_snapshot, operator_name_snapshot,
                        object_ref, customer_id_snapshot,
                        customer_name_snapshot, batch_id, schema_version
                    ) VALUES (
                        NULL, 'IMPORT_REFERENCE_IMAGE', 'Product', ?,
                        'codex-import', 'system', 'product', ?,
                        'IMPORT_REFERENCE_IMAGE Product',
                        'master_data', 'success', 'offline_tool', 'products',
                        'product.import_reference_image', NULL, 'Codex 离线导入',
                        ?, ?, ?, ?, 1
                    )
                    """,
                    (
                        details,
                        row["product_id"],
                        f"Product:{row['product_id']}",
                        row["customer_id"],
                        row["customer_name"],
                        BATCH_ID,
                    ),
                )
                row["drawing_id"] = drawing_id
            connection.commit()
    except Exception:
        for row in stored_rows:
            remove_drawing_files(row["image_path"], row["thumbnail_path"])
        raise

    with closing(_readonly(database)) as connection:
        checks_after = _database_checks(connection)
        imported_drawings = connection.execute(
            "SELECT count(*) FROM operation_logs WHERE batch_id = ?",
            (BATCH_ID,),
        ).fetchone()[0]
        product_coverage = connection.execute(
            """
            SELECT count(DISTINCT d.product_id)
            FROM product_drawings d
            JOIN operation_logs l
              ON l.batch_id = ?
             AND l.entity_type = 'product'
             AND l.entity_id = d.product_id
            """,
            (BATCH_ID,),
        ).fetchone()[0]
    if checks_after["integrity_check"].lower() != "ok":
        raise RuntimeError("导入后数据库完整性检查失败，请立即使用备份回退")
    if checks_after["foreign_key_violations"]:
        raise RuntimeError("导入后出现外键异常，请立即使用备份回退")
    if imported_drawings != plan["assignment_count"]:
        raise RuntimeError("导入日志数量与计划不一致，请保留现场")
    if product_coverage != plan["target_product_count"]:
        raise RuntimeError("产品图片覆盖数量与计划不一致，请保留现场")

    return {
        **plan,
        "status": "applied",
        "backup": backup,
        "upload_root": str(upload_root.resolve()),
        "database_sha256_after": _sha256(database),
        "checks_after": checks_after,
        "imported_drawing_count": imported_drawings,
        "covered_product_count": product_coverage,
        "stored_rows": stored_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="严格按 YKE/KEW 客户与正式存货编码导入样品图片"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--image-manifest", type=Path, required=True)
    parser.add_argument("--source-matches", type=Path, required=True)
    parser.add_argument("--upload-root", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--allow-missing-product-code", action="append", default=[])
    parser.add_argument("--expected-database-sha256")
    parser.add_argument("--expected-target-product-count", type=int, default=131)
    parser.add_argument("--expected-assignment-count", type=int, default=132)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    database = args.database.resolve(strict=True)
    image_manifest = args.image_manifest.resolve(strict=True)
    source_matches = args.source_matches.resolve(strict=True)
    report = args.report.resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    plan = _build_plan(
        database=database,
        image_manifest=image_manifest,
        source_matches=source_matches,
        allowed_missing_codes={
            str(code).strip() for code in args.allow_missing_product_code
        },
    )
    if plan["target_product_count"] != args.expected_target_product_count:
        plan["errors"].append(
            "目标产品数量不符："
            f"actual={plan['target_product_count']}, "
            f"expected={args.expected_target_product_count}"
        )
    if plan["assignment_count"] != args.expected_assignment_count:
        plan["errors"].append(
            "图片绑定数量不符："
            f"actual={plan['assignment_count']}, "
            f"expected={args.expected_assignment_count}"
        )

    result = {**plan, "status": "preflight"}
    if args.apply:
        if not args.expected_database_sha256:
            raise SystemExit("--apply 必须提供 --expected-database-sha256")
        if args.backup_dir is None:
            raise SystemExit("--apply 必须提供 --backup-dir")
        result = _apply(
            plan=plan,
            database=database,
            upload_root=args.upload_root,
            backup_dir=args.backup_dir.resolve(),
            expected_database_sha256=args.expected_database_sha256,
        )
    report.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "database_sha256": result["database_sha256"],
                "database_sha256_after": result.get("database_sha256_after"),
                "assignment_count": result["assignment_count"],
                "target_product_count": result["target_product_count"],
                "assignment_counts_by_customer": result[
                    "assignment_counts_by_customer"
                ],
                "target_product_counts_by_customer": result[
                    "target_product_counts_by_customer"
                ],
                "duplicate_product_images": result[
                    "duplicate_product_images"
                ],
                "skipped_outside_scope_count": len(
                    result["skipped_outside_scope"]
                ),
                "skipped_missing": result["skipped_missing"],
                "existing_drawing_count": len(result["existing_drawings"]),
                "errors": result["errors"],
                "backup": result.get("backup"),
                "report": str(report),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not result["errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
