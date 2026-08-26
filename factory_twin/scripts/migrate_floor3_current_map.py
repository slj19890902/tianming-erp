from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any


FLOOR_CODE = "3F"
EXPECTED_FORMAL_MAP_SHA256 = (
    "eb7292bd521072345835a0f72490c9ddd41ade5e9b1bac9267ab8ebaa691185c"
)
MEASUREMENT_SOURCE = "owner_confirmed_2026-08-26"
MIGRATION_TIMESTAMP = "2026-08-26T21:30:00+08:00"
AE_RIGHT_EDGE_MM = 33_329.0

# x/width are the owner-confirmed scale-model measurements.  The current
# runtime map already records the correct north/south adjacency, so its Y
# centre is preserved while the distorted candidate height is replaced.
MEASURED_AE_ZONES: dict[str, tuple[float, float, float]] = {
    "A1": (0.0, 3.10, 7.43),
    "A2": (0.0, 3.10, 14.00),
    "AB1": (4.80, 2.60, 4.10),
    "AB2": (4.80, 2.60, 1.60),
    "B1": (4.60, 3.00, 10.80),
    "B2": (4.60, 3.00, 14.00),
    "C1": (9.10, 3.50, 13.70),
    "C2": (9.10, 3.50, 14.00),
    "CD1": (9.10, 6.60, 4.00),
    "D1": (14.10, 2.55, 15.00),
    "D2": (14.10, 2.60, 14.00),
    "DE1": (18.15, 6.30, 1.20),
    "E1": (18.15, 5.00, 12.00),
    "E2": (18.20, 5.00, 14.00),
    "E3": (19.30, 2.80, 4.00),
}

FORMAL_AREA_IDS = {
    "A1": 1,
    "A2": 2,
    "AB1": 3,
    "AB2": 4,
    "B1": 5,
    "B2": 6,
    "C1": 7,
    "C2": 8,
    "CD1": 9,
    "D1": 10,
    "D2": 11,
    "DE1": 12,
    "E1": 13,
    "E2": 14,
    "E3": 15,
    "FIN-LOOSE-001": 16,
    "F1": 17,
    "F12": 18,
    "F2": 19,
    "F3": 20,
    "F34": 21,
    "F4": 22,
    "RAW-001": 25,
    "FG-004": 26,
    "SEMI-006": 27,
    "FG-009": 35,
    "SEMI-010": 36,
    "FG-005": 37,
    "FG-006": 38,
    "FG-007": 39,
    "SEMI-008": 40,
    "RAW-004": 41,
    "SEMI-011": 48,
    "FG-008": 56,
}

FEATURE_CODES = {
    **{code: f"ZONE-3F-ERP-{code}" for code in MEASURED_AE_ZONES},
    "F1": "ZONE-3F-ERP-F1",
    "F12": "ZONE-3F-ERP-F12",
    "F2": "ZONE-3F-ERP-F2",
    "F3": "ZONE-3F-ERP-F3",
    "F34": "ZONE-3F-ERP-F34",
    "F4": "ZONE-3F-ERP-F4",
    "FIN-LOOSE-001": "ZONE-3F-FG-001",
    "SEMI-011": "ZONE-3F-SEMI-011",
}

RACK_AREAS = {"F1", "F2", "F3", "F4"}
TEMPORARY_AREAS = {"F12", "F34"}
CURRENT_AREA_NAMES = {
    "A1": "A1 成品存放区（主通道北侧）",
    "A2": "A2 成品存放区（主通道南侧）",
    "AB1": "AB1 成品存放区",
    "AB2": "AB2 成品存放区",
    "B1": "B1 成品存放区（主通道北侧）",
    "B2": "B2 成品存放区（主通道南侧）",
    "C1": "C1 成品存放区（主通道北侧）",
    "C2": "C2 成品存放区（主通道南侧）",
    "CD1": "CD1 成品存放区",
    "D1": "D1 栈板成品存放区（无货架）",
    "D2": "D2 货架为主·栈板混合存放区",
    "DE1": "DE1 成品存放区",
    "E1": "E1 成品存放区（主通道北侧）",
    "E2": "E2 成品存放区（主通道南侧）",
    "E3": "E3 成品存放区",
}


class CurrentMapMigrationError(RuntimeError):
    pass


def _canonical_floor_revision(floor: dict[str, Any]) -> str:
    source = {key: value for key, value in floor.items() if key != "revision"}
    rendered = json.dumps(
        source,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(rendered).hexdigest()[:16]


def _polygon_area(points: list[list[float]]) -> float:
    return round(
        abs(
            sum(
                points[index][0] * points[(index + 1) % len(points)][1]
                - points[(index + 1) % len(points)][0] * points[index][1]
                for index in range(len(points))
            )
        )
        / 2,
        3,
    )


def _y_center(feature: dict[str, Any]) -> float:
    points = feature.get("points") or []
    if len(points) < 3:
        raise CurrentMapMigrationError(
            f"{feature.get('feature_code') or feature.get('id')} 缺少有效边界"
        )
    return (
        min(float(point[1]) for point in points)
        + max(float(point[1]) for point in points)
    ) / 2


def _measured_rectangle(
    feature: dict[str, Any], *, x_m: float, width_m: float, height_m: float
) -> list[list[float]]:
    # The accepted A-E plan is mirrored left/right on the current DXF canvas.
    max_x = AE_RIGHT_EDGE_MM - x_m * 1000
    min_x = max_x - width_m * 1000
    centre_y = _y_center(feature)
    min_y = centre_y - height_m * 500
    max_y = centre_y + height_m * 500
    return [
        [round(min_x, 3), round(min_y, 3)],
        [round(max_x, 3), round(min_y, 3)],
        [round(max_x, 3), round(max_y, 3)],
        [round(min_x, 3), round(max_y, 3)],
    ]


def _feature_index(floor: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for feature in floor.get("features") or []:
        code = str(feature.get("feature_code") or "").strip().upper()
        if not code:
            continue
        if code in rows:
            raise CurrentMapMigrationError(f"地图存在重复要素编码：{code}")
        rows[code] = feature
    return rows


def _set_policy(
    feature: dict[str, Any],
    *,
    area_code: str,
    inventory_type: str,
    storage_layout: str,
    area_name: str,
) -> None:
    feature["erp_area_code"] = area_code
    feature["allowed_inventory_types"] = [inventory_type]
    feature["storage_layout"] = storage_layout
    feature["formal_area_id"] = FORMAL_AREA_IDS[area_code]
    feature["formal_floor_id"] = 1
    feature["formal_area_name"] = area_name
    feature["status"] = "confirmed"
    feature["is_locked"] = True
    feature["source"] = "manual"
    feature["measurement_source"] = MEASUREMENT_SOURCE
    feature["version"] = int(feature.get("version") or 0) + 1


def migrate_document(document: dict[str, Any]) -> dict[str, Any]:
    if document.get("schema_version") != 1:
        raise CurrentMapMigrationError("数字孪生地图版本不受支持")
    floors = document.get("floors") or {}
    floor = floors.get(FLOOR_CODE)
    if not isinstance(floor, dict) or floor.get("floor_code") != FLOOR_CODE:
        raise CurrentMapMigrationError("数字孪生地图缺少三楼")
    current_revision = str(floor.get("revision") or "")
    if current_revision != _canonical_floor_revision(floor):
        raise CurrentMapMigrationError("三楼地图修订值与内容不一致，拒绝迁移")

    features = _feature_index(floor)
    missing = sorted(set(FEATURE_CODES.values()) - set(features))
    if missing:
        raise CurrentMapMigrationError("当前地图缺少受控区域：" + "、".join(missing))

    changed: list[str] = []
    for area_code, (x_m, width_m, height_m) in MEASURED_AE_ZONES.items():
        feature = features[FEATURE_CODES[area_code]]
        if str(feature.get("erp_area_code") or "").upper() != area_code:
            raise CurrentMapMigrationError(f"{area_code} 当前地图身份漂移")
        feature["points"] = _measured_rectangle(
            feature,
            x_m=x_m,
            width_m=width_m,
            height_m=height_m,
        )
        feature["area_mm2"] = _polygon_area(feature["points"])
        feature["name"] = CURRENT_AREA_NAMES[area_code]
        _set_policy(
            feature,
            area_code=area_code,
            inventory_type="finished",
            storage_layout="mixed" if area_code == "D2" else "pallet_ground",
            area_name=CURRENT_AREA_NAMES[area_code],
        )
        changed.append(area_code)

    for area_code in sorted(RACK_AREAS | TEMPORARY_AREAS):
        feature = features[FEATURE_CODES[area_code]]
        if str(feature.get("erp_area_code") or "").upper() != area_code:
            raise CurrentMapMigrationError(f"{area_code} 当前地图身份漂移")
        _set_policy(
            feature,
            area_code=area_code,
            inventory_type="finished",
            storage_layout="rack" if area_code in RACK_AREAS else "pallet_ground",
            area_name=str(feature.get("name") or f"{area_code} 区"),
        )
        changed.append(area_code)

    loose = features[FEATURE_CODES["FIN-LOOSE-001"]]
    if str(loose.get("erp_area_code") or "").upper() not in {
        "E4",
        "FIN-LOOSE-001",
    }:
        raise CurrentMapMigrationError("原 E4 零散暂存区身份漂移")
    loose["name"] = "送货剩余零散库存暂存区（无栈板）"
    loose["subtype"] = "delivery_surplus"
    loose["no_stacking"] = True
    loose["usage_notice"] = (
        "仅少量送货剩余零散库存；本区无栈板位。数量多时请选择正常成品栈板位。"
    )
    _set_policy(
        loose,
        area_code="FIN-LOOSE-001",
        inventory_type="finished",
        storage_layout="functional",
        area_name="送货剩余零散库存暂存区（无栈板）",
    )
    changed.append("FIN-LOOSE-001")

    semi = features[FEATURE_CODES["SEMI-011"]]
    if str(semi.get("erp_area_code") or "").upper() not in {"", "SEMI-011"}:
        raise CurrentMapMigrationError("SEMI-011 当前地图身份漂移")
    semi["name"] = "SEMI-011 半成品堆放区"
    semi["subtype"] = "semi_finished"
    semi["points"] = _measured_rectangle(
        semi,
        x_m=18.05,
        width_m=5.30,
        height_m=1.20,
    )
    semi["area_mm2"] = _polygon_area(semi["points"])
    _set_policy(
        semi,
        area_code="SEMI-011",
        inventory_type="semi_finished",
        storage_layout="pallet_ground",
        area_name="SEMI-011 半成品堆放区",
    )
    changed.append("SEMI-011")

    # Existing current-map zones keep their measured geometry, but their
    # storage policy must no longer inherit the old generic "finished" value
    # merely because the original V11 ledger had only one warehouse type.
    already_changed = set(changed)
    for feature in floor.get("features") or []:
        area_code = str(feature.get("erp_area_code") or "").strip().upper()
        if not area_code:
            match = re.fullmatch(
                r"ZONE-3F-(FG|RAW|SEMI)-(\d{3})",
                str(feature.get("feature_code") or "").strip().upper(),
            )
            inferred = f"{match.group(1)}-{match.group(2)}" if match else ""
            if inferred in FORMAL_AREA_IDS:
                area_code = inferred
                feature["erp_area_code"] = inferred
        if not area_code or area_code in already_changed:
            continue
        formal_area_id = FORMAL_AREA_IDS.get(area_code)
        if formal_area_id is None:
            continue
        feature["formal_area_id"] = formal_area_id
        feature["formal_floor_id"] = 1
        feature["formal_area_name"] = str(feature.get("name") or area_code)
        if area_code.startswith("SEMI-"):
            inventory_types = ["semi_finished"]
        elif area_code.startswith("RAW-"):
            inventory_types = ["raw_material"]
        elif area_code.startswith("FG-"):
            inventory_types = ["finished"]
        else:
            inventory_types = list(feature.get("allowed_inventory_types") or [])
        if area_code == "FG-004":
            # This mixed floor zone already contains one audited semi-finished
            # lot.  Preserve that real fact while its legacy anchor is made
            # source-only by the database migration.
            inventory_types = ["finished", "semi_finished", "raw_material"]
        if inventory_types:
            feature["allowed_inventory_types"] = inventory_types
        feature["status"] = "confirmed"
        feature["is_locked"] = True
        feature["source"] = "manual"
        feature["measurement_source"] = str(
            feature.get("measurement_source") or "current_map_confirmed_2026-08-26"
        )
        feature["version"] = int(feature.get("version") or 0) + 1
        changed.append(area_code)

    duplicate_e4 = [
        feature.get("feature_code")
        for feature in floor.get("features") or []
        if str(feature.get("erp_area_code") or "").strip().upper() == "E4"
    ]
    if duplicate_e4:
        raise CurrentMapMigrationError(
            "迁移后仍存在旧 E4 正式身份：" + "、".join(map(str, duplicate_e4))
        )

    floor["erp_area_codes"] = sorted(
        {
            str(feature.get("erp_area_code") or "").strip().upper()
            for feature in floor.get("features") or []
            if str(feature.get("erp_area_code") or "").strip()
        }
    )
    floor["layout_edited_at"] = MIGRATION_TIMESTAMP
    receipts = list(floor.get("layout_edit_receipts") or [])
    receipts.append(
        {
            "action": "p0_26.owner_measured_ae_and_e4_semantics",
            "changed_area_codes": sorted(changed),
            "measurement_source": MEASUREMENT_SOURCE,
            "previous_revision": current_revision,
        }
    )
    floor["layout_edit_receipts"] = receipts[-100:]
    floor["revision"] = _canonical_floor_revision(floor)
    document["generated_at"] = MIGRATION_TIMESTAMP
    document["p0_26_current_map_migration"] = {
        "measurement_source": MEASUREMENT_SOURCE,
        "previous_floor3_revision": current_revision,
        "floor3_revision": floor["revision"],
        "changed_area_codes": sorted(changed),
    }
    return document


def migrate_file(source: Path, output: Path, *, expected_sha256: str | None) -> dict:
    source_bytes = source.read_bytes()
    source_hash = sha256(source_bytes).hexdigest()
    if expected_sha256 and source_hash.lower() != expected_sha256.lower():
        raise CurrentMapMigrationError(
            f"输入地图 SHA-256 不一致：{source_hash}"
        )
    try:
        document = json.loads(source_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CurrentMapMigrationError("输入地图不是有效 UTF-8 JSON") from error
    migrated = migrate_document(document)
    rendered = (
        json.dumps(migrated, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_bytes(rendered)
    temporary.replace(output)
    floor = migrated["floors"][FLOOR_CODE]
    return {
        "source_sha256": source_hash,
        "output_sha256": sha256(rendered).hexdigest(),
        "floor3_revision": floor["revision"],
        "output": str(output.resolve()),
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="迁移三楼 A-E 实测基线，并消除旧 E4 语义冲突"
    )
    value.add_argument("--source", type=Path, required=True)
    value.add_argument("--output", type=Path, required=True)
    value.add_argument(
        "--expected-sha256",
        default=EXPECTED_FORMAL_MAP_SHA256,
        help="拒绝迁移任何不是本次只读审计基线的地图",
    )
    return value


def main() -> None:
    args = parser().parse_args()
    result = migrate_file(
        args.source,
        args.output,
        expected_sha256=args.expected_sha256,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
