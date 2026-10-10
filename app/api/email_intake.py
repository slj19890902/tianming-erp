from io import BytesIO
import imaplib
from urllib.parse import quote
from fastapi import APIRouter, Depends, HTTPException, Request, Response, Query, UploadFile
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select, func, update
from sqlalchemy.orm import Session, aliased
from sqlalchemy.exc import IntegrityError
from starlette.datastructures import Headers
from app.api.deps import get_db, PermissionChecker, has_unrestricted_customer_access
from app.models.user import User
from app.models.order import Order
from app.models.email_intake import EmailIntakeSettings, EmailIntakeMessage, EmailIntakeAttachment, EmailIntakeOrderLink
from app.services import email_intake as service
from app.services import email_pdf_draft
import json

router = APIRouter()


def allowed(user: User = Depends(PermissionChecker('orders.create')), db: Session = Depends(get_db)):
    if user.role not in ('admin', 'boss') or not has_unrestricted_customer_access(user, db):
        raise HTTPException(403, '未分类收件箱仅由老板或管理员处理')
    return user


class SettingsChange(BaseModel):
    authorization_code: SecretStr
    expected_version: int = Field(ge=0)


class StateChange(BaseModel):
    expected_version: int = Field(ge=1)
    status: str = Field(pattern='^(pending|ignored)$')


class PdfDispositionChange(BaseModel):
    sha256: str = Field(pattern="^[a-f0-9]{64}$")
    action: str = Field(pattern="^(processed|deleted|duplicate)$")


class PdfDraftChange(BaseModel):
    expected_version: int = Field(ge=0)
    draft: dict


class AutomationChange(BaseModel):
    expected_version: int = Field(ge=1)
    automatic_enabled: bool
    sync_interval_minutes: int = Field(default=5, ge=1, le=60)


class SenderChange(BaseModel):
    expected_version: int = Field(ge=1)
    addresses: list[str] = Field(max_length=100)


def _settings_payload(row):
    return {
        'account': service.ACCOUNT,
        'folder': '收件箱',
        'order_start_date': '2026-09-01',
        'configured': row is not None,
        'sender_filter_configured': bool(row and row.sender_addresses_json is not None),
        'sender_addresses': json.loads(row.sender_addresses_json) if row and row.sender_addresses_json is not None else [],
        'version': row.version if row else 0,
        'mode': 'automatic' if row and row.automatic_enabled else 'manual',
        'automatic_enabled': bool(row and row.automatic_enabled),
        'sync_interval_minutes': int(row.sync_interval_minutes) if row else 5,
        'last_sync_started_at': row.last_sync_started_at if row else None,
        'last_sync_completed_at': row.last_sync_completed_at if row else None,
        'last_sync_status': row.last_sync_status if row else 'never',
        'last_sync_received': int(row.last_sync_received) if row else 0,
        'last_sync_remaining': int(row.last_sync_remaining) if row else 0,
        'last_sync_error': row.last_sync_error if row else None,
    }


@router.get('/settings')
def settings(response: Response, db: Session = Depends(get_db), user: User = Depends(allowed)):
    response.headers['Cache-Control'] = 'private, no-store'
    row = db.get(EmailIntakeSettings, 1)
    return _settings_payload(row)


@router.put('/settings')
def save_settings(payload: SettingsChange, request: Request, db: Session = Depends(get_db), user: User = Depends(allowed)):
    if request.url.scheme != 'https' and request.url.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise HTTPException(400, '请从HTTPS ERP地址保存邮箱授权码')
    value = payload.authorization_code.get_secret_value().strip()
    if not 8 <= len(value) <= 256 or any(c in value for c in '\r\n\x00'):
        raise HTTPException(422, '请填写有效的126客户端授权码')
    row = db.get(EmailIntakeSettings, 1)
    # Keychain storage has a side effect, unlike DPAPI encryption. Reject an
    # already-stale request before allocating a new credential. The atomic
    # UPDATE/unique INSERT gates below still handle concurrent changes.
    if payload.expected_version != (row.version if row else 0):
        raise HTTPException(409, '邮箱设置已变化，请刷新')
    try:
        encrypted = service.protect(value)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    if row is None:
        if payload.expected_version != 0:
            raise HTTPException(409, '邮箱设置已变化，请刷新')
        db.add(EmailIntakeSettings(id=1, encrypted_secret=encrypted, automatic_enabled=True,
                                   sync_interval_minutes=5, last_sync_status='never'))
    else:
        changed = db.execute(update(EmailIntakeSettings).where(
            EmailIntakeSettings.id == 1, EmailIntakeSettings.version == payload.expected_version
        ).values(encrypted_secret=encrypted, version=EmailIntakeSettings.version + 1))
        if changed.rowcount != 1:
            raise HTTPException(409, '邮箱设置已变化，请刷新')
    try:
        service.audit(db, user, 'configure', {'account': service.ACCOUNT, 'credential_changed': True})
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '邮箱设置已变化，请刷新') from None
    return {'saved': True}


@router.put('/automation')
def save_automation(payload: AutomationChange, db: Session = Depends(get_db), user: User = Depends(allowed)):
    row = db.get(EmailIntakeSettings, 1)
    if row is None:
        raise HTTPException(409, '请先保存126客户端授权码')
    changed = db.execute(update(EmailIntakeSettings).where(
        EmailIntakeSettings.id == 1,
        EmailIntakeSettings.version == payload.expected_version,
    ).values(
        automatic_enabled=payload.automatic_enabled,
        sync_interval_minutes=payload.sync_interval_minutes,
        version=EmailIntakeSettings.version + 1,
    ))
    if changed.rowcount != 1:
        raise HTTPException(409, '邮箱设置已变化，请刷新')
    service.audit(db, user, 'configure_automatic_sync', {
        'automatic_enabled': payload.automatic_enabled,
        'sync_interval_minutes': payload.sync_interval_minutes,
    })
    db.commit()
    return {'saved': True}


@router.put('/senders')
def save_senders(payload: SenderChange, db: Session = Depends(get_db), user: User = Depends(allowed)):
    from app.services.email_sender_filter import normalize_senders
    try:
        addresses = normalize_senders(payload.addresses)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    if not service.sync_lock.acquire(blocking=False):
        raise HTTPException(409, '正在收件，请稍后保存发件人筛选')
    try:
        changed = db.execute(update(EmailIntakeSettings).where(
            EmailIntakeSettings.id == 1, EmailIntakeSettings.version == payload.expected_version,
        ).values(sender_addresses_json=json.dumps(addresses), version=EmailIntakeSettings.version + 1))
        if changed.rowcount != 1:
            raise HTTPException(409, '邮箱设置已变化或尚未配置，请刷新')
        service.audit(db, user, 'configure_senders', {'addresses': addresses})
        db.commit()
        return {'saved': True}
    finally:
        service.sync_lock.release()


@router.post('/sync')
def sync(db: Session = Depends(get_db), user: User = Depends(allowed)):
    try:
        return service.sync_inbox(db, user)
    except LookupError as error:
        raise HTTPException(409, str(error)) from None
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    except (OSError, imaplib.IMAP4.error):
        raise HTTPException(502, '126邮箱连接失败，请检查IMAP服务、授权码和网络；已读取邮件保留，可重试') from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '另一请求已读取本批邮件，请刷新后继续') from None


@router.get('')
def messages(response: Response, page: int = Query(1, ge=1), state: str = Query('all', pattern='^(all|pending|ignored|oversize)$'),
             association: str = Query('all', pattern='^(all|linked|unlinked)$'),
             db: Session = Depends(get_db), user: User = Depends(allowed)):
    response.headers['Cache-Control'] = 'private, no-store'
    source = aliased(EmailIntakeAttachment)
    linked = (select(EmailIntakeAttachment.message_id.label('mail_id'),
                     func.count(func.distinct(Order.id)).label('order_count'))
        .join(source, source.sha256 == EmailIntakeAttachment.sha256)
        .join(EmailIntakeOrderLink, EmailIntakeOrderLink.attachment_id == source.id)
        .join(Order, Order.id == EmailIntakeOrderLink.order_id)
        .group_by(EmailIntakeAttachment.message_id).subquery())
    count = func.coalesce(linked.c.order_count, 0)
    query = select(EmailIntakeMessage, count).outerjoin(linked, linked.c.mail_id == EmailIntakeMessage.id)
    if state != 'all':
        query = query.where(EmailIntakeMessage.status == state)
    if association != 'all':
        query = query.where(count > 0 if association == 'linked' else count == 0)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.execute(query.order_by(EmailIntakeMessage.id.desc()).offset((page-1)*25).limit(25))
    return {'total': total, 'page': page, 'items': [{**{key: getattr(row, key) for key in
        ('id', 'subject', 'sender', 'received', 'status', 'version', 'notice')},
        'linked_order_count': order_count} for row, order_count in rows]}


@router.get('/orders/{order_id}/source')
def order_source(order_id: int, response: Response, db: Session = Depends(get_db), user: User = Depends(allowed)):
    response.headers['Cache-Control'] = 'private, no-store'
    if not db.get(Order, order_id):
        raise HTTPException(404, '订单不存在')
    rows = db.execute(select(EmailIntakeAttachment.id, EmailIntakeAttachment.message_id,
                             EmailIntakeAttachment.filename)
        .join(EmailIntakeOrderLink, EmailIntakeOrderLink.attachment_id == EmailIntakeAttachment.id)
        .where(EmailIntakeOrderLink.order_id == order_id).order_by(EmailIntakeAttachment.id)).all()
    return {'sources': [dict(row._mapping) for row in rows]}


@router.get('/queue/summary')
def queue_summary(response: Response, db: Session = Depends(get_db), user: User = Depends(allowed)):
    from app.services.email_pdf_queue import pending_attachments
    from app.models.email_intake import EmailPdfRecognition
    response.headers['Cache-Control'] = 'private, no-store'
    statuses = dict(db.execute(select(EmailPdfRecognition.sha256, EmailPdfRecognition.status)).all())
    counts = {'ready': 0, 'improve': 0, 'recognizing': 0}
    for attachment in pending_attachments(db):
        counts[statuses.get(attachment.sha256, 'recognizing')] += 1
    return {'pending': sum(counts.values()), **counts}


def _cached_queue_draft(db, attachment, cached, user):
    from app.api.orders import _match_pdf_preview_for_user, _finalize_pdf_preview_for_user
    if cached.draft_json:
        raw = json.loads(cached.draft_json)
        raw['source_name'] = attachment.filename
        raw['file_hash'] = attachment.sha256
        draft = _finalize_pdf_preview_for_user(_match_pdf_preview_for_user(db, raw, user), user)
    else:
        draft = {'source_name': attachment.filename, 'file_hash': attachment.sha256,
                 'recognition_status': 'failed', 'items': [],
                 'warnings': ['此PDF未能完成识别，请重新识别或提交改进。']}
        draft = _finalize_pdf_preview_for_user(_match_pdf_preview_for_user(db, draft, user), user)
    working = email_pdf_draft.current(db, attachment.id, user)
    if working and draft.get('preview_safety_token'):
        draft = email_pdf_draft.restore(db, draft, json.loads(working.content_json), user)
    draft.update(email_attachment_id=attachment.id,
                 email_draft_version=working.version if working else 0,
                 email_draft_saved_at=working.saved_at if working else None,
                 email_draft_restored=bool(working and draft.get('preview_safety_token')))
    return draft


@router.post('/queue/preview')
async def queue_preview(response: Response, db: Session = Depends(get_db), user: User = Depends(allowed)):
    from app.services.email_pdf_queue import queue_entries, classify
    from app.models.email_intake import EmailPdfRecognition
    from app.api.orders import _match_pdf_preview_for_user, _finalize_pdf_preview_for_user
    response.headers['Cache-Control'] = 'private, no-store'
    drafts = []
    per_state = {"ready": 0, "improve": 0}
    entries = queue_entries(db)
    remaining = [e['attachment'] for e in entries if e['state'] == 'pending']
    excluded = [{'filename': e['attachment'].filename, 'reason': e['state'],
                 'customer_po': e['customer_po'], 'order_number': e['order_number']}
                for e in entries if e['state'] != 'pending']
    for attachment in remaining:
        cached = db.get(EmailPdfRecognition, attachment.sha256)
        if cached is None:
            continue
        draft = _cached_queue_draft(db, attachment, cached, user)
        draft['email_queue_status'] = classify(draft)
        state = draft['email_queue_status']
        if per_state[state] < 20:
            per_state[state] += 1
            drafts.append(draft)
    return {'drafts': drafts, 'pending': len(remaining), 'excluded': excluded, 'order_start_date': '2026-09-01'}


@router.post('/queue/{attachment_id}/retry')
def retry_queue_pdf(attachment_id: int, db: Session = Depends(get_db), user: User = Depends(allowed)):
    from app.services.email_pdf_queue import recognize_pending
    try:
        count = recognize_pending(db, limit=1, retry_id=attachment_id)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    if not count:
        raise HTTPException(409, '后台正在识别，请稍后重试')
    from app.models.email_intake import EmailPdfRecognition
    from app.services.email_pdf_queue import classify
    attachment = db.get(EmailIntakeAttachment, attachment_id)
    cached = db.get(EmailPdfRecognition, attachment.sha256)
    draft = _cached_queue_draft(db, attachment, cached, user)
    draft['email_queue_status'] = classify(draft)
    return draft


@router.post('/queue/{attachment_id}/disposition')
def handle_queue_pdf(attachment_id: int, payload: PdfDispositionChange,
                     db: Session = Depends(get_db), user: User = Depends(allowed)):
    from app.models.email_intake import EmailPdfDisposition
    from app.services.email_pdf_queue import record_disposition
    attachment = db.get(EmailIntakeAttachment, attachment_id)
    if not attachment:
        raise HTTPException(404, '附件不存在')
    if not attachment.filename.lower().endswith('.pdf'):
        raise HTTPException(422, '此入口只处理PDF附件')
    if attachment.sha256 != payload.sha256:
        raise HTTPException(409, '附件已变化，请刷新')
    record = db.get(EmailPdfDisposition, attachment.sha256)
    if record is None:
        record = record_disposition(db, attachment, payload.action, user)
    if record.action != payload.action:
        db.rollback()
        raise HTTPException(409, '此附件已处理，请重新读取列表')
    db.commit()
    return {'saved': True, 'action': record.action}


@router.get('/{message_id}')
def detail(message_id: int, response: Response, db: Session = Depends(get_db), user: User = Depends(allowed)):
    response.headers['Cache-Control'] = 'private, no-store'
    row = db.get(EmailIntakeMessage, message_id)
    if not row:
        raise HTTPException(404, '邮件不存在')
    attachments = db.scalars(select(EmailIntakeAttachment).where(EmailIntakeAttachment.message_id == row.id))
    from app.models.order import Order
    results = []
    for attachment in attachments:
        working = email_pdf_draft.current(db, attachment.id, user)
        links = db.execute(select(EmailIntakeOrderLink.order_id, Order.order_number, Order.customer_po)
            .join(EmailIntakeAttachment, EmailIntakeAttachment.id == EmailIntakeOrderLink.attachment_id)
            .outerjoin(Order, Order.id == EmailIntakeOrderLink.order_id)
            .where(EmailIntakeAttachment.sha256 == attachment.sha256)).all()
        results.append({**{key: getattr(attachment, key) for key in ('id', 'filename', 'sha256', 'duplicate_of')},
            'orders': [{'id': link.order_id, 'order_number': link.order_number, 'customer_po': link.customer_po} for link in links],
            'working_draft': {'version': working.version, 'saved_at': working.saved_at} if working else None})
    return {**{key: getattr(row, key) for key in ('id', 'subject', 'sender', 'received', 'body', 'notice', 'status', 'version')},
            'attachments': results}


@router.put('/{message_id}/state')
def change_state(message_id: int, payload: StateChange, db: Session = Depends(get_db), user: User = Depends(allowed)):
    result = db.execute(update(EmailIntakeMessage).where(EmailIntakeMessage.id == message_id,
        EmailIntakeMessage.version == payload.expected_version).values(status=payload.status, version=EmailIntakeMessage.version+1))
    if result.rowcount != 1:
        raise HTTPException(409, '邮件状态已变化，请刷新')
    service.audit(db, user, 'classify', {'message_id': message_id, 'status': payload.status})
    db.commit()
    return {'saved': True}


@router.get('/attachments/{attachment_id}/download')
def download(attachment_id: int, db: Session = Depends(get_db), user: User = Depends(allowed)):
    row = db.get(EmailIntakeAttachment, attachment_id)
    if not row:
        raise HTTPException(404, '附件不存在')
    return Response(row.content, media_type='application/octet-stream', headers={
        'Content-Disposition': "attachment; filename*=UTF-8''" + quote(row.filename, safe=''),
        'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'})


@router.post('/attachments/{attachment_id}/preview')
async def preview(attachment_id: int, db: Session = Depends(get_db), user: User = Depends(allowed), resume: bool = False):
    row = db.get(EmailIntakeAttachment, attachment_id)
    if not row:
        raise HTTPException(404, '附件不存在')
    if not row.filename.lower().endswith('.pdf'):
        raise HTTPException(422, '订单识别仅支持PDF；其他附件可下载后人工核对')
    from app.api.orders import preview_order_pdf
    result = await preview_order_pdf(UploadFile(filename=row.filename, file=BytesIO(row.content),
        headers=Headers({'content-type': 'application/pdf'})), db, user)
    working = email_pdf_draft.current(db, row.id, user)
    if resume and working:
        result = email_pdf_draft.restore(db, result, json.loads(working.content_json), user)
    return {**result, 'email_attachment_id': row.id, 'email_draft_version': working.version if working else 0,
            'email_draft_saved_at': working.saved_at if working else None, 'email_draft_restored': bool(resume and working)}


@router.put('/attachments/{attachment_id}/working-draft')
def save_pdf_draft(attachment_id: int, payload: PdfDraftChange,
                   db: Session = Depends(get_db), user: User = Depends(allowed)):
    row = db.get(EmailIntakeAttachment, attachment_id)
    if not row:
        raise HTTPException(404, '附件不存在')
    if not row.filename.lower().endswith('.pdf'):
        raise HTTPException(422, '仅支持PDF核对草稿')
    return email_pdf_draft.save(db, attachment_id, user, payload.draft, payload.expected_version)
