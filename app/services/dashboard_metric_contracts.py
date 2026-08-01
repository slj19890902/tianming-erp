from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping, Sequence


MONEY = Decimal("0.01")


@dataclass(frozen=True)
class DashboardMetricDefinition:
    key: str
    title: str
    count_unit: str
    authoritative_source: str
    identity_key: str
    permission: str
    target: str
    amount_formula: str | None = None


METRIC_DEFINITIONS: tuple[DashboardMetricDefinition, ...] = (
    DashboardMetricDefinition(
        key="pending_material",
        title="待报料项",
        count_unit="业务项",
        authoritative_source="/api/requisition/pending",
        identity_key="合并报料组 ID；普通明细使用订单明细 ID",
        permission="requisition.view",
        target="requisition",
    ),
    DashboardMetricDefinition(
        key="pending_incoming",
        title="待入库明细",
        count_unit="来料明细",
        authoritative_source="/api/incoming/pending",
        identity_key="补库明细 ID；普通来料使用报料明细 ID 或订单明细 ID",
        permission="incoming.view",
        target="incoming",
    ),
    DashboardMetricDefinition(
        key="pending_production",
        title="待生产项",
        count_unit="生产任务",
        authoritative_source="/api/production/tasks?status=pending",
        identity_key="生产任务 ID",
        permission="orders.view",
        target="production",
    ),
    DashboardMetricDefinition(
        key="pending_delivery",
        title="待送货客户",
        count_unit="客户",
        authoritative_source="/api/deliveries/pending_items",
        identity_key="customer_id",
        permission="deliveries.view",
        target="deliveries",
    ),
    DashboardMetricDefinition(
        key="pending_receipt",
        title="待回单客户",
        count_unit="客户",
        authoritative_source="已正式送货且尚无有效回单的送货单集合",
        identity_key="customer_id",
        permission="deliveries.view",
        target="deliveries",
    ),
    DashboardMetricDefinition(
        key="pending_reconciliation",
        title="本月待对账客户",
        count_unit="客户",
        authoritative_source="按客户结转周期筛选的待对账回单集合",
        identity_key="customer_id + statement_month",
        permission="finance.view",
        target="finance",
        amount_formula="所选周期未进入有效对账单的回单应收金额合计",
    ),
    DashboardMetricDefinition(
        key="pending_invoice",
        title="待开票客户",
        count_unit="客户",
        authoritative_source="finance_statements",
        identity_key="customer_id",
        permission="finance.view",
        target="finance",
        amount_formula="max(total_receivable - invoiced_amount, 0)",
    ),
    DashboardMetricDefinition(
        key="pending_payment",
        title="待结款客户",
        count_unit="客户",
        authoritative_source="finance_statements",
        identity_key="customer_id",
        permission="finance.view",
        target="finance",
        amount_formula="max(total_receivable - settled_amount, 0)",
    ),
)


def metric_definitions() -> list[dict]:
    return [asdict(row) for row in METRIC_DEFINITIONS]


def _positive_int(value: object, *, field: str) -> int:
    try:
        result = int(value or 0)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} 不是有效整数") from error
    if result <= 0:
        raise ValueError(f"{field} 必须大于 0")
    return result


def _identity(metric_key: str, row: Mapping[str, object]) -> str:
    if metric_key == "pending_material":
        if bool(row.get("is_merge_group")):
            value = row.get("merge_group_id") or row.get("requisition_id")
            return f"requisition-group:{_positive_int(value, field='merge_group_id')}"
        value = row.get("item_id") or row.get("order_item_id")
        return f"order-item:{_positive_int(value, field='order_item_id')}"

    if metric_key == "pending_incoming":
        if row.get("stock_replenishment_item_id"):
            return (
                "stock-replenishment-item:"
                f"{_positive_int(row['stock_replenishment_item_id'], field='stock_replenishment_item_id')}"
            )
        if row.get("requisition_item_id"):
            return (
                "requisition-item:"
                f"{_positive_int(row['requisition_item_id'], field='requisition_item_id')}"
            )
        value = row.get("order_item_id") or row.get("item_id")
        return f"order-item:{_positive_int(value, field='order_item_id')}"

    if metric_key == "pending_production":
        return f"production-task:{_positive_int(row.get('id'), field='production_task_id')}"

    if metric_key in {
        "pending_delivery",
        "pending_receipt",
        "pending_invoice",
        "pending_payment",
    }:
        return f"customer:{_positive_int(row.get('customer_id'), field='customer_id')}"

    if metric_key == "pending_reconciliation":
        customer_id = _positive_int(row.get("customer_id"), field="customer_id")
        month = str(row.get("statement_month") or row.get("month") or "").strip()
        if len(month) != 7 or month[4] != "-":
            raise ValueError("statement_month 必须为 YYYY-MM")
        return f"customer:{customer_id}@{month}"

    raise ValueError(f"未知首页指标：{metric_key}")


def stable_identities(
    metric_key: str,
    rows: Iterable[Mapping[str, object]],
) -> list[str]:
    return sorted({_identity(metric_key, row) for row in rows})


def _money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(MONEY, rounding=ROUND_HALF_UP)


def financial_balance_rows(
    statements: Iterable[Mapping[str, object]],
) -> tuple[list[dict], list[dict]]:
    invoice_by_customer: dict[int, Decimal] = {}
    payment_by_customer: dict[int, Decimal] = {}
    for row in statements:
        customer_id = _positive_int(row.get("customer_id"), field="customer_id")
        receivable = _money(row.get("total_receivable"))
        invoiced = _money(row.get("invoiced_amount"))
        settled = _money(row.get("settled_amount"))
        invoice_balance = max(receivable - invoiced, Decimal("0.00"))
        payment_balance = max(receivable - settled, Decimal("0.00"))
        if invoice_balance > 0:
            invoice_by_customer[customer_id] = _money(
                invoice_by_customer.get(customer_id, Decimal("0.00"))
                + invoice_balance
            )
        if payment_balance > 0:
            payment_by_customer[customer_id] = _money(
                payment_by_customer.get(customer_id, Decimal("0.00"))
                + payment_balance
            )
    invoice_rows = [
        {"customer_id": customer_id, "amount": amount}
        for customer_id, amount in sorted(invoice_by_customer.items())
    ]
    payment_rows = [
        {"customer_id": customer_id, "amount": amount}
        for customer_id, amount in sorted(payment_by_customer.items())
    ]
    return invoice_rows, payment_rows


def build_authoritative_snapshot(
    *,
    pending_material_rows: Sequence[Mapping[str, object]],
    pending_incoming_rows: Sequence[Mapping[str, object]],
    pending_production_rows: Sequence[Mapping[str, object]],
    pending_delivery_rows: Sequence[Mapping[str, object]],
    pending_receipt_rows: Sequence[Mapping[str, object]],
    pending_reconciliation_rows: Sequence[Mapping[str, object]],
    statement_rows: Sequence[Mapping[str, object]],
    statement_month: str,
    as_of: str,
) -> dict:
    invoice_rows, payment_rows = financial_balance_rows(statement_rows)
    rows_by_metric = {
        "pending_material": pending_material_rows,
        "pending_incoming": pending_incoming_rows,
        "pending_production": pending_production_rows,
        "pending_delivery": pending_delivery_rows,
        "pending_receipt": pending_receipt_rows,
        "pending_reconciliation": pending_reconciliation_rows,
        "pending_invoice": invoice_rows,
        "pending_payment": payment_rows,
    }
    definitions = {row.key: row for row in METRIC_DEFINITIONS}
    metrics: dict[str, dict] = {}
    for key, rows in rows_by_metric.items():
        identities = stable_identities(key, rows)
        amount = None
        if key in {"pending_reconciliation", "pending_invoice", "pending_payment"}:
            amount = _money(sum((_money(row.get("amount")) for row in rows), Decimal("0.00")))
        metrics[key] = {
            **asdict(definitions[key]),
            "count": len(identities),
            "identities": identities,
            "amount": str(amount) if amount is not None else None,
        }
    return {
        "timezone": "Asia/Shanghai",
        "as_of": as_of,
        "statement_month": statement_month,
        "metrics": metrics,
    }
