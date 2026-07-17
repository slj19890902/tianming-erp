from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from typing import Any, Literal
from uuid import uuid4

from fastapi import HTTPException, status
import jwt
from sqlalchemy import Boolean, Date, DateTime, Integer, Numeric, select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.core.config import load_settings
from app.models.audit import OperationLog
from app.models.customer import Customer
from app.models.master_data_object_version import MasterDataObjectVersion
from app.models.material import Material
from app.models.product import Product
from app.models.user import User


MasterDataObjectType = Literal["customer", "product", "material"]
VersionedEntity = Customer | Product | Material
SNAPSHOT_SCHEMA_VERSION = 1
CONFIRMATION_TOKEN_TTL_MINUTES = 5


@dataclass(frozen=True, slots=True)
class _ObjectSpec:
    model: type[VersionedEntity]
    fields: tuple[str, ...]
    numeric_fields: frozenset[str]


# These are the only fields that may enter a snapshot or be changed by a
# historical restore.  IDs, version/timestamps, ORM relationships, drawing
# data, deletion metadata, and order/inventory workflow state are deliberately
# absent.  Product history restore therefore rejects trash-state transitions;
# those must continue to use the dedicated product lifecycle API.
_CUSTOMER_FIELDS = (
    "customer_number",
    "customer_code",
    "name",
    "payment_term_days",
    "statement_cycle_start_day",
    "credit_limit",
    "delivery_method",
    "contact_person",
    "phone",
    "address",
    "billing_note",
    "credit_terms",
    "default_tax_rate",
    "invoice_title",
    "tax_no",
    "bank_account",
    "remark",
    "status",
    "is_active",
)
_PRODUCT_FIELDS = (
    "customer_id",
    "product_code",
    "customer_material_code",
    "product_name",
    "material_id",
    "mold_tool_id",
    "legacy_material_text",
    "length_mm",
    "width_mm",
    "height_mm",
    "box_category",
    "box_style",
    "print_content",
    "printing_colors",
    "production_process",
    "unit",
    "sale_unit_price",
    "sale_unit_price_no_tax",
    "cost_unit_price",
    "board_price",
    "suggested_price",
    "default_cardboard_length",
    "default_cardboard_width",
    "default_score_lines",
    "default_material_code",
    "remark",
    "legacy_customer_material_code",
    "flute_type",
    "layer_count",
    "surface_paper_type",
    "report_length_mm",
    "report_width_mm",
    "crease_type",
    "crease_left_mm",
    "crease_middle_mm",
    "crease_right_mm",
    "report_notes",
    "base_report_length_mm",
    "base_report_width_mm",
    "base_crease_type",
    "base_crease_left_mm",
    "base_crease_middle_mm",
    "base_crease_right_mm",
    "base_report_notes",
    "splice_mode",
    "pieces_per_box",
    "flap_mm",
    "is_active",
)
_MATERIAL_FIELDS = (
    "code",
    "paper_composition",
    "layer_count",
    "flute_type",
    "basis_weight_description",
    "quote_price",
    "rule_base_price",
    "price_source",
    "price_unit",
    "supplier_name",
    "quote_date",
    "remarks",
    "is_active",
)

_OBJECT_SPECS: dict[MasterDataObjectType, _ObjectSpec] = {
    "customer": _ObjectSpec(
        model=Customer,
        fields=_CUSTOMER_FIELDS,
        numeric_fields=frozenset(
            {
                "payment_term_days",
                "statement_cycle_start_day",
                "credit_limit",
                "default_tax_rate",
            }
        ),
    ),
    "product": _ObjectSpec(
        model=Product,
        fields=_PRODUCT_FIELDS,
        numeric_fields=frozenset(
            {
                "length_mm",
                "width_mm",
                "height_mm",
                "sale_unit_price",
                "sale_unit_price_no_tax",
                "cost_unit_price",
                "board_price",
                "suggested_price",
                "default_cardboard_length",
                "default_cardboard_width",
                "report_length_mm",
                "report_width_mm",
                "crease_left_mm",
                "crease_middle_mm",
                "crease_right_mm",
                "base_report_length_mm",
                "base_report_width_mm",
                "base_crease_left_mm",
                "base_crease_middle_mm",
                "base_crease_right_mm",
                "pieces_per_box",
                "flap_mm",
            }
        ),
    ),
    "material": _ObjectSpec(
        model=Material,
        fields=_MATERIAL_FIELDS,
        numeric_fields=frozenset({"quote_price", "rule_base_price"}),
    ),
}

_CUSTOMER_IDENTITY_FIELDS = frozenset(
    {"customer_number", "customer_code", "name", "invoice_title", "tax_no"}
)
# The current schema does not persist an internal salesperson owner on a
# customer row.  Keep the canonical names here so the rule remains explicit if
# such a recoverable field is introduced in a later schema version.
_CUSTOMER_OWNERSHIP_FIELDS = frozenset(
    {
        "owner_user_id",
        "sales_owner_id",
        "salesperson_id",
        "account_manager_id",
    }
)
_PRODUCT_CODE_FIELDS = frozenset({"product_code", "customer_material_code"})
_PRODUCT_DIMENSION_FIELDS = frozenset(
    {
        "length_mm",
        "width_mm",
        "height_mm",
        "default_cardboard_length",
        "default_cardboard_width",
        "report_length_mm",
        "report_width_mm",
        "crease_left_mm",
        "crease_middle_mm",
        "crease_right_mm",
        "base_report_length_mm",
        "base_report_width_mm",
        "base_crease_left_mm",
        "base_crease_middle_mm",
        "base_crease_right_mm",
        "flap_mm",
    }
)
_PRODUCT_MATERIAL_FLUTE_FIELDS = frozenset(
    {
        "material_id",
        "legacy_material_text",
        "default_material_code",
        "flute_type",
        "layer_count",
        "surface_paper_type",
    }
)
_PRODUCT_TRASH_REVISION_ACTIONS = frozenset(
    {"soft_delete", "purge", "physical_delete", "archive_purged"}
)


def _spec(object_type: str) -> _ObjectSpec:
    try:
        return _OBJECT_SPECS[object_type]  # type: ignore[index]
    except KeyError as error:
        raise ValueError(
            "object_type 仅允许 customer、product 或 material"
        ) from error


def _validate_entity(
    object_type: str,
    entity: VersionedEntity,
) -> _ObjectSpec:
    spec = _spec(object_type)
    if not isinstance(entity, spec.model):
        raise ValueError(f"{object_type} 与实体类型不匹配")
    return spec


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("JSON 快照不允许 NaN 或 Infinity")
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def normalize_json_value(value: Any) -> Any:
    """Return deterministic JSON-compatible values for snapshots and hashes."""

    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
            return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
        return value.isoformat(timespec="microseconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON 快照不允许 NaN 或 Infinity")
        return _decimal_text(Decimal(str(value)))
    if isinstance(value, Mapping):
        return {
            str(key): normalize_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [normalize_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"不支持写入 JSON 快照的类型：{type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        normalize_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(canonical_payload: str) -> str:
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


def serialize_versioned_entity(
    object_type: str,
    entity: VersionedEntity,
) -> dict[str, Any]:
    spec = _validate_entity(object_type, entity)
    return {
        field: normalize_json_value(getattr(entity, field))
        for field in spec.fields
    }


def _diff(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    return {
        field: {"before": before.get(field), "after": after.get(field)}
        for field in sorted(set(before) | set(after))
        if before.get(field) != after.get(field)
    }


def _as_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _warning(code: str, field: str | None, message: str) -> dict[str, Any]:
    return {"code": code, "field": field, "message": message}


def _change_warnings(
    object_type: str,
    changed: Mapping[str, Mapping[str, Any]],
    *,
    numeric_fields: frozenset[str],
    action: str,
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    changed_fields = set(changed)
    specialized_system_action = action in {
        "system_mapping",
        "system_consistency_fix",
    }

    if specialized_system_action:
        # These admin-only maintenance endpoints already have a dedicated
        # preview/apply workflow and fixed audit reason.  Keep numeric
        # magnitude checks below, but do not require one confirmation token
        # per row merely because a mapping changes a code/material/flute.
        pass
    elif object_type == "customer":
        for field in sorted(changed_fields & _CUSTOMER_IDENTITY_FIELDS):
            warnings.append(
                _warning(
                    "CUSTOMER_IDENTITY_CHANGE",
                    field,
                    "客户身份字段发生变化，请确认不会误合并或错认客户",
                )
            )
        for field in sorted(changed_fields & _CUSTOMER_OWNERSHIP_FIELDS):
            warnings.append(
                _warning(
                    "CUSTOMER_OWNERSHIP_CHANGE",
                    field,
                    "客户归属发生变化，请确认新的负责人和客户范围",
                )
            )
    elif object_type == "product":
        for field in sorted(changed_fields & _PRODUCT_CODE_FIELDS):
            warnings.append(
                _warning(
                    "PRODUCT_CODE_CHANGE",
                    field,
                    "产品编码或客户料号发生变化，请确认历史单据识别不受影响",
                )
            )
        if "customer_id" in changed_fields:
            warnings.append(
                _warning(
                    "PRODUCT_CUSTOMER_CHANGE",
                    "customer_id",
                    "产品所属客户发生变化，请确认客户归属",
                )
            )
        # Ordinary dimension corrections are already shown in the first
        # before/after confirmation.  Only suspicious 5x magnitude changes
        # need the second abnormal-change confirmation below; otherwise every
        # normal 1-2mm correction would create unnecessary operator friction.
        for field in sorted(changed_fields & _PRODUCT_MATERIAL_FLUTE_FIELDS):
            warnings.append(
                _warning(
                    "PRODUCT_MATERIAL_FLUTE_CHANGE",
                    field,
                    "产品材质、层数或楞型发生变化，请确认生产用料",
                )
            )
    else:
        material_rules = {
            "code": (
                "MATERIAL_CODE_CHANGE",
                "材质代码发生变化，请确认不会影响历史匹配",
            ),
            "supplier_name": (
                "MATERIAL_SUPPLIER_CHANGE",
                "材质供应商发生变化，请确认供应来源",
            ),
            "layer_count": (
                "MATERIAL_LAYER_COUNT_CHANGE",
                "材质层数发生变化，请确认材质结构",
            ),
        }
        for field in sorted(changed_fields & material_rules.keys()):
            code, message = material_rules[field]
            warnings.append(_warning(code, field, message))

    for field in sorted(changed_fields & numeric_fields):
        old = _as_decimal(changed[field].get("before"))
        new = _as_decimal(changed[field].get("after"))
        if old is None or old <= 0:
            continue
        if new is None or new == 0:
            warnings.append(
                _warning(
                    "NUMERIC_VALUE_CLEARED",
                    field,
                    "正数被改为 0 或空值，请确认不是误清空",
                )
            )
            continue
        if new > 0 and (new >= old * 5 or new * 5 <= old):
            warnings.append(
                _warning(
                    "NUMERIC_VALUE_MAGNITUDE_CHANGE",
                    field,
                    "数值放大至少 5 倍或缩小到不超过原值的 1/5，请确认单位和小数点",
                )
            )

    if action == "restore":
        warnings.append(
            _warning(
                "MASTER_DATA_RESTORE",
                None,
                "恢复历史版本会覆盖当前主数据字段，请再次确认",
            )
        )
    return warnings


def _actor_identity(user: User) -> tuple[int | None, str | None, str | None]:
    user_id = getattr(user, "id", None)
    username = getattr(user, "username", None)
    role = getattr(user, "role", None)
    return (
        int(user_id) if user_id is not None else None,
        str(username) if username is not None else None,
        str(role) if role is not None else None,
    )


def _token_actor(user: User) -> str:
    user_id, username, _ = _actor_identity(user)
    if user_id is not None:
        return f"id:{user_id}"
    if username:
        return f"username:{username}"
    return "anonymous"


def _confirmation_purpose(
    action: str,
    restored_from_version: int | None,
) -> str:
    if action == "restore" and restored_from_version is not None:
        return "historical_restore"
    if action == "restore":
        return "lifecycle_restore"
    return f"master_data_{action}"


def _confirmation_claims(
    *,
    object_type: str,
    object_id: int,
    expected_version: int,
    target_version: int,
    after_sha256: str,
    warnings: list[dict[str, Any]],
    action: str,
    purpose: str,
    restored_from_version: int | None,
    user: User,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "sub": _token_actor(user),
        "type": "master_data_change_confirmation",
        "object_type": object_type,
        "object_id": object_id,
        "expected_version": expected_version,
        "target_version": target_version,
        "after_sha256": after_sha256,
        "anomaly_codes": sorted({str(item["code"]) for item in warnings}),
        "action": action,
        "purpose": purpose,
        # PyJWT treats a required claim with a JSON null value as missing.
        # Zero is outside the valid version domain and keeps non-restore tokens
        # explicitly bound to the absence of a historical source.
        "restored_from_version": restored_from_version or 0,
        "iat": now,
        "exp": now + timedelta(minutes=CONFIRMATION_TOKEN_TTL_MINUTES),
    }


def _create_confirmation_token(**kwargs: Any) -> str:
    return jwt.encode(
        _confirmation_claims(**kwargs),
        load_settings().secret_key,
        algorithm="HS256",
    )


def _confirmation_matches(token: str | None, **kwargs: Any) -> bool:
    if not token:
        return False
    expected = _confirmation_claims(**kwargs)
    try:
        payload = jwt.decode(
            token,
            load_settings().secret_key,
            algorithms=["HS256"],
            options={
                "require": [
                    "sub",
                    "type",
                    "object_type",
                    "object_id",
                    "expected_version",
                    "target_version",
                    "after_sha256",
                    "anomaly_codes",
                    "action",
                    "purpose",
                    "restored_from_version",
                    "iat",
                    "exp",
                ]
            },
        )
    except jwt.PyJWTError:
        return False
    claim_names = (
        "sub",
        "type",
        "object_type",
        "object_id",
        "expected_version",
        "target_version",
        "after_sha256",
        "anomaly_codes",
        "action",
        "purpose",
        "restored_from_version",
    )
    return all(payload.get(name) == expected.get(name) for name in claim_names)


def _confirmation_required(
    *,
    object_type: str,
    object_id: int,
    expected_version: int,
    target_version: int,
    after_sha256: str,
    changed_fields: list[str],
    warnings: list[dict[str, Any]],
    action: str,
    purpose: str,
    restored_from_version: int | None,
    user: User,
) -> HTTPException:
    token = _create_confirmation_token(
        object_type=object_type,
        object_id=object_id,
        expected_version=expected_version,
        target_version=target_version,
        after_sha256=after_sha256,
        warnings=warnings,
        action=action,
        purpose=purpose,
        restored_from_version=restored_from_version,
        user=user,
    )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "MASTER_CHANGE_CONFIRMATION_REQUIRED",
            "current_version": expected_version,
            "changed_fields": changed_fields,
            "warnings": warnings,
            "confirmation_token": token,
        },
    )


def _version_conflict(expected_version: int, current_version: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "MASTER_VERSION_CONFLICT",
            "expected_version": expected_version,
            "current_version": current_version,
        },
    )


def _validate_historical_restore_source(
    *,
    object_type: str,
    entity: VersionedEntity,
    revision: MasterDataObjectVersion,
    target_version: int,
) -> None:
    object_id = int(entity.id)
    if revision.object_type != object_type or revision.object_id != object_id:
        raise ValueError("目标历史版本不属于该主数据对象")
    if revision.version >= target_version:
        raise ValueError("恢复来源版本必须早于将要创建的新版本")
    if object_type != "product":
        return

    product = entity
    if product.purged_at is not None:
        raise ValueError("永久归档的产品不能通过历史版本恢复")
    if product.deleted_at is not None:
        raise ValueError("垃圾站中的产品必须先使用专用垃圾站恢复接口")
    if revision.action in _PRODUCT_TRASH_REVISION_ACTIONS:
        raise ValueError("通用历史恢复不能恢复到产品垃圾站生命周期版本")


def _database_version(
    db: Session,
    *,
    spec: _ObjectSpec,
    object_id: int,
) -> int | None:
    with db.no_autoflush:
        value = db.scalar(
            select(spec.model.version).where(spec.model.id == object_id)
        )
    return int(value) if value is not None else None


def _new_operation_log(
    *,
    user: User,
    action: str,
    object_type: str,
    object_id: int,
    details: Mapping[str, Any],
) -> OperationLog:
    user_id, username, role = _actor_identity(user)
    action_name = f"MASTER_{action.upper()}"[:30]
    return OperationLog(
        user_id=user_id,
        action=action_name,
        resource=f"master_data.{object_type}",
        details=canonical_json(details),
        username=username,
        role=role,
        entity_type=object_type,
        entity_id=object_id,
        description=f"{action_name} {object_type}#{object_id}",
    )


def _new_revision(
    *,
    object_type: str,
    object_id: int,
    version: int,
    action: str,
    snapshot: Mapping[str, Any],
    changed: Mapping[str, Any],
    restored_from_version: int | None,
    change_set_id: str,
    operation_log_id: int | None,
    user: User,
    reason: str | None,
    source: str,
) -> MasterDataObjectVersion:
    snapshot_json = canonical_json(snapshot)
    user_id, username, _ = _actor_identity(user)
    return MasterDataObjectVersion(
        object_type=object_type,
        object_id=object_id,
        version=version,
        action=action,
        snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
        snapshot_json=snapshot_json,
        snapshot_sha256=_sha256(snapshot_json),
        changed_fields_json=canonical_json(changed),
        restored_from_version=restored_from_version,
        change_set_id=change_set_id,
        operation_log_id=operation_log_id,
        actor_user_id=user_id,
        actor_username_snapshot=username,
        reason=reason,
        source=source,
    )


def record_versioned_create(
    db: Session,
    *,
    object_type: str,
    entity: VersionedEntity,
    user: User,
    reason: str | None,
    source: str,
) -> MasterDataObjectVersion:
    _validate_entity(object_type, entity)
    if getattr(entity, "version", None) not in (None, 1):
        raise ValueError("新建主数据的初始版本必须为 1")
    entity.version = 1
    db.flush()
    object_id = int(entity.id)
    existing = db.scalar(
        select(MasterDataObjectVersion.id).where(
            MasterDataObjectVersion.object_type == object_type,
            MasterDataObjectVersion.object_id == object_id,
            MasterDataObjectVersion.version == 1,
        )
    )
    if existing is not None:
        raise ValueError("该主数据的 v1 版本记录已存在")

    snapshot = serialize_versioned_entity(object_type, entity)
    changed = {
        field: {"before": None, "after": value}
        for field, value in snapshot.items()
    }
    change_set_id = str(uuid4())
    operation_log = _new_operation_log(
        user=user,
        action="create",
        object_type=object_type,
        object_id=object_id,
        details={
            "object_type": object_type,
            "object_id": object_id,
            "version": 1,
            "changed_fields": changed,
            "reason": reason,
            "source": source,
        },
    )
    db.add(operation_log)
    db.flush()
    revision = _new_revision(
        object_type=object_type,
        object_id=object_id,
        version=1,
        action="create",
        snapshot=snapshot,
        changed=changed,
        restored_from_version=None,
        change_set_id=change_set_id,
        operation_log_id=operation_log.id,
        user=user,
        reason=reason,
        source=source,
    )
    db.add(revision)
    db.flush()
    return revision


def apply_versioned_update(
    db: Session,
    *,
    object_type: str,
    entity: VersionedEntity,
    updates: Mapping[str, Any],
    expected_version: int,
    user: User,
    reason: str | None,
    source: str,
    action: str = "update",
    confirmation_token: str | None = None,
    restored_from_version: int | None = None,
    force_version: bool = False,
) -> MasterDataObjectVersion | None:
    spec = _validate_entity(object_type, entity)
    object_id = int(entity.id)
    current_version = _database_version(
        db,
        spec=spec,
        object_id=object_id,
    )
    if current_version is None:
        raise ValueError("主数据实体尚未持久化或已不存在")
    if current_version != expected_version:
        raise _version_conflict(expected_version, current_version)

    unknown_fields = sorted(set(updates) - set(spec.fields))
    if unknown_fields:
        raise ValueError(
            "不可写入版本快照的字段：" + ", ".join(unknown_fields)
        )

    normalized_action = action.strip().lower() or "update"
    target_version = current_version + 1
    purpose = _confirmation_purpose(
        normalized_action,
        restored_from_version,
    )
    if restored_from_version is not None:
        if normalized_action != "restore":
            raise ValueError("仅历史恢复操作可以设置 restored_from_version")
        restore_source = db.scalar(
            select(MasterDataObjectVersion).where(
                MasterDataObjectVersion.object_type == object_type,
                MasterDataObjectVersion.object_id == object_id,
                MasterDataObjectVersion.version == restored_from_version,
            )
        )
        if restore_source is None:
            raise ValueError("恢复来源历史版本不存在")
        _validate_historical_restore_source(
            object_type=object_type,
            entity=entity,
            revision=restore_source,
            target_version=target_version,
        )

    before = serialize_versioned_entity(object_type, entity)
    proposed = dict(before)
    proposed.update(
        {field: normalize_json_value(value) for field, value in updates.items()}
    )
    changed = _diff(before, proposed)
    if not changed and not force_version:
        return None

    warnings = _change_warnings(
        object_type,
        changed,
        numeric_fields=spec.numeric_fields,
        action=normalized_action,
    )
    after_json = canonical_json(proposed)
    after_sha256 = _sha256(after_json)
    confirmation_kwargs = {
        "object_type": object_type,
        "object_id": object_id,
        "expected_version": expected_version,
        "target_version": target_version,
        "after_sha256": after_sha256,
        "warnings": warnings,
        "action": normalized_action,
        "purpose": purpose,
        "restored_from_version": restored_from_version,
        "user": user,
    }
    if warnings and not _confirmation_matches(
        confirmation_token,
        **confirmation_kwargs,
    ):
        raise _confirmation_required(
            changed_fields=sorted(changed),
            **confirmation_kwargs,
        )

    change_set_id = str(uuid4())
    has_v1 = db.scalar(
        select(MasterDataObjectVersion.id).where(
            MasterDataObjectVersion.object_type == object_type,
            MasterDataObjectVersion.object_id == object_id,
            MasterDataObjectVersion.version == 1,
        )
    )
    baseline_needed = current_version == 1 and has_v1 is None

    changed_at = datetime.now()
    new_version = current_version + 1
    cas_result = db.execute(
        update(spec.model)
        .where(
            spec.model.id == object_id,
            spec.model.version == expected_version,
        )
        .values(version=new_version, updated_at=changed_at)
        .execution_options(synchronize_session=False)
    )
    if cas_result.rowcount != 1:
        actual_version = _database_version(
            db,
            spec=spec,
            object_id=object_id,
        )
        raise _version_conflict(
            expected_version,
            actual_version if actual_version is not None else current_version,
        )

    set_committed_value(entity, "version", new_version)
    set_committed_value(entity, "updated_at", changed_at)
    for field in changed:
        setattr(entity, field, updates[field])
    db.flush()
    after = serialize_versioned_entity(object_type, entity)

    operation_log = _new_operation_log(
        user=user,
        action=normalized_action,
        object_type=object_type,
        object_id=object_id,
        details={
            "object_type": object_type,
            "object_id": object_id,
            "from_version": current_version,
            "to_version": new_version,
            "changed_fields": changed,
            "restored_from_version": restored_from_version,
            "reason": reason,
            "source": source,
        },
    )
    db.add(operation_log)
    db.flush()

    if baseline_needed:
        db.add(
            _new_revision(
                object_type=object_type,
                object_id=object_id,
                version=1,
                action="baseline",
                snapshot=before,
                changed={},
                restored_from_version=None,
                change_set_id=change_set_id,
                operation_log_id=operation_log.id,
                user=user,
                reason="首次修改前自动建立基线",
                source=source,
            )
        )

    revision = _new_revision(
        object_type=object_type,
        object_id=object_id,
        version=new_version,
        action=normalized_action,
        snapshot=after,
        changed=changed,
        restored_from_version=restored_from_version,
        change_set_id=change_set_id,
        operation_log_id=operation_log.id,
        user=user,
        reason=reason,
        source=source,
    )
    db.add(revision)
    db.flush()
    return revision


def list_object_versions(
    db: Session,
    *,
    object_type: str,
    object_id: int,
    offset: int = 0,
    limit: int = 100,
) -> list[MasterDataObjectVersion]:
    _spec(object_type)
    if offset < 0 or limit < 1 or limit > 500:
        raise ValueError("offset 必须不小于 0，limit 必须在 1 到 500 之间")
    return list(
        db.scalars(
            select(MasterDataObjectVersion)
            .where(
                MasterDataObjectVersion.object_type == object_type,
                MasterDataObjectVersion.object_id == object_id,
            )
            .order_by(
                MasterDataObjectVersion.version.desc(),
                MasterDataObjectVersion.id.desc(),
            )
            .offset(offset)
            .limit(limit)
        ).all()
    )


def get_object_version(
    db: Session,
    *,
    object_type: str,
    object_id: int,
    version: int,
) -> MasterDataObjectVersion | None:
    _spec(object_type)
    return db.scalar(
        select(MasterDataObjectVersion).where(
            MasterDataObjectVersion.object_type == object_type,
            MasterDataObjectVersion.object_id == object_id,
            MasterDataObjectVersion.version == version,
        )
    )


def revision_snapshot(revision: MasterDataObjectVersion) -> dict[str, Any]:
    try:
        snapshot = json.loads(revision.snapshot_json)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("版本快照 JSON 已损坏") from error
    if not isinstance(snapshot, dict):
        raise ValueError("版本快照必须为 JSON 对象")
    canonical = canonical_json(snapshot)
    if _sha256(canonical) != revision.snapshot_sha256:
        raise ValueError("版本快照校验失败，禁止使用该版本恢复")
    return snapshot


def _coerce_snapshot_value(spec: _ObjectSpec, field: str, value: Any) -> Any:
    if value is None:
        return None
    column_type = spec.model.__table__.c[field].type
    if isinstance(column_type, Numeric):
        return Decimal(str(value))
    if isinstance(column_type, DateTime):
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text)
    if isinstance(column_type, Date):
        return date.fromisoformat(str(value))
    if isinstance(column_type, Boolean):
        return bool(value)
    if isinstance(column_type, Integer):
        return int(value)
    return value


def snapshot_updates(
    object_type: str,
    revision: MasterDataObjectVersion,
) -> dict[str, Any]:
    spec = _spec(object_type)
    if revision.object_type != object_type:
        raise ValueError("版本记录与 object_type 不匹配")
    snapshot = revision_snapshot(revision)
    return {
        field: _coerce_snapshot_value(spec, field, snapshot[field])
        for field in spec.fields
        if field in snapshot
    }


def preview_versioned_restore(
    db: Session,
    *,
    object_type: str,
    entity: VersionedEntity,
    revision: MasterDataObjectVersion,
    expected_version: int,
    user: User,
) -> dict[str, Any]:
    spec = _validate_entity(object_type, entity)
    object_id = int(entity.id)
    if revision.object_id != object_id or revision.object_type != object_type:
        raise ValueError("目标历史版本不属于该主数据对象")
    current_version = _database_version(
        db,
        spec=spec,
        object_id=object_id,
    )
    if current_version is None:
        raise ValueError("主数据实体已不存在")
    if current_version != expected_version:
        raise _version_conflict(expected_version, current_version)

    target_version = current_version + 1
    _validate_historical_restore_source(
        object_type=object_type,
        entity=entity,
        revision=revision,
        target_version=target_version,
    )

    updates = snapshot_updates(object_type, revision)
    before = serialize_versioned_entity(object_type, entity)
    proposed = dict(before)
    proposed.update(
        {field: normalize_json_value(value) for field, value in updates.items()}
    )
    changed = _diff(before, proposed)
    warnings = _change_warnings(
        object_type,
        changed,
        numeric_fields=spec.numeric_fields,
        action="restore",
    )
    after_json = canonical_json(proposed)
    after_sha256 = _sha256(after_json)
    token = _create_confirmation_token(
        object_type=object_type,
        object_id=object_id,
        expected_version=expected_version,
        target_version=target_version,
        after_sha256=after_sha256,
        warnings=warnings,
        action="restore",
        purpose="historical_restore",
        restored_from_version=revision.version,
        user=user,
    )
    return {
        "object_type": object_type,
        "object_id": object_id,
        "current_version": current_version,
        "restore_from_version": revision.version,
        "changed_fields": sorted(changed),
        "changes": changed,
        "warnings": warnings,
        "confirmation_token": token,
        "can_restore": bool(changed),
    }


def serialize_revision(revision: MasterDataObjectVersion) -> dict[str, Any]:
    try:
        changed_fields = json.loads(revision.changed_fields_json)
    except (TypeError, json.JSONDecodeError):
        changed_fields = {}
    return {
        "id": revision.id,
        "object_type": revision.object_type,
        "object_id": revision.object_id,
        "version": revision.version,
        "action": revision.action,
        "snapshot_schema_version": revision.snapshot_schema_version,
        "snapshot_sha256": revision.snapshot_sha256,
        "changed_fields": changed_fields,
        "restored_from_version": revision.restored_from_version,
        "change_set_id": revision.change_set_id,
        "operation_log_id": revision.operation_log_id,
        "actor_user_id": revision.actor_user_id,
        "actor_username": revision.actor_username_snapshot,
        "reason": revision.reason,
        "source": revision.source,
        "created_at": revision.created_at,
    }
