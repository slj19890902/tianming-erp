from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.drawing_design import (DesignWrite, WorkbenchPreviewWrite, get_design,
                                    save_design, workbench_preview)
from app.models import Base
from app.models.customer import Customer
from app.models.product import Product
from app.models.user import User


def test_unsaved_preview_and_editor_state_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv('ERP_FILE_STORAGE_DIR', str(tmp_path / 'files'))
    engine = create_engine(f'sqlite:///{tmp_path / "workbench.sqlite3"}')
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            customer = Customer(name='工作台隔离客户')
            user = User(username='workbench-user', password_hash='x', role='admin', real_name='UAT')
            db.add_all([customer, user])
            db.flush()
            product = Product(customer_id=customer.id, product_code='WB-1', customer_material_code='WB-1',
                              product_name='隔离衬板', length_mm=Decimal('100'), width_mm=Decimal('50'),
                              height_mm=Decimal('3'), flute_type='B', version=1)
            db.add(product)
            db.commit()
            preview = workbench_preview(product.id, WorkbenchPreviewWrite(template_key='liner_v1', parameters={},
                                        editor_state={'dimension_basis': 'inner'}), db, user)
            assert preview['svg'].startswith('<svg')
            assert preview['geometry']['width_mm'] == '50'
            saved = save_design(product.id, DesignWrite(expected_product_version=1, template_key='liner_v1',
                                parameters={}, editor_state={'dimension_basis': 'inner', 'local_overrides': {}},
                                idempotency_key='workbench-save-0001'), db, user)
            assert saved['draft']['editor_state']['dimension_basis'] == 'inner'
            reloaded = get_design(product.id, db, user)
            assert reloaded['draft']['parameters'] == {}
            assert reloaded['draft']['editor_state']['schema_version'] == 'drawing-workbench-v1'
    finally:
        engine.dispose()
