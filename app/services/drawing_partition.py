"""A slotted partition's actual closed outline, in millimetres."""
from decimal import Decimal, InvalidOperation

from app.services.drawing_geometry import DrawingGeometryError, _polyline, number, plain


def partition_geometry(params):
    length = number(params.get("length_mm"), "隔板长")
    height = number(params.get("height_mm"), "隔板高")
    width = number(params.get("slot_width_mm"), "槽宽")
    depth = number(params.get("slot_depth_mm"), "槽深")
    pitch = number(params.get("slot_pitch_mm"), "槽中心间距")
    offset = number(params.get("slot_offset_mm"), "首槽中心距")
    try:
        count, edge = (Decimal(str(params.get(k))) for k in ("slot_count", "slot_edge"))
        if not count.is_finite() or count != int(count) or not 1 <= count <= 32 or edge not in (0, 1):
            raise ValueError()
    except (ValueError, TypeError, InvalidOperation, OverflowError) as error:
        raise DrawingGeometryError("槽数须为1至32的整数，开槽边须选择上边或下边") from error
    if depth >= height:
        raise DrawingGeometryError("槽深须小于隔板高")
    centres = [number(params.get(f"slot_{i+1}_position_mm", offset+i*pitch), f"第{i+1}槽中心距")
               for i in range(int(count))]
    if any(c-width/2 <= 0 or c+width/2 >= length for c in centres):
        raise DrawingGeometryError("插槽越出隔板边界，槽边与隔板边必须留有纸料")
    if any(b-a <= width for a, b in zip(centres, centres[1:])):
        raise DrawingGeometryError("插槽顺序或间距无效，插槽之间不能重叠")
    z = Decimal(0)
    points = [(z,z)]
    for centre in centres:
        points += [(centre-width/2,z), (centre-width/2,depth), (centre+width/2,depth), (centre+width/2,z)]
    points += [(length,z), (length,height), (z,height), (z,z)]
    if edge:
        points = [(x,height-y) for x,y in points]
    cuts = _polyline(points)
    dimensions = {"隔板长": plain(length), "隔板高": plain(height), "槽宽": plain(width),
                  "槽深": plain(depth), "槽中心间距": plain(pitch), "首槽中心距": plain(offset)}
    gap = max(length,height)/20
    specs = [("length_mm","隔板长","x",z,length), ("height_mm","隔板高","y",z,height),
             ("slot_width_mm","槽宽","x",centres[0]-width/2,centres[0]+width/2),
             ("slot_depth_mm","槽深","y",height-depth if edge else z,height if edge else depth)]
    specs += [(f"slot_{i+1}_position_mm",f"第{i+1}槽中心距","x",z,c) for i,c in enumerate(centres)]
    entries, annotations = [], []
    for i,(key,label,axis,start,end) in enumerate(specs):
        code = f"D{i+1}"
        entries.append({"id":code,"parameter_key":key,"name":label,"value_mm":plain(end-start),
                        "axis":axis,"start_mm":plain(start),"end_mm":plain(end)})
        lane = i+1
        if axis == "x":
            coords = [start,height+gap*lane,end,height+gap*lane]
            tx,ty = (start+end)/2,height+gap*lane-gap/8
        else:
            coords = [-gap*lane,start,-gap*lane,end]
            tx,ty = -gap*lane-gap/8,(start+end)/2
        mark = dict(zip(("x1","y1","x2","y2"),map(plain,coords)))
        mark.update(label=code,tx=plain(tx),ty=plain(ty),tick_mm=plain(gap/10))
        annotations.append(mark)
    panel = {"id":"partition","x":"0","y":"0","width":plain(length),"height":plain(height),
             "parameter_keys":["length_mm","height_mm"],"points_mm":[[plain(x),plain(y)] for x,y in points]}
    # Each slot's three physical edges select that slot's independent position.
    for i,line in enumerate(cuts):
        line["id"] = f"cut-{i+1}"
        line["parameter_key"] = f"slot_{(i-1)//4+1}_position_mm" if 1 <= i <= int(count)*4 and i%4 else "length_mm"
    return {"template":"partition_v1","unit":"mm","width_mm":plain(length),"height_mm":plain(height),
            "cut":cuts,"score":[],"panels":[panel],"fold_panels":[panel],"dimensions":dimensions,
            "dimension_index":entries,"annotations":annotations,"annotation_legends":[],
            "annotation_font_mm":plain(gap/3),"annotation_margin_mm":plain(gap*5),
            "view_bounds":{"x":plain(-gap*5),"y":plain(-gap),"width":plain(length+gap*6),
                           "height":plain(height+gap*(len(specs)+2))}}
