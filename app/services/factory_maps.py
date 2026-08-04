from __future__ import annotations

import base64
import json
from pathlib import Path


FACTORY_MAP_FILES = {
    "1F": "factory_1f_v4.json",
}

FACTORY_MAP_ROOT = Path(__file__).resolve().parents[2] / "static" / "factory_maps"
FACTORY_MAP_ASSET_ROOT = FACTORY_MAP_ROOT / "assets"


class FactoryMapNotFoundError(LookupError):
    pass


def load_factory_map(floor_code: str) -> dict:
    normalized = floor_code.strip().upper()
    filename = FACTORY_MAP_FILES.get(normalized)
    if filename is None:
        raise FactoryMapNotFoundError(f"尚未配置 {normalized or floor_code} 工厂地图")

    path = FACTORY_MAP_ROOT / filename
    if not path.is_file():
        raise FactoryMapNotFoundError(f"{normalized} 工厂地图资产不存在")

    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("floor_code") != normalized:
        raise ValueError(f"{normalized} 工厂地图资产楼层编码不匹配")
    asset_names = {
        item.get("machine_asset")
        for item in [*payload.get("primitives", []), *payload.get("staff_overlays", [])]
        if item.get("machine_asset")
    }
    payload["machine_assets"] = {}
    for asset_name in sorted(asset_names):
        asset_path = FACTORY_MAP_ASSET_ROOT / asset_name
        if asset_path.parent != FACTORY_MAP_ASSET_ROOT or not asset_path.is_file():
            raise ValueError(f"工厂地图设备图像不存在：{asset_name}")
        encoded = base64.b64encode(asset_path.read_bytes()).decode("ascii")
        payload["machine_assets"][asset_name] = f"data:image/webp;base64,{encoded}"
    return payload
