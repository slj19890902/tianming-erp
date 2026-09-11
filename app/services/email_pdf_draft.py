"""Persist only editable PDF fields, never client security/cost/inventory state."""
import hashlib
import json
from datetime import datetime, timezone
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from app.models.email_intake import EmailPdfWorkingDraft
from app.services.email_intake import audit

TOP = {'customer_po', 'order_date', 'delivery_date', 'matched_customer_id'}
ITEM = set('line_no client_line_id raw_product_code raw_product_name raw_spec_model product_code product_name specification spec quantity unit_price production_notes matched_product_id matched_material_id layer_count flute_type report_length_mm report_width_mm crease_type crease_left_mm crease_middle_mm crease_right_mm manual_product_selected is_new_product temp_drawing_token'.split())


def editable(value):
    if not isinstance(value, dict) or not isinstance(value.get('items'), list) or len(value['items']) > 500:
        raise HTTPException(422, '草稿需要明细列表，最多500行')
    def fields(row, names):
        if not isinstance(row, dict):
            raise HTTPException(422, '草稿明细格式无效')
        result = {key: val for key, val in row.items() if key in names}
        if any(val is not None and not isinstance(val, (str, bool, int, float)) for val in result.values()):
            raise HTTPException(422, '草稿字段格式无效')
        return result
    result = {**fields(value, TOP), 'items': [fields(row, ITEM) for row in value['items']]}
    try:
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except ValueError:
        raise HTTPException(422, '草稿数字格式无效') from None
    if len(encoded.encode('utf-8')) > 1024 * 1024:
        raise HTTPException(422, '草稿内容超过1MB，请减少明细')
    return encoded


def current(db, attachment_id, user):
    return db.scalar(select(EmailPdfWorkingDraft).where(
        EmailPdfWorkingDraft.attachment_id == attachment_id, EmailPdfWorkingDraft.actor_id == user.id))


def save(db, attachment_id, user, draft, expected_version):
    encoded = editable(draft)
    digest = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
    row = current(db, attachment_id, user)
    if row and row.content_hash == digest:
        return {'version': row.version, 'saved_at': row.saved_at}
    now = datetime.now(timezone.utc).isoformat()
    try:
        if row:
            changed = db.execute(update(EmailPdfWorkingDraft).where(
                EmailPdfWorkingDraft.id == row.id, EmailPdfWorkingDraft.version == expected_version
            ).values(content_json=encoded, content_hash=digest, version=expected_version+1, saved_at=now))
            if changed.rowcount != 1:
                raise HTTPException(409, '草稿已在另一个窗口修改，请重新打开后核对；本窗口修改尚未覆盖')
            version = expected_version + 1
        else:
            if expected_version != 0:
                raise HTTPException(409, '草稿版本已变化，请重新打开')
            version = 1
            db.add(EmailPdfWorkingDraft(attachment_id=attachment_id, actor_id=user.id,
                content_json=encoded, content_hash=digest, version=version, saved_at=now))
        audit(db, user, 'save_pdf_draft', {'attachment_id': attachment_id, 'version': version, 'content_hash': digest})
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '另一窗口已保存草稿，请重新打开后核对') from None
    except Exception:
        db.rollback()
        raise
    return {'version': version, 'saved_at': now}


def restore(db, fresh, edits, user):
    from app.api.orders import DraftRematchRequest, rematch_order_draft
    from app.models.product import Product
    customer_id = edits.get('matched_customer_id') or fresh.get('matched_customer_id')
    result = {**fresh, **{key: value for key, value in edits.items() if key in TOP}, 'items': edits['items']}
    if customer_id:
        result = rematch_order_draft(DraftRematchRequest(draft=result, customer_id=customer_id,
            preview_safety_token=fresh['preview_safety_token']), db, user)
    warnings = list(result.get('warnings') or [])
    for item, saved in zip(result['items'], edits['items']):
        item.update(saved)
        product_id = saved.get('matched_product_id')
        product = db.get(Product, product_id) if product_id else None
        if product and product.customer_id == customer_id and product.is_active and not product.deleted_at:
            item['match_status'] = 'matched'
        else:
            item['matched_product_id'] = None
            item['match_status'] = 'unmatched'
            item['manual_product_selected'] = False
            if product_id:
                warnings.append('已保存明细的常用箱已停用、删除或不属于当前客户，请重新选择；文字和数量已保留。')
        # Always refresh inventory and cost in the existing frontend workflow.
        item.pop('_inventory', None)
        item['cost_status'] = 'pending'
        item['estimated_cost'] = None
        item['quantity_confirmed'] = False
        item['price_conflict_confirmed'] = False
    result['warnings'] = warnings
    return result
