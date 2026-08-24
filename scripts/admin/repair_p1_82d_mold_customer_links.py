from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.database import backup_to_nas  # noqa: E402


TASK_ID = "P1-82D"
SOURCE = "scripts.admin.repair_p1_82d_mold_customer_links"
EXPECTED_REVISION = "de39v8x9z28"
CONFIRMATION = "APPLY_P1_82D_MOLD_CUSTOMER_LINK_REPAIR"
ACTION_CODE = "mold.master.customer_link_repair"


@dataclass(frozen=True)
class RepairTarget:
    mold_code: str
    expected_version: int
    expected_mold_name: str
    expected_label_name: str
    keep_customer_code: str
    expected_customer_codes: tuple[str, ...]
    expected_product_customer_codes: tuple[str, ...]


TARGETS = (
    RepairTarget(
        mold_code="M-CE1694DFDDD6",
        expected_version=1,
        expected_mold_name="瑞邦 S4 内盒",
        expected_label_name="S4",
        keep_customer_code="RBJD",
        expected_customer_codes=("RBJD", "TH"),
        expected_product_customer_codes=(),
    ),
    RepairTarget(
        mold_code="M-22B130591F76",
        expected_version=4,
        expected_mold_name="瑞邦 S2 短纸盒",
        expected_label_name="S2",
        keep_customer_code="RBJD",
        expected_customer_codes=("GLR", "RBJD", "TH", "YYKJ"),
        expected_product_customer_codes=("RBJD",),
    ),
    RepairTarget(
        mold_code="M-C134F1C78479",
        expected_version=2,
        expected_mold_name="诺泰盈 27*18*18",
        expected_label_name="27*18*18",
        keep_customer_code="NTY",
        expected_customer_codes=("GT", "NTY"),
        expected_product_customer_codes=(),
    ),
    RepairTarget(
        mold_code="M-E19D9DD7F36E",
        expected_version=9,
        expected_mold_name="高泰 30256 16*9*11",
        expected_label_name="30256",
        keep_customer_code="GT",
        expected_customer_codes=("GLR", "GT", "NTY", "RBJD", "TH", "YYKJ"),
        expected_product_customer_codes=("GT",),
    ),
)


TARGET_PLAN_HASH = hashlib.sha256(
    json.dumps(
        [asdict(target) for target in TARGETS],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()
BATCH_ID = f"{TASK_ID}-{TARGET_PLAN_HASH[:20]}"

PROTECTED_COUNT_TABLES = (
    "customers",
    "products",
    "mold_tools",
    "mold_master_mutations",
    "mold_location_movements",
    "mold_repair_events",
    "mold_scan_events",
    "mold_label_print_jobs",
    "mold_label_print_job_items",
    "sales_orders",
    "sales_order_items",
    "inventory_lots",
    "inventory_movements",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _readonly_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro",
        uri=True,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA query_only = ON")
    return connection


def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    table_names = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    return {
        table: int(
            connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        )
        for table in PROTECTED_COUNT_TABLES
        if table in table_names
    }


def database_checks(path: Path) -> dict[str, Any]:
    with closing(_readonly_connection(path)) as connection:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        revisions = [
            str(row[0])
            for row in connection.execute(
                "SELECT version_num FROM alembic_version ORDER BY version_num"
            )
        ]
        return {
            "integrity_check": integrity,
            "foreign_key_violations": len(foreign_keys),
            "alembic_revisions": revisions,
            "protected_counts": _table_counts(connection),
            "mold_customer_links": int(
                connection.execute("SELECT COUNT(*) FROM mold_tool_customers").fetchone()[0]
            ),
            "operation_logs": int(
                connection.execute("SELECT COUNT(*) FROM operation_logs").fetchone()[0]
            ),
        }


def _customer_by_code(
    connection: sqlite3.Connection,
    customer_code: str,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT id, customer_code, name, chinese_short_name, is_active
        FROM customers
        WHERE customer_code = ?
        """,
        (customer_code,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"客户代码 {customer_code} 不存在")
    if not bool(row["is_active"]):
        raise RuntimeError(f"客户代码 {customer_code} 已停用")
    return row


def _mold_snapshot(
    connection: sqlite3.Connection,
    mold_code: str,
) -> dict[str, Any]:
    mold = connection.execute(
        """
        SELECT id, mold_code, mold_name, label_name, chinese_short_name,
               identity_status, version, rack_location, location_version,
               repair_status, repair_version, remarks, is_active,
               archive_status, created_by, updated_by, created_at, updated_at
        FROM mold_tools
        WHERE mold_code = ?
        """,
        (mold_code,),
    ).fetchone()
    if mold is None:
        raise RuntimeError(f"目标模具 {mold_code} 不存在")
    links = [
        {
            "link_id": int(row["link_id"]),
            "customer_id": int(row["customer_id"]),
            "customer_code": str(row["customer_code"]),
            "display_order": row["display_order"],
        }
        for row in connection.execute(
            """
            SELECT mc.id AS link_id, mc.customer_id, c.customer_code,
                   mc.display_order
            FROM mold_tool_customers mc
            JOIN customers c ON c.id = mc.customer_id
            WHERE mc.mold_tool_id = ?
            ORDER BY c.customer_code, mc.id
            """,
            (mold["id"],),
        )
    ]
    products = [
        {
            "product_id": int(row["product_id"]),
            "customer_code": str(row["customer_code"]),
        }
        for row in connection.execute(
            """
            SELECT p.id AS product_id, c.customer_code
            FROM products p
            JOIN customers c ON c.id = p.customer_id
            WHERE p.mold_tool_id = ?
            ORDER BY p.id
            """,
            (mold["id"],),
        )
    ]
    return {
        **dict(mold),
        "customers": links,
        "products": products,
    }


def _repair_audit_count(
    connection: sqlite3.Connection,
    mold_code: str,
) -> int:
    return int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM operation_logs
            WHERE batch_id = ?
              AND action_code = ?
              AND object_ref = ?
              AND result = 'success'
            """,
            (BATCH_ID, ACTION_CODE, mold_code),
        ).fetchone()[0]
    )


def build_plan(connection: sqlite3.Connection) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    states: set[str] = set()
    target_codes = {target.mold_code for target in TARGETS}

    for target in TARGETS:
        snapshot = _mold_snapshot(connection, target.mold_code)
        if snapshot["identity_status"] != "frozen":
            raise RuntimeError(f"目标模具 {target.mold_code} 不是正式标签档案")
        if snapshot["mold_name"] != target.expected_mold_name:
            raise RuntimeError(f"目标模具 {target.mold_code} 名称已变化，拒绝修复")
        if snapshot["label_name"] != target.expected_label_name:
            raise RuntimeError(f"目标模具 {target.mold_code} 标签名称已变化，拒绝修复")

        customer_codes = tuple(
            sorted(str(item["customer_code"]) for item in snapshot["customers"])
        )
        expected_codes = tuple(sorted(target.expected_customer_codes))
        product_customer_codes = tuple(
            sorted({str(item["customer_code"]) for item in snapshot["products"]})
        )
        expected_product_codes = tuple(sorted(target.expected_product_customer_codes))
        keep_links = [
            item
            for item in snapshot["customers"]
            if item["customer_code"] == target.keep_customer_code
        ]
        if len(keep_links) != 1 or keep_links[0]["display_order"] != 1:
            raise RuntimeError(
                f"目标模具 {target.mold_code} 的唯一主客户不符合修复计划"
            )
        if product_customer_codes != expected_product_codes:
            raise RuntimeError(
                f"目标模具 {target.mold_code} 的产品绑定客户已变化，拒绝修复"
            )

        audit_count = _repair_audit_count(connection, target.mold_code)
        if (
            snapshot["version"] == target.expected_version
            and customer_codes == expected_codes
            and audit_count == 0
        ):
            state = "ready"
        elif (
            snapshot["version"] == target.expected_version + 1
            and customer_codes == (target.keep_customer_code,)
            and audit_count == 1
        ):
            state = "already_applied"
        else:
            raise RuntimeError(
                f"目标模具 {target.mold_code} 的版本、客户集合或审计状态已变化，拒绝修复"
            )
        states.add(state)
        items.append(
            {
                "mold_id": int(snapshot["id"]),
                "mold_code": target.mold_code,
                "label_name": target.expected_label_name,
                "current_version": int(snapshot["version"]),
                "keep_customer_code": target.keep_customer_code,
                "customer_codes": list(customer_codes),
                "product_customer_codes": list(product_customer_codes),
                "links_to_remove": max(0, len(customer_codes) - 1),
                "audit_count": audit_count,
                "state": state,
            }
        )

    if len(states) != 1:
        raise RuntimeError("4 件目标模具处于部分修复状态，拒绝继续")
    status = next(iter(states))

    multi_customer_codes = {
        str(row[0])
        for row in connection.execute(
            """
            SELECT m.mold_code
            FROM mold_tools m
            JOIN mold_tool_customers mc ON mc.mold_tool_id = m.id
            WHERE m.identity_status = 'frozen'
            GROUP BY m.id, m.mold_code
            HAVING COUNT(*) > 1
            """
        )
    }
    if status == "ready" and multi_customer_codes != target_codes:
        unexpected = sorted(multi_customer_codes.symmetric_difference(target_codes))
        raise RuntimeError(f"正式多客户模具范围已变化，拒绝部分修复：{unexpected}")
    if status == "already_applied" and multi_customer_codes.intersection(target_codes):
        raise RuntimeError("修复审计已存在，但目标模具仍有多客户关联")

    return {
        "task_id": TASK_ID,
        "status": status,
        "batch_id": BATCH_ID,
        "target_plan_sha256": TARGET_PLAN_HASH,
        "target_count": len(items),
        "links_to_remove": sum(item["links_to_remove"] for item in items),
        "items": items,
        "creates_molds": False,
        "changes_locations": False,
        "changes_product_bindings": False,
        "changes_historical_mutations": False,
    }


def inspect_plan(database: Path) -> dict[str, Any]:
    with closing(_readonly_connection(database)) as connection:
        return build_plan(connection)


def _protected_mold_facts(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        key: snapshot[key]
        for key in (
            "id",
            "mold_code",
            "mold_name",
            "label_name",
            "chinese_short_name",
            "identity_status",
            "rack_location",
            "location_version",
            "repair_status",
            "repair_version",
            "remarks",
            "is_active",
            "archive_status",
            "created_by",
            "created_at",
        )
    } | {"products": snapshot["products"]}


def _validate_checks(checks: dict[str, Any], *, stage: str) -> None:
    if checks["integrity_check"].lower() != "ok":
        raise RuntimeError(f"{stage}完整性检查失败：{checks['integrity_check']}")
    if checks["foreign_key_violations"] != 0:
        raise RuntimeError(f"{stage}存在外键异常：{checks['foreign_key_violations']}")
    if checks["alembic_revisions"] != [EXPECTED_REVISION]:
        raise RuntimeError(
            f"{stage}数据库 revision 不是唯一 {EXPECTED_REVISION}："
            f"{checks['alembic_revisions']}"
        )


def _assert_count_deltas(
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    if after["protected_counts"] != before["protected_counts"]:
        raise RuntimeError("修复前后受保护业务表计数发生变化")
    expected_link_count = before["mold_customer_links"] - 10
    if after["mold_customer_links"] != expected_link_count:
        raise RuntimeError(
            f"模具客户关联计数不是预期 {expected_link_count}："
            f"{after['mold_customer_links']}"
        )
    expected_log_count = before["operation_logs"] + len(TARGETS)
    if after["operation_logs"] != expected_log_count:
        raise RuntimeError(
            f"操作审计计数不是预期 {expected_log_count}：{after['operation_logs']}"
        )


def apply_repair(
    *,
    database: Path,
    backup_dir: Path,
    actor_username: str,
    expected_sha256: str,
) -> dict[str, Any]:
    database = database.resolve()
    source_sha256 = sha256_file(database)
    if source_sha256.lower() != expected_sha256.lower():
        raise RuntimeError("正式库 SHA-256 已变化，拒绝执行")

    before_checks = database_checks(database)
    _validate_checks(before_checks, stage="写入前")
    preflight = inspect_plan(database)
    if preflight["status"] == "already_applied":
        return {
            "changed": False,
            "message": "P1-82D 已完整执行，无需重复写入",
            "database_sha256": source_sha256,
            "checks": before_checks,
            "plan": preflight,
        }
    if preflight["links_to_remove"] != 10:
        raise RuntimeError("只读计划不是 4 件/10 条误关联，拒绝执行")

    backup = backup_to_nas(
        source_path=database,
        backup_dir=backup_dir,
        filename_suffix="_P1_82D_MOLD_CUSTOMER_LINKS_BEFORE_REPAIR",
        keep_regular=1_000_000,
    )
    backup_checks = database_checks(backup.path)
    _validate_checks(backup_checks, stage="备份")
    backup_plan = inspect_plan(backup.path)
    if (
        backup_checks != before_checks
        or backup_plan != preflight
        or sha256_file(database).lower() != source_sha256.lower()
    ):
        raise RuntimeError("备份后正式库状态发生变化或备份未通过等价验证")

    connection = sqlite3.connect(database, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        live_plan = build_plan(connection)
        if live_plan != preflight or live_plan["status"] != "ready":
            raise RuntimeError("取得写锁后目标状态已变化，拒绝执行")

        actor = connection.execute(
            """
            SELECT id, username, role, real_name, is_active
            FROM users
            WHERE username = ?
            """,
            (actor_username,),
        ).fetchone()
        if (
            actor is None
            or not bool(actor["is_active"])
            or str(actor["role"]) not in {"admin", "boss"}
        ):
            raise RuntimeError("执行人必须是启用的 admin 或 boss 账号")

        before_snapshots = {
            target.mold_code: _mold_snapshot(connection, target.mold_code)
            for target in TARGETS
        }
        for target in TARGETS:
            before = before_snapshots[target.mold_code]
            keep_customer = _customer_by_code(connection, target.keep_customer_code)
            removed_codes = sorted(
                item["customer_code"]
                for item in before["customers"]
                if item["customer_code"] != target.keep_customer_code
            )
            deleted = connection.execute(
                """
                DELETE FROM mold_tool_customers
                WHERE mold_tool_id = ? AND customer_id <> ?
                """,
                (before["id"], keep_customer["id"]),
            ).rowcount
            if deleted != len(removed_codes):
                raise RuntimeError(
                    f"目标模具 {target.mold_code} 删除关联数量不一致"
                )
            updated = connection.execute(
                """
                UPDATE mold_tools
                SET version = version + 1,
                    updated_by = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND version = ?
                """,
                (actor["id"], before["id"], target.expected_version),
            ).rowcount
            if updated != 1:
                raise RuntimeError(f"目标模具 {target.mold_code} 版本抢占失败")

            details = {
                "task_id": TASK_ID,
                "source": SOURCE,
                "reason": "修复 P1-82R 上线前连续新增残留编辑态造成的客户误关联",
                "before": {
                    "version": target.expected_version,
                    "customer_codes": sorted(target.expected_customer_codes),
                },
                "after": {
                    "version": target.expected_version + 1,
                    "customer_codes": [target.keep_customer_code],
                },
                "removed_customer_codes": removed_codes,
                "protected": {
                    "stable_mold_id": int(before["id"]),
                    "location_unchanged": True,
                    "product_bindings_unchanged": True,
                    "historical_mutations_unchanged": True,
                },
                "target_plan_sha256": TARGET_PLAN_HASH,
            }
            connection.execute(
                """
                INSERT INTO operation_logs (
                    user_id, action, resource, details, username, role,
                    entity_type, entity_id, description, event_category,
                    result, source, module_code, action_code,
                    actor_user_id_snapshot, operator_name_snapshot,
                    object_ref, customer_id_snapshot, customer_name_snapshot,
                    request_id, batch_id, schema_version
                ) VALUES (
                    ?, 'DATA_REPAIR', ?, ?, ?, ?,
                    'mold_tool', ?, ?, 'business',
                    'success', 'script', 'warehouse', ?,
                    ?, ?, ?, ?, ?, ?, ?, 1
                )
                """,
                (
                    actor["id"],
                    f"warehouse/molds/{before['id']}",
                    json.dumps(details, ensure_ascii=False, sort_keys=True),
                    actor["username"],
                    actor["role"],
                    before["id"],
                    "修复连续新增残留客户误关联",
                    ACTION_CODE,
                    actor["id"],
                    actor["real_name"] or actor["username"],
                    target.mold_code,
                    keep_customer["id"],
                    keep_customer["name"],
                    BATCH_ID,
                    BATCH_ID,
                ),
            )

        transaction_plan = build_plan(connection)
        if transaction_plan["status"] != "already_applied":
            raise RuntimeError("事务内回读未达到完整修复状态")
        for target in TARGETS:
            after = _mold_snapshot(connection, target.mold_code)
            if _protected_mold_facts(after) != _protected_mold_facts(
                before_snapshots[target.mold_code]
            ):
                raise RuntimeError(
                    f"目标模具 {target.mold_code} 的位置、产品绑定或其他受保护事实发生变化"
                )
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise RuntimeError(f"事务内出现外键异常：{len(foreign_keys)}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    after_checks = database_checks(database)
    _validate_checks(after_checks, stage="修复后")
    _assert_count_deltas(before_checks, after_checks)
    result_plan = inspect_plan(database)
    if result_plan["status"] != "already_applied":
        raise RuntimeError(
            f"修复后回读失败；使用修复前备份核对：{backup.path}"
        )
    return {
        "changed": True,
        "database_sha256_before": source_sha256,
        "database_sha256_after": sha256_file(database),
        "backup": {
            **asdict(backup),
            "path": str(backup.path),
            "checks": backup_checks,
        },
        "checks_before": before_checks,
        "checks_after": after_checks,
        "plan_before": preflight,
        "plan_after": result_plan,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="P1-82D：精确清理 4 件模具的 10 条连续新增残留客户误关联"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--actor", default="admin")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--confirm-live-service-transaction", action="store_true")
    parser.add_argument("--expected-sha256")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    database = args.database.resolve()
    if not database.is_file():
        raise SystemExit(f"数据库不存在：{database}")
    if not args.apply:
        result = {
            "apply": False,
            "database": str(database),
            "database_sha256": sha256_file(database),
            "checks": database_checks(database),
            "plan": inspect_plan(database),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    if args.confirm != CONFIRMATION:
        raise SystemExit(f"--apply 必须同时提供 --confirm {CONFIRMATION}")
    if not args.confirm_live_service_transaction:
        raise SystemExit("在线正式修复必须提供 --confirm-live-service-transaction")
    if not args.expected_sha256:
        raise SystemExit("正式写入必须提供 --expected-sha256")
    backup_dir = (args.backup_dir or database.parent / "backups").resolve()
    result = apply_repair(
        database=database,
        backup_dir=backup_dir,
        actor_username=args.actor,
        expected_sha256=args.expected_sha256,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
