"""Evidence-based first stage of the inventory assistant. No business writes."""
from collections import defaultdict
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from app.api.deps import get_db, PermissionChecker
from app.api.warehouse import _visible_customer_ids, _require_lot_customer_access
from app.models.user import User
from app.models.warehouse_inventory import InventoryLot
from app.services.inventory_insights import build_inventory_insights
from app.services.inventory_valuation import cost_payload, can_view_inventory_cost
from app.services.mobile_order_dimensions import outstanding_query, line_payload

router=APIRouter()

def allowed(user:User=Depends(PermissionChecker('warehouse.view'))):
    if user.role not in ('admin','boss') or not can_view_inventory_cost(user):raise HTTPException(403,'当前库存助手仅对老板和管理员开放')
    return user

@router.get('')
def workbench(response:Response, focus:str=Query('all',pattern='^(all|demand|materials|aged|missing)$'), q:str=Query('',max_length=100), page:int=Query(1,ge=1), db:Session=Depends(get_db),user:User=Depends(allowed)):
    response.headers['Cache-Control']='private, no-store'
    scope=_visible_customer_ids(user,db)
    insights=build_inventory_insights(db,customer_ids=scope,frozen_cost_only=True,action_limit=None,include_all_available=True)
    rows=insights['action_items'];ids=[r['lot_id'] for r in rows]
    lots={lot.id:lot for lot in db.scalars(select(InventoryLot).options(selectinload(InventoryLot.finished_detail),selectinload(InventoryLot.semi_finished_detail)).where(InventoryLot.id.in_(ids)))}
    demand=defaultdict(int);demand_units={}
    for item,order,customer,product in db.execute(outstanding_query(scope)):
        payload=line_payload(item,order,customer,product);demand[product.id]+=payload['remaining_quantity'];demand_units[product.id]=payload['unit']
    items=[]
    for row in rows:
        lot=lots[row['lot_id']];physical=lot.finished_detail
        row['version']=lot.version
        row['unit_label']={'boxes':'只','sheets':'张'}.get(lot.unit,lot.unit)
        row['open_unit']=demand_units.get(physical.product_id) if physical else None
        row['open_quantity']=demand.get(physical.product_id,0) if physical else None
        row['product_id']=physical.product_id if physical else None
        row['is_material']=lot.semi_finished_detail is not None
        row['cost']=cost_payload(lot,db)
        # Order matching and coverage are rechecked on demand; do not add product-level coverage across lots.
        row.pop('covered_demand_quantity',None);row.pop('uncovered_demand_quantity',None);row.pop('coverage_percent',None)
        row['reasons']=[reason for reason in row['reasons'] if reason['code'] not in ('finished_stock_can_cover_order','finished_stock_exceeds_open_demand','semi_stock_may_cover_demand','semi_product_assignment_missing')]
        if physical and row['open_quantity']>0:row['reasons'].insert(0,{'code':'open_order','text':f"本款未送需求 {row['open_quantity']}{row['open_unit'] or row['unit_label']}，可查看订单核对现货用途"})
        if row['is_material']:row['reasons'].insert(0,{'code':'material_lookup','text':'按实际尺寸、楞型、面纸及适用范围反查产品，近似项仅供选料'})
        if focus=='demand' and not (row['open_quantity'] or 0)>0:continue
        if focus=='materials' and not row['is_material']:continue
        if focus=='aged' and (row['age_days'] is None or row['age_days']<180):continue
        if focus=='missing' and row['cost']['unit_cost'] is not None:continue
        text=' '.join(str(v or '') for v in [row['lot_number'],row.get('location_name'),*row['detail'].values()]).casefold()
        if q.strip().casefold() not in text:continue
        items.append(row)
    from app.api.warehouse import get_inventory_costs
    costs=get_inventory_costs(response,location_id=None,db=db,user=user)
    summary={key:costs[key] for key in ("inventory_value","total_lots","missing_lots","basis")}
    return {'summary':summary,'mode':'local_rules','generated_at':insights['generated_at'],'as_of':insights['as_of'],'total':len(items),'page':page,'page_size':25,'items':items[(page-1)*25:page*25], 'notice':'本地规则建议。未送需求不等于还需采购数量；同款需求不按批次重复相加。金额采用批次冻结成本，缺价单列。'}

@router.get('/{lot_id}/orders')
def orders(lot_id:int,response:Response,page:int=Query(1,ge=1),db:Session=Depends(get_db),user:User=Depends(allowed)):
    response.headers['Cache-Control']='private, no-store'
    lot=_require_lot_customer_access(db,lot_id,user)
    if not lot.finished_detail:raise HTTPException(422,'片料请先反查适用产品')
    from app.models.order import OrderItem
    query=outstanding_query(_visible_customer_ids(user,db)).where(OrderItem.product_id==lot.finished_detail.product_id)
    result=list(db.execute(query.offset((page-1)*25).limit(26)))
    return {'lot_id':lot.id,'version':lot.version,'page':page,'has_more':len(result)>25,'items':[line_payload(*row) for row in result[:25]],'read_only':True}


@router.get('/{lot_id}/materials')
def materials(lot_id:int,response:Response,page:int=Query(1,ge=1),db:Session=Depends(get_db),user:User=Depends(allowed)):
    from app.services.material_candidates import candidate_items
    response.headers['Cache-Control']='private, no-store'
    lot=_require_lot_customer_access(db,lot_id,user)
    candidates=candidate_items(db,lot,_visible_customer_ids(user,db))
    rows=[row for row in candidates if row['color_compatible']]
    return {'lot_id':lot.id,'version':lot.version,'page':page,'has_more':page*25<len(rows),'items':rows[(page-1)*25:page*25],'excluded_color_count':len(candidates)-len(rows),'read_only':True}
