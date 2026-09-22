"""Managed figures survive the legacy receipt-paper compatibility projection."""
from datetime import datetime
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.drawing_adoption import DrawingAdoptionWrite, adopt_drawing
from app.api.drawing_design import DesignWrite, PublishWrite, publish_design, save_design
from app.api.incoming import incoming_production_card
from app.models.drawing_design import DrawingRelease
from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
from app.models.mold_tool import MoldTool
from app.models.order import OrderItem
from app.models.product import Product
from app.models.production import ProductionTask
from app.models.user import User
from app.services.drawing_binding import bound_task_release
from app.services.secure_uploads import resolve_stored_reference
from test_p1_32a2_requisition_production_print import production_print_app


@pytest.mark.parametrize('template', ['liner_v1','slotted_v1','custom_21301634_v1'])
def test_adopted_legacy_receipt_uses_fixed_figure_mold_and_quantities(production_print_app,tmp_path,monkeypatch,template):
    monkeypatch.setenv('ERP_FILE_STORAGE_DIR',str(tmp_path/'files'))
    params={'liner_v1':{}, 'slotted_v1':dict(panel_1_mm=400,panel_2_mm=300,panel_3_mm=400,
        panel_4_mm=300,body_height_mm=200,top_flap_mm=150,bottom_flap_mm=150,glue_flap_mm=30,slot_width_mm=5),
        'custom_21301634_v1':dict(top_cover_mm=150,bottom_cover_mm=150,top_fold_mm=28,bottom_fold_mm=28,
          left_fold_mm=26,right_fold_mm=26,left_wing_mm=40,right_wing_mm=40)}[template]
    panel={'liner_v1':'face','slotted_v1':'panel_1','custom_21301634_v1':'center'}[template]
    with production_print_app['session_factory']() as db:
        product=db.get(Product,production_print_app['product_id'])
        user=db.scalar(select(User).where(User.role=='admin'))
        item=db.get(OrderItem,production_print_app['order_item_id'])
        task=db.scalar(select(ProductionTask).where(ProductionTask.order_item_id==item.id))
        mold=MoldTool(mold_code='UAT-RECEIPT-MOLD',mold_name='隔离原模具',rack_location='1F-M-R03-L1-G02')
        db.add(mold); db.flush()
        product.mold_tool_id=mold.id
        if template=='slotted_v1':
            product.box_style='A1/0201'; product.splice_mode='single'; product.pieces_per_box=1
        receipt=IncomingReceipt(receipt_number='UAT-LEGACY-RECEIPT',received_at=datetime(2026,9,22),
                               received_by=user.id,idempotency_key='legacy-receipt-uat')
        db.add(receipt); db.flush()
        fact=IncomingReceiptItem(receipt_id=receipt.id,order_id=item.order_id,order_item_id=item.id,
            planned_quantity=200,received_quantity=200,cumulative_received_quantity=200,
            variance_quantity=0,variance_type='matched',resolution_status='not_required',status='posted')
        db.add(fact); db.commit()
        before=incoming_production_card(fact.id,db,user)
        assert not before['cards'][0].get('managed_drawings')
        baseline={k:before[k] for k in ['received_sheet_quantity','planned_sheet_quantity','output_factor','production_capacity_quantity']}
        draft=DesignWrite(expected_product_version=product.version,template_key=template,parameters=params,
                          thickness_mm=3,thickness_source='隔离核对',customer_revision=' 客户原版次A ')
        save_design(product.id,draft,db,user)
        first=publish_design(product.id,PublishWrite(expected_product_version=product.version,
            expected_design_version=1,idempotency_key='receipt-first-release'),db,user)
        assert first['revision']==' 客户原版次A '  # independent of absent customer number
        adopt_drawing(task.id,DrawingAdoptionWrite(release_id=first['id'],expected_task_version=task.version,
            idempotency_key='receipt-explicit-adoption',confirmed_not_issued=True),db,user)
        plain=incoming_production_card(fact.id,db,user)
        assert {k:plain[k] for k in baseline}==baseline
        drawing=plain['cards'][0]['managed_drawings'][0]
        assert drawing['release_id']==first['id'] and not drawing['print_objects']
        assert drawing['svg_urls']['structure']==drawing['svg_urls']['print']
        component=plain['cards'][0]['components'][0]
        assert component['mold_name']=='隔离原模具' and component['mold_location']==mold.rack_location
        assert plain['paper_fingerprint']!=before['paper_fingerprint']
        draft.expected_design_version=1; draft.customer_revision=' 客户原版次B '
        draft.print_objects=[dict(kind='text',text='隔离印刷',panel_id=panel,x_mm=5,y_mm=5,width_mm=50,height_mm=20,rotation_deg=0)]
        draft=DesignWrite(**draft.model_dump())
        save_design(product.id,draft,db,user)
        second=publish_design(product.id,PublishWrite(expected_product_version=product.version,
            expected_design_version=2,idempotency_key='receipt-second-release'),db,user)
        assert incoming_production_card(fact.id,db,user)['cards'][0]['managed_drawings'][0]==drawing
        adopt_drawing(task.id,DrawingAdoptionWrite(release_id=second['id'],expected_task_version=task.version,
            idempotency_key='receipt-explicit-adoption-2',confirmed_not_issued=True),db,user)
        printed=incoming_production_card(fact.id,db,user)
        current=printed['cards'][0]['managed_drawings'][0]
        assert current['release_id']==second['id'] and current['print_objects'][0]['text']=='隔离印刷'
        assert current['svg_urls']['structure']!=current['svg_urls']['print']
        assert {k:printed[k] for k in baseline}==baseline
        assert bound_task_release(db,task.id).id==second['id']
        manifest=json.loads(db.get(DrawingRelease,second['id']).manifest_json)
        resolve_stored_reference(manifest['svg_snapshots']['print']['reference']).write_bytes(b'broken')
        with pytest.raises(HTTPException) as failure:
            incoming_production_card(fact.id,db,user)
        assert failure.value.status_code==503
