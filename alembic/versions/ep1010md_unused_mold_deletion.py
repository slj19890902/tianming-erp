"""Retain immutable identities for deleted unused mold masters; no backfill."""
from alembic import op
import sqlalchemy as sa

revision = "ep1010md"
down_revision = "eo1010pi"
branch_labels = None
depends_on = None

# Frozen list for this revision, including order snapshots without a physical FK.
REFERENCES = (
    ("products", "mold_tool_id"),
    ("product_bom_components", "mold_tool_id"),
    ("sales_order_item_bom_components", "snapshot_mold_tool_id"),
    ("drawing_releases", "mold_tool_id"),
    ("finance_customer_charges", "mold_tool_id"),
    ("mold_location_movements", "mold_tool_id"),
    ("mold_repair_events", "mold_tool_id"),
    ("mold_scan_events", "mold_tool_id"),
    ("mold_label_print_job_items", "mold_tool_id"),
    ("mold_tool_customers", "mold_tool_id"),
    ("mold_master_mutations", "mold_tool_id"),
)
JSON_REFERENCES = (
    ("master_data_object_versions", "snapshot_json"),
    ("warehouse_goods_profiles", "data_json"),
    ("warehouse_goods_mutations", "response_json"),
    ("warehouse_shelf_mutations", "result_json"),
    ("finished_goods_inventory_details", "physical_basis_json"),
    ("stock_replenishment_order_items", "production_snapshot_json"),
)


def _json_usage(document, identity, code):
    return f"""EXISTS (SELECT 1 FROM json_tree(CASE WHEN json_valid({document}) THEN {document} ELSE '{{}}' END) AS j
        WHERE (j.key IN ('mold_id','mold_tool_id','snapshot_mold_tool_id') AND CAST(j.value AS TEXT) = CAST({identity} AS TEXT))
           OR (j.key IN ('mold_code','mold_tool_code','snapshot_mold_tool_code') AND j.value = {code}))"""


def guard_statements():
    for event in ("UPDATE", "DELETE"):
        yield f"trg_mold_deleted_no_{event.lower()}", f"""
            CREATE TRIGGER trg_mold_deleted_no_{event.lower()}
            BEFORE {event} ON mold_tools WHEN OLD.deleted_at IS NOT NULL
            BEGIN SELECT RAISE(ABORT, 'deleted mold identity is immutable'); END
        """
    yield "trg_mold_deleted_state", """
        CREATE TRIGGER trg_mold_deleted_state BEFORE UPDATE ON mold_tools
        WHEN (NEW.deleted_at IS NULL AND
              (NEW.deleted_by IS NOT NULL OR NEW.delete_idempotency_key IS NOT NULL))
          OR (NEW.deleted_at IS NOT NULL AND
              (NEW.deleted_by IS NULL OR NEW.delete_idempotency_key IS NULL
               OR length(trim(NEW.delete_idempotency_key)) < 8 OR NEW.is_active != 0
               OR NEW.archive_status != 'active'))
        BEGIN SELECT RAISE(ABORT, 'invalid mold deletion state'); END
    """
    yield "trg_mold_deleted_insert", """
        CREATE TRIGGER trg_mold_deleted_insert BEFORE INSERT ON mold_tools
        WHEN NEW.deleted_at IS NOT NULL OR NEW.deleted_by IS NOT NULL
          OR NEW.delete_idempotency_key IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'new mold cannot start deleted'); END
    """
    used = " OR ".join(
        f'EXISTS (SELECT 1 FROM "{table}" WHERE "{column}" = OLD.id)'
        for table, column in REFERENCES[:-2]
    )
    historical = " OR ".join(
        f'EXISTS (SELECT 1 FROM "{table}" AS v WHERE {_json_usage(f"v.{column}", "OLD.id", "OLD.mold_code")})'
        for table, column in JSON_REFERENCES
    )
    yield "trg_mold_delete_unused_only", f"""
        CREATE TRIGGER trg_mold_delete_unused_only BEFORE UPDATE OF deleted_at ON mold_tools
        WHEN OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL AND (
            {used} OR NOT EXISTS (SELECT 1 FROM mold_master_mutations
                WHERE mold_tool_id = OLD.id AND action = 'create')
            OR {historical}
            OR EXISTS (SELECT 1 FROM operation_logs WHERE entity_type = 'mold_tool'
                AND entity_id = OLD.id AND action IN ('BIND_PRODUCTS','UNBIND_PRODUCT'))
            OR OLD.location_version != 1 OR OLD.repair_version != 1
            OR OLD.last_location_confirmed_at IS NOT NULL OR OLD.restored_at IS NOT NULL
            OR OLD.archive_status != 'active' OR OLD.repair_status != 'normal')
        BEGIN SELECT RAISE(ABORT, 'used mold cannot be deleted'); END
    """
    for table, column in REFERENCES:
        for event, clause in (("insert", "INSERT"), ("update", f'UPDATE OF "{column}"')):
            name = f"trg_mold_deleted_ref_{table}_{event}"
            yield name, f"""
                CREATE TRIGGER "{name}" BEFORE {clause} ON "{table}"
                WHEN EXISTS (SELECT 1 FROM mold_tools
                             WHERE id = NEW."{column}" AND deleted_at IS NOT NULL)
                BEGIN SELECT RAISE(ABORT, 'deleted mold cannot be referenced'); END
            """
    for table, column in JSON_REFERENCES:
        for event, clause in (("insert", "INSERT"), ("update", f'UPDATE OF "{column}"')):
            name = f"trg_mold_deleted_json_{table}_{event}"
            yield name, f"""
                CREATE TRIGGER "{name}" BEFORE {clause} ON "{table}"
                WHEN EXISTS (SELECT 1 FROM mold_tools AS m WHERE m.deleted_at IS NOT NULL
                    AND {_json_usage(f'NEW.{column}', 'm.id', 'm.mold_code')})
                BEGIN SELECT RAISE(ABORT, 'deleted mold cannot be referenced'); END
            """


def upgrade():
    op.add_column("mold_tools", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    # SQLite supports a nullable REFERENCES column without rebuilding the table.
    op.execute("ALTER TABLE mold_tools ADD COLUMN deleted_by INTEGER REFERENCES users(id) ON DELETE RESTRICT")
    op.add_column("mold_tools", sa.Column("delete_idempotency_key", sa.String(120), nullable=True))
    op.create_index("uq_mold_tools_delete_key", "mold_tools", ["delete_idempotency_key"], unique=True)
    for _, statement in guard_statements():
        op.execute(statement)


def downgrade():
    if op.get_bind().execute(sa.text("""
        SELECT 1 FROM mold_tools WHERE deleted_at IS NOT NULL OR deleted_by IS NOT NULL
        OR delete_idempotency_key IS NOT NULL LIMIT 1
    """)).first():
        raise RuntimeError("已有误建模具删除事实，禁止有损降级；保留现库向前修复")
    for name, _ in reversed(list(guard_statements())):
        op.execute(f'DROP TRIGGER "{name}"')
    op.drop_index("uq_mold_tools_delete_key", table_name="mold_tools")
    op.drop_column("mold_tools", "delete_idempotency_key")
    op.drop_column("mold_tools", "deleted_by")
    op.drop_column("mold_tools", "deleted_at")
