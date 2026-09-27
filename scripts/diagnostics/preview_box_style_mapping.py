"""Build a read-only aggregate preview of legacy product box-style mapping."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from types import SimpleNamespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.box_type_rules import (  # noqa: E402
    BOX_TYPE_RULES,
    box_type_code,
    canonical_box_style,
)


CONFIRMED_LEGACY_NAMES = {
    "WC 五层钉箱": "名称明确为普通开槽钉箱；层数与钉箱工艺继续保留在产品字段",
    "WCZX 五层粘箱": "名称明确为普通开槽粘箱；层数与粘箱工艺继续保留在产品字段",
    "SCX 单瓦箱": "名称明确为普通单瓦开槽箱；层数继续保留在产品字段",
    "QCX 七层纸箱": "名称明确为普通七层开槽箱；层数继续保留在产品字段",
    "001 思展钉箱": "名称明确为钉箱，抽样均有三维且无模切证据",
    "008 外箱无钉": "名称明确为普通外箱；无钉只描述结合工艺，不改变开槽结构",
    "006 华元外箱": "名称明确为普通外箱，抽样均有三维且无模切证据",
    "WZX 外纸箱": "名称明确为普通外纸箱，抽样均有三维且无模切证据",
    "NZX 内纸箱": "名称明确为普通内纸箱，抽样均有三维且无模切证据",
    "THWX 腾华外箱": "名称明确为普通外箱，抽样均有三维且无模切证据",
    "TDG 天地盖": "名称明确为天地盖整套",
    "TDGG 天地盖盖": "名称明确为天地盖的盖组件",
    "TDGD 天地盖底": "名称明确为天地盖的底组件",
    "WB 围板": "名称明确为围板",
    "MYG 满摇盖": "名称明确为满摇盖",
    "010 单瓦满摇盖": "名称明确为满摇盖；单瓦继续保留在层数／楞型字段",
    "BJX 半截箱": "名称明确为半开槽箱",
    "QCB 七层板": "名称明确为二维纸板组件；层数继续保留",
    "FJH 飞机盒": "飞机盒属于模切成型盒；本类型没有自动报料公式",
    "MQXX 模切小箱": "名称明确为模切小箱；本类型没有自动报料公式",
    "YXX 异型箱": "名称明确为异形箱；本类型没有自动报料公式",
}

NOT_BOX_ITEMS = {
    "00001 模具费": "费用项目，不是产品箱型",
    "EPE epe": "EPE 项目，不应强套纸箱结构",
    "FWB 蜂窝板": "蜂窝板项目，不应强套现有纸箱结构",
    "HP03 恒鹏护角": "护角产品，现有目录没有对应纸箱结构",
    "HRHJ 华融护角": "护角产品，现有目录没有对应纸箱结构",
    "ZHJ 纸护角": "纸护角产品，现有目录没有对应纸箱结构",
}

PENDING_RECOMMENDATIONS: dict[str, tuple[str, str]] = {
    "NH 天华内盒1": ("a1_0201", "建议普通开槽箱；需确认“内盒1”是否均为开槽结合结构"),
    "THNH1 腾华内盒1": ("a1_0201", "建议普通开槽箱；需确认“内盒1”是否均为开槽结合结构"),
    "007 华元内盒": ("a1_0201", "建议普通开槽箱；需确认是否存在模切内盒"),
    "009 内盒无钉": ("a1_0201", "建议普通开槽箱；需确认无钉是粘合还是无需结合"),
    "CTSNH 抽屉式内盒": ("die_cut_inner_box", "建议模切内盒；需确认是否为抽屉套盒及是否均需模切"),
    "XH 鞋盒": ("die_cut_inner_box", "建议模切内盒；需确认天地盖式鞋盒与模切折叠鞋盒是否混用"),
    "015 白卡内盒": ("die_cut_inner_box", "建议模切内盒；需确认白卡内盒实际结构"),
    "GD2 格挡2": ("divider", "建议隔板；需确认是单片格挡还是多片组装刀卡"),
    "016 井字架": ("divider", "建议隔板；需确认应作为 BOM 组装还是单个刀卡产品"),
    "WGX 无盖箱": ("half_slotted_carton", "建议半开槽箱；需确认开口方向及现有公式适用性"),
    "WDX 无底箱": ("half_slotted_carton", "建议半开槽箱；需确认开口方向及现有公式适用性"),
    "013 半截天地盖": ("a3_set", "建议天地盖；名称同时含半截，需确认是整套还是单组件"),
    "SC AB白卡": ("liner", "建议衬板；需确认它是二维白卡还是成型盒"),
    "模切盖 模切盖": ("top_cover", "建议独立天盖；需确认是否是天地盖组件"),
    "HP01 恒鹏模切1": ("die_cut_partition", "建议刀卡；二维记录较多，但需确认实际模切结构"),
    "HP02 恒鹏模切2": ("die_cut_partition", "建议刀卡；二维记录较多，但需确认实际模切结构"),
    "003 思展模切": ("die_cut_inner_box", "建议模切内盒；需根据图纸确认结构"),
    "011 佩特罗模": ("die_cut_inner_box", "建议模切内盒；需根据图纸确认结构"),
    "014 恒鹏模切3": ("die_cut_inner_box", "建议模切内盒；需根据图纸确认结构"),
    "LXBG 模切日本黄": ("die_cut_inner_box", "建议模切内盒；需根据图纸确认结构"),
    "SAT 驶安特模": ("die_cut_inner_box", "建议模切内盒；需根据图纸确认结构"),
    "TBM 天宝模": ("die_cut_inner_box", "建议模切内盒；需根据图纸确认结构"),
    "THYW 天华压外": ("a1_0201", "建议普通开槽箱；需确认“压外”的结构含义"),
    "004 豪迪纸箱": ("a1_0201", "建议普通开槽箱；客户式名称不足以确认结构"),
    "005 粘合成型": ("a1_0201", "建议普通开槽粘箱；需确认是否存在模切粘盒"),
    "JB 简包": ("other", "建议其他；需确认是纸箱、纸板还是简易包装服务"),
}

SYSTEM_SPECIAL = {
    "BOM组合": "现有 BOM 组合父件专用语义，不归入普通纸箱结构注册表",
}


def _connect_read_only(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _aggregate(connection: sqlite3.Connection) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT box_style,
               COUNT(*) AS product_count,
               SUM(CASE WHEN length_mm IS NOT NULL AND width_mm IS NOT NULL THEN 1 ELSE 0 END) AS dimensions_2d,
               SUM(CASE WHEN height_mm IS NOT NULL THEN 1 ELSE 0 END) AS dimensions_3d,
               SUM(CASE WHEN splice_mode = 'double' OR pieces_per_box = 2 THEN 1 ELSE 0 END) AS double_splice_count,
               SUM(CASE WHEN mold_tool_id IS NOT NULL OR TRIM(COALESCE(die_cut_path, '')) <> '' THEN 1 ELSE 0 END) AS die_evidence_count,
               SUM(CASE WHEN is_composite = 1 OR is_virtual_composite_parent = 1 THEN 1 ELSE 0 END) AS bom_count,
               GROUP_CONCAT(DISTINCT COALESCE(production_process, '')) AS process_values,
               GROUP_CONCAT(DISTINCT COALESCE(unit, '')) AS unit_values,
               GROUP_CONCAT(DISTINCT COALESCE(supply_mode, '')) AS supply_values
          FROM products
         WHERE is_active = 1 AND deleted_at IS NULL AND purged_at IS NULL
         GROUP BY box_style
         ORDER BY product_count DESC, box_style
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _split_values(value: object) -> list[str]:
    return sorted({item.strip() for item in str(value or "").split(",") if item.strip()})


def _classify(row: dict[str, object], names_by_code: dict[str, str]) -> dict[str, object]:
    raw = row["box_style"]
    style = str(raw or "").strip()
    code = box_type_code(style)
    result = {
        "legacy_name": style or None,
        "legacy_name_kind": (
            "null" if raw is None else "empty" if not style else "value"
        ),
        "product_count": int(row["product_count"]),
        "dimensions_2d": int(row["dimensions_2d"] or 0),
        "dimensions_3d": int(row["dimensions_3d"] or 0),
        "double_splice_count": int(row["double_splice_count"] or 0),
        "die_evidence_count": int(row["die_evidence_count"] or 0),
        "bom_count": int(row["bom_count"] or 0),
        "process_values": _split_values(row["process_values"]),
        "unit_values": _split_values(row["unit_values"]),
        "supply_values": _split_values(row["supply_values"]),
        "preserve": "产品ID、客户、编号、尺寸、报料尺寸、单双拼、钉/粘、层数、BOM、模具、图纸及历史快照",
    }
    if not style:
        result.update(status="missing", target_code=None, target_name=None, basis="箱型为空，必须逐产品核对")
    elif style in NOT_BOX_ITEMS:
        from app.services.legacy_product_classification import classification
        target = classification(SimpleNamespace(box_style=style))
        result.update(status="not_box", target_code=target["category_code"], target_name=target["label"],
                      basis="老板2026-09-27确认；外购采购资料仍需按真实来源完善；费用项目不进入产品箱型")
    elif style in SYSTEM_SPECIAL:
        result.update(status="system_special", target_code=None, target_name=None, basis=SYSTEM_SPECIAL[style])
    elif style in CONFIRMED_LEGACY_NAMES:
        result.update(
            status="confirmed_legacy",
            target_code=code,
            target_name=canonical_box_style(style),
            basis=CONFIRMED_LEGACY_NAMES[style],
        )
    elif style in PENDING_RECOMMENDATIONS and code is not None:
        result.update(status="confirmed_legacy", target_code=code, target_name=names_by_code[code],
                      basis="老板2026-09-27确认：指定项按最新指示，其余采纳推荐；保留原工艺及物理尺寸")
    elif code is not None:
        result.update(
            status="existing_recognized",
            target_code=code,
            target_name=names_by_code[code],
            basis="现有规范名称或既有显式别名",
        )
    else:
        suggested_code, basis = PENDING_RECOMMENDATIONS.get(
            style, (None, "现有聚合字段不足以确认结构，需查看代表产品图纸或现场工艺")
        )
        result.update(
            status="pending_confirmation",
            target_code=suggested_code,
            target_name=names_by_code.get(suggested_code),
            basis=basis,
        )
    return result


def _markdown(payload: dict[str, object]) -> str:
    summary = payload["summary"]
    lines = [
        "# 旧箱型名称归并预览",
        "",
        f"数据源：`{payload['database']}`（SQLite 只读 `mode=ro`、`query_only=ON`）。",
        "",
        "本清单只改变代码中的识别规则，不批量改写正式产品或历史订单。钉／粘、层数、单双拼、尺寸、BOM、模具和图纸继续保留在原字段。",
        "",
        "## 汇总",
        "",
        f"- 有效产品：{summary['active_products']} 条；原始箱型值：{summary['distinct_values']} 种。",
        f"- 现有规范／既有别名：{summary['existing_recognized_products']} 条。",
        f"- 本轮确定归并：{summary['confirmed_legacy_products']} 条。",
        f"- 待确认：{summary['pending_products']} 条；非纸箱／系统专用／缺失：{summary['held_products']} 条。",
        "",
        "## 全量清单",
        "",
        "| 旧名称 | 数量 | 状态 | 推荐／目标箱型 | 依据 |",
        "| --- | ---: | --- | --- | --- |",
    ]
    labels = {
        "existing_recognized": "现有已识别",
        "confirmed_legacy": "确定归并",
        "pending_confirmation": "待确认",
        "not_box": "非纸箱，保留",
        "system_special": "系统专用，保留",
        "missing": "缺失，逐条核对",
    }
    for item in payload["items"]:
        name = item["legacy_name"] or (
            "（NULL）" if item["legacy_name_kind"] == "null" else "（空字符串）"
        )
        target = item["target_name"] or "—"
        basis = str(item["basis"]).replace("|", "／")
        lines.append(
            f"| {name} | {item['product_count']} | {labels[item['status']]} | {target} | {basis} |"
        )
    lines.extend(
        [
            "",
            "## 已确认归类及剩余资料",
            "",
            "2026-09-27老板已明确归类并采纳其余推荐，不再重复询问分类方案。",
            "",
            "1. 天华内盒1、腾华内盒1、抽屉式内盒归异形箱；简包归刀卡；AB白卡归模切内盒。",
            "2. 护角、EPE、蜂窝板归现有外购类别；未绑定供应商的产品明确提示待完善，禁止猜填采购合同。",
            "3. 模具费为费用项目，不在产品箱型目录和新订单产品选择器中提供。",
            "4. 其他旧名称按已确认推荐识别；原始别名及历史快照保留，旧名可搜索。",
            "5. 箱型空白、BOM专用语义保持；本轮不据分类重算物理尺寸、采购张数或历史金额。",
            "",
        ]
    )
    return "\n".join(lines)


def build_preview(database: Path) -> dict[str, object]:
    names_by_code = {rule.code: rule.display_name for rule in BOX_TYPE_RULES}
    with _connect_read_only(database) as connection:
        items = [_classify(row, names_by_code) for row in _aggregate(connection)]
    counts: dict[str, int] = {}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + int(item["product_count"])
    return {
        "database": str(database.resolve()),
        "summary": {
            "active_products": sum(int(item["product_count"]) for item in items),
            "distinct_values": len(items),
            "existing_recognized_products": counts.get("existing_recognized", 0),
            "confirmed_legacy_products": counts.get("confirmed_legacy", 0),
            "pending_products": counts.get("pending_confirmation", 0),
            "held_products": sum(
                counts.get(status, 0) for status in ("not_box", "system_special", "missing")
            ),
            "status_product_counts": counts,
        },
        "items": items,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    payload = build_preview(args.db)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "box-style-mapping-preview.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "box-style-mapping-preview.md").write_text(
        _markdown(payload), encoding="utf-8"
    )
    print(json.dumps(payload["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
