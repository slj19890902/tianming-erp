from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.time_contract import utc_now_naive
from app.models.customer import Customer
from app.models.printing_plate import PrintingPlate, PrintingPlateResinReuse
from app.models.product import Product
from app.services.printing_plate_location import (
    PrintingPlateLocationError,
    normalize_printing_plate_location,
)


class PrintingPlateResinReuseError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class PrintingPlateResinReusePreview:
    plate: PrintingPlate
    target_customer: Customer
    binding_count: int
    eligible: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class PrintingPlateResinReuseResult:
    plate: PrintingPlate
    reuse: PrintingPlateResinReuse
    replayed: bool


def printing_plate_binding_filter(plate_id: int):
    return or_(
        Product.printing_plate_1_id == plate_id,
        Product.printing_plate_2_id == plate_id,
        Product.printing_plate_3_id == plate_id,
    )


def printing_plate_binding_count(db: Session, plate_id: int) -> int:
    return int(
        db.scalar(
            select(func.count(Product.id)).where(
                printing_plate_binding_filter(plate_id)
            )
        )
        or 0
    )


def _is_second_plate_rack_level(rack_location: str) -> bool:
    try:
        normalized = normalize_printing_plate_location(rack_location)
    except PrintingPlateLocationError:
        return False
    return "-L2-" in normalized


def preview_printing_plate_resin_reuse(
    db: Session,
    *,
    plate: PrintingPlate,
    target_customer: Customer,
) -> PrintingPlateResinReusePreview:
    binding_count = printing_plate_binding_count(db, plate.id)
    blockers: list[str] = []
    if plate.status != "active":
        blockers.append("只有启用且未报损的实体挂板才能换版复用")
    if not _is_second_plate_rack_level(plate.rack_location):
        blockers.append("请先把实体挂板移动到挂板区货架第 2 层并确认位置")
    if binding_count:
        blockers.append(
            f"该实体挂板仍有 {binding_count} 个常用箱绑定，请先在产品资料中逐一解除"
        )
    return PrintingPlateResinReusePreview(
        plate=plate,
        target_customer=target_customer,
        binding_count=binding_count,
        eligible=not blockers,
        blockers=tuple(blockers),
    )


def _clean_required(value: str, label: str, max_length: int) -> str:
    text = (value or "").strip()
    if not text:
        raise PrintingPlateResinReuseError(f"{label}不能为空", status_code=422)
    if len(text) > max_length:
        raise PrintingPlateResinReuseError(
            f"{label}不能超过 {max_length} 个字符", status_code=422
        )
    return text


def _same_reuse_payload(
    reuse: PrintingPlateResinReuse,
    *,
    plate_id: int,
    target_customer: Customer,
    target_plate_name: str,
    target_color_name: str,
    expected_version: int,
) -> bool:
    return (
        reuse.printing_plate_id == plate_id
        and reuse.to_customer_id == target_customer.id
        and reuse.to_customer_name_snapshot == target_customer.name
        and reuse.to_plate_name_snapshot == target_plate_name
        and reuse.to_color_name_snapshot == target_color_name
        and reuse.expected_version == expected_version
        and reuse.old_resin_removed
        and reuse.new_resin_mounted
    )


def confirm_printing_plate_resin_reuse(
    db: Session,
    *,
    plate: PrintingPlate,
    target_customer: Customer,
    target_plate_name: str,
    target_color_name: str,
    expected_version: int,
    idempotency_key: str,
    actor_id: int | None,
    actor_username: str,
    old_resin_removed: bool,
    new_resin_mounted: bool,
) -> PrintingPlateResinReuseResult:
    key = _clean_required(idempotency_key, "幂等键", 120)
    if len(key) < 8:
        raise PrintingPlateResinReuseError(
            "幂等键去除首尾空白后至少需要 8 个字符", status_code=422
        )
    new_name = _clean_required(target_plate_name, "新挂板名称", 200)
    new_color = _clean_required(target_color_name, "新印刷颜色", 100)
    username = _clean_required(actor_username, "操作员账号", 100)
    if not old_resin_removed or not new_resin_mounted:
        raise PrintingPlateResinReuseError(
            "必须现场确认旧树脂版已撕除且新树脂版已贴好", status_code=422
        )

    existing = db.scalar(
        select(PrintingPlateResinReuse).where(
            PrintingPlateResinReuse.idempotency_key == key
        )
    )
    if existing is not None:
        if not _same_reuse_payload(
            existing,
            plate_id=plate.id,
            target_customer=target_customer,
            target_plate_name=new_name,
            target_color_name=new_color,
            expected_version=expected_version,
        ):
            raise PrintingPlateResinReuseError(
                "幂等键已用于不同的挂板换版复用", status_code=409
            )
        replayed_plate = db.get(PrintingPlate, existing.printing_plate_id)
        if replayed_plate is None:
            raise PrintingPlateResinReuseError(
                "幂等换版对应的实体挂板不存在", status_code=409
            )
        return PrintingPlateResinReuseResult(replayed_plate, existing, True)

    preview = preview_printing_plate_resin_reuse(
        db,
        plate=plate,
        target_customer=target_customer,
    )
    if not preview.eligible:
        raise PrintingPlateResinReuseError("；".join(preview.blockers), status_code=409)
    if plate.version != expected_version:
        raise PrintingPlateResinReuseError(
            f"挂板资料版本已变化（当前版本 {plate.version}），请重新预览",
            status_code=409,
        )

    reused_at = utc_now_naive()
    reuse = PrintingPlateResinReuse(
        printing_plate_id=plate.id,
        plate_code_snapshot=plate.plate_code,
        from_customer_id=plate.customer_id,
        from_customer_name_snapshot=plate.customer.name if plate.customer else "",
        from_plate_name_snapshot=plate.plate_name,
        from_color_name_snapshot=plate.color_name,
        to_customer_id=target_customer.id,
        to_customer_name_snapshot=target_customer.name,
        to_plate_name_snapshot=new_name,
        to_color_name_snapshot=new_color,
        rack_location_snapshot=plate.rack_location,
        actor_id=actor_id,
        actor_username_snapshot=username,
        reused_at=reused_at,
        idempotency_key=key,
        expected_version=expected_version,
        resulting_version=expected_version + 1,
        old_resin_removed=True,
        new_resin_mounted=True,
    )
    try:
        with db.begin_nested():
            no_product_binding = ~select(Product.id).where(
                printing_plate_binding_filter(plate.id)
            ).exists()
            claimed = db.execute(
                update(PrintingPlate)
                .where(
                    PrintingPlate.id == plate.id,
                    PrintingPlate.version == expected_version,
                    PrintingPlate.status == "active",
                    PrintingPlate.rack_location == plate.rack_location,
                    no_product_binding,
                )
                .values(
                    customer_id=target_customer.id,
                    plate_name=new_name,
                    color_name=new_color,
                    version=expected_version + 1,
                    updated_by=actor_id,
                )
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                raise PrintingPlateResinReuseError(
                    "挂板资料、位置、状态或绑定已变化，请重新预览",
                    status_code=409,
                )
            db.add(reuse)
            db.flush()
    except IntegrityError:
        existing = db.scalar(
            select(PrintingPlateResinReuse).where(
                PrintingPlateResinReuse.idempotency_key == key
            )
        )
        if existing is not None and _same_reuse_payload(
            existing,
            plate_id=plate.id,
            target_customer=target_customer,
            target_plate_name=new_name,
            target_color_name=new_color,
            expected_version=expected_version,
        ):
            replayed_plate = db.get(PrintingPlate, existing.printing_plate_id)
            if replayed_plate is not None:
                return PrintingPlateResinReuseResult(
                    replayed_plate, existing, True
                )
        raise
    db.expire(plate)
    db.refresh(plate)
    return PrintingPlateResinReuseResult(plate, reuse, False)
