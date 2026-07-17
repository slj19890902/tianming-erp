from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import re
import sys
from dataclasses import dataclass

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.database import SessionLocal
from app.models.material import Material
from app.models.product import Product
from scripts.master_data_write_guard import reject_legacy_master_data_write_if_versioned


@dataclass
class Stats:
    materials_cleaned: int = 0
    products_split: int = 0
    product_names_cleaned: int = 0
    product_conflicts_skipped: int = 0
    processes_cleared: int = 0


def clean_weight(value: str | None) -> str | None:
    if not value:
        return None
    source = re.split(r"[；;]", value, maxsplit=1)[0]
    values = re.findall(
        r"(\d+(?:\.\d+)?)\s*(?:g|克)",
        source,
        flags=re.IGNORECASE,
    )
    return "/".join(f"{number}g" for number in values) or None


def split_product(
    product: Product,
    *,
    code_counts: Counter,
    material_counts: Counter,
    stats: Stats,
) -> bool:
    if " / " not in product.product_code:
        return False
    code, name = product.product_code.split(" / ", 1)
    code = code.strip()
    name = name.strip()
    if not code or not name:
        return False
    if product.product_name != name:
        product.product_name = name
        stats.product_names_cleaned += 1
    material_code = (
        product.customer_material_code.split(" / ", 1)[0].strip()
    )
    if (
        code_counts[(product.customer_id, code)] > 1
        or material_counts[(product.customer_id, material_code)] > 1
    ):
        stats.product_conflicts_skipped += 1
        return False
    product.product_code = code
    product.customer_material_code = material_code
    return True


def run(*, commit: bool) -> Stats:
    stats = Stats()
    with SessionLocal() as db:
        if commit:
            reject_legacy_master_data_write_if_versioned(
                db,
                script_name="scripts/clean_phase12_master_data.py",
            )
        products = db.scalars(select(Product)).all()
        code_counts = Counter(
            (
                product.customer_id,
                product.product_code.split(" / ", 1)[0].strip(),
            )
            for product in products
        )
        material_counts = Counter(
            (
                product.customer_id,
                product.customer_material_code.split(" / ", 1)[0].strip(),
            )
            for product in products
        )
        for material in db.scalars(select(Material)).all():
            cleaned = clean_weight(material.basis_weight_description)
            if cleaned and cleaned != material.basis_weight_description:
                material.basis_weight_description = cleaned
                stats.materials_cleaned += 1
        for product in products:
            if split_product(
                product,
                code_counts=code_counts,
                material_counts=material_counts,
                stats=stats,
            ):
                stats.products_split += 1
            process = product.production_process or ""
            if "�" in process or "锟" in process:
                product.production_process = None
                stats.processes_cleared += 1
        if commit:
            db.commit()
        else:
            db.rollback()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--commit",
        action="store_true",
        help="真正提交清洗；不传时仅预览并回滚",
    )
    args = parser.parse_args()
    stats = run(commit=args.commit)
    mode = "COMMIT" if args.commit else "DRY-RUN"
    print(
        f"{mode}: materials={stats.materials_cleaned}, "
        f"products={stats.products_split}, "
        f"names={stats.product_names_cleaned}, "
        f"conflicts={stats.product_conflicts_skipped}, "
        f"processes={stats.processes_cleared}"
    )


if __name__ == "__main__":
    main()
