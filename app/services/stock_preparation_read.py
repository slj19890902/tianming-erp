"""Request-local stock preparation projection; ledger rows load only for visible groups."""
from collections import defaultdict
from contextlib import contextmanager
from sqlalchemy import select, or_, tuple_
from sqlalchemy.orm import joinedload, selectinload

from app.models.stock_replenishment import StockReplenishmentOrderItem as Item, StockReplenishmentOrder as Order
from app.models.incoming_receipt import IncomingReceiptItem as Receipt
from app.models.stock_preparation import StockPreparationJob as Job, StockPreparationCommand as Command
from app.models.warehouse_inventory import InventoryLot as Lot, WarehouseLocation, InventoryMovement
from app.models.purchase_receipt import IncomingReceiptPurposeAllocation as Purpose


class Projection:
    def __init__(self, db, scope):
        self.db = db
        self.keep = []  # SQLAlchemy identity map otherwise releases unused rows.
        self.jobs = defaultdict(list);self.commands = defaultdict(list);self.receipts = defaultdict(list)
        self.outputs = defaultdict(list);self.families = defaultdict(list);self.job_results = {}
        self.row_lots = {};self.sources = {}
        stmt = select(Item).join(Order).where(Order.status != 'draft')
        if scope is not None:
            stmt = stmt.where(Item.customer_id.in_(scope))
        self.items = self.load(stmt.options(joinedload(Item.order), joinedload(Item.customer)).order_by(Item.id.desc()))
        from app.models.raw_purchase_plan import RawPurchasePlan
        self.raw_plan_items = set(db.scalars(select(RawPurchasePlan.stock_item_id).where(RawPurchasePlan.stock_item_id.in_([i.id for i in self.items]))))
        from app.models.product import Product
        self.load(select(Product).where(Product.id.in_({i.reference_product_id or i.product_id for i in self.items}-{None})).options(joinedload(Product.material),joinedload(Product.customer)))
        purpose_stmt = select(Purpose).where(Purpose.status=='posted',Purpose.purpose_contract_status_snapshot=='frozen',
            Purpose.surplus_disposition=='semi_finished_reserve',Purpose.receipt_reserve_purpose_sheet_qty>0,
            Purpose.source_kind=='order_item',Purpose.component_type=='whole')
        if scope is not None:purpose_stmt=purpose_stmt.where(Purpose.customer_id.in_(scope))
        self.purposes = self.load(purpose_stmt.order_by(Purpose.id.desc()))
        self.purposes_by_receipt = {p.incoming_receipt_item_id:p for p in self.purposes}
        if self.purposes:
            from app.models.supplier_requisition_order import PurchasePurposeSourceSnapshot, SupplierRequisitionOrderItem
            from app.models.order import OrderItem
            from app.models.customer import Customer
            self.load(select(PurchasePurposeSourceSnapshot).where(PurchasePurposeSourceSnapshot.id.in_({p.purchase_purpose_source_snapshot_id for p in self.purposes})))
            self.load(select(OrderItem).where(OrderItem.id.in_({p.source_order_item_id for p in self.purposes})).options(joinedload(OrderItem.order)))
            self.load(select(SupplierRequisitionOrderItem).where(SupplierRequisitionOrderItem.id.in_({p.supplier_requisition_order_item_id for p in self.purposes})).options(joinedload(SupplierRequisitionOrderItem.supplier_order)))
            self.load(select(Customer).where(Customer.id.in_({p.customer_id for p in self.purposes})))
        receipts = self.load(select(Receipt).where(or_(Receipt.stock_replenishment_item_id.in_([i.id for i in self.items]),
            Receipt.id.in_([p.incoming_receipt_item_id for p in self.purposes]))).order_by(Receipt.id))
        for r in receipts:self.receipts[r.stock_replenishment_item_id].append(r)
        ids=[r.id for r in receipts]
        jobs=self.load(select(Job).where(Job.receipt_item_id.in_(ids)).order_by(Job.id))
        for j in jobs:self.jobs[j.receipt_item_id].append(j)
        for c in self.load(select(Command).where(Command.receipt_item_id.in_(ids)).order_by(Command.created_at,Command.operation_key)):
            self.commands[c.receipt_item_id].append(c)
        roots={i.inventory_lot_id for i in self.items}|{r.received_inventory_lot_id for r in receipts}|{p.semi_finished_inventory_lot_id for p in self.purposes}|{j.output_lot_id for j in jobs}
        options=(joinedload(Lot.finished_detail),joinedload(Lot.semi_finished_detail),selectinload(Lot.allowed_products))
        root_lots=self.load(select(Lot).where(Lot.id.in_(roots-{None})).options(*options))
        families={(l.source_ref_type,l.source_ref_id,l.inventory_type) for l in root_lots if l.source_ref_type and l.source_ref_id}
        related=self.load(select(Lot).where(or_(tuple_(Lot.source_ref_type,Lot.source_ref_id,Lot.inventory_type).in_(families),
            (Lot.source_ref_type=='stock_preparation') & Lot.source_ref_id.in_([j.id for j in jobs]))).options(*options))
        for lot in related:
            self.families[(lot.source_ref_type,lot.source_ref_id,lot.inventory_type)].append(lot)
            if lot.source_ref_type=='stock_preparation':self.outputs[lot.source_ref_id].append(lot)
        self.load(select(WarehouseLocation).where(WarehouseLocation.id.in_({l.warehouse_location_id for l in root_lots+related}-{None})))

    def load(self, statement):
        rows=list(self.db.scalars(statement).unique());self.keep.extend(rows);return rows

    def attach_movements(self, entries):
        targets=[]
        def walk(value):
            if isinstance(value,dict):
                if 'movements' in value and (value.get('receipt_item_id') is not None or str(value.get('key','')).startswith('waiting:')):
                    identity='receipt:'+str(value['receipt_item_id']) if value.get('receipt_item_id') else value['key']
                    targets.append((value,self.row_lots.get(identity,[])))
                for child in value.values():
                    if isinstance(child,(dict,list)):walk(child)
            elif isinstance(value,list):
                for child in value:walk(child)
        walk(entries)
        ids={i for _,lots in targets for i in lots}
        if not ids:return
        from app.core.time_contract import utc_naive_to_api
        movements=list(self.db.scalars(select(InventoryMovement).where(InventoryMovement.inventory_lot_id.in_(ids)).order_by(InventoryMovement.id)))
        for value,lots in targets:
            value['movements']=[dict(at=utc_naive_to_api(m.created_at),reason=m.reason,quantity=m.quantity,
                unit='张' if m.unit=='sheets' else '只',order_item_id=m.related_order_item_id,lot_id=m.inventory_lot_id,
                available_before=m.before_available,available_after=m.after_available) for m in movements if m.inventory_lot_id in lots]


@contextmanager
def projection(db, scope):
    # A scope is bound to this request only; nothing is cached across users or writes.
    previous=db.info.get('stock_preparation_projection')
    if previous is not None:
        yield previous
        return
    cache=Projection(db,scope)
    db.info['stock_preparation_projection']=cache
    from app.services.inventory_read_scope import inventory_summary_read_scope
    try:
        with inventory_summary_read_scope(db):yield cache
    finally:db.info.pop('stock_preparation_projection',None)


def has_raw_plan(db, item_id):
    cache=db.info.get('stock_preparation_projection')
    if cache is not None:return item_id in cache.raw_plan_items
    from app.models.raw_purchase_plan import RawPurchasePlan
    return bool(db.scalar(select(RawPurchasePlan.id).where(RawPurchasePlan.stock_item_id==item_id)))
