"""Reusable explicit profile; customer IDs remain in the existing access editor."""
from app.api.deps import PERMISSION_CATALOG

PERMISSIONS = frozenset({
    "customers.view", "products.view", "orders.view", "orders.create", "dashboard.view",
    "requisition.view", "incoming.view", "warehouse.view", "deliveries.view", "finance.view",
    "production.printing.view", "production.die_cut.view", "finance.invoice_attachment.view",
    "business_requests.submit", "warehouse.alerts.edit",
})

def overrides():
    return {code:code in PERMISSIONS for code in PERMISSION_CATALOG}
