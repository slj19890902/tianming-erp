from datetime import date
from decimal import Decimal
import json
import pytest
from sqlalchemy import select
from test_warehouse_goods import lot_db, prepare, material, stocktake_app
from app.models.supplier_paper_code import SupplierPaperCode
from app.models.supplier import Supplier
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot, Floor3LocationLayout
from app.api.materials import SupplierPaperCodePayload, update_supplier_paper_code
from app.api.warehouse_goods import GoodsFacts, GoodsUpdate, SheetEntry, create_sheet, update_goods, material_price
from app.services.paper_color import material_face
from app.services.warehouse_goods import qualification_issues
from app.services.warehouse_inventory import WarehouseInventoryError
from app.services.semi_finished_inventory import ensure_semi_finished_lot_eligibility, SemiFinishedSignature
from app.services.inventory_cost_snapshot import estimate_from_snapshot
from app.services.supplier_master import normalize_supplier_identity


def paper(db,char,color='kraft'):
    row=SupplierPaperCode(supplier_name='测试供应商',code_char=char,paper_name='测试纸',gram_weight=150,color=color)
    db.add(row);db.flush();return row


def test_supplier_paper_color_default_edit_and_omitted_client_preserve(lot_db):
    db,data,lot,user=prepare(lot_db)
    db.add(Supplier(standard_name='测试供应商',normalized_name=normalize_supplier_identity('测试供应商'),display_name='测试供应商',is_active=True,version=1))
    row=paper(db,'A');m=material(db,'A1B');db.commit()
    assert material_face(db,m)=='kraft'
    payload=SupplierPaperCodePayload(supplier_name='测试供应商',code_char='A',paper_name='测试纸',gram_weight=150,color='white')
    assert update_supplier_paper_code(row.id,payload,db,user)['color']=='white'
    assert material_face(db,m)=='white'
    payload=SupplierPaperCodePayload(supplier_name='测试供应商',code_char='A',paper_name='新纸名',gram_weight=160)
    assert update_supplier_paper_code(row.id,payload,db,user)['color']=='white'
    assert material_face(db,m)=='white'


@pytest.mark.parametrize('raw',[False,True])
def test_different_white_codes_share_without_extra_flags_and_color_blocks(lot_db,raw):
    db,data,lot,user=prepare(lot_db)
    stock=material(db,'X1X');target=material(db,'Y1Y')
    paper(db,'X','white');face=paper(db,'Y','white')
    product=data['products'][0];product.material_id=target.id
    if raw:lot.semi_finished_detail.sheet_type='raw_board'
    facts=GoodsFacts(verified_material_id=stock.id,processing='raw' if raw else 'cut')
    update_goods(lot.id,GoodsUpdate(facts=facts,expected_version=1,idempotency_key=f'color-use-{raw}'),db,user)
    assert not qualification_issues(db,lot,product)
    expected=SemiFinishedSignature(customer_id=product.customer_id,board_length_mm=800,board_width_mm=600,
        normalized_material_code='Y1Y',flute_type='B',component_type='whole',pieces_per_box=1,stock_yield_per_sheet=1)
    assert ensure_semi_finished_lot_eligibility(db,lot=lot,product_id=product.id,customer_id=product.customer_id,expected=expected)=='customer_generic'
    face.color='kraft';db.commit()
    with pytest.raises(WarehouseInventoryError,match='白面纸'):
        ensure_semi_finished_lot_eligibility(db,lot=lot,product_id=product.id,customer_id=product.customer_id,expected=expected)


def test_selected_current_price_frozen_as_batch_settlement(stocktake_app):
    app,factory,ids,_=stocktake_app
    with factory() as db:
        user=db.scalar(select(User).where(User.username=='p147d-admin'))
        m=material(db,'A1B');m.quote_price=Decimal('2.5');m.price_unit='元/㎡';m.purchase_currency='CNY';m.purchase_tax_included=True;m.purchase_tax_rate=Decimal('.13');db.commit()
        preview=material_price(material_id=m.id,flute_type='B',length_mm=800,width_mm=600,quantity=5,db=db,user=user)
        assert Decimal(preview['unit_price'])==Decimal('1.2')
        assert Decimal(preview['total_price'])==6
        loc=ids['loc_fg1_add']
        payload=SheetEntry(facts=GoodsFacts(verified_material_id=m.id,processing='cut'),location_id=loc,
            expected_layout_version=db.get(Floor3LocationLayout,loc).version,quantity=5,stock_date=date.today(),
            internal_name='半成品',board_length_mm=800,board_width_mm=600,layer_count=3,flute_type='B',idempotency_key='price-freeze')
        result=create_sheet(payload,db,user);lot=db.get(InventoryLot,result['lot_id'])
        assert lot.semi_finished_detail.material_code_snapshot=='A1B'
        assert lot.estimated_unit_cost_snapshot==Decimal('1.2')
        detail=json.loads(lot.cost_snapshot_detail_json)
        assert detail['estimate_basis']=='manual_selected_material_settlement'
        assert Decimal(detail['settlement_square_price'])==Decimal('2.5')
        m.quote_price=Decimal('9');db.commit()
        assert estimate_from_snapshot(lot).unit_cost==Decimal('1.2')
        assert create_sheet(payload,db,user)==result
        assert lot.estimated_unit_cost_snapshot==Decimal('1.2')
