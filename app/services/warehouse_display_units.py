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
    detail = lot.finished_detail
    product = detail.product if detail else None
    if product is not None and product.unit in ("套", "片"):
        return product.unit
    if lot.source_ref_type == "subkit_receipt":
        return "片"
    return lot.unit
