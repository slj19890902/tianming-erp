"""P1-87 ground-slot layout and spatial occupancy facts.

Revision ID: cc37v8x9z26
Revises: bb36v8x9z25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "cc37v8x9z26"
down_revision = "bb36v8x9z25"
branch_labels = None
depends_on = None


SQLITE_TRIGGERS = (
    "trg_ground_plans_published_immutable",
    "trg_ground_plans_delete_guard",
    "trg_ground_layout_slots_immutable_update",
    "trg_ground_layout_slots_immutable_delete",
    "trg_ground_occupancies_transition_guard",
    "trg_ground_occupancies_delete_guard",
    "trg_ground_occupancy_slots_transition_guard",
    "trg_ground_occupancy_slots_delete_guard",
    "trg_ground_placement_mutations_immutable_update",
    "trg_ground_placement_mutations_immutable_delete",
)


def _create_sqlite_triggers() -> None:
    op.execute(
        """
        CREATE TRIGGER trg_ground_plans_published_immutable
        BEFORE UPDATE ON warehouse_ground_layout_plans
        WHEN OLD.status = 'published'
        BEGIN
          SELECT RAISE(ABORT, 'published ground layout plan is immutable');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_ground_plans_delete_guard
        BEFORE DELETE ON warehouse_ground_layout_plans
        BEGIN
          SELECT RAISE(ABORT, 'ground layout plan cannot be deleted');
        END
        """
    )
    for action in ("UPDATE", "DELETE"):
        op.execute(
            f"""
            CREATE TRIGGER trg_ground_layout_slots_immutable_{action.lower()}
            BEFORE {action} ON warehouse_ground_layout_slots
            BEGIN
              SELECT RAISE(ABORT, 'published ground layout slot is immutable');
            END
            """
        )
    op.execute(
        """
        CREATE TRIGGER trg_ground_occupancies_transition_guard
        BEFORE UPDATE ON warehouse_ground_occupancies
        WHEN NEW.pallet_id <> OLD.pallet_id
          OR NEW.primary_location_id <> OLD.primary_location_id
          OR NEW.customer_id <> OLD.customer_id
          OR NEW.product_id <> OLD.product_id
          OR NEW.footprint_kind <> OLD.footprint_kind
          OR NEW.capacity_quantity <> OLD.capacity_quantity
          OR NOT (OLD.status = 'active' AND NEW.status = 'released'
                  AND NEW.version = OLD.version + 1
                  AND NEW.released_by IS NOT NULL AND NEW.released_at IS NOT NULL)
        BEGIN
          SELECT RAISE(ABORT, 'invalid ground occupancy transition');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_ground_occupancies_delete_guard
        BEFORE DELETE ON warehouse_ground_occupancies
        BEGIN
          SELECT RAISE(ABORT, 'ground occupancy cannot be deleted');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_ground_occupancy_slots_transition_guard
        BEFORE UPDATE ON warehouse_ground_occupancy_slots
        WHEN NEW.occupancy_id <> OLD.occupancy_id
          OR NEW.location_id <> OLD.location_id
          OR NEW.slot_sequence <> OLD.slot_sequence
          OR NOT (OLD.status = 'active' AND NEW.status = 'released'
                  AND NEW.released_at IS NOT NULL)
        BEGIN
          SELECT RAISE(ABORT, 'invalid ground occupancy slot transition');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_ground_occupancy_slots_delete_guard
        BEFORE DELETE ON warehouse_ground_occupancy_slots
        BEGIN
          SELECT RAISE(ABORT, 'ground occupancy slot cannot be deleted');
        END
        """
    )
    for action in ("UPDATE", "DELETE"):
        op.execute(
            f"""
            CREATE TRIGGER trg_ground_placement_mutations_immutable_{action.lower()}
            BEFORE {action} ON warehouse_ground_placement_mutations
            BEGIN
              SELECT RAISE(ABORT, 'ground placement mutation is immutable');
            END
            """
        )


def _drop_sqlite_triggers() -> None:
    for trigger in reversed(SQLITE_TRIGGERS):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")


def upgrade() -> None:
    op.create_table(
        "warehouse_ground_layout_plans",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("area_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="draft", nullable=False),
        sa.Column("target_slot_count", sa.Integer(), nullable=False),
        sa.Column("numbering_origin", sa.String(length=20), nullable=False),
        sa.Column("row_direction", sa.String(length=30), nullable=False),
        sa.Column("slot_direction", sa.String(length=20), nullable=False),
        sa.Column("row_start_no", sa.Integer(), nullable=False),
        sa.Column("slot_start_no", sa.Integer(), nullable=False),
        sa.Column("draft_map_revision", sa.String(length=64), nullable=False),
        sa.Column("published_map_revision", sa.String(length=64), nullable=True),
        sa.Column("preview_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("publish_idempotency_key", sa.String(length=120), nullable=True),
        sa.Column("publish_request_hash", sa.String(length=64), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=False),
        sa.Column("published_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("status IN ('draft','published')", name="ck_warehouse_ground_layout_plans_status"),
        sa.CheckConstraint("numbering_origin IN ('south','north','west','east')", name="ck_warehouse_ground_layout_plans_origin"),
        sa.CheckConstraint("row_direction IN ('from_aisle_inward','from_inside_outward')", name="ck_warehouse_ground_layout_plans_row_direction"),
        sa.CheckConstraint("slot_direction IN ('left_to_right','right_to_left')", name="ck_warehouse_ground_layout_plans_slot_direction"),
        sa.CheckConstraint("target_slot_count > 0 AND target_slot_count <= 500", name="ck_warehouse_ground_layout_plans_target_count"),
        sa.CheckConstraint("row_start_no BETWEEN 1 AND 99 AND slot_start_no BETWEEN 1 AND 99", name="ck_warehouse_ground_layout_plans_number_starts"),
        sa.CheckConstraint("version > 0", name="ck_warehouse_ground_layout_plans_version"),
        sa.CheckConstraint("length(preview_fingerprint) = 64", name="ck_warehouse_ground_layout_plans_preview_fingerprint"),
        sa.CheckConstraint(
            "(status = 'draft' AND published_map_revision IS NULL AND publish_idempotency_key IS NULL "
            "AND publish_request_hash IS NULL AND published_by IS NULL AND published_at IS NULL) OR "
            "(status = 'published' AND published_map_revision IS NOT NULL "
            "AND length(trim(publish_idempotency_key)) > 0 AND length(publish_request_hash) = 64 "
            "AND published_by IS NOT NULL AND published_at IS NOT NULL)",
            name="ck_warehouse_ground_layout_plans_publish_facts",
        ),
        sa.ForeignKeyConstraint(["area_id"], ["warehouse_areas.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["published_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("area_id", name="uq_warehouse_ground_layout_plans_area"),
        sa.UniqueConstraint("publish_idempotency_key", name="uq_warehouse_ground_layout_plans_publish_idem"),
    )
    op.create_table(
        "warehouse_ground_layout_slots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=False),
        sa.Column("route_sequence", sa.Integer(), nullable=False),
        sa.Column("row_no", sa.Integer(), nullable=False),
        sa.Column("slot_no", sa.Integer(), nullable=False),
        sa.Column("x_mm", sa.Numeric(12, 3), nullable=False),
        sa.Column("y_mm", sa.Numeric(12, 3), nullable=False),
        sa.Column("width_mm", sa.Integer(), nullable=False),
        sa.Column("depth_mm", sa.Integer(), nullable=False),
        sa.CheckConstraint("route_sequence > 0", name="ck_warehouse_ground_layout_slots_route"),
        sa.CheckConstraint("row_no BETWEEN 1 AND 99 AND slot_no BETWEEN 1 AND 99", name="ck_warehouse_ground_layout_slots_address"),
        sa.CheckConstraint("((width_mm = 1200 AND depth_mm = 1000) OR (width_mm = 1000 AND depth_mm = 1200))", name="ck_warehouse_ground_layout_slots_standard_size"),
        sa.ForeignKeyConstraint(["plan_id"], ["warehouse_ground_layout_plans.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["location_id"], ["warehouse_locations.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("location_id", name="uq_warehouse_ground_layout_slots_location"),
        sa.UniqueConstraint("plan_id", "route_sequence", name="uq_warehouse_ground_layout_slots_route"),
        sa.UniqueConstraint("plan_id", "row_no", "slot_no", name="uq_warehouse_ground_layout_slots_address"),
    )
    op.create_index("ix_warehouse_ground_layout_slots_plan", "warehouse_ground_layout_slots", ["plan_id"])
    op.create_table(
        "warehouse_ground_occupancies",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pallet_id", sa.Integer(), nullable=False),
        sa.Column("primary_location_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("footprint_kind", sa.String(length=20), nullable=False),
        sa.Column("capacity_quantity", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("released_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("released_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("status IN ('active','released')", name="ck_warehouse_ground_occupancies_status"),
        sa.CheckConstraint("footprint_kind IN ('single','double')", name="ck_warehouse_ground_occupancies_footprint"),
        sa.CheckConstraint("capacity_quantity > 0", name="ck_warehouse_ground_occupancies_capacity"),
        sa.CheckConstraint("version > 0", name="ck_warehouse_ground_occupancies_version"),
        sa.ForeignKeyConstraint(["pallet_id"], ["inventory_pallets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["primary_location_id"], ["warehouse_locations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["released_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_warehouse_ground_occupancies_status", "warehouse_ground_occupancies", ["status"])
    op.create_index(
        "uq_warehouse_ground_occupancies_active_pallet",
        "warehouse_ground_occupancies",
        ["pallet_id"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "warehouse_ground_occupancy_slots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("occupancy_id", sa.Integer(), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=False),
        sa.Column("slot_sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("released_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("status IN ('active','released')", name="ck_warehouse_ground_occupancy_slots_status"),
        sa.CheckConstraint("slot_sequence IN (1,2)", name="ck_warehouse_ground_occupancy_slots_sequence"),
        sa.ForeignKeyConstraint(["occupancy_id"], ["warehouse_ground_occupancies.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["location_id"], ["warehouse_locations.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("occupancy_id", "slot_sequence", name="uq_warehouse_ground_occupancy_slots_sequence"),
        sa.UniqueConstraint("occupancy_id", "location_id", name="uq_warehouse_ground_occupancy_slots_location"),
    )
    op.create_index(
        "uq_warehouse_ground_occupancy_slots_active_location",
        "warehouse_ground_occupancy_slots",
        ["location_id"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "warehouse_ground_placement_mutations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column("operation", sa.String(length=24), nullable=False),
        sa.Column("source_lot_id", sa.Integer(), nullable=True),
        sa.Column("result_lot_id", sa.Integer(), nullable=False),
        sa.Column("occupancy_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.CheckConstraint("operation IN ('finished_inbound','lot_transfer','pallet_move')", name="ck_warehouse_ground_placement_mutations_operation"),
        sa.CheckConstraint("length(trim(idempotency_key)) > 0 AND length(request_hash) = 64", name="ck_warehouse_ground_placement_mutations_request"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_lot_id"], ["inventory_lots.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["result_lot_id"], ["inventory_lots.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["occupancy_id"], ["warehouse_ground_occupancies.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_warehouse_ground_placement_mutations_idem"),
    )
    op.create_index("ix_warehouse_ground_placement_mutations_occupancy", "warehouse_ground_placement_mutations", ["occupancy_id", "created_at"])

    if op.get_bind().dialect.name == "sqlite":
        _create_sqlite_triggers()


def _assert_safe_downgrade() -> None:
    bind = op.get_bind()
    for table in (
        "warehouse_ground_placement_mutations",
        "warehouse_ground_occupancy_slots",
        "warehouse_ground_occupancies",
        "warehouse_ground_layout_slots",
        "warehouse_ground_layout_plans",
    ):
        count = int(bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar() or 0)
        if count:
            raise RuntimeError("P1-87 已存在地堆排位、占用或地图存放事实，禁止降级丢失。")


def downgrade() -> None:
    _assert_safe_downgrade()
    if op.get_bind().dialect.name == "sqlite":
        _drop_sqlite_triggers()
    op.drop_index("ix_warehouse_ground_placement_mutations_occupancy", table_name="warehouse_ground_placement_mutations")
    op.drop_table("warehouse_ground_placement_mutations")
    op.drop_index("uq_warehouse_ground_occupancy_slots_active_location", table_name="warehouse_ground_occupancy_slots")
    op.drop_table("warehouse_ground_occupancy_slots")
    op.drop_index("uq_warehouse_ground_occupancies_active_pallet", table_name="warehouse_ground_occupancies")
    op.drop_index("ix_warehouse_ground_occupancies_status", table_name="warehouse_ground_occupancies")
    op.drop_table("warehouse_ground_occupancies")
    op.drop_index("ix_warehouse_ground_layout_slots_plan", table_name="warehouse_ground_layout_slots")
    op.drop_table("warehouse_ground_layout_slots")
    op.drop_table("warehouse_ground_layout_plans")
