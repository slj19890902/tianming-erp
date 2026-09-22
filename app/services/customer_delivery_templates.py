"""Allowlisted v2 layouts; v1 remains a separate immutable rendering contract."""
from __future__ import annotations

import math

CATALOG_VERSION = 'delivery-print-v2'
COLUMN_LABELS = {
    'sequence':'序号', 'customer_po':'订单号码', 'product_code':'存货编码',
    'product_name':'产品名称', 'specification':'尺寸', 'unit':'单位', 'quantity':'数量',
    'remarks':'备注', 'customer_material_code':'品目号', 'customer_drawing_number':'图号',
    'customer_category':'类别', 'customer_model':'使用型番', 'customer_product_name':'用途',
    'unit_price':'单价', 'amount':'金额',
}
PRESETS = {'yke':'研光', 'kew':'光洋', 'yl':'驿力'}


def preset_layout(key: str) -> dict:
    if key not in PRESETS:
        raise ValueError('未知客户送货模板')
    columns = {
        'yke': [('sequence',5),('customer_drawing_number',22),('customer_material_code',23),
                ('customer_category',10),('quantity',12),('unit_price',13),('amount',15)],
        'kew': [('sequence',4),('customer_model',27),('customer_drawing_number',17),
                ('customer_material_code',17),('customer_category',7),('quantity',8),
                ('unit_price',9),('amount',11)],
        'yl': [('customer_po',17),('customer_material_code',17),('customer_product_name',25),
               ('specification',17),('quantity',8),('unit_price',8),('amount',8)],
    }[key]
    return {'catalog_version':CATALOG_VERSION, 'preset':key,
            'columns':[{'key':k, 'label':('物料代号' if key=='yl' and k=='customer_material_code' else COLUMN_LABELS[k]),
                        'width':float(w)} for k,w in columns],
            'font_size_pt':10.0, 'show_prices':key!='yl', 'show_headers':key!='kew',
            'show_remarks':True, 'price_decimals':3, 'amount_decimals':2,
            'order_context':'', 'paper_width_mm':241.0, 'paper_height_mm':139.5}


def normalize_layout(value: dict) -> dict:
    if value.get('preset') not in PRESETS:
        raise ValueError('未知客户送货模板')
    result=preset_layout(value['preset'])
    columns=value.get('columns')
    if not isinstance(columns,list) or not 2 <= len(columns) <= 15:
        raise ValueError('送货明细列无效')
    seen=set(); normalized=[]
    for col in columns:
        if not isinstance(col,dict) or col.get('key') not in COLUMN_LABELS or col['key'] in seen:
            raise ValueError('送货明细含未知或重复字段')
        seen.add(col['key'])
        label=col.get('label')
        if not isinstance(label,str) or not label.strip() or len(label)>30 or any(ord(c)<32 for c in label):
            raise ValueError('列标题须为1～30个可见字符')
        width=col.get('width')
        if isinstance(width,bool) or not isinstance(width,(int,float)) or not math.isfinite(width) or not 3 <= width <= 50:
            raise ValueError('明细列宽须在3%～50%之间')
        normalized.append({'key':col['key'],'label':label.strip(),'width':round(float(width),2)})
    if not {'customer_material_code','quantity'}.issubset(seen):
        raise ValueError('客户料号及数量必须保留')
    if value['preset']=='yl' and 'customer_po' not in seen:
        raise ValueError('驿力模板必须保留逐行订单号码')
    if ('unit_price' in seen)!=('amount' in seen):
        raise ValueError('单价与金额列须同时配置')
    if not math.isclose(sum(c['width'] for c in normalized),100,abs_tol=.05):
        raise ValueError('送货模板明细列宽合计必须为100%')
    result['columns']=normalized
    for field in ('show_prices','show_headers','show_remarks'):
        val=value.get(field,result[field])
        if not isinstance(val,bool): raise ValueError('模板显隐设置无效')
        result[field]=val
    if result['show_prices'] and 'unit_price' not in seen:
        raise ValueError('有价模板缺少单价与金额列')
    for field,low,high in (('price_decimals',0,6),('amount_decimals',2,6)):
        val=value.get(field,result[field])
        if isinstance(val,bool) or not isinstance(val,int) or not low<=val<=high: raise ValueError('价格显示精度无效')
        result[field]=val
    font=value.get('font_size_pt',10)
    if isinstance(font,bool) or not isinstance(font,(float,int)) or not 9<=font<=14:
        raise ValueError('针式明细字号须在9～14pt之间')
    result['font_size_pt']=float(font)
    context=value.get('order_context','')
    if context not in ('','海外订单'): raise ValueError('送货场景标识无效')
    result['order_context']=context
    return result
