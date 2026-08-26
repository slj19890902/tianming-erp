from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.services.legacy_pallet_item_lot_normalization import (
    IDEMPOTENCY_KEY,
    LOT_NUMBER,
    MIGRATION_KEY,
    MOVEMENT_NUMBER,
    REASON,
)


ROOT = Path(__file__).resolve().parents[1]


def _isolated_formal_copy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Config, Path]:
    source = os.environ.get("P1_108_ISOLATED_BASELINE")
    if not source:
        pytest.skip("P1_108_ISOLATED_BASELINE is required; this contract never opens the formal database.")
    source_path = Path(source)
    assert source_path.is_file()
    database = tmp_path / "p1-108-isolated.sqlite3"
    shutil.copy2(source_path, database)
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    return config, database


def _connect(database: Path):
    return create_engine(f"sqlite:///{database}")


def test_normalizes_only_audited_legacy_item_and_roundtrips(monkeypatch, tmp_path: Path) -> None:
    config, database = _isolated_formal_copy(monkeypatch, tmp_path)
    command.upgrade(config, "fi44v8x9z33")
    engine = _connect(database)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "fi44v8x9z33"
        lot_id = connection.scalar(text("SELECT inventory_lot_id FROM inventory_pallet_items WHERE id=2"))
        assert lot_id is not None
        assert connection.execute(text("SELECT quantity_available,source_type,source_ref_type,source_ref_id,stock_date_accuracy FROM inventory_lots WHERE id=:id"), {"id": lot_id}).one() == (86, "manual", "legacy_pallet_item", 2, "unknown")
        assert connection.execute(text("SELECT owner_customer_id,product_id,inventory_code_snapshot FROM finished_goods_inventory_details WHERE inventory_lot_id=:id"), {"id": lot_id}).one() == (5, 51, "21301090")
        assert connection.execute(text("SELECT movement_type,quantity,before_available,after_available,reason,idempotency_key FROM inventory_movements WHERE movement_number=:number"), {"number": MOVEMENT_NUMBER}).one() == ("manual_in", 86, 0, 86, REASON, IDEMPOTENCY_KEY)
        assert connection.scalar(text("SELECT COUNT(*) FROM inventory_pallet_items i JOIN inventory_pallets p ON p.id=i.pallet_id WHERE p.status='active' AND p.is_current=1 AND i.quantity>0 AND i.inventory_lot_id IS NULL")) == 0
        assert connection.scalar(text("SELECT COUNT(*) FROM warehouse_current_map_migration_snapshots WHERE migration_key=:key"), {"key": MIGRATION_KEY}) == 1
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
    engine.dispose()
    command.downgrade(config, "fh43v8x9z32")
    command.upgrade(config, "fi44v8x9z33")


def test_downgrade_refuses_changed_normalized_facts(monkeypatch, tmp_path: Path) -> None:
    config, database = _isolated_formal_copy(monkeypatch, tmp_path)
    command.upgrade(config, "fi44v8x9z33")
    engine = _connect(database)
    with engine.begin() as connection:
        connection.execute(text("UPDATE inventory_lots SET remarks='test post-migration drift' WHERE lot_number=:lot_number"), {"lot_number": LOT_NUMBER})
    engine.dispose()
    with pytest.raises(RuntimeError, match="Refusing P1-108 downgrade"):
        command.downgrade(config, "fh43v8x9z32")
    engine = _connect(database)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "fi44v8x9z33"
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
    engine.dispose()
