from __future__ import annotations

import importlib.util
import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "admin" / "p1_147_warehouse_coordinate_normalization.py"
SPEC = importlib.util.spec_from_file_location("p1_147_coordinate_frontend_contract", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _rotate(points: list[list[float]], degrees: float) -> list[list[float]]:
    radians = math.radians(degrees)
    cosine, sine = math.cos(radians), math.sin(radians)
    return [
        [x * cosine - y * sine + 10000, x * sine + y * cosine + 20000]
        for x, y in points
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
@pytest.mark.parametrize(
    "points",
    [
        [[0, 0], [4000, 0], [4000, 5000], [0, 5000]],
        _rotate([[0, 0], [4000, 0], [4000, 5000], [0, 5000]], 27),
        [[0, 0], [4000, 0], [4000, 5000], [2500, 5000], [2500, 3000], [0, 3000]],
    ],
)
def test_python_audit_uses_the_same_visible_center_as_frontend(
    points: list[list[float]],
) -> None:
    layout = {
        "left_pct": 10,
        "top_pct": 20,
        "width_pct": 25,
        "height_pct": 24,
    }
    expected = MODULE.visible_geometry(points, layout, slot_width=1000, slot_depth=1200)
    payload = json.dumps({"points": points, "layout": layout})
    javascript = """
      import {buildMappedLocationPallets} from './factory_twin/frontend/src/warehouseInventory.mjs';
      const payload=JSON.parse(process.argv[1]);
      const feature={id:'zone',feature_kind:'zone',feature_code:'ZONE',points:payload.points};
      const location={location_id:1,location_code:'L1',floor_code:'3F',position_status:'mapped',
        map_feature_id:'zone',storage_type:'ground',map_position:{...payload.layout,version:1,layout_kind:'physical_pallet'},
        pallets:[{pallet_id:1,pallet_code:'PAL-1',items:[{quantity:1}]}]};
      const rows=buildMappedLocationPallets([feature],[location],'3F',
        {contract_version:'standard-pallet-v1',width_mm:1200,depth_mm:1000,height_mm:150});
      console.log(JSON.stringify({x_mm:rows[0].x_mm,y_mm:rows[0].y_mm}));
    """
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", javascript, payload],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    actual = json.loads(completed.stdout)

    assert actual["x_mm"] == pytest.approx(float(expected["visible_center_x_mm"]), abs=0.001)
    assert actual["y_mm"] == pytest.approx(float(expected["visible_center_y_mm"]), abs=0.001)


def test_current_published_three_floor_geometry_is_classified_for_field_sampling() -> None:
    document = json.loads(
        (ROOT / "static" / "factory_maps" / "twin_layout_v1.json").read_text(
            encoding="utf-8"
        )
    )
    floor = document["floors"]["3F"]
    zones = [feature for feature in floor["features"] if feature.get("feature_kind") == "zone"]
    classes = [MODULE.geometry_class(feature["points"])[0] for feature in zones]

    assert floor["revision"] == "3994317ae14a7f18"
    assert classes.count("axis_aligned_rectangle") == 24
    assert classes.count("rotated_rectangle") == 0
    assert classes.count("non_rectangular") == 16
