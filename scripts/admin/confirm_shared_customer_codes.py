"""Owner-reviewed 2026-10-09 same-code maintenance, no CLI or web bypass.

The stopped release job owns its lock, verified backup, exact reviewed plan,
whole-database fact guard and commit. Preview simulations always roll back.
"""
import hashlib
import json
import re
from collections import defaultdict
from sqlalchemy import select, update
from app.models.product import Product
from app.models.mold_tool import MoldTool, MoldToolCustomer
from app.models.warehouse_inventory import InventoryLot, FinishedGoodsInventoryDetail
from app.models.shared_finished_stock import SharedFinishedGroup, SharedFinishedMember, SharedFinishedLot, SharedFinishedMutation
from app.services import shared_finished_stock as shared
from app.services import shared_finished_management as management
from app.services.bom_transactions import atomic_bom
from app.services.audit_log import append_audit_event
from app.services.warehouse_inventory import WarehouseInventoryError

KEY = "shared-customer-codes-20261009"
CUSTOMERS = (137, 138)
PRINTED_CODES = {"80010612", "80010628", "80012133", "80012149", "80012289", "80012396"}
PROCESS = {"80010557": "模切,无需结合", "80010611": "模切,无需结合",
           "80011980": "模切,粘贴", "80012395": "模切,粘贴", "80012287": "模切,粘贴"}
CONFIRMED_MOLDS = {"80012130": {201, 355}, "82021154": {210, 369}}
USE_YANGUANG = {"80010626", "80010627", "80010637", "80011965", "80012157"}
LOT_FACTS = {**{lid:('80012081',{'spec':[None,'452×430×10mm']}) for lid in (1044,1045,1046)},
    1117:('80012287',{'production_process':['无需结合,模切','模切,粘贴']}),
    1178:('80012395',{'production_process':['模切','模切,粘贴']})}
EVIDENCE = ("老板2026-10-09授权研光/光洋当前同存货编码成品共用、共用一个模具；"
    "逐项确认80012289红色，80011947 VIK/B楞，80012131名称DO O5 纸垫及PSP；"
    "80010557/80010611无需结合，80011980/80012395/80012287粘贴；"
    "六款旧库存已按正确图案颜色印刷完成并可共用；80012130/82021154每个编码实际只有一块模具。"
    "80010626/80010627/80010637/80011965/80012157加工资料以研光为准；"
    "80012081三批现货452×430×10mm，80012287的3090只及80012395的6只已粘贴完成且可共用。"
    "BOM角色及未确认非空冲突不覆盖。")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _changes(db):
    products=db.scalars(select(Product).where(Product.customer_id.in_(CUSTOMERS),
        Product.is_active.is_(True),Product.deleted_at.is_(None),Product.purged_at.is_(None))).all()
    codes=defaultdict(list)
    for product in products:codes[product.product_code].append(product)
    changes=[]; pairs=[]; mold_links=[]; mold_versions={}; preserved=[]
    for code,products in sorted(codes.items()):
        if {p.customer_id for p in products}!=set(CUSTOMERS):continue
        products.sort(key=lambda p:(p.customer_id,p.id))
        pair=dict(code=code,product_ids=[p.id for p in products],issues=[])
        pairs.append(pair)
        if len(products)!=2 or any(not shared._ordinary(db,p) for p in products):
            pair["issues"].append("BOM父子件或重复编码角色，保留原结构，另行核对共用规则")
            continue
        a,b=products;desired={p.id:{} for p in products}
        if code in USE_YANGUANG:
            from app.services.finished_stock_identity import FIELDS
            physical_fields=set(FIELDS+shared.EXTRA_FIELDS+('unit','length_mm','width_mm','height_mm',
                'material_id','legacy_material_text','flute_type','die_cut_path'))
            desired[b.id].update({field:getattr(a,field) for field in sorted(physical_fields)})
        if code in PROCESS:
            for p in products:desired[p.id]["production_process"]=PROCESS[code]
        if code=="80012289":
            for p in products:desired[p.id]["printing_colors"]="红色"
        if code in {"80011947","80012131"}:
            assert a.material and a.material.code==("VIK" if code=="80011947" else "PSP")
            for p in products:
                desired[p.id].update(material_id=a.material_id,
                    flute_type="B" if code=="80011947" else a.flute_type,layer_count=a.layer_count)
                if code=="80012131":desired[p.id]["product_name"]="DO O5 纸垫"
        if code in {"80010631","80010651","80012081"}:
            for field in ("length_mm","width_mm","height_mm"):
                if getattr(b,field) is None and getattr(a,field) is not None:
                    desired[b.id][field]=getattr(a,field)
        if code=="80012142" and b.flap_mm is None:
            desired[b.id]["flap_mm"]=a.flap_mm
        canonical_mold=a.mold_tool_id if a.mold_tool_id==b.mold_tool_id else None
        if code=='80012157' and a.mold_tool_id is None:
            # The explicitly selected Yangguang flat pad requires no mold.
            desired[b.id]['mold_tool_id']=None
            preserved.append(dict(mold_id=b.mold_tool_id,canonical_mold_id=None,note='按研光平衬板资料不使用模具；旧模具和历史记录保留'))
        elif a.mold_tool_id!=b.mold_tool_id:
            molds=[db.get(MoldTool,p.mold_tool_id) if p.mold_tool_id else None for p in products]
            identical_legacy = (all(m and m.is_active and m.archive_status=="active" and m.label_name==code for m in molds)
                    and all(m.rack_location=="1F-M-R04" for m in molds))
            confirmed_body = (code in CONFIRMED_MOLDS and
                {p.mold_tool_id for p in products} == CONFIRMED_MOLDS[code] and
                all(m and m.is_active and m.archive_status=="active" for m in molds))
            if identical_legacy or confirmed_body:
                canonical_mold=min(m.id for m in molds)
                for p in products:
                    if p.mold_tool_id!=canonical_mold:
                        desired[p.id]["mold_tool_id"]=canonical_mold
                        preserved.append(dict(mold_id=p.mold_tool_id,canonical_mold_id=canonical_mold,
                            note="旧模具编号和位置历史保留；产品改用同一现用本体，不伪造实物封存移动"))
            else:
                pair["issues"].append("模具的标签/位置/角色不同，须核实实物本体")
        if canonical_mold:
            mold=db.get(MoldTool,canonical_mold)
            existing=list(db.scalars(select(MoldToolCustomer).where(MoldToolCustomer.mold_tool_id==canonical_mold)))
            used={link.display_order for link in existing}; associated={link.customer_id for link in existing}
            for cid in CUSTOMERS:
                if cid not in associated:
                    position=next((x for x in (1,2) if x not in used),None)
                    used.add(position)
                    mold_links.append(dict(mold_tool_id=canonical_mold,customer_id=cid,display_order=position))
                    mold_versions[canonical_mold]=mold.version
        for product in products:
            after={key:value for key,value in desired[product.id].items() if getattr(product,key)!=value}
            if after:
                changes.append(dict(product_id=product.id,code=code,customer_id=product.customer_id,
                    version=product.version,before={key:getattr(product,key) for key in after},after=after))
    # Several paired products can use the same real mold; one link per customer.
    unique={(row["mold_tool_id"],row["customer_id"]):row for row in mold_links}
    return dict(pairs=pairs,products=changes,mold_links=list(unique.values()),
        mold_versions=mold_versions,preserved_old_molds=preserved)


def _apply_master(db, value):
    for change in value["products"]:
        clauses=[Product.id==change["product_id"],Product.customer_id==change["customer_id"],
            Product.product_code==change["code"],Product.version==change["version"]]
        clauses += [getattr(Product,key)==old for key,old in change["before"].items()]
        claimed=db.execute(update(Product).where(*clauses).values(**change["after"],
            version=Product.version+1,updated_at=Product.updated_at).execution_options(synchronize_session=False))
        if claimed.rowcount!=1:raise shared._error("已核实的产品资料发生变化，停止批量关联")
    for mid,version in value["mold_versions"].items():
        claimed=db.execute(update(MoldTool).where(MoldTool.id==int(mid),MoldTool.version==version,
            MoldTool.is_active.is_(True),MoldTool.archive_status=="active").values(
                version=MoldTool.version+1,updated_at=MoldTool.updated_at).execution_options(synchronize_session=False))
        if claimed.rowcount!=1:raise shared._error("现用模具资料发生变化，停止批量关联")
    for link in value["mold_links"]:db.add(MoldToolCustomer(**link,created_by=None))
    db.flush();db.expire_all()


def prepare(db):
    """Whole, reviewable proposed change, with no committed simulation writes."""
    value=_changes(db);result={**value,"groups":[],"pending":[]}
    with atomic_bom(db):
        with db.begin_nested() as simulation:
            _apply_master(db,value)
            for pair in value["pairs"]:
                issues=list(pair["issues"])
                if issues:
                    result["pending"].append(dict(**pair));continue
                pids=pair["product_ids"];products=[db.get(Product,pid) for pid in pids]
                member=db.get(SharedFinishedMember,pids[0])
                gid=member.group_id if member else None
                try:shared.preview(db,product_ids=pids,lot_ids=[],group_id=gid)
                except WarehouseInventoryError as e:
                    result["pending"].append(dict(**pair,identity_issue=str(e)));continue
                lids=[];printed={};supplements={};pending_lots=[];existing_lots=[]
                for lot in db.scalars(select(InventoryLot).join(FinishedGoodsInventoryDetail).where(
                        FinishedGoodsInventoryDetail.product_id.in_(pids),InventoryLot.status=="active",
                        InventoryLot.inventory_type=="finished").order_by(InventoryLot.id)):
                    fact=db.get(SharedFinishedLot,lot.id)
                    if fact:
                        if fact.group_id!=gid:raise shared._error("批次已属于不同共用组")
                        existing_lots.append(lot.id);continue
                    confirmation={lot.id:hashlib.sha256(shared.lot_identity(lot).encode()).hexdigest()} if pair["code"] in PRINTED_CODES else {}
                    supplement={}
                    if lot.id in LOT_FACTS:
                        code,fields=LOT_FACTS[lot.id]
                        assert pair['code']==code
                        frozen=json.loads(lot.finished_detail.physical_basis_json)
                        assert all(frozen.get(key)==values[0] for key,values in fields.items()),'已确认旧批次事实发生变化'
                        supplement={lot.id:dict(identity_sha256=hashlib.sha256(shared.lot_identity(lot).encode()).hexdigest(),
                            fields={key:values[1] for key,values in fields.items()})}
                    try:shared._preview_lots(db,products,[lot.id],confirmed_printed_lots=confirmation,confirmed_lot_fields=supplement)
                    except WarehouseInventoryError as e:
                        try:
                            frozen=json.loads(lot.finished_detail.physical_basis_json)
                            current=json.loads(shared.product_basis(db.get(Product,lot.finished_detail.product_id)))
                            differences={key:[frozen.get(key),current.get(key)] for key in current
                                if frozen.get(key)!=current.get(key) and key!='mold_tool_id'}
                        except (ValueError,TypeError,AttributeError):differences={'identity':['missing','current']}
                        pending_lots.append(dict(lot_id=lot.id,available=lot.quantity_available,
                            reserved=lot.quantity_reserved,reason=str(e),differences=differences));continue
                    lids.append(lot.id);printed.update(confirmation);supplements.update(supplement)
                preview=shared.preview(db,product_ids=pids,lot_ids=lids,group_id=gid,confirmed_printed_lots=printed,confirmed_lot_fields=supplements)
                result["groups"].append(dict(code=pair["code"],product_ids=pids,lot_ids=lids,
                    existing_group_id=gid,existing_lot_ids=existing_lots,confirmed_printed_lots=printed,
                    confirmed_lot_fields=supplements,
                    existing_group_version=db.get(SharedFinishedGroup,gid).version if gid else None,
                    existing_group_enabled=db.get(SharedFinishedGroup,gid).enabled if gid else None,
                    existing_auto_enroll=management._policy(db,gid) if gid else None,
                    preview=preview,pending_lots=pending_lots))
            simulation.rollback()
    db.expire_all()
    result["plan_hash"]=digest(result)
    return result


def apply_reviewed(db, *, expected_plan_hash, backup_receipt):
    if (not isinstance(backup_receipt,dict) or backup_receipt.get("verified") is not True
            or not re.fullmatch(r"[0-9a-fA-F]{64}",str(backup_receipt.get("sha256","")))):
        raise shared._error("缺少已验证的本次停服备份")
    with atomic_bom(db):
        previous=db.scalar(select(SharedFinishedMutation).where(SharedFinishedMutation.operation_key==KEY))
        if previous:
            if json.loads(previous.request_json)["plan_hash"]!=expected_plan_hash:
                raise shared._error("本次维护编号已用于另一份固定清单")
            return {**json.loads(previous.result_json),"replayed":True}
        plan=prepare(db)
        if plan["plan_hash"]!=expected_plan_hash:raise shared._error("正式产品或库存与固定审阅清单不一致，停止维护")
        _apply_master(db,plan)
        groups=[]
        for row in plan["groups"]:
            gid=row["existing_group_id"]
            if gid is None:
                gid=shared._apply_confirmed(db,product_ids=row["product_ids"],lot_ids=row["lot_ids"],
                    preview_hash=row["preview"]["preview_hash"],operation_key=KEY+"-"+row["code"],
                    evidence=EVIDENCE+" 备份SHA256："+backup_receipt["sha256"],actor=None,source="script",
                    confirmed_printed_lots=row["confirmed_printed_lots"],confirmed_lot_fields=row['confirmed_lot_fields'])["group_id"]
            elif row["lot_ids"]:
                if row["confirmed_printed_lots"] or row['confirmed_lot_fields']:raise shared._error("旧组实物差异须独立复核")
                group=db.get(SharedFinishedGroup,gid)
                args=dict(group_id=gid,action="add_lots",expected_version=group.version,lot_ids=row["lot_ids"])
                preview=management.preview_change(db,**args)
                management.change(db,**args,preview_hash=preview["preview_hash"],operation_key=KEY+"-add-"+row["code"],
                    evidence=EVIDENCE,actor=None,source="script")
            group=db.get(SharedFinishedGroup,gid)
            args=dict(group_id=gid,action="configure",expected_version=group.version,enabled=True,auto_enroll=True)
            preview=management.preview_change(db,**args)
            management.change(db,**args,preview_hash=preview["preview_hash"],operation_key=KEY+"-auto-"+row["code"],
                evidence=EVIDENCE,actor=None,source="script")
            groups.append(dict(code=row["code"],group_id=gid,product_ids=row["product_ids"],
                lot_ids=row["existing_lot_ids"]+row["lot_ids"]))
        if not groups:raise shared._error("没有可确认的共用组")
        result=dict(groups=groups,products_changed=len(plan["products"]),mold_links_added=len(plan["mold_links"]),
            pending=plan["pending"],pending_lots=[dict(code=r["code"],lots=r["pending_lots"]) for r in plan["groups"] if r["pending_lots"]],
            preserved_old_molds=plan["preserved_old_molds"],replayed=False)
        db.add(SharedFinishedMutation(group_id=groups[0]["group_id"],operation_key=KEY,
            request_json=canonical(plan),result_json=canonical(result)))
        append_audit_event(db,event_category="business",result="success",source="script",
            module_code="warehouse",action_code="reconcile_shared_customer_products",resource="products",
            entity_type="shared_finished_group",entity_id=groups[0]["group_id"],actor=None,
            operator_name="老板明确授权的发布维护任务",
            details=dict(plan_hash=plan["plan_hash"],backup_sha256=backup_receipt["sha256"],
                products_changed=len(plan["products"]),groups=len(groups),
                immutable_detail_key=KEY,evidence=EVIDENCE))
        db.flush()
        return result
