"""One default cycle for receipt entry; existing explicit months stay frozen."""
from sqlalchemy import select
from app.models.customer import Customer
from app.models.invoice_task import CustomerInvoiceProfile, FinanceSettlementEntity


def cycle_days_for_customers(db, customers):
    days = {key: int(customer.statement_cycle_start_day or 1) for key, customer in customers.items()}
    if not days:
        return days
    entity_days = db.execute(select(CustomerInvoiceProfile.customer_id, FinanceSettlementEntity.statement_cycle_start_day).join(
        CustomerInvoiceProfile, CustomerInvoiceProfile.settlement_entity_id == FinanceSettlementEntity.id).where(
        CustomerInvoiceProfile.customer_id.in_(days),
        CustomerInvoiceProfile.is_enabled.is_(True), CustomerInvoiceProfile.confirmation_status == 'confirmed',
        FinanceSettlementEntity.is_enabled.is_(True), FinanceSettlementEntity.confirmation_status == 'confirmed'))
    days.update({customer_id: int(day or 1) for customer_id, day in entity_days})
    return days


def cycle_day_for_customer(db, customer_id):
    customer = db.get(Customer, customer_id)
    return cycle_days_for_customers(db, {customer_id: customer}).get(customer_id, 1) if customer else 1


def month_for_date(value, cycle_day):
    if cycle_day == 1 or value.day < cycle_day:
        return value.strftime('%Y-%m')
    year, month_zero = divmod(value.year * 12 + value.month, 12)
    return f'{year:04d}-{month_zero + 1:02d}'


def default_receipt_month(db, delivery):
    return month_for_date(delivery.delivery_date, cycle_day_for_customer(db, delivery.customer_id))
