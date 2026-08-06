from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_composite_requisition_keeps_parent_row_and_selects_snapshots() -> None:
    assert "row.is_composite_bom" in INDEX
    assert "component_requirements" in INDEX
    assert "requisitionComponentExpanded" in INDEX
    assert "selectedBomSnapshotIds" in INDEX
    assert "toggleBomSnapshot" in INDEX
    assert "bom_requisition_sources" in INDEX
    assert "订单专用需求" in INDEX
    assert "理论报料" in INDEX


def test_composite_requisition_submits_snapshot_id_to_batch_api() -> None:
    assert 'axios.post("/api/requisition/batches", frozenPayload)' in INDEX
    assert "const frozenPayload = JSON.parse(JSON.stringify(payload))" in INDEX
    assert "bom_snapshot_id:line.bom_snapshot_id ? Number(line.bom_snapshot_id) : null" in INDEX
    assert "actual_yield_per_sheet:line.actual_yield_per_sheet ? Number(line.actual_yield_per_sheet) : null" in INDEX
    assert "openCompositeRequisition" in INDEX


def test_order_form_edits_component_quantities_with_one_order_save() -> None:
    assert "本订单子件数量" in INDEX
    assert "默认随父件成套；客户少要时直接改数量，保存订单即可。" in INDEX
    assert "loadOrderDraftBom" in INDEX
    assert "loadExistingOrderItemBom" in INDEX
    assert "syncOrderBomDemandsForQuantity" in INDEX
    assert "markOrderBomDemandOverride" in INDEX
    assert "resetOrderBomDemand" in INDEX
    assert "product_bom_component_id:Number(component.product_bom_component_id)" in INDEX
    assert "snapshot_id:Number(component.snapshot_id)" in INDEX
    assert ".filter(component => component._manual_override)" in INDEX
    assert ".filter(component => component._dirty)" in INDEX


def test_reported_composite_requisition_has_clear_source_and_void_action() -> None:
    assert 'return "组合 BOM 报料单"' in INDEX
    assert "voidReportedCompositeRequisition(row)" in INDEX
    assert "const targetId = Number(row?.id || 0)" in INDEX
    assert "/api/requisition/batches/${targetId}/void" in INDEX
    assert "executeRequisitionVoidAction" in INDEX
    assert "父件和组件已回到待报料" in INDEX


def test_normal_and_telescoping_requisition_paths_remain_present() -> None:
    assert 'component_type:"cover"' in INDEX
    assert 'component_type:"base"' in INDEX
    assert "if (!isTelescoping)" in INDEX


def test_component_production_tasks_show_piece_quantity_and_destinations() -> None:
    assert "row.is_component_task" in INDEX
    assert "组件需求" in INDEX
    assert "组件直接齐套" in INDEX
    assert "组件入库存" in INDEX
    assert 'disposition: row.completion_mode' in INDEX


def test_delivery_ui_supports_kit_capacity_and_missing_component_hints() -> None:
    assert "deliveryKitInfo" in INDEX
    assert "kit_availability" in INDEX
    assert "missing_components" in INDEX
    assert "按套可发" in INDEX
    assert "缺件：" in INDEX
    assert "remaining_quantity: candidate.remaining_quantity" in INDEX


def test_inventory_source_text_is_backward_compatible_with_component_stock() -> None:
    assert 'source.source_type === "component_stock"' in INDEX
    assert "source.component_code" in INDEX
    assert "source.component_name" in INDEX
    assert "取货${quantity}片" in INDEX
    assert 'source.source_type === "semi_finished" ? "半成品" : "成品"' in INDEX

