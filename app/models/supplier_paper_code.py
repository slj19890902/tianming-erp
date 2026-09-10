from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class SupplierPaperCode(Base):
    __tablename__ = "supplier_paper_codes"
    __table_args__ = (
        UniqueConstraint(
            "supplier_name",
            "code_char",
            name="uq_supplier_paper_codes_supplier_char",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    supplier_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    code_char: Mapped[str] = mapped_column(String(1), nullable=False)
    paper_name: Mapped[str] = mapped_column(String(250), nullable=False)
    gram_weight: Mapped[int] = mapped_column(Integer, nullable=False)
    color: Mapped[str] = mapped_column(String(10), default="kraft", server_default="kraft", nullable=False)
    paper_grade: Mapped[str | None] = mapped_column(String(100), nullable=True)
    paper_role: Mapped[str | None] = mapped_column(String(50), nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        onupdate=func.current_timestamp(),
    )
