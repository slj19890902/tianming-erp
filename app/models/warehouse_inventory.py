from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base

if TYPE_CHECKING:
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.product import Product


WAREHOUSE_CONSTRUCTION_STATUSES = (
    "not_started",
    "ledger_building",
    "ledger_complete",
    "layout_building",
    "layout_complete",
    "enabled",
    "archived",
)

WAREHOUSE_CAPACITY_REVIEW_STATUSES = (
    "pending",
    "confirmed",
    "excluded",
)


class WarehouseFloor(Base):
    """One physical warehouse floor and its digitisation progress."""

    __tablename__ = "warehouse_floors"
    __table_args__ = (
        CheckConstraint(
            "floor_number >= 1 AND floor_number <= 99",
            name="ck_warehouse_floors_number",
        ),
        CheckConstraint(
            "construction_status IN "
            "('not_started','ledger_building','ledger_complete',"
            "'layout_building','layout_complete','enabled')",
            name="ck_warehouse_floors_construction_status",
        ),
        CheckConstraint(
            "planning_reference_pallet_capacity >= 0",
            name="ck_warehouse_floors_planning_reference_capacity",
        ),
        UniqueConstraint("floor_code", name="uq_warehouse_floors_code"),
        UniqueConstraint("floor_number", name="uq_warehouse_floors_number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    floor_code: Mapped[str] = mapped_column(String(30), nullable=False)
    floor_name: Mapped[str] = mapped_column(String(100), nullable=False)
    floor_number: Mapped[int] = mapped_column(Integer, nullable=False)
    construction_status: Mapped[str] = mapped_column(
        String(30), default="not_started", server_default="not_started", nullable=False
    )
    planning_reference_pallet_capacity: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )

    areas: Mapped[list["WarehouseArea"]] = relationship(
        back_populates="floor", cascade="all, delete-orphan"
    )


class WarehouseArea(Base):
    """A real area ledger; capacity does not create or move locations."""

    __tablename__ = "warehouse_areas"
    __table_args__ = (
        CheckConstraint(
            "planned_location_count >= 0",
            name="ck_warehouse_areas_planned_location_count",
        ),
        CheckConstraint(
            "planned_pallet_capacity >= 0",
            name="ck_warehouse_areas_planned_pallet_capacity",
        ),
        CheckConstraint(
            "construction_status IN "
            "('not_started','ledger_building','ledger_complete',"
            "'layout_building','layout_complete','enabled','archived')",
            name="ck_warehouse_areas_construction_status",
        ),
        CheckConstraint(
            "capacity_review_status IN ('pending','confirmed','excluded')",
            name="ck_warehouse_areas_capacity_review_status",
        ),
        CheckConstraint(
            "confirmed_pallet_capacity IS NULL OR confirmed_pallet_capacity > 0",
            name="ck_warehouse_areas_confirmed_pallet_capacity",
        ),
        CheckConstraint(
            "(capacity_review_status = 'pending' AND capacity_eligible = 0 "
            "AND confirmed_pallet_capacity IS NULL "
            "AND capacity_reviewed_by IS NULL AND capacity_reviewed_at IS NULL) OR "
            "(capacity_review_status = 'confirmed' AND capacity_eligible = 1 "
            "AND confirmed_pallet_capacity IS NOT NULL "
            "AND capacity_reviewed_by IS NOT NULL AND capacity_reviewed_at IS NOT NULL) OR "
            "(capacity_review_status = 'excluded' AND capacity_eligible = 0 "
            "AND confirmed_pallet_capacity IS NULL "
            "AND capacity_reviewed_by IS NOT NULL AND capacity_reviewed_at IS NOT NULL)",
            name="ck_warehouse_areas_capacity_review_consistency",
        ),
        UniqueConstraint(
            "floor_id", "area_code", name="uq_warehouse_areas_floor_code"
        ),
        CheckConstraint(
            "(address_zone_code IS NULL AND address_subzone_no IS NULL) OR "
            "(address_zone_code >= 'A' AND address_zone_code <= 'G' "
            "AND length(address_zone_code) = 1 "
            "AND address_subzone_no >= 1 AND address_subzone_no <= 99)",
            name="ck_warehouse_areas_structured_address",
        ),
        CheckConstraint(
            "address_version > 0",
            name="ck_warehouse_areas_address_version",
        ),
        Index("ix_warehouse_areas_floor_id", "floor_id"),
        Index(
            "uq_warehouse_areas_structured_path",
            "floor_id",
            "address_zone_code",
            "address_subzone_no",
            unique=True,
            sqlite_where=text("address_zone_code IS NOT NULL"),
            postgresql_where=text("address_zone_code IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    floor_id: Mapped[int] = mapped_column(
        ForeignKey("warehouse_floors.id", ondelete="RESTRICT"), nullable=False
    )
    area_code: Mapped[str] = mapped_column(String(30), nullable=False)
    area_name: Mapped[str] = mapped_column(String(100), nullable=False)
    address_zone_code: Mapped[str | None] = mapped_column(String(1), nullable=True)
    address_subzone_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    address_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    planned_location_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    planned_pallet_capacity: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    construction_status: Mapped[str] = mapped_column(
        String(30), default="ledger_building", server_default="ledger_building", nullable=False
    )
    capacity_review_status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending", nullable=False
    )
    capacity_eligible: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    confirmed_pallet_capacity: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    capacity_reviewed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    capacity_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )

    floor: Mapped["WarehouseFloor"] = relationship(back_populates="areas")
    storage_policy: Mapped["WarehouseAreaStoragePolicy | None"] = relationship(
        back_populates="area",
        cascade="all, delete-orphan",
        uselist=False,
    )
    address_locations: Mapped[list["WarehouseLocation"]] = relationship(
        back_populates="address_area",
        foreign_keys="WarehouseLocation.address_area_id",
    )


class WarehouseAreaStoragePolicy(Base):
    """Formal link between one warehouse area and one published twin-map zone.

    The map remains a spatial asset.  This row is the business-side authority
    for which inventory usages are allowed in the area and whether its binding
    is still a draft or has been published for operations.
    """

    __tablename__ = "warehouse_area_storage_policies"
    __table_args__ = (
        CheckConstraint(
            "storage_layout IN ('rack','pallet_ground','mixed','functional')",
            name="ck_warehouse_area_storage_policies_layout",
        ),
        CheckConstraint(
            "status IN ('draft','published','archived')",
            name="ck_warehouse_area_storage_policies_status",
        ),
        CheckConstraint(
            "status <> 'archived' OR (archived_at IS NOT NULL "
            "AND archived_by IS NOT NULL "
            "AND length(trim(archive_operation_key)) > 0 "
            "AND length(archive_request_hash) = 64 "
            "AND length(trim(archive_feature_snapshot_json)) > 0)",
            name="ck_warehouse_area_storage_policies_archive_facts",
        ),
        CheckConstraint(
            "version > 0",
            name="ck_warehouse_area_storage_policies_version",
        ),
        UniqueConstraint("area_id", name="uq_warehouse_area_storage_policies_area"),
        UniqueConstraint(
            "map_feature_id",
            name="uq_warehouse_area_storage_policies_feature",
        ),
        Index(
            "ix_warehouse_area_storage_policies_status",
            "status",
        ),
        UniqueConstraint(
            "archive_operation_key",
            name="uq_warehouse_area_storage_policies_archive_idem",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    area_id: Mapped[int] = mapped_column(
        ForeignKey("warehouse_areas.id", ondelete="CASCADE"), nullable=False
    )
    map_feature_id: Mapped[str] = mapped_column(String(80), nullable=False)
    allowed_inventory_types_json: Mapped[str] = mapped_column(Text, nullable=False)
    storage_layout: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="draft", server_default="draft", nullable=False
    )
    draft_map_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published_map_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    updated_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    archived_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    archive_operation_key: Mapped[str | None] = mapped_column(
        String(120), nullable=True
    )
    archive_request_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    archive_feature_snapshot_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )

    area: Mapped["WarehouseArea"] = relationship(back_populates="storage_policy")


class WarehouseLocation(Base):
    __tablename__ = "warehouse_locations"
    __table_args__ = (
        CheckConstraint(
            "warehouse_type IN ('finished','semi_finished','shared')",
            name="ck_warehouse_locations_type",
        ),
        CheckConstraint(
            "storage_type IS NULL OR storage_type IN ('ground','rack','temporary_aisle')",
            name="ck_warehouse_locations_storage_type",
        ),
        CheckConstraint(
            "placement_status IS NULL OR placement_status IN ('unplaced','placed')",
            name="ck_warehouse_locations_placement_status",
        ),
        CheckConstraint(
            "address_kind IN ('legacy','rack_slot','ground_slot','functional')",
            name="ck_warehouse_locations_address_kind",
        ),
        CheckConstraint(
            "address_version > 0",
            name="ck_warehouse_locations_address_version",
        ),
        CheckConstraint(
            "address_kind != 'rack_slot' OR "
            "(address_area_id IS NOT NULL AND rack_code >= 'A' AND rack_code <= 'ZZZZ' "
            "AND length(rack_code) BETWEEN 1 AND 4 AND level_no >= 1 AND level_no <= 99 "
            "AND slot_no >= 1 AND slot_no <= 99)",
            name="ck_warehouse_locations_rack_address",
        ),
        CheckConstraint(
            "address_kind != 'ground_slot' OR "
            "(address_area_id IS NOT NULL AND ground_row_no >= 1 AND ground_row_no <= 99 "
            "AND slot_no >= 1 AND slot_no <= 99)",
            name="ck_warehouse_locations_ground_address",
        ),
        UniqueConstraint("location_code", name="uq_warehouse_locations_code"),
        Index("ix_warehouse_locations_type_active", "warehouse_type", "is_active"),
        Index("ix_warehouse_locations_address_area", "address_area_id"),
        Index("ix_warehouse_locations_map_rack", "map_rack_id", "is_active"),
        Index(
            "uq_warehouse_locations_map_rack_cell",
            "map_rack_id",
            "level_no",
            "slot_no",
            unique=True,
            sqlite_where=text("map_rack_id IS NOT NULL"),
            postgresql_where=text("map_rack_id IS NOT NULL"),
        ),
        Index(
            "uq_warehouse_locations_rack_path",
            "address_area_id",
            "rack_code",
            "level_no",
            "slot_no",
            unique=True,
            sqlite_where=text("address_kind = 'rack_slot'"),
            postgresql_where=text("address_kind = 'rack_slot'"),
        ),
        Index(
            "uq_warehouse_locations_ground_path",
            "address_area_id",
            "ground_row_no",
            "slot_no",
            unique=True,
            sqlite_where=text("address_kind = 'ground_slot'"),
            postgresql_where=text("address_kind = 'ground_slot'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    location_code: Mapped[str] = mapped_column(String(50), nullable=False)
    location_name: Mapped[str] = mapped_column(String(100), nullable=False)
    warehouse_type: Mapped[str] = mapped_column(String(30), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    warehouse_floor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    area_code: Mapped[str | None] = mapped_column(String(30), nullable=True)
    storage_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    level_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    side_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    sort_order: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    is_temporary: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    source_version: Mapped[str | None] = mapped_column(String(30), nullable=True)
    address_kind: Mapped[str] = mapped_column(
        String(24), default="legacy", server_default="legacy", nullable=False
    )
    address_area_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouse_areas.id", ondelete="RESTRICT"), nullable=True
    )
    rack_code: Mapped[str | None] = mapped_column(String(4), nullable=True)
    map_rack_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    rack_display_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    ground_row_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    slot_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    address_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    # Alembic owns the formal non-null/default gate. Keeping ORM-only test
    # schemas nullable preserves legacy fixtures that predate spatial placement.
    placement_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )
    current_pallet: Mapped["InventoryPallet | None"] = relationship(
        primaryjoin=lambda: (
            (WarehouseLocation.id == InventoryPallet.location_id)
            & InventoryPallet.is_current.is_(True)
            & (InventoryPallet.location_occupancy_key == "PRIMARY")
        ),
        viewonly=True,
        uselist=False,
    )
    floor3_layout: Mapped["Floor3LocationLayout | None"] = relationship(
        back_populates="location",
        cascade="all, delete-orphan",
        uselist=False,
    )
    address_area: Mapped["WarehouseArea | None"] = relationship(
        back_populates="address_locations",
        foreign_keys=[address_area_id],
    )
    address_aliases: Mapped[list["WarehouseLocationAlias"]] = relationship(
        back_populates="location",
        cascade="all, delete-orphan",
    )


class WarehouseRackLevelLabelPrintJob(Base):
    """Immutable registration and wording snapshot for one rack-level print."""

    __tablename__ = "warehouse_rack_level_label_print_jobs"
    __table_args__ = (
        CheckConstraint(
            "template_version = 'rack_level_80x40_v1'",
            name="ck_warehouse_rack_level_label_print_jobs_template",
        ),
        CheckConstraint(
            "source = 'region_planning'",
            name="ck_warehouse_rack_level_label_print_jobs_source",
        ),
        CheckConstraint(
            "level_count > 0",
            name="ck_warehouse_rack_level_label_print_jobs_levels",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_warehouse_rack_level_label_print_jobs_idempotency",
        ),
        Index(
            "ix_warehouse_rack_level_label_print_jobs_rack",
            "floor_code",
            "map_rack_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    floor_code: Mapped[str] = mapped_column(String(30), nullable=False)
    map_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    area_id: Mapped[int] = mapped_column(
        ForeignKey("warehouse_areas.id", ondelete="RESTRICT"), nullable=False
    )
    map_feature_id: Mapped[str] = mapped_column(String(80), nullable=False)
    map_rack_id: Mapped[str] = mapped_column(String(80), nullable=False)
    map_rack_code: Mapped[str] = mapped_column(String(50), nullable=False)
    floor_name_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    area_name_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    rack_name_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    level_count: Mapped[int] = mapped_column(Integer, nullable=False)
    labels_json: Mapped[str] = mapped_column(Text, nullable=False)
    template_version: Mapped[str] = mapped_column(String(40), nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )


class WarehouseLocationAlias(Base):
    """Permanent old labels for one stable warehouse location identity."""

    __tablename__ = "warehouse_location_aliases"
    __table_args__ = (
        CheckConstraint(
            "alias_kind IN ('legacy_code','legacy_name','printed_label')",
            name="ck_warehouse_location_aliases_kind",
        ),
        CheckConstraint(
            "length(trim(alias_text)) > 0 AND length(trim(normalized_alias)) > 0",
            name="ck_warehouse_location_aliases_text",
        ),
        UniqueConstraint(
            "normalized_alias", name="uq_warehouse_location_aliases_normalized"
        ),
        Index("ix_warehouse_location_aliases_location", "location_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    location_id: Mapped[int] = mapped_column(
        ForeignKey("warehouse_locations.id", ondelete="RESTRICT"), nullable=False
    )
    alias_text: Mapped[str] = mapped_column(String(120), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(120), nullable=False)
    alias_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    created_by: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )

    location: Mapped["WarehouseLocation"] = relationship(
        back_populates="address_aliases"
    )


class WarehouseLocationAddressMutation(Base):
    """Immutable idempotency and audit fact for one address change transaction."""

    __tablename__ = "warehouse_location_address_mutations"
    __table_args__ = (
        CheckConstraint(
            "action_kind IN ('area','rack','location')",
            name="ck_warehouse_location_address_mutations_action",
        ),
        CheckConstraint(
            "length(trim(idempotency_key)) > 0 AND length(request_hash) = 64 "
            "AND length(preview_fingerprint) = 64",
            name="ck_warehouse_location_address_mutations_frozen",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_warehouse_location_address_mutations_idem",
        ),
        Index(
            "ix_warehouse_location_address_mutations_target",
            "action_kind",
            "target_ref",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    action_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    target_ref: Mapped[str] = mapped_column(String(120), nullable=False)
    actor_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    affected_location_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    before_json: Mapped[str] = mapped_column(Text, nullable=False)
    after_json: Mapped[str] = mapped_column(Text, nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )


class Floor3LocationLayout(Base):
    """Interactive-map placement for one physical or logical warehouse location."""

    __tablename__ = "floor3_location_layouts"
    __table_args__ = (
        CheckConstraint(
            "left_pct >= 0 AND left_pct <= 100",
            name="ck_floor3_location_layouts_left_pct",
        ),
        CheckConstraint(
            "top_pct >= 0 AND top_pct <= 100",
            name="ck_floor3_location_layouts_top_pct",
        ),
        CheckConstraint(
            "width_pct > 0 AND width_pct <= 100",
            name="ck_floor3_location_layouts_width_pct",
        ),
        CheckConstraint(
            "height_pct > 0 AND height_pct <= 100",
            name="ck_floor3_location_layouts_height_pct",
        ),
        CheckConstraint(
            "left_pct + width_pct <= 100",
            name="ck_floor3_location_layouts_right_pct",
        ),
        CheckConstraint(
            "top_pct + height_pct <= 100",
            name="ck_floor3_location_layouts_bottom_pct",
        ),
        CheckConstraint("version > 0", name="ck_floor3_location_layouts_version"),
        CheckConstraint(
            "source_type IN ('seeded','manual')",
            name="ck_floor3_location_layouts_source_type",
        ),
        CheckConstraint(
            "layout_kind IN ('unknown','physical_pallet','physical_rack','logical_anchor')",
            name="ck_floor3_location_layouts_layout_kind",
        ),
        UniqueConstraint("location_id", name="uq_floor3_location_layouts_location"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    location_id: Mapped[int] = mapped_column(
        ForeignKey("warehouse_locations.id", ondelete="CASCADE"), nullable=False
    )
    left_pct: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    top_pct: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    width_pct: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    height_pct: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    z_index: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    source_type: Mapped[str] = mapped_column(
        String(20), default="manual", server_default="manual", nullable=False
    )
    layout_kind: Mapped[str] = mapped_column(
        String(24), default="unknown", server_default="unknown", nullable=False
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )

    location: Mapped["WarehouseLocation"] = relationship(back_populates="floor3_layout")


class InventoryPallet(Base):
    __tablename__ = "inventory_pallets"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','closed')", name="ck_inventory_pallets_status"
        ),
        CheckConstraint(
            "is_current = 0 OR location_id IS NOT NULL",
            name="ck_inventory_pallets_current_location",
        ),
        CheckConstraint("version > 0", name="ck_inventory_pallets_version"),
        UniqueConstraint("pallet_code", name="uq_inventory_pallets_code"),
        Index(
            "uq_inventory_pallets_current_location",
            "location_id",
            "location_occupancy_key",
            unique=True,
            sqlite_where=text("is_current = 1"),
            postgresql_where=text("is_current = true"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    pallet_code: Mapped[str] = mapped_column(String(100), nullable=False)
    location_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouse_locations.id", ondelete="SET NULL"), nullable=True
    )
    # Ordinary physical locations retain one current pallet through the
    # ``PRIMARY`` key.  The formal first-floor dispatch area is an area-level
    # staging location, so each direct-production completion receives its own
    # stable key while still sharing the same authoritative location_id.
    location_occupancy_key: Mapped[str] = mapped_column(
        String(100), default="PRIMARY", server_default="PRIMARY", nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), default="active", server_default="active", nullable=False
    )
    is_current: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    needs_relocation: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    location: Mapped["WarehouseLocation | None"] = relationship()
    items: Mapped[list["InventoryPalletItem"]] = relationship(
        back_populates="pallet", cascade="all, delete-orphan", order_by="InventoryPalletItem.id"
    )
    movements: Mapped[list["InventoryLocationMovement"]] = relationship(
        back_populates="pallet", cascade="all, delete-orphan", order_by="InventoryLocationMovement.id"
    )


class InventoryPalletItem(Base):
    __tablename__ = "inventory_pallet_items"
    __table_args__ = (
        CheckConstraint(
            "item_type IN ('finished','semi_finished','raw_material')",
            name="ck_inventory_pallet_items_type",
        ),
        CheckConstraint("quantity > 0", name="ck_inventory_pallet_items_quantity"),
        CheckConstraint(
            "match_status IN ('matched','pending')",
            name="ck_inventory_pallet_items_match_status",
        ),
        UniqueConstraint(
            "inventory_lot_id", name="uq_inventory_pallet_items_inventory_lot"
        ),
        Index("ix_inventory_pallet_items_pallet_id", "pallet_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    pallet_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_pallets.id", ondelete="CASCADE"), nullable=False
    )
    inventory_lot_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="CASCADE"), nullable=True
    )
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    inventory_code: Mapped[str | None] = mapped_column(String(150), nullable=True)
    order_no: Mapped[str | None] = mapped_column(String(100), nullable=True)
    customer_name_snapshot: Mapped[str | None] = mapped_column(String(200), nullable=True)
    product_name: Mapped[str | None] = mapped_column(String(250), nullable=True)
    item_type: Mapped[str] = mapped_column(String(20), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    unit: Mapped[str] = mapped_column(String(30), nullable=False)
    match_status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending", nullable=False
    )
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )

    pallet: Mapped["InventoryPallet"] = relationship(back_populates="items")
    inventory_lot: Mapped["InventoryLot | None"] = relationship(
        back_populates="pallet_item"
    )
    customer: Mapped["Customer | None"] = relationship()
    product: Mapped["Product | None"] = relationship()


class WarehouseGroundLayoutPlan(Base):
    """One versioned ground-slot numbering plan for a published measured area."""

    __tablename__ = "warehouse_ground_layout_plans"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','published')",
            name="ck_warehouse_ground_layout_plans_status",
        ),
        CheckConstraint(
            "numbering_origin IN ('south','north','west','east')",
            name="ck_warehouse_ground_layout_plans_origin",
        ),
        CheckConstraint(
            "row_direction IN ('from_aisle_inward','from_inside_outward')",
            name="ck_warehouse_ground_layout_plans_row_direction",
        ),
        CheckConstraint(
            "slot_direction IN ('left_to_right','right_to_left')",
            name="ck_warehouse_ground_layout_plans_slot_direction",
        ),
        CheckConstraint(
            "target_slot_count > 0 AND target_slot_count <= 500",
            name="ck_warehouse_ground_layout_plans_target_count",
        ),
        CheckConstraint(
            "row_start_no BETWEEN 1 AND 99 AND slot_start_no BETWEEN 1 AND 99",
            name="ck_warehouse_ground_layout_plans_number_starts",
        ),
        CheckConstraint(
            "version > 0",
            name="ck_warehouse_ground_layout_plans_version",
        ),
        CheckConstraint(
            "length(preview_fingerprint) = 64",
            name="ck_warehouse_ground_layout_plans_preview_fingerprint",
        ),
        CheckConstraint(
            "(status = 'draft' AND published_map_revision IS NULL "
            "AND publish_idempotency_key IS NULL AND publish_request_hash IS NULL "
            "AND published_by IS NULL AND published_at IS NULL) OR "
            "(status = 'published' AND published_map_revision IS NOT NULL "
            "AND length(trim(publish_idempotency_key)) > 0 "
            "AND length(publish_request_hash) = 64 "
            "AND published_by IS NOT NULL AND published_at IS NOT NULL)",
            name="ck_warehouse_ground_layout_plans_publish_facts",
        ),
        UniqueConstraint("area_id", name="uq_warehouse_ground_layout_plans_area"),
        UniqueConstraint(
            "publish_idempotency_key",
            name="uq_warehouse_ground_layout_plans_publish_idem",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    area_id: Mapped[int] = mapped_column(
        ForeignKey("warehouse_areas.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), default="draft", server_default="draft", nullable=False
    )
    target_slot_count: Mapped[int] = mapped_column(Integer, nullable=False)
    numbering_origin: Mapped[str] = mapped_column(String(20), nullable=False)
    row_direction: Mapped[str] = mapped_column(String(30), nullable=False)
    slot_direction: Mapped[str] = mapped_column(String(20), nullable=False)
    row_start_no: Mapped[int] = mapped_column(Integer, nullable=False)
    slot_start_no: Mapped[int] = mapped_column(Integer, nullable=False)
    draft_map_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    published_map_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    preview_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    publish_idempotency_key: Mapped[str | None] = mapped_column(
        String(120), nullable=True
    )
    publish_request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    published_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    area: Mapped["WarehouseArea"] = relationship()
    slots: Mapped[list["WarehouseGroundLayoutSlot"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="WarehouseGroundLayoutSlot.route_sequence"
    )


class WarehouseGroundLayoutPlanRetirement(Base):
    """Immutable retirement fact for a preserved published ground layout plan."""

    __tablename__ = "warehouse_ground_layout_plan_retirements"
    __table_args__ = (
        CheckConstraint(
            "length(trim(operation_key)) > 0 AND length(request_hash) = 64 "
            "AND length(trim(snapshot_json)) > 0",
            name="ck_warehouse_ground_layout_plan_retirements_request",
        ),
        UniqueConstraint(
            "plan_id", name="uq_warehouse_ground_layout_plan_retirements_plan"
        ),
        UniqueConstraint(
            "operation_key",
            name="uq_warehouse_ground_layout_plan_retirements_operation",
        ),
        Index(
            "ix_warehouse_ground_layout_plan_retirements_area",
            "area_id",
            "retired_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("warehouse_ground_layout_plans.id", ondelete="RESTRICT"),
        nullable=False,
    )
    area_id: Mapped[int] = mapped_column(
        ForeignKey("warehouse_areas.id", ondelete="RESTRICT"), nullable=False
    )
    operation_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    retired_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    retired_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )

    plan: Mapped["WarehouseGroundLayoutPlan"] = relationship()
    area: Mapped["WarehouseArea"] = relationship()


class WarehouseGroundLayoutSlot(Base):
    """Measured 1200x1000 footprint associated with one stable location."""

    __tablename__ = "warehouse_ground_layout_slots"
    __table_args__ = (
        CheckConstraint("route_sequence > 0", name="ck_warehouse_ground_layout_slots_route"),
        CheckConstraint(
            "row_no BETWEEN 1 AND 99 AND slot_no BETWEEN 1 AND 99",
            name="ck_warehouse_ground_layout_slots_address",
        ),
        CheckConstraint(
            "((width_mm = 1200 AND depth_mm = 1000) OR "
            "(width_mm = 1000 AND depth_mm = 1200))",
            name="ck_warehouse_ground_layout_slots_standard_size",
        ),
        UniqueConstraint("location_id", name="uq_warehouse_ground_layout_slots_location"),
        UniqueConstraint(
            "plan_id", "route_sequence", name="uq_warehouse_ground_layout_slots_route"
        ),
        UniqueConstraint(
            "plan_id", "row_no", "slot_no", name="uq_warehouse_ground_layout_slots_address"
        ),
        Index("ix_warehouse_ground_layout_slots_plan", "plan_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("warehouse_ground_layout_plans.id", ondelete="RESTRICT"), nullable=False
    )
    location_id: Mapped[int] = mapped_column(
        ForeignKey("warehouse_locations.id", ondelete="RESTRICT"), nullable=False
    )
    route_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    slot_no: Mapped[int] = mapped_column(Integer, nullable=False)
    x_mm: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    y_mm: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    width_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    depth_mm: Mapped[int] = mapped_column(Integer, nullable=False)

    plan: Mapped["WarehouseGroundLayoutPlan"] = relationship(back_populates="slots")
    location: Mapped["WarehouseLocation"] = relationship()


class WarehouseGroundOccupancy(Base):
    """Spatial footprint for one pallet; InventoryLot remains the quantity ledger."""

    __tablename__ = "warehouse_ground_occupancies"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','released')",
            name="ck_warehouse_ground_occupancies_status",
        ),
        CheckConstraint(
            "footprint_kind IN ('single','double')",
            name="ck_warehouse_ground_occupancies_footprint",
        ),
        CheckConstraint(
            "capacity_quantity > 0",
            name="ck_warehouse_ground_occupancies_capacity",
        ),
        CheckConstraint("version > 0", name="ck_warehouse_ground_occupancies_version"),
        Index(
            "uq_warehouse_ground_occupancies_active_pallet",
            "pallet_id",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_warehouse_ground_occupancies_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    pallet_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_pallets.id", ondelete="RESTRICT"), nullable=False
    )
    primary_location_id: Mapped[int] = mapped_column(
        ForeignKey("warehouse_locations.id", ondelete="RESTRICT"), nullable=False
    )
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    footprint_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    capacity_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="active", server_default="active", nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    created_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    released_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    pallet: Mapped["InventoryPallet"] = relationship()
    primary_location: Mapped["WarehouseLocation"] = relationship()
    slots: Mapped[list["WarehouseGroundOccupancySlot"]] = relationship(
        back_populates="occupancy", cascade="all, delete-orphan", order_by="WarehouseGroundOccupancySlot.slot_sequence"
    )


class WarehouseGroundOccupancySlot(Base):
    """One active/released ground slot belonging to a single spatial occupancy."""

    __tablename__ = "warehouse_ground_occupancy_slots"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','released')",
            name="ck_warehouse_ground_occupancy_slots_status",
        ),
        CheckConstraint(
            "slot_sequence IN (1,2)",
            name="ck_warehouse_ground_occupancy_slots_sequence",
        ),
        UniqueConstraint(
            "occupancy_id", "slot_sequence", name="uq_warehouse_ground_occupancy_slots_sequence"
        ),
        UniqueConstraint(
            "occupancy_id", "location_id", name="uq_warehouse_ground_occupancy_slots_location"
        ),
        Index(
            "uq_warehouse_ground_occupancy_slots_active_location",
            "location_id",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    occupancy_id: Mapped[int] = mapped_column(
        ForeignKey("warehouse_ground_occupancies.id", ondelete="RESTRICT"), nullable=False
    )
    location_id: Mapped[int] = mapped_column(
        ForeignKey("warehouse_locations.id", ondelete="RESTRICT"), nullable=False
    )
    slot_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="active", server_default="active", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    occupancy: Mapped["WarehouseGroundOccupancy"] = relationship(back_populates="slots")
    location: Mapped["WarehouseLocation"] = relationship()


class WarehouseGroundPlacementMutation(Base):
    """Immutable idempotency fact for one map-selected placement transaction."""

    __tablename__ = "warehouse_ground_placement_mutations"
    __table_args__ = (
        CheckConstraint(
            "operation IN ('finished_inbound','lot_transfer','pallet_move')",
            name="ck_warehouse_ground_placement_mutations_operation",
        ),
        CheckConstraint(
            "length(trim(idempotency_key)) > 0 AND length(request_hash) = 64",
            name="ck_warehouse_ground_placement_mutations_request",
        ),
        UniqueConstraint(
            "idempotency_key", name="uq_warehouse_ground_placement_mutations_idem"
        ),
        Index(
            "ix_warehouse_ground_placement_mutations_occupancy",
            "occupancy_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    operation: Mapped[str] = mapped_column(String(24), nullable=False)
    source_lot_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=True
    )
    result_lot_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=False
    )
    occupancy_id: Mapped[int] = mapped_column(
        ForeignKey("warehouse_ground_occupancies.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )


class InventoryLocationMovement(Base):
    __tablename__ = "inventory_location_movements"
    __table_args__ = (
        CheckConstraint(
            "movement_type IN ('create','add_item','move','clear')",
            name="ck_inventory_location_movements_type",
        ),
        Index(
            "uq_inventory_location_movements_idempotency_key",
            "idempotency_key",
            unique=True,
            sqlite_where=text("idempotency_key IS NOT NULL"),
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index("ix_inventory_location_movements_pallet_moved", "pallet_id", "moved_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    pallet_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_pallets.id", ondelete="CASCADE"), nullable=False
    )
    from_location_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouse_locations.id", ondelete="SET NULL"), nullable=True
    )
    to_location_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouse_locations.id", ondelete="SET NULL"), nullable=True
    )
    movement_type: Mapped[str] = mapped_column(String(20), nullable=False)
    operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    moved_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    pallet_version_before: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pallet_version_after: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)

    pallet: Mapped["InventoryPallet"] = relationship(back_populates="movements")


class InventoryLot(Base):
    __tablename__ = "inventory_lots"
    __table_args__ = (
        CheckConstraint(
            "inventory_type IN ('finished','semi_finished','assembly_body')",
            name="ck_inventory_lots_type",
        ),
        CheckConstraint("unit IN ('boxes','sheets')", name="ck_inventory_lots_unit"),
        CheckConstraint(
            "status IN ('active','frozen','closed')",
            name="ck_inventory_lots_status",
        ),
        CheckConstraint(
            "source_type IN ('manual','production_completion','production_surplus',"
            "'purchase_surplus','purchase_reserve','stocktake','transfer',"
            "'replenishment','delivery_return')",
            name="ck_inventory_lots_source_type",
        ),
        CheckConstraint(
            "stock_date_accuracy IN ('exact','estimated','unknown')",
            name="ck_inventory_lots_stock_date_accuracy",
        ),
        CheckConstraint("quantity_available >= 0", name="ck_inventory_lots_available"),
        CheckConstraint("quantity_reserved >= 0", name="ck_inventory_lots_reserved"),
        CheckConstraint("quantity_consumed >= 0", name="ck_inventory_lots_consumed"),
        CheckConstraint("quantity_damaged >= 0", name="ck_inventory_lots_damaged"),
        CheckConstraint("quantity_scrapped >= 0", name="ck_inventory_lots_scrapped"),
        UniqueConstraint("lot_number", name="uq_inventory_lots_number"),
        UniqueConstraint("id", "inventory_type", name="uq_inventory_lot_type_identity"),
        Index("ix_inventory_lots_type_status", "inventory_type", "status"),
        Index("ix_inventory_lots_location_status", "warehouse_location_id", "status"),
        Index("ix_inventory_lots_stock_date", "stock_date"),
        Index("ix_inventory_lots_last_movement", "last_movement_at"),
        Index(
            "uq_inventory_lots_onboarding_line_source",
            "source_ref_type",
            "source_ref_id",
            unique=True,
            sqlite_where=text(
                "source_ref_type = 'inventory_onboarding_line' "
                "AND source_ref_id IS NOT NULL"
            ),
            postgresql_where=text(
                "source_ref_type = 'inventory_onboarding_line' "
                "AND source_ref_id IS NOT NULL"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    lot_number: Mapped[str] = mapped_column(String(50), nullable=False)
    inventory_type: Mapped[str] = mapped_column(String(30), nullable=False)
    warehouse_location_id: Mapped[int] = mapped_column(
        ForeignKey("warehouse_locations.id", ondelete="RESTRICT"), nullable=False
    )
    quantity_available: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    quantity_reserved: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    quantity_consumed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    quantity_damaged: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    quantity_scrapped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unit: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source_ref_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source_ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stock_date: Mapped[date] = mapped_column(Date, nullable=False)
    stock_date_accuracy: Mapped[str] = mapped_column(
        String(20), default="exact", server_default="exact", nullable=False
    )
    stock_date_original_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_movement_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    estimated_unit_cost_snapshot: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 4), nullable=True
    )
    estimated_square_price_snapshot: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4), nullable=True
    )
    estimated_cost_area_m2_snapshot: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 6), nullable=True
    )
    cost_snapshot_source: Mapped[str | None] = mapped_column(
        String(80), nullable=True
    )
    cost_snapshot_detail_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_snapshot_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )

    location: Mapped["WarehouseLocation"] = relationship()
    finished_detail: Mapped["FinishedGoodsInventoryDetail | None"] = relationship(
        back_populates="lot", cascade="all, delete-orphan", uselist=False
    )
    semi_finished_detail: Mapped["SemiFinishedInventoryDetail | None"] = relationship(
        back_populates="lot", cascade="all, delete-orphan", uselist=False
    )
    movements: Mapped[list["InventoryMovement"]] = relationship(
        back_populates="lot", order_by="InventoryMovement.id"
    )
    pallet_item: Mapped["InventoryPalletItem | None"] = relationship(
        back_populates="inventory_lot", uselist=False
    )
    allowed_products: Mapped[list["SemiFinishedLotAllowedProduct"]] = relationship(
        back_populates="lot", cascade="all, delete-orphan", order_by="SemiFinishedLotAllowedProduct.id"
    )


class FinishedGoodsInventoryDetail(Base):
    __tablename__ = "finished_goods_inventory_details"
    __table_args__ = (
        CheckConstraint(
            "is_general = 1 OR owner_customer_id IS NOT NULL",
            name="ck_finished_inventory_owner",
        ),
        Index("ix_finished_inventory_owner_product", "owner_customer_id", "product_id"),
        Index("ix_finished_inventory_product", "product_id"),
        Index("ix_finished_inventory_general", "is_general"),
        Index("ix_finished_inventory_code", "inventory_code_snapshot"),
    )

    inventory_lot_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="CASCADE"), primary_key=True
    )
    owner_customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )
    owner_customer_name_snapshot: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    is_general: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    inventory_code_snapshot: Mapped[str] = mapped_column(String(150), nullable=False)
    product_name_snapshot: Mapped[str] = mapped_column(String(250), nullable=False)
    box_type_snapshot: Mapped[str | None] = mapped_column(String(150), nullable=True)
    length_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    width_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    material_code_snapshot: Mapped[str | None] = mapped_column(String(100), nullable=True)
    flute_type_snapshot: Mapped[str | None] = mapped_column(String(20), nullable=True)
    physical_basis_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    lot: Mapped["InventoryLot"] = relationship(back_populates="finished_detail")
    customer: Mapped["Customer | None"] = relationship()
    product: Mapped["Product"] = relationship()


class SemiFinishedInventoryDetail(Base):
    __tablename__ = "semi_finished_inventory_details"
    __table_args__ = (
        CheckConstraint("layer_count IN (1,3,5,7)", name="ck_semi_inventory_layer"),
        CheckConstraint(
            "(layer_count=1 AND flute_type='NONE') OR "
            "(layer_count=3 AND flute_type IN ('A','B','E')) OR "
            "(layer_count=5 AND flute_type IN ('AB','BE')) OR "
            "(layer_count=7 AND flute_type IN ('AAA','ABC'))",
            name="ck_semi_inventory_flute",
        ),
        CheckConstraint("board_length_mm > 0", name="ck_semi_inventory_length"),
        CheckConstraint("board_width_mm > 0", name="ck_semi_inventory_width"),
        CheckConstraint(
            "sheet_type IN ('raw_board','net_sheet','creased_sheet')",
            name="ck_semi_inventory_sheet_type",
        ),
        CheckConstraint(
            "component_type IN ('whole','cover','base')",
            name="ck_semi_inventory_component",
        ),
        CheckConstraint(
            "pieces_per_box > 0",
            name="ck_semi_inventory_pieces_per_box",
        ),
        CheckConstraint(
            "stock_yield_per_sheet > 0",
            name="ck_semi_inventory_stock_yield",
        ),
        Index(
            "ix_semi_inventory_flute_size",
            "flute_type",
            "board_length_mm",
            "board_width_mm",
        ),
        Index("ix_semi_inventory_sheet_flute", "sheet_type", "flute_type"),
        Index("ix_semi_inventory_owner", "owner_customer_id"),
        Index(
            "ix_semi_inventory_customer_generic",
            "owner_customer_id",
            "customer_generic_eligible",
        ),
        Index("ix_semi_inventory_material", "material_code_snapshot"),
        Index("ix_semi_inventory_material_id", "material_id"),
        Index(
            "ix_semi_inventory_shared_signature",
            "owner_customer_id",
            "board_length_mm",
            "board_width_mm",
            "normalized_material_code",
            "flute_type",
            "component_type",
            "pieces_per_box",
            "stock_yield_per_sheet",
        ),
    )

    inventory_lot_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="CASCADE"), primary_key=True
    )
    supplier_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    owner_customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )
    material_id: Mapped[int | None] = mapped_column(
        ForeignKey("materials.id", ondelete="SET NULL"), nullable=True
    )
    owner_customer_name_snapshot: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    customer_generic_eligible: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    internal_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    material_code_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    normalized_material_code: Mapped[str] = mapped_column(String(100), nullable=False)
    layer_count: Mapped[int] = mapped_column(Integer, nullable=False)
    flute_type: Mapped[str] = mapped_column(String(20), nullable=False)
    board_length_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    board_width_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    component_type: Mapped[str] = mapped_column(
        String(20), default="whole", nullable=False
    )
    pieces_per_box: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    stock_yield_per_sheet: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False
    )
    sheet_type: Mapped[str] = mapped_column(String(30), nullable=False)
    crease_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    crease_left_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crease_middle_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crease_right_mm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cutting_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    lot: Mapped["InventoryLot"] = relationship(back_populates="semi_finished_detail")
    customer: Mapped["Customer | None"] = relationship()
    material: Mapped["Material | None"] = relationship()


class OrderItemSemiRequirement(Base):
    __tablename__ = "order_item_semi_requirements"
    __table_args__ = (
        CheckConstraint(
            "component_type IN ('whole','cover','base')",
            name="ck_order_item_semi_requirements_component",
        ),
        CheckConstraint(
            "board_length_mm > 0 AND board_width_mm > 0",
            name="ck_order_item_semi_requirements_dimensions",
        ),
        CheckConstraint(
            "pieces_per_box > 0 AND stock_yield_per_sheet > 0",
            name="ck_order_item_semi_requirements_conversion",
        ),
        CheckConstraint(
            "required_piece_quantity > 0",
            name="ck_order_item_semi_requirements_quantity",
        ),
        Index(
            "uq_order_item_semi_requirements_regular_component",
            "order_item_id",
            "component_type",
            unique=True,
            sqlite_where=text("sales_order_item_bom_component_id IS NULL"),
            postgresql_where=text("sales_order_item_bom_component_id IS NULL"),
        ),
        Index(
            "uq_order_item_semi_requirements_bom_component",
            "sales_order_item_bom_component_id",
            "component_type",
            unique=True,
            sqlite_where=text("sales_order_item_bom_component_id IS NOT NULL"),
            postgresql_where=text("sales_order_item_bom_component_id IS NOT NULL"),
        ),
        Index(
            "ix_order_item_semi_requirements_signature",
            "customer_id",
            "board_length_mm",
            "board_width_mm",
            "normalized_material_code",
            "flute_type",
            "component_type",
        ),
        Index(
            "ix_order_item_semi_requirements_bom_component",
            "sales_order_item_bom_component_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_item_id: Mapped[int] = mapped_column(
        ForeignKey("sales_order_items.id", ondelete="CASCADE"), nullable=False
    )
    sales_order_item_bom_component_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_order_item_bom_components.id", ondelete="SET NULL"),
        nullable=True,
    )
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    component_type: Mapped[str] = mapped_column(String(20), nullable=False)
    board_length_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    board_width_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    material_code_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    normalized_material_code: Mapped[str] = mapped_column(String(100), nullable=False)
    flute_type: Mapped[str] = mapped_column(String(20), nullable=False)
    pieces_per_box: Mapped[int] = mapped_column(Integer, nullable=False)
    stock_yield_per_sheet: Mapped[int] = mapped_column(Integer, nullable=False)
    required_piece_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )


class SemiFinishedMatchRule(Base):
    __tablename__ = "semi_finished_match_rules"
    __table_args__ = (
        CheckConstraint(
            "component_type IN ('whole','cover','base')",
            name="ck_semi_finished_match_rules_component",
        ),
        CheckConstraint(
            "board_length_mm > 0 AND board_width_mm > 0",
            name="ck_semi_finished_match_rules_dimensions",
        ),
        CheckConstraint(
            "pieces_per_box > 0 AND stock_yield_per_sheet > 0",
            name="ck_semi_finished_match_rules_conversion",
        ),
        UniqueConstraint(
            "customer_id",
            "board_length_mm",
            "board_width_mm",
            "normalized_material_code",
            "flute_type",
            "component_type",
            "pieces_per_box",
            "stock_yield_per_sheet",
            name="uq_semi_finished_match_rules_signature",
        ),
        Index(
            "ix_semi_finished_match_rules_active_component",
            "active",
            "component_type",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    board_length_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    board_width_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    normalized_material_code: Mapped[str] = mapped_column(String(100), nullable=False)
    flute_type: Mapped[str] = mapped_column(String(20), nullable=False)
    component_type: Mapped[str] = mapped_column(String(20), nullable=False)
    pieces_per_box: Mapped[int] = mapped_column(Integer, nullable=False)
    stock_yield_per_sheet: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )


class SemiFinishedMatchRuleProduct(Base):
    """Shared recommendation memory; this is not a per-lot authorization."""

    __tablename__ = "semi_finished_match_rule_products"
    __table_args__ = (
        UniqueConstraint(
            "rule_id",
            "product_id",
            name="uq_semi_finished_match_rule_products_rule_product",
        ),
        Index(
            "ix_semi_finished_match_rule_products_product",
            "product_id",
            "rule_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(
        ForeignKey("semi_finished_match_rules.id", ondelete="CASCADE"),
        nullable=False,
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    confirmed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class SemiFinishedLotAllowedProduct(Base):
    """Hard product binding for one semi-finished inventory lot."""

    __tablename__ = "semi_finished_lot_allowed_products"
    __table_args__ = (
        UniqueConstraint(
            "inventory_lot_id",
            "product_id",
            name="uq_semi_finished_lot_allowed_products_lot_product",
        ),
        Index(
            "ix_semi_finished_lot_allowed_products_product_lot",
            "product_id",
            "inventory_lot_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    inventory_lot_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    confirmed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    lot: Mapped["InventoryLot"] = relationship(back_populates="allowed_products")
    product: Mapped["Product"] = relationship()


class InventoryReservation(Base):
    __tablename__ = "inventory_reservations"
    __table_args__ = (
        CheckConstraint(
            "reservation_type IN "
            "('finished_order','finished_surplus_delivery','semi_requisition','semi_order')",
            name="ck_inventory_reservations_type",
        ),
        CheckConstraint(
            "status IN ('active','partial','released','consumed','cancelled')",
            name="ck_inventory_reservations_status",
        ),
        CheckConstraint(
            "reserved_stock_quantity > 0",
            name="ck_inventory_reservations_quantity",
        ),
        CheckConstraint(
            "consumed_stock_quantity >= 0 AND released_stock_quantity >= 0 "
            "AND consumed_stock_quantity + released_stock_quantity "
            "<= reserved_stock_quantity",
            name="ck_inventory_reservations_cumulative_quantities",
        ),
        CheckConstraint(
            "consumed_requirement_quantity >= 0 "
            "AND released_requirement_quantity >= 0 "
            "AND (credited_requirement_quantity IS NULL OR "
            "consumed_requirement_quantity + released_requirement_quantity "
            "<= credited_requirement_quantity)",
            name="ck_inventory_reservations_cumulative_requirement_quantities",
        ),
        UniqueConstraint("reservation_number", name="uq_inventory_reservations_number"),
        UniqueConstraint("idempotency_key", name="uq_inventory_reservations_idempotency"),
        UniqueConstraint(
            "reservation_group_key",
            "inventory_lot_id",
            name="uq_inventory_reservations_group_lot",
        ),
        Index("ix_inventory_reservations_lot_status", "inventory_lot_id", "status"),
        Index(
            "ix_inventory_reservations_order_item",
            "order_item_id",
            "reservation_type",
            "status",
        ),
        Index("ix_inventory_reservations_requisition", "requisition_item_id"),
        Index(
            "ix_inventory_reservations_semi_requirement",
            "semi_requirement_id",
            "status",
        ),
        Index("ix_inventory_reservations_match_rule", "match_rule_id"),
        Index("ix_inventory_reservations_group", "reservation_group_key"),
        Index(
            "ix_inventory_reservations_bom_component",
            "sales_order_item_bom_component_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    reservation_number: Mapped[str] = mapped_column(String(50), nullable=False)
    inventory_lot_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=False
    )
    reservation_type: Mapped[str] = mapped_column(String(30), nullable=False)
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_orders.id", ondelete="SET NULL"), nullable=True
    )
    order_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_order_items.id", ondelete="SET NULL"), nullable=True
    )
    sales_order_item_bom_component_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_order_item_bom_components.id", ondelete="SET NULL"),
        nullable=True,
    )
    requisition_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("material_requisition_items.id", ondelete="SET NULL"), nullable=True
    )
    semi_requirement_id: Mapped[int | None] = mapped_column(
        ForeignKey("order_item_semi_requirements.id", ondelete="SET NULL"),
        nullable=True,
    )
    match_rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("semi_finished_match_rules.id", ondelete="SET NULL"),
        nullable=True,
    )
    reserved_stock_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    credited_requirement_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    yield_factor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cut_plan_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    consumed_stock_quantity: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    released_stock_quantity: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    consumed_requirement_quantity: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    released_requirement_quantity: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    warning_codes: Mapped[str | None] = mapped_column(Text, nullable=True)
    warning_acknowledged_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reserved_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reserved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    released_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consumed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    release_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reservation_group_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reservation_group_requested_quantity: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, onupdate=func.current_timestamp(), nullable=True
    )


class InventoryMovement(Base):
    __tablename__ = "inventory_movements"
    __table_args__ = (
        CheckConstraint(
            "movement_type IN ('manual_in','adjust','freeze','unfreeze','damage','scrap',"
            "'transfer_to_general','location_transfer','reserve','release_reserve',"
            "'consume','reverse_consume','return_in','return_reconsume')",
            name="ck_inventory_movements_type",
        ),
        CheckConstraint("quantity >= 0", name="ck_inventory_movements_quantity"),
        UniqueConstraint("movement_number", name="uq_inventory_movements_number"),
        UniqueConstraint("idempotency_key", name="uq_inventory_movements_idempotency"),
        Index("ix_inventory_movements_lot_created", "inventory_lot_id", "created_at"),
        Index("ix_inventory_movements_reservation", "reservation_id"),
        Index("ix_inventory_movements_order_item", "related_order_item_id"),
        Index("ix_inventory_movements_supplier_order", "related_supplier_order_id"),
        Index("ix_inventory_movements_delivery", "related_delivery_id"),
        Index("ix_inventory_movements_type_created", "movement_type", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    movement_number: Mapped[str] = mapped_column(String(50), nullable=False)
    inventory_lot_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=False
    )
    movement_type: Mapped[str] = mapped_column(String(30), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit: Mapped[str] = mapped_column(String(20), nullable=False)
    before_available: Mapped[int] = mapped_column(Integer, nullable=False)
    after_available: Mapped[int] = mapped_column(Integer, nullable=False)
    before_reserved: Mapped[int] = mapped_column(Integer, nullable=False)
    after_reserved: Mapped[int] = mapped_column(Integer, nullable=False)
    before_consumed: Mapped[int] = mapped_column(Integer, nullable=False)
    after_consumed: Mapped[int] = mapped_column(Integer, nullable=False)
    before_damaged: Mapped[int] = mapped_column(Integer, nullable=False)
    after_damaged: Mapped[int] = mapped_column(Integer, nullable=False)
    before_scrapped: Mapped[int] = mapped_column(Integer, nullable=False)
    after_scrapped: Mapped[int] = mapped_column(Integer, nullable=False)
    reservation_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_reservations.id", ondelete="SET NULL"), nullable=True
    )
    related_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_orders.id", ondelete="SET NULL"), nullable=True
    )
    related_order_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_order_items.id", ondelete="SET NULL"), nullable=True
    )
    related_requisition_id: Mapped[int | None] = mapped_column(
        ForeignKey("material_requisitions.id", ondelete="SET NULL"), nullable=True
    )
    related_supplier_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("supplier_requisition_orders.id", ondelete="SET NULL"), nullable=True
    )
    related_delivery_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_deliveries.id", ondelete="SET NULL"), nullable=True
    )
    reversal_of_movement_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_movements.id", ondelete="SET NULL"), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )

    lot: Mapped["InventoryLot"] = relationship(back_populates="movements")


class InventoryLotTransfer(Base):
    __tablename__ = "inventory_lot_transfers"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_inventory_lot_transfers_quantity"),
        CheckConstraint(
            "available_quantity >= 0 AND reserved_quantity >= 0 "
            "AND available_quantity + reserved_quantity = quantity",
            name="ck_inventory_lot_transfers_balance",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_inventory_lot_transfers_request_hash",
        ),
        UniqueConstraint(
            "idempotency_key", name="uq_inventory_lot_transfers_idempotency"
        ),
        Index("ix_inventory_lot_transfers_source", "source_lot_id", "transferred_at"),
        Index("ix_inventory_lot_transfers_target", "target_lot_id", "transferred_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_lot_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=False
    )
    target_lot_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=False
    )
    source_location_id: Mapped[int] = mapped_column(
        ForeignKey("warehouse_locations.id", ondelete="RESTRICT"), nullable=False
    )
    target_location_id: Mapped[int] = mapped_column(
        ForeignKey("warehouse_locations.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    available_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    source_version_before: Mapped[int] = mapped_column(Integer, nullable=False)
    source_version_after: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    transferred_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    transferred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )


class WarehouseLocationDiscrepancy(Base):
    """Employee-reported mismatch between the ledger and the physical map.

    This is a review fact, not a second inventory ledger.  The registered
    location remains authoritative until a separately authorized correction
    creates an ``InventoryLotTransfer`` in the same transaction.
    """

    __tablename__ = "warehouse_location_discrepancies"
    __table_args__ = (
        CheckConstraint(
            "reported_quantity > 0",
            name="ck_warehouse_location_discrepancies_quantity",
        ),
        CheckConstraint(
            "registered_location_id <> observed_location_id",
            name="ck_warehouse_location_discrepancies_locations",
        ),
        CheckConstraint(
            "status IN ('open','resolved','cancelled')",
            name="ck_warehouse_location_discrepancies_status",
        ),
        CheckConstraint(
            "version > 0",
            name="ck_warehouse_location_discrepancies_version",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_warehouse_location_discrepancies_idempotency",
        ),
        Index(
            "ix_warehouse_location_discrepancies_status_reported",
            "status",
            "reported_at",
            "id",
        ),
        Index(
            "ix_warehouse_location_discrepancies_lot_status",
            "inventory_lot_id",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    inventory_lot_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=False
    )
    registered_location_id: Mapped[int] = mapped_column(
        ForeignKey("warehouse_locations.id", ondelete="RESTRICT"), nullable=False
    )
    observed_location_id: Mapped[int] = mapped_column(
        ForeignKey("warehouse_locations.id", ondelete="RESTRICT"), nullable=False
    )
    # Immutable map-version snapshot captured when the employee reported the
    # observed point.  NULL is retained only for reports created before the
    # version gate existed.
    observed_location_layout_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    reported_lot_version: Mapped[int] = mapped_column(Integer, nullable=False)
    reported_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="open", server_default="open", nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    reported_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reported_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    resolved_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolution_transfer_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_lot_transfers.id", ondelete="RESTRICT"), nullable=True
    )
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class WarehouseUnmatchedInventoryObservation(Base):
    """Physical goods seen at a mapped position but not found in the ledger.

    This is an administrator review marker, never an inventory quantity fact.
    A later stocktake/onboarding transaction must create or correct inventory.
    """

    __tablename__ = "warehouse_unmatched_inventory_observations"
    __table_args__ = (
        CheckConstraint(
            "reported_quantity IS NULL OR reported_quantity > 0",
            name="ck_warehouse_unmatched_observations_quantity",
        ),
        CheckConstraint(
            "status IN ('open','resolved','cancelled')",
            name="ck_warehouse_unmatched_observations_status",
        ),
        CheckConstraint(
            "version > 0",
            name="ck_warehouse_unmatched_observations_version",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_warehouse_unmatched_observations_idempotency",
        ),
        UniqueConstraint(
            "resolution_idempotency_key",
            name="uq_warehouse_unmatched_observations_resolution_idempotency",
        ),
        Index(
            "ix_warehouse_unmatched_observations_location_status",
            "observed_location_id",
            "status",
        ),
        Index(
            "ix_warehouse_unmatched_observations_status_reported",
            "status",
            "reported_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    observed_location_id: Mapped[int] = mapped_column(
        ForeignKey("warehouse_locations.id", ondelete="RESTRICT"), nullable=False
    )
    observed_location_layout_version: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    customer_keyword: Mapped[str | None] = mapped_column(String(120), nullable=True)
    inventory_keyword: Mapped[str] = mapped_column(String(200), nullable=False)
    reported_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reported_unit: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="open", server_default="open", nullable=False
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    reported_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reported_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    resolved_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_idempotency_key: Mapped[str | None] = mapped_column(
        String(120), nullable=True
    )
    resolved_inventory_lot_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=True
    )


class DeliveryInventoryAllocation(Base):
    __tablename__ = "delivery_inventory_allocations"
    __table_args__ = (
        CheckConstraint(
            "consumed_stock_quantity > 0 AND credited_requirement_quantity > 0",
            name="ck_delivery_inventory_allocations_quantities",
        ),
        CheckConstraint(
            "reversed_stock_quantity >= 0 "
            "AND reversed_stock_quantity <= consumed_stock_quantity "
            "AND reversed_requirement_quantity >= 0 "
            "AND reversed_requirement_quantity <= credited_requirement_quantity",
            name="ck_delivery_inventory_allocations_reversed",
        ),
        CheckConstraint(
            "status IN ('active','partial','reversed')",
            name="ck_delivery_inventory_allocations_status",
        ),
        UniqueConstraint(
            "consume_movement_id",
            name="uq_delivery_inventory_allocations_consume_movement",
        ),
        Index(
            "ix_delivery_inventory_allocations_delivery_item",
            "delivery_item_id",
            "status",
        ),
        Index(
            "ix_delivery_inventory_allocations_reservation",
            "reservation_id",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    delivery_item_id: Mapped[int] = mapped_column(
        ForeignKey("sales_delivery_items.id", ondelete="RESTRICT"), nullable=False
    )
    reservation_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_reservations.id", ondelete="RESTRICT"), nullable=False
    )
    consume_movement_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_movements.id", ondelete="RESTRICT"), nullable=False
    )
    consumed_stock_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    credited_requirement_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    reversed_stock_quantity: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    reversed_requirement_quantity: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reversed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class OrderedFinishedReceiptReturn(Base):
    """Immutable receipt-short return fact for an order-backed delivery line."""

    __tablename__ = "ordered_finished_receipt_returns"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_ordered_receipt_returns_quantity"),
        CheckConstraint(
            "resolution_action IN ('continue_delivery','accept_short')",
            name="ck_ordered_receipt_returns_resolution",
        ),
        CheckConstraint(
            "(status = 'active' AND reconsume_movement_id IS NULL "
            "AND source_reconsume_movement_id IS NULL "
            "AND reconsumed_at IS NULL) OR "
            "(status = 'reconsumed' AND reconsume_movement_id IS NOT NULL "
            "AND source_reconsume_movement_id IS NOT NULL "
            "AND reconsumed_at IS NOT NULL)",
            name="ck_ordered_receipt_returns_status",
        ),
        UniqueConstraint(
            "return_receipt_item_id",
            "sequence_no",
            name="uq_ordered_receipt_returns_item_sequence",
        ),
        UniqueConstraint(
            "return_inventory_lot_id",
            name="uq_ordered_receipt_returns_lot",
        ),
        UniqueConstraint(
            "return_in_movement_id",
            name="uq_ordered_receipt_returns_in_movement",
        ),
        UniqueConstraint(
            "source_reverse_movement_id",
            name="uq_ordered_receipt_returns_source_reverse_movement",
        ),
        UniqueConstraint(
            "source_transfer_movement_id",
            name="uq_ordered_receipt_returns_source_transfer_movement",
        ),
        UniqueConstraint(
            "reconsume_movement_id",
            name="uq_ordered_receipt_returns_reconsume_movement",
        ),
        UniqueConstraint(
            "source_reconsume_movement_id",
            name="uq_ordered_receipt_returns_source_reconsume_movement",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_ordered_receipt_returns_idempotency",
        ),
        Index(
            "ix_ordered_receipt_returns_receipt_item",
            "return_receipt_item_id",
            "status",
        ),
        Index(
            "ix_ordered_receipt_returns_location",
            "return_location_id",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    return_receipt_item_id: Mapped[int] = mapped_column(
        ForeignKey("finance_return_receipt_items.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    delivery_item_id: Mapped[int] = mapped_column(
        ForeignKey("sales_delivery_items.id", ondelete="RESTRICT"), nullable=False
    )
    delivery_inventory_allocation_id: Mapped[int] = mapped_column(
        ForeignKey("delivery_inventory_allocations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_inventory_lot_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=False
    )
    return_inventory_lot_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=False
    )
    return_location_id: Mapped[int] = mapped_column(
        ForeignKey("warehouse_locations.id", ondelete="RESTRICT"), nullable=False
    )
    reservation_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_reservations.id", ondelete="RESTRICT"), nullable=True
    )
    return_in_movement_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_movements.id", ondelete="RESTRICT"), nullable=False
    )
    source_reverse_movement_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_movements.id", ondelete="RESTRICT"), nullable=False
    )
    source_transfer_movement_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_movements.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    resolution_action: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="active", server_default="active", nullable=False
    )
    reconsume_movement_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_movements.id", ondelete="RESTRICT"), nullable=True
    )
    source_reconsume_movement_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_movements.id", ondelete="RESTRICT"), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
    reconsumed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reconsumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    return_lot: Mapped["InventoryLot"] = relationship(
        foreign_keys=[return_inventory_lot_id]
    )
    source_lot: Mapped["InventoryLot"] = relationship(
        foreign_keys=[source_inventory_lot_id]
    )
    return_location: Mapped["WarehouseLocation"] = relationship(
        foreign_keys=[return_location_id]
    )
    reservation: Mapped["InventoryReservation | None"] = relationship(
        foreign_keys=[reservation_id]
    )


class UnorderedFinishedDeliveryAllocation(Base):
    """Planned and posted lot effects for one order-less finished-goods delivery line.

    This deliberately does not use ``InventoryReservation`` or
    ``DeliveryInventoryAllocation``.  A draft records the intended lots only;
    the formal dispatch service must later create the inventory movement.
    """

    __tablename__ = "unordered_finished_delivery_allocations"
    __table_args__ = (
        CheckConstraint(
            "planned_quantity > 0",
            name="ck_unordered_finished_delivery_allocations_planned_quantity",
        ),
        CheckConstraint(
            "consumed_quantity >= 0 AND consumed_quantity <= planned_quantity "
            "AND restored_quantity >= 0 AND restored_quantity <= consumed_quantity",
            name="ck_unordered_finished_delivery_allocations_quantity_progress",
        ),
        CheckConstraint(
            "status IN ('planned', 'dispatched', 'partial_restored', 'restored')",
            name="ck_unordered_finished_delivery_allocations_status",
        ),
        UniqueConstraint(
            "delivery_item_id",
            "inventory_lot_id",
            name="uq_unordered_finished_delivery_allocations_item_lot",
        ),
        UniqueConstraint(
            "consume_movement_id",
            name="uq_unordered_finished_delivery_allocations_consume_movement",
        ),
        Index(
            "ix_unordered_finished_delivery_allocations_item_status",
            "delivery_item_id",
            "status",
        ),
        Index(
            "ix_unordered_finished_delivery_allocations_lot_status",
            "inventory_lot_id",
            "status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    delivery_item_id: Mapped[int] = mapped_column(
        ForeignKey("sales_delivery_items.id", ondelete="RESTRICT"), nullable=False
    )
    inventory_lot_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_lots.id", ondelete="RESTRICT"), nullable=False
    )
    planned_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    consumed_quantity: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    restored_quantity: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(24), default="planned", server_default="planned", nullable=False
    )
    lot_number_snapshot: Mapped[str | None] = mapped_column(String(50), nullable=True)
    warehouse_location_id_snapshot: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    warehouse_location_code_snapshot: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    pallet_code_snapshot: Mapped[str | None] = mapped_column(String(100), nullable=True)
    consume_movement_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_movements.id", ondelete="RESTRICT"), nullable=True
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    dispatched_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )

    delivery_item: Mapped["DeliveryItem"] = relationship()
    inventory_lot: Mapped["InventoryLot"] = relationship()


class UnorderedFinishedDeliveryReversal(Base):
    """Immutable audit row for an order-less delivery lot restoration."""

    __tablename__ = "unordered_finished_delivery_reversals"
    __table_args__ = (
        CheckConstraint(
            "reversal_kind IN ('dispatch_cancel', 'receipt_short_return', "
            "'receipt_refusal_return')",
            name="ck_unordered_finished_delivery_reversals_kind",
        ),
        CheckConstraint(
            "reversal_quantity > 0",
            name="ck_unordered_finished_delivery_reversals_quantity",
        ),
        CheckConstraint(
            "(status = 'active' AND reconsumed_movement_id IS NULL "
            "AND reconsumed_at IS NULL) OR "
            "(status = 'reconsumed' AND reconsumed_movement_id IS NOT NULL "
            "AND reconsumed_at IS NOT NULL)",
            name="ck_unordered_finished_delivery_reversals_reconsumed",
        ),
        UniqueConstraint(
            "inventory_movement_id",
            name="uq_unordered_finished_delivery_reversals_movement",
        ),
        UniqueConstraint(
            "reconsumed_movement_id",
            name="uq_unordered_finished_delivery_reversals_reconsumed_movement",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_unordered_finished_delivery_reversals_idempotency",
        ),
        Index(
            "ix_unordered_finished_delivery_reversals_allocation",
            "allocation_id",
        ),
        Index(
            "ix_unordered_finished_delivery_reversals_receipt_item",
            "return_receipt_item_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    allocation_id: Mapped[int] = mapped_column(
        ForeignKey("unordered_finished_delivery_allocations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    return_receipt_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("finance_return_receipt_items.id", ondelete="RESTRICT"), nullable=True
    )
    inventory_movement_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_movements.id", ondelete="RESTRICT"), nullable=False
    )
    reversal_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    reversal_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="active", server_default="active", nullable=False
    )
    reconsumed_movement_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_movements.id", ondelete="RESTRICT"), nullable=True
    )
    reconsumed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reconsumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )

    allocation: Mapped["UnorderedFinishedDeliveryAllocation"] = relationship()
    inventory_movement: Mapped["InventoryMovement"] = relationship(
        foreign_keys=[inventory_movement_id]
    )
    reconsumed_movement: Mapped["InventoryMovement | None"] = relationship(
        foreign_keys=[reconsumed_movement_id]
    )
