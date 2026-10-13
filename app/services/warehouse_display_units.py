"""Employee product units; never change the underlying ledger unit contract."""
def lot_display_unit(lot):
    if lot.source_ref_type == 'direct_external_receipt':
        import json
        try:
            snapshot = json.loads(lot.cost_snapshot_detail_json or '{}')
            unit = snapshot.get('stock_unit') if isinstance(snapshot, dict) else None
            if unit:
                return {'boxes': '只', 'pieces': '片'}.get(unit, unit)
        except (ValueError, TypeError):
            pass
    import json
    try:
        basis = json.loads(lot.finished_detail.physical_basis_json or '{}') if lot.finished_detail else {}
        if not isinstance(basis, dict):
            basis = {}
        quantity_basis = basis.get('quantity_basis') or {}
        if not isinstance(quantity_basis, dict):
            quantity_basis = {}
        if quantity_basis.get('ledger') == 'physical' and quantity_basis.get('physical_unit'):
            return quantity_basis['physical_unit']
        if basis.get('unit'):
            from app.services.product_unit_labels import basis_unit_label
            return basis_unit_label({**basis, 'quantity_basis': quantity_basis}, component=lot.source_ref_type == 'subkit_receipt')
    except (ValueError, TypeError):
        pass
    detail = lot.finished_detail
    product = detail.product if detail else None
    if product is not None:
        from app.services.product_unit_labels import product_unit_label
        return product_unit_label(product) or lot.unit
    if getattr(lot, "semi_finished_detail", None):
        from sqlalchemy.orm import object_session
        db = object_session(lot)
        if db is not None:
            from app.services.processed_component_stock import output_identity
            try:
                identity = output_identity(db, lot)
            except (ValueError, TypeError, AttributeError):
                identity = None
            if identity:
                from app.services.product_unit_labels import basis_unit_label
                return basis_unit_label(json.loads(identity[2]), component=True)
    if lot.source_ref_type == "subkit_receipt":
        return "片"
    return {"boxes":"只","sheets":"张","pieces":"片","sets":"套"}.get(lot.unit, lot.unit)
