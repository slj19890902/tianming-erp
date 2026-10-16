"""Customer identity rules frozen into new labels, never inferred on reprints."""
from sqlalchemy import select
from app.models.customer import Customer
from app.models.product import Product

DRAWING_CUSTOMER_CODES = frozenset({'YG', 'GY'})
DRAWING_PRODUCT_PROFILE = 'customer-drawing-code-quantity-v1'


def uses_drawing_label(customer) -> bool:
    return str(getattr(customer, 'customer_code', '') or '').strip().upper() in DRAWING_CUSTOMER_CODES


def freeze_product_label_profiles(db, plans, labels):
    """Attach profile and drawing before the caller computes its fingerprint."""
    ids = {int(p['product_id']) for p in plans if p.get('product_id')}
    products = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(ids))).all()} if ids else {}
    profiles = {}
    for plan in plans:
        product = products.get(plan.get('product_id'))
        customer = db.get(Customer, plan.get('customer_id')) if plan.get('customer_id') else None
        if not uses_drawing_label(customer):
            continue
        if product is None or product.customer_id != customer.id:
            raise ValueError('产品标签客户与常用箱身份不一致')
        profile = dict(label_profile=DRAWING_PRODUCT_PROFILE,
                       drawing_number=str(product.customer_drawing_number or '').strip())
        plan.update(profile)
        profiles[str(plan['selection_key'])] = profile
    for label in labels:
        profile = profiles.get(str(label.get('selection_key')))
        if profile:
            label.update(profile)
