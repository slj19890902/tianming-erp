"""Resolve the customer-level interpretation of stored sales prices.

The selected mode never converts a stored common-box or order price.  It only
states whether that frozen numeric value already includes tax.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.invoice_task import CustomerInvoiceProfile


DEFAULT_TAX_RATE = Decimal("0.13")
VALID_PRICE_TAX_MODES = frozenset({"tax_inclusive", "tax_exclusive"})


@dataclass(frozen=True)
class CustomerPriceTaxTerms:
    price_tax_mode: str
    tax_rate: Decimal
    profile_id: int | None
    profile_version: int | None


def resolve_customer_price_tax_terms(
    db: Session,
    customer_id: int,
) -> CustomerPriceTaxTerms:
    """Return explicit profile terms, defaulting safely without name guessing."""

    profile = db.scalar(
        select(CustomerInvoiceProfile).where(
            CustomerInvoiceProfile.customer_id == customer_id,
            CustomerInvoiceProfile.is_enabled.is_(True),
        )
    )
    customer = db.get(Customer, customer_id)
    mode = (
        profile.price_tax_mode
        if profile is not None and profile.price_tax_mode in VALID_PRICE_TAX_MODES
        else "tax_inclusive"
    )
    raw_rate = (
        profile.default_tax_rate
        if profile is not None and profile.default_tax_rate is not None
        else (customer.default_tax_rate if customer is not None else DEFAULT_TAX_RATE)
    )
    tax_rate = Decimal(str(raw_rate if raw_rate is not None else DEFAULT_TAX_RATE))
    if tax_rate < 0 or tax_rate > 1:
        tax_rate = DEFAULT_TAX_RATE
    return CustomerPriceTaxTerms(
        price_tax_mode=mode,
        tax_rate=tax_rate,
        profile_id=profile.id if profile is not None else None,
        profile_version=profile.version if profile is not None else None,
    )
