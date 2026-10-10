from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class ExcelOrderImportBatch(Base):
    """Immutable source ledger for one customer Excel file."""

    __tablename__ = "excel_order_import_batches"
    __table_args__ = (
        UniqueConstraint(
            "customer_id",
            "source_type",
            "source_sha256",
            name="uq_excel_order_import_batches_source",
        ),
        Index(
            "ix_excel_order_import_batches_customer_created",
            "customer_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(Integer, nullable=False)
    customer_code_snapshot: Mapped[str | None] = mapped_column(String(50), nullable=True)
    customer_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    source_type: Mapped[str] = mapped_column(String(80), nullable=False)
    source_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(80), nullable=False)
    worksheet_name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_source_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )


class ExcelOrderImportRow(Base):
    """Immutable normalized source-row snapshot belonging to an import batch."""

    __tablename__ = "excel_order_import_rows"
    __table_args__ = (
        UniqueConstraint(
            "batch_id",
            "source_row_number",
            name="uq_excel_order_import_rows_batch_row",
        ),
        Index("ix_excel_order_import_rows_batch", "batch_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    source_row_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_row_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )


class ExcelOrderImportConversion(Base):
    """Immutable fact that one source batch became one formal order."""

    __tablename__ = "excel_order_import_conversions"
    __table_args__ = (
        UniqueConstraint(
            "batch_id", name="uq_excel_order_import_conversions_batch"
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_excel_order_import_conversions_idempotency",
        ),
        Index("ix_excel_order_import_conversions_order", "order_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    normalized_order_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    order_id: Mapped[int] = mapped_column(Integer, nullable=False)
    operator_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confirmation_token_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp(), nullable=False
    )
