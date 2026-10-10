from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base

if TYPE_CHECKING:
    from app.models.customer import Customer


class EmailOrderIntakePollState(Base):
    __tablename__ = "email_order_intake_poll_states"
    __table_args__ = (
        CheckConstraint(
            "last_status IN ('never','running','success','failed')",
            name="ck_email_intake_poll_states_status",
        ),
    )

    mailbox_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="never", server_default="never"
    )
    examined_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    duplicate_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    failed_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )


class EmailOrderSenderMapping(Base):
    __tablename__ = "email_order_sender_mappings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    sender_email: Mapped[str] = mapped_column(String(320), nullable=False)
    normalized_sender_email: Mapped[str] = mapped_column(
        String(320), nullable=False, unique=True, index=True
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    customer: Mapped["Customer"] = relationship()


class EmailOrderIntakeMessage(Base):
    __tablename__ = "email_order_intake_messages"
    __table_args__ = (
        UniqueConstraint(
            "mailbox_key",
            "imap_uidvalidity",
            "imap_uid",
            name="uq_email_intake_message_mailbox_uid",
        ),
        CheckConstraint(
            "status IN ('received','review_ready','needs_mapping','needs_confirmation',"
            "'waiting_excel_parser','failed','ignored')",
            name="ck_email_intake_messages_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    mailbox_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    imap_uidvalidity: Mapped[str] = mapped_column(String(100), nullable=False)
    imap_uid: Mapped[str] = mapped_column(String(100), nullable=False)
    message_id_header: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sender_email: Mapped[str] = mapped_column(String(320), nullable=False)
    normalized_sender_email: Mapped[str] = mapped_column(
        String(320), nullable=False, index=True
    )
    subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    raw_message_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mapped_customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(
        String(40), nullable=False, default="received", index=True
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    attachments: Mapped[list["EmailOrderIntakeAttachment"]] = relationship(
        back_populates="message", cascade="all, delete-orphan", passive_deletes=True
    )
    drafts: Mapped[list["EmailOrderIntakeDraft"]] = relationship(
        back_populates="message", cascade="all, delete-orphan", passive_deletes=True
    )
    mapped_customer: Mapped["Customer | None"] = relationship()


class EmailOrderIntakeAttachment(Base):
    __tablename__ = "email_order_intake_attachments"
    __table_args__ = (
        UniqueConstraint(
            "message_id", "part_index", name="uq_email_intake_attachment_part"
        ),
        CheckConstraint(
            "status IN ('stored','parsed','waiting_excel_parser','unsupported','failed')",
            name="ck_email_intake_attachments_status",
        ),
        CheckConstraint(
            "file_type IN ('pdf','xls','xlsx','unsupported')",
            name="ck_email_intake_attachments_file_type",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("email_order_intake_messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    part_index: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    storage_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    file_type: Mapped[str] = mapped_column(String(20), nullable=False)
    is_duplicate_content: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    duplicate_of_attachment_id: Mapped[int | None] = mapped_column(
        ForeignKey("email_order_intake_attachments.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    message: Mapped["EmailOrderIntakeMessage"] = relationship(
        back_populates="attachments"
    )
    draft: Mapped["EmailOrderIntakeDraft | None"] = relationship(
        back_populates="attachment", uselist=False, passive_deletes=True
    )


class EmailOrderIntakeDraft(Base):
    __tablename__ = "email_order_intake_drafts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('review_ready','needs_mapping','needs_confirmation',"
            "'waiting_excel_parser','failed','archived','converted')",
            name="ck_email_intake_drafts_status",
        ),
        CheckConstraint(
            "parser_type IN ('pdf','xls','xlsx')",
            name="ck_email_intake_drafts_parser_type",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("email_order_intake_messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attachment_id: Mapped[int] = mapped_column(
        ForeignKey("email_order_intake_attachments.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    parser_type: Mapped[str] = mapped_column(String(20), nullable=False)
    parse_method: Mapped[str | None] = mapped_column(String(40), nullable=True)
    parse_status: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    draft_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_name: Mapped[str] = mapped_column(String(500), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    converted_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("sales_orders.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
        index=True,
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    message: Mapped["EmailOrderIntakeMessage"] = relationship(back_populates="drafts")
    attachment: Mapped["EmailOrderIntakeAttachment"] = relationship(
        back_populates="draft"
    )
    customer: Mapped["Customer | None"] = relationship()
