from io import BytesIO
import imaplib
from urllib.parse import quote
from fastapi import APIRouter, Depends, HTTPException, Request, Response, Query, UploadFile
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select, func, update
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from starlette.datastructures import Headers
from app.api.deps import get_db, PermissionChecker, has_unrestricted_customer_access
from app.models.user import User
from app.models.email_intake import EmailIntakeSettings, EmailIntakeMessage, EmailIntakeAttachment, EmailIntakeOrderLink
from app.services import email_intake as service

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


@router.get('/settings')
def settings(response: Response, db: Session = Depends(get_db), user: User = Depends(allowed)):
    response.headers['Cache-Control'] = 'private, no-store'
    row = db.get(EmailIntakeSettings, 1)
    return {'account': service.ACCOUNT, 'folder': '收件箱', 'configured': row is not None,
            'version': row.version if row else 0, 'mode': 'manual'}


@router.put('/settings')
def save_settings(payload: SettingsChange, request: Request, db: Session = Depends(get_db), user: User = Depends(allowed)):
    if request.url.scheme != 'https' and request.url.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise HTTPException(400, '请从HTTPS ERP地址保存邮箱授权码')
    value = payload.authorization_code.get_secret_value().strip()
    if not 8 <= len(value) <= 256 or any(c in value for c in '\r\n\x00'):
        raise HTTPException(422, '请填写有效的126客户端授权码')
    try:
        encrypted = service.protect(value)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    row = db.get(EmailIntakeSettings, 1)
    if row is None:
        if payload.expected_version != 0:
            raise HTTPException(409, '邮箱设置已变化，请刷新')
        db.add(EmailIntakeSettings(id=1, encrypted_secret=encrypted))
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
             db: Session = Depends(get_db), user: User = Depends(allowed)):
    response.headers['Cache-Control'] = 'private, no-store'
    query = select(EmailIntakeMessage)
    if state != 'all':
        query = query.where(EmailIntakeMessage.status == state)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(query.order_by(EmailIntakeMessage.id.desc()).offset((page-1)*25).limit(25))
    return {'total': total, 'page': page, 'items': [{key: getattr(row, key) for key in
        ('id', 'subject', 'sender', 'received', 'status', 'version', 'notice')} for row in rows]}


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
        links = db.execute(select(EmailIntakeOrderLink.order_id, Order.order_number, Order.customer_po)
            .join(EmailIntakeAttachment, EmailIntakeAttachment.id == EmailIntakeOrderLink.attachment_id)
            .outerjoin(Order, Order.id == EmailIntakeOrderLink.order_id)
            .where(EmailIntakeAttachment.sha256 == attachment.sha256)).all()
        results.append({**{key: getattr(attachment, key) for key in ('id', 'filename', 'sha256', 'duplicate_of')},
            'orders': [{'id': link.order_id, 'order_number': link.order_number, 'customer_po': link.customer_po} for link in links]})
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
async def preview(attachment_id: int, db: Session = Depends(get_db), user: User = Depends(allowed)):
    row = db.get(EmailIntakeAttachment, attachment_id)
    if not row:
        raise HTTPException(404, '附件不存在')
    if not row.filename.lower().endswith('.pdf'):
        raise HTTPException(422, 'Excel草稿映射正在接入；当前可下载原附件核对')
    from app.api.orders import preview_order_pdf
    result = await preview_order_pdf(UploadFile(filename=row.filename, file=BytesIO(row.content),
        headers=Headers({'content-type': 'application/pdf'})), db, user)
    return {**result, 'email_attachment_id': row.id}
