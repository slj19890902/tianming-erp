"""Layout parser for SATE/Unison purchase contracts, within the existing preview.

No database access, guessed quantities, price fallback or customer-ID constants.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

CUSTOMERS = {'SATE': '苏州驶安特汽车电子有限公司', 'UNISON': '苏州并作汽车电子有限公司'}
PO = re.compile(r'(?<![A-Z0-9])(?:SATE|UNISON)\d{10,16}(?![A-Z0-9])', re.I)


def compact(value):
    return re.sub(r'\s+', '', str(value or ''))


def code_candidate_key(value):
    """For suggestions only: never writes a normalized code or selects a product."""
    match = re.match(r'SATJ[1I]TP([A-Z0-9]{6,})$', compact(value).upper())
    return 'SATJ1TP'+match.group(1).translate(str.maketrans({'I':'1','O':'0','G':'6'})) if match else ''


def number(value):
    text = compact(value).replace(',', '').replace('，', '')
    if not re.fullmatch(r'\d+(?:\.\d+)?', text):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def parse_date(value):
    match = re.fullmatch(r'(20\d{2})[/.-](\d{1,2})[/.-](\d{1,2})', compact(value))
    try:
        return date(*map(int, match.groups())).isoformat() if match else None
    except ValueError:
        return None


def blocks(page):
    return [dict(b, x=sum(p[0] for p in b['box'])/4,
                 y=sum(p[1] for p in b['box'])/4,
                 h=max(p[1] for p in b['box'])-min(p[1] for p in b['box'])) for b in page]


def text_column_regions(page):
    """Only refine the three descriptive columns inside a positively found table."""
    entries = blocks(page)
    heads = []
    for label in ['物料编码', '品名', '规格', '数量']:
        found = [b for b in entries if re.sub(r'[^\u4e00-\u9fff]', '', b['text']) == label]
        if len(found) != 1:
            return []
        heads.append(found[0])
    xs = [b['x'] for b in heads]
    if xs != sorted(xs):
        return []
    top = max(b['y']+b['h']/2 for b in heads)+5
    notes = [b['y']-b['h']/2 for b in entries if b['y']>top and compact(b['text']).startswith('备注')]
    if not notes:
        return []
    edges = [xs[0]-(xs[1]-xs[0])/2, *[(a+b)/2 for a,b in zip(xs,xs[1:])]]
    return [(edges[i],top,edges[i+1],min(notes)-5) for i in range(3)]


def parse_sat_contract(text, source_name, route=None):
    """Return None for unrelated formats; incomplete contracts stay reviewable."""
    source = str(text)
    pos = list(dict.fromkeys(m.group(0) for m in PO.finditer(source)))
    names = [name for name in CUSTOMERS.values()
             if re.search(r'需方[）):：]{0,4}'+re.escape(name), compact(source))]
    if not pos or not names or '需方' not in compact(source):
        return None
    prefix = 'SATE' if pos[0].upper().startswith('SATE') else 'UNISON'
    name = CUSTOMERS[prefix]
    conflicts = len(pos) != 1 or names != [name]
    if route and (route.get('status') == 'needs_confirmation' or
                  (route.get('status') == 'locked' and route.get('customer_name') != name)):
        conflicts = True
    errors = ['客户或订单号证据冲突，请核对需方。'] if conflicts else []
    warnings = ['扫描订单：请核对编码和规格；原单未列数量单位。']
    items = []
    source_count = 0
    pages = getattr(text, 'pages', [])
    if not pages:
        errors.append('表格位置证据缺失，请重新识别或人工核对。')
    order_date_match = re.search(r'签订时间\s*[:：]?\s*(20\d{2}[/.-]\d{1,2}[/.-]\d{1,2})', source)
    for page_number, page in enumerate(pages, 1):
        entries = blocks(page)
        labels = ['物料编码', '品名', '规格', '数量', '税前单价', '税率', '税后单价', '价税合计', '出货时间']
        headers = []
        for label in labels:
            found = [b for b in entries if re.sub(r'[^\u4e00-\u9fff]', '', b['text']) == label]
            if len(found) != 1:
                errors.append(f'第{page_number}页表头“{label}”不唯一或缺失。')
                break
            headers.append(found[0])
        if len(headers) != len(labels):
            continue
        xs = [b['x'] for b in headers]
        if xs != sorted(xs):
            errors.append(f'第{page_number}页表格列顺序异常。')
            continue
        header_y = max(b['y']+b['h']/2 for b in headers)
        notes = [b['y']-b['h']/2 for b in entries if b['y'] > header_y and compact(b['text']).startswith('备注')]
        bottom = min(notes) if notes else float('inf')
        edges = [xs[0]-(xs[1]-xs[0])/2, *[(a+b)/2 for a,b in zip(xs,xs[1:])], xs[-1]+(xs[-1]-xs[-2])/2]
        cells = [[] for _ in labels]
        for b in entries:
            if not header_y < b['y'] < bottom:
                continue
            for col in range(len(labels)):
                if edges[col] <= b['x'] < edges[col+1]:
                    cells[col].append(b)
                    break
        # Two independent numeric columns locate rows, not OCR's missing row numbers.
        anchors = sorted([b for b in cells[3] if number(b['text']) is not None], key=lambda b:b['y'])
        delivery = [b for b in cells[8] if parse_date(b['text'])]
        source_count += max(len(anchors), len(delivery))
        if not anchors or len(anchors) != len(delivery):
            errors.append(f'第{page_number}页数量行与交期行不一致，可能漏行。')
        ys = [b['y'] for b in anchors]
        for i, anchor in enumerate(anchors):
            lo = (ys[i-1]+ys[i])/2 if i else header_y
            hi = (ys[i]+ys[i+1])/2 if i+1 < len(ys) else bottom
            grouped = [[b for b in col if lo <= b['y'] < hi] for col in cells]
            values = [' '.join(b['text'] for b in sorted(col,key=lambda b:(b['y'],b['x']))) for col in grouped]
            code_match = re.search(r'SAT[A-Z0-9]+', compact(values[0]), re.I)
            code = code_match.group(0).upper() if code_match else ''
            quantity, net, rate, price, amount = [number(values[j].replace('%','')) for j in (3,4,5,6,7)]
            due = parse_date(values[8])
            line = len(items)+1
            item_warnings = []
            if not code or not re.fullmatch(r'SATJ[1I]TP\d{6,}', code):
                item_warnings.append('编码疑似误读或裁切，请选择常用箱核对。')
            if not due or quantity is None or quantity <= 0 or price is None or amount is None:
                errors.append(f'第{line}行数量、含税价、金额或交期缺失/无效。')
            if quantity is not None and price is not None and amount is not None and abs(quantity*price-amount)>Decimal('0.005'):
                errors.append(f'第{line}行数量×含税单价与金额不符。')
            if net is None or rate is None or price is None or abs(net*(1+rate/100)-price)>Decimal('0.0002'):
                errors.append(f'第{line}行税前/税后单价核验不符。')
            confidence = min((b['confidence'] for col in grouped for b in col), default=0)
            if confidence < .8:
                item_warnings.append('部分文字置信度较低，请核对原单。')
            items.append(dict(line_no=line, product_code=code, raw_product_code=values[0],
                product_name=values[1], raw_product_name=values[1], specification=values[2], raw_spec_model=values[2],
                quantity=str(quantity) if quantity is not None else None, raw_quantity=values[3], unit='',
                unit_price=str(price) if price is not None else None, amount=str(amount) if amount is not None else None,
                tax_exclusive_unit_price=str(net) if net is not None else None,
                tax_rate=str(rate) if rate is not None else None, delivery_date=due,
                raw_lines=values, warnings=item_warnings, ocr_confidence=confidence,
                source_page=page_number, matched_product_id=None, preserve_pdf_price=True))
    if not items:
        errors.append('未能定位明细，请核对原单。')
    if source_count != len(items):
        errors.append('原单行数与解析行数不一致。')
    return dict(source_name=source_name, source_type='purchase_order_pdf',
        customer_name=name if not conflicts else None, customer_name_raw=name if not conflicts else None,
        customer_type='sat_contract', customer_po=pos[0], order_date=parse_date(order_date_match.group(1)) if order_date_match else None,
        delivery_date=min((i['delivery_date'] for i in items if i['delivery_date']), default=None),
        items=items, item_count=len(items), warnings=errors+warnings,
        recognition_status='needs_confirmation', parse_status='needs_confirmation', is_tianhua=False,
        customer_route={'status':'needs_confirmation' if conflicts else 'locked', 'customer_name':name if not conflicts else None,
                        'parser_key':'sat_contract', 'customer_type':'sat_contract'},
        integrity_check={'source_detail_count':source_count, 'parsed_detail_count':len(items),
                         'integrity_status':'failed' if errors else 'unknown', 'integrity_errors':errors,
                         'integrity_warnings':['原单无独立总计，请逐行核对。']},
        message='采购订单已识别，请核对后保存。')
