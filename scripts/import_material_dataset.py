"""scripts/import_material_dataset.py

Safe importer for the new 3-supplier carton material dataset
(`三家供应商纸板材质数据集_ClaudeCode版.xlsx`) into the ERP `materials` table.

Design constraints (v0.19.2 Hotfix, NOT v0.19.2-C):
  * Only touches the `materials` master table. Never products / orders / legacy_*.
  * Never deletes a material that is still referenced by a product/order.
  * --dry-run by default: prints a full diff and writes nothing.
  * --apply: backs up the live DB first (with integrity_check), then upserts.
  * Natural key = material code (globally UNIQUE in schema). Cross-supplier
    duplicate codes do not exist in this dataset (audited = 0).

Usage:
    python -X utf8 scripts/import_material_dataset.py --file "三家供应商纸板材质数据集_ClaudeCode版.xlsx" --dry-run
    python -X utf8 scripts/import_material_dataset.py --file "三家供应商纸板材质数据集_ClaudeCode版.xlsx" --apply
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import shutil
import sqlite3
import sys
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

import openpyxl
from sqlalchemy import select
from sqlalchemy.orm import Session

# project root on path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.database import create_sqlite_engine  # noqa: E402
from app.models.material import Material  # noqa: E402

SUPPLIER_SHEETS = ("昆山鸣朋", "苏州佳丰", "苏州嘉林亿")
DB_PATH = ROOT / "data" / "carton_erp.sqlite3"
BACKUP_DIR = ROOT / "data" / "backups"

# 供应商别名归一 → 标准显示名
SUPPLIER_ALIASES = {
    "苏州嘉林亿包装科技有限公司": "苏州嘉林亿",
    "嘉林亿": "苏州嘉林亿",
    "苏州嘉林亿": "苏州嘉林亿",
    "鸣朋": "昆山鸣朋",
    "昆山鸣朋纸板": "昆山鸣朋",
    "昆山鸣朋": "昆山鸣朋",
    "佳丰": "苏州佳丰",
    "苏州佳丰纸板": "苏州佳丰",
    "苏州佳丰": "苏州佳丰",
}


def normalize_supplier(name: str | None) -> str | None:
    if not name:
        return name
    s = str(name).strip()
    if s in SUPPLIER_ALIASES:
        return SUPPLIER_ALIASES[s]
    for key, std in SUPPLIER_ALIASES.items():
        if key in s:
            return std
    return s


def derive_flute(code: str, layer: int | None, source: str) -> str | None:
    """楞型解析：

    - 特价单后缀 `-B/E`（三层）→ 'B/E'  （三层 B瓦 或 E瓦，不是五层 BE）
    - 特价单后缀 `-AB/EB`（五层）→ 'AB/BE'（五层 AB瓦 或 BE瓦）
    - 来源含「五层BA/EB楞组合」→ 'AB/BE'
    - 其余三层 → 'B'
    - 其余五层 → 'AB'
    """
    c = (code or "").upper()
    if c.endswith("-B/E"):
        return "B/E"
    if c.endswith("-AB/EB"):
        return "AB/BE"
    if layer == 5:
        if "BA/EB" in (source or "") or "EB楞" in (source or ""):
            return "AB/BE"
        return "AB"
    if layer == 3:
        return "B"
    return None  # 待确认（七层等）


def parse_layer(value) -> int | None:
    if value is None:
        return None
    m = re.search(r"\d+", str(value))
    return int(m.group()) if m else None


def parse_price(value) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def parse_quote_date(source: str) -> dt.date | None:
    if not source:
        return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(source))
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _g(value) -> str | None:
    """单格克重 → '150g'，空 → None。"""
    if value is None or str(value).strip() == "":
        return None
    s = str(value).strip()
    m = re.search(r"\d+(?:\.\d+)?", s)
    return f"{m.group()}g" if m else None


def build_weight_structure(row: tuple, layer: int | None) -> str | None:
    """逐层克重结构。

    三层: 面纸(D) / 第一芯纸(E) / 里纸(H)
    五层: 面纸(D) / 第一芯纸(E) / 中夹纸(F) / 第二芯纸(G) / 里纸(H)
    列序: A供应商 B代码 C层数 D面纸 E第一芯 F中夹 G第二芯 H里纸 I价 J来源 K待确认 L纸种
    """
    face, core1, mid, core2, liner = row[3], row[4], row[5], row[6], row[7]
    if layer == 5:
        cells = [face, core1, mid, core2, liner]
    else:  # 三层及其他默认三层结构
        cells = [face, core1, liner]
    parts = [_g(c) for c in cells]
    parts = [p for p in parts if p]
    return "/".join(parts) or None


class PlannedRow:
    __slots__ = ("code", "supplier", "layer", "flute", "weights", "price",
                 "price_unit", "quote_date", "paper", "remarks", "source", "confirm")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


def load_xlsx(path: Path) -> list[PlannedRow]:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    planned: list[PlannedRow] = []
    for sheet in SUPPLIER_SHEETS:
        for row in list(wb[sheet].iter_rows(values_only=True))[1:]:
            code = row[1]
            if not code:
                continue
            code = str(code).strip()
            supplier = normalize_supplier(row[0]) or sheet
            layer = parse_layer(row[2])
            source = str(row[9] or "").strip()
            confirm = str(row[10] or "").strip()
            paper = str(row[11] or "").strip() or None
            flute = derive_flute(code, layer, source)
            remarks_bits = []
            if source:
                remarks_bits.append(f"来源：{source}")
            if confirm:
                remarks_bits.append(f"待确认：{confirm}")
            planned.append(PlannedRow(
                code=code,
                supplier=supplier,
                layer=layer,
                flute=flute,
                weights=build_weight_structure(row, layer),
                price=parse_price(row[8]),
                price_unit="元/㎡",
                quote_date=parse_quote_date(source),
                paper=(paper[:200] if paper else None),
                remarks="｜".join(remarks_bits) or None,
                source=source,
                confirm=confirm,
            ))
    return planned


def validate(planned: list[PlannedRow]) -> dict:
    codes = Counter(p.code for p in planned)
    return {
        "dup_codes": [c for c, n in codes.items() if n > 1],
        "empty_code": sum(1 for p in planned if not p.code),
        "empty_price": [p.code for p in planned if p.price is None],
        "empty_weight": [p.code for p in planned if not p.weights],
        "unresolved_flute": [p.code for p in planned if not p.flute],
        "confirm_items": [p.code for p in planned if p.confirm],
    }


def backup_db() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / f"carton_erp_BEFORE_MATERIAL_DATASET_IMPORT_{stamp}.sqlite3"
    shutil.copy2(DB_PATH, dest)
    if not dest.exists():
        raise RuntimeError("备份文件未生成，终止写库")
    src_size, dst_size = DB_PATH.stat().st_size, dest.stat().st_size
    if dst_size < src_size * 0.95:
        raise RuntimeError(f"备份大小异常：源 {src_size} vs 备份 {dst_size}")
    conn = sqlite3.connect(dest)
    try:
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    if ok != "ok":
        raise RuntimeError(f"备份 integrity_check 失败：{ok}")
    print(f"[backup] {dest}  ({dst_size} bytes, integrity_check=ok)")
    return dest


def referenced_codes(session: Session) -> set[str]:
    """被产品/订单引用的材质 code（用于「不删除仍被引用」保护）。"""
    refs: set[str] = set()
    try:
        from app.models.product import Product
        for (mid,) in session.execute(
            select(Product.material_id).where(Product.material_id.is_not(None))
        ).all():
            mat = session.get(Material, mid)
            if mat:
                refs.add(mat.code)
    except Exception:
        pass
    return refs


def run(path: Path, apply: bool) -> None:
    planned = load_xlsx(path)
    checks = validate(planned)

    by_supplier = Counter(p.supplier for p in planned)
    by_layer = Counter(p.layer for p in planned)
    by_flute = Counter(p.flute for p in planned)

    print("=" * 70)
    print(f"数据集：{path.name}")
    print(f"模式：{'APPLY（写库）' if apply else 'DRY-RUN（只读，不写库）'}")
    print(f"解析材质总数：{len(planned)}")
    print(f"按供应商：{dict(by_supplier)}")
    print(f"按层数：{dict(by_layer)}")
    print(f"按楞型：{dict(by_flute)}")
    print(f"重复代码：{checks['dup_codes'] or '无'}")
    print(f"空代码：{checks['empty_code']}")
    print(f"空报价：{len(checks['empty_price'])} {checks['empty_price'][:5]}")
    print(f"空克重：{len(checks['empty_weight'])} {checks['empty_weight'][:5]}")
    print(f"楞型未解析（待确认）：{len(checks['unresolved_flute'])} {checks['unresolved_flute'][:5]}")
    print(f"待确认说明条数：{len(checks['confirm_items'])}")

    engine = create_sqlite_engine(DB_PATH)
    with Session(engine) as session:
        existing = {m.code: m for m in session.scalars(select(Material)).all()}
        refs = referenced_codes(session)

        xlsx_codes = {p.code for p in planned}
        to_insert = [p for p in planned if p.code not in existing]
        to_update = [p for p in planned if p.code in existing]
        # 旧库中、不在新版 xlsx 内的材质：保留，仅归一供应商名
        orphans = [m for code, m in existing.items() if code not in xlsx_codes]

        # 供应商名归一：旧库中非标准名（如 全称 / 简称）
        rename = [
            (m.code, m.supplier_name, normalize_supplier(m.supplier_name))
            for m in existing.values()
            if normalize_supplier(m.supplier_name) != m.supplier_name
        ]

        print("-" * 70)
        print(f"将新增：{len(to_insert)} 条")
        print(f"将更新：{len(to_update)} 条")
        print(f"将保留(旧库不在xlsx)：{len(orphans)} 条 -> {[m.code for m in orphans][:10]}")
        print(f"供应商名归一：{len(rename)} 条")
        for code, old, new in rename[:10]:
            print(f"    {code}: {old!r} -> {new!r}")
        print(f"被产品引用、需保护不删除的旧材质：{len(refs)} {sorted(refs)[:10]}")
        # 三家供应商最终库内数量预测
        final_supplier = Counter()
        for p in planned:
            final_supplier[p.supplier] += 1
        for m in orphans:
            final_supplier[normalize_supplier(m.supplier_name)] += 1
        print(f"导入后各供应商预测数量：{dict(final_supplier)}")

        if not apply:
            print("-" * 70)
            print("DRY-RUN 完成，未写入数据库。加 --apply 执行写库（会先自动备份）。")
            return

        # ---- APPLY ----
        print("-" * 70)
        backup_db()
        now = dt.datetime.utcnow()
        ins = upd = 0
        for p in planned:
            m = existing.get(p.code)
            if m is None:
                m = Material(code=p.code)
                session.add(m)
                ins += 1
            else:
                upd += 1
            m.supplier_name = p.supplier
            m.layer_count = p.layer
            m.flute_type = p.flute
            m.basis_weight_description = p.weights
            m.quote_price = p.price
            m.price_unit = p.price_unit
            m.quote_date = p.quote_date
            m.paper_composition = p.paper
            m.remarks = p.remarks
            m.is_active = True
            m.updated_at = now
        # 旧库孤儿：保留，仅归一供应商名（不删除、不停用，因可能被引用）
        for m in orphans:
            m.supplier_name = normalize_supplier(m.supplier_name)
        session.commit()
        print(f"[apply] 新增 {ins} 条，更新 {upd} 条，保留旧库 {len(orphans)} 条。")

        # 写库后核对
        final = Counter(
            m.supplier_name for m in session.scalars(select(Material)).all()
        )
        print(f"[apply] 写库后各供应商数量：{dict(final)}")
        print(f"[apply] materials 总数：{sum(final.values())}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="xlsx 数据集路径")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="只读预览（默认）")
    g.add_argument("--apply", action="store_true", help="写库（先自动备份）")
    args = ap.parse_args()
    path = Path(args.file)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        raise SystemExit(f"找不到数据集文件：{path}")
    run(path, apply=bool(args.apply))


if __name__ == "__main__":
    main()
