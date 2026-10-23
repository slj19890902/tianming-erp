from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from tests.test_master_data_versioning_p4_writers import writer_app
from tests.test_product_unknown_structure import seed, body, save, URL


@pytest.mark.parametrize('price', ['5.8506', '2.9988', '2.3716', '1.234567'])
def test_master_sale_price_save_readback_keeps_source_precision(writer_app, price):
    from app.models.product import Product
    from app.models.order import OrderItem
    pid, _, oid = seed(writer_app, known=True)
    with TestClient(writer_app) as client:
        updated, _ = save(client, pid, body(client, pid, sale_unit_price=price))
        assert Decimal(str(updated['sale_unit_price'])) == Decimal(price)
        assert Decimal(str(client.get(f'{URL}/{pid}').json()['sale_unit_price'])) == Decimal(price)
    with writer_app.state.session_factory() as db:
        assert db.get(Product, pid).sale_unit_price == Decimal(price)
        assert db.get(OrderItem, oid).unit_price == Decimal('3')


def test_master_sale_price_excess_precision_is_rejected_not_rounded(writer_app):
    from app.models.product import Product
    pid, _, _ = seed(writer_app, known=True)
    with TestClient(writer_app) as client:
        result = client.post(f'{URL}/{pid}/update-preview', json=body(client, pid, sale_unit_price='1.1234567'))
        assert result.status_code == 422, result.text
    with writer_app.state.session_factory() as db:
        assert db.get(Product, pid).sale_unit_price == Decimal('3')
