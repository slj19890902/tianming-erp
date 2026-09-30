"""Complete isolated HTTP workflow for partitions and composite drawings."""
from decimal import Decimal

from fastapi.testclient import TestClient

from app.models.product import Product
from app.models.product_bom import ProductBomComponent
from tests.test_drawing_workbench_http import _app, _fixture


def _save(client, pid, template, parameters, *, version=None, state=None, key='complete-save-001', number=None):
    payload = {'expected_product_version': 1, 'template_key': template, 'parameters': parameters,
               'idempotency_key': key}
    if template != 'assembly_v1':
        payload.update(thickness_mm='3', thickness_source='隔离UAT')
    if version is not None: payload['expected_design_version'] = version
    if state is not None: payload['editor_state'] = state
    if number is not None: payload['customer_number'] = number
    response = client.put(f'/api/master/products/{pid}/managed-drawing', json=payload)
    assert response.status_code == 200, response.text
    return response.json()['draft']['version']


def _publish(client, pid, version, key):
    response = client.post(f'/api/master/products/{pid}/managed-drawing/releases', json={
        'expected_product_version': 1, 'expected_design_version': version, 'idempotency_key': key})
    assert response.status_code == 200, response.text
    return response.json()['id']


def test_partition_and_composite_http_workflow(tmp_path, monkeypatch):
    monkeypatch.setenv('ERP_FILE_STORAGE_DIR', str(tmp_path / 'files'))
    engine, factory, ids = _fixture(tmp_path)
    with factory() as db:
        root = db.get(Product, ids['one']); root.box_style = '模切内盒'
        inner = Product(customer_id=root.customer_id, product_code='ASM', customer_material_code='ASM',
                        product_name='组合内衬', box_style='BOM组合', version=1, length_mm=None, width_mm=None)
        long = Product(customer_id=root.customer_id, product_code='LONG', customer_material_code='LONG',
                       product_name='长隔板', box_style='隔板', version=1, length_mm=Decimal('290'), width_mm=Decimal('233'))
        short = Product(customer_id=root.customer_id, product_code='SHORT', customer_material_code='SHORT',
                        product_name='短隔板', box_style='隔板', version=1, length_mm=Decimal('358'), width_mm=Decimal('233'))
        db.add_all([inner, long, short]); db.flush()
        for parent, child, qty, order in ((root, inner, 1, 1), (inner, long, 2, 1), (inner, short, 6, 2)):
            db.add(ProductBomComponent(parent_product_id=parent.id, component_product_id=child.id,
                   quantity_per_set=qty, display_order=order, internal_component_code=f'c{child.id}', is_die_cut=False))
        db.commit(); ids.update(inner=inner.id, long=long.id, short=short.id)
    actor = [ids['admin']]
    params = {'length_mm': '290', 'height_mm': '233', 'slot_count': 2, 'slot_width_mm': '6',
              'slot_depth_mm': '110', 'slot_pitch_mm': '100', 'slot_offset_mm': '70', 'slot_edge': 0}
    try:
        with TestClient(_app(factory, actor)) as client:
            # Workbench dimensions survive product facts and a legacy partial edit.
            first = _save(client, ids['long'], 'partition_v1', params, state={'dimension_basis': 'dieline'}, key='complete-long-001')
            assert client.get(f'/api/master/products/{ids["long"]}/managed-drawing').json()['draft']['parameters']['length_mm'] == '290'
            assert _save(client, ids['long'], 'partition_v1', {'slot_1_position_mm': '80'}, version=first, key='complete-long-legacy-002') == 2
            saved = client.get(f"/api/master/products/{ids['long']}/managed-drawing").json()['draft']
            assert saved['parameters']['length_mm'] == '290' and saved['editor_state']['dimension_basis'] == 'dieline'
            assert client.post(f'/api/master/products/{ids["long"]}/managed-drawing/workbench-preview', json={
                'template_key': 'partition_v1', 'parameters': {**params, 'slot_1_position_mm': '2'}}).status_code == 422
            assert client.post(f'/api/master/products/{ids["long"]}/managed-drawing/workbench-preview', json={
                'template_key': 'partition_v1', 'parameters': {**params, 'slot_1_position_mm': '70', 'slot_2_position_mm': '74'}}).status_code == 422
            long_release = _publish(client, ids['long'], 2, 'complete-long-release-003')
            short_release = _publish(client, ids['short'], _save(client, ids['short'], 'partition_v1', {**params, 'length_mm': '358'}, state={'dimension_basis': 'dieline'}, key='complete-short-001'), 'complete-short-release-002')
            context = client.get(f'/api/master/products/{ids["inner"]}/managed-drawing/workbench-context').json()['component_context']
            node_release = {ids['long']: long_release, ids['short']: short_release}
            placements = []
            for item in context['instances']:
                if item['product_id'] not in node_release: continue
                for index in range(item['instance_count']):
                    placements.append({'path': item['path'], 'instance_index': index, 'child_release_id': node_release[item['product_id']],
                                       'position_mm': [index, 0, 0], 'rotation_deg': [0, 0, 0]})
            state = {'dimension_basis': 'dieline', 'assembly': {'basis_hash': context['basis_hash'], 'placements': placements}}
            assembly_release = _publish(client, ids['inner'], _save(client, ids['inner'], 'assembly_v1', {}, state=state, key='complete-asm-001'), 'complete-asm-release-002')
            # Parent has a body, so it keeps liner geometry and references the frozen assembly release.
            root_context = client.get(f'/api/master/products/{ids["one"]}/managed-drawing/workbench-context').json()['component_context']
            root_state = {'dimension_basis': 'dieline', 'assembly': {'basis_hash': root_context['basis_hash'], 'placements': [
                {'path': root_context['instances'][0]['path'], 'instance_index': 0, 'child_release_id': assembly_release,
                 'position_mm': [0, 0, 0], 'rotation_deg': [0, 0, 0]}]}}
            root_release = _publish(client, ids['one'], _save(client, ids['one'], 'liner_v1', {'length_mm':'310','width_mm':'200'}, state=root_state, key='complete-root-cn-001', number='图号中文'), 'complete-root-release-002')
            assert client.get(f'/api/master/products/{ids["one"]}/managed-drawing/releases/{root_release}/export', params={'format':'dxf'}).status_code == 200
            assert client.get(f'/api/master/products/{ids["inner"]}/managed-drawing/releases/{assembly_release}/export', params={'format':'dxf'}).status_code == 422
            assert client.get(f'/api/master/products/{ids["inner"]}/managed-drawing/releases/{assembly_release}/export', params={'format':'pdf_1to1'}).status_code == 422
    finally:
        engine.dispose()
