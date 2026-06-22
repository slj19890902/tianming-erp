from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.models.audit import OperationLog
from app.models.user import User


def audit_master_change(
    db: Session,
    *,
    user: User,
    action: str,
    resource: str,
    resource_id: int,
    details: dict[str, Any],
) -> None:
    db.add(
        OperationLog(
            user_id=user.id,
            action=action,
            resource=resource,
            details=json.dumps(details, ensure_ascii=False, default=str),
            username=user.username,
            role=user.role,
            entity_type=resource.lower(),
            entity_id=resource_id,
            description=f"{action} {resource}",
        )
    )


def clean_code(value: str) -> str:
    return value.strip()
