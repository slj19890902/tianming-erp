from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import unicodedata
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORT_DIR = ROOT / "docs" / "migration_reports"
APPLY_CONFIRMATION = "APPLY_HISTORICAL_MATERIAL_NORMALIZATION"
STOP_CONFIRMATION = "ERP_STOPPED_AND_DATABASE_UNLOCKED"

VALID_FLUTES: dict[int, frozenset[str]] = {
    3: frozenset({"A", "B", "E"}),
    5: frozenset({"AB", "BE"}),
    7: frozenset({"AAA", "ABC"}),
}
SUPPORTED_TABLES = (
    "historical_purchase_entries",
    "historical_requisition_maps",
)


class NormalizationError(RuntimeError):
    pass


class ReportPublicationError(NormalizationError):
    def __init__(self, message: str, cleanup_records: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.cleanup_records = cleanup_records


@dataclass(frozen=True, slots=True)
class ApprovedMapping:
    table_name: str
    row_id: int
    expected_material_code: str
    approved_material_code: str
    reviewer: str
    reviewed_at: str
    authority_override: str


@dataclass(frozen=True, slots=True)
class RowDecision:
    table_name: str
    row_id: int
    product_id: int | None
    old_material_code: str
    new_material_code: str | None
    status: str
    reason: str
    product_layer_count: int | None
    product_flute_type: str | None
    product_material_base: str | None
    product_code: str | None
    product_name: str | None
    source_workbook: str | None
    source_sheet: str | None
    source_row: int | None
    supplier_name: str | None
    product_reference: str | None
    search_text: str | None
    old_normalized_search_text: str | None = None
    new_normalized_search_text: str | None = None

    @property
    def will_update(self) -> bool:
        return self.status.startswith("planned_")

    @property
    def needs_review(self) -> bool:
        return self.status.startswith("review_") or self.status.startswith("conflict_")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def normalize_lookup_text(value: Any) -> str:
    text = str(value or "").strip().upper()
    text = text.replace("×", "*").replace("X", "*")
    return re.sub(r"[^0-9A-Z\u4e00-\u9fff]+", "", text)


def _plain_material(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().upper()


def _clean_flute(value: Any) -> str | None:
    text = _plain_material(value).replace(" ", "")
    text = re.sub(r"(?:瓦楞|楞|瓦)$", "", text)
    return text or None


def canonical_parts(value: Any) -> tuple[str, int, str] | None:
    text = _plain_material(value)
    match = re.fullmatch(r"([A-Z0-9]+)/(A|B|E|AB|BE|AAA|ABC)", text)
    if not match:
        return None
    base, flute = match.groups()
    layer_count = len(base)
    if flute not in VALID_FLUTES.get(layer_count, frozenset()):
        return None
    return base, layer_count, flute


def format_only_candidate(value: Any) -> tuple[str, int, str] | None:
    text = _plain_material(value).replace("／", "/")
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"(?:瓦楞|楞|瓦)$", "", text)
    match = re.fullmatch(r"([A-Z0-9]+)/(A|B|E|AB|BE|AAA|ABC)", text)
    if not match:
        return None
    base, flute = match.groups()
    layer_count = len(base)
    if flute not in VALID_FLUTES.get(layer_count, frozenset()):
        return None
    return f"{base}/{flute}", layer_count, flute


def product_authority(layer_count: Any, flute_type: Any) -> tuple[int, str] | None:
    try:
        layer = int(layer_count)
    except (TypeError, ValueError):
        return None
    flute = _clean_flute(flute_type)
    if flute not in VALID_FLUTES.get(layer, frozenset()):
        return None
    return layer, flute


def base_for_product(value: Any, layer_count: int) -> str | None:
    text = _plain_material(value).replace("／", "/")
    text = re.sub(r"\s+", "", text)
    match = re.match(rf"^([A-Z0-9]{{{layer_count}}})(.*)$", text)
    if not match:
        return None
    base, remainder = match.groups()
    # A second numeric fragment normally means a composite/multi-material source
    # row. Product layer/flute alone cannot identify which base is intended.
    if any(character.isdigit() for character in remainder):
        return None
    if remainder and remainder[0] not in "/-_":
        return None
    return base


def _evidence_base(value: Any, layer_count: int) -> str | None:
    text = _plain_material(value)
    text = re.sub(r"\s+", "", text)
    match = re.match(rf"^([A-Z0-9]{{{layer_count}}})(?:$|[/_-])", text)
    return match.group(1) if match else None


def product_material_base(row: sqlite3.Row, layer_count: int) -> str | None:
    candidates = {
        candidate
        for value in (row["product_default_material_code"], row["linked_material_code"])
        if (candidate := _evidence_base(value, layer_count)) is not None
    }
    return next(iter(candidates)) if len(candidates) == 1 else None


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone() is not None


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table_name}")')}


def _search_value(row: sqlite3.Row, material_code: str) -> str:
    searchable = " | ".join(
        value
        for value in (
            row["search_text"],
            row["product_reference"],
            material_code,
            row["supplier_name"] or "",
        )
        if value
    )
    return normalize_lookup_text(searchable)


def load_approved_mappings(path: Path | None) -> dict[tuple[str, int], ApprovedMapping]:
    if path is None:
        return {}
    if not path.is_file():
        raise NormalizationError(f"人工审批文件不存在：{path}")
    mappings: dict[tuple[str, int], ApprovedMapping] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "table_name",
            "row_id",
            "expected_material_code",
            "approved_material_code",
            "review_status",
            "reviewer",
            "reviewed_at",
            "authority_override",
        }
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise NormalizationError(f"人工审批文件缺少字段：{', '.join(sorted(missing))}")
        for line_number, row in enumerate(reader, 2):
            if (row.get("review_status") or "").strip().lower() != "approved":
                continue
            table_name = (row.get("table_name") or "").strip()
            if table_name not in SUPPORTED_TABLES:
                raise NormalizationError(f"审批文件第 {line_number} 行表名不受支持：{table_name}")
            try:
                row_id = int(row.get("row_id") or "")
            except ValueError as error:
                raise NormalizationError(f"审批文件第 {line_number} 行 row_id 无效") from error
            expected = row.get("expected_material_code") or ""
            approved = _plain_material(row.get("approved_material_code"))
            if canonical_parts(approved) is None:
                raise NormalizationError(
                    f"审批文件第 {line_number} 行目标材质不规范：{approved!r}"
                )
            reviewer = (row.get("reviewer") or "").strip()
            reviewed_at = (row.get("reviewed_at") or "").strip()
            authority_override = (row.get("authority_override") or "").strip().lower()
            if not reviewer or not reviewed_at:
                raise NormalizationError(
                    f"审批文件第 {line_number} 行缺少 reviewer/reviewed_at"
                )
            if authority_override not in {"", "approved/manual"}:
                raise NormalizationError(
                    f"审批文件第 {line_number} 行 authority_override 只能为空或 approved/manual"
                )
            key = (table_name, row_id)
            if key in mappings:
                raise NormalizationError(f"审批文件存在重复行：{table_name}#{row_id}")
            mappings[key] = ApprovedMapping(
                table_name=table_name,
                row_id=row_id,
                expected_material_code=expected,
                approved_material_code=approved,
                reviewer=reviewer,
                reviewed_at=reviewed_at,
                authority_override=authority_override,
            )
    return mappings


def _decide(
    *,
    table_name: str,
    row: sqlite3.Row,
    approved: ApprovedMapping | None,
) -> RowDecision:
    old = str(row["material_code"] or "")
    authority = product_authority(row["product_layer_count"], row["product_flute_type"])
    authority_base = product_material_base(row, authority[0]) if authority else None
    current = canonical_parts(old)
    normalized_before = (
        row["normalized_search_text"]
        if table_name == "historical_purchase_entries"
        else None
    )

    def result(status: str, reason: str, new: str | None = None) -> RowDecision:
        normalized_after = None
        if new is not None and table_name == "historical_purchase_entries":
            normalized_after = _search_value(row, new)
        return RowDecision(
            table_name=table_name,
            row_id=int(row["id"]),
            product_id=row["product_id"],
            old_material_code=old,
            new_material_code=new,
            status=status,
            reason=reason,
            product_layer_count=(authority[0] if authority else row["product_layer_count"]),
            product_flute_type=(authority[1] if authority else row["product_flute_type"]),
            product_material_base=authority_base,
            product_code=row["product_code"],
            product_name=row["product_name"],
            source_workbook=row["source_workbook"],
            source_sheet=row["source_sheet"],
            source_row=row["source_row"],
            supplier_name=row["supplier_name"],
            product_reference=row["product_reference"],
            search_text=row["search_text"],
            old_normalized_search_text=normalized_before,
            new_normalized_search_text=normalized_after,
        )

    if approved is not None:
        if approved.expected_material_code != old:
            return result("review_mapping_stale", "审批文件 expected_material_code 与当前值不一致")
        approved_parts = canonical_parts(approved.approved_material_code)
        assert approved_parts is not None
        if (
            authority
            and approved_parts[1:] != authority
            and approved.authority_override != "approved/manual"
        ):
            return result(
                "review_mapping_vs_product",
                "审批结果与关联常用箱冲突；需显式填写 authority_override=approved/manual",
            )
        if approved.approved_material_code == old:
            return result("unchanged_canonical", "审批值与当前规范值相同")
        return result(
            "planned_approved_mapping",
            (
                f"人工批准：{approved.reviewer} @ {approved.reviewed_at}"
                + (
                    "；显式以人工历史判断覆盖当前常用箱"
                    if approved.authority_override == "approved/manual"
                    else ""
                )
            ),
            approved.approved_material_code,
        )

    if current is not None and old != f"{current[0]}/{current[2]}":
        reason = "仅规范大小写、全角字符或首尾空格"
        status = "planned_format_normalization"
        if authority is not None and current[1:] != authority:
            status = "planned_format_normalization_product_warning"
            reason += "；规范后的历史值与当前常用箱不同，仍保留历史语义"
        return result(status, reason, f"{current[0]}/{current[2]}")

    if current is not None:
        if authority and current[1:] != authority:
            return result(
                "warning_canonical_vs_product",
                "历史值本身规范但与当前常用箱不同；保留历史值并报告警告",
            )
        return result("unchanged_canonical", "已符合层数/楞型规范")

    formatted = format_only_candidate(old)
    if formatted is not None:
        reason = "仅清理空格、全角符号或楞型文字"
        status = "planned_format_normalization"
        if authority is not None and formatted[1:] != authority:
            status = "planned_format_normalization_product_warning"
            reason += "；格式化后的历史值与当前常用箱不同，仍保留历史语义"
        return result(status, reason, formatted[0])

    if authority is not None and authority[0] in {3, 5}:
        base = base_for_product(old, authority[0])
        if base is not None and authority_base == base:
            return result(
                "planned_product_authority",
                "使用关联常用箱的明确 layer_count/flute_type 修正历史材质",
                f"{base}/{authority[1]}",
            )
        if base is not None and authority_base != base:
            return result(
                "review_product_material_base_mismatch",
                "历史材质基码与产品 default_material_code/关联材质基码不一致或产品基码证据冲突",
            )

    if authority is not None and authority[0] == 7:
        return result(
            "review_seven_layer_noncanonical",
            "七层历史值缺失或非法；本任务只放行 7位/AAA|ABC，不按产品自动补写",
        )
    if row["product_id"] is not None and authority is None:
        return result("review_product_authority_invalid", "关联常用箱缺少合法层数/楞型")
    return result("review_ambiguous", "无法从格式或关联常用箱唯一确定楞型")


def build_plan(
    connection: sqlite3.Connection,
    approved_mappings: dict[tuple[str, int], ApprovedMapping] | None = None,
    selected_tables: Iterable[str] | None = None,
) -> list[RowDecision]:
    connection.row_factory = sqlite3.Row
    approved_mappings = approved_mappings or {}
    selected = tuple(selected_tables or SUPPORTED_TABLES)
    unknown = set(selected) - set(SUPPORTED_TABLES)
    if unknown:
        raise NormalizationError(f"不支持的目标表：{', '.join(sorted(unknown))}")
    decisions: list[RowDecision] = []
    found_tables: set[str] = set()
    products_available = _table_exists(connection, "products")
    product_columns = _table_columns(connection, "products") if products_available else set()
    materials_available = _table_exists(connection, "materials")
    material_columns = _table_columns(connection, "materials") if materials_available else set()
    can_join_material = (
        products_available
        and "material_id" in product_columns
        and materials_available
        and {"id", "code"}.issubset(material_columns)
    )
    for table_name in selected:
        if not _table_exists(connection, table_name):
            continue
        found_tables.add(table_name)
        required = {"id", "product_id", "material_code"}
        columns = _table_columns(connection, table_name)
        if table_name == "historical_purchase_entries":
            required |= {
                "search_text",
                "product_reference",
                "supplier_name",
                "normalized_search_text",
            }
        missing = required - columns
        if missing:
            raise NormalizationError(
                f"{table_name} 缺少必要字段：{', '.join(sorted(missing))}"
            )
        extras = ""
        if table_name == "historical_purchase_entries":
            extras = (
                ", h.search_text, h.product_reference, h.supplier_name,"
                " h.normalized_search_text, h.source_workbook, h.source_sheet, h.source_row"
            )
        else:
            extras = (
                ", h.search_key AS search_text, h.search_key AS product_reference,"
                " NULL AS supplier_name, NULL AS normalized_search_text,"
                " h.source_workbook, h.source_sheet, h.source_row"
            )
        if products_available:
            default_material_expr = (
                "p.default_material_code"
                if "default_material_code" in product_columns
                else "NULL"
            )
            linked_material_expr = "m.code" if can_join_material else "NULL"
            product_code_expr = "p.product_code" if "product_code" in product_columns else "NULL"
            product_name_expr = "p.product_name" if "product_name" in product_columns else "NULL"
            material_join = "LEFT JOIN materials AS m ON m.id = p.material_id" if can_join_material else ""
            query = f"""
                SELECT h.id, h.product_id, h.material_code{extras},
                       p.layer_count AS product_layer_count,
                       p.flute_type AS product_flute_type,
                       {default_material_expr} AS product_default_material_code,
                       {linked_material_expr} AS linked_material_code,
                       {product_code_expr} AS product_code,
                       {product_name_expr} AS product_name
                FROM {table_name} AS h
                LEFT JOIN products AS p ON p.id = h.product_id
                {material_join}
                ORDER BY h.id
            """
        else:
            query = f"""
                SELECT h.id, h.product_id, h.material_code{extras},
                       NULL AS product_layer_count, NULL AS product_flute_type,
                       NULL AS product_default_material_code, NULL AS linked_material_code,
                       NULL AS product_code, NULL AS product_name
                FROM {table_name} AS h
                ORDER BY h.id
            """
        for row in connection.execute(query):
            key = (table_name, int(row["id"]))
            decisions.append(
                _decide(table_name=table_name, row=row, approved=approved_mappings.get(key))
            )
    if not found_tables:
        raise NormalizationError("数据库中不存在受支持的历史报料表")
    considered_mapping_keys = {
        key for key in approved_mappings if key[0] in set(selected)
    }
    unused = sorted(considered_mapping_keys - {(d.table_name, d.row_id) for d in decisions})
    if unused:
        rendered = ", ".join(f"{table}#{row_id}" for table, row_id in unused[:10])
        raise NormalizationError(f"审批文件引用了不存在的历史行：{rendered}")
    return decisions


@contextmanager
def _open_read_only(database: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        yield connection
    finally:
        connection.close()


def _summary(decisions: Iterable[RowDecision]) -> dict[str, Any]:
    rows = list(decisions)
    statuses: dict[str, int] = {}
    tables: dict[str, int] = {}
    for decision in rows:
        statuses[decision.status] = statuses.get(decision.status, 0) + 1
        tables[decision.table_name] = tables.get(decision.table_name, 0) + 1
    return {
        "total_rows": len(rows),
        "planned_updates": sum(d.will_update for d in rows),
        "needs_review": sum(d.needs_review for d in rows),
        "tables": tables,
        "statuses": statuses,
    }


def _decision_fingerprint(decisions: Iterable[RowDecision]) -> str:
    # The final exclusive-lock plan must prove that not only the proposed value,
    # but every authority/source input used to reach that decision stayed fixed.
    payload = [asdict(decision) for decision in decisions]
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _protected_snapshot(
    connection: sqlite3.Connection, decisions: Iterable[RowDecision]
) -> dict[tuple[str, int], tuple[Any, ...]]:
    snapshots: dict[tuple[str, int], tuple[Any, ...]] = {}
    for decision in decisions:
        if not decision.will_update:
            continue
        allowed = {"material_code"}
        if decision.table_name == "historical_purchase_entries":
            allowed.add("normalized_search_text")
        columns = sorted(_table_columns(connection, decision.table_name) - allowed)
        quoted = ", ".join(f'"{column}"' for column in columns)
        row = connection.execute(
            f'SELECT {quoted} FROM "{decision.table_name}" WHERE id=?',
            (decision.row_id,),
        ).fetchone()
        if row is None:
            raise NormalizationError(f"待更新行已消失：{decision.table_name}#{decision.row_id}")
        snapshots[(decision.table_name, decision.row_id)] = tuple(row)
    return snapshots


def _apply_cas(connection: sqlite3.Connection, decisions: list[RowDecision]) -> int:
    protected_before = _protected_snapshot(connection, decisions)
    updated = 0
    for decision in decisions:
        if not decision.will_update:
            continue
        if decision.table_name == "historical_purchase_entries":
            cursor = connection.execute(
                """
                UPDATE historical_purchase_entries
                SET material_code=?, normalized_search_text=?
                WHERE id=? AND material_code=? AND normalized_search_text=?
                """,
                (
                    decision.new_material_code,
                    decision.new_normalized_search_text,
                    decision.row_id,
                    decision.old_material_code,
                    decision.old_normalized_search_text,
                ),
            )
        else:
            cursor = connection.execute(
                """
                UPDATE historical_requisition_maps
                SET material_code=?
                WHERE id=? AND material_code=?
                """,
                (decision.new_material_code, decision.row_id, decision.old_material_code),
            )
        if cursor.rowcount != 1:
            raise NormalizationError(
                f"CAS 更新失败：{decision.table_name}#{decision.row_id} 已被并发修改"
            )
        updated += 1
    protected_after = _protected_snapshot(connection, decisions)
    if protected_after != protected_before:
        raise NormalizationError("原始来源/追溯字段发生意外变化，已回滚")
    return updated


def _verify_database(connection: sqlite3.Connection) -> dict[str, Any]:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    if integrity != "ok" or foreign_key_errors:
        raise NormalizationError(
            f"数据库验证失败：integrity={integrity!r}, foreign_key_errors={len(foreign_key_errors)}"
        )
    return {
        "integrity_check": integrity,
        "foreign_key_errors": 0,
        "journal_mode": journal_mode,
    }


def _online_backup(
    database: Path,
    backup_dir: Path,
    *,
    label: str = "before_historical_material_cleanup",
) -> tuple[Path, dict[str, Any]]:
    backup_dir = backup_dir.resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    if backup_dir == database.parent or backup_dir == database:
        # Same directory is allowed for the project backup convention, but the
        # resolved destination must still be a new file distinct from source.
        pass
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup = backup_dir / f"{database.stem}_{label}_{timestamp}.sqlite3"
    pending = backup_dir / f".{backup.name}.pending"
    if backup.exists() or pending.exists() or backup.resolve() == database:
        raise NormalizationError("备份目标无效或已存在")
    source: sqlite3.Connection | None = None
    destination: sqlite3.Connection | None = None
    try:
        try:
            source = sqlite3.connect(database)
            destination = sqlite3.connect(pending)
            source.backup(destination)
        finally:
            if destination is not None:
                destination.close()
            if source is not None:
                source.close()
        with _open_read_only(pending) as check:
            verification = _verify_database(check)
        pending_hash = sha256_file(pending)
        pending_size = pending.stat().st_size
        verification.update(
            {
                "path": str(backup),
                "sha256": pending_hash,
                "size": pending_size,
                "published_from": str(pending),
            }
        )
        os.replace(pending, backup)
        with _open_read_only(backup) as published_check:
            published_verification = _verify_database(published_check)
        published_hash = sha256_file(backup)
        published_size = backup.stat().st_size
        if published_hash != pending_hash or published_size != pending_size:
            raise NormalizationError("在线备份发布后最终路径的 SHA-256/大小不一致")
        verification["published_verification"] = published_verification
        verification["published_sha256"] = published_hash
        verification["published_size"] = published_size
        return backup, verification
    except BaseException as failure:
        cleanup_errors: list[str] = []
        for path in (pending, backup):
            try:
                path.unlink(missing_ok=True)
            except BaseException as cleanup_error:
                cleanup_errors.append(
                    f"{path}: {type(cleanup_error).__name__}: {cleanup_error}"
                )
        if cleanup_errors:
            raise NormalizationError(
                "在线备份失败且临时/最终文件无法清理；残留位置："
                + " | ".join(cleanup_errors)
            ) from failure
        raise


def _nonempty_transaction_sidecars(database: Path) -> list[Path]:
    sidecars = [
        Path(f"{database}-wal"),
        Path(f"{database}-shm"),
        Path(f"{database}-journal"),
    ]
    return [path for path in sidecars if path.is_file() and path.stat().st_size > 0]


def _write_failure_report(
    *,
    preferred_dir: Path,
    fallback_dir: Path,
    payload: dict[str, Any],
) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    last_error: OSError | None = None
    for directory in (preferred_dir, fallback_dir):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"HISTORICAL_MATERIAL_NORMALIZATION_FAILED_{timestamp}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return path
        except OSError as error:
            last_error = error
    raise NormalizationError(f"恢复完成但失败报告无法写入：{last_error}")


def _write_emergency_report(
    *,
    preferred_dir: Path,
    fallback_dir: Path,
    database_dir: Path,
    payload: dict[str, Any],
) -> tuple[Path | None, list[str]]:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    errors: list[str] = []
    attempted: list[Path] = []
    for directory in (preferred_dir, fallback_dir, database_dir):
        if directory in attempted:
            continue
        attempted.append(directory)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / (
                f"EMERGENCY_HISTORICAL_MATERIAL_NORMALIZATION_{timestamp}.json"
            )
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return path, errors
        except BaseException as error:
            errors.append(f"{directory}: {type(error).__name__}: {error}")
    return None, errors


def _safe_path_exists(path: Path) -> tuple[bool | None, str | None]:
    try:
        return path.exists(), None
    except BaseException as error:
        return None, f"{type(error).__name__}: {error}"


def _cleanup_report_paths(
    paths: Iterable[tuple[Path, str]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path, role in paths:
        rendered = str(path)
        if rendered in seen:
            continue
        seen.add(rendered)
        delete_error: str | None = None
        try:
            path.unlink(missing_ok=True)
        except BaseException as error:
            delete_error = f"{type(error).__name__}: {error}"
        exists_after, exists_error = _safe_path_exists(path)
        records.append(
            {
                "path": rendered,
                "role": role,
                "delete_error": delete_error,
                "exists_after_cleanup": exists_after,
                "exists_check_error": exists_error,
            }
        )
    return records


def _unresolved_report_records(
    records: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        dict(record)
        for record in records
        if record.get("exists_after_cleanup") is not False
    ]


def _merge_report_cleanup_records(
    *groups: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        for record in group:
            key = str(record.get("path") or "")
            if not key:
                continue
            previous = merged.get(key)
            if previous is None:
                merged[key] = dict(record)
                continue
            # Prefer the latest observable state, while retaining the first
            # deletion error that explains why this artifact became suspect.
            combined = dict(previous)
            combined.update(record)
            if previous.get("delete_error") and not record.get("delete_error"):
                combined["delete_error"] = previous["delete_error"]
            merged[key] = combined
    return list(merged.values())


def _invalidate_report_artifacts(
    *,
    records: list[dict[str, Any]],
    database: Path,
    restored_sha256: str,
    failure: BaseException,
) -> list[dict[str, Any]]:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    results: list[dict[str, Any]] = []
    for original in records:
        record = dict(original)
        path = Path(str(record["path"]))
        tombstone_payload = {
            "status": "INVALIDATED_APPLY_REPORT",
            "valid": False,
            "prohibit_erp_start": True,
            "instruction": "该 APPLY 报告已失效；数据库已进入恢复/紧急处置流程，禁止据此认定清洗成功。",
            "invalidated_report": str(path),
            "database": str(database),
            "restored_sha256": restored_sha256,
            "failure": f"{type(failure).__name__}: {failure}",
        }
        is_authoritative_json = path.suffix.lower() == ".json"
        target = path if is_authoritative_json else Path(f"{path}.INVALIDATED.json")
        pending = target.with_name(f".{target.name}.{timestamp}.pending")
        try:
            pending.write_text(
                json.dumps(tombstone_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(pending, target)
            record["tombstone_path"] = str(target)
            record["authoritative_json_replaced"] = is_authoritative_json
            record["tombstone_error"] = None
        except BaseException as error:
            record["tombstone_path"] = str(target)
            record["authoritative_json_replaced"] = False
            record["tombstone_error"] = f"{type(error).__name__}: {error}"
            try:
                pending.unlink(missing_ok=True)
            except BaseException as cleanup_error:
                record["tombstone_pending_cleanup_error"] = (
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
        exists_after, exists_error = _safe_path_exists(path)
        record["artifact_exists_after_invalidation"] = exists_after
        record["artifact_exists_check_error"] = exists_error
        results.append(record)
    return results


def _raise_restore_emergency(
    *,
    database: Path,
    restore_temp: Path,
    backup_path: Path,
    backup_info: dict[str, Any],
    output_dir: Path,
    backup_dir: Path,
    original_failure: BaseException,
    restore_stage: str,
    restore_error: BaseException,
    sidecars: list[dict[str, Any]],
    compensate_sidecars: bool,
    main_replace_state: str,
    emergency_status: str = "EMERGENCY_RESTORE_FAILED",
    extra_evidence: dict[str, Any] | None = None,
) -> None:
    for record in reversed(sidecars):
        if not record.get("moved_to_quarantine"):
            continue
        original = Path(record["original"])
        quarantine = Path(record["quarantine"])
        if compensate_sidecars:
            try:
                os.replace(quarantine, original)
                record["compensation_restored"] = True
                record["compensation_error"] = None
            except BaseException as error:
                record["compensation_restored"] = False
                record["compensation_error"] = f"{type(error).__name__}: {error}"
        else:
            record["compensation_restored"] = False
            record["compensation_error"] = None
            record["compensation_skipped_reason"] = (
                "主数据库可能或已经替换；旧事务 sidecar 必须继续隔离，禁止重新附着"
            )
        original_exists, original_exists_error = _safe_path_exists(original)
        quarantine_exists, quarantine_exists_error = _safe_path_exists(quarantine)
        record["original_exists_after_compensation"] = original_exists
        record["original_exists_error"] = original_exists_error
        record["quarantine_exists_after_compensation"] = quarantine_exists
        record["quarantine_exists_error"] = quarantine_exists_error

    database_exists, database_exists_error = _safe_path_exists(database)
    restore_temp_exists, restore_temp_exists_error = _safe_path_exists(restore_temp)

    payload = {
        "status": emergency_status,
        "prohibit_erp_start": True,
        "instruction": "禁止启动 ERP；必须由人工核对正式库、恢复临时文件、备份和全部 sidecar 后再决定恢复。",
        "database": str(database),
        "database_exists": database_exists,
        "database_exists_error": database_exists_error,
        "restore_temp": str(restore_temp),
        "restore_temp_exists": restore_temp_exists,
        "restore_temp_exists_error": restore_temp_exists_error,
        "pre_apply_backup_path": str(backup_path),
        "pre_apply_backup": backup_info,
        "original_failure": f"{type(original_failure).__name__}: {original_failure}",
        "restore_stage": restore_stage,
        "restore_error": f"{type(restore_error).__name__}: {restore_error}",
        "main_replace_state": main_replace_state,
        "sidecar_compensation_attempted": compensate_sidecars,
        "sidecars": sidecars,
    }
    if extra_evidence:
        payload["extra_evidence"] = extra_evidence
    emergency_report, report_errors = _write_emergency_report(
        preferred_dir=output_dir,
        fallback_dir=backup_dir,
        database_dir=database.parent,
        payload=payload,
    )
    report_text = (
        str(emergency_report)
        if emergency_report is not None
        else "无法写入；尝试结果=" + " | ".join(report_errors)
    )
    prefix = (
        "自动恢复失败，禁止启动 ERP；"
        if emergency_status == "EMERGENCY_RESTORE_FAILED"
        else "报告失效标记或清理失败，禁止启动 ERP；"
    )
    raise NormalizationError(
        prefix
        + f"database={database}；restore_temp={restore_temp}；backup={backup_path}；"
        f"stage={restore_stage}；main_replace_state={main_replace_state}；"
        f"sidecars={json.dumps(sidecars, ensure_ascii=False)}；"
        f"EMERGENCY 报告={report_text}"
    ) from restore_error


def _restore_verified_backup_after_failure(
    *,
    database: Path,
    backup_path: Path,
    backup_info: dict[str, Any],
    backup_dir: Path,
    output_dir: Path,
    failure: BaseException,
    locked_plan: list[RowDecision],
    report_cleanup_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    restore_temp = database.with_name(f".{database.name}.restore-{timestamp}.tmp")
    sidecar_records: list[dict[str, Any]] = []
    requested_backup_dir = backup_dir

    try:
        backup_dir = backup_dir.resolve()
        backup_dir.mkdir(parents=True, exist_ok=True)
    except BaseException as workspace_error:
        _raise_restore_emergency(
            database=database,
            restore_temp=restore_temp,
            backup_path=backup_path,
            backup_info=backup_info,
            output_dir=output_dir,
            backup_dir=requested_backup_dir,
            original_failure=failure,
            restore_stage="restore_workspace_prepare",
            restore_error=workspace_error,
            sidecars=sidecar_records,
            compensate_sidecars=False,
            main_replace_state="not_started",
        )

    failed_raw = backup_dir / f"{database.stem}_FAILED_RAW_{timestamp}.sqlite3"
    try:
        if restore_temp.exists() or failed_raw.exists():
            raise NormalizationError("自动恢复目标发生命名冲突，未覆盖任何文件")
    except BaseException as naming_error:
        _raise_restore_emergency(
            database=database,
            restore_temp=restore_temp,
            backup_path=backup_path,
            backup_info=backup_info,
            output_dir=output_dir,
            backup_dir=backup_dir,
            original_failure=failure,
            restore_stage="restore_target_preflight",
            restore_error=naming_error,
            sidecars=sidecar_records,
            compensate_sidecars=False,
            main_replace_state="not_started",
        )
    try:
        shutil.copy2(backup_path, restore_temp)
    except BaseException as copy_error:
        _raise_restore_emergency(
            database=database,
            restore_temp=restore_temp,
            backup_path=backup_path,
            backup_info=backup_info,
            output_dir=output_dir,
            backup_dir=backup_dir,
            original_failure=failure,
            restore_stage="restore_temp_copy",
            restore_error=copy_error,
            sidecars=sidecar_records,
            compensate_sidecars=False,
            main_replace_state="not_started",
        )
    try:
        restore_temp_hash = sha256_file(restore_temp)
        if restore_temp_hash != backup_info["sha256"]:
            raise NormalizationError("自动恢复临时文件哈希与已验证备份不一致")
    except BaseException as hash_error:
        _raise_restore_emergency(
            database=database,
            restore_temp=restore_temp,
            backup_path=backup_path,
            backup_info=backup_info,
            output_dir=output_dir,
            backup_dir=backup_dir,
            original_failure=failure,
            restore_stage="restore_temp_hash_verification",
            restore_error=hash_error,
            sidecars=sidecar_records,
            compensate_sidecars=False,
            main_replace_state="not_started",
        )
    try:
        with _open_read_only(restore_temp) as restore_check:
            _verify_database(restore_check)
    except BaseException as integrity_error:
        _raise_restore_emergency(
            database=database,
            restore_temp=restore_temp,
            backup_path=backup_path,
            backup_info=backup_info,
            output_dir=output_dir,
            backup_dir=backup_dir,
            original_failure=failure,
            restore_stage="restore_temp_integrity_verification",
            restore_error=integrity_error,
            sidecars=sidecar_records,
            compensate_sidecars=False,
            main_replace_state="not_started",
        )

    # Preserve failed state on a best-effort basis. Neither an unreadable failed
    # database nor a snapshot storage error is allowed to block restoration.
    failed_raw_path: str | None = None
    failed_raw_error: str | None = None
    try:
        shutil.copy2(database, failed_raw)
        failed_raw_path = str(failed_raw)
    except BaseException as error:
        failed_raw_error = f"{type(error).__name__}: {error}"

    failed_snapshot: Path | None = None
    failed_snapshot_info: dict[str, Any] | None = None
    failed_snapshot_error: str | None = None
    try:
        failed_snapshot, failed_snapshot_info = _online_backup(
            database,
            backup_dir,
            label="FAILED_COMMITTED_HISTORY_MATERIAL",
        )
    except BaseException as error:
        failed_snapshot_error = f"{type(error).__name__}: {error}"

    # Old transaction sidecars must never be allowed to attach to the restored
    # main file. Track every original/quarantine location for compensation and
    # emergency handoff if replacement cannot complete.
    try:
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(f"{database}{suffix}")
            target = backup_dir / f"{failed_raw.name}{suffix}"
            record: dict[str, Any] = {
                "suffix": suffix,
                "original": str(sidecar),
                "quarantine": str(target),
                "existed_before_isolation": sidecar.exists(),
                "moved_to_quarantine": False,
                "isolation_error": None,
                "compensation_restored": None,
                "compensation_error": None,
            }
            sidecar_records.append(record)
    except BaseException as inventory_error:
        _raise_restore_emergency(
            database=database,
            restore_temp=restore_temp,
            backup_path=backup_path,
            backup_info=backup_info,
            output_dir=output_dir,
            backup_dir=backup_dir,
            original_failure=failure,
            restore_stage="sidecar_inventory",
            restore_error=inventory_error,
            sidecars=sidecar_records,
            compensate_sidecars=False,
            main_replace_state="not_started",
        )

    for record in sidecar_records:
        sidecar = Path(record["original"])
        target = Path(record["quarantine"])
        if not record["existed_before_isolation"]:
            continue
        try:
            os.replace(sidecar, target)
            record["moved_to_quarantine"] = True
        except BaseException as isolation_error:
            record["isolation_error"] = (
                f"{type(isolation_error).__name__}: {isolation_error}"
            )
            _raise_restore_emergency(
                database=database,
                restore_temp=restore_temp,
                backup_path=backup_path,
                backup_info=backup_info,
                output_dir=output_dir,
                backup_dir=backup_dir,
                original_failure=failure,
                restore_stage="sidecar_isolation",
                restore_error=isolation_error,
                sidecars=sidecar_records,
                compensate_sidecars=True,
                main_replace_state="not_replaced",
            )

    # The production path remains present until this single atomic replacement.
    try:
        os.replace(restore_temp, database)
    except BaseException as replacement_error:
        restore_temp_exists, restore_temp_exists_error = _safe_path_exists(restore_temp)
        database_hash_after_error: str | None = None
        database_hash_error: str | None = None
        try:
            database_hash_after_error = sha256_file(database)
        except BaseException as hash_error:
            database_hash_error = f"{type(hash_error).__name__}: {hash_error}"
        definitely_not_replaced = (
            restore_temp_exists is True
            and database_hash_after_error is not None
            and database_hash_after_error != backup_info["sha256"]
        )
        replace_state = (
            "not_replaced"
            if definitely_not_replaced
            else "replace_outcome_unknown_or_replaced"
        )
        _raise_restore_emergency(
            database=database,
            restore_temp=restore_temp,
            backup_path=backup_path,
            backup_info=backup_info,
            output_dir=output_dir,
            backup_dir=backup_dir,
            original_failure=failure,
            restore_stage="main_database_atomic_replace",
            restore_error=replacement_error,
            sidecars=sidecar_records,
            compensate_sidecars=definitely_not_replaced,
            main_replace_state=replace_state,
            extra_evidence={
                "restore_temp_exists_after_replace_error": restore_temp_exists,
                "restore_temp_exists_error": restore_temp_exists_error,
                "database_hash_after_replace_error": database_hash_after_error,
                "database_hash_error": database_hash_error,
            },
        )
    try:
        with _open_read_only(database) as restored:
            restored_verification = _verify_database(restored)
    except BaseException as post_replace_integrity_error:
        _raise_restore_emergency(
            database=database,
            restore_temp=restore_temp,
            backup_path=backup_path,
            backup_info=backup_info,
            output_dir=output_dir,
            backup_dir=backup_dir,
            original_failure=failure,
            restore_stage="post_replace_integrity_verification",
            restore_error=post_replace_integrity_error,
            sidecars=sidecar_records,
            compensate_sidecars=False,
            main_replace_state="replaced_unverified",
        )
    try:
        restored_hash = sha256_file(database)
        if restored_hash != backup_info["sha256"]:
            raise NormalizationError(
                "自动恢复后的数据库哈希与已验证备份不一致；失败原库和备份均已保留"
            )
    except BaseException as post_replace_hash_error:
        _raise_restore_emergency(
            database=database,
            restore_temp=restore_temp,
            backup_path=backup_path,
            backup_info=backup_info,
            output_dir=output_dir,
            backup_dir=backup_dir,
            original_failure=failure,
            restore_stage="post_replace_hash_verification",
            restore_error=post_replace_hash_error,
            sidecars=sidecar_records,
            compensate_sidecars=False,
            main_replace_state="replaced_unverified",
        )
    payload = {
        "status": "apply_failed_and_backup_restored",
        "failure": f"{type(failure).__name__}: {failure}",
        "database": str(database),
        "restored_sha256": restored_hash,
        "restored_verification": restored_verification,
        "pre_apply_backup": backup_info,
        "failed_consistent_snapshot": failed_snapshot_info,
        "failed_consistent_snapshot_path": str(failed_snapshot) if failed_snapshot else None,
        "failed_consistent_snapshot_error": failed_snapshot_error,
        "failed_raw_main": failed_raw_path,
        "failed_raw_error": failed_raw_error,
        "failed_raw_sidecars": [
            record["quarantine"]
            for record in sidecar_records
            if record["moved_to_quarantine"]
        ],
        "sidecars": sidecar_records,
        "report_cleanup_records": report_cleanup_records or [],
        "locked_plan": [asdict(item) for item in locked_plan],
    }
    unresolved_reports = _unresolved_report_records(report_cleanup_records or [])
    if unresolved_reports:
        invalidated_reports = _invalidate_report_artifacts(
            records=unresolved_reports,
            database=database,
            restored_sha256=restored_hash,
            failure=failure,
        )
        report_cleanup_error = NormalizationError(
            "数据库已恢复，但 APPLY 报告或临时报告无法删除，必须人工核对失效标记"
        )
        _raise_restore_emergency(
            database=database,
            restore_temp=restore_temp,
            backup_path=backup_path,
            backup_info=backup_info,
            output_dir=output_dir,
            backup_dir=backup_dir,
            original_failure=failure,
            restore_stage="report_artifact_cleanup_after_restore",
            restore_error=report_cleanup_error,
            sidecars=sidecar_records,
            compensate_sidecars=False,
            main_replace_state="replaced_and_verified",
            emergency_status="EMERGENCY_REPORT_CLEANUP_FAILED",
            extra_evidence={
                "database_restore_verified": True,
                "restored_sha256": restored_hash,
                "report_artifacts": invalidated_reports,
            },
        )
    try:
        failure_report = _write_failure_report(
            preferred_dir=output_dir.resolve(),
            fallback_dir=backup_dir,
            payload=payload,
        )
    except BaseException as failure_report_error:
        _raise_restore_emergency(
            database=database,
            restore_temp=restore_temp,
            backup_path=backup_path,
            backup_info=backup_info,
            output_dir=output_dir,
            backup_dir=backup_dir,
            original_failure=failure,
            restore_stage="failure_report_publish_after_restore",
            restore_error=failure_report_error,
            sidecars=sidecar_records,
            compensate_sidecars=False,
            main_replace_state="replaced_and_verified",
            extra_evidence={
                "database_restore_verified": True,
                "restored_sha256": restored_hash,
                "recovery_payload": payload,
            },
        )
    payload["failure_report"] = str(failure_report)
    return payload


def _commit_transaction(connection: sqlite3.Connection) -> None:
    """Small seam used by fault-injection tests around durable commit."""
    connection.commit()


def _persistent_change_after_transaction_failure(
    *,
    database: Path,
    database_sha256_before: str,
    plan_before: list[RowDecision],
    approved: dict[tuple[str, int], ApprovedMapping],
    selected_tables: tuple[str, ...],
) -> tuple[bool, dict[str, Any]]:
    evidence: dict[str, Any] = {}
    try:
        current_hash = sha256_file(database)
        evidence["database_sha256"] = current_hash
        evidence["hash_changed"] = current_hash != database_sha256_before
    except BaseException as error:
        evidence["hash_error"] = f"{type(error).__name__}: {error}"
        return True, evidence

    try:
        with _open_read_only(database) as check:
            _verify_database(check)
            current_plan = build_plan(check, approved, selected_tables)
        evidence["plan_changed"] = (
            _decision_fingerprint(current_plan) != _decision_fingerprint(plan_before)
        )
    except BaseException as error:
        evidence["plan_error"] = f"{type(error).__name__}: {error}"
        return True, evidence
    return bool(evidence["hash_changed"] or evidence["plan_changed"]), evidence


def write_reports(
    *,
    output_dir: Path,
    mode: str,
    database: Path,
    database_sha256_before: str,
    database_sha256_after: str,
    decisions: list[RowDecision],
    verification: dict[str, Any],
    backup: dict[str, Any] | None,
    approval: dict[str, Any] | None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    stem = f"HISTORICAL_MATERIAL_NORMALIZATION_{mode}_{timestamp}"
    final_paths = {
        "json": output_dir / f"{stem}.json",
        "csv": output_dir / f"{stem}.csv",
        "review_csv": output_dir / f"{stem}_REVIEW.csv",
        "markdown": output_dir / f"{stem}.md",
    }
    json_path = output_dir / f".{stem}.pending.json"
    csv_path = output_dir / f".{stem}.pending.csv"
    review_path = output_dir / f".{stem}_REVIEW.pending.csv"
    markdown_path = output_dir / f".{stem}.pending.md"
    summary = _summary(decisions)
    payload = {
        "mode": mode,
        "database": str(database),
        "database_sha256_before": database_sha256_before,
        "database_sha256_after": database_sha256_after,
        "summary": summary,
        "verification": verification,
        "backup": backup,
        "approval": approval,
        "decisions": [asdict(decision) for decision in decisions],
    }
    fields = list(asdict(decisions[0]).keys()) if decisions else list(RowDecision.__annotations__)
    review_fields = [
        "table_name",
        "row_id",
        "expected_material_code",
        "approved_material_code",
        "review_status",
        "reviewer",
        "reviewed_at",
        "authority_override",
        "reason",
        "product_id",
        "product_layer_count",
        "product_flute_type",
        "product_material_base",
        "product_code",
        "product_name",
        "source_workbook",
        "source_sheet",
        "source_row",
        "supplier_name",
        "product_reference",
        "search_text",
    ]
    lines = [
        f"# 历史报料材质规范化 {mode}",
        "",
        f"- 数据库：`{database}`",
        f"- 执行前 SHA-256：`{database_sha256_before}`",
        f"- 执行后 SHA-256：`{database_sha256_after}`",
        f"- 总行数：{summary['total_rows']}",
        f"- 计划/已更新：{summary['planned_updates']}",
        f"- 待人工复核：{summary['needs_review']}",
        f"- 完整性：{verification.get('integrity_check')}",
        f"- 外键异常：{verification.get('foreign_key_errors')}",
        "",
        "## 状态统计",
        "",
    ]
    lines.extend(f"- `{key}`：{value}" for key, value in sorted(summary["statuses"].items()))
    if backup:
        lines.extend(["", "## 备份", "", f"- `{backup['path']}`", f"- SHA-256：`{backup['sha256']}`"])
    if approval:
        lines.extend(
            [
                "",
                "## 人工审批文件",
                "",
                f"- 路径：`{approval['path']}`",
                f"- SHA-256：`{approval['sha256']}`",
                f"- 已批准：{approval['approved_count']}",
            ]
        )
    pending_paths = {
        "csv": csv_path,
        "review_csv": review_path,
        "markdown": markdown_path,
        # JSON is the authoritative success record and is published last.
        "json": json_path,
    }
    all_report_paths = [*pending_paths.values(), *final_paths.values()]
    collisions = [str(path) for path in all_report_paths if path.exists()]
    if collisions:
        raise NormalizationError(
            "报告暂存/发布目标已存在，拒绝覆盖：" + " | ".join(collisions)
        )
    published: list[Path] = []
    try:
        # Every staging write is inside the same cleanup boundary as atomic
        # publication. A partial file from any writer must never escape as an
        # apparently usable report after a committed APPLY is rolled back.
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(asdict(decision) for decision in decisions)
        with review_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=review_fields)
            writer.writeheader()
            for decision in decisions:
                if not decision.needs_review:
                    continue
                writer.writerow(
                    {
                        "table_name": decision.table_name,
                        "row_id": decision.row_id,
                        "expected_material_code": decision.old_material_code,
                        "approved_material_code": "",
                        "review_status": "pending",
                        "reviewer": "",
                        "reviewed_at": "",
                        "authority_override": "",
                        "reason": decision.reason,
                        "product_id": decision.product_id or "",
                        "product_layer_count": decision.product_layer_count or "",
                        "product_flute_type": decision.product_flute_type or "",
                        "product_material_base": decision.product_material_base or "",
                        "product_code": decision.product_code or "",
                        "product_name": decision.product_name or "",
                        "source_workbook": decision.source_workbook or "",
                        "source_sheet": decision.source_sheet or "",
                        "source_row": decision.source_row or "",
                        "supplier_name": decision.supplier_name or "",
                        "product_reference": decision.product_reference or "",
                        "search_text": decision.search_text or "",
                    }
                )
        markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        for key, pending in pending_paths.items():
            # Record the destination before replace so a platform/filesystem
            # that completes the move and then raises is also cleaned safely.
            published.append(final_paths[key])
            os.replace(pending, final_paths[key])
    except BaseException as report_error:
        # These names are unique to this invocation. Remove partial output so a
        # restored apply cannot be mistaken for a successful apply report.
        cleanup_records = _cleanup_report_paths(
            [
                *((path, "published_success_report") for path in published),
                *((path, "pending_report") for path in pending_paths.values()),
            ]
        )
        unresolved = _unresolved_report_records(cleanup_records)
        if unresolved:
            raise ReportPublicationError(
                "报告暂存/发布失败且部分成功/临时报告无法删除："
                + json.dumps(unresolved, ensure_ascii=False, sort_keys=True),
                cleanup_records,
            ) from report_error
        raise
    return final_paths


def _audit_before_return(
    *,
    database: Path,
    mode: str,
    database_sha256_before: str,
    database_sha256_after: str,
    database_stat_after: Any,
    reports: dict[str, Path],
) -> dict[str, Any]:
    required = {"json", "csv", "review_csv", "markdown"}
    if set(reports) != required:
        raise NormalizationError("返回前审计发现报告集合不完整")
    report_sizes: dict[str, int] = {}
    for key, path in reports.items():
        report_stat = path.stat()
        if not path.is_file() or report_stat.st_size <= 0:
            raise NormalizationError(f"返回前审计发现报告缺失或为空：{path}")
        report_sizes[key] = report_stat.st_size
    payload = json.loads(reports["json"].read_text(encoding="utf-8"))
    expected_pairs = {
        "mode": mode,
        "database": str(database),
        "database_sha256_before": database_sha256_before,
        "database_sha256_after": database_sha256_after,
    }
    for field, expected in expected_pairs.items():
        if payload.get(field) != expected:
            raise NormalizationError(f"返回前审计发现 JSON 报告字段不一致：{field}")
    current_hash = sha256_file(database)
    current_stat = database.stat()
    if current_hash != database_sha256_after:
        raise NormalizationError("返回前审计发现数据库 SHA-256 在报告生成后变化")
    if (
        current_stat.st_size != database_stat_after.st_size
        or current_stat.st_mtime_ns != database_stat_after.st_mtime_ns
    ):
        raise NormalizationError("返回前审计发现数据库文件状态在报告生成后变化")
    return {
        "database_sha256": current_hash,
        "database_size": current_stat.st_size,
        "database_mtime_ns": current_stat.st_mtime_ns,
        "report_sizes": report_sizes,
    }


def _discard_published_reports(
    reports: dict[str, Path] | None,
) -> list[dict[str, Any]]:
    return _cleanup_report_paths(
        (path, "published_success_report") for path in (reports or {}).values()
    )


def execute(args: argparse.Namespace) -> dict[str, Any]:
    requested_database = args.database.expanduser()
    if requested_database.is_symlink():
        raise NormalizationError(f"数据库不能是符号链接：{requested_database}")
    database = requested_database.resolve()
    if not database.is_file():
        raise NormalizationError(f"数据库必须是存在的普通文件且不能是符号链接：{database}")
    before_stat = database.stat()
    before_hash = sha256_file(database)
    approved = load_approved_mappings(args.approved_mapping)
    approval_info = None
    if args.approved_mapping is not None:
        approval_path = args.approved_mapping.resolve()
        approval_info = {
            "path": str(approval_path),
            "sha256": sha256_file(approval_path),
            "approved_count": len(approved),
            "approvals": [asdict(item) for item in approved.values()],
        }
    selected_tables = tuple(args.tables or SUPPORTED_TABLES)
    with _open_read_only(database) as connection:
        verification = _verify_database(connection)
        decisions = build_plan(connection, approved, selected_tables)
    summary = _summary(decisions)

    backup_info: dict[str, Any] | None = None
    backup_path: Path | None = None
    locked_plan = decisions
    committed = False
    if args.apply:
        if args.confirm_apply != APPLY_CONFIRMATION:
            raise NormalizationError(f"--confirm-apply 必须精确为 {APPLY_CONFIRMATION}")
        if args.confirm_service_stopped != STOP_CONFIRMATION:
            raise NormalizationError(
                f"--confirm-service-stopped 必须精确为 {STOP_CONFIRMATION}"
            )
        if not args.expected_sha256:
            raise NormalizationError("--apply 必须提供 --expected-sha256")
        if before_hash != args.expected_sha256.strip().upper():
            raise NormalizationError(
                f"数据库 SHA-256 不匹配：expected={args.expected_sha256}, actual={before_hash}"
            )
        if summary["needs_review"]:
            raise NormalizationError(
                f"仍有 {summary['needs_review']} 行歧义/冲突；必须先完成审批，禁止部分清洗"
            )
        if summary["planned_updates"]:
            if args.backup_dir is None:
                raise NormalizationError("--apply 必须提供 --backup-dir")
            journal_mode = str(verification.get("journal_mode") or "").lower()
            if journal_mode != "delete":
                raise NormalizationError(
                    "正式写入精确要求 journal_mode=delete；"
                    f"当前为 {journal_mode or 'unknown'}（WAL/journal 风险）"
                )
            sidecars = _nonempty_transaction_sidecars(database)
            if sidecars:
                rendered = ", ".join(str(path) for path in sidecars)
                raise NormalizationError(
                    f"检测到非空 SQLite WAL/journal：{rendered}；请停机并完成检查点后重试"
                )
            # Fail fast if another process currently owns the database.
            probe = sqlite3.connect(database, timeout=0)
            try:
                probe.execute("BEGIN EXCLUSIVE")
                probe.rollback()
            except sqlite3.OperationalError as error:
                raise NormalizationError("数据库仍被占用；请停止 ERP 后重试") from error
            finally:
                probe.close()
            backup_path, backup_info = _online_backup(database, args.backup_dir)
            if sha256_file(database) != before_hash:
                raise NormalizationError("在线备份期间源数据库发生变化，禁止继续")

            transaction_failure: BaseException | None = None
            rollback_failure: BaseException | None = None
            mutation_attempted = False
            connection = sqlite3.connect(database, timeout=0)
            try:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("BEGIN EXCLUSIVE")
                locked_journal_mode = str(
                    connection.execute("PRAGMA journal_mode").fetchone()[0]
                ).lower()
                if locked_journal_mode != "delete":
                    raise NormalizationError(
                        "最终加锁后 journal_mode 不再是 delete，禁止继续"
                    )
                sidecars = _nonempty_transaction_sidecars(database)
                if sidecars:
                    raise NormalizationError("最终加锁后检测到非空 WAL/journal，禁止继续")
                if sha256_file(database) != before_hash:
                    raise NormalizationError("最终加锁后数据库 SHA-256 已变化，禁止继续")
                if approval_info is not None:
                    current_approval_hash = sha256_file(Path(approval_info["path"]))
                    if current_approval_hash != approval_info["sha256"]:
                        raise NormalizationError("最终加锁后人工审批文件 SHA-256 已变化")
                locked_plan = build_plan(connection, approved, selected_tables)
                if _decision_fingerprint(locked_plan) != _decision_fingerprint(decisions):
                    raise NormalizationError("加锁前后清洗计划发生变化，禁止继续")
                mutation_attempted = True
                updated = _apply_cas(connection, locked_plan)
                if updated != summary["planned_updates"]:
                    raise NormalizationError("实际更新数量与计划不一致")
                precommit_plan = build_plan(connection, {}, selected_tables)
                precommit_summary = _summary(precommit_plan)
                if precommit_summary["planned_updates"] or precommit_summary["needs_review"]:
                    raise NormalizationError("提交前复核仍存在可修或待复核材质，已回滚")
                _verify_database(connection)
                _commit_transaction(connection)
                committed = True
            except BaseException as failure:
                transaction_failure = failure
                try:
                    connection.rollback()
                except BaseException as rollback_error:
                    rollback_failure = rollback_error
            finally:
                try:
                    connection.close()
                except BaseException as close_error:
                    if transaction_failure is None:
                        transaction_failure = close_error

            if transaction_failure is not None:
                # A failure before our first CAS write must never overwrite a
                # concurrent/external database change with our backup.
                if not mutation_attempted:
                    raise transaction_failure
                changed, failure_evidence = _persistent_change_after_transaction_failure(
                    database=database,
                    database_sha256_before=before_hash,
                    plan_before=decisions,
                    approved=approved,
                    selected_tables=selected_tables,
                )
                if rollback_failure is not None:
                    failure_evidence["rollback_error"] = (
                        f"{type(rollback_failure).__name__}: {rollback_failure}"
                    )
                if changed:
                    assert backup_path is not None and backup_info is not None
                    detected_failure = NormalizationError(
                        "写入/提交异常后检测到数据库已变化或无法安全判定："
                        + json.dumps(failure_evidence, ensure_ascii=False, sort_keys=True)
                    )
                    recovery = _restore_verified_backup_after_failure(
                        database=database,
                        backup_path=backup_path,
                        backup_info=backup_info,
                        backup_dir=args.backup_dir,
                        output_dir=args.output_dir,
                        failure=detected_failure,
                        locked_plan=locked_plan,
                    )
                    raise NormalizationError(
                        "写入/提交异常且发现持久变化，已自动恢复验证备份；"
                        f"失败报告：{recovery['failure_report']}"
                    ) from transaction_failure
                raise transaction_failure
    reports: dict[str, Path] | None = None
    try:
        if committed:
            # Reopen after commit so validation observes durable state.
            with _open_read_only(database) as final_check:
                verification = _verify_database(final_check)
                post_plan = build_plan(final_check, {}, selected_tables)
                post_summary = _summary(post_plan)
                if post_summary["planned_updates"] or post_summary["needs_review"]:
                    raise NormalizationError("提交后复核仍存在可修或待复核材质")

        after_hash = sha256_file(database)
        after_stat = database.stat()
        if not args.apply and (
            after_hash != before_hash
            or after_stat.st_size != before_stat.st_size
            or after_stat.st_mtime_ns != before_stat.st_mtime_ns
        ):
            raise NormalizationError("dry-run 检测到数据库文件发生变化")
        reports = write_reports(
            output_dir=args.output_dir.resolve(),
            mode="APPLY" if args.apply else "DRY_RUN",
            database=database,
            database_sha256_before=before_hash,
            database_sha256_after=after_hash,
            decisions=locked_plan if committed else decisions,
            verification=verification,
            backup=backup_info,
            approval=approval_info,
        )
        return_audit = _audit_before_return(
            database=database,
            mode="APPLY" if args.apply else "DRY_RUN",
            database_sha256_before=before_hash,
            database_sha256_after=after_hash,
            database_stat_after=after_stat,
            reports=reports,
        )
    except BaseException as failure:
        if not committed:
            raise
        assert backup_path is not None and backup_info is not None and args.backup_dir is not None
        publication_cleanup = (
            failure.cleanup_records
            if isinstance(failure, ReportPublicationError)
            else []
        )
        discard_cleanup = _discard_published_reports(reports)
        cleanup_records = _merge_report_cleanup_records(
            publication_cleanup,
            discard_cleanup,
        )
        cleanup_issues = [
            record
            for record in cleanup_records
            if record.get("delete_error")
            or record.get("exists_check_error")
            or record.get("exists_after_cleanup") is not False
        ]
        protected_failure: BaseException = failure
        if cleanup_issues:
            protected_failure = NormalizationError(
                f"{type(failure).__name__}: {failure}；成功/临时报告清理异常："
                + json.dumps(cleanup_issues, ensure_ascii=False, sort_keys=True)
            )
        recovery = _restore_verified_backup_after_failure(
            database=database,
            backup_path=backup_path,
            backup_info=backup_info,
            backup_dir=args.backup_dir,
            output_dir=args.output_dir,
            failure=protected_failure,
            locked_plan=locked_plan,
            report_cleanup_records=cleanup_records,
        )
        raise NormalizationError(
            "提交后收尾审计失败，已自动恢复验证备份；"
            f"失败报告：{recovery['failure_report']}"
        ) from failure
    return {
        "summary": summary,
        "reports": {key: str(value) for key, value in reports.items()},
        "backup": backup_info,
        "return_audit": return_audit,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="安全规范化历史报料材质；默认只读 dry-run，不猜测楞型。"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--approved-mapping", type=Path)
    parser.add_argument(
        "--table",
        dest="tables",
        action="append",
        choices=SUPPORTED_TABLES,
        help="可重复指定；缺省处理数据库中存在的两张历史表。",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-apply", default="")
    parser.add_argument("--confirm-service-stopped", default="")
    parser.add_argument("--expected-sha256", default="")
    parser.add_argument("--backup-dir", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        result = execute(build_parser().parse_args(argv))
    except (NormalizationError, sqlite3.Error, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
