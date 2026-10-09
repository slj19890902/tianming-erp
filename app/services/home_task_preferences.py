"""Durable, scoped attention controls; never mutate a business document."""
import hashlib
import json
from datetime import date, timedelta
from fastapi import HTTPException
from sqlalchemy import select, update
from app.core.time_contract import utc_now_naive
from app.models.home_task import HomeTaskPreference, HomeTaskMutation
from app.models.audit import OperationLog

REASONS = {
    "customer_delay": ("waiting_customer", "客户暂缓", "保留订单及库存预留，到期联系客户"),
    "excess_stock": ("stock_review", "超量备货", "留库待消化，补库策略需另行调整"),
    "customer_cancel": ("cancel_review", "取消待确认", "需正式确认及处理已有实物，不自动撤销单据"),
    "product_stopped": ("stock_review", "停用待确认", "保留现货，停用与停止补库需另行确认"),
    "dependency": ("condition_wait", "等待条件", "来料、模具或审批条件变化后重新核对"),
    "verify": ("verify", "数据待核", "核对数量、位置或资料后处理"),
    "later": ("snoozed", "稍后处理", "仅调整提醒时间"),
}
STATES = {v[0] for v in REASONS.values()} | {"active", "hidden"}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def task_hash(task):
    # Do not hash computed "overdue N days", labels or display position: those
    # change every day without any new business event.
    return digest({key: task.get(key) for key in (
        "id", "key", "customer_id", "customer_ids", "product_code", "order_id",
        "due_date", "message", "source_basis", "amount", "request_id",
    )})


def _scope_ok(encoded_ids, visible):
    ids = set(json.loads(encoded_ids))
    return bool(ids) and (visible is None or ids.issubset(visible))


def annotate_tasks(db, *, user, workbench, today):
    tasks = workbench["tasks"]
    keys = [t["id"] for t in tasks]
    personal = f"user:{user.id}"
    records = list(db.scalars(select(HomeTaskPreference).where(
        HomeTaskPreference.scope_key.in_([personal, "team"]),
        HomeTaskPreference.task_key.in_(keys)))) if keys else []
    by_key = {(r.scope_key, r.task_key): r for r in records}
    for task in tasks:
        basis = task_hash(task)
        task.update(source_hash=basis, attention_state="active", attention_reason="",
                    attention_label="", attention_effect="", remind_on=None,
                    attention_scope="personal", attention_versions={"personal": 0, "team": 0})
        candidates = []
        for scope, key in (("team", "team"), ("personal", personal)):
            record = by_key.get((key, task["id"]))
            if record is None:
                continue
            task["attention_versions"][scope] = record.version
            if record.state == "active":
                continue
            if record.source_hash != basis:
                task["resurface_reason"] = "业务已变化，重新提醒"
            elif record.remind_on and record.remind_on <= today:
                task["resurface_reason"] = "已到提醒日期"
            else:
                candidates.append((scope, record))
        if candidates:
            scope, record = candidates[-1]  # personal preferences never alter others
            reason = REASONS.get(record.reason, (record.state, "仅对我隐藏", "可随时恢复"))
            task.update(attention_state=record.state, attention_reason=record.reason,
                        attention_label="仅对我隐藏" if record.state == "hidden" else reason[1],
                        attention_effect=reason[2], remind_on=record.remind_on.isoformat() if record.remind_on else None,
                        attention_scope=scope)
    workbench["can_manage_team_reminders"] = user.role in {"admin", "boss"}
    workbench["attention_reasons"] = [dict(code=k, state=v[0], label=v[1], effect=v[2]) for k, v in REASONS.items()]


def replay_mutation(db, *, user, payload, visible_customer_ids):
    replay = db.scalar(select(HomeTaskMutation).where(
        HomeTaskMutation.actor_id == user.id,
        HomeTaskMutation.idempotency_key == payload["idempotency_key"]))
    if replay is None:
        return None
    if not _scope_ok(replay.customer_ids_json, visible_customer_ids):
        raise HTTPException(403, "无权访问该提醒的客户范围")
    if replay.request_hash != digest(payload):
        raise HTTPException(409, "此操作编号已用于其他修改，请刷新后重试")
    return json.loads(replay.response_json)


def save_preference(db, *, user, payload, task, today):
    scope = payload["scope"]
    state = payload["state"]
    reason = payload["reason"]
    until = payload["remind_on"]
    if scope == "team" and user.role not in {"admin", "boss"}:
        raise HTTPException(403, "仅管理员或老板可调整团队提醒")
    if state not in STATES or (state == "hidden" and scope != "personal"):
        raise HTTPException(422, "提醒状态无效；隐藏仅对本人有效")
    if state != "active":
        if reason not in REASONS:
            raise HTTPException(422, "请选择处理原因")
        if state != "hidden" and REASONS[reason][0] != state:
            raise HTTPException(422, "处理分类与原因不一致")
        if state != "hidden" and (not until or not today < until <= today + timedelta(days=365)):
            raise HTTPException(422, "请选择未来一年内的提醒日期")
    elif until or reason:
        raise HTTPException(422, "恢复提醒不应携带原因或延后日期")
    if state == "hidden" and until:
        raise HTTPException(422, "隐藏提醒不应携带延后日期")
    if payload["source_hash"] != task_hash(task):
        raise HTTPException(409, "业务内容已变化，请刷新后重新选择")
    scope_key = "team" if scope == "team" else f"user:{user.id}"
    record = db.scalar(select(HomeTaskPreference).where(
        HomeTaskPreference.scope_key == scope_key, HomeTaskPreference.task_key == task["id"]))
    version = record.version if record else 0
    if payload["expected_version"] != version:
        raise HTTPException(409, "提醒已被更新，请刷新后重试")
    ids = sorted(set(task.get("customer_ids") or [task["customer_id"]]))
    values = dict(customer_ids_json=encoded(ids), source_hash=task_hash(task),
                  state=state, reason=reason, remind_on=until, actor_id=user.id,
                  updated_at=utc_now_naive(), version=version + 1)
    if record:
        result = db.execute(update(HomeTaskPreference).where(
            HomeTaskPreference.id == record.id, HomeTaskPreference.version == version).values(**values),
            execution_options={"synchronize_session": False})
        if result.rowcount != 1:
            raise HTTPException(409, "提醒已被更新，请刷新后重试")
        record_id = record.id
    else:
        record = HomeTaskPreference(scope_key=scope_key, task_key=task["id"], **values)
        db.add(record); db.flush(); record_id = record.id
    response = dict(task_id=task["id"], version=version + 1, state=state, scope=scope)
    db.add(HomeTaskMutation(actor_id=user.id, preference_id=record_id,
        idempotency_key=payload["idempotency_key"], request_hash=digest(payload),
        customer_ids_json=encoded(ids), response_json=encoded(response), created_at=utc_now_naive()))
    db.add(OperationLog(user_id=user.id, username=user.username, role=user.role,
        action="HOME_TASK_PREFERENCE", resource="HomeTaskPreference", entity_id=record_id,
        description="调整首页提醒", details=encoded({"task_id":task["id"], "scope":scope,
            "before_version":version, "state":state, "reason":reason, "remind_on":until}),
        source="web", module_code="dashboard", event_category="business", result="success"))
    db.flush()
    return response
