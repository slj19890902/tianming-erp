from __future__ import annotations

import json
from pathlib import Path


TWIN_LAYOUT_PATH = (
    Path(__file__).resolve().parents[2] / "static" / "factory_maps" / "twin_layout_v1.json"
)


class WarehouseTwinLayoutNotFoundError(LookupError):
    pass


def load_warehouse_twin_floor(
    floor_code: str,
    *,
    path: Path = TWIN_LAYOUT_PATH,
) -> dict:
    normalized = str(floor_code or "").strip().upper()
    if normalized not in {"1F", "3F"}:
        raise WarehouseTwinLayoutNotFoundError(f"尚未配置 {normalized or floor_code} 数字孪生平面")
    if not path.is_file():
        raise WarehouseTwinLayoutNotFoundError("数字孪生平面资产尚未导出")
    payload = json.loads(path.read_text(encoding="utf-8"))
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
