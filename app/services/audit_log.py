from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import Request
from sqlalchemy.orm import Session

from app.core.request_context import get_current_request
from app.models.audit import OperationLog
from app.models.user import User


AUDIT_SCHEMA_VERSION = 1
AUDIT_EVENT_CATEGORIES = frozenset(
    {"business", "security", "audit_access", "system"}
)
AUDIT_RESULTS = frozenset(
    {"success", "failed", "denied", "partial", "legacy", "no_change"}
)
AUDIT_SOURCES = frozenset(
    {
        "web",
        "mobile",
        "import",
        "system",
        "script",
        "migration",
        "api",
        "legacy",
    }
)

MAX_AUDIT_DEPTH = 6
MAX_AUDIT_MAPPING_ITEMS = 64
MAX_AUDIT_SEQUENCE_ITEMS = 50
MAX_AUDIT_TEXT_CHARS = 2_000
MAX_AUDIT_DETAILS_JSON_CHARS = 16_000
MAX_AUDIT_USER_AGENT_CHARS = 256

REDACTED = "[已脱敏]"
TRUNCATED = "[内容过长，已截断]"
_SAFE_SENSITIVE_BOOLEAN_KEYS = frozenset(
    {
        "credential_change_required",
        "credential_changed",
    }
)

_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?:^|[_\-.])(?:"
    r"password|passwd|pwd|password_hash|"
    r"token|access_token|refresh_token|security_token|"
    r"authorization|cookie|set_cookie|session|session_id|"
    r"secret|secret_key|api_key|credential|private_key|"
    r"connection_string|database_url|dsn|"
    r"file_content|attachment_content|document_content|"
    r"pdf_content|excel_content|image_content|"
    r"raw_file|raw_pdf|raw_excel|raw_image|"
    r"file_bytes|attachment_bytes|pdf_bytes|excel_bytes|image_bytes|"
    r"binary|base64"
    r")(?:$|[_\-.])",
    re.IGNORECASE,
)
_INLINE_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{12,}"),
    re.compile(
        r"(?i)\b(?:password|passwd|pwd|token|secret|api[_-]?key|"
        r"authorization|cookie)\s*[:=]\s*[^&,\s;]+"
    ),
    re.compile(
        r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\."
        r"[A-Za-z0-9_-]{5,}\b"
    ),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s/]+@"),
)


class AuditContractError(ValueError):
    """Raised before persistence when an audit envelope is invalid."""


def _truncate_text(value: str, limit: int = MAX_AUDIT_TEXT_CHARS) -> str:
    if len(value) <= limit:
        return value
    marker = f"…{TRUNCATED}"
    return f"{value[: max(limit - len(marker), 0)]}{marker}"


def _redact_inline_secrets(value: str) -> str:
    result = value
    for pattern in _INLINE_SECRET_PATTERNS:
        result = pattern.sub(REDACTED, result)
    return result


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(
        r"([A-Z]+)([A-Z][a-z])",
        r"\1_\2",
        key.strip(),
    )
    normalized = re.sub(
        r"([a-z0-9])([A-Z])",
        r"\1_\2",
        normalized,
    )
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", normalized).strip("_").lower()
    return _SENSITIVE_KEY_PATTERN.search(normalized) is not None


def sanitize_audit_value(value: Any, *, _depth: int = 0) -> Any:
    """Return a JSON-safe, bounded value with recursive secret redaction.

    Unknown objects are represented by their type only.  Calling ``str`` on an
    arbitrary object is intentionally avoided because credentials and request
    bodies are frequently embedded in object representations.
    """

    if _depth >= MAX_AUDIT_DEPTH:
        return "[层级过深，已截断]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (UUID, Path)):
        return str(value)
    if isinstance(value, Enum):
        return sanitize_audit_value(value.value, _depth=_depth + 1)
    if isinstance(value, str):
        return _truncate_text(_redact_inline_secrets(value))
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return {
            "binary_omitted": True,
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        items = list(value.items())
        for key, child in items[:MAX_AUDIT_MAPPING_ITEMS]:
            safe_key = _truncate_text(str(key), 200)
            if (
                safe_key in _SAFE_SENSITIVE_BOOLEAN_KEYS
                and isinstance(child, bool)
            ):
                sanitized[safe_key] = child
            else:
                sanitized[safe_key] = (
                    REDACTED
                    if _is_sensitive_key(safe_key)
                    else sanitize_audit_value(child, _depth=_depth + 1)
                )
        if len(items) > MAX_AUDIT_MAPPING_ITEMS:
            sanitized["_truncated_fields"] = (
                len(items) - MAX_AUDIT_MAPPING_ITEMS
            )
        return sanitized
    if isinstance(value, Sequence):
        items = list(value)
        sanitized_items = [
            sanitize_audit_value(child, _depth=_depth + 1)
            for child in items[:MAX_AUDIT_SEQUENCE_ITEMS]
        ]
        if len(items) > MAX_AUDIT_SEQUENCE_ITEMS:
            sanitized_items.append(
                {
                    "_truncated_items": (
                        len(items) - MAX_AUDIT_SEQUENCE_ITEMS
                    )
                }
            )
        return sanitized_items
    return f"<{type(value).__name__}>"


def serialize_audit_details(details: Any) -> str | None:
    """Serialize one bounded audit detail payload after sanitization."""

    if details is None:
        return None
    sanitized = sanitize_audit_value(details)
    rendered = json.dumps(
        sanitized,
        ensure_ascii=False,
        sort_keys=True,
    )
    if len(rendered) <= MAX_AUDIT_DETAILS_JSON_CHARS:
        return rendered
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    # A JSON string may nearly double in size when quotes and backslashes are
    # escaped, so keep the preview to one third of the persisted upper bound.
    preview_limit = MAX_AUDIT_DETAILS_JSON_CHARS // 3
    return json.dumps(
        {
            "_truncated": True,
            "original_sha256": digest,
            "preview": rendered[:preview_limit],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _required_contract_value(
    value: str,
    *,
    field_name: str,
    max_length: int,
    allowed: frozenset[str] | None = None,
    lowercase: bool = False,
) -> str:
    normalized = str(value or "").strip()
    if lowercase:
        normalized = normalized.lower()
    if not normalized:
        raise AuditContractError(f"{field_name} is required")
    if len(normalized) > max_length:
        raise AuditContractError(
            f"{field_name} exceeds {max_length} characters"
        )
    if allowed is not None and normalized not in allowed:
        raise AuditContractError(f"unsupported {field_name}: {normalized}")
    return normalized


def _optional_text(value: Any, max_length: int) -> str | None:
    if value is None:
        return None
    normalized = _redact_inline_secrets(str(value).strip())
    if not normalized:
        return None
    return _truncate_text(normalized, max_length)


def _optional_header_text(value: Any, max_length: int) -> str | None:
    """Bound a request header while preserving the legacy exact-prefix contract."""

    if value is None:
        return None
    normalized = _redact_inline_secrets(str(value).strip())
    if not normalized:
        return None
    return normalized[:max_length]


def _request_context(
    request: Request | None,
    explicit_request_id: str | None,
) -> tuple[str, str | None, str | None]:
    request = request or get_current_request()
    server_request_id = None
    ip_address = None
    user_agent = None
    if request is not None:
        server_request_id = getattr(request.state, "request_id", None)
        ip_address = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")
    # A server-generated request.state value always wins over an explicit
    # argument, so a forged client header can never become the audit key.
    request_id = _optional_text(
        server_request_id or explicit_request_id or uuid4().hex,
        64,
    )
    assert request_id is not None
    return (
        request_id,
        _optional_text(ip_address, 64),
        _optional_header_text(user_agent, MAX_AUDIT_USER_AGENT_CHARS),
    )


def append_audit_event(
    db: Session,
    *,
    event_category: str,
    result: str,
    source: str,
    module_code: str,
    action_code: str,
    resource: str,
    legacy_action: str | None = None,
    request: Request | None = None,
    actor: User | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    object_ref: str | None = None,
    customer_id: int | None = None,
    customer_name: str | None = None,
    request_id: str | None = None,
    batch_id: str | None = None,
    description: str | None = None,
    details: Any = None,
    operator_name: str | None = None,
) -> OperationLog:
    """Append and flush one Q1-02 audit event without committing.

    The caller owns the transaction.  A business write and its success event
    therefore commit or roll back together; audit persistence errors are never
    swallowed and must abort the caller's business action.
    """

    normalized_category = _required_contract_value(
        event_category,
        field_name="event_category",
        max_length=30,
        allowed=AUDIT_EVENT_CATEGORIES,
        lowercase=True,
    )
    normalized_result = _required_contract_value(
        result,
        field_name="result",
        max_length=20,
        allowed=AUDIT_RESULTS,
        lowercase=True,
    )
    normalized_source = _required_contract_value(
        source,
        field_name="source",
        max_length=30,
        allowed=AUDIT_SOURCES,
        lowercase=True,
    )
    normalized_module = _required_contract_value(
        module_code,
        field_name="module_code",
        max_length=50,
        lowercase=True,
    )
    normalized_action = _required_contract_value(
        action_code,
        field_name="action_code",
        max_length=80,
    )
    normalized_resource = _required_contract_value(
        resource,
        field_name="resource",
        max_length=100,
    )
    normalized_legacy_action = (
        _required_contract_value(
            legacy_action,
            field_name="legacy_action",
            max_length=30,
        )
        if legacy_action is not None
        else normalized_action[:30]
    )
    resolved_request_id, ip_address, user_agent = _request_context(
        request,
        request_id,
    )

    actor_id = int(actor.id) if actor is not None and actor.id is not None else None
    actor_username = (
        _optional_text(actor.username, 50) if actor is not None else None
    )
    resolved_operator_name = _optional_text(operator_name, 100)
    if resolved_operator_name is None and actor is not None:
        resolved_operator_name = _optional_text(
            actor.display_name or actor.real_name or actor.username,
            100,
        )

    row = OperationLog(
        user_id=actor_id,
        action=normalized_legacy_action,
        resource=normalized_resource,
        details=serialize_audit_details(details),
        ip_address=ip_address,
        username=actor_username or resolved_operator_name,
        role=_optional_text(actor.role, 30) if actor is not None else None,
        entity_type=_optional_text(entity_type, 100),
        entity_id=entity_id,
        description=_optional_text(description, MAX_AUDIT_TEXT_CHARS),
        user_agent=user_agent,
        event_category=normalized_category,
        result=normalized_result,
        source=normalized_source,
        module_code=normalized_module,
        action_code=normalized_action,
        actor_user_id_snapshot=actor_id,
        operator_name_snapshot=resolved_operator_name or actor_username,
        object_ref=_optional_text(object_ref, 200),
        customer_id_snapshot=customer_id,
        customer_name_snapshot=_optional_text(customer_name, 200),
        request_id=resolved_request_id,
        batch_id=_optional_text(batch_id, 64),
        schema_version=AUDIT_SCHEMA_VERSION,
    )
    db.add(row)
    db.flush()
    return row
