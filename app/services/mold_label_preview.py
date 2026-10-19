"""Label-only content in the existing versioned print-layout snapshot."""
import json

CONTENT_FIELDS = ('mold_id', 'selected_product_id', 'label_title_kind', 'label_title_text',
                  'label_inventory_codes', 'label_inventory_code', 'label_customer_name',
                  'label_rack_location', 'label_mold_count', 'label_report_specification')


def freeze_content(row, option):
    title = option.get('title_text')
    if title is not None:
        title = ' '.join(str(title).split())
        if not title or len(title) > 250:
            raise ValueError('标签预览名称不能为空或超过250字')
        if row['label_title_kind'] != 'name':
            raise ValueError('图号须使用正式产品图号，不能在名称编辑中覆盖')
        row['label_title_text'] = title
    result = {key: row.get(key) for key in CONTENT_FIELDS}
    if row.get('label_text_overrides'):
        from app.services.mold_label_content import normalize_label_texts
        result['label_text_overrides'] = normalize_label_texts(row['label_text_overrides'])
    return result


def normalize_content_snapshots(raw):
    if not isinstance(raw, list) or len(raw) > 100:
        raise ValueError('打印内容快照无效')
    seen = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) not in (set(CONTENT_FIELDS), {*CONTENT_FIELDS, 'label_text_overrides'}):
            raise ValueError('打印内容快照字段不完整')
        identity = item['mold_id']
        if type(identity) is not int or identity <= 0 or identity in seen:
            raise ValueError('打印内容模具身份无效')
        seen.add(identity)
        if 'label_text_overrides' in item:
            from app.services.mold_label_content import normalize_label_texts
            normalize_label_texts(item['label_text_overrides'])
        if item['label_title_kind'] not in ('name', 'drawing'):
            raise ValueError('打印内容类型无效')
        if item['selected_product_id'] is not None and (type(item['selected_product_id']) is not int or item['selected_product_id'] <= 0):
            raise ValueError('打印产品身份无效')
        if not isinstance(item['label_inventory_codes'], list) or not all(isinstance(x, str) for x in item['label_inventory_codes']):
            raise ValueError('打印编码快照无效')
        for key in CONTENT_FIELDS:
            if key in ('mold_id', 'selected_product_id', 'label_inventory_codes'):
                continue
            if not isinstance(item[key], str) or len(item[key]) > 5000:
                raise ValueError('打印文字快照无效')
    # Detach caller-owned references before hashing/storing.
    return json.loads(json.dumps(raw, ensure_ascii=False))


def apply_frozen_content(row, layout_envelope):
    snapshots = (layout_envelope or {}).get('layout', {}).get('content_snapshots', [])
    content = next((x for x in snapshots if x['mold_id'] == row['mold_id']), None)
    if layout_envelope:
        row['label_text_overrides'] = {}
    if content:
        row.update(content)
    return row
