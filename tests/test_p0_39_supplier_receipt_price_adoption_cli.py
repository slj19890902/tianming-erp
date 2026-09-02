from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import shutil

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from scripts.admin import adopt_supplier_receipt_price_facts as cli


def _build_cli_database(
    tmp_path: Path,
    *,
    operator_username: str = "p0-39-cli-admin",
    operator_role: str = "admin",
) -> tuple[Path, dict[str, int]]:
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.material import Material
    from app.models.stock_replenishment import (
        StockReplenishmentOrder,
        StockReplenishmentOrderItem,
    )
    from app.models.supplier import Supplier
    from app.models.user import User
    from app.services.supplier_master import normalize_supplier_identity

    database = tmp_path / "p0_39_cli.sqlite3"
    engine = create_sqlite_engine(database)
    Base.metadata.create_all(engine)
    head = cli.code_alembic_heads()[0]
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        connection.execute(
            text("INSERT INTO alembic_version(version_num) VALUES (:head)"),
            {"head": head},
        )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        user = User(
            username=operator_username,
            password_hash="not-used",
            role=operator_role,
            real_name=f"P0-39 CLI {operator_role}",
            is_active=True,
            must_change_password=False,
        )
        supplier = Supplier(
            standard_name="苏州佳丰纸板有限公司",
            normalized_name=normalize_supplier_identity("苏州佳丰纸板有限公司"),
            display_name="佳丰纸板",
            is_active=True,
            version=1,
        )
        db.add_all([user, supplier])
        db.flush()
        material = Material(
            code="A416D",
            supplier_name="苏州佳丰纸板有限公司",
            quote_price=Decimal("2.80"),
            price_unit="元/㎡",
            purchase_currency="CNY",
            purchase_tax_included=True,
            purchase_tax_rate=Decimal("0.13"),
            is_active=True,
            version=3,
        )
        db.add(material)
        db.flush()
        replenishment = StockReplenishmentOrder(
            order_number="SR-CLI-HIST-202608",
            supplier_name="苏州佳丰纸板有限公司",
            source_type="manual_history",
            status="stocked",
            created_by=user.id,
        )
        db.add(replenishment)
        db.flush()
        source = StockReplenishmentOrderItem(
            replenishment_order_id=replenishment.id,
            target_inventory_type="semi_finished",
            material_id=material.id,
            product_name_snapshot="历史补库片料",
            material_code_snapshot="A416D",
            normalized_material_code="A416D",
            report_length_mm=1000,
            report_width_mm=500,
            quantity=10,
            stocked_quantity=10,
        )
        db.add(source)
        db.flush()
        receipt = IncomingReceipt(
            receipt_number="IR-CLI-HIST-20260805",
            status="posted",
            received_at=datetime(2026, 8, 5, 3, 0, 0),
            received_by=user.id,
            idempotency_key="p0-39-cli-historical-receipt",
        )
        db.add(receipt)
        db.flush()
        item = IncomingReceiptItem(
            receipt_id=receipt.id,
            stock_replenishment_item_id=source.id,
            planned_quantity=10,
            received_quantity=10,
            cumulative_received_quantity=10,
            variance_quantity=0,
            variance_type="matched",
            resolution_status="not_required",
            resolution_action=None,
            status="posted",
        )
        db.add(item)
        db.commit()
        ids = {
            "user_id": int(user.id),
            "material_id": int(material.id),
            "receipt_item_id": int(item.id),
        }
    engine.dispose()
    return database, ids


def _plan_artifacts(database: Path, output_dir: Path) -> cli.PlanArtifacts:
    plan = cli.build_read_only_plan(
        database,
        settlement_month="2026-08",
        batch_size=500,
    )
    return cli.write_plan_artifacts(plan, output_dir)


def _apply_kwargs(
    *,
    database: Path,
    plan: cli.PlanArtifacts,
    backup: Path,
    batch_key: str,
    operator_username: str = "p0-39-cli-admin",
) -> dict:
    return {
        "database": database,
        "plan_path": plan.json_path,
        "expected_plan_sha256": plan.json_sha256,
        "batch_idempotency_key": batch_key,
        "expected_database_sha256": cli.sha256_file(database),
        "expected_app_version": cli.APP_VERSION,
        "expected_alembic_head": cli.code_alembic_heads()[0],
        "backup_path": backup,
        "expected_backup_sha256": cli.sha256_file(backup),
        "backup_reference": "P0-39 CLI 隔离副本时点备份",
        "operator_username": operator_username,
        "confirmation_text": cli.HISTORICAL_ADOPTION_REASON,
        "apply_confirmation": cli.APPLY_CONFIRMATION,
        "confirm_service_stopped": True,
        "allow_formal_database": False,
    }


def test_cli_month_and_all_history_dry_run_write_csv_json_without_database_write(
    tmp_path: Path,
) -> None:
    database, ids = _build_cli_database(tmp_path)
    before = cli.sha256_file(database)

    month_plan = cli.build_read_only_plan(
        database,
        settlement_month="2026-08",
    )
    all_plan = cli.build_read_only_plan(database, all_history=True)
    artifacts = cli.write_plan_artifacts(month_plan, tmp_path / "reports")

    assert cli.sha256_file(database) == before
    assert month_plan["writes_performed"] is False
    assert month_plan["eligible_count"] == 1
    assert month_plan["rejected_count"] == 0
    assert month_plan["selections"] == [
        {
            "incoming_receipt_item_id": ids["receipt_item_id"],
            "source_hash": month_plan["eligible"][0]["source_hash"],
        }
    ]
    assert month_plan["amount_summary"]["erp_amount"] == "14.00"
    assert all_plan["scope"] == "all_history"
    assert all_plan["eligible_count"] == 1
    assert artifacts.json_path.is_file()
    assert artifacts.csv_path.is_file()
    assert cli.sha256_file(artifacts.json_path) == artifacts.json_sha256
    csv_text = artifacts.csv_path.read_text(encoding="utf-8-sig")
    assert "IR-CLI-HIST-20260805" in csv_text
    assert "eligible" in csv_text
    assert month_plan["plan_hash"] in csv_text


def test_cli_apply_is_transactional_audited_and_batch_idempotent(
    tmp_path: Path,
) -> None:
    from app.core.database import create_sqlite_engine
    from app.models.audit import OperationLog
    from app.models.finance import FinanceIdempotencyRecord
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact

    database, _ids = _build_cli_database(tmp_path)
    plan = _plan_artifacts(database, tmp_path / "reports")
    backup = tmp_path / "before_apply.sqlite3"
    shutil.copy2(database, backup)
    kwargs = _apply_kwargs(
        database=database,
        plan=plan,
        backup=backup,
        batch_key="p0-39-cli-batch-20260903-001",
    )

    first = cli.apply_plan_file(**kwargs)
    replay = cli.apply_plan_file(**kwargs)

    assert first["status"] == "applied"
    assert first["replayed"] is False
    assert first["business_response"]["created_count"] == 1
    assert first["business_response"]["amount_before"]["erp_amount"] == "14.00"
    assert first["business_response"]["amount_after"]["erp_amount"] == "14.00"
    assert first["business_response"]["monthly_candidate_verification"][
        "amount_summary"
    ]["erp_amount"] == "14.00"
    assert replay["status"] == "replayed"
    assert replay["writes_performed"] is False

    engine = create_sqlite_engine(database)
    with Session(engine) as db:
        assert db.scalar(select(func.count(SupplierReceiptSettlementPriceFact.id))) == 1
        assert db.scalar(select(func.count(FinanceIdempotencyRecord.id))) == 1
        audit = db.scalar(
            select(OperationLog).where(OperationLog.action_code == cli.ACTION_CODE)
        )
        assert audit is not None
        assert audit.source == "script"
        assert audit.object_ref == "p0-39-cli-batch-20260903-001"
    engine.dispose()


def test_cli_boss_role_can_apply_historical_price_fact(tmp_path: Path) -> None:
    from app.core.database import create_sqlite_engine
    from app.models.audit import OperationLog
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact

    username = "p0-39-cli-boss"
    database, ids = _build_cli_database(
        tmp_path,
        operator_username=username,
        operator_role="boss",
    )
    plan = _plan_artifacts(database, tmp_path / "reports")
    backup = tmp_path / "before_boss_apply.sqlite3"
    shutil.copy2(database, backup)

    result = cli.apply_plan_file(
        **_apply_kwargs(
            database=database,
            plan=plan,
            backup=backup,
            batch_key="p0-39-cli-batch-20260903-boss",
            operator_username=username,
        )
    )

    assert result["status"] == "applied"
    assert result["business_response"]["created_count"] == 1
    engine = create_sqlite_engine(database)
    with Session(engine) as db:
        assert db.scalar(select(func.count(SupplierReceiptSettlementPriceFact.id))) == 1
        audit = db.scalar(
            select(OperationLog).where(OperationLog.action_code == cli.ACTION_CODE)
        )
        assert audit is not None
        assert audit.actor_user_id_snapshot == ids["user_id"]
        assert audit.role == "boss"
    engine.dispose()


def test_cli_finance_role_is_rejected_before_any_write(tmp_path: Path) -> None:
    from app.core.database import create_sqlite_engine
    from app.models.audit import OperationLog
    from app.models.finance import FinanceIdempotencyRecord
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact

    username = "p0-39-cli-finance"
    database, _ids = _build_cli_database(
        tmp_path,
        operator_username=username,
        operator_role="finance",
    )
    plan = _plan_artifacts(database, tmp_path / "reports")
    backup = tmp_path / "before_finance_apply.sqlite3"
    shutil.copy2(database, backup)
    kwargs = _apply_kwargs(
        database=database,
        plan=plan,
        backup=backup,
        batch_key="p0-39-cli-batch-20260903-finance",
        operator_username=username,
    )
    before_sha256 = cli.sha256_file(database)

    with pytest.raises(
        cli.AdoptionCliError,
        match="只允许 admin 或 boss 执行",
    ):
        cli.apply_plan_file(**kwargs)

    assert cli.sha256_file(database) == before_sha256
    engine = create_sqlite_engine(database)
    with Session(engine) as db:
        assert db.scalar(select(func.count(SupplierReceiptSettlementPriceFact.id))) == 0
        assert db.scalar(select(func.count(FinanceIdempotencyRecord.id))) == 0
        assert db.scalar(select(func.count(OperationLog.id))) == 0
    engine.dispose()


def test_cli_row_source_hash_cas_failure_rolls_back_fact_audit_and_idempotency(
    tmp_path: Path,
) -> None:
    from app.core.database import create_sqlite_engine
    from app.models.audit import OperationLog
    from app.models.finance import FinanceIdempotencyRecord
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact

    database, _ids = _build_cli_database(tmp_path)
    plan = _plan_artifacts(database, tmp_path / "reports")
    payload = json.loads(plan.json_path.read_text(encoding="utf-8"))
    stale_hash = "f" * 64
    payload["eligible"][0]["source_hash"] = stale_hash
    payload["selections"][0]["source_hash"] = stale_hash
    plan.json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tampered = cli.PlanArtifacts(
        json_path=plan.json_path,
        csv_path=plan.csv_path,
        json_sha256=cli.sha256_file(plan.json_path),
    )
    backup = tmp_path / "before_stale_apply.sqlite3"
    shutil.copy2(database, backup)
    kwargs = _apply_kwargs(
        database=database,
        plan=tampered,
        backup=backup,
        batch_key="p0-39-cli-batch-20260903-stale",
    )

    with pytest.raises(cli.SupplierReceiptPriceFactError) as caught:
        cli.apply_plan_file(**kwargs)
    assert caught.value.code == "SUPPLIER_RECEIPT_ADOPTION_PLAN_STALE"

    engine = create_sqlite_engine(database)
    with Session(engine) as db:
        assert db.scalar(select(func.count(SupplierReceiptSettlementPriceFact.id))) == 0
        assert db.scalar(select(func.count(FinanceIdempotencyRecord.id))) == 0
        assert db.scalar(select(func.count(OperationLog.id))) == 0
    engine.dispose()


def test_cli_apply_version_gate_fails_before_any_write(tmp_path: Path) -> None:
    from app.core.database import create_sqlite_engine
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact

    database, _ids = _build_cli_database(tmp_path)
    plan = _plan_artifacts(database, tmp_path / "reports")
    backup = tmp_path / "before_wrong_version.sqlite3"
    shutil.copy2(database, backup)
    kwargs = _apply_kwargs(
        database=database,
        plan=plan,
        backup=backup,
        batch_key="p0-39-cli-batch-20260903-version",
    )
    kwargs["expected_app_version"] = "v0.0.0-wrong"

    with pytest.raises(cli.AdoptionCliError, match="APP version"):
        cli.apply_plan_file(**kwargs)

    engine = create_sqlite_engine(database)
    with Session(engine) as db:
        assert db.scalar(select(func.count(SupplierReceiptSettlementPriceFact.id))) == 0
    engine.dispose()


def test_cli_tampered_plan_amount_fails_against_persisted_fact_and_rolls_back(
    tmp_path: Path,
) -> None:
    from app.core.database import create_sqlite_engine
    from app.models.audit import OperationLog
    from app.models.finance import FinanceIdempotencyRecord
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact

    database, _ids = _build_cli_database(tmp_path)
    plan = _plan_artifacts(database, tmp_path / "reports")
    payload = json.loads(plan.json_path.read_text(encoding="utf-8"))
    payload["eligible"][0]["erp_amount"] = "9999.99"
    plan.json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tampered = cli.PlanArtifacts(
        json_path=plan.json_path,
        csv_path=plan.csv_path,
        json_sha256=cli.sha256_file(plan.json_path),
    )
    backup = tmp_path / "before_amount_mismatch.sqlite3"
    shutil.copy2(database, backup)
    kwargs = _apply_kwargs(
        database=database,
        plan=tampered,
        backup=backup,
        batch_key="p0-39-cli-batch-20260903-amount",
    )

    with pytest.raises(cli.AdoptionCliError, match="事实金额"):
        cli.apply_plan_file(**kwargs)

    engine = create_sqlite_engine(database)
    with Session(engine) as db:
        assert db.scalar(select(func.count(SupplierReceiptSettlementPriceFact.id))) == 0
        assert db.scalar(select(func.count(FinanceIdempotencyRecord.id))) == 0
        assert db.scalar(select(func.count(OperationLog.id))) == 0
    engine.dispose()
