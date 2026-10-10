"""Weekly demand table, with independent source stock references, never ledger writes."""
import re
from io import BytesIO

CODE = re.compile(r'(?<![\w])\d{6,20}(?![\w])')
NUMBER = re.compile(r'^\d{1,10}$')


def _spatial_lines(pages):
    lines = []
    for page in pages:
        bands = []
        for entry in sorted(page, key=lambda e: sum(p[1] for p in e['box']) / 4):
            y = sum(p[1] for p in entry['box']) / 4
            height = max(p[1] for p in entry['box']) - min(p[1] for p in entry['box'])
            if not bands or abs(y - bands[-1][0]) > max(6, height * .55):
                bands.append((y, []))
            bands[-1][1].append(entry)
        lines.extend('\t'.join(e['text'] for e in sorted(band, key=lambda e: min(p[0] for p in e['box'])))
                     for _, band in bands)
    return lines


def source_lines(content, extension):
    if extension in ('.xls', '.xlsx'):
        from app.services.excel_document_import import _read_sheets
        _, sheets = _read_sheets(content)
        return [(f'{s.name}!{i}', '\t'.join(c.display_value for c in row))
                for s in sheets if not s.hidden for i, row in enumerate(s.rows, 1)]
    if extension == '.txt':
        return [(str(i), line) for i, line in enumerate(content.decode('utf-8-sig').splitlines(), 1)]
    import fitz
    if extension == '.pdf':
        pdf = content
    else:
        from PIL import Image, ImageOps
        with Image.open(BytesIO(content)) as original:
            if original.width * original.height > 24_000_000:
                raise ValueError('图片超过2400万像素，请缩小后重试')
            image = ImageOps.exif_transpose(original).convert('RGB')
            buffer = BytesIO()
            image.save(buffer, format='PDF', resolution=150)
            pdf = buffer.getvalue()
    with fitz.open(stream=pdf, filetype='pdf') as doc:
        if len(doc) > 5:
            raise ValueError('周需求单每次最多5页')
        if any(p.rect.width * p.rect.height > 2_000_000 for p in doc):
            raise ValueError('页面过大，请缩小后重试')
        text = '\n'.join(p.get_text(sort=True) for p in doc)
    if len(CODE.findall(text)) < 2:
        from app.services.pdf_ocr import ocr_pdf_bytes
        text, method = ocr_pdf_bytes(pdf)
        if not text:
            raise ValueError('本机图片识别不可用或未识别出文字，请使用Excel或粘贴需求表后核对')
        lines = _spatial_lines(text.pages) if getattr(text, 'pages', None) else str(text).splitlines()
    else:
        lines = text.splitlines()
    return [(str(i), line) for i, line in enumerate(lines, 1)]


def parse_weekly_lines(lines):
    rows, references, same_line_references, warnings = [], {}, {}, []
    for source_row, raw in lines:
        text = str(raw).strip().replace('，', ',')
        codes = list(CODE.finditer(text))
        if not codes:
            continue
        for index, match in enumerate(codes):
            code = match.group()
            end = codes[index + 1].start() if index + 1 < len(codes) else len(text)
            tokens = re.split(r'[\s|]+', text[match.end():end].strip())
            # Right panel begins with a category, not the demand quantity.
            if len(tokens) >= 3 and not NUMBER.fullmatch(tokens[0]):
                if re.fullmatch(r'\d+', tokens[1]) and re.fullmatch(r'-?\d+', tokens[2]):
                    reference = dict(category=tokens[0], on_hand=int(tokens[1]), remaining=int(tokens[2]))
                    if reference not in references.setdefault(code, []):
                        references[code].append(reference)
                    same_line_references[(str(source_row), code)] = reference
                    continue
            if index > 0:
                warnings.append(f'{source_row}：右侧区域未可靠识别，仅保留原文供核对')
                continue
            valid = bool(tokens and NUMBER.fullmatch(tokens[0]) and 0 < int(tokens[0]) <= 2147483647)
            source_parts = str(source_row).rsplit('!', 1)
            rows.append(dict(line_no=str(len(rows) + 1), source_row=int(source_parts[-1]),
                             source_sheet=source_parts[0] if len(source_parts) > 1 else None,
                             _source_key=str(source_row),
                             raw_product_code=code, raw_product_name=code,
                             quantity=int(tokens[0]) if valid else None,
                             source_location=text[:match.start()].strip(),
                             raw_lines=[text], warnings=[] if valid else ['需求数量未可靠识别，请核对原单']))
    if not rows:
        raise ValueError('没有识别到“存货编码 + 需求数量”。请粘贴左表，每行：位置 编码 数量；右侧库存表不能作为订单需求。')
    if len(rows) > 200:
        raise ValueError('一次最多导入200条需求')
    seen = set()
    for row in rows:
        code = row['raw_product_code']
        candidates = references.get(code, [])
        row['source_stock_reference'] = same_line_references.get((row.pop('_source_key'), code)) or (candidates[0] if len(candidates) == 1 else None)
        if len(candidates) > 1 and row['source_stock_reference'] is None:
            row['warnings'].append('原单同款库存参考不一致，请逐行核对')
        if code in seen:
            row['warnings'].append('同一存货编码重复，保留为独立需求行，请核对是否需要合并')
        seen.add(code)
    return rows, warnings
