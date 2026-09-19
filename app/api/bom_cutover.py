"""Explicit, narrow administrator handoff of fully reserved legacy parts."""
import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api.deps import PermissionChecker, get_db, require_customer_access
from app.models.order import Order, OrderItem
from app.models.user import User
from app.models.multilevel_bom import OrderBomExecutionCutover, OrderBomGraph
from app.services.bom_transactions import atomic_bom
from app.services.multilevel_bom_cutover import convert_reserved_legacy_order, prepare_reserved_cutover
from app.services.multilevel_bom_cutover_review import review_legacy_cutover, _row
from app.services.multilevel_bom_plan import BomPlanError
from app.services.warehouse_inventory import _location, WarehouseInventoryError
from app.services.bom_subkits import SubkitError
from app.services.composite_bom import CompositeBOMError
from app.services.warehouse_twin_layout import resolve_warehouse_twin_layout_path

router = APIRouter()
can_edit = PermissionChecker("orders.edit")
can_confirm_production = PermissionChecker("orders.status")
can_execute_inventory = PermissionChecker("warehouse.execute")
SCOPE = "全预占库存切换：支持子件转组装父件，或保留原子件分存；含采购、本体或未完成采购交接须另行核对"


class CutoverPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_locations: dict[int, int] = Field(default_factory=dict, max_length=99)


class SemiProductionPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: int = Field(gt=0, strict=True)


class SemiProductionExecute(SemiProductionPreview):
    reviewed_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation_key: str = Field(min_length=1, max_length=64)


@router.get("/items/{item_id}/semi-production")
def list_semi_production(item_id: int, db: Session = Depends(get_db), user: User = Depends(can_confirm_production)):
    _access(db, user, item_id)
    from sqlalchemy import select
    from app.services.multilevel_bom_orders import read_compiled_order_bom
    from app.models.production import ProductionCompletion
    from app.models.warehouse_inventory import InventoryLot
    compiled = read_compiled_order_bom(db, item_id)
    if compiled is None:
        raise HTTPException(409, "订单缺少冻结BOM")
    completions = []
    for completion, lot in db.execute(select(ProductionCompletion, InventoryLot).join(
            InventoryLot, InventoryLot.id == ProductionCompletion.inventory_lot_id).where(
                ProductionCompletion.order_item_id == item_id, ProductionCompletion.origin == "manual")):
        detail = json.loads(lot.cost_snapshot_detail_json or "{}")
        if detail.get("bom_semi_confirmation"):
            completions.append(dict(id=completion.id, product_id=detail["bom_material_product_id"],
                quantity=completion.quantity, status=completion.status))
    return dict(products=[dict(id=node.product_id, name=node.name, unit=node.unit)
        for node in compiled.graph.nodes if node.source == "manufactured"], completions=completions)


@router.post("/items/{item_id}/semi-production/preview")
def preview_semi(item_id: int, payload: SemiProductionPreview, db: Session = Depends(get_db), user: User = Depends(can_confirm_production)):
    _access(db, user, item_id)
    from app.services.multilevel_bom_semi_production import preview_semi_production
    from app.services.production_workflow import ProductionWorkflowError
    try:
        return preview_semi_production(db, order_item_id=item_id, product_id=payload.product_id)[2]
    except (BomPlanError, SubkitError, WarehouseInventoryError, ProductionWorkflowError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/items/{item_id}/semi-production/execute", dependencies=[Depends(can_execute_inventory)])
def execute_semi(item_id: int, payload: SemiProductionExecute, db: Session = Depends(get_db), user: User = Depends(can_confirm_production)):
    _access(db, user, item_id)
    from app.services.multilevel_bom_semi_production import confirm_semi_production
    from app.services.production_workflow import ProductionWorkflowError
    try:
        result = confirm_semi_production(db, order_item_id=item_id, actor=user, **payload.model_dump())
        db.commit()
        return result
    except (BomPlanError, SubkitError, WarehouseInventoryError, ProductionWorkflowError) as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc


@router.post("/items/{item_id}/semi-production/{completion_id}/revert", dependencies=[Depends(can_execute_inventory)])
def revert_semi(item_id: int, completion_id: int, db: Session = Depends(get_db), user: User = Depends(can_confirm_production)):
    _access(db, user, item_id)
    from app.services.multilevel_bom_semi_production import reverse_semi_production
    from app.services.production_workflow import ProductionWorkflowError
    try:
        result = reverse_semi_production(db, order_item_id=item_id, completion_id=completion_id, actor=user)
        db.commit()
        return result
    except (BomPlanError, SubkitError, WarehouseInventoryError, ProductionWorkflowError) as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc


class CutoverExecute(CutoverPreview):
    reviewed_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    preview_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_lot_versions: dict[int, int] = Field(min_length=1, max_length=999)
    operation_key: str = Field(min_length=1, max_length=64)


class StockedExecute(CutoverExecute):
    rule_revision: int = Field(ge=0, strict=True)


class PurchaseExecute(StockedExecute):
    source_lot_versions: dict[int, int] = Field(default_factory=dict, max_length=999)


class UnstartedPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_locations: dict[int, int] = Field(default_factory=dict, max_length=0)


class UnstartedExecute(UnstartedPreview):
    reviewed_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    preview_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_lot_versions: dict[int, int] = Field(default_factory=dict, max_length=0)
    operation_key: str = Field(min_length=1, max_length=64)
    rule_revision: int | None = Field(default=None, ge=0, strict=True)


def _access(db, user, item_id, *, allow_raw_processing=False):
    db.refresh(user)
    if user.role != "admin" or not user.is_active:
        raise HTTPException(403, "仅活动管理员可执行此操作")
    item = db.get(OrderItem, item_id)
    order = db.get(Order, item.order_id) if item else None
    if order is None:
        raise HTTPException(404, "订单明细不存在")
    require_customer_access(order.customer_id, user, db)
    if not allow_raw_processing:
        from app.services.raw_purchase_plans import assert_no_plan
        assert_no_plan(db,[item_id])
    return order.customer_id


def _preview(db, item_id, customer_id, targets):
    review = review_legacy_cutover(db, order_item_id=item_id, customer_id=customer_id)
    manifest = json.loads(review.document)
    active = [r for r in manifest["reservations"] if
        r["reserved_stock_quantity"] > r["consumed_stock_quantity"] + r["released_stock_quantity"]]
    active_ids = {r["inventory_lot_id"] for r in active}
    versions = {r["id"]: r["version"] for r in manifest["lots"] if r["id"] in active_ids}
    assembled = {n.product_id for n in review.compiled.graph.nodes if n.source == "assembled"}
    # Check the same support/quantity rules before offering any execution UI.
    item, graph, _, _, lots, remaining, plan = prepare_reserved_cutover(db,
        review=review, customer_id=customer_id, source_lot_versions=versions,
        target_locations=targets or dict.fromkeys(assembled, None))
    if any(type(k) is not int or k <= 0 or type(v) is not int or v <= 0 for k, v in targets.items()):
        raise BomPlanError("请选择有效正式货位")
    locations = [_row(_location(db, lid, "finished")) for _, lid in sorted(targets.items())]
    map_hash = hashlib.sha256(resolve_warehouse_twin_layout_path().read_bytes()).hexdigest()
    basis = json.dumps({"review": review.checksum, "targets": sorted(targets.items()),
        "locations": locations, "map": map_hash}, sort_keys=True, ensure_ascii=False, default=str)
    nodes = {n.product_id: n for n in graph.nodes}
    separate = nodes[graph.root_id].source == "separate"
    return {"scope": SCOPE, "ready": separate or bool(targets), "order_item_id": item_id,
        "inventory_mode": "separate" if separate else "assembled",
        "quantity": item.quantity, "delivered_quantity": item.delivered_quantity or 0,
        "execution_quantity": remaining, "reviewed_hash": review.checksum,
        "preview_hash": hashlib.sha256(basis.encode()).hexdigest(), "source_lot_versions": versions,
        "target_locations": targets,
        "outputs": [{"product_id": s.product_id, "name": nodes[s.product_id].name,
            "unit": nodes[s.product_id].unit, "quantity": s.produced_units,
            "consumed": [{"name": nodes[pid].name, "quantity": qty, "unit": nodes[pid].unit}
                for pid, qty in s.consumed]} for s in plan.steps],
        "release_reservations": [{"id": r["id"], "lot_id": r["inventory_lot_id"],
            "name": nodes[lots[r["inventory_lot_id"]].finished_detail.product_id].name,
            "unit": nodes[lots[r["inventory_lot_id"]].finished_detail.product_id].unit,
            "quantity": r["reserved_stock_quantity"] - r["consumed_stock_quantity"] - r["released_stock_quantity"]}
            for r in active],
        "material_impact": "全部剩余子件由现有预占覆盖，不新增报料；保留原报料、完工及已送历史",
        "procurement_impact": "本操作不新增或修改采购；原采购、收料及成本补录事实保留",
        "cost_impact": "沿用下列批次来源成本；组装只转移成本，不重复计价。估算来源不会因转换变为实际成本",
        "source_costs": [{**row,
            "unit": nodes[lots[row["lot_id"]].finished_detail.product_id].unit}
            for row in manifest["remaining_finished_costs"]],
        "picking_impact": ("继续按冻结子件和原批次货位拿货" if separate else
            "切换后按父件剩余套数及所选组装货位拿货，原子件不再重复出库"),
        "inventory_impact": ("剩余预占切换到新冻结子件规则；原批次、货位及库存数量不变，不生成父库存，旧消耗不改" if separate else
            "释放下列剩余预占并消耗子件，形成组装库存；父件按剩余套数重新预占，旧消耗不改"),
        "retained_locations": [{"lot_id": lot.id, "location_id": lot.warehouse_location_id,
            "product_id": lot.finished_detail.product_id} for lot in lots.values()] if separate else [],
        "product_versions": {n.product_id: n.version for n in graph.nodes}}


@router.post("/items/{item_id}/reserved-kit-cutover/preview")
def preview(item_id: int, payload: CutoverPreview, db: Session = Depends(get_db), user: User = Depends(can_edit)):
    customer_id = _access(db, user, item_id)
    try:
        return _preview(db, item_id, customer_id, payload.target_locations)
    except (BomPlanError, WarehouseInventoryError, SubkitError, CompositeBOMError, OSError) as exc:
        db.rollback()
        raise HTTPException(409, f"{SCOPE}。{exc}") from exc


@router.post("/items/{item_id}/reserved-kit-cutover/execute")
def execute(item_id: int, payload: CutoverExecute, db: Session = Depends(get_db), user: User = Depends(can_edit)):
    customer_id = _access(db, user, item_id)
    try:
        with atomic_bom(db):
            # SQLite's write lock covers both the refreshed preview and writer.
            # Replays go through the writer's original full-payload/actor check.
            if db.get(OrderBomExecutionCutover, item_id) is None:
                current = _preview(db, item_id, customer_id, payload.target_locations)
                if (not current["ready"] or current["preview_hash"] != payload.preview_hash
                        or current["reviewed_hash"] != payload.reviewed_hash
                        or current["source_lot_versions"] != payload.source_lot_versions):
                    raise BomPlanError("预览或货位/地图版本已变化，请重新预览")
            result = convert_reserved_legacy_order(db, order_item_id=item_id, customer_id=customer_id,
                reviewed_hash=payload.reviewed_hash, source_lot_versions=payload.source_lot_versions,
                target_locations=payload.target_locations, operation_key=payload.operation_key, actor=user)
        db.commit()
        return result
    except (BomPlanError, WarehouseInventoryError, SubkitError, CompositeBOMError, OSError) as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except Exception:
        db.rollback()
        raise


@router.post("/items/{item_id}/stocked-bom-cutover/preview")
def preview_stocked(item_id: int, payload: CutoverPreview, db: Session = Depends(get_db), user: User = Depends(can_edit)):
    customer_id = _access(db, user, item_id)
    from app.services.multilevel_bom_stocked_handoff import review_stocked_handoff
    try:
        return review_stocked_handoff(db, order_item_id=item_id,
            customer_id=customer_id, target_locations=payload.target_locations).preview
    except (BomPlanError, SubkitError, CompositeBOMError, WarehouseInventoryError, OSError) as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc


@router.get("/items/{item_id}/bom-procurement-impact")
def procurement_impact(item_id: int, db: Session = Depends(get_db), user: User = Depends(can_edit)):
    customer_id = _access(db, user, item_id)
    from app.services.multilevel_bom_procurement_impact import review_procurement_impact
    from app.services.incoming_receipts import IncomingReceiptError
    try:
        return review_procurement_impact(db, order_item_id=item_id, customer_id=customer_id)
    except (BomPlanError, SubkitError, CompositeBOMError, IncomingReceiptError) as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc


@router.post("/items/{item_id}/stocked-bom-cutover/execute")
def execute_stocked(item_id: int, payload: StockedExecute, db: Session = Depends(get_db), user: User = Depends(can_edit)):
    return _execute_stocked(item_id, payload, db, user, carry_purchases=False)


@router.post("/items/{item_id}/purchase-bom-cutover/preview")
def preview_purchase(item_id: int, payload: CutoverPreview, db: Session = Depends(get_db), user: User = Depends(can_edit)):
    customer_id = _access(db, user, item_id)
    from app.services.multilevel_bom_stocked_handoff import review_stocked_handoff
    try:
        return review_stocked_handoff(db, order_item_id=item_id, customer_id=customer_id,
            target_locations=payload.target_locations, carry_purchases=True).preview
    except (BomPlanError, SubkitError, CompositeBOMError, WarehouseInventoryError, OSError) as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc


@router.post("/items/{item_id}/purchase-bom-cutover/execute")
def execute_purchase(item_id: int, payload: PurchaseExecute, db: Session = Depends(get_db), user: User = Depends(can_edit)):
    return _execute_stocked(item_id, payload, db, user, carry_purchases=True)


@router.post("/items/{item_id}/material-bom-cutover/preview")
def preview_material(item_id: int, payload: CutoverPreview, db: Session = Depends(get_db), user: User = Depends(can_edit)):
    customer_id = _access(db, user, item_id)
    from app.services.multilevel_bom_stocked_handoff import review_stocked_handoff
    try:
        return review_stocked_handoff(db, order_item_id=item_id, customer_id=customer_id,
            target_locations=payload.target_locations, carry_materials=True).preview
    except (BomPlanError, SubkitError, CompositeBOMError, WarehouseInventoryError, OSError) as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc


@router.post("/items/{item_id}/material-bom-cutover/execute")
def execute_material(item_id: int, payload: PurchaseExecute, db: Session = Depends(get_db), user: User = Depends(can_edit)):
    return _execute_stocked(item_id, payload, db, user, carry_purchases=True, carry_materials=True)


def _execute_stocked(item_id, payload, db, user, *, carry_purchases, carry_materials=False):
    customer_id = _access(db, user, item_id)
    from app.services.multilevel_bom_stocked_handoff import execute_stocked_handoff
    try:
        if payload.preview_hash != payload.reviewed_hash:
            raise BomPlanError("预览摘要不一致，请重新预览")
        result = execute_stocked_handoff(db, order_item_id=item_id, customer_id=customer_id,
            reviewed_hash=payload.reviewed_hash, expected_revision=payload.rule_revision,
            target_locations=payload.target_locations, source_lot_versions=payload.source_lot_versions,
            operation_key=payload.operation_key, actor=user, carry_purchases=carry_purchases, carry_materials=carry_materials)
        db.commit()
        return result
    except (BomPlanError, SubkitError, CompositeBOMError, WarehouseInventoryError, OSError) as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except Exception:
        db.rollback()
        raise


@router.post("/items/{item_id}/unstarted-bom-cutover/preview")
def preview_unstarted(item_id: int, payload: UnstartedPreview, db: Session = Depends(get_db), user: User = Depends(can_edit)):
    customer_id = _access(db, user, item_id)
    from app.services.multilevel_bom_unstarted_cutover import unstarted_preview
    try:
        if db.get(OrderBomGraph, item_id) is not None:
            from app.services.multilevel_bom_rule_cutover import rule_cutover_preview
            return rule_cutover_preview(db, order_item_id=item_id, customer_id=customer_id)
        return unstarted_preview(db, order_item_id=item_id, customer_id=customer_id)
    except (BomPlanError, SubkitError, CompositeBOMError) as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc


@router.post("/items/{item_id}/unstarted-bom-cutover/execute")
def execute_unstarted(item_id: int, payload: UnstartedExecute, db: Session = Depends(get_db), user: User = Depends(can_edit)):
    customer_id = _access(db, user, item_id)
    from app.services.multilevel_bom_unstarted_cutover import execute_unstarted_cutover
    try:
        if payload.preview_hash != payload.reviewed_hash:
            raise BomPlanError("预览摘要不一致，请重新预览")
        if payload.rule_revision is None:
            result = execute_unstarted_cutover(db, order_item_id=item_id, customer_id=customer_id,
                reviewed_hash=payload.reviewed_hash, operation_key=payload.operation_key, actor=user)
        else:
            from app.services.multilevel_bom_rule_cutover import execute_rule_cutover
            result = execute_rule_cutover(db, order_item_id=item_id, customer_id=customer_id,
                reviewed_hash=payload.reviewed_hash, expected_revision=payload.rule_revision,
                operation_key=payload.operation_key, actor=user)
        db.commit()
        return result
    except (BomPlanError, SubkitError, CompositeBOMError) as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except Exception:
        db.rollback()
        raise

@router.get('/items/{item_id}/component-processing')
def list_component_processing(item_id:int,db:Session=Depends(get_db),user:User=Depends(can_confirm_production)):
    _access(db,user,item_id,allow_raw_processing=True)
    from app.models.product import Product
    from app.models.product_bom import SalesOrderItemBomComponent
    from app.services.unfinished_components import unfinished_reservations
    from app.services.component_processing import processing_completions
    item=db.get(OrderItem,item_id)
    products=set()
    for _,requirement,_ in unfinished_reservations(db,item_id):
        snapshot=db.get(SalesOrderItemBomComponent,requirement.sales_order_item_bom_component_id) if requirement.sales_order_item_bom_component_id else None
        products.add(snapshot.component_product_id if snapshot else item.product_id)
    completions=[dict(id=c.id,product_id=d['processing_product_id'],quantity=c.quantity,status=c.status)
        for c,d in processing_completions(db,item_id,posted=False)]
    return dict(products=[dict(id=pid,name=db.get(Product,pid).product_name,unit='只') for pid in sorted(products)],completions=completions)


@router.post('/items/{item_id}/component-processing/preview')
def preview_component_processing(item_id:int,payload:SemiProductionPreview,db:Session=Depends(get_db),user:User=Depends(can_confirm_production)):
    _access(db,user,item_id,allow_raw_processing=True)
    from app.services.component_processing import preview_processing
    from app.services.production_workflow import ProductionWorkflowError
    try:
        return preview_processing(db,order_item_id=item_id,product_id=payload.product_id)
    except (SubkitError,WarehouseInventoryError,ProductionWorkflowError) as error:
        raise HTTPException(409,str(error)) from error


@router.post('/items/{item_id}/component-processing/execute',dependencies=[Depends(can_execute_inventory)])
def confirm_component_processing(item_id:int,payload:SemiProductionExecute,db:Session=Depends(get_db),user:User=Depends(can_confirm_production)):
    _access(db,user,item_id,allow_raw_processing=True)
    from app.services.component_processing import confirm_processing
    from app.services.production_workflow import ProductionWorkflowError
    try:
        result=confirm_processing(db,order_item_id=item_id,actor=user,**payload.model_dump())
        db.commit()
        return result
    except (SubkitError,WarehouseInventoryError,ProductionWorkflowError) as error:
        db.rollback()
        raise HTTPException(409,str(error)) from error


@router.post('/items/{item_id}/component-processing/{completion_id}/revert',dependencies=[Depends(can_execute_inventory)])
def revert_component_processing(item_id:int,completion_id:int,db:Session=Depends(get_db),user:User=Depends(can_confirm_production)):
    _access(db,user,item_id,allow_raw_processing=True)
    from app.services.component_processing import reverse_processing
    from app.services.production_workflow import ProductionWorkflowError
    try:
        result=reverse_processing(db,order_item_id=item_id,completion_id=completion_id,actor=user)
        db.commit()
        return result
    except (SubkitError,WarehouseInventoryError,ProductionWorkflowError) as error:
        db.rollback()
        raise HTTPException(409,str(error)) from error
