from __future__ import annotations

from dataclasses import dataclass
import re

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.core.time_contract import utc_now_naive
from app.models.printing_plate import PrintingPlate, PrintingPlateLocationMovement


PLATE_LOCATION_SOURCES = frozenset(
    {"manual_input", "scanner_paste", "url_parameter", "api"}
)
_PLATE_LOCATION_PATTERN = re.compile(
    r"^1F-PL-R0?1-L(?P<level>[12])-P(?P<position>\d+)$",
    re.IGNORECASE,
)


class PrintingPlateLocationError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class PrintingPlateLocationPreview:
    plate: PrintingPlate
    target_location: str
    target_guide: dict
    same_location: bool
    occupant: PrintingPlate | None


@dataclass(frozen=True)
class PrintingPlateMoveResult:
    plate: PrintingPlate
    movement: PrintingPlateLocationMovement | None
    replayed: bool
    no_change: bool


def normalize_printing_plate_location(value: str) -> str:
    match = _PLATE_LOCATION_PATTERN.fullmatch((value or "").strip())
    if match is None:
        raise PrintingPlateLocationError(
            "挂板位置必须使用 1F-PL-R01-L1-P01 或 1F-PL-R01-L2-P01 格式",
            status_code=422,
        )
    position = int(match.group("position"))
    if position <= 0:
        raise PrintingPlateLocationError("挂板位置编号必须大于 0", status_code=422)
    return f"1F-PL-R01-L{int(match.group('level'))}-P{position:02d}"


def describe_printing_plate_location(value: str) -> dict:
    try:
        normalized = normalize_printing_plate_location(value)
    except PrintingPlateLocationError:
        return {
            "kind": "manual",
            "location_code": (value or "").strip(),
            "prompt": f"请到挂板区核对“{(value or '').strip() or '未登记位置'}”。",
        }
    match = _PLATE_LOCATION_PATTERN.fullmatch(normalized)
    assert match is not None
    level = int(match.group("level"))
    position = int(match.group("position"))
    return {
        "kind": "plate_rack",
        "location_code": normalized,
        "floor": "1F",
        "rack": "R01",
        "level": level,
        "position": position,
        "prompt": (
            f"前往一楼挂板区 R01，第{level}层，从左到右第{position}格；"
            "拿取前核对挂板编号和颜色。"
        ),
    }


def _plate_by_code(db: Session, plate_code: str) -> PrintingPlate:
    code = (plate_code or "").strip()
    if not code:
        raise PrintingPlateLocationError("请输入或扫描挂板编号", status_code=422)
    plate = db.scalar(
        select(PrintingPlate).where(
            func.upper(func.trim(PrintingPlate.plate_code)) == code.upper()
        )
    )
    if plate is None:
        raise PrintingPlateLocationError("未找到该挂板编号", status_code=404)
    if plate.status != "active":
        raise PrintingPlateLocationError("该挂板不是启用状态，不能确认移动", status_code=409)
    return plate


def _occupant(
    db: Session, *, target_location: str, exclude_plate_id: int
) -> PrintingPlate | None:
    return db.scalar(
        select(PrintingPlate).where(
            PrintingPlate.id != exclude_plate_id,
            PrintingPlate.status.in_(("active", "damaged")),
            func.upper(func.trim(PrintingPlate.rack_location)) == target_location,
        )
    )


def preview_printing_plate_move(
    db: Session, *, plate_code: str, target_location: str
) -> PrintingPlateLocationPreview:
    plate = _plate_by_code(db, plate_code)
    target = normalize_printing_plate_location(target_location)
    return PrintingPlateLocationPreview(
        plate=plate,
        target_location=target,
        target_guide=describe_printing_plate_location(target),
        same_location=plate.rack_location.strip().upper() == target,
        occupant=_occupant(
            db, target_location=target, exclude_plate_id=plate.id
        ),
    )


def _clean_note(value: str | None) -> str | None:
    return (value or "").strip() or None


def _idempotent_result(
    db: Session,
    movement: PrintingPlateLocationMovement,
    *,
    plate_code: str,
    target_location: str,
    expected_version: int,
    source: str,
    note: str | None,
) -> PrintingPlateMoveResult:
    if (
        movement.plate_code_snapshot.upper() != plate_code.strip().upper()
        or movement.to_location != target_location
        or movement.expected_version != expected_version
        or movement.source != source
        or _clean_note(movement.note) != _clean_note(note)
    ):
        raise PrintingPlateLocationError("幂等键已用于不同的挂板移动", status_code=409)
    plate = db.get(PrintingPlate, movement.printing_plate_id)
    if plate is None:
        raise PrintingPlateLocationError("幂等移动对应的挂板不存在", status_code=409)
    return PrintingPlateMoveResult(plate, movement, True, False)


def confirm_printing_plate_move(
    db: Session,
    *,
    plate_code: str,
    target_location: str,
    expected_version: int,
    idempotency_key: str,
    actor_id: int | None,
    source: str,
    note: str | None,
) -> PrintingPlateMoveResult:
    target = normalize_printing_plate_location(target_location)
    key = idempotency_key.strip()
    clean_source = source.strip()
    clean_note = _clean_note(note)
    if len(key) < 8:
        raise PrintingPlateLocationError("幂等键至少需要 8 个字符", status_code=422)
    if clean_source not in PLATE_LOCATION_SOURCES:
        raise PrintingPlateLocationError("未知的挂板移动来源", status_code=422)
    existing = db.scalar(
        select(PrintingPlateLocationMovement).where(
            PrintingPlateLocationMovement.idempotency_key == key
        )
    )
    if existing is not None:
        return _idempotent_result(
            db,
            existing,
            plate_code=plate_code,
            target_location=target,
            expected_version=expected_version,
            source=clean_source,
            note=clean_note,
        )
    plate = _plate_by_code(db, plate_code)
    if plate.location_version != expected_version:
        raise PrintingPlateLocationError(
            f"挂板位置版本已变化（当前版本 {plate.location_version}），请重新预览",
            status_code=409,
        )
    if plate.rack_location.strip().upper() == target:
        return PrintingPlateMoveResult(plate, None, False, True)
    if _occupant(db, target_location=target, exclude_plate_id=plate.id) is not None:
        raise PrintingPlateLocationError("目标格位已被其他挂板占用", status_code=409)

    other = aliased(PrintingPlate)
    occupied = (
        select(other.id)
        .where(
            other.id != plate.id,
            other.status.in_(("active", "damaged")),
            func.upper(func.trim(other.rack_location)) == target,
        )
        .exists()
    )
    moved_at = utc_now_naive()
    movement: PrintingPlateLocationMovement | None = None
    try:
        with db.begin_nested():
            claimed = db.execute(
                update(PrintingPlate)
                .where(
                    PrintingPlate.id == plate.id,
                    PrintingPlate.status == "active",
                    PrintingPlate.location_version == expected_version,
                    ~occupied,
                )
                .values(
                    rack_location=target,
                    location_version=expected_version + 1,
                    version=PrintingPlate.version + 1,
                    last_location_confirmed_at=moved_at,
                    last_location_confirmed_by=actor_id,
                    updated_by=actor_id,
                )
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                raise PrintingPlateLocationError(
                    "挂板位置版本已变化或目标格位已占用，请重新预览",
                    status_code=409,
                )
            movement = PrintingPlateLocationMovement(
                printing_plate_id=plate.id,
                plate_code_snapshot=plate.plate_code,
                from_location=plate.rack_location,
                to_location=target,
                actor_id=actor_id,
                moved_at=moved_at,
                idempotency_key=key,
                expected_version=expected_version,
                resulting_version=expected_version + 1,
                source=clean_source,
                note=clean_note,
            )
            db.add(movement)
            db.flush()
    except IntegrityError:
        existing = db.scalar(
            select(PrintingPlateLocationMovement).where(
                PrintingPlateLocationMovement.idempotency_key == key
            )
        )
        if existing is not None:
            return _idempotent_result(
                db,
                existing,
                plate_code=plate_code,
                target_location=target,
                expected_version=expected_version,
                source=clean_source,
                note=clean_note,
            )
        raise
    db.expire(plate)
    db.refresh(plate)
    return PrintingPlateMoveResult(plate, movement, False, False)
