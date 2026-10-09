"""Private seal assets and immutable export receipts, never public attachments."""
from datetime import datetime
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, LargeBinary, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base


class ContractSeal(Base):
    __tablename__ = "contract_seals"
    id: Mapped[int] = mapped_column(primary_key=True)
    image_png: Mapped[bytes] = mapped_column(LargeBinary)
    sha256: Mapped[str] = mapped_column(String(64))
    width_px: Mapped[int] = mapped_column(Integer)
    height_px: Mapped[int] = mapped_column(Integer)
    size_mm: Mapped[int] = mapped_column(Integer)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())


class ContractSealState(Base):
    __tablename__ = "contract_seal_state"
    __table_args__ = (CheckConstraint("id = 1 AND version >= 0", name="ck_contract_seal_state_singleton"),)
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    version: Mapped[int] = mapped_column(Integer, default=0)
    active_seal_id: Mapped[int | None] = mapped_column(ForeignKey("contract_seals.id", ondelete="RESTRICT"))


class ContractSealedExport(Base):
    __tablename__ = "contract_sealed_exports"
    id: Mapped[int] = mapped_column(primary_key=True)
    operation_key: Mapped[str] = mapped_column(String(100), unique=True)
    request_json: Mapped[str] = mapped_column(Text)
    # Numeric document reference survives deletion of a draft; the PDF is its snapshot.
    contract_id: Mapped[int] = mapped_column(Integer, index=True)
    contract_version: Mapped[int] = mapped_column(Integer)
    seal_id: Mapped[int] = mapped_column(ForeignKey("contract_seals.id", ondelete="RESTRICT"))
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    pdf_content: Mapped[bytes] = mapped_column(LargeBinary)
    pdf_sha256: Mapped[str] = mapped_column(String(64))
    template_version: Mapped[str] = mapped_column(String(40))
    font_sha256: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(250))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())
