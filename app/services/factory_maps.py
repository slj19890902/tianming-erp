from __future__ import annotations

import json
from pathlib import Path


FACTORY_MAP_FILES = {
    "1F": "factory_1f_v4.json",
}

FACTORY_MAP_ROOT = Path(__file__).resolve().parents[2] / "static" / "factory_maps"


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
    return payload
