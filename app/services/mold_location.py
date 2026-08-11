from __future__ import annotations

from dataclasses import dataclass
import re

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.time_contract import utc_now_naive
from app.models.mold_tool import MoldLocationMovement, MoldTool
from app.services.warehouse_twin_layout import load_warehouse_twin_floor


_CANONICAL_FLAT_PATTERN = re.compile(
    r"^(?P<floor>\d+F)-M-R(?P<rack>\d+)-L(?P<level>[1-3])-D(?P<row>\d+)-P(?P<position>\d+)$",
    re.IGNORECASE,
)
_CANONICAL_VERTICAL_PATTERN = re.compile(
    r"^(?P<floor>\d+F)-M-R(?P<rack>\d+)-L(?P<level>[1-3])-V-P(?P<position>\d+)$",
    re.IGNORECASE,
)
_STORAGE_GRID_PATTERN = re.compile(
    r"^(?P<floor>\d+F)-M-R(?P<rack>\d+)-L(?P<level>\d+)-G(?P<grid>\d+)$",
    re.IGNORECASE,
)
_STORAGE_LEVEL_PATTERN = re.compile(
    r"^(?P<floor>\d+F)-M-R(?P<rack>\d+)-L(?P<level>\d+)$",
    re.IGNORECASE,
)
_STORAGE_RACK_PATTERN = re.compile(
    r"^(?P<floor>\d+F)-M-R(?P<rack>\d+)$",
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

# Owner-confirmed physical rack identities.  The 2026-08-11 rule removes the
# unstable left-to-right P position from new mold locations.  Published rack
# levels/bays are read from the warehouse layout; the constants below only
# retain the confirmed rack identity and machine-blocked bottom levels.
ONE_FLOOR_MOLD_RACKS = (
    {
        "rack": 1,
        "rack_code": "R01",
        "name": "左架（模具002）",
        "zone_code": "ZONE-1F-MOLD-002",
        "blocked_levels": (1,),
        "levels": ({"level": 2, "kind": "flat"}, {"level": 3, "kind": "flat"}),
    },
    {
        "rack": 2,
        "rack_code": "R02",
        "name": "中架（模具002）",
        "zone_code": "ZONE-1F-MOLD-002",
        "blocked_levels": (1,),
        "levels": ({"level": 2, "kind": "flat"},),
    },
    {
        "rack": 3,
        "rack_code": "R03",
        "name": "右架（模具001）",
        "zone_code": "ZONE-1F-MOLD-001",
        "blocked_levels": (),
        "levels": (
            {"level": 1, "kind": "vertical"},
            {"level": 2, "kind": "flat"},
            {"level": 3, "kind": "flat"},
        ),
    },
    {
        "rack": 4,
        "rack_code": "R04",
        "name": "靠墙特大模具区",
        "zone_code": "ZONE-1F-MOLD-R04",
        "blocked_levels": (),
        "levels": ({"level": 1, "kind": "vertical"},),
    },
)
_ONE_FLOOR_RACKS_BY_NUMBER = {item["rack"]: item for item in ONE_FLOOR_MOLD_RACKS}
_ONE_FLOOR_ALLOWED_LEVELS = {
    (rack["rack"], level["level"]): level["kind"]
    for rack in ONE_FLOOR_MOLD_RACKS
    for level in rack["levels"]
}


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
    occupants: tuple[MoldTool, ...]


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


def one_floor_mold_location_options(
    floor_layout: dict | None = None,
) -> list[dict]:
    """Return rack/level/grid choices from the published warehouse layout."""

    floor = floor_layout if floor_layout is not None else load_warehouse_twin_floor("1F")
    layout_racks = {
        str(row.get("mold_rack_code") or "").strip().upper(): row
        for row in (floor.get("racks") or [])
        if str(row.get("mold_rack_code") or "").strip()
    }
    options: list[dict] = []
    for confirmed in ONE_FLOOR_MOLD_RACKS:
        rack_code = str(confirmed["rack_code"])
        layout = layout_racks.get(rack_code)
        if layout is None:
            if rack_code != "R04":
                continue
            options.append(
                {
                    "rack": confirmed["rack"],
                    "rack_code": rack_code,
                    "name": confirmed["name"],
                    "zone_code": confirmed["zone_code"],
                    "location_depth": "rack",
                    "grid_count": 0,
                    "levels": [],
                }
            )
            continue

        level_count = max(0, int(layout.get("levels") or 0))
        grid_count = max(0, int(layout.get("bays") or 0))
        blocked = set(confirmed.get("blocked_levels") or ())
        old_kinds = {
            int(row["level"]): str(row["kind"])
            for row in confirmed.get("levels") or ()
        }
        levels = [
            {
                "level": level,
                "kind": old_kinds.get(level, "flat"),
                "grids": list(range(1, grid_count + 1)),
            }
            for level in range(1, level_count + 1)
            if level not in blocked
        ]
        options.append(
            {
                "rack": confirmed["rack"],
                "rack_code": rack_code,
                "name": str(layout.get("name") or confirmed["name"]),
                "zone_code": str(layout.get("area_code") or confirmed["zone_code"]),
                "location_depth": "grid" if grid_count else ("level" if levels else "rack"),
                "grid_count": grid_count,
                "levels": levels,
            }
        )
    return options


def _rack_prompt(floor: str, rack: int) -> str:
    if floor == "1F" and rack in _ONE_FLOOR_RACKS_BY_NUMBER:
        item = _ONE_FLOOR_RACKS_BY_NUMBER[rack]
        return f"{item['rack_code']} {item['name']}"
    return f"第{rack}号货架"


def describe_mold_location(value: str) -> dict:
    """Turn a stable rack code into a prompt a workshop worker can follow."""

    raw = (value or "").strip()
    normalized = raw.upper()
    storage_grid = _STORAGE_GRID_PATTERN.fullmatch(normalized)
    if storage_grid:
        parts = storage_grid.groupdict()
        rack = _number(parts["rack"])
        level = _number(parts["level"])
        grid = _number(parts["grid"])
        return {
            "kind": "storage_grid",
            "location_code": normalized,
            "floor": parts["floor"].upper(),
            "area": "M",
            "rack": rack,
            "level": level,
            "grid": grid,
            "row": None,
            "position": None,
            "prompt": (
                f"前往{_floor_text(parts['floor'])}，{_rack_prompt(parts['floor'].upper(), rack)}，"
                f"第{level}层、第{grid}格查找。格内左右顺序会随拿取变化，不作为系统位置。"
                "拿取前请核对模具编号和存货编码。"
            ),
        }
    storage_level = _STORAGE_LEVEL_PATTERN.fullmatch(normalized)
    if storage_level:
        parts = storage_level.groupdict()
        rack = _number(parts["rack"])
        level = _number(parts["level"])
        return {
            "kind": "storage_level",
            "location_code": normalized,
            "floor": parts["floor"].upper(),
            "area": "M",
            "rack": rack,
            "level": level,
            "grid": None,
            "row": None,
            "position": None,
            "prompt": (
                f"前往{_floor_text(parts['floor'])}，{_rack_prompt(parts['floor'].upper(), rack)}，"
                f"第{level}层查找。该层没有正式格号，左右顺序不作为系统位置。"
                "拿取前请核对模具编号和存货编码。"
            ),
        }
    storage_rack = _STORAGE_RACK_PATTERN.fullmatch(normalized)
    if storage_rack:
        parts = storage_rack.groupdict()
        rack = _number(parts["rack"])
        return {
            "kind": "storage_rack",
            "location_code": normalized,
            "floor": parts["floor"].upper(),
            "area": "M",
            "rack": rack,
            "level": None,
            "grid": None,
            "row": None,
            "position": None,
            "prompt": (
                f"前往{_floor_text(parts['floor'])}，{_rack_prompt(parts['floor'].upper(), rack)}查找。"
                "该货架没有正式层号或格号，左右顺序不作为系统位置。"
                "拿取前请核对模具编号和存货编码。"
            ),
        }
    flat = _CANONICAL_FLAT_PATTERN.fullmatch(normalized)
    if flat:
        parts = flat.groupdict()
        rack = _number(parts["rack"])
        prompt = (
            f"前往{_floor_text(parts['floor'])}，{_rack_prompt(parts['floor'].upper(), rack)}，"
            f"第{_number(parts['level'])}层、第{_number(parts['row'])}排，"
            f"原档案 P{_number(parts['position']):02d} 仅作历史记录，不再代表从左到右固定顺序。"
            "拿取前请核对模具编号和存货编码。"
        )
        return {
            "kind": "flat",
            "location_code": normalized,
            "floor": parts["floor"].upper(),
            "area": "M",
            "rack": rack,
            "level": _number(parts["level"]),
            "row": _number(parts["row"]),
            "position": _number(parts["position"]),
            "prompt": prompt,
        }
    vertical = _CANONICAL_VERTICAL_PATTERN.fullmatch(normalized)
    if vertical:
        parts = vertical.groupdict()
        rack = _number(parts["rack"])
        level_text = "底层（第1层）" if _number(parts["level"]) == 1 else f"第{_number(parts['level'])}层"
        prompt = (
            f"前往{_floor_text(parts['floor'])}，{_rack_prompt(parts['floor'].upper(), rack)}，"
            f"{level_text}竖放区查找；原档案 P{_number(parts['position']):02d} 仅作历史记录，"
            "不再代表从左到右固定顺序。大模具较重，请按现场要求两人搬运；"
            "拿取前核对模具编号和存货编码。"
        )
        return {
            "kind": "vertical",
            "location_code": normalized,
            "floor": parts["floor"].upper(),
            "area": "M",
            "rack": rack,
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
                f"原档案 P{_number(parts['position']):02d} 仅作历史记录，不再代表从左到右固定顺序。"
                "该位置使用旧简写，建议现场复核后改为新货架/层/格位置码；"
                "拿取前核对模具编号和存货编码。"
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
                f"第{_number(parts['level'])}层竖放区查找；原档案 P{_number(parts['position']):02d} "
                "仅作历史记录，不再代表从左到右固定顺序。"
                "该位置使用旧简写，建议现场复核后改为新货架/层/格位置码；大模具请两人搬运。"
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
    """Accept only canonical, physically addressable confirmed mold positions."""

    guide = describe_mold_location(value)
    if guide["kind"] in {"storage_grid", "storage_level", "storage_rack"}:
        if guide["floor"] != "1F":
            raise MoldLocationError("新的货架/层/格位置码当前只允许一楼模具区", status_code=422)
        options = {row["rack"]: row for row in one_floor_mold_location_options()}
        rack = options.get(int(guide["rack"] or 0))
        if rack is None:
            raise MoldLocationError("该一楼模具货架尚未在已发布布局中配置", status_code=422)
        if rack["location_depth"] == "rack":
            if guide["kind"] != "storage_rack":
                raise MoldLocationError("该模具区没有正式层号或格号，请只选择货架", status_code=422)
        else:
            levels = {int(row["level"]): row for row in rack.get("levels") or []}
            level = levels.get(int(guide["level"] or 0))
            if level is None:
                raise MoldLocationError("该层不是已发布布局中的可用模具层", status_code=422)
            if rack["location_depth"] == "grid":
                if guide["kind"] != "storage_grid":
                    raise MoldLocationError("该货架已配置格数，请选择具体格", status_code=422)
                if int(guide["grid"] or 0) not in set(level.get("grids") or []):
                    raise MoldLocationError("该格不是已发布布局中的可用模具格", status_code=422)
            elif guide["kind"] != "storage_level":
                raise MoldLocationError("该货架未配置格数，请选择到具体层", status_code=422)
        return str(guide["location_code"])
    if guide["kind"] not in {"flat", "vertical"} or guide["floor"] not in {"1F", "3F"}:
        raise MoldLocationError(
            "目标位置必须是合法的 1F-M 或 3F-M 平放/竖放位置码，旧自由文本不能用于移动确认",
            status_code=422,
        )
    numeric_fields = ("rack", "level", "position")
    if guide["kind"] == "flat":
        numeric_fields += ("row",)
    if any(int(guide[field] or 0) <= 0 for field in numeric_fields):
        raise MoldLocationError(
            "模具位置码中的货架、层、排和位置编号必须大于 0",
            status_code=422,
        )
    if guide["floor"] == "1F":
        expected_kind = _ONE_FLOOR_ALLOWED_LEVELS.get((guide["rack"], guide["level"]))
        if expected_kind is None:
            raise MoldLocationError(
                "该一楼层位不是可用模板位：R01/R02 机器底层及未确认层位禁止使用",
                status_code=422,
            )
        if guide["kind"] != expected_kind:
            readable = "竖放位 V-P" if expected_kind == "vertical" else "平放位 D01-P"
            raise MoldLocationError(
                f"该一楼层位必须使用{readable}格式",
                status_code=422,
            )
        if guide["kind"] == "flat" and guide["row"] != 1:
            raise MoldLocationError(
                "一楼模板架当前只确认单排 D01，其他排位尚未实测，禁止使用",
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


def _location_occupants(
    db: Session,
    *,
    target_location: str,
    exclude_mold_id: int,
) -> tuple[MoldTool, ...]:
    return tuple(db.scalars(
        select(MoldTool).where(
            MoldTool.id != exclude_mold_id,
            MoldTool.is_active.is_(True),
            func.upper(func.trim(MoldTool.rack_location)) == target_location,
        )
        .order_by(MoldTool.mold_code, MoldTool.id)
    ).all())


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
        occupants=_location_occupants(
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
                    "模具位置版本已变化，请重新预览",
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
