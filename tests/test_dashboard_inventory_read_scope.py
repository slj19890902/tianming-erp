import json
import pytest
from sqlalchemy import event
from types import SimpleNamespace
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.models import Base
from app.models.material import Material
from app.models.customer import Customer
from app.models.product import Product
from app.models.supplier_paper_code import SupplierPaperCode
from app.services.inventory_read_scope import inventory_summary_read_scope
from app.services.warehouse_goods import qualification_issues
from tests.test_semi_finished_lot_eligibility import eligibility_db, _add_lot


def test_summary_read_cache_preserves_matching_and_is_discarded(tmp_path):
    engine = create_engine("sqlite:///" + (tmp_path / "read-scope.sqlite3").as_posix())
    Base.metadata.create_all(engine)
    db = Session(engine)
    customer = Customer(customer_number=1, customer_code="CACHE", name="scope test", payment_term_days=0, credit_limit=0)
    stock = Material(code="X1X", supplier_name="test", layer_count=3)
    target = Material(code="Y1Y", supplier_name="test", layer_count=3)
    face = SupplierPaperCode(supplier_name="test", code_char="Y", paper_name="paper", gram_weight=150, color="white")
    db.add_all([customer, stock, target, face, SupplierPaperCode(supplier_name="test", code_char="X", paper_name="paper", gram_weight=150, color="white")])
    db.flush()
    product = Product(customer_id=customer.id, product_code="P", customer_material_code="P", product_name="product", material=target, box_category="normal")
    db.add(product)
    db.commit()
    # Read qualification only: no inventory creation/cost authorization bypass.
    lot = SimpleNamespace(id=999, semi_finished_detail=SimpleNamespace(layer_count=3, material_id=stock.id, material_code_snapshot="X1X", supplier_name="test"))
    queries = []
    engine = db.get_bind()
    def record(*args):
        queries.append(args[2])
    event.listen(engine, 'before_cursor_execute', record)
    try:
        expected = [qualification_issues(db, lot, product) for _ in range(20)]
        baseline = len(queries)
        queries.clear()
        with inventory_summary_read_scope(db):
            actual = [qualification_issues(db, lot, product) for _ in range(20)]
        assert actual == expected
        assert len(queries) < baseline / 4
        # The write path after the read-only summary must see current color.
        face.color = 'kraft'
        db.commit()
        assert '白面纸与瓦楞色不能互用' in qualification_issues(db, lot, product)
        with inventory_summary_read_scope(db):
            assert '白面纸与瓦楞色不能互用' in qualification_issues(db, lot, product)
    finally:
        event.remove(engine, 'before_cursor_execute', record)
        db.close()
        engine.dispose()


def test_summary_scope_is_session_specific_and_cleans_up_on_failure():
    from app.services.inventory_read_scope import summary_read
    first, second = object(), object()
    calls = []
    def read():
        calls.append(1)
        return len(calls)
    try:
        with inventory_summary_read_scope(first):
            assert summary_read(first, 'missing', lambda: None) is None
            assert summary_read(first, 'missing', read) is None
            assert summary_read(second, 'missing', read) == 1
            raise ValueError('summary aborted')
    except ValueError:
        pass
    assert summary_read(first, 'missing', read) == 2


@pytest.mark.parametrize('abort', [False, True])
def test_bulk_summary_keeps_missing_rows_scoped_and_write_reads_fresh(eligibility_db, abort):
    from app.models.warehouse_goods import WarehouseGoodsProfile
    from app.services.inventory_read_scope import summary_prefetch, summary_get
    from tests.test_processed_sheet_matching import setup
    db, data = eligibility_db
    product, lot, profile, facts = setup(db, data, approved=True)
    missing_lot = _add_lot(db, data, key='profile-not-yet-present',
                           customer_id=product.customer_id)
    db.flush()
    statements = []

    def record(conn, cursor, statement, *rest):
        if 'warehouse_goods_profiles' in statement:
            statements.append(statement)

    engine = db.get_bind()
    event.listen(engine, 'before_cursor_execute', record)
    try:
        identities = [lot.id, missing_lot.id, *range(1000000, 1001100)]
        try:
            with inventory_summary_read_scope(db):
                summary_prefetch(db, WarehouseGoodsProfile, identities)
                assert len(statements) == 3  # bounded parameter batches, not per identity
                for identity in identities:
                    row = summary_get(db, WarehouseGoodsProfile, identity)
                    assert (row is not None) == (identity == lot.id)
                summary_prefetch(db, WarehouseGoodsProfile, identities)
                assert len(statements) == 3
                with Session(engine) as other:
                    summary_prefetch(other, WarehouseGoodsProfile, identities)
                assert len(statements) == 3  # cannot seed another Session's reads
                if abort:
                    raise ValueError('synthetic abandoned read')
        except ValueError:
            assert abort
        before = len(statements)
        summary_prefetch(db, WarehouseGoodsProfile, identities)
        assert len(statements) == before  # ordinary writer never enables this cache
        new_profile = WarehouseGoodsProfile(lot_id=missing_lot.id,
                                            data_json=json.dumps(facts))
        db.add(new_profile)
        db.flush()
        with inventory_summary_read_scope(db):
            summary_prefetch(db, WarehouseGoodsProfile, [missing_lot.id])
            assert summary_get(db, WarehouseGoodsProfile, missing_lot.id) is new_profile
        assert summary_get(db, WarehouseGoodsProfile, missing_lot.id) is new_profile
    finally:
        event.remove(engine, 'before_cursor_execute', record)


@pytest.mark.parametrize('mode', ['approved', 'unconfirmed', 'excluded_customer'])
def test_prefetched_stock_qualification_keeps_customer_and_shape_guards(eligibility_db, mode):
    from tests.test_processed_sheet_matching import setup, candidates
    db, data = eligibility_db
    product, lot, profile, facts = setup(db, data, approved=mode == 'approved')
    if mode == 'excluded_customer':
        facts['customer_ids'] = [data['other_customer'].id]
        profile.data_json = json.dumps(facts)
        db.flush()
    expected = candidates(db, product)
    with inventory_summary_read_scope(db):
        actual = candidates(db, product)
    assert actual == expected
    if mode == 'approved':
        assert actual and actual[0].direct_deduction_eligible
    elif mode == 'excluded_customer':
        assert not actual
    else:
        assert actual and not actual[0].direct_deduction_eligible


def test_approval_scope_is_discarded_before_an_ordinary_qualification_read(eligibility_db):
    from app.core.time_contract import utc_now_naive
    from app.models.warehouse_inventory import SemiFinishedLotAllowedProduct
    from tests.test_processed_sheet_matching import setup, candidates
    db, data = eligibility_db
    product, lot, profile, facts = setup(db, data, approved=False)
    with inventory_summary_read_scope(db):
        assert not candidates(db, product)[0].direct_deduction_eligible
        assert not candidates(db, product)[0].direct_deduction_eligible
    db.add(SemiFinishedLotAllowedProduct(inventory_lot_id=lot.id, product_id=product.id,
                                       confirmed_by=data['admin'].id,
                                       confirmed_at=utc_now_naive()))
    db.flush()
    assert candidates(db, product)[0].direct_deduction_eligible
    with inventory_summary_read_scope(db):
        assert candidates(db, product)[0].direct_deduction_eligible
