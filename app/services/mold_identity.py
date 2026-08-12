from __future__ import annotations

from dataclasses import dataclass
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.mold_tool import MoldTool


_MOLD_NAME_PATTERN = re.compile(
    r"^(?P<customer>.*?)[\s:：_-]*(?P<inventory>[A-Za-z0-9][A-Za-z0-9._/+\-]*)$"
)
_INITIALS_PATTERN = re.compile(r"^[A-Z0-9]{1,20}$")
_LEADING_CHINESE_LABEL_PATTERN = re.compile(
    r"^(?P<label>[\u3400-\u9fff]{2,8})[\s:：_-]*(?=[A-Za-z0-9])"
)


class MoldIdentityError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MoldIdentityParts:
    customer_label: str
    customer_initials: str
    inventory_code: str
    base_code: str


def mold_customer_short_name(
    mold_name: str | None,
    customer_name: str | None,
    customer_code: str | None,
) -> str | None:
    """Return the maintained Chinese label embedded in a formal mold name.

    New mold names are entered as ``customer Chinese short name + inventory
    code``.  Reuse that explicit operator-maintained text for the physical
    label, but never guess a short name from a mold code or truncate a legal
    customer name.  Older molds without that convention fall back to the full
    customer name and finally the customer code.
    """

    name = str(mold_name or "").strip()
    normalized_customer_name = str(customer_name or "").strip()
    match = _LEADING_CHINESE_LABEL_PATTERN.match(name)
    if match is not None and normalized_customer_name:
        label = match.group("label")
        if label in normalized_customer_name:
            return label
    for value in (normalized_customer_name, customer_code):
        normalized = str(value or "").strip()
        if normalized:
            return normalized
    return None


def parse_mold_identity(
    mold_name: str,
    customer_initials: str,
) -> MoldIdentityParts:
    name = str(mold_name or "").strip()
    match = _MOLD_NAME_PATTERN.fullmatch(name)
    if match is None or not match.group("customer").strip():
        raise MoldIdentityError(
            "模具名称请按“客户中文简写 + 存货编码”填写，例如：仪元 Z.001.000093"
        )

    initials = re.sub(r"[^A-Za-z0-9]", "", str(customer_initials or "")).upper()
    if not _INITIALS_PATTERN.fullmatch(initials):
        raise MoldIdentityError("客户中文简写无法生成拼音缩写，请检查模具名称后重试")

    customer_label = match.group("customer").strip(" -_:：")
    inventory_code = match.group("inventory").upper()
    if not customer_label or not inventory_code:
        raise MoldIdentityError(
            "模具名称请按“客户中文简写 + 存货编码”填写，例如：仪元 Z.001.000093"
        )

    # Reserve room for a stable collision suffix such as -9999 while keeping the
    # persisted mold code inside the existing VARCHAR(100) contract.
    base_code = f"{initials}-{inventory_code}"[:95].rstrip("-._/")
    if not base_code:
        raise MoldIdentityError("模具编号无法生成，请检查客户简写和存货编码")
    return MoldIdentityParts(
        customer_label=customer_label,
        customer_initials=initials,
        inventory_code=inventory_code,
        base_code=base_code,
    )


def next_available_mold_code(
    db: Session,
    *,
    mold_name: str,
    customer_initials: str,
) -> tuple[str, MoldIdentityParts]:
    parts = parse_mold_identity(mold_name, customer_initials)
    existing = {
        str(code or "").strip().upper()
        for code in db.scalars(select(MoldTool.mold_code)).all()
    }
    if parts.base_code not in existing:
        return parts.base_code, parts
    suffix = 2
    while suffix <= 9999:
        candidate = f"{parts.base_code}-{suffix:02d}"
        if candidate not in existing:
            return candidate, parts
        suffix += 1
    raise MoldIdentityError("同一客户简写和存货编码的模具编号过多，请联系管理员处理")
