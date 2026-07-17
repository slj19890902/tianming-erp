from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class MasterDataObjectVersion(Base):
    """Append-only snapshots for recoverable master-data fields."""

    __tablename__ = "master_data_object_versions"
    __table_args__ = (
        UniqueConstraint(
            "object_type",
            "object_id",
            "version",
            name="uq_master_data_object_versions_object_version",
        ),
        CheckConstraint(
            "object_type IN ('customer', 'product', 'material')",
            name="ck_master_data_object_versions_object_type",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_master_data_object_versions_version",
        ),
        CheckConstraint(
            "snapshot_schema_version >= 1",
            name="ck_master_data_object_versions_snapshot_schema_version",
        ),
        CheckConstraint(
            "restored_from_version IS NULL OR "
            "(restored_from_version >= 1 AND restored_from_version < version)",
            name="ck_master_data_object_versions_restored_from_version",
        ),
        CheckConstraint(
            "length(snapshot_sha256) = 64",
            name="ck_master_data_object_versions_snapshot_sha256",
        ),
        Index(
            "ix_master_data_object_versions_object_created",
            "object_type",
            "object_id",
            "created_at",
        ),
        Index(
            "ix_master_data_object_versions_change_set_id",
            "change_set_id",
        ),
        Index(
            "ix_master_data_object_versions_operation_log_id",
            "operation_log_id",
        ),
        Index(
            "ix_master_data_object_versions_actor_user_id",
            "actor_user_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    object_type: Mapped[str] = mapped_column(String(20), nullable=False)
    object_id: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    snapshot_schema_version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default="1",
        nullable=False,
    )
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    changed_fields_json: Mapped[str] = mapped_column(
        Text,
        default="{}",
        server_default="{}",
        nullable=False,
    )
    restored_from_version: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    change_set_id: Mapped[str] = mapped_column(String(36), nullable=False)
    operation_log_id: Mapped[int | None] = mapped_column(
        ForeignKey("operation_logs.id", ondelete="SET NULL"),
        nullable=True,
    )
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    actor_username_snapshot: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )
