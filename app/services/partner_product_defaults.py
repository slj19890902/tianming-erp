"""Common-box defaults, without overriding explicit choices."""
def product_defaults(*, customer_name, layer_count, flute_type, box_style, production_process):
    values = {}
    if layer_count == 5 and not flute_type:
        values['flute_type'] = 'AB'
    a1 = 'A1' in (box_style or '').upper() or '0201' in (box_style or '')
    processes = [p.strip() for p in (production_process or '').replace('，', ',').split(',') if p.strip()]
    if a1 and not any(p in processes for p in ('打钉', '钉箱', '粘贴', '粘箱')):
        values['production_process'] = ','.join([p for p in processes if p != '无需结合'] + ['打钉'])
    if any(part in (customer_name or '') for part in ('研光', '光洋')):
        values['production_label_enabled'] = True
        values['production_label_units_per_label'] = 5 if a1 else 50
    return values
