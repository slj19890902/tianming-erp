from __future__ import annotations

from dataclasses import dataclass
import re

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.core.time_contract import utc_now_naive
from app.models.mold_tool import MoldLocationMovement, MoldTool


_CANONICAL_FLAT_PATTERN = re.compile(
    r"^(?P<floor>\d+F)-M-R(?P<rack>\d+)-L(?P<level>[1-3])-D(?P<row>\d+)-P(?P<position>\d+)$",
    re.IGNORECASE,
)
_CANONICAL_VERTICAL_PATTERN = re.compile(
    r"^(?P<floor>\d+F)-M-R(?P<rack>\d+)-L(?P<level>[1-3])-V-P(?P<position>\d+)$",
    re.IGNORECASE,
)
_SHORT_FLAT_PATTERN = re.compile(
    r"^(?P<area>[A-Z0-9\u4e00-\u9fff]+)-H(?P<rack>\d+)-L(?P<level>[1-3])-R(?P<row>\d+)-P(?P<position>\d+)$",
    re.IGNORECASE,
)
_SHORT_VERTICAL_PATTERN = re.compile(
    r"^(?P<area>[A-Z0-9\u4e00-\u9fff]+)-H(?P<rack>\d+)-L(?P<level>[1-3])-V(?P<position>\d+)$",
    re.IGNORECASE,
)

MOLD_LOCATION_SOURCES = frozenset(
    {"manual_input", "scanner_paste", "url_parameter", "api"}
)


class MoldLocationError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class MoldLocationPreview:
    mold: MoldTool
    target_location: str
    target_guide: dict
    same_location: bool
    occupant: MoldTool | None


@dataclass(frozen=True)
class MoldLocationMoveResult:
    mold: MoldTool
    movement: MoldLocationMovement | None
    replayed: bool
    no_change: bool


def _number(value: str) -> int:
    return int(value.lstrip("0") or "0")


def _area_text(value: str) -> str:
    area = value.upper()
    return area if area.endswith("区") else f"{area}区"


def _floor_text(value: str) -> str:
    floor = _number(value[:-1])
    chinese = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六"}
    return f"{chinese.get(floor, floor)}楼模具区"


def describe_mold_location(value: str) -> dict:
    """Turn a stable rack code into a prompt a workshop worker can follow."""

    raw = (value or "").strip()
    normalized = raw.upper()
    flat = _CANONICAL_FLAT_PATTERN.fullmatch(normalized)
    if flat:
        parts = flat.groupdict()
        prompt = (
            f"前往{_floor_text(parts['floor'])}，第{_number(parts['rack'])}号货架，"
            f"第{_number(parts['level'])}层、第{_number(parts['row'])}排，"
            f"从左到右第{_number(parts['position'])}块。"
            "拿取前请核对模具编号和存货编码。"
        )
        return {
            "kind": "flat",
            "location_code": normalized,
            "floor": parts["floor"].upper(),
            "area": "M",
            "rack": _number(parts["rack"]),
            "level": _number(parts["level"]),
            "row": _number(parts["row"]),
            "position": _number(parts["position"]),
            "prompt": prompt,
        }
    vertical = _CANONICAL_VERTICAL_PATTERN.fullmatch(normalized)
    if vertical:
        parts = vertical.groupdict()
        level_text = "底层（第1层）" if _number(parts["level"]) == 1 else f"第{_number(parts['level'])}层"
        prompt = (
            f"前往{_floor_text(parts['floor'])}，第{_number(parts['rack'])}号货架，"
            f"{level_text}竖放区，从左到右第{_number(parts['position'])}块。"
            "大模具较重，请按现场要求两人搬运；拿取前核对模具编号和存货编码。"
        )
        return {
            "kind": "vertical",
            "location_code": normalized,
            "floor": parts["floor"].upper(),
            "area": "M",
            "rack": _number(parts["rack"]),
            "level": _number(parts["level"]),
            "row": None,
            "position": _number(parts["position"]),
            "prompt": prompt,
        }
    flat = _SHORT_FLAT_PATTERN.fullmatch(normalized)
    if flat:
        parts = flat.groupdict()
        return {
            "kind": "flat_legacy",
            "location_code": normalized,
            "floor": None,
            "area": parts["area"].upper(),
            "rack": _number(parts["rack"]),
            "level": _number(parts["level"]),
            "row": _number(parts["row"]),
            "position": _number(parts["position"]),
            "prompt": (
                f"前往{_area_text(parts['area'])}第{_number(parts['rack'])}号模具架，"
                f"第{_number(parts['level'])}层、第{_number(parts['row'])}排，"
                f"从左到右第{_number(parts['position'])}块。"
                "该位置使用旧简写，建议现场复核后改为 3F-M 标准位置码；拿取前核对模具编号和存货编码。"
            ),
        }
    vertical = _SHORT_VERTICAL_PATTERN.fullmatch(normalized)
    if vertical:
        parts = vertical.groupdict()
        return {
            "kind": "vertical_legacy",
            "location_code": normalized,
            "floor": None,
            "area": parts["area"].upper(),
            "rack": _number(parts["rack"]),
            "level": _number(parts["level"]),
            "row": None,
            "position": _number(parts["position"]),
            "prompt": (
                f"前往{_area_text(parts['area'])}第{_number(parts['rack'])}号模具架，"
                f"第{_number(parts['level'])}层竖放区，从左到右第{_number(parts['position'])}块。"
                "该位置使用旧简写，建议现场复核后改为 3F-M 标准位置码；大模具请两人搬运。"
            ),
        }
    return {
        "kind": "manual",
        "location_code": raw,
        "floor": None,
        "area": None,
        "rack": None,
        "level": None,
        "row": None,
        "position": None,
        "prompt": f"请前往“{raw or '未登记位置'}”查找，拿取前核对模具编号和存货编码。",
    }


def normalize_mold_location_code(value: str) -> str:
    """Accept only canonical, physically addressable 3F-M location codes."""

    guide = describe_mold_location(value)
    if guide["kind"] not in {"flat", "vertical"} or guide["floor"] != "3F":
        raise MoldLocationError(
            "目标位置必须是合法的 3F-M 平放或竖放位置码，旧自由文本不能用于移动确认",
            status_code=422,
        )
    numeric_fields = ("rack", "level", "position")
    if guide["kind"] == "flat":
        numeric_fields += ("row",)
    if any(int(guide[field] or 0) <= 0 for field in numeric_fields):
        raise MoldLocationError(
            "3F-M 位置码中的货架、层、排和位置编号必须大于 0",
            status_code=422,
        )
    return str(guide["location_code"])


def _normalized_text(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _mold_by_code(db: Session, mold_code: str) -> MoldTool:
    code = (mold_code or "").strip()
    if not code:
        raise MoldLocationError("请输入或扫描模具码", status_code=422)
    rows = db.scalars(
        select(MoldTool).where(
            func.upper(func.trim(MoldTool.mold_code)) == code.upper()
        )
    ).all()
    if not rows:
        raise MoldLocationError("未找到该模具码", status_code=404)
    if len(rows) != 1:
        raise MoldLocationError("模具码存在大小写重复，必须先由管理员清理", status_code=409)
    row = rows[0]
    if not row.is_active:
        raise MoldLocationError("该模具已停用，不能确认移动", status_code=409)
    return row


def _location_occupant(
    db: Session,
    *,
    target_location: str,
    exclude_mold_id: int,
) -> MoldTool | None:
    return db.scalar(
        select(MoldTool).where(
            MoldTool.id != exclude_mold_id,
            MoldTool.is_active.is_(True),
            func.upper(func.trim(MoldTool.rack_location)) == target_location,
        )
    )


def preview_mold_location_move(
    db: Session,
    *,
    mold_code: str,
    target_location: str,
) -> MoldLocationPreview:
    mold = _mold_by_code(db, mold_code)
    target = normalize_mold_location_code(target_location)
    return MoldLocationPreview(
        mold=mold,
        target_location=target,
        target_guide=describe_mold_location(target),
        same_location=mold.rack_location.strip().upper() == target,
        occupant=_location_occupant(
            db,
            target_location=target,
            exclude_mold_id=mold.id,
        ),
    )


def _movement_by_key(
    db: Session,
    idempotency_key: str,
) -> MoldLocationMovement | None:
    return db.scalar(
        select(MoldLocationMovement).where(
            MoldLocationMovement.idempotency_key == idempotency_key
        )
    )


def _idempotent_result(
    db: Session,
    movement: MoldLocationMovement,
    *,
    mold_code: str,
    target_location: str,
    expected_version: int,
    source: str,
    note: str | None,
) -> MoldLocationMoveResult:
    if (
        movement.mold_code_snapshot.upper() != mold_code.strip().upper()
        or movement.to_location != target_location
        or movement.expected_version != expected_version
        or movement.source != source
        or _normalized_text(movement.note) != _normalized_text(note)
    ):
        raise MoldLocationError("幂等键已用于不同的模具移动业务", status_code=409)
    mold = db.get(MoldTool, movement.mold_tool_id)
    if mold is None:
        raise MoldLocationError("幂等移动对应的模具不存在", status_code=409)
    return MoldLocationMoveResult(
        mold=mold,
        movement=movement,
        replayed=True,
        no_change=False,
    )


def confirm_mold_location_move(
    db: Session,
    *,
    mold_code: str,
    target_location: str,
    expected_version: int,
    idempotency_key: str,
    actor_id: int | None,
    source: str,
    note: str | None,
) -> MoldLocationMoveResult:
    target = normalize_mold_location_code(target_location)
    key = idempotency_key.strip()
    clean_source = source.strip()
    clean_note = _normalized_text(note)
    if len(key) < 8:
        raise MoldLocationError(
            "幂等键去除首尾空白后至少需要 8 个字符",
            status_code=422,
        )
    if clean_source not in MOLD_LOCATION_SOURCES:
        raise MoldLocationError("未知的模具移动来源", status_code=422)

    existing = _movement_by_key(db, key)
    if existing is not None:
        return _idempotent_result(
            db,
            existing,
            mold_code=mold_code,
            target_location=target,
            expected_version=expected_version,
            source=clean_source,
            note=clean_note,
        )

    mold = _mold_by_code(db, mold_code)
    if mold.location_version != expected_version:
        raise MoldLocationError(
            f"模具位置版本已变化（当前版本 {mold.location_version}），请重新预览",
            status_code=409,
        )
    if mold.rack_location.strip().upper() == target:
        return MoldLocationMoveResult(
            mold=mold,
            movement=None,
            replayed=False,
            no_change=True,
        )
    occupant = _location_occupant(
        db,
        target_location=target,
        exclude_mold_id=mold.id,
    )
    if occupant is not None:
        raise MoldLocationError(
            f"目标位置已被启用模具 {occupant.mold_code} 占用",
            status_code=409,
        )

    other_mold = aliased(MoldTool)
    target_is_occupied = (
        select(other_mold.id)
        .where(
            other_mold.id != mold.id,
            other_mold.is_active.is_(True),
            func.upper(func.trim(other_mold.rack_location)) == target,
        )
        .exists()
    )
    moved_at = utc_now_naive()
    movement: MoldLocationMovement | None = None
    try:
        with db.begin_nested():
            claimed = db.execute(
                update(MoldTool)
                .where(
                    MoldTool.id == mold.id,
                    MoldTool.is_active.is_(True),
                    MoldTool.location_version == expected_version,
                    ~target_is_occupied,
                )
                .values(
                    rack_location=target,
                    location_version=expected_version + 1,
                    last_location_confirmed_at=moved_at,
                    last_location_confirmed_by=actor_id,
                    updated_by=actor_id,
                )
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                raise MoldLocationError(
                    "模具位置版本已变化或目标位置已被占用，请重新预览",
                    status_code=409,
                )
            movement = MoldLocationMovement(
                mold_tool_id=mold.id,
                mold_code_snapshot=mold.mold_code,
                from_location=mold.rack_location,
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
        existing = _movement_by_key(db, key)
        if existing is not None:
            return _idempotent_result(
                db,
                existing,
                mold_code=mold_code,
                target_location=target,
                expected_version=expected_version,
                source=clean_source,
                note=clean_note,
            )
        raise

    db.expire(mold)
    db.refresh(mold)
    return MoldLocationMoveResult(
        mold=mold,
        movement=movement,
        replayed=False,
        no_change=False,
    )
