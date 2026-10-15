"""Current-reference assembled stocktake costing, not an inventory conversion.

Material stays in the existing material-cost column. Standard assembly labour
is frozen separately so material margin and actual purchase facts do not change.
"""
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json

from app.models.processing_cost import ProcessingCostSettings
from app.services.processing_cost import get_product_processing_profile, calculate_worker_day_cost

Q = Decimal('.0001')
DEFAULT_OUTPUT_PER_PERSON_HOUR = Decimal(50)


def assembly_standard(db, product_id):
    settings = db.get(ProcessingCostSettings, 1)
    if settings is None:
        raise ValueError('请先维护人工成本基础设置')
    daily = calculate_worker_day_cost(settings)
    if daily is None:
        raise ValueError('请先维护每人月人工费用')
    profile = get_product_processing_profile(db, product_id)
    days = profile.assembly_worker_days_per_1000 if profile else None
    hours = Decimal(settings.working_hours_per_day)
    if days is None:
        days = Decimal(1000) / DEFAULT_OUTPUT_PER_PERSON_HOUR / hours
    days = Decimal(days)
    unit = daily * days / Decimal(1000)
    return dict(rule='assembly-person-hour-v1', basis='standard_labour_not_actual_payroll',
        unit_cost=str(unit.quantize(Q, rounding=ROUND_HALF_UP)),
        output_per_person_hour=str(Decimal(1000) / days / hours),
        worker_days_per_1000=str(days), settings_version=settings.version,
        monthly_salary=str(settings.average_worker_monthly_salary),
        monthly_social_cost=str(settings.average_worker_monthly_social_cost),
        working_days_per_month=str(settings.working_days_per_month), working_hours_per_day=str(hours),
        profile_id=profile.id if profile else None, profile_version=profile.version if profile else None)


def assembled_entry_cost(db, product, *, for_entry=False):
    from types import SimpleNamespace
    from sqlalchemy import select
    from app.models.product_bom import ProductBomComponent
    from app.models.product import Product
    from app.services.multilevel_bom_master import load_master_structure
    from app.services.sheet_cutting_settings import component_settings
    from app.services.inventory_valuation import CostResolution, CONFIRMED_SOURCE, resolve_product_cost
    from app.services.inventory_cost_snapshot import InventoryCostEstimate
    from app.services.bom_physical_quantities import resolve_bom_sheet_yield

    try:
        # This values physical stock already present. A production compiler's
        # mold-registration gate is not evidence of that stock's dimensions or
        # material cost. Keep the real graph, sheet yield and current quote
        # checks, without requiring employees to plan a new production run.
        structure = load_master_structure(db, product.id)
        profiles = structure['profiles']
        edges = structure['edges']
        relations = {row.id:row for row in db.scalars(select(ProductBomComponent).where(
            ProductBomComponent.id.in_([edge['bom_component_id'] for edge in edges])))}
        cache = {}

        def visit(pid):
            if pid in cache:
                return cache[pid]
            master = db.get(Product, pid)
            source = profiles.get(pid, 'purchased' if master.supply_mode == 'external_purchase' else 'manufactured')
            children = [e for e in edges if e['parent_id'] == pid and e['relation'] == 'assembly']
            material, labour, parts = Decimal(0), Decimal(0), []
            if source != 'assembled':
                incoming = [relations[e['bom_component_id']] for e in edges if e['child_id'] == pid]
                limits = {(row.mold_max_yield_per_sheet, row.spare_sheet_quantity) for row in incoming}
                if len(limits) > 1:
                    raise ValueError(f'{master.product_code}：BOM开料出数配置不一致，请核对常用箱配方')
                settings = component_settings(master.sheet_cutting_settings)
                snapshot = SimpleNamespace(
                    mold_max_yield_per_sheet=incoming[0].mold_max_yield_per_sheet if incoming else None,
                    is_die_cut=settings.is_die_cut if settings else master.box_category == 'die_cut',
                    sheet_cutting_settings_snapshot=master.sheet_cutting_settings,
                    snapshot_component_box_style=master.box_style,
                    snapshot_component_default_cutting_mode=master.default_cutting_mode)
                output = resolve_bom_sheet_yield(snapshot, strict=True)
                result = resolve_product_cost(db, master, main_only=True,
                    physical_yield=output.yield_per_sheet, assembled_body_only=True, for_entry=for_entry)
                if not result.estimate:
                    raise ValueError(f'{master.product_code} · {master.product_name}：' + '；'.join(result.missing))
                material += result.estimate.unit_cost
                parts.append(dict(product_id=pid, quantity_per_set='1', unit_cost=str(material),
                    evidence=result.estimate.detail))
            if source == 'assembled' and not children:
                raise ValueError(f'{master.product_name}：缺少组装消耗子件')
            for edge in children:
                cm, cl, evidence = visit(edge['child_id'])
                quantity = Decimal(str(edge['quantity']))
                material += cm * quantity
                labour += cl * quantity
                parts.append(dict(product_id=edge['child_id'], quantity_per_set=str(quantity),
                    unit_cost=str(cm), standard_labour_unit_cost=str(cl), evidence=evidence))
            own = assembly_standard(db, pid) if children else None
            if own:
                labour += Decimal(own['unit_cost'])
            evidence = dict(product_id=pid, product_version=master.version, source=source,
                components=parts, assembly_standard=own, standard_labour_unit_cost=str(labour))
            cache[pid] = material, labour, evidence
            return cache[pid]

        material, labour, evidence = visit(product.id)
        material = material.quantize(Q, rounding=ROUND_HALF_UP)
        labour = labour.quantize(Q, rounding=ROUND_HALF_UP)
        return CostResolution(InventoryCostEstimate(material, Decimal(0), Decimal(0), CONFIRMED_SOURCE,
            {**evidence, 'algorithm_version':'assembled-entry-v1', 'currency':'CNY', 'tax_included':True,
             'product_unit':product.unit,
             'estimate_basis':'stocktake_current_bom_reference_not_historical_purchase',
             'formula':'本体材料或外购成本（如有）＋各组装子件每片成本 × 每套用量；不含随货附件',
             'standard_labour_unit_cost':str(labour), 'material_unit_cost':str(material),
             'standard_total_unit_cost':str(material+labour)}), [])
    except ValueError as error:
        return CostResolution(None, [str(error)])


def freeze_assembly_standard(db, product_id, sources, quantity):
    """Freeze only labour; source material conservation remains unchanged."""
    from app.services.inventory_valuation import frozen_cost
    inherited = Decimal(0)
    for lot, take in sources:
        _, evidence = frozen_cost(lot, db)
        if evidence.get('standard_labour_missing'):
            return dict(standard_labour_missing=f'来源批次{lot.id}：'+evidence['standard_labour_missing'])
        inherited += Decimal(evidence.get('standard_labour_unit_cost', '0')) * take
    try:
        own = assembly_standard(db, product_id)
    except ValueError as error:
        return dict(standard_labour_missing=str(error))
    unit = (inherited / quantity + Decimal(own['unit_cost'])).quantize(Q, rounding=ROUND_HALF_UP)
    from app.models.product import Product
    product = db.get(Product, product_id)
    return dict(assembly_standard=own, standard_labour_unit_cost=str(unit), product_unit=product.unit,
        inherited_standard_labour_total=str(inherited))


def _lot_physical_basis(db, lot):
    if lot.finished_detail and lot.finished_detail.physical_basis_json:
        return lot.finished_detail.physical_basis_json
    from app.models.warehouse_goods import WarehouseGoodsProfile
    profile = db.get(WarehouseGoodsProfile, lot.id)
    if profile:
        try:
            value = json.loads(profile.data_json)
        except (TypeError, ValueError):
            value = {}
        raw = value.get('physical_basis') if isinstance(value, dict) else None
        if isinstance(raw, str) and raw:
            return raw
    return None


def consumed_assembly_spec_issues(spec, parent_basis, actual_inputs=None):
    """Validate frozen consumed-child facts; never consult today's BOM/master."""
    try:
        basis = json.loads(parent_basis) if isinstance(parent_basis, str) else parent_basis
    except (TypeError, ValueError):
        basis = None
    if not isinstance(spec, dict) or spec.get('schema') != 'consumed-assembly-v1':
        return ['组套缺少本次真实消耗子件的冻结依据']
    if not isinstance(basis, dict) or spec.get('parent_product_id') != basis.get('product_id'):
        return ['组套父件身份与冻结依据不一致']
    try:
        output_quantity = Decimal(str(spec.get('output_quantity')))
    except (TypeError, ValueError, ArithmeticError):
        output_quantity = Decimal(0)
    if output_quantity <= 0:
        return ['组套套数无效']
    assembly = basis.get('assembly')
    components = spec.get('components')
    if not isinstance(assembly, list) or not assembly or not isinstance(components, list):
        return ['纯组套父件缺少有效冻结配方']
    expected = {}
    for row in assembly:
        try:
            pid = int(row['product_id']); per_set = Decimal(str(row['quantity']))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return ['冻结配方的子件身份或每套用量无效']
        if pid in expected or per_set <= 0 or not row.get('basis_hash'):
            return ['冻结配方的子件身份或每套用量无效']
        expected[pid] = (per_set, row['basis_hash'])
    issues=[]; seen=set(); frozen_inputs=[]
    for component in components:
        if not isinstance(component, dict):
            return ['组套子件冻结依据格式无效']
        try:
            pid=int(component['product_id']); per_set=Decimal(str(component['quantity_per_set']))
            consumed=Decimal(str(component['consumed_quantity']))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return ['组套子件冻结数量格式无效']
        code=str(component.get('product_code') or pid)
        if pid in seen or pid not in expected:
            issues.append(f'子件 {code}：不在父件冻结配方或重复出现')
            continue
        seen.add(pid)
        raw=component.get('basis_json')
        target_raw=component.get('target_basis_json') or raw
        shared_evidence=component.get('shared_bom_evidence')
        try:
            child=json.loads(raw) if isinstance(raw,str) else None
        except (TypeError,ValueError):
            child=None
        expected_per_set,expected_hash=expected[pid]
        if (not isinstance(child,dict) or child.get('schema')!=1
                or (child.get('product_id')!=pid and not shared_evidence)):
            issues.append(f'子件 {code}：缺少冻结实物身份')
        elif not child.get('unit'):
            issues.append(f'子件 {code}：缺少冻结单位')
        elif not child.get('spec') and not child.get('assembly'):
            issues.append(f'子件 {code}：缺少实物规格')
        if shared_evidence and (not isinstance(shared_evidence,dict)
                or shared_evidence.get('product_id')!=pid
                or shared_evidence.get('target_basis')!=target_raw
                or not shared_evidence.get('source_identity')):
            issues.append(f'子件 {code}：共用库存转换依据不完整')
        if (per_set!=expected_per_set or consumed!=output_quantity*expected_per_set
                or not isinstance(target_raw,str)
                or hashlib.sha256(target_raw.encode()).hexdigest()!=expected_hash):
            issues.append(f'子件 {code}：配比、消耗数量或冻结身份与父件配方不一致')
        sources=component.get('source_lots')
        if not isinstance(sources,list) or not sources:
            issues.append(f'子件 {code}：缺少来源批次')
        else:
            source_total=Decimal(0)
            for source in sources:
                try:
                    lot_id=int(source['lot_id']); quantity=Decimal(str(source['quantity']))
                except (KeyError,TypeError,ValueError,ArithmeticError):
                    issues.append(f'子件 {code}：来源批次数量无效');continue
                if quantity<=0:
                    issues.append(f'子件 {code}：来源批次数量无效')
                source_total+=quantity
                frozen_inputs.append((lot_id,pid,quantity))
            if source_total!=consumed:
                issues.append(f'子件 {code}：来源批次合计与实际消耗不一致')
    if seen!=set(expected):
        issues.append('组套未覆盖冻结配方的全部子件')
    if actual_inputs is not None:
        try:
            actual=sorted((int(r['lot_id']),int(r['product_id']),Decimal(str(r['quantity']))) for r in actual_inputs)
        except (KeyError,TypeError,ValueError,ArithmeticError):
            actual=[]
        if sorted(frozen_inputs)!=actual:
            issues.append('组套冻结来源与实际消耗流水不一致')
    return list(dict.fromkeys(issues))


def freeze_consumed_assembly_spec(db, *, parent_basis, output_quantity, sources):
    """Freeze actual child lots used to form a pure-assembly parent."""
    try:
        basis=json.loads(parent_basis)
    except (TypeError,ValueError) as error:
        raise ValueError('父件冻结配方格式无效') from error
    recipe={int(row['product_id']):row for row in basis.get('assembly') or []}
    if not recipe:
        raise ValueError('纯组套父件缺少有效冻结配方')
    grouped={}
    for source in sources:
        if len(source) == 3:
            lot,take,product_id = source
            shared_evidence = None
        else:
            lot,take,product_id,shared_evidence = source
        pid=int(product_id);raw=_lot_physical_basis(db,lot)
        from app.models.product import Product
        product=db.get(Product,pid);code=product.product_code if product else str(pid)
        if pid not in recipe:
            raise ValueError(f'子件 {code}：不在父件冻结配方')
        if not raw:
            raise ValueError(f'子件 {code}：缺少冻结实物身份')
        target_raw = shared_evidence.get('target_basis') if shared_evidence else raw
        if shared_evidence:
            from app.services.shared_bom_stock import verify_consumed
            if not verify_consumed(db, lot, shared_evidence, pid, shared_evidence.get('customer_id')):
                raise ValueError(f'子件 {code}：共用库存转换依据已失效')
        row=grouped.setdefault(pid,dict(product_id=pid,product_code=code,
            quantity_per_set=recipe[pid]['quantity'],consumed_quantity=0,
            basis_json=raw,target_basis_json=target_raw,
            **({'shared_bom_evidence':shared_evidence} if shared_evidence else {}),source_lots=[]))
        if row['basis_json']!=raw or row['target_basis_json']!=target_raw:
            raise ValueError(f'子件 {code}：同次组套混用了不同实物身份')
        row['consumed_quantity']+=int(take)
        row['source_lots'].append(dict(lot_id=lot.id,quantity=int(take)))
    spec=dict(schema='consumed-assembly-v1',parent_product_id=basis.get('product_id'),
        output_quantity=int(output_quantity),components=sorted(grouped.values(),key=lambda row:row['product_id']))
    issues=consumed_assembly_spec_issues(spec,basis)
    if issues:
        raise ValueError('；'.join(issues))
    return spec
