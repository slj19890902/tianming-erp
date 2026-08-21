"""P1-86 structured warehouse addresses and permanent aliases.

Revision ID: bb36v8x9z25
Revises: xx32v8x9z21
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "bb36v8x9z25"
down_revision = "xx32v8x9z21"
branch_labels = None
depends_on = None


AREA_INSERT_TRIGGER = "trg_warehouse_areas_structured_address_insert"
AREA_UPDATE_TRIGGER = "trg_warehouse_areas_structured_address_update"
AREA_DELETE_REFERENCE_TRIGGER = "trg_warehouse_areas_address_reference_delete"
LOCATION_INSERT_TRIGGER = "trg_warehouse_locations_structured_address_insert"
LOCATION_UPDATE_TRIGGER = "trg_warehouse_locations_structured_address_update"
ALIAS_UPDATE_TRIGGER = "trg_warehouse_location_aliases_immutable_update"
ALIAS_DELETE_TRIGGER = "trg_warehouse_location_aliases_immutable_delete"
MUTATION_UPDATE_TRIGGER = "trg_warehouse_location_address_mutations_immutable_update"
MUTATION_DELETE_TRIGGER = "trg_warehouse_location_address_mutations_immutable_delete"


def _sqlite_create_guards() -> None:
    op.execute(
        f"""
        CREATE TRIGGER {AREA_INSERT_TRIGGER}
        BEFORE INSERT ON warehouse_areas
        WHEN NEW.address_version <= 0 OR NOT (
          (NEW.address_zone_code IS NULL AND NEW.address_subzone_no IS NULL) OR
          (length(NEW.address_zone_code) = 1
           AND NEW.address_zone_code >= 'A' AND NEW.address_zone_code <= 'G'
           AND NEW.address_subzone_no BETWEEN 1 AND 99)
        )
        BEGIN
          SELECT RAISE(ABORT, 'invalid structured warehouse area address');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {AREA_UPDATE_TRIGGER}
        BEFORE UPDATE OF address_zone_code, address_subzone_no, address_version
        ON warehouse_areas
        WHEN NEW.address_version <= 0 OR NOT (
          (NEW.address_zone_code IS NULL AND NEW.address_subzone_no IS NULL) OR
          (length(NEW.address_zone_code) = 1
           AND NEW.address_zone_code >= 'A' AND NEW.address_zone_code <= 'G'
           AND NEW.address_subzone_no BETWEEN 1 AND 99)
        )
        BEGIN
          SELECT RAISE(ABORT, 'invalid structured warehouse area address');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {AREA_DELETE_REFERENCE_TRIGGER}
        BEFORE DELETE ON warehouse_areas
        WHEN EXISTS (
          SELECT 1 FROM warehouse_locations location
          WHERE location.address_area_id = OLD.id
        )
        BEGIN
          SELECT RAISE(ABORT, 'warehouse area is referenced by a structured address');
        END
        """
    )
    location_guard = """
        NEW.address_version <= 0
        OR NEW.address_kind NOT IN ('legacy','rack_slot','ground_slot','functional')
        OR (NEW.address_area_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM warehouse_areas area WHERE area.id = NEW.address_area_id
        ))
        OR (NEW.address_kind = 'rack_slot' AND NOT (
          NEW.address_area_id IS NOT NULL
          AND length(NEW.rack_code) = 1
          AND NEW.rack_code >= 'A' AND NEW.rack_code <= 'Z'
          AND NEW.level_no BETWEEN 1 AND 99
          AND NEW.slot_no BETWEEN 1 AND 99
        ))
        OR (NEW.address_kind = 'ground_slot' AND NOT (
          NEW.address_area_id IS NOT NULL
          AND NEW.ground_row_no BETWEEN 1 AND 99
          AND NEW.slot_no BETWEEN 1 AND 99
        ))
    """
    op.execute(
        f"""
        CREATE TRIGGER {LOCATION_INSERT_TRIGGER}
        BEFORE INSERT ON warehouse_locations
        WHEN {location_guard}
        BEGIN
          SELECT RAISE(ABORT, 'invalid structured warehouse location address');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {LOCATION_UPDATE_TRIGGER}
        BEFORE UPDATE OF address_kind, address_area_id, rack_code,
          ground_row_no, level_no, slot_no, address_version
        ON warehouse_locations
        WHEN {location_guard}
        BEGIN
          SELECT RAISE(ABORT, 'invalid structured warehouse location address');
        END
        """
    )
    for trigger, table, action in (
        (ALIAS_UPDATE_TRIGGER, "warehouse_location_aliases", "UPDATE"),
        (ALIAS_DELETE_TRIGGER, "warehouse_location_aliases", "DELETE"),
        (
            MUTATION_UPDATE_TRIGGER,
            "warehouse_location_address_mutations",
            "UPDATE",
        ),
        (
            MUTATION_DELETE_TRIGGER,
            "warehouse_location_address_mutations",
            "DELETE",
        ),
    ):
        op.execute(
            f"""
            CREATE TRIGGER {trigger}
            BEFORE {action} ON {table}
            BEGIN
              SELECT RAISE(ABORT, '{table} is immutable');
            END
            """
        )


def _sqlite_drop_guards() -> None:
    for trigger in (
        MUTATION_DELETE_TRIGGER,
        MUTATION_UPDATE_TRIGGER,
        ALIAS_DELETE_TRIGGER,
        ALIAS_UPDATE_TRIGGER,
        LOCATION_UPDATE_TRIGGER,
        LOCATION_INSERT_TRIGGER,
        AREA_DELETE_REFERENCE_TRIGGER,
        AREA_UPDATE_TRIGGER,
        AREA_INSERT_TRIGGER,
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    op.add_column(
        "warehouse_areas",
        sa.Column("address_zone_code", sa.String(length=1), nullable=True),
    )
    op.add_column(
        "warehouse_areas",
        sa.Column("address_subzone_no", sa.Integer(), nullable=True),
    )
    op.add_column(
        "warehouse_areas",
        sa.Column(
            "address_version", sa.Integer(), server_default="1", nullable=False
        ),
    )
    op.create_index(
        "uq_warehouse_areas_structured_path",
        "warehouse_areas",
        ["floor_id", "address_zone_code", "address_subzone_no"],
        unique=True,
        sqlite_where=sa.text("address_zone_code IS NOT NULL"),
        postgresql_where=sa.text("address_zone_code IS NOT NULL"),
    )

    op.add_column(
        "warehouse_locations",
        sa.Column(
            "address_kind",
            sa.String(length=24),
            server_default="legacy",
            nullable=False,
        ),
    )
    op.add_column(
        "warehouse_locations",
        sa.Column("address_area_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "warehouse_locations",
        sa.Column("rack_code", sa.String(length=1), nullable=True),
    )
    op.add_column(
        "warehouse_locations",
        sa.Column("ground_row_no", sa.Integer(), nullable=True),
    )
    op.add_column(
        "warehouse_locations",
        sa.Column("slot_no", sa.Integer(), nullable=True),
    )
    op.add_column(
        "warehouse_locations",
        sa.Column(
            "address_version", sa.Integer(), server_default="1", nullable=False
        ),
    )
    op.create_index(
        "ix_warehouse_locations_address_area",
        "warehouse_locations",
        ["address_area_id"],
    )
    op.create_index(
        "uq_warehouse_locations_rack_path",
        "warehouse_locations",
        ["address_area_id", "rack_code", "level_no", "slot_no"],
        unique=True,
        sqlite_where=sa.text("address_kind = 'rack_slot'"),
        postgresql_where=sa.text("address_kind = 'rack_slot'"),
    )
    op.create_index(
        "uq_warehouse_locations_ground_path",
        "warehouse_locations",
        ["address_area_id", "ground_row_no", "slot_no"],
        unique=True,
        sqlite_where=sa.text("address_kind = 'ground_slot'"),
        postgresql_where=sa.text("address_kind = 'ground_slot'"),
    )

    if dialect != "sqlite":
        op.create_foreign_key(
            "fk_warehouse_locations_address_area",
            "warehouse_locations",
            "warehouse_areas",
            ["address_area_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_check_constraint(
            "ck_warehouse_areas_structured_address",
            "warehouse_areas",
            "(address_zone_code IS NULL AND address_subzone_no IS NULL) OR "
            "(address_zone_code >= 'A' AND address_zone_code <= 'G' "
            "AND length(address_zone_code) = 1 "
            "AND address_subzone_no >= 1 AND address_subzone_no <= 99)",
        )
        op.create_check_constraint(
            "ck_warehouse_areas_address_version",
            "warehouse_areas",
            "address_version > 0",
        )
        op.create_check_constraint(
            "ck_warehouse_locations_address_kind",
            "warehouse_locations",
            "address_kind IN ('legacy','rack_slot','ground_slot','functional')",
        )
        op.create_check_constraint(
            "ck_warehouse_locations_address_version",
            "warehouse_locations",
            "address_version > 0",
        )
        op.create_check_constraint(
            "ck_warehouse_locations_rack_address",
            "warehouse_locations",
            "address_kind != 'rack_slot' OR "
            "(address_area_id IS NOT NULL AND rack_code >= 'A' AND rack_code <= 'Z' "
            "AND length(rack_code) = 1 AND level_no >= 1 AND level_no <= 99 "
            "AND slot_no >= 1 AND slot_no <= 99)",
        )
        op.create_check_constraint(
            "ck_warehouse_locations_ground_address",
            "warehouse_locations",
            "address_kind != 'ground_slot' OR "
            "(address_area_id IS NOT NULL AND ground_row_no >= 1 "
            "AND ground_row_no <= 99 AND slot_no >= 1 AND slot_no <= 99)",
        )

    op.create_table(
        "warehouse_location_aliases",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=False),
        sa.Column("alias_text", sa.String(length=120), nullable=False),
        sa.Column("normalized_alias", sa.String(length=120), nullable=False),
        sa.Column("alias_kind", sa.String(length=24), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "alias_kind IN ('legacy_code','legacy_name','printed_label')",
            name="ck_warehouse_location_aliases_kind",
        ),
        sa.CheckConstraint(
            "length(trim(alias_text)) > 0 AND length(trim(normalized_alias)) > 0",
            name="ck_warehouse_location_aliases_text",
        ),
        sa.ForeignKeyConstraint(
            ["location_id"], ["warehouse_locations.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "normalized_alias", name="uq_warehouse_location_aliases_normalized"
        ),
    )
    op.create_index(
        "ix_warehouse_location_aliases_location",
        "warehouse_location_aliases",
        ["location_id"],
    )

    op.create_table(
        "warehouse_location_address_mutations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("idempotency_key", sa.String(length=120), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("preview_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("action_kind", sa.String(length=20), nullable=False),
        sa.Column("target_ref", sa.String(length=120), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column("affected_location_ids_json", sa.Text(), nullable=False),
        sa.Column("before_json", sa.Text(), nullable=False),
        sa.Column("after_json", sa.Text(), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action_kind IN ('area','rack','location')",
            name="ck_warehouse_location_address_mutations_action",
        ),
        sa.CheckConstraint(
            "length(trim(idempotency_key)) > 0 AND length(request_hash) = 64 "
            "AND length(preview_fingerprint) = 64",
            name="ck_warehouse_location_address_mutations_frozen",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_warehouse_location_address_mutations_idem",
        ),
    )
    op.create_index(
        "ix_warehouse_location_address_mutations_target",
        "warehouse_location_address_mutations",
        ["action_kind", "target_ref", "created_at"],
    )

    if dialect == "sqlite":
        _sqlite_create_guards()


def _assert_safe_downgrade() -> None:
    bind = op.get_bind()
    alias_count = int(
        bind.execute(sa.text("SELECT count(*) FROM warehouse_location_aliases")).scalar()
        or 0
    )
    mutation_count = int(
        bind.execute(
            sa.text("SELECT count(*) FROM warehouse_location_address_mutations")
        ).scalar()
        or 0
    )
    area_count = int(
        bind.execute(
            sa.text(
                "SELECT count(*) FROM warehouse_areas "
                "WHERE address_zone_code IS NOT NULL OR address_subzone_no IS NOT NULL "
                "OR address_version <> 1"
            )
        ).scalar()
        or 0
    )
    location_count = int(
        bind.execute(
            sa.text(
                "SELECT count(*) FROM warehouse_locations "
                "WHERE address_kind <> 'legacy' OR address_area_id IS NOT NULL "
                "OR rack_code IS NOT NULL OR ground_row_no IS NOT NULL "
                "OR slot_no IS NOT NULL OR address_version <> 1"
            )
        ).scalar()
        or 0
    )
    if alias_count or mutation_count or area_count or location_count:
        raise RuntimeError(
            "P1-86 已存在结构化地址、旧码别名或改名审计事实，禁止降级丢失。"
        )


def downgrade() -> None:
    _assert_safe_downgrade()
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "sqlite":
        _sqlite_drop_guards()

    op.drop_index(
        "ix_warehouse_location_address_mutations_target",
        table_name="warehouse_location_address_mutations",
    )
    op.drop_table("warehouse_location_address_mutations")
    op.drop_index(
        "ix_warehouse_location_aliases_location",
        table_name="warehouse_location_aliases",
    )
    op.drop_table("warehouse_location_aliases")

    if dialect != "sqlite":
        op.drop_constraint(
            "ck_warehouse_locations_ground_address",
            "warehouse_locations",
            type_="check",
        )
        op.drop_constraint(
            "ck_warehouse_locations_rack_address",
            "warehouse_locations",
            type_="check",
        )
        op.drop_constraint(
            "ck_warehouse_locations_address_version",
            "warehouse_locations",
            type_="check",
        )
        op.drop_constraint(
            "ck_warehouse_locations_address_kind",
            "warehouse_locations",
            type_="check",
        )
        op.drop_constraint(
            "fk_warehouse_locations_address_area",
            "warehouse_locations",
            type_="foreignkey",
        )

    op.drop_index(
        "uq_warehouse_locations_ground_path", table_name="warehouse_locations"
    )
    op.drop_index(
        "uq_warehouse_locations_rack_path", table_name="warehouse_locations"
    )
    op.drop_index(
        "ix_warehouse_locations_address_area", table_name="warehouse_locations"
    )
    for column_name in (
        "address_version",
        "slot_no",
        "ground_row_no",
        "rack_code",
        "address_area_id",
        "address_kind",
    ):
        op.drop_column("warehouse_locations", column_name)

    if dialect != "sqlite":
        op.drop_constraint(
            "ck_warehouse_areas_address_version",
            "warehouse_areas",
            type_="check",
        )
        op.drop_constraint(
            "ck_warehouse_areas_structured_address",
            "warehouse_areas",
            type_="check",
        )
    op.drop_index(
        "uq_warehouse_areas_structured_path", table_name="warehouse_areas"
    )
    for column_name in (
        "address_version",
        "address_subzone_no",
        "address_zone_code",
    ):
        op.drop_column("warehouse_areas", column_name)
