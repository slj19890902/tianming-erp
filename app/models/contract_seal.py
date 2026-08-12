from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class ContractSealAssetVersion(Base):
    __tablename__ = "contract_seal_asset_versions"
    __table_args__ = (
        UniqueConstraint("version", name="uq_contract_seal_asset_versions_version"),
        UniqueConstraint("sha256", name="uq_contract_seal_asset_versions_sha256"),
        CheckConstraint("version >= 1", name="ck_contract_seal_asset_versions_version"),
        CheckConstraint("width_px > 0 AND height_px > 0", name="ck_contract_seal_asset_versions_dimensions"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(200), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(50), nullable=False)
    png_content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    width_px: Mapped[int] = mapped_column(Integer, nullable=False)
    height_px: Mapped[int] = mapped_column(Integer, nullable=False)
    uploaded_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    uploaded_by_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )


class ContractSealSelection(Base):
    __tablename__ = "contract_seal_selection"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_contract_seal_selection_singleton"),
        CheckConstraint("version >= 1", name="ck_contract_seal_selection_version"),
        CheckConstraint(
            "is_enabled = 0 OR asset_version_id IS NOT NULL",
            name="ck_contract_seal_selection_enabled_asset",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    asset_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("contract_seal_asset_versions.id", ondelete="RESTRICT"), nullable=True
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_operation_key: Mapped[str | None] = mapped_column(String(120), nullable=True, unique=True)
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp(), onupdate=func.current_timestamp()
    )


class ContractSealedPdfArchive(Base):
    __tablename__ = "contract_sealed_pdf_archives"
    __table_args__ = (
        UniqueConstraint("operation_key", name="uq_contract_sealed_pdf_archives_operation_key"),
        CheckConstraint("contract_version >= 1", name="ck_contract_sealed_pdf_archives_contract_version"),
        CheckConstraint("contract_status IN ('confirmed','converted')", name="ck_contract_sealed_pdf_archives_status"),
        Index("ix_contract_sealed_pdf_archives_contract_time", "contract_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(
        ForeignKey("customer_contracts.id", ondelete="RESTRICT"), nullable=False
    )
    contract_version: Mapped[int] = mapped_column(Integer, nullable=False)
    contract_status: Mapped[str] = mapped_column(String(20), nullable=False)
    contract_no_snapshot: Mapped[str] = mapped_column(String(40), nullable=False)
    customer_id_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    customer_name_snapshot: Mapped[str] = mapped_column(String(250), nullable=False)
    template_version: Mapped[str] = mapped_column(String(50), nullable=False)
    seal_asset_version_id: Mapped[int] = mapped_column(
        ForeignKey("contract_seal_asset_versions.id", ondelete="RESTRICT"), nullable=False
    )
    seal_asset_version: Mapped[int] = mapped_column(Integer, nullable=False)
    seal_selection_version: Mapped[int] = mapped_column(Integer, nullable=False)
    seal_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    pdf_content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    pdf_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    download_filename: Mapped[str] = mapped_column(String(240), nullable=False)
    operation_key: Mapped[str] = mapped_column(String(120), nullable=False)
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    actor_username_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_role_snapshot: Mapped[str] = mapped_column(String(30), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )


def _reject_immutable_fact_change(_mapper, _connection, target) -> None:
    raise ValueError(f"{target.__class__.__name__} 是不可变归档事实，禁止修改或删除")


event.listen(ContractSealAssetVersion, "before_update", _reject_immutable_fact_change)
event.listen(ContractSealAssetVersion, "before_delete", _reject_immutable_fact_change)
event.listen(ContractSealedPdfArchive, "before_update", _reject_immutable_fact_change)
event.listen(ContractSealedPdfArchive, "before_delete", _reject_immutable_fact_change)
