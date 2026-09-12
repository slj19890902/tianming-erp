"""Mail intake staging; no automatic sales facts."""
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, String, Text, LargeBinary, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base


class EmailIntakeSettings(Base):
    __tablename__ = 'email_intake_settings'
    __table_args__ = (
        CheckConstraint('sync_interval_minutes BETWEEN 1 AND 60', name='ck_email_intake_sync_interval'),
        CheckConstraint("last_sync_status IN ('never','running','success','failed')", name='ck_email_intake_sync_status'),
        CheckConstraint('last_sync_received >= 0 AND last_sync_remaining >= 0', name='ck_email_intake_sync_counts'),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    encrypted_secret: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    sender_addresses_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    automatic_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    sync_interval_minutes: Mapped[int] = mapped_column(Integer, default=5)
    last_sync_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sync_completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_sync_status: Mapped[str] = mapped_column(String(30), default="never")
    last_sync_received: Mapped[int] = mapped_column(Integer, default=0)
    last_sync_remaining: Mapped[int] = mapped_column(Integer, default=0)
    last_sync_error: Mapped[str | None] = mapped_column(String(500), nullable=True)


class EmailIntakeMessage(Base):
    __tablename__ = 'email_intake_messages'
    __table_args__ = (UniqueConstraint('mailbox_key', 'uid_validity', 'uid', name='uq_email_intake_uid'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mailbox_key: Mapped[str] = mapped_column(String(200))
    uid_validity: Mapped[str] = mapped_column(String(40))
    uid: Mapped[int] = mapped_column(Integer)
    message_id: Mapped[str] = mapped_column(String(1000), default='')
    subject: Mapped[str] = mapped_column(String(1000), default='')
    sender: Mapped[str] = mapped_column(String(1000), default='')
    received: Mapped[str] = mapped_column(String(100), default='')
    body: Mapped[str] = mapped_column(Text, default='')
    status: Mapped[str] = mapped_column(String(30), default='pending')
    notice: Mapped[str] = mapped_column(Text, default='')
    version: Mapped[int] = mapped_column(Integer, default=1)


class EmailIntakeAttachment(Base):
    __tablename__ = 'email_intake_attachments'
    __table_args__ = (UniqueConstraint('message_id', 'part_number', name='uq_email_intake_part'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey('email_intake_messages.id'))
    part_number: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(240))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    duplicate_of: Mapped[int | None] = mapped_column(ForeignKey('email_intake_attachments.id'), nullable=True)


class EmailIntakeOrderLink(Base):
    __tablename__ = 'email_intake_order_links'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    import_key: Mapped[str] = mapped_column(String(64), unique=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    attachment_id: Mapped[int] = mapped_column(ForeignKey('email_intake_attachments.id'))
    order_id: Mapped[int | None] = mapped_column(ForeignKey('sales_orders.id', ondelete='SET NULL'), nullable=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))


class EmailPdfWorkingDraft(Base):
    __tablename__ = 'email_pdf_working_drafts'
    __table_args__ = (UniqueConstraint('attachment_id', 'actor_id', name='uq_email_pdf_actor_draft'),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attachment_id: Mapped[int] = mapped_column(ForeignKey('email_intake_attachments.id'))
    actor_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    content_json: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    saved_at: Mapped[str] = mapped_column(String(40))
