from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


def normalize_rotation(value: int) -> int:
    normalized = value % 360
    if normalized not in {0, 90, 180, 270}:
        raise ValueError("旋转角度只允许 0、90、180、270")
    return normalized


def validate_rack_level_heights(levels: int, height_mm: float, heights: list[float]) -> list[float]:
    normalized = [float(value) for value in heights]
    if not normalized:
        return normalized
    if len(normalized) != max(0, levels - 1):
        raise ValueError("层板高度数量必须等于层数减一")
    if normalized != sorted(set(normalized)):
        raise ValueError("层板高度必须从低到高且不能重复")
    if normalized[0] <= 0 or normalized[-1] >= height_mm:
        raise ValueError("层板高度必须大于0且小于货架总高度")
    return normalized


class AssetTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    category: str = Field(default="生产设备", min_length=1, max_length=80)
    render_type: str = Field(default="box25d", pattern="^(box25d|png)$")
    color: str = Field(default="#2563eb", pattern=r"^#[0-9A-Fa-f]{6}$")
    default_width_mm: float = Field(gt=0, le=200_000)
    default_depth_mm: float = Field(gt=0, le=200_000)
    default_height_mm: float = Field(gt=0, le=100_000)


class PlacementCreate(BaseModel):
    template_id: str
    name: str | None = Field(default=None, max_length=160)
    x_mm: float = Field(ge=-10_000_000, le=10_000_000)
    y_mm: float = Field(ge=-10_000_000, le=10_000_000)
    z_mm: float = Field(default=0, ge=0, le=100_000)
    width_mm: float | None = Field(default=None, gt=0, le=200_000)
    depth_mm: float | None = Field(default=None, gt=0, le=200_000)
    height_mm: float | None = Field(default=None, gt=0, le=100_000)
    rotation_deg: int = 0

    @field_validator("rotation_deg")
    @classmethod
    def validate_rotation(cls, value: int) -> int:
        return normalize_rotation(value)


class PlacementUpdate(BaseModel):
    version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    x_mm: float | None = Field(default=None, ge=-10_000_000, le=10_000_000)
    y_mm: float | None = Field(default=None, ge=-10_000_000, le=10_000_000)
    z_mm: float | None = Field(default=None, ge=0, le=100_000)
    width_mm: float | None = Field(default=None, gt=0, le=200_000)
    depth_mm: float | None = Field(default=None, gt=0, le=200_000)
    height_mm: float | None = Field(default=None, gt=0, le=100_000)
    rotation_deg: int | None = None
    is_confirmed: bool | None = None
    is_locked: bool | None = None

    @field_validator("rotation_deg")
    @classmethod
    def validate_rotation(cls, value: int | None) -> int | None:
        if value is None:
            return value
        return normalize_rotation(value)


class RackCreate(BaseModel):
    rack_code: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=160)
    x_mm: float = Field(ge=-10_000_000, le=10_000_000)
    y_mm: float = Field(ge=-10_000_000, le=10_000_000)
    z_mm: float = Field(default=0, ge=0, le=100_000)
    width_mm: float = Field(gt=0, le=200_000)
    depth_mm: float = Field(gt=0, le=200_000)
    height_mm: float = Field(gt=0, le=100_000)
    levels: int = Field(default=1, ge=1, le=20)
    level_heights_mm: list[float] = Field(default_factory=list, max_length=19)
    cargo_rows: int = Field(default=4, ge=3, le=5)
    bays: int = Field(default=1, ge=1, le=50)
    access_side: str = Field(default="south", pattern="^(north|south|east|west|both)$")
    min_aisle_width_mm: float = Field(default=1200, ge=0, le=20_000)
    rotation_deg: int = 0
    color: str = Field(default="#8b5cf6", pattern=r"^#[0-9A-Fa-f]{6}$")
    source: str = Field(default="manual", pattern="^(manual|ai)$")

    @field_validator("rotation_deg")
    @classmethod
    def validate_rotation(cls, value: int) -> int:
        return normalize_rotation(value)

    @model_validator(mode="after")
    def validate_levels(self) -> "RackCreate":
        self.level_heights_mm = validate_rack_level_heights(
            self.levels, self.height_mm, self.level_heights_mm
        )
        return self


class RackUpdate(BaseModel):
    version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    x_mm: float | None = Field(default=None, ge=-10_000_000, le=10_000_000)
    y_mm: float | None = Field(default=None, ge=-10_000_000, le=10_000_000)
    width_mm: float | None = Field(default=None, gt=0, le=200_000)
    depth_mm: float | None = Field(default=None, gt=0, le=200_000)
    height_mm: float | None = Field(default=None, gt=0, le=100_000)
    levels: int | None = Field(default=None, ge=1, le=20)
    level_heights_mm: list[float] | None = Field(default=None, max_length=19)
    cargo_rows: int | None = Field(default=None, ge=3, le=5)
    bays: int | None = Field(default=None, ge=1, le=50)
    access_side: str | None = Field(default=None, pattern="^(north|south|east|west|both)$")
    min_aisle_width_mm: float | None = Field(default=None, ge=0, le=20_000)
    rotation_deg: int | None = None
    color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    is_locked: bool | None = None

    @field_validator("rotation_deg")
    @classmethod
    def validate_rotation(cls, value: int | None) -> int | None:
        return None if value is None else normalize_rotation(value)


class PalletCreate(BaseModel):
    pallet_code: str = Field(min_length=1, max_length=80)
    name: str = Field(default="标准栈板", min_length=1, max_length=160)
    x_mm: float = Field(ge=-10_000_000, le=10_000_000)
    y_mm: float = Field(ge=-10_000_000, le=10_000_000)
    z_mm: float = Field(default=0, ge=0, le=100_000)
    width_mm: float = Field(default=1200, gt=0, le=20_000)
    depth_mm: float = Field(default=1000, gt=0, le=20_000)
    height_mm: float = Field(default=150, gt=0, le=5_000)
    rotation_deg: int = 0
    color: str = Field(default="#b7793f", pattern=r"^#[0-9A-Fa-f]{6}$")
    visual_status: str = Field(
        default="empty", pattern="^(empty|waiting|in_process|completed|abnormal)$"
    )
    status_note: str = Field(default="", max_length=160)
    is_simulated: bool = False
    snap_enabled: bool = True
    snap_threshold_mm: float = Field(default=180, ge=0, le=1000)

    @field_validator("rotation_deg")
    @classmethod
    def validate_rotation(cls, value: int) -> int:
        return normalize_rotation(value)


class PalletUpdate(BaseModel):
    version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    x_mm: float | None = Field(default=None, ge=-10_000_000, le=10_000_000)
    y_mm: float | None = Field(default=None, ge=-10_000_000, le=10_000_000)
    width_mm: float | None = Field(default=None, gt=0, le=20_000)
    depth_mm: float | None = Field(default=None, gt=0, le=20_000)
    height_mm: float | None = Field(default=None, gt=0, le=5_000)
    rotation_deg: int | None = None
    color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    visual_status: str | None = Field(
        default=None, pattern="^(empty|waiting|in_process|completed|abnormal)$"
    )
    status_note: str | None = Field(default=None, max_length=160)
    is_simulated: bool | None = None
    snap_enabled: bool = True
    snap_threshold_mm: float = Field(default=180, ge=0, le=1000)

    @field_validator("rotation_deg")
    @classmethod
    def validate_rotation(cls, value: int | None) -> int | None:
        return None if value is None else normalize_rotation(value)


class FeatureCreate(BaseModel):
    feature_code: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=160)
    feature_kind: str = Field(pattern="^(zone|aisle|no_go|structure)$")
    subtype: str = Field(min_length=1, max_length=50)
    points: list[list[float]]
    width_mm: float | None = Field(default=None, gt=0, le=20_000)
    direction: str | None = Field(default=None, pattern="^(one_way|two_way|none)$")
    no_stacking: bool = False
    storage_mode: str = Field(default="floor", pattern="^(floor|rack|overhead)$")
    elevation_mm: float = Field(default=0, ge=0, le=100_000)
    storage_height_mm: float = Field(default=1000, gt=0, le=100_000)
    color: str = Field(default="#3b82f6", pattern=r"^#[0-9A-Fa-f]{6}$")
    source: str = Field(default="manual", pattern="^(manual|ai)$")

    @model_validator(mode="after")
    def validate_geometry(self):
        minimum = 2 if self.feature_kind in {"aisle", "structure"} else 3
        if len(self.points) < minimum:
            raise ValueError(f"{self.feature_kind} 至少需要 {minimum} 个坐标点")
        for point in self.points:
            if len(point) != 2 or any(abs(float(value)) > 10_000_000 for value in point):
                raise ValueError("坐标点必须是有效的毫米 X/Y 数组")
        if self.feature_kind in {"aisle", "structure"} and self.width_mm is None:
            raise ValueError("通道或人工结构必须设置真实宽度")
        if self.feature_kind == "structure" and self.subtype not in {
            "custom_wall", "rolling_door", "custom_window", "custom_column", "freight_elevator", "dxf_hidden"
        }:
            raise ValueError("人工结构分类无效")
        if self.feature_kind == "structure" and self.storage_mode != "floor":
            raise ValueError("人工结构必须使用地面基准")
        if self.feature_kind not in {"zone", "structure"} and (
            self.storage_mode != "floor" or self.elevation_mm != 0
        ):
            raise ValueError("只有堆放区域或人工结构可以设置离地高度")
        if self.feature_kind == "zone" and self.storage_mode != "floor" and self.elevation_mm <= 0:
            raise ValueError("货架上层或架空区域必须设置大于 0 的离地高度")
        return self


class FeatureUpdate(BaseModel):
    version: int = Field(ge=1)
    feature_code: str | None = Field(default=None, min_length=1, max_length=80)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    subtype: str | None = Field(default=None, min_length=1, max_length=50)
    points: list[list[float]] | None = None
    width_mm: float | None = Field(default=None, gt=0, le=20_000)
    direction: str | None = Field(default=None, pattern="^(one_way|two_way|none)$")
    no_stacking: bool | None = None
    storage_mode: str | None = Field(default=None, pattern="^(floor|rack|overhead)$")
    elevation_mm: float | None = Field(default=None, ge=0, le=100_000)
    storage_height_mm: float | None = Field(default=None, gt=0, le=100_000)
    color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")


class LayoutSummary(BaseModel):
    id: str
    name: str
    floor_code: str
    source_name: str
    source_sha256: str
    source_units: str
    bounds_mm: dict
    created_at: datetime
    updated_at: datetime


class PlacementRead(BaseModel):
    id: str
    layout_id: str
    template_id: str
    name: str
    x_mm: float
    y_mm: float
    z_mm: float
    width_mm: float
    depth_mm: float
    height_mm: float
    rotation_deg: int
    is_confirmed: bool
    is_locked: bool
    version: int


class RackRead(BaseModel):
    id: str
    layout_id: str
    rack_code: str
    name: str
    x_mm: float
    y_mm: float
    z_mm: float
    width_mm: float
    depth_mm: float
    height_mm: float
    levels: int
    level_heights_mm: list[float]
    cargo_rows: int
    bays: int
    access_side: str
    min_aisle_width_mm: float
    rotation_deg: int
    color: str
    source: str
    status: str
    is_locked: bool
    version: int


class PalletRead(BaseModel):
    id: str
    layout_id: str
    pallet_code: str
    name: str
    zone_id: str
    zone_code: str
    x_mm: float
    y_mm: float
    z_mm: float
    width_mm: float
    depth_mm: float
    height_mm: float
    rotation_deg: int
    color: str
    visual_status: str
    status_note: str
    is_simulated: bool
    version: int
    snapped: bool = False


class ProductionProjectionBind(BaseModel):
    target_kind: Literal["pallet", "zone"]
    target_id: str = Field(min_length=1, max_length=36)
    version: int | None = Field(default=None, ge=1)


class ProductionProjectionMappingRead(BaseModel):
    id: str
    source_task_id: int
    source_type: str
    target_kind: str
    target_id: str
    target_code: str | None = None
    target_name: str | None = None
    target_missing: bool = False
    confirmed_at: datetime
    version: int


class FeatureRead(BaseModel):
    id: str
    layout_id: str
    feature_code: str
    name: str
    feature_kind: str
    subtype: str
    points: list[list[float]]
    width_mm: float | None
    direction: str | None
    no_stacking: bool
    storage_mode: str
    elevation_mm: float
    storage_height_mm: float
    color: str
    area_mm2: float
    source: str
    status: str
    is_locked: bool
    version: int


class ViolationRead(BaseModel):
    id: str
    severity: str
    rule_code: str
    message: str
    entity_kind: str
    entity_id: str
    related_kind: str | None = None
    related_id: str | None = None


class AssetTemplateRead(BaseModel):
    id: str
    name: str
    category: str
    render_type: str
    image_url: str | None
    color: str
    default_width_mm: float
    default_depth_mm: float
    default_height_mm: float


class LayoutRead(LayoutSummary):
    structures: list[dict]
    warnings: list[str]
    placements: list[PlacementRead]
    racks: list[RackRead]
    pallets: list[PalletRead]
    features: list[FeatureRead]
    violations: list[ViolationRead]
    rule_defaults: dict[str, float]
