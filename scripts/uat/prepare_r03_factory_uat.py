"""Create the disposable data set for R03 factory-isolated Chrome validation."""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FACTORY_UAT_ROOT = Path(r"D:\tm-uat").resolve()


def _path_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root)
    except ValueError:
        return False
    return True


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    return parser.parse_args()


def _seed(database: Path) -> dict[str, object]:
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.product import Product
    from app.models.user import User

    engine = create_sqlite_engine(database)
    try:
        with Session(engine) as session:
            admin = User(
                username="r03-admin",
                password_hash=hash_password("123456"),
                role="admin",
                real_name="R03 隔离管理员",
                display_name="R03 隔离管理员",
                is_active=True,
                must_change_password=False,
                customer_access_mode="all",
            )
            sales = User(
                username="r03-sales",
                password_hash=hash_password("123456"),
                role="sales",
                real_name="R03 隔离业务员",
                display_name="R03 隔离业务员",
                is_active=True,
                must_change_password=False,
                customer_access_mode="all",
            )
            customer = Customer(
                customer_number=300301,
                customer_code="R03-UAT",
                name="R03 Chrome 隔离验收客户",
                payment_term_days=30,
                statement_cycle_start_day=20,
                credit_limit=Decimal("100000"),
                delivery_method="配送",
                status="active",
                is_active=True,
            )
            material = Material(
                code="R03-UAT-KA5",
                paper_composition="K=A=K",
                layer_count=5,
                flute_type="AB",
                supplier_name="R03 隔离供应商",
                quote_price=Decimal("2.50"),
                is_active=True,
            )
            session.add_all((admin, sales, customer, material))
            session.flush()
            product = Product(
                customer_id=customer.id,
                product_code="R03-UAT-BOX-001",
                customer_material_code="R03-UAT-BOX-001",
                product_name="R03 Chrome 隔离验收纸箱",
                material_id=material.id,
                legacy_material_text="K=A=K",
                length_mm=Decimal("600"),
                width_mm=Decimal("400"),
                height_mm=Decimal("300"),
                box_category="normal",
                box_style="A1",
                unit="个",
                sale_unit_price=Decimal("10"),
                report_length_mm=1200,
                report_width_mm=700,
                crease_type="净料",
                flute_type="AB",
                layer_count=5,
                is_active=True,
            )
            session.add(product)
            session.commit()
            head = session.connection().exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()
            return {
                "database": str(database),
                "revision": str(head),
                "accounts": [admin.username, sales.username],
                "customer_code": customer.customer_code,
                "product_code": product.product_code,
            }
    finally:
        engine.dispose()


def main() -> int:
    args = _parse_args()
    root = args.root.resolve(strict=False)
    database = args.database.resolve(strict=False)
    if not _path_inside(root, FACTORY_UAT_ROOT) or root.parent != FACTORY_UAT_ROOT:
        raise SystemExit("R03 factory UAT root must be a direct child of D:\\tm-uat")
    if database != root / "carton_erp.sqlite3":
        raise SystemExit("R03 database must be <root>\\carton_erp.sqlite3")
    if root.exists() or database.exists():
        raise SystemExit("R03 factory UAT root must be new; refusing to reuse or overwrite it")
    root.mkdir(parents=True)
    os.environ.update(
        {
            "ERP_ENVIRONMENT": "test",
            "ERP_DATABASE_PATH": str(database),
            "ERP_BACKUP_DIR": str(root / "bootstrap-backups"),
            "ERP_SECRET_KEY_FILE": str(root / "bootstrap-secret.key"),
        }
    )
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    print(json.dumps(_seed(database), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
