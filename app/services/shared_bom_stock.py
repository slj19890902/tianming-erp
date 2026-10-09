"""Explicit BOM equivalence. Original product/customer/quantity facts never move."""
import json
from sqlalchemy import select
from app.models.product import Product
from app.models.product_bom import ProductBomComponent
from app.models.multilevel_bom import ProductBomProfile, ProductBomInventoryRelation
from app.models.shared_finished_stock import SharedFinishedMember, SharedFinishedReservation, SharedBomMember
from app.services.finished_stock_identity import product_basis


def encode(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def role(db, product):
    if (product is None or not product.is_active or product.deleted_at or product.purged_at
            or product.supply_mode != 'corrugated_production' or product.is_virtual_composite_parent):
        return None
    profile=db.get(ProductBomProfile,product.id)
    if product.is_internal_component and not product.is_composite and (profile is None or profile.source=='manufactured'):
        return 'component' if product.unit in {'只','片'} else None
    if (product.is_composite and not product.is_internal_component and product.unit=='套'
            and profile and profile.source=='assembled' and profile.delivery_mode=='parent'):
        return 'assembled'
    return None


def compiled_bases(db,parent_id):
    from app.models.order import OrderItem
    from app.services.multilevel_bom_compile import compile_master_order_bom
    from app.services.finished_stock_identity import compiled_product_bases
    # The compiler deliberately returns detached snapshots; no synthetic order
    # is persisted and no flush/event hook is invoked.
    item=OrderItem(id=0,product_id=parent_id,quantity=1,combination_role='standalone')
    with db.no_autoflush:
        return compiled_product_bases(compile_master_order_bom(db,item))


def contract(db,product):
    from app.services import shared_finished_stock as shared
    kind=role(db,product)
    if kind is None:
        raise shared._error('仅支持明确的组装成套BOM及其自制单片零件；本体、随货配套和外购请单独核对')
    variants={product_basis(product)}
    children=[]
    if kind=='assembled':
        edges=list(db.scalars(select(ProductBomComponent).where(ProductBomComponent.parent_product_id==product.id)))
        if not edges or len(edges)>20:
            raise shared._error('BOM缺少有效子件或超出本次共用核对范围')
        for edge in edges:
            relation=db.get(ProductBomInventoryRelation,edge.id)
            child=db.get(Product,edge.component_product_id)
            member=db.get(SharedFinishedMember,edge.component_product_id)
            confirmed=db.get(SharedBomMember,edge.component_product_id)
            if (not relation or relation.relation!='assembly' or role(db,child)!='component'
                    or child.customer_id!=product.customer_id or not member or not confirmed
                    or confirmed.role!='component' or member.identity_json!=shared.member_identity(child)):
                raise shared._error('请先把每种对应零件分别确认为共用，再确认同配比整套；不能混用父件与子件')
            children.append(dict(product_id=child.id,group_id=member.group_id,quantity=int(edge.quantity_per_set),
                member_identity=member.identity_json))
        if len({c['group_id'] for c in children})!=len(children):
            raise shared._error('同一配方不能把不同零件归入同一共用组')
        variants.add(compiled_bases(db,product.id)[product.id])
    else:
        parents=list(db.scalars(select(ProductBomComponent.parent_product_id).where(
            ProductBomComponent.component_product_id==product.id).distinct()))
        if not parents:
            raise shared._error('零件缺少真实BOM父件关系')
        for pid in parents:
            parent=db.get(Product,pid)
            if role(db,parent)=='assembled':
                variants.add(compiled_bases(db,pid)[product.id])
    return dict(role=kind,bases=sorted(variants),children=sorted(children,key=lambda r:r['product_id']),
        mold_tool_id=product.mold_tool_id,die_cut_path=product.die_cut_path)


def comparison(identity):
    """Only approved count-name aliases and child equivalence replace identity."""
    value=json.loads(identity)
    info=value.pop('bom')
    basis=value['basis']
    basis['unit']='片' if info['role']=='component' else '套'
    basis['assembly']=sorted([dict(group_id=c['group_id'],quantity=c['quantity']) for c in info['children']],
        key=lambda c:c['group_id'])
    value['bom']=dict(role=info['role'],mold_tool_id=info['mold_tool_id'],die_cut_path=info['die_cut_path'])
    from app.services.shared_finished_stock import _process_key
    basis['production_process']=_process_key(basis.get('production_process'))
    return encode(value)


def persist_contract(db,product_id,identity):
    info=json.loads(identity).get('bom')
    if info:
        db.add(SharedBomMember(product_id=product_id,role=info['role'],contract_json=encode(info)))


def basis_matches(db,product_id,actual,expected=None,*,info=None):
    """Validate against immutable approved variants, including frozen orders."""
    record=db.get(SharedBomMember,product_id) if info is None else None
    info=info or (json.loads(record.contract_json) if record else None)
    if not info:
        return False
    def normalize(raw):
        value=json.loads(raw or '{}');value.pop('quantity_basis',None)
        if value.get('product_id')!=product_id or value.get('schema')!=1:
            return None
        if info['role']=='component' and value.get('unit') in {'只','片'}:
            value['unit']='片'
        return encode(value)
    try:
        approved={normalize(b) for b in info['bases']}
        left=normalize(actual)
        return left is not None and left in approved and (expected is None or normalize(expected) in approved)
    except (TypeError,ValueError,AttributeError):
        return False


def lot_product(db,lot):
    if lot.finished_detail:
        d=lot.finished_detail
        return d.product_id,d.owner_customer_id,d.physical_basis_json
    from app.services.processed_component_stock import output_identity
    return output_identity(db,lot)


def processed_identity(db,lot):
    identity=lot_product(db,lot)
    if not identity:
        return None
    from app.services.warehouse_goods import goods_profile
    d=lot.semi_finished_detail
    return encode(dict(type=lot.inventory_type,product_id=identity[0],customer_id=identity[1],unit=lot.unit,
        profile=goods_profile(db,lot),component_type=d.component_type,
        stock_yield_per_sheet=d.stock_yield_per_sheet,pieces_per_box=d.pieces_per_box))


def validate_lot(db,lot,product,*,automatic=False):
    from app.services import shared_finished_stock as shared
    from app.services.bom_inventory_contract import is_body_lot
    from app.services.bom_subkits import require_free_subkit_stock
    from app.services.fixed_shelf_staging import staging_owner
    info=contract(db,product)
    identity=lot_product(db,lot)
    if (not identity or identity[:2]!=(product.id,product.customer_id) or is_body_lot(lot)
            or staging_owner(db,lot.id) or (lot.finished_detail and lot.finished_detail.is_general)
            or not basis_matches(db,product.id,identity[2],info=info)):
        raise shared._error('BOM批次缺少已确认的完成状态、客户、零件或成套冻结配方，不能共用')
    require_free_subkit_stock(db,lot)
    if ((info['role']=='assembled' and (not lot.finished_detail or not json.loads(identity[2]).get('assembly')))
            or (info['role']=='component' and json.loads(identity[2]).get('assembly'))
            or (lot.finished_detail and lot.unit!='boxes')):
        raise shared._error('整套与单片角色或计量关系不一致，禁止共用')
    return dict(lot_id=lot.id,version=lot.version,available=lot.quantity_available,identity_json=shared.lot_identity(lot))


def reservation_matches(db,reservation,lot,product_id,customer_id,expected_basis):
    from app.services import shared_finished_stock as shared
    fact=db.get(SharedFinishedReservation,reservation.id)
    return bool(fact and fact.product_id==product_id and fact.customer_id==customer_id and db.get(SharedBomMember,product_id)
        and shared.reserved_match(db,reservation,lot,product_id=product_id,customer_id=customer_id)
        and basis_matches(db,product_id,fact.product_basis_json,expected_basis))


def execution_identity(db,lot,compiled,order_item_id,*,allow_free=False):
    """Project target identity only within the exact frozen consuming order."""
    from app.services import shared_finished_stock as shared
    from app.models.warehouse_inventory import InventoryReservation
    from app.services.finished_stock_identity import compiled_product_bases
    raw=lot_product(db,lot)
    if not raw:
        return None
    bases=compiled_product_bases(compiled)
    if raw[0] in bases and raw[1]==compiled.graph.customer_id:
        return raw[:2]
    rows=list(db.scalars(select(InventoryReservation).where(InventoryReservation.order_item_id==order_item_id,
        InventoryReservation.inventory_lot_id==lot.id,InventoryReservation.status.in_(('active','partial')))))
    targets=set()
    for pid,expected in bases.items():
        if rows and all(reservation_matches(db,r,lot,pid,compiled.graph.customer_id,expected) for r in rows):
            targets.add(pid)
        if allow_free and shared.match(db,lot,product_id=pid,customer_id=compiled.graph.customer_id,expected_basis=expected):
            targets.add(pid)
    if len(targets)==1:
        return next(iter(targets)),compiled.graph.customer_id
    return raw[:2]


def consumed_evidence(db,lot,product_id,customer_id,expected_basis):
    from app.services.shared_finished_stock import lot_identity
    member=db.get(SharedFinishedMember,product_id)
    if not member or not db.get(SharedBomMember,product_id):
        return None
    return dict(group_id=member.group_id,product_id=product_id,customer_id=customer_id,
        target_basis=expected_basis,source_identity=lot_identity(lot))


def verify_consumed(db,lot,evidence,product_id,customer_id):
    from app.services.shared_finished_stock import lot_identity
    from app.models.shared_finished_stock import SharedFinishedLot
    member=db.get(SharedFinishedMember,product_id)
    source=db.get(SharedFinishedLot,lot.id) if lot else None
    return bool(evidence and member and source and source.group_id==member.group_id and evidence.get('product_id')==product_id
        and evidence.get('customer_id')==customer_id and member.customer_id==customer_id
        and member.group_id==evidence.get('group_id') and evidence.get('source_identity')==lot_identity(lot)
        and basis_matches(db,product_id,evidence.get('target_basis')))


def enroll_completed_bom(db,lot,*,operator_id):
    """Called by actual completion transactions, never a historical sweep."""
    identity=lot_product(db,lot)
    if not identity or not db.get(SharedBomMember,identity[0]):
        return False
    product=db.get(Product,identity[0])
    printed=('印刷' in (product.production_process or '') or any(getattr(product,key,None)
        for key in ('print_content','printing_colors','printing_plate_1_id','printing_plate_2_id','printing_plate_3_id')))
    if printed:
        # Old stock-preparation snapshots do not prove artwork/colour. Only an
        # actual order completion with the exact frozen printing task qualifies.
        if lot.source_ref_type!='production_completion':return False
        from app.models.production import ProductionCompletion,ProductionTask
        from app.services.production_workflow import _new_task_printing_snapshot,ProductionWorkflowError
        completion=db.get(ProductionCompletion,lot.source_ref_id)
        task=db.get(ProductionTask,completion.task_id) if completion else None
        if task is None:return False
        try:expected=_new_task_printing_snapshot(db,product)
        except ProductionWorkflowError:return False
        from app.services.shared_finished_stock import _value
        if any(_value(getattr(task,key))!=_value(value) for key,value in expected.items()):return False
    from app.services.shared_finished_management import enroll_new_lot
    return enroll_new_lot(db,lot,operator_id=operator_id,production_verified=True)
