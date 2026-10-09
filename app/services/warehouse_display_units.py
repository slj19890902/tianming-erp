"""Employee product units; never change the underlying ledger unit contract."""
def lot_display_unit(lot):
    if lot.source_ref_type == 'direct_external_receipt':
        import json
        try:
            unit = json.loads(lot.cost_snapshot_detail_json or '{}').get('stock_unit')
            if unit:
                return {'boxes': '只', 'pieces': '片'}.get(unit, unit)
        except (ValueError, TypeError):
            pass
    import json
    try:
        basis = json.loads(lot.finished_detail.physical_basis_json or '{}') if lot.finished_detail else {}
        quantity_basis = basis.get('quantity_basis') or {}
        if quantity_basis.get('ledger') == 'physical' and quantity_basis.get('physical_unit'):
            return quantity_basis['physical_unit']
        if basis.get('unit'):
            from app.services.product_unit_labels import basis_unit_label
            return basis_unit_label(basis, component=lot.source_ref_type == 'subkit_receipt')
    except (ValueError, TypeError):
        pass
    detail = lot.finished_detail
    product = detail.product if detail else None
    if product is not None:
        from app.services.product_unit_labels import product_unit_label
        return product_unit_label(product) or lot.unit
    if lot.source_ref_type == "subkit_receipt":
        return "片"
    return {"boxes":"只","sheets":"张","pieces":"片","sets":"套"}.get(lot.unit, lot.unit)
