"""Retain processed stock guards with explicit current receipt cost fixtures."""
from decimal import Decimal

import pytest

from tests.test_semi_finished_lot_eligibility import eligibility_db


@pytest.fixture
def priced_eligibility_db(eligibility_db):
    from app.models.material import Material
    from app.models.supplier import Supplier
    db, data = eligibility_db
    db.add(Supplier(standard_name='隔离资格测试供应商', normalized_name='隔离资格测试供应商', is_active=True))
    db.add(Material(code='A416D', supplier_name='隔离资格测试供应商', layer_count=3,
        flute_type='B', quote_price=Decimal('2.20'), price_unit='元/㎡',
        purchase_currency='CNY', purchase_tax_included=True, purchase_tax_rate=Decimal('0.13'), is_active=True))
    db.commit()
    return db, data


def test_registered_mold_and_printing_rules_remain_enforced(priced_eligibility_db):
    from tests.test_processed_sheet_matching import test_blank_same_mold_multiple_codes_automatic_but_printed_not_inferred
    test_blank_same_mold_multiple_codes_automatic_but_printed_not_inferred(priced_eligibility_db)


@pytest.mark.parametrize('field,value', [('flute_type','A'), ('layer_count',5), ('pieces_per_box',2), ('stock_yield_per_sheet',2)])
def test_physical_conversion_differences_are_still_rejected(priced_eligibility_db, field, value):
    from tests.test_processed_sheet_matching import test_physical_and_conversion_conflicts_not_automatically_relaxed
    test_physical_and_conversion_conflicts_not_automatically_relaxed(priced_eligibility_db, field, value)


def test_frozen_crease_height_and_replay_remain_enforced(eligibility_db):
    from tests.test_sheet_measurement import test_crease_trim_reserves_once_and_frozen_height_cannot_be_bypassed
    # This existing crease scenario already creates its own quotation; adding
    # a second same-code supplier would correctly make valuation ambiguous.
    test_crease_trim_reserves_once_and_frozen_height_cannot_be_bypassed(eligibility_db)
