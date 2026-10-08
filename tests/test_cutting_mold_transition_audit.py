import hashlib
import importlib.util
from pathlib import Path
import sqlite3

import pytest


SPEC = importlib.util.spec_from_file_location("cutting_audit", Path(__file__).resolve().parents[1] / "scripts/admin/audit_cutting_mold_transition.py")
audit_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit_module)


def product(**overrides):
    row = {key: None for key in audit_module.PRODUCT_FIELDS}
    row.update(id=1, product_code="TEST", box_category="normal", box_style="隔板",
               production_process="无需结合", mold_tool_id=None, default_cutting_mode="一开一",
               report_length_mm=340, report_width_mm=200, version=3, is_active=1,
               supply_mode="corrugated_production", is_virtual_composite_parent=0)
    row.update(overrides)
    return row


def test_actual_die_cut_process_is_used_even_when_old_category_is_normal():
    row = product(production_process="无需结合,模切", mold_tool_id=5, default_cutting_mode="一开四")
    result = audit_module.classify_product(row, {5}, set())
    assert result["status"] == "candidate"
    assert result["proposed"]["mold_count"] == 4
    assert result["proposed"]["length_parts"] == result["proposed"]["width_parts"] == 1
    assert result["before"] == row


def test_plain_liner_multiple_output_is_reviewed_instead_of_inventing_mold():
    result = audit_module.classify_product(product(default_cutting_mode="一开二"), set(), set())
    assert result["status"] == "needs_review"
    assert result["proposed"] is None


@pytest.mark.parametrize("overrides,molds,yields", [
    ({"box_category": "die_cut"}, {5}, set()),
    ({"production_process": "模切", "mold_tool_id": 5}, set(), set()),
    ({"production_process": "模切", "mold_tool_id": 5, "default_cutting_mode": "一开二"}, {5}, {4}),
    ({"default_cutting_mode": "invalid"}, set(), set()),
    ({"report_length_mm": None}, set(), set()),
])
def test_ambiguous_or_incomplete_facts_have_no_automatic_conversion(overrides, molds, yields):
    result = audit_module.classify_product(product(**overrides), molds, yields)
    assert result["status"] == "needs_review"
    assert result["proposed"] is None


def test_no_own_sheet_product_does_not_get_fabricated_dimensions():
    result = audit_module.classify_product(product(box_style="BOM组合", report_length_mm=None), set(), set())
    assert result["status"] == "no_own_sheet"
    assert result["proposed"] is None


def test_database_audit_preserves_bytes_and_inactive_state(tmp_path):
    source = tmp_path / "audit.sqlite3"
    row = product(is_active=0)
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE alembic_version(version_num TEXT)")
        db.execute("INSERT INTO alembic_version VALUES ('ef1007cp')")
        db.execute("CREATE TABLE mold_tools(id INTEGER)")
        db.execute("CREATE TABLE product_bom_components(component_product_id INTEGER, mold_max_yield_per_sheet INTEGER, is_die_cut INTEGER)")
        db.execute("CREATE TABLE products(" + ",".join(audit_module.PRODUCT_FIELDS) + ")")
        db.execute("INSERT INTO products VALUES (" + ",".join("?" for _ in row) + ")", tuple(row.values()))
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    result = audit_module.audit(source)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert result["products"][0]["before"]["is_active"] == 0
    assert result["statuses"] == {"candidate": 1}


def test_missing_source_database_is_never_created(tmp_path):
    source = tmp_path / "absent.sqlite3"
    with pytest.raises(FileNotFoundError):
        audit_module.audit(source)
    assert not source.exists()
