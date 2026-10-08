"""Durable quotation commands. The command, CAS, business data and audit commit together."""
from contextlib import contextmanager
from decimal import Decimal
import hashlib
import json

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from app.models.quotation import QuotationMutation, QuotationOrder
from app.services.audit_log import append_audit_event


def encoded(value):
    return json.dumps(jsonable_encoder(value, custom_encoder={Decimal: str}), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@contextmanager
def command(db, user, action, customer_id, target_id, payload):
    key = str(payload.idempotency_key or "").strip()
    if not 8 <= len(key) <= 120:
        raise HTTPException(422, "报价提交标识缺失或无效，请刷新页面后重试")
    fingerprint = hashlib.sha256(encoded({
        "action": action, "customer_id": customer_id, "target_id": target_id,
        "payload": payload.model_dump(mode="json", exclude={"idempotency_key"}),
    }).encode("utf-8")).hexdigest()
    query = select(QuotationMutation).where(
        QuotationMutation.actor_id == user.id, QuotationMutation.idempotency_key == key)

    def replay(existing):
        if existing.request_hash != fingerprint or existing.action != action or existing.customer_id != customer_id:
            raise HTTPException(409, "本次提交标识已用于其他报价内容，请先核对原提交结果")
        return json.loads(existing.response_json)

    try:
        existing = db.scalar(query)
        if existing is not None:
            yield None, replay(existing)
            return
        row = QuotationMutation(actor_id=user.id, customer_id=customer_id, action=action,
                                idempotency_key=key, request_hash=fingerprint, response_json="{}")
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            existing = db.scalar(query)
            if existing is None:
                raise HTTPException(409, "报价提交正在处理，请使用原提交重试")
            yield None, replay(existing)
            return
        yield row, None
        if row.response_json == "{}" or row.quotation_id is None:
            raise RuntimeError("quotation command has no completed business result")
        db.commit()
    except OperationalError as error:
        db.rollback()
        raise HTTPException(409, "报价正在由其他操作处理，请核对后使用原提交重试") from error
    except Exception:
        db.rollback()
        raise


def claim_version(db, quotation, expected_version, statuses):
    if expected_version is None:
        raise HTTPException(409, "报价版本缺失，请刷新并重新打开报价单")
    result = db.execute(update(QuotationOrder).where(
        QuotationOrder.id == quotation.id,
        QuotationOrder.version == expected_version,
        QuotationOrder.status.in_(statuses),
    ).values(version=QuotationOrder.version + 1).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise HTTPException(409, "报价内容或状态已变化，请重新打开核对；本次修改未保存")
    db.refresh(quotation)


def finish(db, mutation, quotation, user, response):
    mutation.quotation_id = quotation.id
    mutation.response_json = encoded(response)
    append_audit_event(
        db, actor=user, event_category="business", result="success", source="web",
        module_code="quotations", action_code="quotation." + mutation.action,
        resource="quotation", entity_type="quotation", entity_id=quotation.id,
        object_ref=quotation.quotation_no, customer_id=quotation.customer_id,
        details={"version": quotation.version, "status": quotation.status,
                 "request_hash": mutation.request_hash, "mutation_id": mutation.id},
    )
    db.flush()
    return json.loads(mutation.response_json)
