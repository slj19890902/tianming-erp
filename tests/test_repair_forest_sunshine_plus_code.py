from __future__ import annotations

import sqlite3
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker


def test_forest_sunshine_zero_to_plus_repair_is_audited_and_scoped(tmp_path):
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.audit import OperationLog
    from app.models.master_data_object_version import MasterDataObjectVersion
    from app.models.material import Material
    from app.models.supplier_paper_code import SupplierPaperCode
    from app.models.user import User
    from scripts.admin.repair_forest_sunshine_plus_code import (
        MATERIAL_CODE_CHANGES,
        SOURCE,
        apply_repair,
        inspect_plan,
        sha256_file,
    )

    database = tmp_path / "repair.sqlite3"
    backup_dir = tmp_path / "backups"
    engine = create_sqlite_engine(database)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add(
            User(
                username="admin",
                password_hash=hash_password("RepairPass123!"),
                role="admin",
                real_name="修正测试管理员",
                must_change_password=False,
            )
        )
        db.add(
            SupplierPaperCode(
                id=70,
                supplier_name="森林阳光",
                code_char="0",
                paper_name="芯纸",
                gram_weight=105,
                paper_role="芯纸",
                is_active=True,
            )
        )
        for material_id, (old_code, _new_code) in MATERIAL_CODE_CHANGES.items():
            composition = " | ".join(
                f"层{index}:{char}=105g 芯纸"
                for index, char in enumerate(old_code, start=1)
            )
            db.add(
                Material(
                    id=material_id,
                    code=old_code,
                    paper_composition=composition,
                    layer_count=len(old_code),
                    quote_price=Decimal("1.0000"),
                    supplier_name="森林阳光",
                    is_active=True,
                    version=1,
                )
            )
        db.commit()
    engine.dispose()

    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE historical_material_snapshot (material_code TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO historical_material_snapshot(material_code) VALUES (?)",
            [(old_code,) for old_code, _new_code in MATERIAL_CODE_CHANGES.values()],
        )
        connection.commit()

    source_sha_before_dry_run = sha256_file(database)
    before = inspect_plan(database)
    assert before["status"] == "ready"
    assert before["historical_snapshots_changed"] is False
    assert sha256_file(database) == source_sha_before_dry_run

    result = apply_repair(
        database=database,
        backup_dir=backup_dir,
        actor_username="admin",
        expected_sha256=sha256_file(database),
    )
    assert result["changed"] is True
    assert result["checks_after"]["integrity_check"] == "ok"
    assert result["checks_after"]["foreign_key_violations"] == 0
    assert result["plan_after"]["status"] == "already_applied"
    assert list(backup_dir.glob("*_FOREST_SUNSHINE_ZERO_TO_PLUS_BEFORE_UPDATE.sqlite3"))

    engine = create_sqlite_engine(database)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        paper = db.get(SupplierPaperCode, 70)
        assert paper is not None
        assert paper.code_char == "+"
        for material_id, (_old_code, new_code) in MATERIAL_CODE_CHANGES.items():
            material = db.get(Material, material_id)
            assert material is not None
            assert material.code == new_code
            assert material.version == 2
            assert ":0=105g" not in (material.paper_composition or "")
            assert ":+=105g" in (material.paper_composition or "")
        assert db.scalar(
            select(func.count(MasterDataObjectVersion.id)).where(
                MasterDataObjectVersion.object_type == "material"
            )
        ) == len(MATERIAL_CODE_CHANGES) * 2
        assert db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.details.contains(SOURCE)
            )
        ) == len(MATERIAL_CODE_CHANGES) + 1
    engine.dispose()

    with sqlite3.connect(database) as connection:
        snapshots = [
            row[0]
            for row in connection.execute(
                "SELECT material_code FROM historical_material_snapshot ORDER BY rowid"
            )
        ]
    assert snapshots == [old for old, _new in MATERIAL_CODE_CHANGES.values()]
    assert inspect_plan(database)["status"] == "already_applied"
