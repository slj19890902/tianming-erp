from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from tests.test_p1_131_cost_pool import _login, p1_131_cost_app


def _enable_router(app) -> None:
    from app.api.finance_simplified import router

    app.include_router(router, prefix="/api/finance")


def _centers(factory) -> dict[str, int]:
    from app.models.finance_cost import FinanceCostCenter

    with factory() as db:
        return {row.code: row.id for row in db.query(FinanceCostCenter).all()}


def _rule_payload(
    *,
    key: str,
    rule_type: str,
    name: str,
    center_id: int,
    **values,
) -> dict:
    return {
        "rule_type": rule_type,
        "name": name,
        "role_name": values.get("role_name"),
        "cost_center_id": center_id,
        "start_month": values.get("start_month", "2026-01"),
        "end_month": values.get("end_month"),
        "base_amount": values.get("base_amount", "0"),
        "allowance_amount": values.get("allowance_amount", "0"),
        "employer_social_amount": values.get("employer_social_amount", "0"),
        "deduction_amount": values.get("deduction_amount", "0"),
        "annual_amount": values.get("annual_amount", "0"),
        "monthly_amount": values.get("monthly_amount", "0"),
        "due_day": values.get("due_day", 20),
        "note": values.get("note"),
        "is_active": values.get("is_active", True),
        "idempotency_key": key,
    }


def test_recurring_wages_rent_and_vehicle_loan_generate_monthly_drafts(
    p1_131_cost_app,
) -> None:
    app, factory = p1_131_cost_app
    _enable_router(app)
    centers = _centers(factory)
    with TestClient(app) as client:
        _login(client)
        employee = client.post(
            "/api/finance/simple-finance/recurring-rules",
            json=_rule_payload(
                key="p1144-rule-employee-001",
                rule_type="employee_wage",
                name="张师傅",
                role_name="车间",
                center_id=centers["PROD"],
                base_amount="5000",
                allowance_amount="300",
                employer_social_amount="700",
                deduction_amount="100",
            ),
        )
        assert employee.status_code == 201, employee.text
        assert employee.json()["cost_category"] == "production_wages"

        rent = client.post(
            "/api/finance/simple-finance/recurring-rules",
            json=_rule_payload(
                key="p1144-rule-rent-001",
                rule_type="annual_rent",
                name="厂房房租",
                center_id=centers["PROD"],
                annual_amount="380000",
            ),
        )
        assert rent.status_code == 201, rent.text
        loan = client.post(
            "/api/finance/simple-finance/recurring-rules",
            json=_rule_payload(
                key="p1144-rule-loan-001",
                rule_type="fixed_monthly",
                name="车辆月供",
                center_id=centers["FINANCE"],
                monthly_amount="8000",
            ),
        )
        assert loan.status_code == 201, loan.text
        wrong_wage_center = client.post(
            "/api/finance/simple-finance/recurring-rules",
            json=_rule_payload(
                key="p1144-rule-wrong-center",
                rule_type="employee_wage",
                name="错误成本中心",
                center_id=centers["FINANCE"],
                base_amount="1000",
            ),
        )
        assert wrong_wage_center.status_code == 409

        generated = client.post(
            "/api/finance/simple-finance/recurring-generate",
            json={"month": "2026-11", "idempotency_key": "p1144-generate-202611"},
        )
        assert generated.status_code == 200, generated.text
        assert generated.json()["created_count"] == 3
        assert generated.json()["active_employee_count"] == 1

        replay = client.post(
            "/api/finance/simple-finance/recurring-generate",
            json={"month": "2026-11", "idempotency_key": "p1144-generate-202611"},
        )
        assert replay.status_code == 200
        assert replay.json() == generated.json()

        summary = client.get(
            "/api/finance/simple-finance/summary", params={"month": "2026-11"}
        )
        assert summary.status_code == 200, summary.text
        groups = summary.json()["primary_groups"]
        assert Decimal(str(groups["personnel"]["draft_amount"])) == Decimal("5900.00")
        assert Decimal(str(groups["fixed_operating"]["draft_amount"])) == Decimal(
            "39666.67"
        )
        assert summary.json()["active_employee_count"] == 1

        december = client.post(
            "/api/finance/simple-finance/recurring-generate",
            json={"month": "2026-12", "idempotency_key": "p1144-generate-202612"},
        )
        assert december.status_code == 200, december.text

    from app.models.finance_cost import FinanceCostPoolEntry

    with factory() as db:
        entries = db.query(FinanceCostPoolEntry).filter_by(cost_month="2026-12").all()
        rent_entry = next(row for row in entries if row.cost_category == "factory_rent")
        assert rent_entry.amount == Decimal("31666.63")
        assert len({row.source_fingerprint for row in entries}) == 3


def test_utility_meter_uses_invoice_fact_and_carries_previous_reading(
    p1_131_cost_app,
) -> None:
    app, factory = p1_131_cost_app
    _enable_router(app)
    centers = _centers(factory)
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/finance/simple-finance/utility-readings",
            json={
                "cost_month": "2026-08",
                "utility_type": "electricity",
                "cost_center_id": centers["PROD"],
                "previous_reading": "100.000",
                "current_reading": "120.000",
                "unit_price": "1.2000",
                "paid_amount": "0",
                "idempotency_key": "p1144-electricity-create",
            },
        )
        assert created.status_code == 200, created.text
        assert Decimal(str(created.json()["calculated_amount"])) == Decimal("24.00")
        assert Decimal(str(created.json()["recognized_amount"])) == Decimal("24.00")

        updated = client.post(
            "/api/finance/simple-finance/utility-readings",
            json={
                "cost_month": "2026-08",
                "utility_type": "electricity",
                "cost_center_id": centers["PROD"],
                "previous_reading": "100.000",
                "current_reading": "120.000",
                "unit_price": "1.2000",
                "invoice_number": "ELEC-202608",
                "invoice_date": "2026-08-31",
                "invoice_amount": "25.00",
                "paid_amount": "25.00",
                "payment_date": "2026-09-05",
                "expected_version": 1,
                "idempotency_key": "p1144-electricity-invoice",
            },
        )
        assert updated.status_code == 200, updated.text
        assert Decimal(str(updated.json()["difference_amount"])) == Decimal("1.00")
        assert updated.json()["version"] == 2

        next_month = client.get(
            "/api/finance/simple-finance/utility-readings", params={"month": "2026-09"}
        )
        assert next_month.status_code == 200
        assert Decimal(
            str(next_month.json()["recommended_previous_readings"]["electricity"])
        ) == Decimal("120.000")

    from app.models.finance_cost import FinanceCostPoolEntry

    with factory() as db:
        entry = db.query(FinanceCostPoolEntry).filter_by(
            source_fingerprint="utility:electricity:2026-08"
        ).one()
        assert entry.amount == Decimal("25.00")
        assert entry.source_type == "utility_reading"


def _seed_payable_statement(factory) -> tuple[int, int]:
    from app.models.customer import Customer
    from app.models.finance_payable import FinancePayable
    from app.models.supplier import Supplier
    from app.models.supplier_settlement import (
        SupplierMonthlyAdjustment,
        SupplierMonthlyInvoice,
        SupplierMonthlyStatement,
    )
    from app.models.user import User

    with factory() as db:
        admin = db.query(User).filter_by(username="p1131-admin").one()
        customer = Customer(
            name="昆山华诚电子有限公司",
            chinese_short_name="华诚",
            customer_code="P1144-CUSTOMER",
            is_active=True,
        )
        supplier = Supplier(
            standard_name="上游纸板厂",
            normalized_name="上游纸板厂",
            display_name="纸板厂",
            is_active=True,
        )
        db.add_all([customer, supplier])
        db.flush()
        payable = FinancePayable(
            supplier_id=supplier.id,
            counterparty_name=supplier.standard_name,
            category="material",
            document_number="SUP-ST-202608",
            document_date=date(2026, 8, 20),
            amount=Decimal("1000.00"),
            status="confirmed",
            idempotency_key="p1144-seeded-payable",
            created_by=admin.id,
        )
        db.add(payable)
        db.flush()
        statement = SupplierMonthlyStatement(
            statement_number="SUP-ST-202608",
            supplier_id=supplier.id,
            supplier_name_snapshot=supplier.standard_name,
            settlement_month="2026-08",
            period_start=date(2026, 7, 21),
            period_end=date(2026, 8, 20),
            currency="CNY",
            tax_basis="tax_inclusive",
            status="invoiced_pending_payment",
            erp_amount=Decimal("980.00"),
            adjustment_amount=Decimal("20.00"),
            adjusted_amount=Decimal("1000.00"),
            supplier_statement_number="BILL-202608",
            supplier_statement_date=date(2026, 8, 21),
            supplier_statement_amount=Decimal("1000.00"),
            confirmed_amount=Decimal("1000.00"),
            invoice_allocated_amount=Decimal("1000.00"),
            paid_amount=Decimal("0.00"),
            finance_payable_id=payable.id,
            generated_by=admin.id,
        )
        db.add(statement)
        db.flush()
        db.add(
            SupplierMonthlyInvoice(
                statement_id=statement.id,
                supplier_id=supplier.id,
                invoice_number="INV-202608",
                invoice_date=date(2026, 8, 25),
                received_date=date(2026, 8, 26),
                invoice_total_amount=Decimal("1000.00"),
                allocated_amount=Decimal("1000.00"),
                tax_amount=Decimal("115.04"),
                created_by=admin.id,
            )
        )
        db.add(
            SupplierMonthlyAdjustment(
                statement_id=statement.id,
                difference_type="price",
                amount=Decimal("20.00"),
                note="供应商调价",
                created_by=admin.id,
            )
        )
        db.commit()
        return customer.id, statement.id


def test_acceptance_endorsement_is_traceable_non_cash_supplier_payment_and_export(
    p1_131_cost_app,
) -> None:
    app, factory = p1_131_cost_app
    _enable_router(app)
    customer_id, statement_id = _seed_payable_statement(factory)
    with TestClient(app) as client:
        _login(client)
        created = client.post(
            "/api/finance/simple-finance/acceptances",
            json={
                "bill_number": "ACCEPT-202608-001",
                "customer_id": customer_id,
                "amount": "600.00",
                "received_date": "2026-08-28",
                "maturity_date": "2027-02-28",
                "note": "客户承兑",
                "idempotency_key": "p1144-acceptance-create",
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["status"] == "held"

        metadata = client.get("/api/finance/simple-finance/metadata")
        assert metadata.status_code == 200, metadata.text
        option = next(
            row for row in metadata.json()["supplier_statements"] if row["id"] == statement_id
        )
        assert Decimal(str(option["invoice_available_amount"])) == Decimal("1000.00")

        endorsed = client.post(
            f"/api/finance/simple-finance/acceptances/{created.json()['id']}/endorse",
            json={
                "expected_version": 1,
                "supplier_statement_id": statement_id,
                "endorsement_date": "2026-09-01",
                "idempotency_key": "p1144-acceptance-endorse",
            },
        )
        assert endorsed.status_code == 200, endorsed.text
        assert endorsed.json()["acceptance"]["status"] == "endorsed"
        assert endorsed.json()["supplier_statement"]["status"] == "partial_payment"
        assert Decimal(str(endorsed.json()["supplier_statement"]["paid_amount"])) == Decimal(
            "600.00"
        )
        assert "不是银行现金付款" in endorsed.json()["message"]

        blocked = client.post(
            f"/api/finance/simple-finance/acceptances/{created.json()['id']}/transition",
            json={
                "expected_version": 2,
                "action": "returned",
                "idempotency_key": "p1144-acceptance-return-blocked",
            },
        )
        assert blocked.status_code == 409

        summary = client.get(
            "/api/finance/simple-finance/summary", params={"month": "2026-08"}
        )
        assert summary.status_code == 200, summary.text
        material = summary.json()["primary_groups"]["material"]
        assert Decimal(str(material["erp_amount"])) == Decimal("1000.00")
        assert Decimal(str(material["invoice_amount"])) == Decimal("1000.00")
        assert material["differences"][0]["adjustments"][0]["note"] == "供应商调价"

        exported = client.get(
            "/api/finance/simple-finance/material-export", params={"month": "2026-08"}
        )
        assert exported.status_code == 200, exported.text
        workbook = load_workbook(BytesIO(exported.content), data_only=True)
        assert workbook.sheetnames == [
            "供应商核票汇总",
            "纸板与外购包材明细",
            "差异明细",
        ]
        assert workbook["供应商核票汇总"]["A2"].value == "上游纸板厂"
        assert workbook["差异明细"]["C2"].value == "价格差异"

    from app.models.finance_simplified import FinanceAcceptanceNote
    from app.models.supplier_settlement import SupplierMonthlyPayment

    with factory() as db:
        note = db.query(FinanceAcceptanceNote).filter_by(
            bill_number="ACCEPT-202608-001"
        ).one()
        payment = db.get(SupplierMonthlyPayment, note.supplier_payment_id)
        assert payment.payment_method == "acceptance"
        assert payment.acceptance_note_id == note.id
        assert payment.reference == "承兑背书 ACCEPT-202608-001"
