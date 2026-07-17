"""Prevent legacy offline scripts from bypassing master-data version auditing."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import inspect


class LegacyMasterDataWriteBlocked(RuntimeError):
    """Raised before a legacy script can write version-managed master data."""


def versioning_markers(bind: Any) -> tuple[str, ...]:
    """Return schema markers that mean master-data writes require the version service.

    The check deliberately examines schema only.  A freshly upgraded database has
    versioning enabled even before its first audit row is recorded.
    """

    get_bind = getattr(bind, "get_bind", None)
    inspectable = get_bind() if callable(get_bind) else bind
    inspector = inspect(inspectable)
    tables = set(inspector.get_table_names())
    markers: list[str] = []

    if "master_data_object_versions" in tables:
        markers.append("master_data_object_versions")

    for table_name in ("customers", "products", "materials"):
        if table_name not in tables:
            continue
        columns: Iterable[dict[str, Any]] = inspector.get_columns(table_name)
        if any(column["name"] == "version" for column in columns):
            markers.append(f"{table_name}.version")

    return tuple(markers)


def reject_legacy_master_data_write_if_versioned(bind: Any, *, script_name: str) -> None:
    """Fail closed before a legacy script enters an apply/commit path."""

    markers = versioning_markers(bind)
    if not markers:
        return

    marker_text = "、".join(markers)
    raise LegacyMasterDataWriteBlocked(
        f"已拒绝旧离线脚本 {script_name} 进入写入模式：检测到主数据版本/审计已启用（{marker_text}）。"
        "直接写入 customers/products/materials 会绕过版本服务和审计记录。"
        "请改用受控的主数据版本服务；如只需核对，请使用 dry-run 或不带 --commit/--apply 的预览模式。"
    )
