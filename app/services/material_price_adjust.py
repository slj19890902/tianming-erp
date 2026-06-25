"""Supplier unified price-adjustment service (v0.19.2-B).

调价规则（手动输入百分比，无 OCR）：
  "+5%" / "5"  -> 上调 5%  (price * 1.05)
  "-10%"       -> 下调 10% (price * 0.90)
  "-3"         -> 下调 3%
报价保留两位小数；报价日期更新为生效日期。

写库前必须备份数据库（带 integrity_check），并写入：
  material_price_adjustment_batches  一条批次
  material_price_history             每条被调价材质一条历史

只改 materials.quote_price / quote_date，绝不改历史订单/产品/legacy_*。
"""
from __future__ import annotations

import datetime as dt
import re
import shutil
import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import load_settings
from app.models.material import Material
from app.models.material_price_history import (
    MaterialPriceAdjustmentBatch,
    MaterialPriceHistory,
)
from app.models.product import Product

TWO_PLACES = Decimal("0.01")


class PriceAdjustError(ValueError):
    pass


def parse_adjust_percent(raw: object) -> Decimal:
    """解析调价幅度字符串/数字 → 百分比数（+5% → 5, -10% → -10, 5 → 5）。"""
    if raw is None or str(raw).strip() == "":
        raise PriceAdjustError("调价幅度不能为空")
    s = str(raw).strip().replace("％", "%").replace(" ", "")
    s = s.rstrip("%")
    m = re.fullmatch(r"[+-]?\d+(?:\.\d+)?", s)
    if not m:
        raise PriceAdjustError(f"无法解析调价幅度：{raw!r}")
    pct = Decimal(s)
    if pct <= -100:
        raise PriceAdjustError("调价幅度不能使价格 ≤ 0")
    return pct


def compute_new_price(old_price: Decimal | None, percent: Decimal) -> Decimal | None:
    if old_price is None:
        return None
    factor = (Decimal("100") + percent) / Decimal("100")
    return (Decimal(old_price) * factor).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _referenced_material_ids(session: Session) -> set[int]:
    rows = session.execute(
        select(Product.material_id).where(Product.material_id.is_not(None))
    ).all()
    return {mid for (mid,) in rows}


def select_affected_materials(session: Session, supplier_name: str) -> list[Material]:
    """默认调整：该供应商「启用材质」 + 「仍被常用箱引用的材质」。

    不调整：已停用且未被任何常用箱引用的废弃材质。
    materials 表有 is_active 字段，故按此规则执行。
    """
    if not supplier_name:
        raise PriceAdjustError("必须指定供应商")
    rows = list(
        session.scalars(
            select(Material).where(Material.supplier_name == supplier_name)
        ).all()
    )
    referenced = _referenced_material_ids(session)
    result = [
        m
        for m in rows
        if m.quote_price is not None and (m.is_active or m.id in referenced)
    ]
    return result


def _detail_row(m: Material, new_price: Decimal | None, eff: dt.date | None) -> dict:
    old = None if m.quote_price is None else Decimal(m.quote_price)
    delta = None if (old is None or new_price is None) else (new_price - old)
    return {
        "material_id": m.id,
        "material_code": m.code,
        "old_price": None if old is None else float(old),
        "new_price": None if new_price is None else float(new_price),
        "delta": None if delta is None else float(delta),
        "old_quote_date": m.quote_date.isoformat() if m.quote_date else None,
        "new_quote_date": eff.isoformat() if eff else None,
    }


def preview(
    session: Session,
    *,
    supplier_name: str,
    adjust_percent_raw: object,
    effective_date: dt.date | None,
) -> dict:
    percent = parse_adjust_percent(adjust_percent_raw)
    affected = select_affected_materials(session, supplier_name)
    new_prices = {m.id: compute_new_price(m.quote_price, percent) for m in affected}

    old_vals = [Decimal(m.quote_price) for m in affected if m.quote_price is not None]
    new_vals = [v for v in new_prices.values() if v is not None]
    details = [
        _detail_row(m, new_prices[m.id], effective_date) for m in affected[:10]
    ]
    return {
        "supplier_name": supplier_name,
        "adjust_percent": float(percent),
        "affected_count": len(affected),
        "old_min": float(min(old_vals)) if old_vals else None,
        "old_max": float(max(old_vals)) if old_vals else None,
        "new_min": float(min(new_vals)) if new_vals else None,
        "new_max": float(max(new_vals)) if new_vals else None,
        "details": details,
    }


def backup_database() -> Path:
    """备份 data/carton_erp.sqlite3 → data/backups/..._BEFORE_SUPPLIER_PRICE_ADJUST_*。"""
    db_path = load_settings().database_path
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = backup_dir / f"carton_erp_BEFORE_SUPPLIER_PRICE_ADJUST_{stamp}.sqlite3"
    shutil.copy2(db_path, dest)
    if not dest.exists():
        raise PriceAdjustError("备份文件未生成，终止写库")
    if dest.stat().st_size < db_path.stat().st_size * 0.95:
        raise PriceAdjustError("备份大小异常，终止写库")
    conn = sqlite3.connect(dest)
    try:
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    if ok != "ok":
        raise PriceAdjustError(f"备份 integrity_check 失败：{ok}")
    return dest


def apply(
    session: Session,
    *,
    supplier_name: str,
    adjust_percent_raw: object,
    effective_date: dt.date | None,
    remark: str | None,
    operator: str | None,
) -> dict:
    """执行调价：先备份，再写 materials + 批次 + 历史。"""
    percent = parse_adjust_percent(adjust_percent_raw)
    affected = select_affected_materials(session, supplier_name)
    if not affected:
        raise PriceAdjustError("没有符合条件的材质可调价")

    backup_path = backup_database()

    batch = MaterialPriceAdjustmentBatch(
        supplier_name=supplier_name,
        adjust_percent=percent,
        effective_date=effective_date,
        affected_count=len(affected),
        remark=remark,
        operator=operator,
        backup_path=str(backup_path),
    )
    session.add(batch)
    session.flush()  # batch.id

    for m in affected:
        old_price = None if m.quote_price is None else Decimal(m.quote_price)
        new_price = compute_new_price(m.quote_price, percent)
        session.add(
            MaterialPriceHistory(
                material_id=m.id,
                supplier_name=m.supplier_name,
                material_code=m.code,
                old_price=old_price,
                new_price=new_price,
                adjust_percent=percent,
                effective_date=effective_date,
                adjust_reason=remark,
                operator=operator,
                batch_id=batch.id,
            )
        )
        m.quote_price = new_price
        if effective_date is not None:
            m.quote_date = effective_date

    session.commit()
    return {
        "batch_id": batch.id,
        "supplier_name": supplier_name,
        "adjust_percent": float(percent),
        "affected_count": len(affected),
        "backup_path": str(backup_path),
        "effective_date": effective_date.isoformat() if effective_date else None,
    }
