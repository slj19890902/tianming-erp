"""Personal checklist and company file library, independent of business queues."""
from datetime import datetime
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base


class HomeMemo(Base):
    __tablename__ = "home_memos"
    __table_args__ = (CheckConstraint("version > 0", name="ck_home_memo_version"),
        Index("ix_home_memo_owner_state", "owner_id", "deleted", "completed", "updated_at"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    text: Mapped[str] = mapped_column(String(600))
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class SharedFile(Base):
    __tablename__ = "shared_files"
    __table_args__ = (CheckConstraint("version > 0", name="ck_shared_file_version"),
        Index("ix_shared_file_deleted_updated", "deleted", "updated_at"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    updated_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    content_version: Mapped[int] = mapped_column(Integer, default=1)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class SharedFileVersion(Base):
    __tablename__ = "shared_file_versions"
    __table_args__ = (UniqueConstraint("file_id", "version", name="uq_shared_file_version"),
        CheckConstraint("version > 0 AND size > 0", name="ck_shared_file_content"))
    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("shared_files.id", ondelete="RESTRICT"))
    version: Mapped[int] = mapped_column(Integer)
    reference: Mapped[str] = mapped_column(String(255), unique=True)
    original_name: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(120))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime)


class HomeCollaborationMutation(Base):
    __tablename__ = "home_collaboration_mutations"
    __table_args__ = (UniqueConstraint("actor_id", "request_key", name="uq_home_collaboration_request"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    request_key: Mapped[str] = mapped_column(String(80))
    request_hash: Mapped[str] = mapped_column(String(64))
    response_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime)
