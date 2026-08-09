from __future__ import annotations

import json
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
TWIN_LAYOUT_PATH = (
    _PROJECT_ROOT / "static" / "factory_maps" / "twin_layout_v1.json"
)
TWIN_LAYOUT_RUNTIME_PATH = (
    _PROJECT_ROOT / "data" / "layout_runtime" / "twin_layout_v1.json"
)


class WarehouseTwinLayoutNotFoundError(LookupError):
    pass


def resolve_warehouse_twin_layout_path(path: Path | None = None) -> Path:
    """Use the published runtime copy when present; never mask a damaged copy."""

    if path is not None:
        return path
    if TWIN_LAYOUT_RUNTIME_PATH.exists():
        return TWIN_LAYOUT_RUNTIME_PATH
    return TWIN_LAYOUT_PATH


def load_warehouse_twin_floor(
    floor_code: str,
    *,
    path: Path | None = None,
) -> dict:
    normalized = str(floor_code or "").strip().upper()
    if normalized not in {"1F", "3F"}:
        raise WarehouseTwinLayoutNotFoundError(f"尚未配置 {normalized or floor_code} 数字孪生平面")
    target = resolve_warehouse_twin_layout_path(path)
    if not target.is_file():
        raise WarehouseTwinLayoutNotFoundError("数字孪生平面资产尚未导出")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        if path is None and target == TWIN_LAYOUT_RUNTIME_PATH:
            raise ValueError(
                "数字孪生运行地图损坏，已停止读取；请恢复运行地图后重试"
            ) from error
        raise ValueError("数字孪生平面资产无法读取") from error
    if payload.get("schema_version") != 1:
        raise ValueError("数字孪生平面资产版本不受支持")
    floor = (payload.get("floors") or {}).get(normalized)
    if floor is None or floor.get("floor_code") != normalized:
        raise WarehouseTwinLayoutNotFoundError(f"数字孪生平面缺少 {normalized}")
    return {
        **floor,
        "generated_at": payload.get("generated_at"),
        "projection_notice": "仅投影已确认或人工候选空间；正式库存数量仍以 ERP 库存账为准。",
    }
