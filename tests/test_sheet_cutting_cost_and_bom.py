from decimal import Decimal
from types import SimpleNamespace

from app.services.sheet_cutting_settings import SheetCuttingSettings, theoretical_product_yield, theoretical_order_yield
from app.services.inventory_cost_snapshot import estimate_finished_product_cost
from app.services.bom_physical_quantities import resolve_bom_sheet_yield
from tests.test_multilevel_bom_physical_contract import snapshot


def test_theoretical_inventory_yield_excludes_supplier_splitting():
    settings = {'schema_version': 2, 'whole': SheetCuttingSettings(2, 3, 4, True).to_dict()}
    assert theoretical_product_yield(SimpleNamespace(sheet_cutting_settings=settings)) == 4
    assert theoretical_order_yield(SimpleNamespace(sheet_cutting_settings_snapshot=settings)) == 4


def test_bom_keeps_mold_and_supplier_cutting_independent():
    settings = {'schema_version': 2, 'whole': SheetCuttingSettings(2, 3, 4, True).to_dict()}
    source = snapshot(is_die_cut=True, mold_max_yield_per_sheet=4, sheet_cutting_settings_snapshot=settings)
    output = resolve_bom_sheet_yield(source)
    assert output.yield_per_sheet == 24
    assert output.cutting_factor == 6
    assert resolve_bom_sheet_yield(source, actual_yield_per_sheet=3).yield_per_sheet == 18


def test_supplier_cutting_does_not_discount_per_piece_material_cost(monkeypatch):
    import app.services.inventory_cost_snapshot as module
    material = SimpleNamespace(id=1, code='P', supplier_name='供应商', layer_count=1,
        price_unit='元/平方米', quote_date=None, price_source='test')
    monkeypatch.setattr(module, '_find_material', lambda *a, **kw: material)
    monkeypatch.setattr(module, '_effective_square_price', lambda *a, **kw: (Decimal('2'), {}))
    product = SimpleNamespace(material_id=1, default_material_code='P', legacy_material_text=None,
        material=material, layer_count=1, flute_type='NONE', report_length_mm=340, report_width_mm=200,
        box_style='A3天地盖', base_report_length_mm=300, base_report_width_mm=100, pieces_per_box=2)
    product.sheet_cutting_settings = {'schema_version': 2,
        'cover': SheetCuttingSettings(2, 3, 2, True).to_dict(),
        'base': SheetCuttingSettings(4, 2, 3, True).to_dict()}
    estimate = estimate_finished_product_cost(None, product=product)
    assert estimate.area_m2 == Decimal('0.044')
    assert estimate.unit_cost == Decimal('0.088')
    product.sheet_cutting_settings['schema_version'] = 3
    product.sheet_cutting_settings['cover'] = SheetCuttingSettings(2, 3, 2, True, 680, 630).to_dict()
    # 680*630/12 plus 300*100/3; trim is bought but never extra output.
    assert estimate_finished_product_cost(None, product=product).unit_cost == Decimal('0.0914')
    product.sheet_cutting_settings = None
    assert estimate_finished_product_cost(None, product=product).unit_cost == Decimal('0.196')

    # Existing decimal-size v2 facts retain their original per-theoretical-sheet rounding.
    product.box_style = 'A1/0201 普通开槽箱'
    product.base_report_length_mm = product.base_report_width_mm = None
    product.report_length_mm, product.report_width_mm = Decimal('340.01'), Decimal('200.02')
    product.pieces_per_box = 1
    product.sheet_cutting_settings = {'schema_version': 2, 'whole': SheetCuttingSettings(2, 3, 2, True).to_dict()}
    estimate = estimate_finished_product_cost(None, product=product)
    assert estimate.area_m2 == Decimal('0.034005')
    assert estimate.detail['area_basis'] == 'theoretical_sheet_area_per_mold_output'
