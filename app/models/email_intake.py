"""Mail intake staging; no automatic sales facts."""
from sqlalchemy import Integer, String, Text, LargeBinary, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.models import Base


class EmailIntakeSettings(Base):
    __tablename__ = 'email_intake_settings'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    encrypted_secret: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)


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
