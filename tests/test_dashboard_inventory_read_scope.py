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
