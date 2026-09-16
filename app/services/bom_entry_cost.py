"""Current-reference assembled stocktake costing, not an inventory conversion.

Material stays in the existing material-cost column. Standard assembly labour
is frozen separately so material margin and actual purchase facts do not change.
"""
from decimal import Decimal, ROUND_HALF_UP

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


def assembled_entry_cost(db, product):
    from app.models.order import OrderItem
    from app.models.product import Product
    from app.services.multilevel_bom_compile import compile_master_order_bom
    from app.services.inventory_valuation import CostResolution, CONFIRMED_SOURCE, resolve_product_cost
    from app.services.inventory_cost_snapshot import InventoryCostEstimate
    from app.services.bom_physical_quantities import resolve_bom_sheet_yield

    try:
        compiled = compile_master_order_bom(db, OrderItem(id=0, order_id=0,
            product_id=product.id, quantity=1, unit_price=0, subtotal=0))
        nodes = {n.product_id:n for n in compiled.graph.nodes}
        snapshots = {s.component_product_id:s for s in compiled.snapshots}
        cache = {}

        def visit(pid):
            if pid in cache:
                return cache[pid]
            node = nodes[pid]
            children = [e for e in compiled.graph.edges if e.parent_id == pid and e.relation == 'assembly']
            material, labour, parts = Decimal(0), Decimal(0), []
            master = db.get(Product, pid)
            if node.source != 'assembled':
                output = resolve_bom_sheet_yield(snapshots[pid], strict=True)
                result = resolve_product_cost(db, master, main_only=True,
                    physical_yield=output.yield_per_sheet, assembled_body_only=True)
                if not result.estimate:
                    raise ValueError(f'{master.product_name}：' + '；'.join(result.missing))
                material += result.estimate.unit_cost
                parts.append(dict(product_id=pid, quantity_per_set='1', unit_cost=str(material),
                    evidence=result.estimate.detail))
            if node.source == 'assembled' and not children:
                raise ValueError(f'{master.product_name}：缺少组装消耗子件')
            for edge in children:
                cm, cl, evidence = visit(edge.child_id)
                quantity = Decimal(str(edge.quantity))
                material += cm * quantity
                labour += cl * quantity
                parts.append(dict(product_id=edge.child_id, quantity_per_set=str(quantity),
                    unit_cost=str(cm), standard_labour_unit_cost=str(cl), evidence=evidence))
            own = assembly_standard(db, pid) if children else None
            if own:
                labour += Decimal(own['unit_cost'])
            evidence = dict(product_id=pid, product_version=node.version, source=node.source,
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
