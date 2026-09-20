"""Background PDF recognition cache; never writes orders or inventory."""
import json
import threading
from datetime import date, datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from sqlalchemy import select
from sqlalchemy.orm import defer
from app.models.email_intake import (
    EmailIntakeSettings, EmailIntakeMessage, EmailIntakeAttachment,
    EmailIntakeOrderLink, EmailPdfRecognition, EmailPdfDisposition,
)
from app.services.email_sender_filter import normalize_senders, sender_matches

recognition_lock = threading.Lock()


ORDER_START = date(2026, 9, 1)
INBOX_SINCE = '01-Sep-2026'


def before_start(value, *, mail_date=False):
    if not value:
        return False
    try:
        if mail_date:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo:
                parsed = parsed.astimezone(timezone(timedelta(hours=8)))
            day = parsed.date()
        else:
            day = date.fromisoformat(str(value)[:10])
        return day < ORDER_START
    except (ValueError, TypeError, OverflowError):
        # Unknown dates stay visible for manual handling; never guess from filenames.
        return False


def queue_entries(db):
    """One read-only eligibility policy for badges, previews and background work."""
    settings = db.get(EmailIntakeSettings, 1)
    if settings is None or settings.sender_addresses_json is None:
        return []
    allowed = normalize_senders(json.loads(settings.sender_addresses_json))
    if not allowed:
        return []
    linked = set(db.scalars(select(EmailIntakeAttachment.sha256)
        .join(EmailIntakeOrderLink, EmailIntakeOrderLink.attachment_id == EmailIntakeAttachment.id)))
    from app.models.audit import OperationLog
    for details in db.scalars(select(OperationLog.details).where(
            OperationLog.module_code == 'orders', OperationLog.result == 'success',
            OperationLog.action_code.in_(('order.pdf_create', 'order.pdf_safety_override')))):
        try:
            digest = json.loads(details or '{}').get('source_hash')
        except (ValueError, AttributeError):
            continue
        if isinstance(digest, str) and len(digest) == 64:
            linked.add(digest)
    ignored = set(db.scalars(select(EmailIntakeAttachment.sha256)
        .join(EmailIntakeMessage, EmailIntakeMessage.id == EmailIntakeAttachment.message_id)
        .where(EmailIntakeMessage.status == 'ignored')))
    dispositions = {r.sha256: r for r in db.scalars(select(EmailPdfDisposition))}
    cached = {r.sha256: r for r in db.scalars(select(EmailPdfRecognition))}
    rows = db.execute(select(EmailIntakeAttachment, EmailIntakeMessage.sender, EmailIntakeMessage.received)
        .options(defer(EmailIntakeAttachment.content))
        .join(EmailIntakeMessage, EmailIntakeMessage.id == EmailIntakeAttachment.message_id)
        .where(EmailIntakeMessage.status == 'pending')
        .order_by(EmailIntakeAttachment.id))
    result, seen = [], set()
    for attachment, sender, received in rows:
        if (not attachment.filename.lower().endswith('.pdf') or attachment.sha256 in seen
                or not sender_matches([sender], allowed)):
            continue
        seen.add(attachment.sha256)
        record = cached.get(attachment.sha256)
        raw = json.loads(record.draft_json) if record and record.draft_json else {}
        state, po, order_number = 'pending', raw.get('customer_po'), None
        handled = dispositions.get(attachment.sha256)
        if handled:
            state, po, order_number = handled.action, handled.customer_po, handled.order_number
        elif attachment.sha256 in linked:
            state = 'processed'
        elif attachment.sha256 in ignored:
            state = 'deleted'
        elif before_start(received, mail_date=True) or before_start(raw.get('order_date')):
            state = 'before_start'
        result.append({'attachment': attachment, 'state': state, 'customer_po': po, 'order_number': order_number})
    return result


def pending_attachments(db):
    return [entry['attachment'] for entry in queue_entries(db) if entry['state'] == 'pending']


def record_disposition(db, attachment, action, user=None, *, customer_po=None, order_number=None):
    """Append once per content hash; audited in the same caller-owned transaction."""
    from sqlalchemy.dialects.sqlite import insert
    result = db.execute(insert(EmailPdfDisposition).values(
        sha256=attachment.sha256, attachment_id=attachment.id, action=action,
        actor_id=user.id if user else None, handled_at=datetime.now(),
        customer_po=customer_po, order_number=order_number,
    ).on_conflict_do_nothing(index_elements=['sha256']))
    if result.rowcount:
        from app.services.email_intake import audit
        audit(db, user, 'handle_pdf', {'attachment_id': attachment.id, 'sha256': attachment.sha256,
              'action': action, 'customer_po': customer_po, 'order_number': order_number})
    return db.get(EmailPdfDisposition, attachment.sha256)


def settle_queue(db):
    # Also retain prior ignored messages and deleted order links across new mail UIDs.
    for entry in queue_entries(db):
        if entry['state'] in ('processed', 'deleted', 'duplicate'):
            record_disposition(db, entry['attachment'], entry['state'],
                customer_po=entry['customer_po'], order_number=entry['order_number'])
    db.commit()


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
        settle_queue(db)
        cached = set(db.scalars(select(EmailPdfRecognition.sha256)))
        rows = [a for a in pending_attachments(db)
                if (a.id == retry_id if retry_id is not None else a.sha256 not in cached)][:limit]
        if retry_id is not None and not rows:
            raise ValueError('此PDF已处理、早于收单日期或不在发件人名单中，请刷新')
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


def run_recognition_cycle(session_factory):
    """Process already received PDFs without any mailbox connection."""
    with session_factory() as db:
        settings = db.get(EmailIntakeSettings, 1)
        if settings is None or not settings.automatic_enabled:
            return 0
        return recognize_pending(db)


async def recognition_loop(stop, session_factory, interval_seconds=5):
    import asyncio
    import logging
    while not stop.is_set():
        delay = interval_seconds
        try:
            await asyncio.to_thread(run_recognition_cycle, session_factory)
        except Exception:
            # Do not log exception bodies containing document data or paths.
            logging.getLogger(__name__).warning('邮箱PDF后台识别暂时失败，将自动重试')
            delay = max(60, interval_seconds)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
