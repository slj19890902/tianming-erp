from app.services.weekly_demand_import import _spatial_lines, parse_weekly_lines


def box(x, y, text, width=70):
    return dict(box=[[x,y],[x+width,y],[x+width,y+20],[x,y+20]],text=text,confidence=.99)


def test_tilted_side_by_side_tables_cannot_mix_reference_into_demand():
    page=[]
    for i in range(5):
        y=30*i
        code=str(80010000+i)
        page += [box(10,y,'板',20),box(60,y,code+('_' if i==0 else '-' if i==1 else '')),
                 box(145,y,str(900+i),35),box(300,y+22,code),
                 box(390,y+22,'A',20),box(430,y+22,'1110',50),box(500,y+22,'210',40)]
    rows,_=parse_weekly_lines([(str(i),line) for i,line in enumerate(_spatial_lines([page]),1)])
    assert [(r['raw_product_code'],r['quantity']) for r in rows]==[(str(80010000+i),900+i) for i in range(5)]
    assert all(r['source_stock_reference']['on_hand']==1110 for r in rows)


def test_single_demand_table_keeps_legitimate_repeated_rows():
    page=[]
    for i in range(4):
        page += [box(10,30*i,'板',20),box(60,30*i,'80010000'),box(145,30*i,str(i+1),30)]
    rows,_=parse_weekly_lines([(str(i),line) for i,line in enumerate(_spatial_lines([page]),1)])
    assert [r['quantity'] for r in rows]==[1,2,3,4]


def test_unreadable_left_codes_keep_their_delivery_rows_for_manual_review():
    page=[]
    for i in range(5):
        code=str(80010000+i)
        page += [box(60,30*i,code if i<3 else '蠲',70),box(145,30*i,str(20+i),35),
                 box(300,30*i+22,code),box(390,30*i+22,'A',20),
                 box(430,30*i+22,'100',50),box(500,30*i+22,'80',40)]
    rows,_=parse_weekly_lines([(str(i),line) for i,line in enumerate(_spatial_lines([page]),1)])
    assert len(rows)==5
    assert [r['quantity'] for r in rows]==[20,21,22,23,24]
    assert [r['raw_product_code'] for r in rows[3:]]==['','']
    assert all('存货编码未可靠识别' in r['warnings'][0] for r in rows[3:])
    assert all(r['source_stock_reference'] is None for r in rows[3:])


def test_two_demand_tables_are_not_mislabeled_as_stock_reference():
    page=[]
    for i in range(5):
        code=str(80010000+i)
        page += [box(60,30*i,code),box(145,30*i,'30',35),
                 box(300,30*i,code),box(390,30*i,'50',35)]
    assert not any(line.startswith('[库存参考]') for line in _spatial_lines([page]))
