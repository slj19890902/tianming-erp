from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


class Layout(Base):
    __tablename__ = "twin_layouts"
    __table_args__ = (
        UniqueConstraint("source_sha256", "floor_code", name="uq_twin_layout_source_floor"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    floor_code: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_units: Mapped[str] = mapped_column(String(20), nullable=False)
    bounds_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    structures_json: Mapped[list] = mapped_column(JSON, nullable=False)
    warnings_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    placements: Mapped[list["EquipmentPlacement"]] = relationship(
        back_populates="layout", cascade="all, delete-orphan"
    )
    racks: Mapped[list["RackPlacement"]] = relationship(
        back_populates="layout", cascade="all, delete-orphan"
    )
    features: Mapped[list["LayoutFeature"]] = relationship(
        back_populates="layout", cascade="all, delete-orphan"
    )


class AssetTemplate(Base):
    __tablename__ = "twin_asset_templates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    category: Mapped[str] = mapped_column(String(80), nullable=False, default="生产设备")
    render_type: Mapped[str] = mapped_column(String(20), nullable=False, default="box25d")
    image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    color: Mapped[str] = mapped_column(String(20), nullable=False, default="#2563eb")
    default_width_mm: Mapped[float] = mapped_column(Float, nullable=False)
    default_depth_mm: Mapped[float] = mapped_column(Float, nullable=False)
    default_height_mm: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    placements: Mapped[list["EquipmentPlacement"]] = relationship(
        back_populates="template"
    )


class EquipmentPlacement(Base):
    __tablename__ = "twin_equipment_placements"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    layout_id: Mapped[str] = mapped_column(
        ForeignKey("twin_layouts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    template_id: Mapped[str] = mapped_column(
        ForeignKey("twin_asset_templates.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    x_mm: Mapped[float] = mapped_column(Float, nullable=False)
    y_mm: Mapped[float] = mapped_column(Float, nullable=False)
    z_mm: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    width_mm: Mapped[float] = mapped_column(Float, nullable=False)
    depth_mm: Mapped[float] = mapped_column(Float, nullable=False)
    height_mm: Mapped[float] = mapped_column(Float, nullable=False)
    rotation_deg: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    layout: Mapped[Layout] = relationship(back_populates="placements")
    template: Mapped[AssetTemplate] = relationship(back_populates="placements")


class RackPlacement(Base):
    __tablename__ = "twin_rack_placements"
    __table_args__ = (
        UniqueConstraint("layout_id", "rack_code", name="uq_twin_rack_layout_code"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    layout_id: Mapped[str] = mapped_column(
        ForeignKey("twin_layouts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rack_code: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    x_mm: Mapped[float] = mapped_column(Float, nullable=False)
    y_mm: Mapped[float] = mapped_column(Float, nullable=False)
    z_mm: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    width_mm: Mapped[float] = mapped_column(Float, nullable=False)
    depth_mm: Mapped[float] = mapped_column(Float, nullable=False)
    height_mm: Mapped[float] = mapped_column(Float, nullable=False)
    levels: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    bays: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    access_side: Mapped[str] = mapped_column(String(20), nullable=False, default="south")
    min_aisle_width_mm: Mapped[float] = mapped_column(Float, nullable=False, default=1200)
    rotation_deg: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    color: Mapped[str] = mapped_column(String(20), nullable=False, default="#8b5cf6")
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="candidate")
    is_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    layout: Mapped[Layout] = relationship(back_populates="racks")


class LayoutFeature(Base):
    __tablename__ = "twin_layout_features"
    __table_args__ = (
        UniqueConstraint("layout_id", "feature_code", name="uq_twin_feature_layout_code"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    layout_id: Mapped[str] = mapped_column(
        ForeignKey("twin_layouts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    feature_code: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    feature_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    subtype: Mapped[str] = mapped_column(String(50), nullable=False)
    points_json: Mapped[list] = mapped_column(JSON, nullable=False)
    width_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    direction: Mapped[str | None] = mapped_column(String(20), nullable=True)
    no_stacking: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    storage_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="floor")
    elevation_mm: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    storage_height_mm: Mapped[float] = mapped_column(Float, nullable=False, default=1000)
    color: Mapped[str] = mapped_column(String(20), nullable=False, default="#3b82f6")
    area_mm2: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="candidate")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    layout: Mapped[Layout] = relationship(back_populates="features")
