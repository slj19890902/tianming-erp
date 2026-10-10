"""Delete only unused mistaken mold masters, retaining their identity and audit."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.core.time_contract import utc_now_naive
from app.models.mold_tool import MoldTool


# Customer identity associations and create/edit receipts are deliberately retained.
# All usage references, including inactive products and non-FK order snapshots, block.
USAGE_REFERENCES = (
    ("products", "mold_tool_id", "已绑定过常用箱"),
    ("product_bom_components", "mold_tool_id", "已有BOM使用记录"),
    ("sales_order_item_bom_components", "snapshot_mold_tool_id", "已有订单生产快照"),
    ("drawing_releases", "mold_tool_id", "已有图纸发布记录"),
    ("finance_customer_charges", "mold_tool_id", "已有模具费用记录"),
    ("mold_location_movements", "mold_tool_id", "已有移位或封存记录"),
    ("mold_repair_events", "mold_tool_id", "已有维修记录"),
    ("mold_scan_events", "mold_tool_id", "已有扫码使用记录"),
    ("mold_label_print_job_items", "mold_tool_id", "已有标签打印记录"),
)
JSON_USAGE_REFERENCES = (
    ("master_data_object_versions", "snapshot_json", "已有历史产品或BOM绑定，解绑后也不能删除"),
    ("warehouse_goods_profiles", "data_json", "已有库存适用模具记录"),
    ("warehouse_goods_mutations", "response_json", "已有库存适用模具历史"),
    ("warehouse_shelf_mutations", "result_json", "已有位置操作记录"),
    ("finished_goods_inventory_details", "physical_basis_json", "已有库存实物身份快照"),
    ("stock_replenishment_order_items", "production_snapshot_json", "已有报料加工身份快照"),
)


def mold_deletion_blockers(db: Session, row: MoldTool) -> list[str]:
    reasons = []
    if row.deleted_at is not None:
        return ["该误建档案已删除"]
    if not db.execute(text("SELECT 1 FROM mold_master_mutations WHERE mold_tool_id = :id AND action = 'create' LIMIT 1"), {"id": row.id}).first():
        reasons.append("历史档案缺少新建凭证，无法确认从未使用")
    if row.archive_status != "active" or row.restored_at is not None:
        reasons.append("已有封存或恢复记录")
    if row.location_version != 1 or row.last_location_confirmed_at is not None:
        reasons.append("已有位置确认记录")
    if row.repair_version != 1 or row.repair_status != "normal":
        reasons.append("已有维修状态记录")
    for table, column, reason in USAGE_REFERENCES:
        if db.execute(text(f'SELECT 1 FROM "{table}" WHERE "{column}" = :id LIMIT 1'), {"id": row.id}).first():
            reasons.append(reason)
    # Unbinding does not make a previously used mold deletable. Version snapshots
    # include indirect product/BOM writers, not just the warehouse binding endpoint.
    for table, column, reason in JSON_USAGE_REFERENCES:
        if db.execute(text(f"""
            SELECT 1 FROM "{table}" AS v,
            json_tree(CASE WHEN json_valid(v."{column}") THEN v."{column}" ELSE '{{}}' END) AS j
            WHERE (j.key IN ('mold_id', 'mold_tool_id', 'snapshot_mold_tool_id') AND CAST(j.value AS TEXT) = :id)
               OR (j.key IN ('mold_code', 'mold_tool_code', 'snapshot_mold_tool_code') AND j.value = :code)
            LIMIT 1
        """), {"id": str(row.id), "code": row.mold_code}).first():
            reasons.append(reason)
    if db.execute(text("""
        SELECT 1 FROM operation_logs
        WHERE entity_type = 'mold_tool' AND entity_id = :id
          AND action IN ('BIND_PRODUCTS', 'UNBIND_PRODUCT') LIMIT 1
    """), {"id": row.id}).first():
        reasons.append("已有历史绑定操作")
    return list(dict.fromkeys(reasons))


def delete_unused_mold(db: Session, *, mold_id: int, expected_version: int,
                       idempotency_key: str, actor_id: int) -> tuple[MoldTool, bool]:
    replay = db.scalar(select(MoldTool).where(MoldTool.delete_idempotency_key == idempotency_key))
    if replay is not None:
        if replay.id != mold_id or replay.deleted_by != actor_id or replay.version != expected_version + 1:
            raise HTTPException(409, "删除凭证已用于其他请求，请刷新核对")
        return replay, True
    row = db.get(MoldTool, mold_id)
    if row is None or row.deleted_at is not None:
        raise HTTPException(404, "模具不存在或已删除")
    # Take SQLite's writer lock before inspecting history. Concurrent binding
    # either commits before these checks, or is refused by the DB reference guard.
    claimed = db.execute(update(MoldTool).where(
        MoldTool.id == mold_id, MoldTool.version == expected_version,
        MoldTool.deleted_at.is_(None),
    ).values(version=MoldTool.version + 1).execution_options(synchronize_session=False))
    if claimed.rowcount != 1:
        # Another connection may have completed this same command while we
        # waited for its writer lock. Return that receipt rather than a conflict.
        db.refresh(row)
        if (row.deleted_at is not None and row.delete_idempotency_key == idempotency_key
                and row.deleted_by == actor_id and row.version == expected_version + 1):
            return row, True
        raise HTTPException(409, "模具资料已变化，请刷新后重新核对")
    db.refresh(row)
    reasons = mold_deletion_blockers(db, row)
    if reasons:
        raise HTTPException(409, "不能删除：" + "；".join(reasons) + "。仅允许删除误建且未使用过的档案。")
    row.deleted_at = utc_now_naive()
    row.deleted_by = actor_id
    row.delete_idempotency_key = idempotency_key
    row.is_active = False
    row.updated_by = actor_id
    db.flush()
    return row, False
