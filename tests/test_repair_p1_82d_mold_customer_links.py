from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker


def _build_repair_fixture(tmp_path):
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.audit import OperationLog  # noqa: F401
    from app.models.customer import Customer
    from app.models.mold_tool import MoldTool, MoldToolCustomer
    from app.models.product import Product
    from app.models.user import User
    from scripts.admin.repair_p1_82d_mold_customer_links import TARGETS

    database = tmp_path / "p1_82d.sqlite3"
    engine = create_sqlite_engine(database)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    customer_codes = sorted(
        {
            code
            for target in TARGETS
            for code in (
                *target.expected_customer_codes,
                *target.expected_product_customer_codes,
            )
        }
    )
    with factory() as db:
        actor = User(
            username="admin",
            password_hash=hash_password("RepairPass123!"),
            role="admin",
            real_name="修复测试管理员",
            must_change_password=False,
            is_active=True,
        )
        db.add(actor)
        customers = {}
        for index, customer_code in enumerate(customer_codes, start=1):
            customer = Customer(
                customer_number=index,
                customer_code=customer_code,
                name=f"测试客户-{customer_code}",
                chinese_short_name=customer_code,
                is_active=True,
            )
            db.add(customer)
            customers[customer_code] = customer
        db.flush()

        for target_index, target in enumerate(TARGETS, start=1):
            mold = MoldTool(
                mold_code=target.mold_code,
                mold_name=target.expected_mold_name,
                label_name=target.expected_label_name,
                identity_status="frozen",
                version=target.expected_version,
                rack_location="1F-M-R03-L2-G01",
                location_version=3,
                repair_status="normal",
                repair_version=2,
                remarks=f"protected-{target_index}",
                created_by=actor.id,
                updated_by=actor.id,
            )
            db.add(mold)
            db.flush()
            for customer_code in target.expected_customer_codes:
                db.add(
                    MoldToolCustomer(
                        mold_tool_id=mold.id,
                        customer_id=customers[customer_code].id,
                        display_order=(
                            1 if customer_code == target.keep_customer_code else None
                        ),
                        created_by=actor.id,
                    )
                )
            for product_index, customer_code in enumerate(
                target.expected_product_customer_codes,
                start=1,
            ):
                db.add(
                    Product(
                        customer_id=customers[customer_code].id,
                        product_code=f"P-{target_index}-{product_index}",
                        customer_material_code=f"CM-{target_index}-{product_index}",
                        product_name=f"绑定产品-{target_index}-{product_index}",
                        mold_tool_id=mold.id,
                    )
                )
        db.commit()

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO alembic_version(version_num) VALUES ('de39v8x9z28')"
        )
    engine.dispose()
    return database


def test_p1_82d_repair_is_scoped_audited_and_idempotent(tmp_path):
    from app.core.database import create_sqlite_engine
    from app.models.audit import OperationLog
    from app.models.mold_tool import MoldTool, MoldToolCustomer
    from scripts.admin.repair_p1_82d_mold_customer_links import (
        ACTION_CODE,
        BATCH_ID,
        TARGETS,
        apply_repair,
        inspect_plan,
        sha256_file,
    )

    database = _build_repair_fixture(tmp_path)
    backup_dir = tmp_path / "backups"
    before_sha = sha256_file(database)
    before_plan = inspect_plan(database)
    assert before_plan["status"] == "ready"
    assert before_plan["target_count"] == 4
    assert before_plan["links_to_remove"] == 10
    assert before_plan["creates_molds"] is False
    assert sha256_file(database) == before_sha

    result = apply_repair(
        database=database,
        backup_dir=backup_dir,
        actor_username="admin",
        expected_sha256=before_sha,
    )
    assert result["changed"] is True
    assert result["plan_after"]["status"] == "already_applied"
    assert result["checks_after"]["integrity_check"] == "ok"
    assert result["checks_after"]["foreign_key_violations"] == 0
    assert list(
        backup_dir.glob("*_P1_82D_MOLD_CUSTOMER_LINKS_BEFORE_REPAIR.sqlite3")
    )

    engine = create_sqlite_engine(database)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        for target in TARGETS:
            mold = db.scalar(
                select(MoldTool).where(MoldTool.mold_code == target.mold_code)
            )
            assert mold is not None
            assert mold.version == target.expected_version + 1
            assert mold.rack_location == "1F-M-R03-L2-G01"
            assert mold.location_version == 3
            assert mold.repair_version == 2
            assert mold.remarks is not None and mold.remarks.startswith("protected-")
            links = db.scalars(
                select(MoldToolCustomer).where(
                    MoldToolCustomer.mold_tool_id == mold.id
                )
            ).all()
            assert len(links) == 1
            assert links[0].display_order == 1
        logs = db.scalars(
            select(OperationLog).where(
                OperationLog.batch_id == BATCH_ID,
                OperationLog.action_code == ACTION_CODE,
            )
        ).all()
        assert len(logs) == 4
        assert all(log.result == "success" for log in logs)
        assert all(log.source == "script" for log in logs)
    engine.dispose()

    second = apply_repair(
        database=database,
        backup_dir=backup_dir,
        actor_username="admin",
        expected_sha256=sha256_file(database),
    )
    assert second["changed"] is False
    assert inspect_plan(database)["status"] == "already_applied"


def test_p1_82d_repair_rolls_back_all_targets_when_audit_insert_fails(tmp_path):
    from scripts.admin.repair_p1_82d_mold_customer_links import (
        ACTION_CODE,
        TARGETS,
        apply_repair,
        inspect_plan,
        sha256_file,
    )

    database = _build_repair_fixture(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            f"""
            CREATE TRIGGER force_p1_82d_audit_failure
            BEFORE INSERT ON operation_logs
            WHEN NEW.action_code = '{ACTION_CODE}'
            BEGIN
                SELECT RAISE(ABORT, 'forced audit failure');
            END
            """
        )
        connection.commit()
    source_sha = sha256_file(database)

    with pytest.raises(sqlite3.IntegrityError, match="forced audit failure"):
        apply_repair(
            database=database,
            backup_dir=tmp_path / "backups",
            actor_username="admin",
            expected_sha256=source_sha,
        )

    plan = inspect_plan(database)
    assert plan["status"] == "ready"
    assert plan["links_to_remove"] == 10
    assert [item["current_version"] for item in plan["items"]] == [
        target.expected_version for target in TARGETS
    ]


def test_p1_82d_repair_rejects_removed_customer_with_product_binding(tmp_path):
    from scripts.admin.repair_p1_82d_mold_customer_links import inspect_plan

    database = _build_repair_fixture(tmp_path)
    with sqlite3.connect(database) as connection:
        mold_id = connection.execute(
            "SELECT id FROM mold_tools WHERE mold_code = 'M-CE1694DFDDD6'"
        ).fetchone()[0]
        removed_customer_id = connection.execute(
            "SELECT id FROM customers WHERE customer_code = 'TH'"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO products (
                customer_id, product_code, customer_material_code, product_name,
                mold_tool_id, box_category, supply_mode, printing_plate_mode,
                unit, default_cutting_mode, production_label_enabled,
                is_composite, is_virtual_composite_parent, combination_mode,
                composite_fulfillment_mode, is_internal_component, is_active,
                manual_modified, version
            ) VALUES (
                ?, 'unexpected', 'unexpected', 'unexpected', ?, 'normal',
                'corrugated_production', 'no_plate', '只', '一开一', 0,
                0, 0, 'parent_priced_set', 'component_delivery', 0, 1, 0, 1
            )
            """,
            (removed_customer_id, mold_id),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="产品绑定客户已变化"):
        inspect_plan(database)
