"""End-to-end vector coordinates, historical snapshots and output scale."""
from copy import deepcopy
import io
import json
import subprocess

import fitz
import pytest

from app.services.drawing_exports import engineering_pdf, engineering_pdf_1to1
from app.services.drawing_geometry import DrawingGeometryError, build_geometry
from app.services.drawing_workbench import preview_payload, fold_model, same_manufacturing_geometry, with_frozen_fold_panels


A1 = dict(panel_1_mm=200,panel_2_mm=100,panel_3_mm=200,panel_4_mm=100,body_height_mm=120,
          top_flap_mm=50,bottom_flap_mm=50,glue_flap_mm=30,slot_width_mm=6)
CUSTOM = dict(panel_width_mm=200,panel_height_mm=100,top_cover_mm=50,bottom_cover_mm=50,
              top_fold_mm=20,bottom_fold_mm=20,left_fold_mm=30,right_fold_mm=30,left_wing_mm=25,right_wing_mm=25)
PARTITION = dict(length_mm=290,height_mm=233,slot_count=2,slot_width_mm=6,slot_depth_mm=110,
                 slot_pitch_mm=100,slot_offset_mm=70,slot_edge=0)


def folded(template, params):
    geometry=build_geometry(template,params)
    script="const fs=require('fs'),w=require('./static/drawing-workbench.js'),d=JSON.parse(fs.readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(w.foldPanels(d.geometry,d.hinges,1)));"
    return json.loads(subprocess.check_output(['node','-e',script],input=json.dumps(
        {'geometry':geometry,'hinges':fold_model(template,geometry)}).encode()))


def test_fully_folded_a1_has_closed_walls_and_flaps_inside_not_outside():
    panels=folded('slotted_v1',A1)
    for panel in panels:
        for x,y,z in panel['points']:
            assert -1e-7 <= x <= 200+1e-7, (panel['id'],x)
            assert 50-1e-7 <= y <= 170+1e-7, (panel['id'],y)
            assert -100-1e-7 <= z <= 1e-7, (panel['id'],z)
    walls={p['id']:p for p in panels if p['id'].startswith('wall_')}
    assert walls['wall_1']['points'][0] == pytest.approx(walls['wall_4']['points'][1])


def test_custom_all_walls_fold_towards_same_side_and_legacy_uses_frozen_dimensions():
    assert min(p[2] for panel in folded('custom_21301634_v1',CUSTOM) for p in panel['points']) >= -1e-7
    geometry=build_geometry('custom_21301634_v1',CUSTOM)
    legacy=deepcopy(geometry);legacy.pop('fold_panels')
    original=deepcopy(legacy)
    enriched=with_frozen_fold_panels('custom_21301634_v1',legacy)
    assert enriched['fold_panels'] == geometry['fold_panels']
    assert same_manufacturing_geometry(legacy,geometry)
    assert legacy == original
    changed=deepcopy(geometry);changed['width_mm']='999'
    assert not same_manufacturing_geometry(legacy,changed)


def test_dimension_selection_maps_to_real_parameters_and_individual_slots():
    for key,params in [('liner_v1',dict(length_mm=310,width_mm=200)),('slotted_v1',A1),('custom_21301634_v1',CUSTOM),('partition_v1',PARTITION)]:
        payload=preview_payload(key,params)
        bindings={ref for item in payload['editable_dimensions'] for ref in item['geometry_refs']['dimension_ids']}
        assert bindings == {d['id'] for d in payload['geometry']['dimension_index']}
    changed=build_geometry('partition_v1',{**PARTITION,'slot_1_position_mm':80})
    assert changed['dimensions']['默认首槽中心距']=='70'
    assert changed['dimensions']['第1槽中心距']=='80'
    assert changed['cut'][1]['x1']=='77'
    points=[(float(l['x1']),float(l['y1'])) for l in changed['cut']]
    area=abs(sum(a[0]*b[1]-a[1]*b[0] for a,b in zip(points,points[1:]+points[:1])))/2
    assert area == 290*233-2*6*110
    bottom=build_geometry('partition_v1',{**PARTITION,'slot_edge':1})
    assert bottom['cut'][1]['y1']=='233' and bottom['cut'][1]['y2']=='123'
    for overrides in ({'slot_depth_mm':233},{'slot_count':1.5},{'slot_2_position_mm':72},{'slot_offset_mm':1}):
        with pytest.raises(DrawingGeometryError): build_geometry('partition_v1',{**PARTITION,**overrides})


def test_actual_pdf_coordinates_and_pure_assembly_has_no_fake_size_pages():
    geometry=build_geometry('liner_v1',dict(length_mm=100,width_mm=50))
    doc=fitz.open(stream=engineering_pdf_1to1(geometry),filetype='pdf')
    # The first four vector paths are cuts; the remaining paths form the ruler.
    cuts=doc[0].get_drawings()[:4]
    rect=fitz.Rect(min(c['rect'].x0 for c in cuts),min(c['rect'].y0 for c in cuts),
                   max(c['rect'].x1 for c in cuts),max(c['rect'].y1 for c in cuts))
    assert rect.width*25.4/72 == pytest.approx(50,abs=.001)
    assert rect.height*25.4/72 == pytest.approx(100,abs=.001)
    assert rect.x0*25.4/72 == pytest.approx(10,abs=.001)
    assembly=build_geometry('assembly_v1',{})
    content=engineering_pdf(assembly,customer='UAT',product='组合内衬',number='DRAW-1',revision='R01',
        thickness='按子件',print_objects=[],drawing_metadata={'editor_state':{'assembly':{'placements':[
            {'path':'1','product_name':'长隔板','child_release_id':2,'position_mm':[0,0,0],'rotation_deg':[0,90,0]}]}}})
    pdf=fitz.open(stream=content,filetype='pdf')
    assert len(pdf)==1
    assert '1 × 1' not in pdf[0].get_text()
