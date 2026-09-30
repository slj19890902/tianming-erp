"""Factory-confirmed nominal thickness by flute, independent of material code."""
from decimal import Decimal
import re

FLUTE_THICKNESS = {'AB':7, 'B':3, 'A':4, 'E':1, '白卡':1, 'BE':4, 'ABC':9, 'AAA':12}


def flute_paper(product):
    key = re.sub(r'\s+', '', str(product.flute_type or '')).upper().removesuffix('瓦')
    if key in {'白卡纸', '卡纸', '无楞'} or (key == 'NONE' and product.layer_count == 1):
        key = '白卡'
    value = FLUTE_THICKNESS.get(key)
    return {'flute': key, 'thickness_mm': value,
            'source': f'常用箱楞型：{key}（2026-09-30）' if value is not None else None}


def bind_slot_width(product, template, parameters, state):
    values = dict(parameters)
    if (state or {}).get('slot_width_mode') == 'flute' and template in {'slotted_v1', 'partition_v1'}:
        paper = flute_paper(product)
        if paper['thickness_mm'] is None:
            from app.services.drawing_geometry import DrawingGeometryError
            raise DrawingGeometryError('常用箱楞型没有对应厚度，请先核对楞型')
        values['slot_width_mm'] = Decimal(paper['thickness_mm'])
    return values
