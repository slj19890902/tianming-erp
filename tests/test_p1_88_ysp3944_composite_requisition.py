from __future__ import annotations

import json
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from test_n039_composite_bom_requisition import (
    _component_payload,
    _login,
    _parent_payload,
    composite_requisition_app,
)
from test_p1_50b_common_box_label_policy_save import (
    _login as _product_login,
    _update_payload,
    product_api_app,
)


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_composite_double_splice_parent_uses_two_sheets_per_carton(
    composite_requisition_app,
) -> None:
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.models.requisition import RequisitionItem

    app, session_factory = composite_requisition_app
    with session_factory() as session:
        product = session.scalar(
            select(Product).where(Product.product_code == "KIT-001")
        )
        item = session.get(OrderItem, 1)
        assert product is not None
        assert item is not None
        product.box_style = "A1/0201 普通开槽箱"
        product.splice_mode = "double"
        product.pieces_per_box = 2
        item.snapshot_splice_mode = "double"
        item.snapshot_pieces_per_box = 2
        session.commit()

    with TestClient(app) as client:
        _login(client)
        pending = client.get("/api/requisition/pending")
        assert pending.status_code == 200, pending.text
        parent = pending.json()["items"][0]["parent_requirement"]
        assert parent["required_piece_quantity"] == 20
        assert parent["remaining_required_piece_qty"] == 20
        assert parent["requisition_qty"] == 20

        created = client.post(
            "/api/requisition/batches",
            json={
                "supplier_name": "N039 供应商",
                "items": [
                    _parent_payload(),
                    _component_payload(1),
                    _component_payload(2),
                ],
            },
        )
        assert created.status_code == 201, created.text

    with session_factory() as session:
        parent_line = session.scalar(
            select(RequisitionItem)
            .where(RequisitionItem.order_item_id == 1)
            .order_by(RequisitionItem.id)
        )
        assert parent_line is not None
        assert parent_line.pieces_per_box == 2
        assert parent_line.required_piece_qty == 20
        assert parent_line.requisition_qty == 20


def test_supplier_print_merges_identical_component_physical_lines_but_keeps_sources(
    composite_requisition_app,
) -> None:
    from app.models.product_bom import (
        RequisitionItemBomSource,
        SalesOrderItemBomComponent,
    )
    from app.models.requisition import RequisitionItem

    app, session_factory = composite_requisition_app
    with session_factory() as session:
        snapshots = session.scalars(
            select(SalesOrderItemBomComponent)
            .where(SalesOrderItemBomComponent.sales_order_item_id == 1)
            .order_by(SalesOrderItemBomComponent.id)
        ).all()
        assert len(snapshots) == 2
        snapshots[1].snapshot_component_report_length_mm = snapshots[
            0
        ].snapshot_component_report_length_mm
        snapshots[1].snapshot_component_report_width_mm = snapshots[
            0
        ].snapshot_component_report_width_mm
        snapshots[1].snapshot_component_spec = snapshots[0].snapshot_component_spec
        for snapshot in snapshots:
            snapshot.snapshot_component_crease_type = "毛片"
        session.commit()

    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/requisition/batches",
            json={
                "supplier_name": "N039 供应商",
                "items": [
                    _parent_payload(),
                    _component_payload(1),
                    _component_payload(2),
                ],
            },
        )
        assert created.status_code == 201, created.text
        printed = client.get(f"/api/requisition/batches/{created.json()['id']}/print")
        assert printed.status_code == 200, printed.text

    items = printed.json()["items"]
    assert len(items) == 2
    parent, components = items
    assert parent["quantity"] == 10
    assert components["quantity"] == 50
    assert components["source_count"] == 2
    assert {"COMP-A", "COMP-B"}.issubset(set(components["product_codes"]))
    with session_factory() as session:
        assert len(session.scalars(select(RequisitionItem)).all()) == 3
        sources = session.scalars(
            select(RequisitionItemBomSource).order_by(RequisitionItemBomSource.id)
        ).all()
        assert [row.sales_order_item_bom_component_id for row in sources] == [1, 2]


def _composite_grouping_helpers() -> str:
    start = INDEX_HTML.index("compositePhysicalMergeKey(line) {")
    end = INDEX_HTML.index("async openCompositeRequisition(rows)", start)
    payload_start = INDEX_HTML.index("requisitionBatchLinePayload(line) {", end)
    payload_end = INDEX_HTML.index("async openRequisition()", payload_start)
    return INDEX_HTML[start:end] + INDEX_HTML[payload_start:payload_end]


def test_browser_groups_same_physical_components_and_expands_traceable_payloads() -> (
    None
):
    script = f"""
const helpers = ({{{_composite_grouping_helpers()}}});
const context = {{
  ...helpers,
  normalizeCuttingMode:value=>String(value || '一开一'),
  cuttingModeFactor:()=>1,
  purchasePurposeAuthoritativeOrderQty:line=>Number(line._authoritative_order_purpose_sheet_qty ?? line.requisition_qty ?? 0),
  purchasePurposePayload:line=>line.purpose_plan_version ? ({{
    purchase_total_sheet_qty:Number(line.purchase_total_sheet_qty || 0),
    order_purpose_sheet_qty:Number(line.order_purpose_sheet_qty || 0),
    stock_purpose_sheet_qty:Number(line.stock_purpose_sheet_qty || 0),
    purpose_plan_version:Number(line.purpose_plan_version),
    purpose_plan_fingerprint:line.purpose_plan_fingerprint,
  }}) : ({{}}),
}};
const base = {{
  is_composite_component:true, is_die_cut:false, order_item_id:9,
  snapshot_supplier_name:'同一供应商', material_id:7, material_display:'K=A / AB',
  layer_count:5, flute_type:'AB', cardboard_len:730, cardboard_width:610,
  crease_type:'毛片', crease_left_mm:null, crease_middle_mm:null, crease_right_mm:null,
  special_process:'一开一', cutting_mode:'一开一', component_type:'whole',
  finished_inventory_reserved_qty:0, semi_finished_reserved_piece_qty:0,
}};
const sourceLines = [
  {{...base,bom_snapshot_id:11,product_code:'YSP3944-A',product_name:'圆衬板',required_piece_qty:30,remaining_required_piece_qty:30,requisition_qty:30,purchase_total_sheet_qty:30,order_purpose_sheet_qty:30,stock_purpose_sheet_qty:0,_minimum_requisition_qty:30,_authoritative_order_purpose_sheet_qty:30,purpose_plan_version:1,purpose_plan_fingerprint:'a'.repeat(64)}},
  {{...base,bom_snapshot_id:12,product_code:'YSP3944-B',product_name:'纸护角',required_piece_qty:120,remaining_required_piece_qty:120,requisition_qty:120,purchase_total_sheet_qty:120,order_purpose_sheet_qty:120,stock_purpose_sheet_qty:0,_minimum_requisition_qty:120,_authoritative_order_purpose_sheet_qty:120,purpose_plan_version:1,purpose_plan_fingerprint:'b'.repeat(64)}},
];
const fresh = () => sourceLines.map(row => ({{...row}}));
const grouped = context.groupCompositeRequisitionLines(fresh());
if (grouped.length !== 1) throw new Error(`expected one row, got ${{grouped.length}}`);
if (grouped[0].purchase_total_sheet_qty !== 150 || grouped[0].required_piece_qty !== 150) throw new Error('quantity not conserved');
grouped[0].purchase_total_sheet_qty = 155;
grouped[0].order_purpose_sheet_qty = 150;
grouped[0].stock_purpose_sheet_qty = 5;
const payloads = context.requisitionBatchLinePayloads(grouped[0]);
const differentSize = fresh(); differentSize[1].cardboard_width = 611;
const missingCrease = fresh(); missingCrease[1].crease_type = '';
const differentMaterial = fresh(); differentMaterial[1].material_id = 8;
const differentSourceRole = fresh(); differentSourceRole[1].component_type = 'cover';
console.log(JSON.stringify({{
  grouped:grouped.length,
  sourceCount:grouped[0]._merged_component_lines.length,
  payloadCount:payloads.length,
  requisitionQty:payloads.map(row=>row.requisition_qty),
  orderPurposeQty:payloads.map(row=>row.order_purpose_sheet_qty),
  stockPurposeQty:payloads.map(row=>row.stock_purpose_sheet_qty),
  snapshots:payloads.map(row=>row.bom_snapshot_id),
  differentSizeRows:context.groupCompositeRequisitionLines(differentSize).length,
  missingCreaseRows:context.groupCompositeRequisitionLines(missingCrease).length,
  differentMaterialRows:context.groupCompositeRequisitionLines(differentMaterial).length,
  differentSourceRoleRows:context.groupCompositeRequisitionLines(differentSourceRole).length,
}}));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert json.loads(completed.stdout) == {
        "grouped": 1,
        "sourceCount": 2,
        "payloadCount": 2,
        "requisitionQty": [30, 125],
        "orderPurposeQty": [30, 120],
        "stockPurposeQty": [0, 5],
        "snapshots": [11, 12],
        "differentSizeRows": 2,
        "missingCreaseRows": 2,
        "differentMaterialRows": 2,
        "differentSourceRoleRows": 1,
    }


def test_common_box_composite_controls_and_production_note_layout() -> None:
    product_editor = INDEX_HTML.split(
        "<div v-else-if=\"modal.type === 'product'\">", 1
    )[1].split("<div v-else-if=\"modal.type === 'material'\">", 1)[0]
    material_index = product_editor.index("材质（代码｜供应商｜克重｜报价）")
    note_index = product_editor.index("生产备注说明")
    bom_index = product_editor.index("启用组合产品")
    fulfillment_index = product_editor.index("交付标签和成品方式")
    assert material_index < note_index < bom_index
    assert abs(fulfillment_index - bom_index) < 1200
    assert "父件按套下单，组件用于内部生产。" not in product_editor
    assert "订单会冻结本次选择" not in product_editor
    assert 'v-model.trim="productForm.production_notes"' in product_editor


def test_common_box_production_note_saves_reloads_and_versions(product_api_app) -> None:
    app, _factory = product_api_app
    note = "  红色标识朝外，模切边缘重点检查  "
    with TestClient(app) as client:
        _product_login(client)
        current = client.get("/api/master/products/1")
        assert current.status_code == 200, current.text
        saved = client.put(
            "/api/master/products/1",
            json=_update_payload(current.json(), production_notes=note),
        )
        assert saved.status_code == 200, saved.text
        reloaded = client.get("/api/master/products/1")
        assert reloaded.status_code == 200, reloaded.text

    assert saved.json()["production_notes"] == note.strip()
    assert reloaded.json()["production_notes"] == note.strip()
    assert reloaded.json()["version"] == current.json()["version"] + 1
