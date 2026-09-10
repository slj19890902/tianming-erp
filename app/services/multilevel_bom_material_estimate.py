"""Gross theoretical board inputs, never stock netting or actual-cost facts."""

from app.services.bom_physical_quantities import resolve_bom_sheet_yield
from app.services.multilevel_bom_orders import read_compiled_order_bom
from app.services.multilevel_bom_plan import BomPlanError, plan_bom

ROUTE_LABELS = {"whole": "主片", "cover": "盖片", "base": "底片"}


def graph_material_estimate_inputs(db, item):
    compiled = read_compiled_order_bom(db, item.id)
    if compiled is None:
        raise BomPlanError("多级BOM材料快照缺少冻结关系，不能按旧子件估算")
    return compiled_material_estimate_inputs(compiled, item.quantity)


def compiled_material_estimate_inputs(compiled, quantity):
    """Read-only detached inputs for an explicit proposed recipe review."""
    graph = compiled.graph
    nodes = {node.product_id: node for node in graph.nodes}
    snapshots = {row.component_product_id: row for row in compiled.snapshots}
    sources = []
    for demand in plan_bom(graph, quantity).materials:
        node = nodes[demand.product_id]
        snapshot = snapshots[node.product_id]
        output = resolve_bom_sheet_yield(snapshot, strict=True)
        is_base = demand.route_key == "base"
        sources.append({
            "source_type": "bom_graph_material",
            "label": f"{node.name}·{ROUTE_LABELS[demand.route_key]}",
            "length_mm": snapshot.snapshot_component_base_report_length_mm if is_base else snapshot.snapshot_component_report_length_mm,
            "width_mm": snapshot.snapshot_component_base_report_width_mm if is_base else snapshot.snapshot_component_report_width_mm,
            "required_piece_qty": demand.required_pieces,
            "cutting_mode": output.cutting_mode,
            "physical_yield": output.yield_per_sheet,
            "material_id": snapshot.snapshot_component_material_id,
            "supplier_name": snapshot.snapshot_component_supplier_name,
            "layer_count": snapshot.snapshot_component_layer_count,
            "flute_type": snapshot.snapshot_component_flute_type,
            "spare_sheet_quantity": int(snapshot.spare_sheet_quantity or 0),
            "source_identity": {"product_id": node.product_id,
                "bom_snapshot_id": snapshot.id, "route_key": demand.route_key},
        })
    return sources, []
