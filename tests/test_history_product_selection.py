from datetime import date
from decimal import Decimal
import subprocess
import shutil
from pathlib import Path

import pytest
from fastapi import HTTPException
from tests.test_p1_09c_product_query_scaling import _fixture


def test_history_products_use_current_eligibility_and_scope(tmp_path):
    from sqlalchemy import select
    from app.models.product import Product
    from app.models.order import Order, OrderItem
    from app.models.user import User
    from app.api.products import list_products
    engine, factory, user_id = _fixture(tmp_path, visible_count=3)
    try:
        with factory() as db:
            user = db.get(User, user_id)
            products = db.scalars(select(Product).where(Product.product_code != 'P1-09C-P-HIDDEN').order_by(Product.id)).all()
            hidden = db.scalar(select(Product).where(Product.product_code == 'P1-09C-P-HIDDEN'))
            order = Order(customer_id=products[0].customer_id, order_number='SYNTHETIC-HISTORY',order_date=date(2020,1,1),customer_po='OLD-PO')
            other = Order(customer_id=hidden.customer_id,order_number='SYNTHETIC-HIDDEN',order_date=date(2020,1,1))
            db.add_all([order,other]);db.flush()
            for product in [products[0], products[0], products[1]]:
                db.add(OrderItem(order_id=order.id,product_id=product.id,quantity=99,unit_price=Decimal('88'),
                                 subtotal=Decimal('8712'),snapshot_product_name='OLD NAME'))
            products[1].is_active=False
            products[0].product_name='CURRENT NAME'
            db.commit()
            params=dict(db=db,user=user,selection_context='order',response_mode='summary',page=1,page_size=50)
            data=list_products(source_order_id=order.id,customer_id=order.customer_id,**params)
            assert [p['id'] for p in data['items']]==[products[0].id]
            assert data['items'][0]['product_name']=='CURRENT NAME'
            assert 'quantity' not in data['items'][0]
            for kwargs in [dict(source_order_id=other.id),dict(source_order_id=order.id,customer_id=hidden.customer_id)]:
                with pytest.raises(HTTPException) as exc:list_products(**params,**kwargs)
                assert exc.value.status_code in (400,403)
            # Independently enforce the order-view boundary, rather than relying on customer scope.
            from unittest.mock import patch
            with patch('app.api.products.has_permission',return_value=False):
                with pytest.raises(HTTPException) as exc:list_products(**params,source_order_id=order.id)
                assert exc.value.status_code==403
    finally:engine.dispose()


def test_history_product_frontend_boundaries():
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run([shutil.which('node'),str(root/'tests/history_product_selection.cjs'),str(root)],
                          capture_output=True,text=True,encoding='utf-8',errors='replace')
    assert result.returncode==0,result.stdout+result.stderr
