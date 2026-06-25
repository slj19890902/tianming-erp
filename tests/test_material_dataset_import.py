"""Tests for scripts/import_material_dataset.py (v0.19.2 Hotfix material sync).

Covers the pure parsing/normalization logic that guarantees the new 3-supplier
xlsx is mapped correctly into the `materials` master table:

  * supplier alias normalization (no full-name / short-name duplicates)
  * flute derivation isolates 三层 `B/E` from 五层 `BE` (no cross-layer bleed)
  * layer / price / weight-structure parsing
  * dry-run never writes (importer default mode)
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "import_material_dataset", ROOT / "scripts" / "import_material_dataset.py"
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


class TestSupplierNormalization:
    def test_full_and_short_collapse_to_one_standard(self):
        std = "苏州嘉林亿"
        assert mod.normalize_supplier("苏州嘉林亿包装科技有限公司") == std
        assert mod.normalize_supplier("嘉林亿") == std
        assert mod.normalize_supplier("苏州嘉林亿") == std

    def test_mingpeng_aliases(self):
        assert mod.normalize_supplier("鸣朋") == "昆山鸣朋"
        assert mod.normalize_supplier("昆山鸣朋纸板") == "昆山鸣朋"
        assert mod.normalize_supplier("昆山鸣朋") == "昆山鸣朋"

    def test_jiafeng_aliases(self):
        assert mod.normalize_supplier("佳丰") == "苏州佳丰"
        assert mod.normalize_supplier("苏州佳丰纸板") == "苏州佳丰"

    def test_no_duplicate_standard_names(self):
        names = {mod.normalize_supplier(x) for x in
                 ["苏州嘉林亿包装科技有限公司", "嘉林亿", "鸣朋", "昆山鸣朋纸板", "佳丰", "苏州佳丰纸板"]}
        assert names == {"苏州嘉林亿", "昆山鸣朋", "苏州佳丰"}

    def test_unknown_supplier_passthrough(self):
        assert mod.normalize_supplier("某新供应商") == "某新供应商"
        assert mod.normalize_supplier(None) is None


class TestFluteDerivation:
    def test_three_layer_combo_be_is_not_five_layer_be(self):
        # 特价单后缀 -B/E 是三层（B瓦或E瓦），绝不是五层 BE
        assert mod.derive_flute("D4B-B/E", 3, "") == "B/E"

    def test_five_layer_combo(self):
        assert mod.derive_flute("X-AB/EB", 5, "") == "AB/BE"

    def test_five_layer_source_combo(self):
        assert mod.derive_flute("X1", 5, "五层BA/EB楞组合") == "AB/BE"

    def test_plain_three_layer(self):
        assert mod.derive_flute("D4B", 3, "") == "B"

    def test_plain_five_layer(self):
        assert mod.derive_flute("Z9", 5, "") == "AB"

    def test_seven_layer_unresolved(self):
        assert mod.derive_flute("Q7", 7, "") is None


class TestParsers:
    def test_parse_layer(self):
        assert mod.parse_layer("3层") == 3
        assert mod.parse_layer(3) == 3
        assert mod.parse_layer("5") == 5
        assert mod.parse_layer(None) is None

    def test_parse_price(self):
        assert float(mod.parse_price("3.85")) == 3.85
        assert mod.parse_price("") is None
        assert mod.parse_price("abc") is None

    def test_parse_quote_date(self):
        d = mod.parse_quote_date("报价单 2026-05-01 版本")
        assert (d.year, d.month, d.day) == (2026, 5, 1)
        assert mod.parse_quote_date("无日期") is None

    def test_single_cell_weight(self):
        assert mod._g("150") == "150g"
        assert mod._g("150g") == "150g"
        assert mod._g(None) is None


class TestWeightStructure:
    def test_three_layer_uses_face_core_liner(self):
        # 列: A B C D面 E芯1 F中夹 G芯2 H里
        row = ("供", "C1", "三层", 150, 100, None, None, 120, 3.0, "", "", "")
        assert mod.build_weight_structure(row, 3) == "150g/100g/120g"

    def test_five_layer_uses_all_five(self):
        row = ("供", "C1", "五层", 150, 100, 90, 100, 120, 3.0, "", "", "")
        assert mod.build_weight_structure(row, 5) == "150g/100g/90g/100g/120g"


class TestDryRunNoWrite:
    def test_dry_run_does_not_call_apply_branch(self, monkeypatch, tmp_path):
        """run(apply=False) must never invoke backup_db (the write gate)."""
        called = {"backup": False, "commit": False}
        monkeypatch.setattr(mod, "load_xlsx", lambda p: [])
        monkeypatch.setattr(mod, "backup_db", lambda: called.__setitem__("backup", True))

        class _DummySession:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def scalars(self, *a, **k):
                class _R:
                    def all(self_inner): return []
                return _R()
            def execute(self, *a, **k):
                class _R:
                    def all(self_inner): return []
                return _R()
            def commit(self): called["commit"] = True

        monkeypatch.setattr(mod, "Session", lambda engine: _DummySession())
        monkeypatch.setattr(mod, "create_sqlite_engine", lambda p: object())
        monkeypatch.setattr(mod, "referenced_codes", lambda s: set())

        mod.run(tmp_path / "nope.xlsx", apply=False)
        assert called["backup"] is False
        assert called["commit"] is False
