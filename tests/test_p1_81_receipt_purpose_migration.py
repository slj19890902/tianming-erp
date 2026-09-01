from __future__ import annotations

import importlib.util
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "alembic" / "versions"
BASE_REVISION = "ww31v8x9z20"
P1_81_REVISION = "xx32v8x9z21"
P1_81_INTEGRATION_ANCESTOR = "de39v8x9z28"
P1_81_TABLES = {
    "purchase_receipt_facts",
    "incoming_receipt_purpose_allocations",
    "incoming_receipt_purpose_reversals",
}
MATERIAL_CORRECTION_TRIGGER = (
    "trg_sales_order_item_bom_components_material_correction_guard"
)


def _migration_path() -> Path:
    matches = sorted(VERSIONS.glob("xx32v8x9z21_*.py"))
    assert len(matches) == 1, [path.name for path in matches]
    return matches[0]


def _load_migration():
    path = _migration_path()
    spec = importlib.util.spec_from_file_location("p1_81_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    resolved = db_path.resolve()
    protected = (ROOT / "data" / "carton_erp.sqlite3").resolve()
    assert resolved != protected
    assert resolved.is_relative_to(db_path.parent.resolve())
    monkeypatch.setenv("ERP_DATABASE_PATH", str(resolved))
    monkeypatch.setenv("ERP_ENVIRONMENT", "development")
    monkeypatch.setenv(
        "ERP_SECRET_KEY",
        "p1-81-anonymous-migration-test-secret-key",
    )
    monkeypatch.setenv(
        "ERP_BACKUP_DIR",
        str((db_path.parent / "backups").resolve()),
    )
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{resolved.as_posix()}")
    return config


def _revision(engine) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(text("SELECT version_num FROM alembic_version"))
            .scalar_one()
        )


def _pragma(db_path: Path, statement: str):
    with sqlite3.connect(db_path) as connection:
        return connection.execute(statement).fetchall()


def _trigger_exists(db_path: Path, trigger_name: str) -> bool:
    with sqlite3.connect(db_path) as connection:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
                (trigger_name,),
            ).fetchone()
            is not None
        )


def _insert_p1_80_supplier_sources(engine) -> tuple[int, int, int, int]:
    """Create one frozen P1-80 source and one genuine legacy source."""

    with engine.begin() as connection:
        user_id = connection.execute(
            text(
                "INSERT INTO users (username,password_hash,role,real_name,"
                "must_change_password,is_active) VALUES "
                "('p181-migration-user','x','admin','anonymous',0,1) RETURNING id"
            )
        ).scalar_one()
        customer_id = connection.execute(
            text(
                "INSERT INTO customers (customer_number,customer_code,name) "
                "VALUES (181,'P181-ANON','匿名客户') RETURNING id"
            )
        ).scalar_one()
        order_id = connection.execute(
            text(
                "INSERT INTO supplier_requisition_orders "
                "(order_number,supplier_name,total_quantity,stock_deduction_qty,"
                "requisition_qty,status,created_by) VALUES "
                "('SRO-P181-FROZEN','匿名供应商',600,0,600,'confirmed',:uid) "
                "RETURNING id"
            ),
            {"uid": user_id},
        ).scalar_one()
        frozen_item_id = connection.execute(
            text(
                "INSERT INTO supplier_requisition_order_items "
                "(supplier_order_id,source_key,product_code,product_name,quantity,"
                "stock_deduction_qty,requisition_qty,cutting_mode,pieces_per_box,"
                "required_piece_qty,customer_name,status,version) VALUES "
                "(:oid,'direct_supplier_item:p181-frozen','P181-F','匿名冻结纸箱',"
                "500,0,600,'一开一',1,500,'匿名客户','active',1) RETURNING id"
            ),
            {"oid": order_id},
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO purchase_purpose_source_snapshots "
                "(snapshot_key,allocation_group_key,"
                "supplier_requisition_order_item_id,material_requisition_item_id,"
                "source_kind,source_key,source_order_item_id,"
                "source_requisition_item_id,source_bom_requisition_source_id,"
                "customer_id,customer_name_snapshot,component_type,"
                "source_finished_qty_snapshot,pieces_per_finished_snapshot,"
                "source_required_piece_qty_snapshot,"
                "source_semi_reserved_piece_qty_snapshot,"
                "source_effective_piece_qty_snapshot,yield_per_sheet_snapshot,"
                "group_effective_piece_qty_snapshot,"
                "group_authoritative_order_sheet_qty_snapshot,purchase_sheet_qty,"
                "order_purpose_sheet_qty,reserve_purpose_sheet_qty,"
                "calculation_rule_version,snapshot_version,preview_fingerprint,"
                "request_hash,created_by) VALUES "
                "('p181-snapshot','p181-group',:item,NULL,"
                "'direct_supplier_item','direct_supplier_item:p181-frozen',"
                "NULL,NULL,NULL,:customer,'匿名客户','whole',500,1,500,0,500,1,"
                "500,500,600,500,100,'p1-80-v1',1,:fingerprint,:hash,:uid)"
            ),
            {
                "item": frozen_item_id,
                "customer": customer_id,
                "fingerprint": "a" * 64,
                "hash": "b" * 64,
                "uid": user_id,
            },
        )

        legacy_order_id = connection.execute(
            text(
                "INSERT INTO supplier_requisition_orders "
                "(order_number,supplier_name,total_quantity,stock_deduction_qty,"
                "requisition_qty,status,created_by) VALUES "
                "('SRO-P181-LEGACY','匿名历史供应商',10,0,10,'confirmed',:uid) "
                "RETURNING id"
            ),
            {"uid": user_id},
        ).scalar_one()
        legacy_item_id = connection.execute(
            text(
                "INSERT INTO supplier_requisition_order_items "
                "(supplier_order_id,source_key,product_code,product_name,quantity,"
                "stock_deduction_qty,requisition_qty,cutting_mode,pieces_per_box,"
                "required_piece_qty,customer_name,status,version) VALUES "
                "(:oid,'direct_supplier_item:p181-legacy','P181-L','匿名历史纸箱',"
                "10,0,10,'一开一',1,10,'匿名客户','active',1) RETURNING id"
            ),
            {"oid": legacy_order_id},
        ).scalar_one()
    return user_id, customer_id, frozen_item_id, legacy_item_id


def test_migration_is_linear_and_declares_immutable_conserved_facts() -> None:
    module = _load_migration()
    assert module.revision == P1_81_REVISION
    assert module.down_revision == BASE_REVISION
    assert module.branch_labels is None

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1
    current_integration_head = heads[0]
    integration_chain = {
        revision.revision
        for revision in script.walk_revisions(
            base=P1_81_REVISION,
            head=current_integration_head,
        )
    }
    assert P1_81_INTEGRATION_ANCESTOR in integration_chain
    assert P1_81_REVISION in integration_chain

    source = _migration_path().read_text(encoding="utf-8")
    for table in P1_81_TABLES:
        assert table in source
        assert f"trg_{{table_name}}_immutable_update" in source
        assert f"trg_{{table_name}}_immutable_delete" in source
    for required in (
        "purpose_contract_status",
        "receipt_fact_version",
        "receipt_plan_fingerprint",
        "actual_material_id",
        "expected_material_id",
        "unit_price",
        "currency",
        "price_unit",
        "tax_included",
        "tax_rate",
        "receipt_order_purpose_sheet_qty",
        "receipt_reserve_purpose_sheet_qty",
        "finished_output_qty_delta",
        "order_purpose_cost + reserve_purpose_cost = total_cost",
        "origin IN ('manual','receipt_auto')",
        "purchase_reserve",
    ):
        assert required in source


def test_empty_sqlite_upgrade_downgrade_upgrade_roundtrip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "p1_81_empty.sqlite3"
    config = _config(db_path, monkeypatch)
    command.upgrade(config, BASE_REVISION)
    engine = create_engine(config.get_main_option("sqlalchemy.url"))
    before_tables = set(inspect(engine).get_table_names())
    assert _trigger_exists(db_path, MATERIAL_CORRECTION_TRIGGER)

    command.upgrade(config, P1_81_REVISION)
    assert _revision(engine) == P1_81_REVISION
    assert P1_81_TABLES <= set(inspect(engine).get_table_names())
    assert _trigger_exists(db_path, MATERIAL_CORRECTION_TRIGGER)
    assert _pragma(db_path, "PRAGMA integrity_check") == [("ok",)]
    assert _pragma(db_path, "PRAGMA foreign_key_check") == []

    command.downgrade(config, BASE_REVISION)
    assert _revision(engine) == BASE_REVISION
    assert not (P1_81_TABLES & set(inspect(engine).get_table_names()))
    assert before_tables == set(inspect(engine).get_table_names())
    assert _trigger_exists(db_path, MATERIAL_CORRECTION_TRIGGER)
    assert _pragma(db_path, "PRAGMA integrity_check") == [("ok",)]
    assert _pragma(db_path, "PRAGMA foreign_key_check") == []

    command.upgrade(config, P1_81_REVISION)
    assert _revision(engine) == P1_81_REVISION
    assert P1_81_TABLES <= set(inspect(engine).get_table_names())
    assert _trigger_exists(db_path, MATERIAL_CORRECTION_TRIGGER)
    assert _pragma(db_path, "PRAGMA integrity_check") == [("ok",)]
    assert _pragma(db_path, "PRAGMA foreign_key_check") == []


def test_p1_80_snapshot_backfills_frozen_legacy_stays_unset_and_roundtrips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "p1_81_marker_roundtrip.sqlite3"
    config = _config(db_path, monkeypatch)
    command.upgrade(config, BASE_REVISION)
    engine = create_engine(config.get_main_option("sqlalchemy.url"))
    _user_id, _customer_id, frozen_item_id, legacy_item_id = (
        _insert_p1_80_supplier_sources(engine)
    )

    command.upgrade(config, P1_81_REVISION)
    with engine.connect() as connection:
        statuses = dict(
            connection.execute(
                text(
                    "SELECT id,purpose_contract_status "
                    "FROM supplier_requisition_order_items WHERE id IN (:frozen,:legacy)"
                ),
                {"frozen": frozen_item_id, "legacy": legacy_item_id},
            ).all()
        )
    assert statuses == {frozen_item_id: "frozen", legacy_item_id: "legacy_unset"}

    # The P1-80 purpose snapshot survives a downgrade.  The P1-81 marker is
    # derivable, so it must not by itself make a no-receipt-fact rollback unsafe.
    command.downgrade(config, BASE_REVISION)
    assert _revision(engine) == BASE_REVISION
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM purchase_purpose_source_snapshots")
        ).scalar_one() == 1

    command.upgrade(config, P1_81_REVISION)
    with engine.connect() as connection:
        status = connection.execute(
            text(
                "SELECT purpose_contract_status "
                "FROM supplier_requisition_order_items WHERE id=:id"
            ),
            {"id": frozen_item_id},
        ).scalar_one()
    assert status == "frozen"
    assert _pragma(db_path, "PRAGMA integrity_check") == [("ok",)]
    assert _pragma(db_path, "PRAGMA foreign_key_check") == []


def test_price_fact_is_immutable_and_blocks_destructive_downgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.security import hash_password
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.purchase_receipt import PurchaseReceiptFact
    from app.models.supplier_requisition_order import (
        PurchasePurposeSourceSnapshot,
        SupplierRequisitionOrder,
        SupplierRequisitionOrderItem,
    )
    from app.models.user import User
    from app.services.purchase_receipt_facts import material_calculation_fingerprint

    db_path = tmp_path / "p1_81_fact_guard.sqlite3"
    config = _config(db_path, monkeypatch)
    command.upgrade(config, P1_81_REVISION)
    # The SQLAlchemy model needs the later schema carried by P1-81's own
    # integration ancestor before creating fixture rows through that model.
    # Keep this guard local so newer irreversible migrations cannot mask the
    # P1-81 destructive-downgrade protection exercised below.
    command.upgrade(config, P1_81_INTEGRATION_ANCESTOR)
    engine = create_engine(config.get_main_option("sqlalchemy.url"))
    with Session(engine) as session:
        user = User(
            username="p181-fact-user",
            password_hash=hash_password("AnonymousPass123!"),
            role="admin",
            real_name="匿名迁移用户",
            must_change_password=False,
        )
        customer = Customer(
            customer_number=181,
            customer_code="P181-FACT",
            name="匿名事实客户",
        )
        material = Material(
            code="P181-FACT-M",
            layer_count=5,
            flute_type="AB",
            supplier_name="匿名供应商",
            is_active=True,
            version=1,
        )
        session.add_all([user, customer, material])
        session.flush()
        supplier_order = SupplierRequisitionOrder(
            order_number="SRO-P181-FACT",
            supplier_name="匿名供应商",
            total_quantity=1,
            stock_deduction_qty=0,
            requisition_qty=1,
            status="confirmed",
            created_by=user.id,
        )
        session.add(supplier_order)
        session.flush()
        supplier_item = SupplierRequisitionOrderItem(
            supplier_order_id=supplier_order.id,
            source_key="direct_supplier_item:p181-fact",
            material_id=material.id,
            material_code_snapshot=material.code,
            product_code="P181-FACT-P",
            product_name="匿名事实纸箱",
            quantity=1,
            stock_deduction_qty=0,
            requisition_qty=1,
            cutting_mode="一开一",
            pieces_per_box=1,
            required_piece_qty=1,
            customer_name=customer.name,
            purpose_contract_status="frozen",
            status="active",
            version=1,
        )
        session.add(supplier_item)
        session.flush()
        purpose = PurchasePurposeSourceSnapshot(
            snapshot_key="p181-fact-snapshot",
            allocation_group_key="p181-fact-group",
            supplier_requisition_order_item_id=supplier_item.id,
            material_requisition_item_id=None,
            source_kind="direct_supplier_item",
            source_key="direct_supplier_item:p181-fact",
            source_order_item_id=None,
            source_requisition_item_id=None,
            source_bom_requisition_source_id=None,
            customer_id=customer.id,
            customer_name_snapshot=customer.name,
            component_type="whole",
            source_finished_qty_snapshot=1,
            pieces_per_finished_snapshot=1,
            source_required_piece_qty_snapshot=1,
            source_semi_reserved_piece_qty_snapshot=0,
            source_effective_piece_qty_snapshot=1,
            yield_per_sheet_snapshot=1,
            group_effective_piece_qty_snapshot=1,
            group_authoritative_order_sheet_qty_snapshot=1,
            purchase_sheet_qty=1,
            order_purpose_sheet_qty=1,
            reserve_purpose_sheet_qty=0,
            calculation_rule_version="p1-80-v1",
            snapshot_version=1,
            preview_fingerprint="a" * 64,
            request_hash="b" * 64,
            created_by=user.id,
        )
        session.add(purpose)
        session.flush()
        actual_material_fingerprint = material_calculation_fingerprint(material)
        receipt_fact = PurchaseReceiptFact(
            supplier_requisition_order_item_id=supplier_item.id,
            material_requisition_item_id=None,
            purchase_purpose_source_snapshot_id=purpose.id,
            source_key=purpose.source_key,
            receipt_fact_version=1,
            expected_source_version=1,
            purpose_snapshot_version=1,
            receipt_plan_fingerprint="c" * 64,
            actual_material_id=material.id,
            actual_material_code_snapshot=material.code,
            actual_material_version=material.version,
            actual_material_layer_count_snapshot=material.layer_count,
            actual_material_flute_type_snapshot=material.flute_type,
            actual_material_is_active_snapshot=material.is_active,
            actual_material_fingerprint=actual_material_fingerprint,
            expected_material_id=material.id,
            expected_material_code_snapshot=material.code,
            material_change_confirmed=False,
            unit_price=Decimal("2.500000"),
            currency="CNY",
            price_unit="per_sheet",
            tax_included=True,
            tax_rate=Decimal("0.130000"),
            idempotency_key="p181-migration-price-fact",
            request_hash="d" * 64,
            created_by=user.id,
        )
        session.add(receipt_fact)
        session.commit()
        fact_id = receipt_fact.id

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE purchase_receipt_facts SET unit_price=3 WHERE id=:id"),
                {"id": fact_id},
            )
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM purchase_receipt_facts WHERE id=:id"),
                {"id": fact_id},
            )

    with pytest.raises(Exception):
        command.downgrade(config, BASE_REVISION)
    assert _revision(engine) == P1_81_REVISION
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM purchase_receipt_facts WHERE id=:id"),
            {"id": fact_id},
        ).scalar_one() == 1
    assert _pragma(db_path, "PRAGMA integrity_check") == [("ok",)]
    assert _pragma(db_path, "PRAGMA foreign_key_check") == []
