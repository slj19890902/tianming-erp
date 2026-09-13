from decimal import Decimal

from tests.test_p1_131_cost_pool import p1_131_cost_app


def test_draft_only_customer_remains_visible_without_becoming_invoice_ready(p1_131_cost_app):
    from app.api.finance import current_customer_months
    from app.models.customer import Customer
    from app.models.finance import Statement
    from app.models.user import User
    _, factory = p1_131_cost_app
    with factory() as db:
        customer = Customer(name="待确认客户", customer_code="FLOW393", is_active=True)
        db.add(customer)
        db.flush()
        statement = Statement(statement_number="FLOW393", customer_id=customer.id,
            statement_month="2026-09", total_receivable=Decimal("4072"),
            total_gross_profit=0, confirmation_status="draft", status="unsettled")
        db.add(statement)
        db.commit()
        admin = db.query(User).filter_by(username="p1131-admin").one()
        def read(balance=None, user=admin):
            return current_customer_months(statement_month="2026-09", balance_type=balance,
                customer_id=None, page=1, page_size=50, db=db, user=user)
        result = read()
        assert result["total"] == 1
        row = result["items"][0]
        assert row["primary_action"] == "review"
        assert row["pending_confirmation_count"] == 1
        assert row["pending_confirmation_amount"] == Decimal("4072")
        assert row["pending_invoice_amount"] == 0
        assert read("pending_reconciliation")["total"] == 1
        assert read("pending_invoice")["total"] == 0
        restricted = db.query(User).filter_by(username="p1131-restricted").one()
        assert read(user=restricted)["total"] == 0
        assert statement.confirmation_status == "draft"


def test_stored_material_exits_arrangement_but_remains_in_stock_and_history(monkeypatch):
    from app.services import stock_preparation_groups as groups
    from app.services import stock_preparation_disposition as disposition
    monkeypatch.setattr(groups, "groups", lambda db, rows: {"candidates":[], "groups":[]})
    monkeypatch.setattr(disposition, "assembly_rows", lambda db, ids: [])
    row = dict(key="receipt:1", status="keep", available=5, physical=5, customer_id=1,
        product_id=1, jobs=[], history=[{"action":"keep_raw"}])
    assert groups.workspace_rows(None, [row], "preparation") == []
    assert len(groups.workspace_rows(None, [row], "stock")) == 1
    assert len(groups.workspace_rows(None, [row], "history")) == 1
    assert row["physical"] == 5


def test_kept_material_does_not_reappear_as_kit(monkeypatch):
    from app.services import stock_preparation_groups as groups
    candidate = {"available_sets":1,"recipe":{"parent_id":2,"children":[{"product_id":1}]}}
    monkeypatch.setattr(groups, "groups", lambda db, rows: {"candidates":[candidate],"groups":[]})
    row = dict(key="receipt:1", status="keep", available=5, can_plan=True, product_id=1)
    assert groups.workspace_rows(None, [row], "preparation") == []


def test_incoming_template_renders_with_no_production_dialog():
    import subprocess, shutil
    node = shutil.which("node") or r"C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
    result = subprocess.run([node,"tests/check_flowui393_incoming.cjs"],capture_output=True,text=True)
    assert result.returncode == 0, result.stderr
