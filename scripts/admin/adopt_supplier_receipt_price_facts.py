from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Iterable, Sequence

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.finance import FinanceIdempotencyRecord  # noqa: E402
from app.models.incoming_receipt import IncomingReceipt  # noqa: E402
from app.models.supplier_settlement import (  # noqa: E402
    SupplierReceiptSettlementPriceFact,
)
from app.models.user import User  # noqa: E402
from app.services.audit_log import append_audit_event  # noqa: E402
from app.services.purchase_receipt_facts import (  # noqa: E402
    calculate_purchase_sheet_cost_breakdown,
)
from app.services.supplier_monthly_settlement import (  # noqa: E402
    scan_settlement_candidates,
    settlement_period_utc_bounds,
)
from app.services.supplier_receipt_price_facts import (  # noqa: E402
    HISTORICAL_ADOPTION_REASON,
    SupplierReceiptPriceFactError,
    adopt_historical_price_facts,
    preview_historical_price_adoptions,
)
from app.version import APP_VERSION  # noqa: E402


APPLY_CONFIRMATION = "APPLY_P0_39_HISTORICAL_PRICE_ADOPTION"
ACTION_CODE = "SUPPLIER_RECEIPT_PRICE_FACT_HISTORICAL_ADOPT"
RESOURCE_TYPE = "supplier_receipt_price_fact"
FORMAL_DATABASE_NAME = "carton_erp.sqlite3"
MAX_BATCH_SIZE = 500
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
BATCH_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,120}$")

ROLLBACK_INSTRUCTIONS = (
    "价格采用事实为不可变事实，禁止逐行 UPDATE/DELETE 回退。若执行后尚未恢复 ERP、"
    "尚未产生下游对账/发票/付款且验收失败，应保留失败数据库，停止 ERP，用本次已验证的"
    "时点整库备份恢复，再核对 SHA-256、integrity_check、foreign_key_check 和 Alembic head。"
    "若已产生下游事实，不得恢复旧备份覆盖新业务，应另走受控更正任务。"
)


class AdoptionCliError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PlanArtifacts:
    json_path: Path
    csv_path: Path
    json_sha256: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(payload: Any) -> str:
    body = json.dumps(
        _jsonable(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return value


def _read_only_sqlite(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _read_only_engine(path: Path) -> Engine:
    return create_engine(
        "sqlite+pysqlite://",
        creator=lambda: _read_only_sqlite(path),
        future=True,
    )


def _apply_engine(path: Path) -> Engine:
    resolved = path.resolve()
    engine = create_engine(
        f"sqlite+pysqlite:///{resolved.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 30},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _configure_sqlite(connection: sqlite3.Connection, _record) -> None:
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA busy_timeout = 30000")
        finally:
            cursor.close()

    return engine


def code_alembic_heads() -> list[str]:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return sorted(ScriptDirectory.from_config(config).get_heads())


def database_checks(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_file():
        raise AdoptionCliError(f"数据库不存在：{resolved}")
    with _read_only_sqlite(resolved) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        revisions = (
            [
                str(row[0])
                for row in connection.execute(
                    "SELECT version_num FROM alembic_version ORDER BY version_num"
                ).fetchall()
            ]
            if "alembic_version" in tables
            else []
        )
        integrity_rows = [
            str(row[0]) for row in connection.execute("PRAGMA integrity_check")
        ]
        foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
    return {
        "path": str(resolved),
        "sha256": sha256_file(resolved),
        "size_bytes": resolved.stat().st_size,
        "alembic_revisions": revisions,
        "integrity_check": integrity_rows,
        "foreign_key_violation_count": len(foreign_key_rows),
    }


def _assert_healthy_database(checks: dict[str, Any], *, expected_head: str) -> None:
    if checks["integrity_check"] != ["ok"]:
        raise AdoptionCliError(
            f"数据库完整性失败：{checks['integrity_check']}"
        )
    if checks["foreign_key_violation_count"] != 0:
        raise AdoptionCliError(
            f"数据库存在 {checks['foreign_key_violation_count']} 条外键异常"
        )
    if checks["alembic_revisions"] != [expected_head]:
        raise AdoptionCliError(
            "数据库 Alembic revision 不匹配："
            f"{checks['alembic_revisions']} != {[expected_head]}"
        )


def _scope_bounds(
    db: Session,
    *,
    settlement_month: str | None,
    all_history: bool,
) -> tuple[str, str, datetime, datetime]:
    if all_history:
        earliest, latest = db.execute(
            select(
                func.min(IncomingReceipt.received_at),
                func.max(IncomingReceipt.received_at),
            ).where(IncomingReceipt.status == "posted")
        ).one()
        if earliest is None or latest is None:
            start_utc = datetime(1900, 1, 1)
            end_utc = datetime(2100, 1, 1)
        else:
            start_utc = earliest
            end_utc = latest + timedelta(microseconds=1)
        return "all_history", "all-history", start_utc, end_utc
    if settlement_month is None or not MONTH_RE.fullmatch(settlement_month):
        raise AdoptionCliError("dry-run 必须指定 --settlement-month YYYY-MM 或 --all-history")
    _start, _end, start_utc, end_utc = settlement_period_utc_bounds(
        settlement_month
    )
    return "settlement_month", settlement_month, start_utc, end_utc


def _amount_summary(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[tuple[int, str], dict[str, Any]] = {}
    total = Decimal("0")
    tax_total = Decimal("0")
    row_count = 0
    quantity = 0
    for row in rows:
        row_count += 1
        amount = Decimal(str(row["erp_amount"]))
        tax_amount = Decimal(str(row["tax_amount"]))
        received = int(row["received_quantity"])
        total += amount
        tax_total += tax_amount
        quantity += received
        key = (int(row["supplier_id"]), str(row["supplier_name"]))
        group = groups.setdefault(
            key,
            {
                "supplier_id": key[0],
                "supplier_name": key[1],
                "row_count": 0,
                "received_quantity": 0,
                "erp_amount": Decimal("0"),
                "tax_amount": Decimal("0"),
            },
        )
        group["row_count"] += 1
        group["received_quantity"] += received
        group["erp_amount"] += amount
        group["tax_amount"] += tax_amount
    return {
        "row_count": row_count,
        "received_quantity": quantity,
        "erp_amount": total.quantize(Decimal("0.01")),
        "tax_amount": tax_total.quantize(Decimal("0.01")),
        "by_supplier": [
            {
                **row,
                "erp_amount": row["erp_amount"].quantize(Decimal("0.01")),
                "tax_amount": row["tax_amount"].quantize(Decimal("0.01")),
            }
            for _key, row in sorted(groups.items())
        ],
    }


def _fact_amount_rows(
    facts: Iterable[SupplierReceiptSettlementPriceFact],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for fact in facts:
        breakdown = calculate_purchase_sheet_cost_breakdown(
            unit_price=fact.unit_price,
            price_unit=fact.price_unit,
            tax_included=fact.tax_included,
            tax_rate=fact.tax_rate,
            report_length_mm=fact.report_length_mm,
            report_width_mm=fact.report_width_mm,
        )
        quantity = Decimal(fact.received_quantity_snapshot)
        rows.append(
            {
                "incoming_receipt_item_id": int(fact.incoming_receipt_item_id),
                "supplier_id": int(fact.supplier_id),
                "supplier_name": fact.supplier_name_snapshot,
                "received_quantity": int(quantity),
                "erp_amount": (
                    breakdown.gross_per_sheet * quantity
                ).quantize(Decimal("0.01")),
                "tax_amount": (
                    breakdown.tax_per_sheet * quantity
                ).quantize(Decimal("0.01")),
            }
        )
    return rows


def _settlement_month_for_receipt(receipt_date: date) -> str:
    if receipt_date.day <= 20:
        return receipt_date.strftime("%Y-%m")
    if receipt_date.month == 12:
        return f"{receipt_date.year + 1:04d}-01"
    return f"{receipt_date.year:04d}-{receipt_date.month + 1:02d}"


def _verify_monthly_candidate_admission(
    db: Session,
    *,
    facts: Sequence[SupplierReceiptSettlementPriceFact],
    fact_rows: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    facts_by_item = {int(row.incoming_receipt_item_id): row for row in facts}
    amount_by_item = {
        int(row["incoming_receipt_item_id"]): row for row in fact_rows
    }
    expected_ids = set(facts_by_item)
    admitted: list[dict[str, Any]] = []
    months = sorted(
        {
            _settlement_month_for_receipt(row.receipt_date_snapshot)
            for row in facts
        }
    )
    for month in months:
        candidates, issues, _period_start, _period_end = scan_settlement_candidates(
            db, settlement_month=month
        )
        selected_issue_rows = [
            issue
            for issue in issues
            if str(issue.get("source_key") or "")
            in {f"paperboard:{item_id}" for item_id in expected_ids}
        ]
        if selected_issue_rows:
            raise AdoptionCliError(
                "采用事实仍被月结候选拒绝："
                + "; ".join(
                    f"{row.get('source_key')}:{row.get('code')}"
                    for row in selected_issue_rows
                )
            )
        for candidate in candidates:
            if candidate.incoming_receipt_item_id not in expected_ids:
                continue
            item_id = int(candidate.incoming_receipt_item_id)
            fact = facts_by_item[item_id]
            expected_amount = amount_by_item[item_id]
            if (
                candidate.supplier_receipt_price_fact_id != fact.id
                or candidate.supplier_id != fact.supplier_id
                or Decimal(candidate.erp_amount) != expected_amount["erp_amount"]
                or Decimal(candidate.tax_amount) != expected_amount["tax_amount"]
            ):
                raise AdoptionCliError(
                    f"收料明细 #{item_id} 的月结供应商或金额与采用事实不一致"
                )
            admitted.append(
                {
                    "incoming_receipt_item_id": item_id,
                    "supplier_receipt_price_fact_id": int(fact.id),
                    "supplier_id": int(candidate.supplier_id),
                    "supplier_name": candidate.supplier_name,
                    "received_quantity": int(candidate.received_quantity),
                    "erp_amount": Decimal(candidate.erp_amount),
                    "tax_amount": Decimal(candidate.tax_amount),
                    "settlement_month": month,
                }
            )
    admitted_ids = {int(row["incoming_receipt_item_id"]) for row in admitted}
    if admitted_ids != expected_ids or len(admitted) != len(expected_ids):
        missing = sorted(expected_ids - admitted_ids)
        raise AdoptionCliError(
            f"采用后仍有收料明细未进入对应供应商月结候选：{missing}"
        )
    return {
        "row_count": len(admitted),
        "settlement_months": months,
        "amount_summary": _amount_summary(admitted),
        "rows": admitted,
    }


def build_read_only_plan(
    database: Path,
    *,
    settlement_month: str | None = None,
    all_history: bool = False,
    batch_size: int = MAX_BATCH_SIZE,
) -> dict[str, Any]:
    database = database.resolve()
    if batch_size < 1 or batch_size > MAX_BATCH_SIZE:
        raise AdoptionCliError(f"batch_size 必须在 1 至 {MAX_BATCH_SIZE} 之间")
    heads = code_alembic_heads()
    if len(heads) != 1:
        raise AdoptionCliError(f"代码必须只有一个 Alembic head，当前为 {heads}")
    before = database_checks(database)
    _assert_healthy_database(before, expected_head=heads[0])
    engine = _read_only_engine(database)
    try:
        with Session(engine, expire_on_commit=False) as db:
            scope, label, start_utc, end_utc = _scope_bounds(
                db,
                settlement_month=settlement_month,
                all_history=all_history,
            )
            preview = preview_historical_price_adoptions(
                db,
                settlement_month=label,
                start_utc=start_utc,
                end_utc=end_utc,
            )
    finally:
        engine.dispose()
    after_sha = sha256_file(database)
    if after_sha != before["sha256"]:
        raise AdoptionCliError("只读扫描后数据库 SHA-256 发生变化，已停止")
    eligible = list(preview["eligible"])
    selected_rows = eligible[:batch_size]
    plan = {
        "schema": "tianming.p0_39.supplier_receipt_price_adoption_plan.v1",
        "mode": "dry-run",
        "writes_performed": False,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": scope,
        "settlement_month": label,
        "period_start_utc": start_utc.isoformat(),
        "period_end_utc": end_utc.isoformat(),
        "app_version": APP_VERSION,
        "code_alembic_head": heads[0],
        "database": before,
        "adoption_reason": HISTORICAL_ADOPTION_REASON,
        "plan_hash": preview["plan_hash"],
        "eligible_count": len(eligible),
        "rejected_count": len(preview["rejected"]),
        "selected_count": len(selected_rows),
        "remaining_after_selected_count": len(eligible) - len(selected_rows),
        "eligible": eligible,
        "rejected": list(preview["rejected"]),
        "selections": [
            {
                "incoming_receipt_item_id": int(row["incoming_receipt_item_id"]),
                "source_hash": str(row["source_hash"]).lower(),
            }
            for row in selected_rows
        ],
        "amount_summary": _amount_summary(selected_rows),
        "rollback_instructions": ROLLBACK_INSTRUCTIONS,
    }
    return _jsonable(plan)


CSV_FIELDS = [
    "record_status",
    "selected_for_batch",
    "incoming_receipt_item_id",
    "source_key",
    "receipt_number",
    "receipt_date",
    "source_kind",
    "purchase_document_number",
    "supplier_id",
    "supplier_name",
    "material_id",
    "material_code",
    "material_version",
    "received_quantity",
    "quantity_unit",
    "report_length_mm",
    "report_width_mm",
    "unit_price",
    "price_unit",
    "currency",
    "tax_included",
    "tax_rate",
    "shipping_fee_mode",
    "match_strategy",
    "erp_amount",
    "tax_amount",
    "source_hash",
    "rejection_code",
    "rejection_message",
    "recommended_action",
    "plan_hash",
    "adoption_reason",
]


def _csv_rows(plan: dict[str, Any]) -> Iterable[dict[str, Any]]:
    selected_ids = {
        int(row["incoming_receipt_item_id"]) for row in plan["selections"]
    }
    for row in plan["eligible"]:
        yield {
            **row,
            "record_status": "eligible",
            "selected_for_batch": (
                "yes"
                if int(row["incoming_receipt_item_id"]) in selected_ids
                else "no"
            ),
            "rejection_code": "",
            "rejection_message": "",
            "recommended_action": "",
            "plan_hash": plan["plan_hash"],
            "adoption_reason": plan["adoption_reason"],
        }
    for row in plan["rejected"]:
        yield {
            **row,
            "record_status": "rejected",
            "selected_for_batch": "no",
            "rejection_code": row.get("code", ""),
            "rejection_message": row.get("message", ""),
            "plan_hash": plan["plan_hash"],
            "adoption_reason": plan["adoption_reason"],
        }


def write_plan_artifacts(plan: dict[str, Any], output_dir: Path) -> PlanArtifacts:
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    stem = f"p0_39_supplier_receipt_price_adoption_{stamp}"
    json_path = output_dir / f"{stem}.plan.json"
    csv_path = output_dir / f"{stem}.plan.csv"
    encoded = json.dumps(
        _jsonable(plan), ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"
    json_path.write_text(encoded, encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in _csv_rows(plan):
            writer.writerow(_jsonable(row))
    return PlanArtifacts(
        json_path=json_path,
        csv_path=csv_path,
        json_sha256=sha256_file(json_path),
    )


def load_plan(path: Path, *, expected_sha256: str) -> tuple[dict[str, Any], str]:
    resolved = path.resolve()
    if not resolved.is_file():
        raise AdoptionCliError(f"计划 JSON 不存在：{resolved}")
    actual_sha = sha256_file(resolved)
    if actual_sha != _required_sha256(expected_sha256, "计划 JSON SHA-256"):
        raise AdoptionCliError(
            f"计划 JSON SHA-256 不匹配：{actual_sha} != {expected_sha256.lower()}"
        )
    try:
        plan = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AdoptionCliError(f"计划 JSON 无法读取：{error}") from error
    if plan.get("schema") != "tianming.p0_39.supplier_receipt_price_adoption_plan.v1":
        raise AdoptionCliError("计划 JSON schema 不受支持")
    if plan.get("mode") != "dry-run" or plan.get("writes_performed") is not False:
        raise AdoptionCliError("只接受由 dry-run 生成且零写入的计划 JSON")
    return plan, actual_sha


def _required_sha256(value: str, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if not SHA256_RE.fullmatch(normalized):
        raise AdoptionCliError(f"{label} 必须是 64 位十六进制 SHA-256")
    return normalized


def _validated_batch_key(value: str) -> str:
    key = str(value or "").strip()
    if not BATCH_KEY_RE.fullmatch(key):
        raise AdoptionCliError(
            "批次幂等键必须为 8—120 位，只能包含字母、数字、点、下划线、冒号或连字符"
        )
    return key


def _validate_plan_shape(plan: dict[str, Any]) -> list[tuple[int, str]]:
    eligible_by_id = {
        int(row["incoming_receipt_item_id"]): row for row in plan.get("eligible", [])
    }
    selections: list[tuple[int, str]] = []
    for raw in plan.get("selections", []):
        item_id = int(raw["incoming_receipt_item_id"])
        source_hash = _required_sha256(raw["source_hash"], "逐行 source_hash")
        eligible = eligible_by_id.get(item_id)
        if eligible is None or str(eligible.get("source_hash", "")).lower() != source_hash:
            raise AdoptionCliError(f"计划选择行 #{item_id} 与 eligible 明细不一致")
        selections.append((item_id, source_hash))
    if not selections or len(selections) > MAX_BATCH_SIZE:
        raise AdoptionCliError(f"每批必须选择 1 至 {MAX_BATCH_SIZE} 条计划")
    if len({item_id for item_id, _hash in selections}) != len(selections):
        raise AdoptionCliError("计划包含重复收料明细")
    if int(plan.get("selected_count", -1)) != len(selections):
        raise AdoptionCliError("计划 selected_count 与选择明细数量不一致")
    if int(plan.get("eligible_count", -1)) != len(eligible_by_id):
        raise AdoptionCliError("计划 eligible_count 与明细数量不一致")
    rejected = list(plan.get("rejected", []))
    if int(plan.get("rejected_count", -1)) != len(rejected):
        raise AdoptionCliError("计划 rejected_count 与拒绝明细数量不一致")
    if rejected:
        codes = sorted({str(row.get("code") or "UNKNOWN") for row in rejected})
        raise AdoptionCliError(
            f"计划仍有 {len(rejected)} 条阻断项（{', '.join(codes)}），禁止 apply"
        )
    return selections


def _load_operator(db: Session, username: str) -> User:
    normalized = str(username or "").strip()
    user = db.scalar(select(User).where(User.username == normalized))
    if user is None or not user.is_active:
        raise AdoptionCliError(f"操作员不存在或已停用：{normalized}")
    if user.role not in {"admin", "boss"}:
        raise AdoptionCliError("历史结算价格正式采用只允许 admin 或 boss 执行")
    return user


def _request_payload(
    *,
    batch_key: str,
    plan: dict[str, Any],
    plan_file_sha256: str,
    selections: Sequence[tuple[int, str]],
    expected_database_sha256: str,
    expected_app_version: str,
    expected_alembic_head: str,
    backup_reference: str,
    backup_sha256: str,
    operator_username: str,
) -> dict[str, Any]:
    return {
        "action": ACTION_CODE,
        "batch_idempotency_key": batch_key,
        "plan_file_sha256": plan_file_sha256,
        "plan_hash": plan["plan_hash"],
        "settlement_month": plan["settlement_month"],
        "period_start_utc": plan["period_start_utc"],
        "period_end_utc": plan["period_end_utc"],
        "selections": [
            {"incoming_receipt_item_id": item_id, "source_hash": source_hash}
            for item_id, source_hash in selections
        ],
        "expected_database_sha256": expected_database_sha256,
        "expected_app_version": expected_app_version,
        "expected_alembic_head": expected_alembic_head,
        "backup_reference": backup_reference,
        "backup_sha256": backup_sha256,
        "operator_username": operator_username,
        "adoption_reason": HISTORICAL_ADOPTION_REASON,
    }


def _existing_replay(
    db: Session,
    *,
    batch_key: str,
    request_hash: str,
    user: User,
) -> dict[str, Any] | None:
    record = db.scalar(
        select(FinanceIdempotencyRecord).where(
            FinanceIdempotencyRecord.idempotency_key == batch_key
        )
    )
    if record is None:
        return None
    if (
        record.action != ACTION_CODE
        or record.request_hash != request_hash
        or record.actor_user_id != user.id
    ):
        raise AdoptionCliError("该批次幂等键已经用于不同操作、内容或操作员")
    return json.loads(record.response_json)


def _verify_replay_facts(
    db: Session,
    *,
    selections: Sequence[tuple[int, str]],
    evidence: str,
) -> None:
    facts = list(
        db.scalars(
            select(SupplierReceiptSettlementPriceFact).where(
                SupplierReceiptSettlementPriceFact.incoming_receipt_item_id.in_(
                    [item_id for item_id, _source_hash in selections]
                )
            )
        ).all()
    )
    facts_by_item = {int(row.incoming_receipt_item_id): row for row in facts}
    for item_id, source_hash in selections:
        fact = facts_by_item.get(item_id)
        if (
            fact is None
            or fact.fact_origin != "historical_master_adoption"
            or fact.source_hash != source_hash
            or fact.adoption_reason != HISTORICAL_ADOPTION_REASON
            or fact.adoption_evidence_reference != evidence
        ):
            raise AdoptionCliError(
                f"批次回放记录存在，但收料明细 #{item_id} 的不可变采用事实缺失或不一致"
            )


def _formal_database_target(path: Path) -> bool:
    resolved = path.resolve()
    configured = os.getenv("ERP_DATABASE_PATH", "").strip()
    known = {(ROOT / "data" / FORMAL_DATABASE_NAME).resolve()}
    if configured:
        known.add(Path(configured).expanduser().resolve())
    return resolved in known or resolved.name.casefold() == FORMAL_DATABASE_NAME


def apply_plan_file(
    *,
    database: Path,
    plan_path: Path,
    expected_plan_sha256: str,
    batch_idempotency_key: str,
    expected_database_sha256: str,
    expected_app_version: str,
    expected_alembic_head: str,
    backup_path: Path,
    expected_backup_sha256: str,
    backup_reference: str,
    operator_username: str,
    confirmation_text: str,
    apply_confirmation: str,
    confirm_service_stopped: bool,
    allow_formal_database: bool,
) -> dict[str, Any]:
    database = database.resolve()
    backup_path = backup_path.resolve()
    if database == backup_path:
        raise AdoptionCliError("备份文件不能与目标数据库是同一个文件")
    if _formal_database_target(database) and not allow_formal_database:
        raise AdoptionCliError("正式数据库 apply 必须显式提供 --allow-formal-database")
    if apply_confirmation != APPLY_CONFIRMATION:
        raise AdoptionCliError(f"--apply 必须同时提供 --confirm-apply {APPLY_CONFIRMATION}")
    if confirmation_text.strip() != HISTORICAL_ADOPTION_REASON:
        raise AdoptionCliError(
            f"--confirm-adoption 必须逐字填写：{HISTORICAL_ADOPTION_REASON}"
        )
    if not confirm_service_stopped:
        raise AdoptionCliError("apply 前必须停止 ERP，并提供 --confirm-service-stopped")
    batch_key = _validated_batch_key(batch_idempotency_key)
    expected_db_sha = _required_sha256(expected_database_sha256, "预期数据库 SHA-256")
    expected_backup_sha = _required_sha256(
        expected_backup_sha256, "预期备份 SHA-256"
    )
    plan, plan_file_sha = load_plan(
        plan_path, expected_sha256=expected_plan_sha256
    )
    selections = _validate_plan_shape(plan)
    heads = code_alembic_heads()
    if heads != [expected_alembic_head]:
        raise AdoptionCliError(
            f"代码 Alembic head 不匹配：{heads} != {[expected_alembic_head]}"
        )
    if APP_VERSION != expected_app_version:
        raise AdoptionCliError(
            f"APP version 不匹配：{APP_VERSION} != {expected_app_version}"
        )
    if plan.get("app_version") != expected_app_version:
        raise AdoptionCliError("计划生成版本与本次预期 APP version 不一致")
    if plan.get("code_alembic_head") != expected_alembic_head:
        raise AdoptionCliError("计划生成 Alembic head 与本次预期 head 不一致")
    backup_checks = database_checks(backup_path)
    _assert_healthy_database(backup_checks, expected_head=expected_alembic_head)
    if backup_checks["sha256"] != expected_backup_sha:
        raise AdoptionCliError(
            f"备份 SHA-256 不匹配：{backup_checks['sha256']} != {expected_backup_sha}"
        )
    evidence = (
        f"{str(backup_reference or '').strip()}; "
        f"backup_sha256={expected_backup_sha}; batch={batch_key}"
    )
    if len(evidence) < 8 or len(evidence) > 255:
        raise AdoptionCliError("备份回执加批次信息后必须为 8—255 个字符")
    payload = _request_payload(
        batch_key=batch_key,
        plan=plan,
        plan_file_sha256=plan_file_sha,
        selections=selections,
        expected_database_sha256=expected_db_sha,
        expected_app_version=expected_app_version,
        expected_alembic_head=expected_alembic_head,
        backup_reference=evidence,
        backup_sha256=expected_backup_sha,
        operator_username=operator_username,
    )
    request_hash = _canonical_hash(payload)

    read_engine = _read_only_engine(database)
    try:
        with Session(read_engine, expire_on_commit=False) as db:
            operator = _load_operator(db, operator_username)
            replay = _existing_replay(
                db,
                batch_key=batch_key,
                request_hash=request_hash,
                user=operator,
            )
            if replay is not None:
                _verify_replay_facts(db, selections=selections, evidence=evidence)
    finally:
        read_engine.dispose()
    if replay is not None:
        current = database_checks(database)
        _assert_healthy_database(current, expected_head=expected_alembic_head)
        return {
            "status": "replayed",
            "replayed": True,
            "writes_performed": False,
            "batch_idempotency_key": batch_key,
            "request_hash": request_hash,
            "original_response": replay,
            "database": current,
            "rollback_instructions": ROLLBACK_INSTRUCTIONS,
        }

    before = database_checks(database)
    _assert_healthy_database(before, expected_head=expected_alembic_head)
    if before["sha256"] != expected_db_sha:
        raise AdoptionCliError(
            f"目标数据库 SHA-256 不匹配：{before['sha256']} != {expected_db_sha}"
        )
    if str(plan.get("database", {}).get("sha256", "")).lower() != expected_db_sha:
        raise AdoptionCliError("计划来源数据库 SHA-256 与本次目标数据库不一致")

    start_utc = datetime.fromisoformat(str(plan["period_start_utc"]))
    end_utc = datetime.fromisoformat(str(plan["period_end_utc"]))
    selected_ids = {item_id for item_id, _source_hash in selections}
    selected_rows = [
        row
        for row in plan["eligible"]
        if int(row["incoming_receipt_item_id"]) in selected_ids
    ]
    amount_summary = _amount_summary(selected_rows)
    engine = _apply_engine(database)
    business_response: dict[str, Any] | None = None
    try:
        with Session(engine, expire_on_commit=False) as db:
            try:
                db.execute(text("BEGIN IMMEDIATE"))
                locked_sha = sha256_file(database)
                if locked_sha != expected_db_sha:
                    raise AdoptionCliError(
                        "取得写锁后数据库 SHA-256 已变化，禁止继续 apply："
                        f"{locked_sha} != {expected_db_sha}"
                    )
                locked_revisions = [
                    str(row[0])
                    for row in db.execute(
                        text(
                            "SELECT version_num FROM alembic_version "
                            "ORDER BY version_num"
                        )
                    ).all()
                ]
                if locked_revisions != [expected_alembic_head]:
                    raise AdoptionCliError(
                        "取得写锁后 Alembic revision 已变化，禁止继续 apply："
                        f"{locked_revisions} != {[expected_alembic_head]}"
                    )
                operator = _load_operator(db, operator_username)
                replay = _existing_replay(
                    db,
                    batch_key=batch_key,
                    request_hash=request_hash,
                    user=operator,
                )
                if replay is not None:
                    _verify_replay_facts(
                        db, selections=selections, evidence=evidence
                    )
                    db.rollback()
                    business_response = replay
                else:
                    service_result = adopt_historical_price_facts(
                        db,
                        settlement_month=str(plan["settlement_month"]),
                        start_utc=start_utc,
                        end_utc=end_utc,
                        selections=selections,
                        confirmation_text=confirmation_text,
                        plan_hash=str(plan["plan_hash"]),
                        backup_reference=evidence,
                        user=operator,
                    )
                    facts = list(
                        db.scalars(
                            select(SupplierReceiptSettlementPriceFact).where(
                                SupplierReceiptSettlementPriceFact.id.in_(
                                    service_result["fact_ids"]
                                )
                            )
                        ).all()
                    )
                    facts_by_item = {
                        int(row.incoming_receipt_item_id): row for row in facts
                    }
                    for item_id, source_hash in selections:
                        fact = facts_by_item.get(item_id)
                        if (
                            fact is None
                            or fact.source_hash != source_hash
                            or fact.adoption_evidence_reference != evidence
                        ):
                            raise AdoptionCliError(
                                f"收料明细 #{item_id} 采用后事实校验失败，整批回滚"
                            )
                    fact_amount_rows = _fact_amount_rows(facts)
                    amount_after = _amount_summary(fact_amount_rows)
                    if _jsonable(amount_after) != _jsonable(amount_summary):
                        raise AdoptionCliError(
                            "采用后不可变事实金额与 dry-run 计划金额不一致，整批回滚"
                        )
                    candidate_verification = _verify_monthly_candidate_admission(
                        db,
                        facts=facts,
                        fact_rows=fact_amount_rows,
                    )
                    if _jsonable(
                        candidate_verification["amount_summary"]
                    ) != _jsonable(amount_after):
                        raise AdoptionCliError(
                            "采用后月结候选金额与不可变事实金额不一致，整批回滚"
                        )
                    business_response = _jsonable(
                        {
                            **service_result,
                            "status": "applied",
                            "batch_idempotency_key": batch_key,
                            "request_hash": request_hash,
                            "plan_file_sha256": plan_file_sha,
                            "amount_before": amount_summary,
                            "amount_after": amount_after,
                            "monthly_candidate_verification": candidate_verification,
                            "database_sha256_before": before["sha256"],
                            "backup": backup_checks,
                            "operator_username": operator.username,
                            "remaining_plan_rows": int(
                                plan.get("remaining_after_selected_count", 0)
                            ),
                        }
                    )
                    append_audit_event(
                        db,
                        event_category="business",
                        result="success",
                        source="script",
                        module_code="supplier_monthly_settlement",
                        action_code=ACTION_CODE,
                        resource="SupplierReceiptSettlementPriceFact",
                        legacy_action="historical_price_adopt",
                        actor=operator,
                        entity_type="supplier_receipt_price_fact",
                        entity_id=(
                            int(service_result["fact_ids"][0])
                            if service_result["fact_ids"]
                            else None
                        ),
                        object_ref=batch_key,
                        request_id=f"p0-39-{request_hash[:32]}",
                        batch_id=hashlib.sha256(batch_key.encode("utf-8")).hexdigest(),
                        description="受控采用旧收料供应商月结价格事实",
                        details={
                            "batch_idempotency_key": batch_key,
                            "request_hash": request_hash,
                            "plan_hash": plan["plan_hash"],
                            "plan_file_sha256": plan_file_sha,
                            "database_sha256_before": before["sha256"],
                            "backup_reference": evidence,
                            "backup_sha256": expected_backup_sha,
                            "selected_count": len(selections),
                            "amount_summary": amount_summary,
                            "adoption_reason": HISTORICAL_ADOPTION_REASON,
                        },
                    )
                    db.add(
                        FinanceIdempotencyRecord(
                            idempotency_key=batch_key,
                            request_hash=request_hash,
                            action=ACTION_CODE,
                            actor_user_id=operator.id,
                            resource_type=RESOURCE_TYPE,
                            resource_id=(
                                int(service_result["fact_ids"][0])
                                if service_result["fact_ids"]
                                else 0
                            ),
                            response_json=json.dumps(
                                business_response,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        )
                    )
                    db.commit()
            except Exception:
                db.rollback()
                raise
    except IntegrityError as error:
        raise AdoptionCliError(f"并发写入或唯一性门禁失败，整批已回滚：{error}") from error
    finally:
        engine.dispose()
    assert business_response is not None
    after = database_checks(database)
    _assert_healthy_database(after, expected_head=expected_alembic_head)
    return {
        "status": "applied",
        "replayed": False,
        "writes_performed": bool(business_response.get("writes_performed")),
        "batch_idempotency_key": batch_key,
        "request_hash": request_hash,
        "business_response": business_response,
        "database_before": before,
        "database_after": after,
        "rollback_instructions": ROLLBACK_INSTRUCTIONS,
    }


def _write_result(result: dict[str, Any], output_dir: Path) -> Path:
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = output_dir / f"p0_39_supplier_receipt_price_adoption_{stamp}.result.json"
    path.write_text(
        json.dumps(_jsonable(result), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="P0-39 旧收料供应商月结价格事实只读计划与受控采用"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--settlement-month")
    scope.add_argument("--all-history", action="store_true")
    parser.add_argument("--batch-size", type=int, default=MAX_BATCH_SIZE)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan-json", type=Path)
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--batch-idempotency-key")
    parser.add_argument("--expected-database-sha256")
    parser.add_argument("--expected-app-version")
    parser.add_argument("--expected-alembic-head")
    parser.add_argument("--backup-path", type=Path)
    parser.add_argument("--expected-backup-sha256")
    parser.add_argument("--backup-reference")
    parser.add_argument("--operator-username")
    parser.add_argument("--confirm-adoption")
    parser.add_argument("--confirm-apply")
    parser.add_argument("--confirm-service-stopped", action="store_true")
    parser.add_argument("--allow-formal-database", action="store_true")
    return parser


def _required_apply_argument(args: argparse.Namespace, name: str) -> Any:
    value = getattr(args, name)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise AdoptionCliError(f"--apply 必须提供 --{name.replace('_', '-')}")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not args.apply:
            plan = build_read_only_plan(
                args.database,
                settlement_month=args.settlement_month,
                all_history=args.all_history,
                batch_size=args.batch_size,
            )
            artifacts = write_plan_artifacts(plan, args.output_dir)
            output = {
                "status": "dry-run",
                "writes_performed": False,
                "plan_json": str(artifacts.json_path),
                "plan_csv": str(artifacts.csv_path),
                "plan_json_sha256": artifacts.json_sha256,
                "eligible_count": plan["eligible_count"],
                "rejected_count": plan["rejected_count"],
                "selected_count": plan["selected_count"],
                "amount_summary": plan["amount_summary"],
                "database": plan["database"],
                "app_version": plan["app_version"],
                "alembic_head": plan["code_alembic_head"],
                "rollback_instructions": ROLLBACK_INSTRUCTIONS,
            }
            print(json.dumps(_jsonable(output), ensure_ascii=False, indent=2))
            return 0
        result = apply_plan_file(
            database=args.database,
            plan_path=_required_apply_argument(args, "plan_json"),
            expected_plan_sha256=_required_apply_argument(
                args, "expected_plan_sha256"
            ),
            batch_idempotency_key=_required_apply_argument(
                args, "batch_idempotency_key"
            ),
            expected_database_sha256=_required_apply_argument(
                args, "expected_database_sha256"
            ),
            expected_app_version=_required_apply_argument(args, "expected_app_version"),
            expected_alembic_head=_required_apply_argument(
                args, "expected_alembic_head"
            ),
            backup_path=_required_apply_argument(args, "backup_path"),
            expected_backup_sha256=_required_apply_argument(
                args, "expected_backup_sha256"
            ),
            backup_reference=_required_apply_argument(args, "backup_reference"),
            operator_username=_required_apply_argument(args, "operator_username"),
            confirmation_text=_required_apply_argument(args, "confirm_adoption"),
            apply_confirmation=_required_apply_argument(args, "confirm_apply"),
            confirm_service_stopped=args.confirm_service_stopped,
            allow_formal_database=args.allow_formal_database,
        )
        result_path = _write_result(result, args.output_dir)
        output = {**result, "result_json": str(result_path)}
        print(json.dumps(_jsonable(output), ensure_ascii=False, indent=2))
        return 0
    except (AdoptionCliError, SupplierReceiptPriceFactError) as error:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "writes_performed": False,
                    "error": str(error),
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
