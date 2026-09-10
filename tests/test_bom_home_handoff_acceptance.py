"""Acceptance of the September 10 export on disposable database copies."""
from sqlalchemy import select, text

from app.models.product import Product
from app.models.product_bom import ProductBomComponent
from app.models.user import User
from app.services.composite_bom import get_product_bom, replace_product_bom
from tests.test_multilevel_bom_factory_compile import factory_copy
from tests.test_multilevel_bom_master_transition import business_rows


def test_other_sixteen_keep_real_components_units_and_business_facts(factory_copy):
    db = factory_copy
    actor = db.scalar(select(User).where(User.role == "admin", User.is_active.is_(True)))
    parents = list(db.scalars(select(Product).where(
        Product.is_active.is_(True), Product.is_composite.is_(True),
        Product.id.not_in([3765, 3799])).order_by(Product.id)))
    assert len(parents) == 16
    before = business_rows(db)
    units = db.execute(text("SELECT id,unit FROM products ORDER BY id")).all()
    edge_columns = [ProductBomComponent.id, ProductBomComponent.parent_product_id,
                    ProductBomComponent.component_product_id, ProductBomComponent.quantity_per_set]
    def edges():
        return db.execute(select(*edge_columns).order_by(ProductBomComponent.id)).all()
    old_edges = edges()
    inactive = db.get(Product, 3494).is_active
    for parent in parents:
        original = get_product_bom(db, parent.id)
        components = [dict(row, inventory_relation="accompany") for row in original["components"]]
        replace_product_bom(db, parent_product_id=parent.id,
            components=components, inventory_mode="manufactured",
            expected_version=parent.version, user=actor)
        db.commit()
        reopened = get_product_bom(db, parent.id)
        assert [(r["component_product_id"], r["quantity_per_set"]) for r in reopened["components"]] == [
            (r["component_product_id"], r["quantity_per_set"]) for r in original["components"]]
        assert all(r["inventory_relation"] == "accompany" for r in reopened["components"])
    assert business_rows(db) == before
    assert db.execute(text("SELECT id,unit FROM products ORDER BY id")).all() == units
    assert edges() == old_edges
    assert db.get(Product, 3494).is_active == inactive is False
