"""Managed drawings are separate from legacy product photo attachments."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class DrawingDesign(Base):
    __tablename__ = "drawing_designs"
    __table_args__ = (CheckConstraint("version >= 1", name="ck_drawing_design_version"),)

    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True)
    template_key: Mapped[str] = mapped_column(String(50), nullable=False)
    parameters_json: Mapped[str] = mapped_column(Text, nullable=False)
    print_objects_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    paper_color: Mapped[str] = mapped_column(String(20), nullable=False, default="white")
    thickness_mm: Mapped[str | None] = mapped_column(String(30))
    thickness_source: Mapped[str | None] = mapped_column(String(160))
    thickness_approximate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    customer_number: Mapped[str | None] = mapped_column(String(160))
    customer_revision: Mapped[str | None] = mapped_column(String(50))
    internal_number: Mapped[str | None] = mapped_column(String(30), unique=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_product_version: Mapped[int] = mapped_column(Integer, nullable=False)
    last_save_key: Mapped[str | None] = mapped_column(String(100))
    last_save_hash: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp(), onupdate=func.current_timestamp())


class DrawingNumberSequence(Base):
    __tablename__ = "drawing_number_sequences"

    business_date: Mapped[str] = mapped_column(String(8), primary_key=True)
    last_number: Mapped[int] = mapped_column(Integer, nullable=False)


class DrawingRelease(Base):
    __tablename__ = "drawing_releases"
    __table_args__ = (UniqueConstraint("product_id", "external_number", "revision", name="uq_drawing_release_product_revision"),
                      UniqueConstraint("product_id", "design_version", "product_version", name="uq_drawing_release_source_version"),
                      UniqueConstraint("product_id", "idempotency_key", name="uq_drawing_release_idempotency"),
                      Index("ix_drawing_release_customer_product", "customer_id", "product_id"))

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False)
    revision: Mapped[str] = mapped_column(String(50), nullable=False)
    external_number: Mapped[str] = mapped_column(String(160), nullable=False)
    internal_number: Mapped[str] = mapped_column(String(30), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    design_version: Mapped[int] = mapped_column(Integer, nullable=False)
    product_version: Mapped[int] = mapped_column(Integer, nullable=False)
    template_key: Mapped[str] = mapped_column(String(50), nullable=False)
    manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    pdf_reference: Mapped[str] = mapped_column(Text, nullable=False)
    pdf_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mold_tool_id: Mapped[int | None] = mapped_column(ForeignKey("mold_tools.id", ondelete="RESTRICT"))
    published_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    published_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp(), nullable=False)


class ProductionTaskDrawing(Base):
    __tablename__ = "production_task_drawings"

    task_id: Mapped[int] = mapped_column(ForeignKey("production_tasks.id", ondelete="RESTRICT"), primary_key=True)
    release_id: Mapped[int] = mapped_column(ForeignKey("drawing_releases.id", ondelete="RESTRICT"), nullable=False)
    bound_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp(), nullable=False)
    bound_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class ProductionTaskDrawingAdoption(Base):
    """Explicit task-only adoption; original bindings and earlier events remain."""
    __tablename__ = "production_task_drawing_adoptions"
    __table_args__ = (
        UniqueConstraint("task_id", "resulting_task_version", name="uq_task_drawing_adoption_version"),
        CheckConstraint("expected_task_version >= 1 AND resulting_task_version = expected_task_version + 1",
                        name="ck_task_drawing_adoption_version"),
        CheckConstraint("confirmed_not_issued = 1", name="ck_task_drawing_adoption_confirmation"),
        Index("ix_task_drawing_adoption_task_id", "task_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("production_tasks.id", ondelete="RESTRICT"), nullable=False)
    old_release_id: Mapped[int | None] = mapped_column(ForeignKey("drawing_releases.id", ondelete="RESTRICT"))
    new_release_id: Mapped[int] = mapped_column(ForeignKey("drawing_releases.id", ondelete="RESTRICT"), nullable=False)
    legacy_reference: Mapped[str | None] = mapped_column(Text)
    expected_task_version: Mapped[int] = mapped_column(Integer, nullable=False)
    resulting_task_version: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmed_not_issued: Mapped[int] = mapped_column(Integer, nullable=False)
    adopted_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    adopted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp(), nullable=False)
