"""Attach source and duplicate protection inside the normal order transaction."""
import hashlib
import json
from fastapi import HTTPException
from sqlalchemy import select
from app.models.email_intake import EmailIntakeAttachment, EmailIntakeOrderLink
from app.services.email_intake import audit


def prepare(db, payload, user, claims):
    if payload.email_attachment_id is None:
        return None, None
    if user.role not in ('admin', 'boss'):
        raise HTTPException(403, '邮箱订单来源仅由管理员处理')
    attachment = db.get(EmailIntakeAttachment, payload.email_attachment_id)
    if attachment is None:
        raise HTTPException(404, '邮箱附件不存在')
    if not claims or claims.get('source_hash') != attachment.sha256:
        raise HTTPException(409, '订单识别结果与邮箱附件不一致，请从原附件重新进入')
    key = hashlib.sha256(json.dumps([attachment.sha256, payload.customer_id,
        (payload.customer_po or '').strip()], ensure_ascii=False).encode()).hexdigest()
    values = payload.model_dump(mode='json', exclude={'email_attachment_id', 'pdf_import_confirmation',
        'import_integrity_status', 'import_integrity_errors', 'mold_repair_confirmation_token'})
    for item in values.get('items') or []:
        item.pop('client_line_id', None)
    digest = hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    link = db.scalar(select(EmailIntakeOrderLink).where(EmailIntakeOrderLink.import_key == key))
    if link:
        if link.payload_hash != digest:
            raise HTTPException(409, '此邮件附件的该客户单号已导入，内容有变化，请查看原订单处理改单')
        if link.order_id is None:
            raise HTTPException(409, '此附件关联订单已删除，请先人工核对来源记录')
    if link is None:
        from app.models.email_intake import EmailPdfDisposition
        if db.get(EmailPdfDisposition, attachment.sha256):
            raise HTTPException(409, '此PDF已处理或删除，请重新读取待处理列表')
    return {'import_key': key, 'payload_hash': digest, 'attachment_id': attachment.id, 'actor_id': user.id}, link


def attach(db, context, order, user):
    if context is None:
        return
    db.add(EmailIntakeOrderLink(**context, order_id=order.id))
    db.flush()
    audit(db, user, 'order_link', {'attachment_id': context['attachment_id'], 'order_id': order.id})
