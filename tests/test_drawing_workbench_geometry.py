from app.services.drawing_geometry import build_geometry
from app.services.drawing_workbench import fold_model, preview_payload, template_catalog


def _area(panels):
    return sum(float(panel['width']) * float(panel['height']) for panel in panels)


def _edge_contains(panel, x1, y1, x2, y2):
    x, y, width, height = (float(panel[key]) for key in ('x', 'y', 'width', 'height'))
    return ((x1 == x2 and x <= x1 <= x + width and y <= min(y1, y2) and max(y1, y2) <= y + height)
            or (y1 == y2 and y <= y1 <= y + height and x <= min(x1, x2) and max(x1, x2) <= x + width))


def test_preview_exposes_mm_dimension_bindings_and_hinges():
    payload = preview_payload('slotted_v1', {
        'panel_1_mm': '200', 'panel_2_mm': '100', 'panel_3_mm': '200', 'panel_4_mm': '100',
        'body_height_mm': '120', 'top_flap_mm': '50', 'bottom_flap_mm': '50',
        'glue_flap_mm': '30', 'slot_width_mm': '8',
    }, {'dimension_basis': 'inner'})
    assert payload['unit'] == 'mm'
    assert payload['dimension_basis']['key'] == 'inner'
    assert payload['validation']['valid']
    assert len(payload['panel_hinges']) == 12
    assert all(item['parent_hinge_axis']['x1_mm'] for item in payload['panel_hinges'])
    assert any(item['key'] == 'slot_width_mm' for item in payload['editable_dimensions'])
    assert all(panel['parameter_keys'] for panel in payload['geometry']['fold_panels'])


def test_fold_model_only_uses_same_geometry_panels():
    geometry = build_geometry('custom_21301634_v1', {
        'panel_width_mm': '200', 'panel_height_mm': '100', 'top_cover_mm': '50', 'bottom_cover_mm': '50',
        'top_fold_mm': '20', 'bottom_fold_mm': '20', 'left_fold_mm': '30', 'right_fold_mm': '30',
        'left_wing_mm': '25', 'right_wing_mm': '25',
    })
    hinges = fold_model('custom_21301634_v1', geometry)
    ids = {panel['id'] for panel in geometry['panels']}
    fold_ids = {panel['id'] for panel in geometry['fold_panels']}
    assert len(fold_ids) == 9
    assert all(item['parent_panel_id'] in fold_ids and item['child_panel_id'] in fold_ids for item in hinges)
    assert _area(geometry['fold_panels']) == 59000
    panels = {panel['id']: panel for panel in geometry['fold_panels']}
    for hinge in hinges:
        axis = hinge['parent_hinge_axis']
        coords = tuple(float(axis[key]) for key in ('x1_mm', 'y1_mm', 'x2_mm', 'y2_mm'))
        assert _edge_contains(panels[hinge['parent_panel_id']], *coords)
        assert _edge_contains(panels[hinge['child_panel_id']], *coords)
    assert {item['key'] for item in template_catalog()['dimension_basis_options']} == {'inner', 'outer', 'dieline'}


def test_slotted_fold_panels_cover_the_cut_area_and_share_hinge_edges():
    geometry = build_geometry('slotted_v1', {
        'panel_1_mm': '200', 'panel_2_mm': '100', 'panel_3_mm': '200', 'panel_4_mm': '100',
        'body_height_mm': '120', 'top_flap_mm': '50', 'bottom_flap_mm': '50',
        'glue_flap_mm': '30', 'slot_width_mm': '8',
    })
    # The orthogonal cut outline's signed area equals the union of all foldable
    # paper panels; slots are excluded from the flap rectangles.
    points = [(float(line['x1']), float(line['y1'])) for line in geometry['cut']]
    points.append(points[0])
    cut_area = abs(sum(a[0] * b[1] - a[1] * b[0] for a, b in zip(points, points[1:]))) / 2
    assert _area(geometry['fold_panels']) == cut_area
    panels = {panel['id']: panel for panel in geometry['fold_panels']}
    hinges = fold_model('slotted_v1', geometry)
    assert len(panels) == 13
    assert all(item['max_angle_deg'] == 90 and item['direction'] == 1
               for item in hinges if item['id'].startswith('hinge-wall-') and '-top' not in item['id'] and '-bottom' not in item['id'])
    for hinge in hinges:
        axis = hinge['parent_hinge_axis']
        coords = tuple(float(axis[key]) for key in ('x1_mm', 'y1_mm', 'x2_mm', 'y2_mm'))
        assert _edge_contains(panels[hinge['parent_panel_id']], *coords)
        assert _edge_contains(panels[hinge['child_panel_id']], *coords)
