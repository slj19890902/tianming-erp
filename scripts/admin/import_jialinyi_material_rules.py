from __future__ import annotations

import argparse
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import load_settings
from app.core.database import backup_to_nas


SUPPLIER = "苏州嘉林亿"
SOURCE_DATE = date(2026, 4, 14)
SOURCE = "苏州嘉林亿报价表 2026-04-14"
PAPER_CODES = {
    "X": (230, "进口AAA级俄卡"),
    "R": (170, "进口AAA级俄卡"),
    "Y": (170, "国产AA级白牛"),
    "F": (180, "进口木浆牛卡"),
    "L": (160, "进口木浆牛卡"),
    "G": (140, "进口木浆牛卡"),
    "K": (240, "国产AA级牛卡"),
    "J": (190, "国产AA级牛卡"),
    "E": (170, "国产A级牛卡"),
    "A": (150, "国产A级牛卡"),
    "D": (130, "国产A级牛卡"),
    "B": (100, "国产A级牛卡"),
    "C": (80, "国产A级牛卡"),
    "1": (50, "国产普瓦"),
    "4": (100, "国产A级施胶高瓦"),
    "6": (130, "国产A级施胶高瓦"),
    "9": (150, "国产A级施胶高瓦"),
    "7": (170, "国产A级施胶高瓦"),
}
THREE_LAYER_PRICES = {
    "C4C":"1.21","B4B":"1.31","D4C":"1.34","D4B":"1.39","D4D":"1.47",
    "A4B":"1.44","A4D":"1.52","A4A":"1.57","E4D":"1.59","E4A":"1.64",
    "E4E":"1.71","J4A":"1.76","J4E":"1.83","J4J":"1.95","G4B":"1.52",
    "G4D":"1.60","G4A":"1.65","L4D":"1.66","L4A":"1.71","L4E":"1.78",
    "F4D":"1.74","F4A":"1.79","F4E":"1.86","F4J":"1.98","K4A":"1.88",
    "K4E":"1.95","K4J":"2.07","K4K":"2.19","R4D":"1.81","R4A":"1.86",
    "R4E":"1.93","R4J":"2.05","X4A":"2.11","X4E":"2.18","X4R":"2.40",
    "X4J":"2.30","X4K":"2.42","X4X":"2.65","Y4D":"1.79","Y4A":"1.84",
    "Y4E":"1.91","Y4R":"2.13","Y4Y":"2.11",
}
FIVE_LAYER_PRICES = {
    f"{code[0]}414{code[2]}": Decimal(price) + Decimal("0.71")
    for code, price in THREE_LAYER_PRICES.items()
}
SUBSTITUTION_RULES = [
    *[("corrugated_b", "4", code, value) for code, value in (("6","0.10"),("9","0.17"),("7","0.25"))],
    *[("corrugated_a", "4", code, value) for code, value in (("6","0.10"),("9","0.18"),("7","0.26"))],
    *[("core", "1", code, value) for code, value in (
        ("4","0.10"),("6","0.17"),("9","0.22"),("7","0.28"),("C","0.05"),
        ("B","0.10"),("D","0.18"),("A","0.23"),("E","0.30"),("J","0.42"),
        ("K","0.54"),("R","0.52"),("X","0.77"),
    )],
]
CONFIGS = [
    ("min_trim_mm","225","active","最小修边：225mm",False),
    ("a_flute_core_limit","C","active","A楞质量限制：9号瓦楞及以上时，芯纸最低使用C号纸",False),
    ("small_order_rule","100m","active","小单规则：100米以下接受随机配料并按牌价计价",False),
    ("min_cut_width_mm","270","active","最小切宽：270mm",False),
    ("min_cut_length_mm","500","active","最小切长：500mm",False),
    ("short_length_split_surcharge",None,"pending_confirmation","分纸加价金额待确认，暂不参与自动报价",False),
    ("over_3000mm_surcharge",None,"inactive","长度超过3000mm存在加价规则，本轮暂不自动计算",False),
]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _paper_name(value: str) -> str:
    return re.sub(r"^\s*\d+\s*g\s*", "", value, flags=re.IGNORECASE).strip()


def build_plan(connection: sqlite3.Connection) -> dict:
    required = {
        "supplier_paper_codes",
        "supplier_material_base_prices",
        "supplier_material_substitution_rules",
        "supplier_material_rule_configs",
    }
    tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing_tables = sorted(required - tables)
    if missing_tables:
        raise RuntimeError(f"请先执行 Alembic 迁移，缺少表：{', '.join(missing_tables)}")

    paper_plan = []
    existing_paper = {
        row[0]: row[1:]
        for row in connection.execute(
            "SELECT code_char,paper_name,gram_weight,is_active "
            "FROM supplier_paper_codes WHERE supplier_name=?",
            (SUPPLIER,),
        )
    }
    for code, (weight, name) in PAPER_CODES.items():
        old = existing_paper.get(code)
        if old is None:
            status = "add"
        elif _paper_name(old[0]) == name and int(old[1]) == weight and old[2]:
            status = "skip"
        else:
            status = "conflict"
        paper_plan.append({"code":code,"gram_weight":weight,"paper_name":name,"status":status,"existing":old})

    prices = {
        **{code: (3, Decimal(value)) for code, value in THREE_LAYER_PRICES.items()},
        **{code: (5, Decimal(value)) for code, value in FIVE_LAYER_PRICES.items()},
    }
    existing_base = {
        row[0]: Decimal(str(row[1]))
        for row in connection.execute(
            "SELECT material_code,base_price FROM supplier_material_base_prices "
            "WHERE supplier_name=? AND effective_date=?",
            (SUPPLIER, SOURCE_DATE.isoformat()),
        )
    }
    base_plan = [
        {
            "code": code, "layer_count": layer, "base_price": str(price),
            "status": "add" if code not in existing_base else (
                "skip" if existing_base[code] == price else "conflict"
            ),
        }
        for code, (layer, price) in prices.items()
    ]
    existing_rules = {
        (r[0],r[1],r[2]): Decimal(str(r[3]))
        for r in connection.execute(
            "SELECT rule_type,from_code,to_code,price_delta "
            "FROM supplier_material_substitution_rules "
            "WHERE supplier_name=? AND effective_date=?",
            (SUPPLIER, SOURCE_DATE.isoformat()),
        )
    }
    rule_plan = []
    for rule_type, from_code, to_code, value in SUBSTITUTION_RULES:
        delta = Decimal(value)
        old = existing_rules.get((rule_type,from_code,to_code))
        status = "add" if old is None else ("skip" if old == delta else "conflict")
        rule_plan.append({"rule_type":rule_type,"from_code":from_code,"to_code":to_code,"price_delta":str(delta),"status":status})

    existing_configs = {
        row[0]: row[1:]
        for row in connection.execute(
            "SELECT rule_key,rule_value,status,description,participates_in_pricing "
            "FROM supplier_material_rule_configs WHERE supplier_name=?",
            (SUPPLIER,),
        )
    }
    config_plan = []
    for key,value,status,description,participates in CONFIGS:
        old = existing_configs.get(key)
        expected = (value,status,description,int(participates))
        state = "add" if old is None else ("skip" if tuple(old) == expected else "conflict")
        config_plan.append({"rule_key":key,"rule_value":value,"status_value":status,"description":description,"participates_in_pricing":participates,"status":state})

    current = {
        row[0]: (Decimal(str(row[1])), row[2])
        for row in connection.execute(
            "SELECT code,quote_price,quote_date FROM materials "
            "WHERE supplier_name=? AND quote_price IS NOT NULL",
            (SUPPLIER,),
        )
    }
    price_conflicts = [
        {
            "code": code, "base_price": str(price), "current_price": str(current[code][0]),
            "current_date": current[code][1], "reason": "2026-06-26统一涨价5%",
        }
        for code, (_, price) in prices.items()
        if code in current and current[code][0] != price
    ]
    return {
        "supplier": SUPPLIER,
        "source_date": SOURCE_DATE.isoformat(),
        "paper_codes": paper_plan,
        "base_prices": base_plan,
        "substitution_rules": rule_plan,
        "configs": config_plan,
        "current_price_conflicts": price_conflicts,
        "protected_material_price_count": connection.execute(
            "SELECT COUNT(*) FROM materials WHERE supplier_name=?", (SUPPLIER,)
        ).fetchone()[0],
    }


def apply_plan(connection: sqlite3.Connection, plan: dict) -> None:
    for row in plan["paper_codes"]:
        if row["status"] == "add":
            connection.execute(
                "INSERT INTO supplier_paper_codes "
                "(supplier_name,code_char,paper_name,gram_weight,paper_role,remark,is_active) "
                "VALUES (?,?,?,?,?,?,1)",
                (SUPPLIER,row["code"],row["paper_name"],row["gram_weight"],"通用",SOURCE),
            )
    for row in plan["base_prices"]:
        if row["status"] == "add":
            connection.execute(
                "INSERT INTO supplier_material_base_prices "
                "(supplier_name,material_code,layer_count,base_price,effective_date,source,is_active) "
                "VALUES (?,?,?,?,?,?,1)",
                (SUPPLIER,row["code"],row["layer_count"],row["base_price"],SOURCE_DATE.isoformat(),SOURCE),
            )
    for row in plan["substitution_rules"]:
        if row["status"] == "add":
            connection.execute(
                "INSERT INTO supplier_material_substitution_rules "
                "(supplier_name,rule_type,from_code,to_code,price_delta,effective_date,source,is_active) "
                "VALUES (?,?,?,?,?,?,?,1)",
                (SUPPLIER,row["rule_type"],row["from_code"],row["to_code"],row["price_delta"],SOURCE_DATE.isoformat(),SOURCE),
            )
    for row in plan["configs"]:
        if row["status"] == "add":
            connection.execute(
                "INSERT INTO supplier_material_rule_configs "
                "(supplier_name,rule_key,rule_value,status,description,participates_in_pricing) "
                "VALUES (?,?,?,?,?,?)",
                (SUPPLIER,row["rule_key"],row["rule_value"],row["status_value"],row["description"],int(row["participates_in_pricing"])),
            )


def report_markdown(plan: dict, *, mode: str, backup: str | None) -> str:
    def counts(key: str) -> str:
        rows = plan[key]
        return ", ".join(f"{state}={sum(r['status']==state for r in rows)}" for state in ("add","skip","conflict"))
    return "\n".join([
        "# 嘉林亿材质规则导入报告",
        "",
        f"- 模式：{mode}",
        f"- 供应商：{plan['supplier']}",
        f"- 规则基准日期：{plan['source_date']}",
        f"- 基础纸种代码：{counts('paper_codes')}",
        f"- 基础报价：{counts('base_prices')}",
        f"- 换纸规则：{counts('substitution_rules')}",
        f"- 规则备注：{counts('configs')}",
        f"- 正式材质价格保护数量：{plan['protected_material_price_count']}",
        f"- 当前价与图片基准价不同：{len(plan['current_price_conflicts'])}（原因：2026-06-26统一涨价5%，未覆盖）",
        f"- 备份：{backup or 'dry-run未写库，无需备份'}",
        "",
        "## 需人工确认",
        "",
        "- 长度低于500mm分纸加价金额待确认，暂不参与自动报价。",
        "- 长度超过3000mm加价本轮不参与自动成本。",
        "",
        "## 冲突基础代码",
        "",
        *[f"- {r['code']}：现有={r['existing']}，规则={r['gram_weight']}g {r['paper_name']}" for r in plan["paper_codes"] if r["status"]=="conflict"],
        "",
        "## 基础纸种代码明细",
        "",
        *[f"- [{r['status']}] {r['code']} = {r['gram_weight']}g {r['paper_name']}" for r in plan["paper_codes"]],
        "",
        "## 基础报价明细",
        "",
        *[f"- [{r['status']}] {r['code']} ({r['layer_count']}层) = {r['base_price']}元/㎡" for r in plan["base_prices"]],
        "",
        "## 换纸规则明细",
        "",
        *[f"- [{r['status']}] {r['rule_type']} {r['from_code']}换{r['to_code']} = +{r['price_delta']}元/㎡" for r in plan["substitution_rules"]],
        "",
        "## 正式当前价保护明细",
        "",
        *[f"- {r['code']}：基准价{r['base_price']}，当前价{r['current_price']}（{r['current_date']}，{r['reason']}），未覆盖" for r in plan["current_price_conflicts"]],
    ])


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--report-dir", type=Path)
    args = parser.parse_args()
    settings = load_settings()
    database = (args.database or settings.database_path).resolve()
    before_hash = _sha256(database)
    uri = f"file:{database.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        plan = build_plan(connection)
    backup_path = None
    if args.apply:
        backup = backup_to_nas(
            source_path=database,
            backup_dir=(args.backup_dir or ROOT / "data" / "backups"),
            filename_suffix="_JIALINYI_RULES_BEFORE_IMPORT",
            keep_regular=100,
        )
        backup_path = str(backup.path)
        with sqlite3.connect(database) as connection:
            apply_plan(connection, plan)
            connection.commit()
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("导入后数据库完整性检查失败")
    after_hash = _sha256(database)
    if not args.apply and before_hash != after_hash:
        raise RuntimeError("dry-run意外修改了数据库")
    report_dir = args.report_dir or ROOT / "docs" / "material_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report = report_dir / f"JIALINYI_MATERIAL_RULES_{'APPLY' if args.apply else 'DRY_RUN'}_{stamp}.md"
    report.write_text(report_markdown(plan, mode="apply" if args.apply else "dry-run", backup=backup_path), encoding="utf-8")
    print(json.dumps({"mode":"apply" if args.apply else "dry-run","report":str(report),"database_changed":before_hash!=after_hash,"plan":plan},ensure_ascii=False,default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
