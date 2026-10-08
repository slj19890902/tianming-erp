from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.core.database import create_sqlite_engine
from app.models import Base
from app.models.customer import Customer
from app.models.delivery import Delivery
from app.models.tianhua_pre_delivery import TianhuaPreDeliveryImportBatch, TianhuaPreDeliveryDraft
from app.api.deliveries import _pre_delivery_batch_reference


@pytest.fixture
def facts(tmp_path):
    engine = create_sqlite_engine(tmp_path / 'fictional-recovery.sqlite3')
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        customers = [Customer(customer_number=925+i, customer_code=f'R25-{i}', name=f'虚构R25客户{i}', credit_limit=Decimal('0')) for i in range(2)]
        db.add_all(customers); db.flush()
        deliveries = [Delivery(delivery_number=f'R25-DELIVERY-{i}', customer_id=customer.id, delivery_date=date(2026,10,7), status='pending', total_quantity=0) for i, customer in enumerate(customers)]
        db.add_all(deliveries); db.flush()
        batch = TianhuaPreDeliveryImportBatch(batch_number='R25-BATCH', filename='fictional.xlsx', customer_id=customers[0].id, customer_name=customers[0].name, status='draft_created', source_type='excel_upload')
        db.add(batch); db.flush()
        draft = TianhuaPreDeliveryDraft(draft_number='R25-DRAFT', batch_id=batch.id, customer_id=customers[0].id, delivery_id=deliveries[0].id, status='draft')
        db.add(draft); db.commit()
        yield db, customers, deliveries, batch, draft
    engine.dispose()


@pytest.mark.parametrize('source_type', ['excel_upload', 'tianhua_image'])
def test_saved_delivery_exposes_original_batch_header_without_creating_new_facts(facts, source_type):
    db, customers, deliveries, batch, draft = facts
    batch.source_type=source_type; db.commit()
    before = (batch.id, batch.status, draft.id, draft.delivery_id, deliveries[0].version)
    assert _pre_delivery_batch_reference(db, deliveries[0]) == {'batch_id':batch.id, 'batch_number':'R25-BATCH', 'source_type':source_type}
    assert before == (batch.id, batch.status, draft.id, draft.delivery_id, deliveries[0].version)
    assert not db.dirty and not db.new and not db.deleted


def test_ordinary_delivery_has_no_fabricated_batch(facts):
    db, customers, deliveries, batch, draft = facts
    assert _pre_delivery_batch_reference(db, deliveries[1]) is None


@pytest.mark.parametrize('wrong_customer_field', ['batch', 'draft'])
def test_inconsistent_customer_link_does_not_expose_foreign_batch(facts, wrong_customer_field):
    db, customers, deliveries, batch, draft = facts
    target = batch if wrong_customer_field == 'batch' else draft
    target.customer_id = customers[1].id; db.commit()
    assert _pre_delivery_batch_reference(db, deliveries[0]) is None
    assert not db.dirty
