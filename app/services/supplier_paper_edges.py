"""Supplier trimmed edges are independent of the board's crease processing."""
def product_supplier_edge(product, component='whole'):
    if product is None:
        return None
    prefix = 'base_' if component == 'base' else ''
    explicit = getattr(product, prefix+'supplier_edge_type', None)
    if explicit in ('毛片','净料'):
        return explicit
    crease = getattr(product, prefix+'crease_type', None)
    return crease if crease in ('毛片','净料') else None
