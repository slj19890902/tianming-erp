from __future__ import annotations

from dataclasses import dataclass
import re
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.mold_tool import MoldTool


_MOLD_NAME_PATTERN = re.compile(
    r"^(?P<customer>.*?)[\s:：_#＃-]*(?P<inventory>[A-Za-z0-9][A-Za-z0-9._/+\-]*)$"
)
_INITIALS_PATTERN = re.compile(r"^[A-Z0-9]{1,20}$")
_LEADING_CHINESE_LABEL_PATTERN = re.compile(
    r"^(?P<label>[\u3400-\u9fff]{2,8})[\s:：_#＃-]*(?=[A-Za-z0-9])"
)
_MOLD_LABEL_ALLOWED_PATTERN = re.compile(
    r"^[\u3400-\u9fffA-Za-z0-9\s*×/._\-()（）]+$"
)
_MOLD_SHORT_NAME_ALLOWED_PATTERN = re.compile(
    r"^[\u3400-\u9fff\u0370-\u03ffA-Za-z0-9\s*×/._\-()（）,，]+$"
)


class MoldIdentityError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MoldIdentityParts:
    customer_label: str
    customer_initials: str
    inventory_code: str
    base_code: str


def normalize_mold_label_name(value: str | None) -> str:
    label = " ".join(str(value or "").strip().split())
    if not label:
        raise MoldIdentityError("请填写模具标签名称")
    if len(label) > 200:
        raise MoldIdentityError("模具标签名称不能超过 200 个字符")
    if _MOLD_LABEL_ALLOWED_PATTERN.fullmatch(label) is None:
        raise MoldIdentityError("模具标签名称包含不支持的字符")
    return label


def normalize_mold_chinese_short_name(value: str | None) -> str | None:
    short_name = " ".join(str(value or "").strip().split())
    if not short_name:
        return None
    if len(short_name) > 100:
        raise MoldIdentityError("模具中文简写不能超过 100 个字符")
    # Customer model names contain Greek series letters and comma-separated
    # variants. Preserve those names without relaxing the inventory-code label.
    if _MOLD_SHORT_NAME_ALLOWED_PATTERN.fullmatch(short_name) is None:
        raise MoldIdentityError("模具中文简写包含不支持的字符")
    return short_name


def compose_mold_display_name(
    customer_short_names: list[str] | tuple[str, ...],
    label_name: str,
    chinese_short_name: str | None = None,
) -> str:
    customers = [str(value or "").strip() for value in customer_short_names]
    customers = [value for value in customers if value]
    if not customers:
        raise MoldIdentityError("请至少选择一个主显示客户")
    if len(customers) > 2:
        raise MoldIdentityError("主标签最多显示两个客户简称")
    normalized_label = normalize_mold_label_name(label_name)
    normalized_short = normalize_mold_chinese_short_name(chinese_short_name)
    parts = ["/".join(customers), normalized_label]
    if normalized_short:
        parts.append(normalized_short)
    return " ".join(parts)


def next_available_internal_mold_code(db: Session) -> str:
    """Allocate an opaque stable code without deriving identity from Chinese text."""

    for _attempt in range(100):
        candidate = f"M-{uuid4().hex[:12].upper()}"
        if db.scalar(select(MoldTool.id).where(MoldTool.mold_code == candidate)) is None:
            return candidate
    raise MoldIdentityError("模具内部编号生成失败，请重试")


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


def mold_label_display_number(
    mold_name: str | None,
    mold_code: str | None,
    customer_name: str | None,
    customer_code: str | None,
) -> str:
    """Return the operator-maintained mold number used on a compact label.

    The formal mold name already follows ``customer Chinese label + inventory
    code`` for newly maintained molds.  Prefer that explicit identity because
    legacy mold-code prefixes are not always identical to the customer code
    (for example ``JCD-61452621`` belongs to customer code ``JSD``).  Only fall
    back to stripping a mold-code prefix when it is proven to equal the current
    customer code; never split an arbitrary code at the first hyphen.
    """

    name = str(mold_name or "").strip()
    short_name = mold_customer_short_name(
        name,
        customer_name,
        customer_code,
    )
    match = _MOLD_NAME_PATTERN.fullmatch(name)
    if match is not None and short_name:
        maintained_customer = match.group("customer").strip(" -_:：#＃")
        inventory_code = match.group("inventory").strip()
        if maintained_customer == short_name and inventory_code:
            return inventory_code

    code = str(mold_code or "").strip()
    normalized_customer_code = str(customer_code or "").strip()
    prefix = f"{normalized_customer_code}-"
    if (
        code
        and normalized_customer_code
        and code.upper().startswith(prefix.upper())
        and len(code) > len(prefix)
    ):
        return code[len(prefix) :]
    return code or "待完善"


def mold_label_display_identity(
    mold_name: str | None,
    mold_code: str | None,
    customer_name: str | None,
    customer_code: str | None,
) -> str:
    """Return ``customer label + mold number`` without guessing either part."""

    customer = mold_customer_short_name(
        mold_name,
        customer_name,
        customer_code,
    ) or "待完善"
    number = mold_label_display_number(
        mold_name,
        mold_code,
        customer_name,
        customer_code,
    )
    return f"{customer}{number}"


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
