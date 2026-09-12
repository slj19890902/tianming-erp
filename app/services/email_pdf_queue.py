"""Background PDF recognition cache; never writes orders or inventory."""
import json
import threading
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.orm import defer
from app.models.email_intake import (
    EmailIntakeSettings, EmailIntakeMessage, EmailIntakeAttachment,
    EmailIntakeOrderLink, EmailPdfRecognition,
)
from app.models.order import Order
from app.services.email_sender_filter import normalize_senders, sender_matches

recognition_lock = threading.Lock()


def pending_attachments(db):
    settings = db.get(EmailIntakeSettings, 1)
    if settings is None or settings.sender_addresses_json is None:
        return []
    allowed = normalize_senders(json.loads(settings.sender_addresses_json))
    if not allowed:
        return []
    linked = set(db.scalars(select(EmailIntakeAttachment.sha256)
        .join(EmailIntakeOrderLink, EmailIntakeOrderLink.attachment_id == EmailIntakeAttachment.id)
        .join(Order, Order.id == EmailIntakeOrderLink.order_id)))
    rows = db.execute(select(EmailIntakeAttachment, EmailIntakeMessage.sender).options(defer(EmailIntakeAttachment.content))
        .join(EmailIntakeMessage, EmailIntakeMessage.id == EmailIntakeAttachment.message_id)
        .where(EmailIntakeMessage.status == 'pending')
        .order_by(EmailIntakeAttachment.id))
    result, seen = [], set()
    for attachment, sender in rows:
        if (not attachment.filename.lower().endswith('.pdf') or attachment.sha256 in linked
                or attachment.sha256 in seen or not sender_matches([sender], allowed)):
            continue
        seen.add(attachment.sha256)
        result.append(attachment)
    return result


def classify(draft):
    if draft.get('recognition_status') == 'failed' or not draft.get('items'):
        return 'improve'
    if (draft.get('integrity_check') or {}).get('integrity_status') != 'passed':
        return 'improve'
    if not draft.get('matched_customer_id'):
        return 'improve'
    for item in draft['items']:
        try:
            quantity = float(item.get('quantity') or 0)
        except (ValueError, TypeError):
            return 'improve'
        if (not item.get('matched_product_id') or quantity <= 0 or not quantity.is_integer()
                or item.get('unit_price') in (None, '')):
            return 'improve'
    return 'ready'


def recognize_pending(db, limit=5, retry_id=None):
    if not recognition_lock.acquire(blocking=False):
        return 0
    try:
        from app.api.orders import _parse_order_pdf_preview, _pdf_failure_draft
        from app.services.order_pdf_import import match_import_draft
        from app.api.orders import load_active_pdf_template_rules, PdfParseError
        cached = set(db.scalars(select(EmailPdfRecognition.sha256)))
        rows = [a for a in pending_attachments(db)
                if (a.id == retry_id if retry_id is not None else a.sha256 not in cached)][:limit]
        if retry_id is not None and not rows:
            raise ValueError('此PDF已处理、已忽略或不在发件人名单中，请刷新')
        rules = load_active_pdf_template_rules(db)
        for attachment in rows:
            error_code = None
            try:
                draft = _parse_order_pdf_preview(attachment.content, attachment.filename, rules)
                draft['file_hash'] = attachment.sha256
                state = classify(match_import_draft(db, draft))
            except PdfParseError as error:
                draft = _pdf_failure_draft(attachment.filename, error, digest=attachment.sha256)
                state, error_code = 'improve', 'pdf_parse_failed'
            except Exception:
                # Store no exception body: it may contain paths or document text.
                draft = None
                state, error_code = 'improve', 'pdf_recognition_failed'
            record = db.get(EmailPdfRecognition, attachment.sha256)
            if record is None:
                record = EmailPdfRecognition(sha256=attachment.sha256, attachment_id=attachment.id)
                db.add(record)
            record.status = state
            record.draft_json = json.dumps(draft, ensure_ascii=False, default=str) if draft else None
            record.error_code = error_code
            record.recognized_at = datetime.now()
            from app.services.email_intake import audit
            audit(db, None, 'recognize_pdf', {'attachment_id': attachment.id, 'sha256': attachment.sha256, 'status': state, 'error_code': error_code})
            db.commit()
        return len(rows)
    finally:
        recognition_lock.release()
