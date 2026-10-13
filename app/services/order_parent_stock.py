"""One parent-stock qualification for candidate reads and new-order plans."""
from sqlalchemy.orm import object_session
from app.models.multilevel_bom import ProductBomProfile


def parent_inventory_eligible(product, *, db=None, profiles=None):
    if product.is_virtual_composite_parent:
        return False
    db = db or object_session(product)
    profile = (profiles.get(product.id) if profiles is not None else
               db.get(ProductBomProfile, product.id) if db is not None else None)
    if profile and profile.source == "separate":
        return False
    if product.is_composite:
        return bool(profile and profile.source in {"assembled", "manufactured", "purchased"})
    return True


def physical_bom_parent(db, product):
    return bool(product.is_composite and parent_inventory_eligible(product, db=db))
