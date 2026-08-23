from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_integrated_candidate_has_one_linear_migration_head() -> None:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == ["dh42v8x9z31"]
    assert scripts.get_revision("dh42v8x9z31").down_revision == "dg41v8x9z30"
    assert scripts.get_revision("dg41v8x9z30").down_revision == "df40v8x9z29"
    assert scripts.get_revision("df40v8x9z29").down_revision == "de39v8x9z28"
