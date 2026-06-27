from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String

from app.models import Base


class CompanyConfig(Base):
    """单行配置表，始终只有 id=1 的记录。"""

    __tablename__ = "company_config"

    id = Column(Integer, primary_key=True, default=1)
    company_name = Column(String(100), nullable=False, default="")
    short_name = Column(String(50), nullable=True)
    address = Column(String(200), nullable=True)
    phone = Column(String(50), nullable=True)
    fax = Column(String(50), nullable=True)
    tax_number = Column(String(50), nullable=True)
    bank_name = Column(String(100), nullable=True)
    bank_account = Column(String(50), nullable=True)
    contact_person = Column(String(50), nullable=True)
    contact_phone = Column(String(50), nullable=True)
    updated_at = Column(DateTime, nullable=True)
