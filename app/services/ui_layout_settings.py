from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models.ui_layout_revision import UiLayoutRevision


CATALOG_VERSION = "q2-02c1-v1"
LAYOUT_ROLES = ("admin", "finance", "sales", "workshop", "boss")
DISPLAY_MODES = ("standard", "large", "mobile")
LAYOUT_SECTIONS = ("menus", "dashboard_cards", "quick_actions")
ALL_PERMISSION_COMPONENT_IDS = frozenset({"mobile_production"})

MENU_CATALOG = (
    ("dashboard", "首页", ("dashboard.view",)),
    ("workbench", "订单与仓库", ("orders.view", "requisition.view", "incoming.view", "warehouse.view")),
    ("production", "生产确认", ("orders.view",)),
    ("deliveries", "送货与回单", ("deliveries.view",)),
    ("finance", "对账开票收款", ("finance.view",)),
    ("master", "主数据", ("customers.view", "products.view")),
    ("system_hub", "系统管理", ("audit.view", "system.backup", "users.manage")),
)
MOBILE_MENU_CATALOG = (
    ("mobile_home", "首页", ("dashboard.view",)),
    ("mobile_search", "查产品", ("warehouse.view",)),
    ("mobile_production", "近期生产", ("orders.view", "incoming.view")),
)
ROLE_MENU_IDS = {
    "admin": ("dashboard", "workbench", "production", "deliveries", "finance", "master", "system_hub"),
    "finance": ("dashboard", "workbench", "deliveries", "finance", "master"),
    "sales": ("dashboard", "workbench", "master"),
    "workshop": ("dashboard", "workbench", "production", "deliveries"),
    "boss": ("dashboard", "workbench", "production", "deliveries", "finance", "master"),
}
DASHBOARD_CARD_CATALOG = (
    ("pending_material", "待报料", ("requisition.view",)),
    ("pending_incoming", "待入库", ("incoming.view",)),
    ("pending_production", "待生产", ("orders.view",)),
    ("pending_delivery", "待送货", ("deliveries.view",)),
    ("pending_receipt", "待回单", ("deliveries.view",)),
    ("pending_reconciliation", "待对账", ("finance.view",)),
    ("pending_invoice", "待开票", ("finance.view",)),
    ("pending_payment", "待结款", ("finance.view",)),
    ("warehouse_capacity", "仓储容量", ("warehouse.view",)),
    ("inventory_risk", "库存风险", ("warehouse.view",)),
    ("business_anomaly", "异常", ("dashboard.view",)),
)
QUICK_ACTION_CATALOG = (
    ("order_create", "新建订单", ("orders.create",)),
    ("orders", "订单管理", ("orders.view",)),
    ("deliveries", "送货管理", ("deliveries.view",)),
    ("finance", "月结对账", ("finance.execute",)),
    ("incoming", "仓库来料入库", ("incoming.view",)),
    ("warehouse", "仓库库存管理", ("warehouse.view",)),
    ("stocktake", "手机库存盘点", ("warehouse.stocktake.view",)),
    ("mobile_scan", "手机扫码入口", ("incoming.execute",)),
)


class UiLayoutError(ValueError):
    pass


class UiLayoutConflict(UiLayoutError):
    pass


def validate_profile(role_code: str, display_mode: str) -> tuple[str, str]:
    if role_code not in LAYOUT_ROLES:
        raise UiLayoutError("不支持的角色")
    if display_mode not in DISPLAY_MODES:
        raise UiLayoutError("不支持的显示模式")
    return role_code, display_mode


def _section_catalog(
    section: str,
    role_code: str,
    display_mode: str,
) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    if display_mode == "mobile":
        if section == "menus":
            return MOBILE_MENU_CATALOG
        if section in {"dashboard_cards", "quick_actions"}:
            return ()
    if section == "menus":
        allowed = set(ROLE_MENU_IDS[role_code])
        return tuple(item for item in MENU_CATALOG if item[0] in allowed)
    if section == "dashboard_cards":
        return DASHBOARD_CARD_CATALOG
    if section == "quick_actions":
        return QUICK_ACTION_CATALOG
    raise UiLayoutError("未知界面配置分区")


def catalog_for(role_code: str, display_mode: str) -> dict[str, Any]:
    validate_profile(role_code, display_mode)
    result: dict[str, Any] = {
        "catalog_version": CATALOG_VERSION,
        "role_code": role_code,
        "display_mode": display_mode,
    }
    for section in LAYOUT_SECTIONS:
        result[section] = [
            {
                "id": component_id,
                "label": label,
                "required_permissions": list(required_permissions),
                "protected": (
                    section == "menus"
                    and component_id
                    == ("mobile_home" if display_mode == "mobile" else "dashboard")
                ),
            }
            for component_id, label, required_permissions in _section_catalog(
                section, role_code, display_mode
            )
        ]
    return result


def default_layout(role_code: str, display_mode: str) -> dict[str, Any]:
    catalog = catalog_for(role_code, display_mode)
    return {
        "catalog_version": CATALOG_VERSION,
        **{
            section: [{"id": item["id"], "visible": True} for item in catalog[section]]
            for section in LAYOUT_SECTIONS
        },
    }


def normalize_layout(
    role_code: str,
    display_mode: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    validate_profile(role_code, display_mode)
    if not isinstance(payload, dict):
        raise UiLayoutError("界面布局必须是对象")
    if payload.get("catalog_version") != CATALOG_VERSION:
        raise UiLayoutError("界面组件目录版本已变化，请重新载入默认布局")
    result: dict[str, Any] = {"catalog_version": CATALOG_VERSION}
    for section in LAYOUT_SECTIONS:
        raw_items = payload.get(section)
        if not isinstance(raw_items, list):
            raise UiLayoutError(f"{section} 必须是列表")
        expected = [
            item[0] for item in _section_catalog(section, role_code, display_mode)
        ]
        expected_set = set(expected)
        seen: set[str] = set()
        normalized: list[dict[str, Any]] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise UiLayoutError(f"{section} 中存在无效组件")
            component_id = raw.get("id")
            visible = raw.get("visible")
            if not isinstance(component_id, str) or component_id not in expected_set:
                raise UiLayoutError(f"{section} 中存在未登记组件")
            if component_id in seen:
                raise UiLayoutError(f"{section} 中存在重复组件 {component_id}")
            if not isinstance(visible, bool):
                raise UiLayoutError(f"{component_id} 的显示状态无效")
            seen.add(component_id)
            normalized.append({"id": component_id, "visible": visible})
        missing = [component_id for component_id in expected if component_id not in seen]
        if missing:
            raise UiLayoutError(f"{section} 缺少已登记组件：{','.join(missing)}")
        result[section] = normalized
    protected_id = "mobile_home" if display_mode == "mobile" else "dashboard"
    protected = next(item for item in result["menus"] if item["id"] == protected_id)
    if not protected["visible"]:
        raise UiLayoutError("首页是受保护入口，不能隐藏")
    return result


def _render(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_render(payload).encode("utf-8")).hexdigest()


def _latest(db: Session, role_code: str, display_mode: str, stream: str) -> UiLayoutRevision | None:
    return db.scalar(
        select(UiLayoutRevision)
        .where(
            UiLayoutRevision.role_code == role_code,
            UiLayoutRevision.display_mode == display_mode,
            UiLayoutRevision.stream == stream,
        )
        .order_by(desc(UiLayoutRevision.version))
        .limit(1)
    )


def _release_history(db: Session, role_code: str, display_mode: str, limit: int = 10) -> list[UiLayoutRevision]:
    return list(
        db.scalars(
            select(UiLayoutRevision)
            .where(
                UiLayoutRevision.role_code == role_code,
                UiLayoutRevision.display_mode == display_mode,
                UiLayoutRevision.stream == "release",
            )
            .order_by(desc(UiLayoutRevision.version))
            .limit(limit)
        ).all()
    )


def _row_layout(row: UiLayoutRevision) -> dict[str, Any]:
    if row.catalog_version != CATALOG_VERSION:
        raise UiLayoutError("保存的界面组件目录版本已过期")
    try:
        payload = json.loads(row.payload_json)
    except json.JSONDecodeError as error:
        raise UiLayoutError("保存的界面布局已损坏") from error
    normalized = normalize_layout(row.role_code, row.display_mode, payload)
    if _digest(normalized) != row.payload_hash:
        raise UiLayoutError("保存的界面布局校验失败")
    return normalized


def _safe_row_layout(
    row: UiLayoutRevision | None,
    role_code: str,
    display_mode: str,
) -> tuple[dict[str, Any], str | None]:
    if row is None:
        return default_layout(role_code, display_mode), None
    try:
        return _row_layout(row), None
    except UiLayoutError as error:
        return default_layout(role_code, display_mode), str(error)


def _version(row: UiLayoutRevision | None) -> int:
    return int(row.version) if row is not None else 0


def admin_state(db: Session, role_code: str, display_mode: str) -> dict[str, Any]:
    validate_profile(role_code, display_mode)
    release = _latest(db, role_code, display_mode, "release")
    draft = _latest(db, role_code, display_mode, "draft")
    release_layout, release_fallback = _safe_row_layout(release, role_code, display_mode)
    draft_layout, draft_fallback = _safe_row_layout(draft, role_code, display_mode)
    if draft is None:
        draft_layout = release_layout
    history = _release_history(db, role_code, display_mode)
    return {
        "role_code": role_code,
        "display_mode": display_mode,
        "catalog": catalog_for(role_code, display_mode),
        "draft": {
            "version": _version(draft),
            "base_release_version": int(draft.base_release_version) if draft else _version(release),
            "layout": draft_layout,
            "fallback_reason": draft_fallback,
        },
        "published": {
            "version": _version(release),
            "layout": release_layout,
            "fallback_reason": release_fallback,
        },
        "history": [
            {
                "version": int(row.version),
                "operation_kind": row.operation_kind,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in history
        ],
        "can_rollback": len(history) >= 2,
    }


def _has_required(
    permissions: set[str],
    required: Iterable[str],
    *,
    require_all: bool = False,
) -> bool:
    required_set = set(required)
    if not required_set:
        return True
    if require_all:
        return required_set.issubset(permissions)
    return bool(required_set & permissions)


def effective_layout(
    db: Session,
    *,
    role_code: str,
    display_mode: str,
    permissions: Iterable[str],
) -> dict[str, Any]:
    validate_profile(role_code, display_mode)
    release = _latest(db, role_code, display_mode, "release")
    layout, fallback_reason = _safe_row_layout(release, role_code, display_mode)
    allowed_permissions = set(permissions)
    filtered: dict[str, Any] = {"catalog_version": CATALOG_VERSION}
    for section in LAYOUT_SECTIONS:
        metadata = {
            component_id: required
            for component_id, _label, required in _section_catalog(
                section, role_code, display_mode
            )
        }
        filtered[section] = [
            item
            for item in layout[section]
            if item["visible"]
            and item["id"] in metadata
            and _has_required(
                allowed_permissions,
                metadata[item["id"]],
                require_all=item["id"] in ALL_PERMISSION_COMPONENT_IDS,
            )
        ]
    return {
        "role_code": role_code,
        "display_mode": display_mode,
        "version": _version(release),
        "layout": filtered,
        "fallback_reason": fallback_reason,
    }


def _request_fingerprint(operation_kind: str, body: dict[str, Any]) -> str:
    return hashlib.sha256(
        _render({"operation_kind": operation_kind, **body}).encode("utf-8")
    ).hexdigest()


def _operation_replay(
    db: Session,
    operation_key: str,
    fingerprint: str,
) -> UiLayoutRevision | None:
    row = db.scalar(
        select(UiLayoutRevision).where(UiLayoutRevision.operation_key == operation_key)
    )
    if row is None:
        return None
    if row.request_fingerprint != fingerprint:
        raise UiLayoutConflict("操作编号已被另一项界面配置使用")
    return row


def _append_revision(
    db: Session,
    *,
    role_code: str,
    display_mode: str,
    stream: str,
    version: int,
    layout: dict[str, Any],
    base_release_version: int,
    operation_kind: str,
    created_by: int | None,
    operation_key: str | None = None,
    request_fingerprint: str | None = None,
    source_release_version: int | None = None,
) -> UiLayoutRevision:
    row = UiLayoutRevision(
        role_code=role_code,
        display_mode=display_mode,
        stream=stream,
        version=version,
        catalog_version=CATALOG_VERSION,
        payload_json=_render(layout),
        payload_hash=_digest(layout),
        base_release_version=base_release_version,
        operation_kind=operation_kind,
        operation_key=operation_key,
        request_fingerprint=request_fingerprint,
        source_release_version=source_release_version,
        created_by=created_by,
    )
    db.add(row)
    db.flush()
    return row


def _result(db: Session, role_code: str, display_mode: str, *, replayed: bool, operation_kind: str) -> dict[str, Any]:
    return {
        **admin_state(db, role_code, display_mode),
        "replayed": replayed,
        "operation_kind": operation_kind,
    }


def save_draft(
    db: Session,
    *,
    role_code: str,
    display_mode: str,
    layout: dict[str, Any],
    expected_draft_version: int,
    operation_key: str,
    actor_id: int | None,
) -> dict[str, Any]:
    validate_profile(role_code, display_mode)
    normalized = normalize_layout(role_code, display_mode, layout)
    request = {
        "role_code": role_code,
        "display_mode": display_mode,
        "layout": normalized,
        "expected_draft_version": expected_draft_version,
    }
    fingerprint = _request_fingerprint("save_draft", request)
    if _operation_replay(db, operation_key, fingerprint):
        return _result(db, role_code, display_mode, replayed=True, operation_kind="save_draft")
    draft = _latest(db, role_code, display_mode, "draft")
    release = _latest(db, role_code, display_mode, "release")
    if expected_draft_version != _version(draft):
        raise UiLayoutConflict("草稿版本已变化，请重新载入")
    _append_revision(
        db,
        role_code=role_code,
        display_mode=display_mode,
        stream="draft",
        version=_version(draft) + 1,
        layout=normalized,
        base_release_version=_version(release),
        operation_kind="save_draft",
        operation_key=operation_key,
        request_fingerprint=fingerprint,
        created_by=actor_id,
    )
    return _result(db, role_code, display_mode, replayed=False, operation_kind="save_draft")


def publish_draft(
    db: Session,
    *,
    role_code: str,
    display_mode: str,
    expected_draft_version: int,
    expected_release_version: int,
    operation_key: str,
    actor_id: int | None,
) -> dict[str, Any]:
    validate_profile(role_code, display_mode)
    request = {
        "role_code": role_code,
        "display_mode": display_mode,
        "expected_draft_version": expected_draft_version,
        "expected_release_version": expected_release_version,
    }
    fingerprint = _request_fingerprint("publish", request)
    if _operation_replay(db, operation_key, fingerprint):
        return _result(db, role_code, display_mode, replayed=True, operation_kind="publish")
    draft = _latest(db, role_code, display_mode, "draft")
    release = _latest(db, role_code, display_mode, "release")
    if draft is None:
        raise UiLayoutConflict("请先保存草稿")
    if expected_draft_version != _version(draft) or expected_release_version != _version(release):
        raise UiLayoutConflict("草稿或已发布版本已变化，请重新载入")
    if int(draft.base_release_version) != _version(release):
        raise UiLayoutConflict("草稿基于旧版本，请重新载入后保存")
    layout = _row_layout(draft)
    release_version = _version(release) + 1
    _append_revision(
        db,
        role_code=role_code,
        display_mode=display_mode,
        stream="release",
        version=release_version,
        layout=layout,
        base_release_version=_version(release),
        operation_kind="publish",
        operation_key=operation_key,
        request_fingerprint=fingerprint,
        created_by=actor_id,
    )
    _append_revision(
        db,
        role_code=role_code,
        display_mode=display_mode,
        stream="draft",
        version=_version(draft) + 1,
        layout=layout,
        base_release_version=release_version,
        operation_kind="publish_sync",
        created_by=actor_id,
    )
    return _result(db, role_code, display_mode, replayed=False, operation_kind="publish")


def _replace_release(
    db: Session,
    *,
    role_code: str,
    display_mode: str,
    expected_draft_version: int,
    expected_release_version: int,
    operation_key: str,
    actor_id: int | None,
    operation_kind: str,
    replacement: dict[str, Any],
    source_release_version: int | None,
) -> dict[str, Any]:
    request = {
        "role_code": role_code,
        "display_mode": display_mode,
        "expected_draft_version": expected_draft_version,
        "expected_release_version": expected_release_version,
    }
    fingerprint = _request_fingerprint(operation_kind, request)
    if _operation_replay(db, operation_key, fingerprint):
        return _result(db, role_code, display_mode, replayed=True, operation_kind=operation_kind)
    draft = _latest(db, role_code, display_mode, "draft")
    release = _latest(db, role_code, display_mode, "release")
    if expected_draft_version != _version(draft) or expected_release_version != _version(release):
        raise UiLayoutConflict("草稿或已发布版本已变化，请重新载入")
    normalized = normalize_layout(role_code, display_mode, replacement)
    release_version = _version(release) + 1
    _append_revision(
        db,
        role_code=role_code,
        display_mode=display_mode,
        stream="release",
        version=release_version,
        layout=normalized,
        base_release_version=_version(release),
        operation_kind=operation_kind,
        operation_key=operation_key,
        request_fingerprint=fingerprint,
        source_release_version=source_release_version,
        created_by=actor_id,
    )
    _append_revision(
        db,
        role_code=role_code,
        display_mode=display_mode,
        stream="draft",
        version=_version(draft) + 1,
        layout=normalized,
        base_release_version=release_version,
        operation_kind=f"{operation_kind}_sync",
        source_release_version=source_release_version,
        created_by=actor_id,
    )
    return _result(db, role_code, display_mode, replayed=False, operation_kind=operation_kind)


def restore_default(
    db: Session,
    **kwargs: Any,
) -> dict[str, Any]:
    role_code = str(kwargs["role_code"])
    display_mode = str(kwargs["display_mode"])
    validate_profile(role_code, display_mode)
    return _replace_release(
        db,
        **kwargs,
        operation_kind="restore_default",
        replacement=default_layout(role_code, display_mode),
        source_release_version=0,
    )


def rollback_release(
    db: Session,
    **kwargs: Any,
) -> dict[str, Any]:
    role_code = str(kwargs["role_code"])
    display_mode = str(kwargs["display_mode"])
    validate_profile(role_code, display_mode)
    history = _release_history(db, role_code, display_mode, limit=2)
    if len(history) < 2:
        raise UiLayoutConflict("当前没有上一版已发布布局可回滚")
    source = history[1]
    replacement = _row_layout(source)
    return _replace_release(
        db,
        **kwargs,
        operation_kind="rollback",
        replacement=replacement,
        source_release_version=_version(source),
    )


def layout_diff_summary(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for section in LAYOUT_SECTIONS:
        old = {item["id"]: (index, item["visible"]) for index, item in enumerate(before[section])}
        new = {item["id"]: (index, item["visible"]) for index, item in enumerate(after[section])}
        changed = [component_id for component_id in new if old.get(component_id) != new[component_id]]
        summary[section] = {"changed_count": len(changed), "changed_ids": changed[:20]}
    return summary
