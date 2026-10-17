"""Describe a legacy frozen die without inventing finished-piece dimensions."""
import hashlib
import json
from types import SimpleNamespace


def completion_basis(db, item, snapshot):
    original = snapshot['physical_basis']
    basis = json.loads(original)
    if str(basis.get('spec') or '').strip():
        return original, None
    identity = snapshot.get('purchase_identity')
    if not isinstance(identity, dict):
        return original, None
    from app.services.stock_purchase_identity import _view, resolve
    from app.models.mold_tool import MoldTool
    from app.services.product_specification import product_stock_specification
    frozen = resolve(db, item)
    if (identity != frozen or identity.get('physical_basis') != original
            or basis.get('product_id') != snapshot.get('product_id')):
        return original, None
    view = _view(identity, item)
    mold_id = view.mold_tool_id
    if mold_id is None or str(basis.get('mold_tool_id')) != str(mold_id):
        return original, None
    mold = db.get(MoldTool, mold_id)
    # Use frozen product fields and frozen cutting rules, never today's product
    # dimensions, die selection, or yield. Active registry validates identity.
    projection = SimpleNamespace(
        length_mm=None, width_mm=None, height_mm=None,
        product_code=view.product_code, mold_tool_id=mold_id, mold_tool=mold,
        sheet_cutting_settings=view.sheet_cutting_settings,
    )
    specification = product_stock_specification(projection)
    if specification is None:
        return original, None
    basis['spec'] = specification
    encoded = json.dumps(basis, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return encoded, {
        'source': 'frozen_purchase_registered_die', 'mold_tool_id': mold_id,
        'original_basis_sha256': hashlib.sha256(original.encode()).hexdigest(),
        'output_basis_sha256': hashlib.sha256(encoded.encode()).hexdigest(),
        'dimensions_invented': False,
    }
