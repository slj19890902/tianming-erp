"""An existing physical mold can retain a label after its SKU is discontinued."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from app.models.product import Product
from app.models.mold_tool import MoldTool, MoldToolCustomer, MoldLabelPrintJob
from app.core.time_contract import utc_now_naive
from tests.test_mold_tool_workflow import _login
from tests.test_p1_103_mold_label_layout import _complete_named_mold, mold_app


@pytest.mark.parametrize('state', ['inactive_product', 'deleted_product', 'inactive_mold', 'unlinked_customer', 'legacy_identity'])
def test_physical_mold_label_retains_discontinued_sku_without_relaxing_asset_gates(mold_app, state):
    app, factory = mold_app
    mold_id = _complete_named_mold(factory, label_name='80011929', chinese_short_name='APS')
    with factory() as db:
        product = db.scalar(select(Product).where(Product.mold_tool_id == mold_id))
        product.is_active = False
        if state == 'deleted_product':
            product.deleted_at = utc_now_naive()
        if state == 'inactive_mold':
            db.get(MoldTool, mold_id).is_active = False
        if state == 'legacy_identity':
            mold = db.get(MoldTool, mold_id)
            mold.identity_status = 'legacy_unset'
            mold.label_name = None
            mold.chinese_short_name = None
        if state != 'unlinked_customer':
            db.add(MoldToolCustomer(mold_tool_id=mold_id, customer_id=product.customer_id, display_order=1))
        db.commit()
        product_id, product_version = product.id, product.version
    with TestClient(app) as client:
        _login(client, 'admin')
        compact = client.get(f'/api/warehouse/molds/{mold_id}/label')
        payload = dict(mold_ids=[mold_id], source='batch', template_version='mold_80x40_v1',
                       idempotency_key='inactive-physical-mold-'+state)
        registered = client.post('/api/warehouse/molds/label-prints', json=payload)
        if state != 'inactive_product':
            assert compact.status_code == 409, compact.text
            assert registered.status_code == 409, registered.text
            with factory() as db:
                assert db.scalar(select(func.count()).select_from(MoldLabelPrintJob)) == 0
            return
        assert compact.status_code == 200, compact.text
        assert compact.json()['label_mold_name'] == '80011929'
        assert registered.status_code == 200, registered.text
        replay = client.post('/api/warehouse/molds/label-prints', json=payload)
        assert replay.status_code == 200 and replay.json()['replayed']
        printed = client.get('/api/warehouse/molds/labels', params=dict(mold_ids=str(mold_id),
            print_job_id=registered.json()['print_job_id'], template_version='mold_80x40_v1'))
        assert printed.status_code == 200, printed.text
        assert printed.json()['items'][0]['label_mold_name'] == '80011929'
    with factory() as db:
        product = db.get(Product, product_id)
        assert not product.is_active and product.version == product_version
        assert db.scalar(select(func.count()).select_from(MoldLabelPrintJob)) == 1
