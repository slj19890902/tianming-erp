"""Bounded offline Mac OCR for positively identified ruled purchase contracts.

Read text from real cell crops; never complete characters using the catalogue.
The signed helper uses Apple's installed Vision framework, without downloads.
"""
from __future__ import annotations
import base64
import json
import re
import subprocess
import sys
from pathlib import Path

LABELS = ['序号', '物料编码', '品名', '规格', '数量', '税前单价', '税率', '税后单价', '价税合计', '出货时间']


def helper_path():
    return Path(__file__).resolve().parents[2] / 'runtime/bin/tianming-pdf-ocr'


def _recognize(images):
    encoded = json.dumps(images).encode()
    if len(encoded) > 48 * 1024 * 1024:
        raise ValueError('OCR image budget exceeded')
    result = subprocess.run([str(helper_path())], input=encoded, capture_output=True,
        timeout=90, check=True, env={'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8'})
    if len(result.stdout) > 4 * 1024 * 1024:
        raise ValueError('OCR result budget exceeded')
    pages = json.loads(result.stdout)
    if len(pages) != len(images):
        raise ValueError('OCR page count mismatch')
    return pages


def _table_plan(image, entries):
    import cv2
    import numpy as np
    heads = []
    for label in LABELS:
        found = [b for b in entries if re.sub(r'[^\u4e00-\u9fff]', '', b['text']) == label]
        if len(found) != 1:
            return None
        heads.append(found[0])
    xs = [b['x'] for b in heads]
    if xs != sorted(xs):
        return None
    top = max(b['y'] + b['h']/2 for b in heads) + 2
    notes = [b['y'] - b['h']/2 for b in entries if b['y'] > top and b['text'].startswith('备注')]
    if not notes:
        return None
    bottom = min(notes)-4
    roi = image[int(top*2):int(bottom*2)]
    height = len(roi)
    if height < 80:
        return None
    lines = cv2.HoughLinesP(cv2.Canny(roi,70,180),1,np.pi/1800,
        threshold=50,minLineLength=height*.22,maxLineGap=height*.12)
    if lines is None:
        return None
    vertical = []
    for row in lines.reshape(-1,4):
        x1,y1,x2,y2 = map(int,row)
        if abs(y2-y1)>height*.22 and abs(x2-x1)<abs(y2-y1)*.06:
            vertical.append(((x1+x2)/4,abs(y2-y1)))
    groups=[]
    for x,length in sorted(vertical):
        if not groups or x-groups[-1][-1][0]>7.5:
            groups.append([(x,length)])
        else:
            groups[-1].append((x,length))
    boundaries=[max(group,key=lambda p:p[1]) for group in groups]
    left=[x for x,_ in boundaries if x<xs[0]]
    right=[x for x,_ in boundaries if x>xs[-1]]
    if not left or not right:
        return None
    edges=[max(left)]
    for a,b in zip(xs,xs[1:]):
        choices=[(x,length) for x,length in boundaries if a<x<b]
        if not choices:
            return None
        edges.append(max(choices,key=lambda p:p[1])[0])
    edges.append(min(right))
    if any(b-a<15 for a,b in zip(edges,edges[1:])):
        return None
    return top,bottom,edges


def refine_scanned_contract(content, text):
    """Original evidence remains available when helper or grid recognition fails."""
    from app.services.pdf_ocr import OCRText, _try_import_fitz, _get_easyocr_reader
    from app.services.pdf_sat_contract import blocks, number
    if sys.platform != 'darwin' or not isinstance(text,OCRText) or not helper_path().is_file():
        return text
    if not 0<len(content)<=32*1024*1024 or not 0<len(text.pages)<=10:
        return text
    # This is a format gate, not a customer/PO identity inference.
    if '采购订单' not in str(text) or '需方' not in str(text):
        return text
    try:
        import numpy as np
        import cv2
        fitz=_try_import_fitz()
        if fitz is None:
            return text
        result_pages=[]
        header_text=[]
        with fitz.open(stream=content,filetype='pdf') as doc:
            if len(doc)!=len(text.pages):
                return text
            for page,evidence in zip(doc,text.pages):
                if page.rect.width*page.rect.height*(400/72)**2>40_000_000:
                    return text
                pix=page.get_pixmap(matrix=fitz.Matrix(400/72,400/72),colorspace=fitz.csGRAY)
                image=np.frombuffer(pix.samples,dtype=np.uint8).reshape(pix.height,pix.width)
                entries=blocks(evidence)
                plan=_table_plan(image,entries)
                if plan is None:
                    return text
                top,bottom,edges=plan
                columns=[1,2,3,9]
                inputs=[]
                origins=[]
                for col in columns:
                    x0,y0,x1,y1=map(int,[edges[col]*2-(12 if col==1 else 0),top*2,edges[col+1]*2+(12 if col==1 else 0),bottom*2])
                    crop=cv2.copyMakeBorder(image[y0:y1,x0:x1],20,20,20,20,cv2.BORDER_CONSTANT,value=255)
                    inputs.append({'image':base64.b64encode(cv2.imencode('.png',crop)[1]).decode(),
                                   'latin':col not in (2,3)})
                    origins.append((x0-20,y0-20))
                anchors=sorted([b['y'] for b in entries if edges[4]<=b['x']<edges[5]
                                and top<b['y']<bottom and number(b['text']) is not None])
                if not 0<len(anchors)<=100:
                    return text
                sequence_blocks=[]
                for y in anchors:
                    x0,y0,x1,y1=map(int,[edges[0]*2+12,(y-18)*2,edges[1]*2-12,(y+18)*2])
                    crop=image[y0:y1,x0:x1].copy()
                    mask=cv2.threshold(crop,0,255,cv2.THRESH_BINARY_INV+cv2.THRESH_OTSU)[1]
                    _,_,stats,_=cv2.connectedComponentsWithStats(mask)
                    ink=[v for v in stats[1:] if 4<v[3]<60 and 1<v[2]<50 and v[4]>12]
                    if not ink:
                        return text
                    a=min(v[0] for v in ink);b=min(v[1] for v in ink)
                    c=max(v[0]+v[2] for v in ink);d=max(v[1]+v[3] for v in ink)
                    crop=cv2.copyMakeBorder(crop[b:d,a:c],8,8,8,8,cv2.BORDER_CONSTANT,value=255)
                    sequence=_get_easyocr_reader().recognize(crop,
                        horizontal_list=[[0,crop.shape[1],0,crop.shape[0]]],free_list=[],
                        detail=1,allowlist='0123456789',paragraph=False)
                    if len(sequence)!=1 or float(sequence[0][2])<.9:
                        return text
                    sequence_blocks.append({'text':sequence[0][1],'confidence':float(sequence[0][2]),
                        'box':[[x0/2,y0/2],[x1/2,y0/2],[x1/2,y1/2],[x0/2,y1/2]],'contract_column':0})
                # Read the document header separately; never use the filename as PO.
                inputs.append({'image':base64.b64encode(cv2.imencode('.png',image[:int(top*2)-50])[1]).decode(),'latin':False})
                recognized=_recognize(inputs)
                header_text.extend(b['text'] for b in recognized[-1])
                kept=list(sequence_blocks)
                for b in entries:
                    if not top<b['y']<bottom:
                        kept.append(b)
                    else:
                        col=next((i for i in range(10) if edges[i]<=b['x']<edges[i+1]),None)
                        if col in (4,5,6,7,8):
                            kept.append({**b,'contract_column':col})
                for col,(x0,y0),items in zip(columns,origins,recognized):
                    for b in items:
                        kept.append({**b,'box':[[(x+x0)/2,(y+y0)/2] for x,y in b['box']],
                                     'contract_column':col})
                result_pages.append(kept)
        output=OCRText(str(text)+'\n'+'\n'.join(header_text),result_pages)
        output.contract_layout_verified=True
        return output
    except (ImportError,OSError,ValueError,RuntimeError,subprocess.SubprocessError,KeyError,TypeError):
        # Do not log document contents or local paths. Existing OCR remains usable.
        return text
