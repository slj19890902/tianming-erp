"""Alternative mold/cutting routes for one finished product, never a BOM sum."""
from copy import deepcopy
from decimal import Decimal,InvalidOperation


def normalize_variants(value):
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > 8:
        raise ValueError('每款产品最多8种加工方案')
    result=[];seen=set()
    for raw in value:
        if not isinstance(raw,dict):raise ValueError('加工方案格式无效')
        row={}
        for field in ('mold_tool_id','mold_count','length_parts','width_parts'):
            number=raw.get(field,1 if field!='mold_tool_id' else None)
            try:numeric=Decimal(str(number))
            except (InvalidOperation,ValueError):raise ValueError('模具、模数和分切份数须为正整数')
            if isinstance(number,bool) or not numeric.is_finite() or numeric!=numeric.to_integral_value():
                raise ValueError('模具、模数和分切份数须为正整数')
            row[field]=int(numeric)
            if not 1<=row[field]<= (2**31-1 if field=='mold_tool_id' else 100):
                raise ValueError('加工方案的模具或份数无效')
        row['id']=str(raw.get('id') or f"mold-{row['mold_tool_id']}").strip()
        if not row['id'] or len(row['id'])>60 or row['id'] in seen:raise ValueError('加工方案标识重复或无效')
        seen.add(row['id'])
        row['name']=str(raw.get('name') or f"{row['mold_count']}模").strip()[:80]
        for field in ('report_length_mm','report_width_mm'):
            val=raw.get(field)
            if val is not None and val!='':
                from app.core.sheet_dimensions import sheet_dimension_number
                row[field]=sheet_dimension_number(val)
                if row[field]<=0:raise ValueError('实际纸板长宽须大于0')
        if ('report_length_mm' in row) != ('report_width_mm' in row):
            raise ValueError('加工方案的纸板长宽须同时填写')
        from app.services.sheet_cutting_settings import SheetCuttingSettings
        settings=SheetCuttingSettings(length_parts=row['length_parts'],width_parts=row['width_parts'],
            mold_count=row['mold_count'],is_die_cut=True)
        settings.legacy_yield_mode
        result.append(row)
    return result or None


def validate_references(db,customer_id,variants):
    from app.api.products import _validate_references
    for row in variants or []:
        _validate_references(db,customer_id=customer_id,material_id=None,mold_tool_id=row['mold_tool_id'])


def resolve_variant(db,product,variant_id=None):
    variants=normalize_variants(product.production_variants)
    if not variants:
        if variant_id:raise ValueError('加工方案已变化，请重新选择')
        return product,None
    row=next((r for r in variants if r['id']==variant_id),None) if variant_id else variants[0]
    if row is None:raise ValueError('加工方案已变化，请重新选择')
    validate_references(db,product.customer_id,[row])
    # A detached projection changes this batch's process only, never the master.
    from types import SimpleNamespace
    values={column.key:getattr(product,column.key) for column in product.__table__.columns}
    for rel in ('material','mold_tool'):
        values[rel]=getattr(product,rel)
    from app.models.mold_tool import MoldTool
    values.update(mold_tool_id=row['mold_tool_id'],mold_tool=db.get(MoldTool,row['mold_tool_id']))
    values.update({k:row[k] for k in ('report_length_mm','report_width_mm') if k in row})
    values['sheet_cutting_settings']={'schema_version':2,'whole':{
        'mold_count':row['mold_count'],'length_parts':row['length_parts'],
        'width_parts':row['width_parts'],'is_die_cut':True}}
    return SimpleNamespace(**values),deepcopy(row)
