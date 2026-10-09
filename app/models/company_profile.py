"""Issuer companies own private seals; customers never own seller seals."""
from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base


class CompanyProfile(Base):
    __tablename__ = "company_profiles"
    __table_args__ = (CheckConstraint("version >= 0 AND seal_version >= 0", name="ck_company_profiles_versions"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_name: Mapped[str] = mapped_column(String(100), unique=True)
    details_json: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=0)
    seal_version: Mapped[int] = mapped_column(Integer, default=0)
    active_seal_id: Mapped[int | None] = mapped_column(ForeignKey("contract_seals.id", ondelete="RESTRICT"))


class CompanySelection(Base):
    __tablename__ = "company_selection"
    __table_args__ = (CheckConstraint("id = 1 AND version >= 0", name="ck_company_selection_singleton"),)
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    version: Mapped[int] = mapped_column(Integer, default=0)
    active_company_id: Mapped[int] = mapped_column(ForeignKey("company_profiles.id", ondelete="RESTRICT"))
    # Captured before the first company change; never derive old headers from a new default.
    legacy_details_json: Mapped[str] = mapped_column(Text)


class ContractCompanySnapshot(Base):
    __tablename__ = "contract_company_snapshots"
    contract_id: Mapped[int] = mapped_column(ForeignKey("customer_contracts.id", ondelete="CASCADE"), primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company_profiles.id", ondelete="RESTRICT"))
    details_json: Mapped[str] = mapped_column(Text)
