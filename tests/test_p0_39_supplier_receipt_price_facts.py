from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker


BACKUP_REFERENCE = "carton_erp_before_p0_39_20260903.sqlite3 sha256:test"


@pytest.fixture()
def price_fact_db(tmp_path: Path):
    from app.core.database import create_sqlite_engine
    from app.models import Base
    from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem
    from app.models.material import Material
    from app.models.stock_replenishment import (
        StockReplenishmentOrder,
        StockReplenishmentOrderItem,
    )
    from app.models.supplier import Supplier, SupplierAlias
    from app.models.user import User
    from app.services.supplier_master import normalize_supplier_identity

    engine = create_sqlite_engine(tmp_path / "p0-39-price-facts.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as db:
        user = User(
            username="p0-39-admin",
            password_hash="not-used",
            role="admin",
            real_name="P0-39 管理员",
            must_change_password=False,
        )
        supplier = Supplier(
            standard_name="苏州佳丰纸板有限公司",
            normalized_name=normalize_supplier_identity("苏州佳丰纸板有限公司"),
            display_name="佳丰纸板",
            is_active=True,
            version=1,
        )
        db.add_all([user, supplier])
        db.flush()
        db.add(
            SupplierAlias(
                supplier_id=supplier.id,
                alias_name="佳丰",
                normalized_alias=normalize_supplier_identity("佳丰"),
            )
        )
        stable_material = Material(
            code="A416D",
            supplier_name="佳丰",
            quote_price=Decimal("2.80"),
            price_unit="元/㎡",
            purchase_currency="CNY",
            purchase_tax_included=True,
            purchase_tax_rate=Decimal("0.13"),
            is_active=True,
            version=3,
        )
        fallback_material = Material(
            code="B516D",
            supplier_name="佳丰",
            quote_price=Decimal("4.00"),
            price_unit="元/张",
            purchase_currency="CNY",
            purchase_tax_included=True,
            purchase_tax_rate=Decimal("0.13"),
            is_active=True,
            version=2,
        )
        missing_price_material = Material(
            code="C616D",
            supplier_name="佳丰",
            quote_price=None,
            price_unit=None,
            purchase_currency=None,
            purchase_tax_included=None,
            purchase_tax_rate=None,
            is_active=True,
            version=1,
        )
        db.add_all([stable_material, fallback_material, missing_price_material])
        db.flush()

        historical_order = StockReplenishmentOrder(
            order_number="SR-HIST-202608",
            supplier_name="佳丰",
            source_type="manual_history",
            status="stocked",
            created_by=user.id,
        )
        db.add(historical_order)
        db.flush()
        stable_source = StockReplenishmentOrderItem(
            replenishment_order_id=historical_order.id,
            target_inventory_type="semi_finished",
            material_id=stable_material.id,
            product_name_snapshot="历史稳定材质补库片料",
            material_code_snapshot="A416D",
            normalized_material_code="A416D",
            report_length_mm=1000,
            report_width_mm=500,
            quantity=10,
            stocked_quantity=10,
        )
        fallback_source = StockReplenishmentOrderItem(
            replenishment_order_id=historical_order.id,
            target_inventory_type="semi_finished",
            material_id=None,
            product_name_snapshot="历史供应商材质编码补库片料",
            material_code_snapshot="B516D/BC",
            normalized_material_code="B516D/BC",
            report_length_mm=1000,
            report_width_mm=500,
            quantity=5,
            stocked_quantity=5,
        )
        db.add_all([stable_source, fallback_source])
        db.flush()
        historical_receipt = IncomingReceipt(
            receipt_number="IR-HIST-20260805",
            status="posted",
            received_at=datetime(2026, 8, 5, 3, 0, 0),
            received_by=user.id,
            idempotency_key="p0-39-historical-receipt",
        )
        db.add(historical_receipt)
        db.flush()
        stable_receipt_item = _receipt_item(
            receipt_id=historical_receipt.id,
            source_id=stable_source.id,
            quantity=10,
        )
        fallback_receipt_item = _receipt_item(
            receipt_id=historical_receipt.id,
            source_id=fallback_source.id,
            quantity=5,
        )
        db.add_all([stable_receipt_item, fallback_receipt_item])

        live_order = StockReplenishmentOrder(
            order_number="SR-LIVE-202609",
            supplier_name="佳丰",
            source_type="stock_warning",
            status="confirmed",
            created_by=user.id,
        )
        db.add(live_order)
        db.flush()
        live_source = StockReplenishmentOrderItem(
            replenishment_order_id=live_order.id,
            target_inventory_type="semi_finished",
            material_id=stable_material.id,
            product_name_snapshot="新补库片料",
            material_code_snapshot="A416D",
            normalized_material_code="A416D",
            report_length_mm=1200,
            report_width_mm=800,
            quantity=8,
            stocked_quantity=8,
        )
        missing_source = StockReplenishmentOrderItem(
            replenishment_order_id=live_order.id,
            target_inventory_type="semi_finished",
            material_id=missing_price_material.id,
            product_name_snapshot="缺价补库片料",
            material_code_snapshot="C616D",
            normalized_material_code="C616D",
            report_length_mm=1200,
            report_width_mm=800,
            quantity=6,
            stocked_quantity=6,
        )
        db.add_all([live_source, missing_source])
        db.flush()
        live_receipt = IncomingReceipt(
            receipt_number="IR-LIVE-20260901",
            status="posted",
            received_at=datetime(2026, 9, 1, 3, 0, 0),
            received_by=user.id,
            idempotency_key="p0-39-live-receipt",
        )
        db.add(live_receipt)
        db.flush()
        live_receipt_item = _receipt_item(
            receipt_id=live_receipt.id,
            source_id=live_source.id,
            quantity=8,
        )
        missing_receipt_item = _receipt_item(
            receipt_id=live_receipt.id,
            source_id=missing_source.id,
            quantity=6,
        )
        db.add_all([live_receipt_item, missing_receipt_item])
        db.commit()
        fixture = {
            "user_id": user.id,
            "supplier_id": supplier.id,
            "stable_receipt_item_id": stable_receipt_item.id,
            "fallback_receipt_item_id": fallback_receipt_item.id,
            "live_receipt_item_id": live_receipt_item.id,
            "missing_receipt_item_id": missing_receipt_item.id,
        }

    yield session_factory, fixture
    engine.dispose()


def _receipt_item(*, receipt_id: int, source_id: int, quantity: int):
    from app.models.incoming_receipt import IncomingReceiptItem

    return IncomingReceiptItem(
        receipt_id=receipt_id,
        stock_replenishment_item_id=source_id,
        planned_quantity=quantity,
        received_quantity=quantity,
        cumulative_received_quantity=quantity,
        variance_quantity=0,
        variance_type="matched",
        resolution_status="not_required",
        resolution_action=None,
        status="posted",
    )


def _august_period() -> tuple[datetime, datetime]:
    from app.services.supplier_monthly_settlement import settlement_period_utc_bounds

    _start, _end, start_utc, end_utc = settlement_period_utc_bounds("2026-08")
    return start_utc, end_utc


def _preview(db: Session):
    from app.services.supplier_receipt_price_facts import (
        preview_historical_price_adoptions,
    )

    start_utc, end_utc = _august_period()
    return preview_historical_price_adoptions(
        db,
        settlement_month="2026-08",
        start_utc=start_utc,
        end_utc=end_utc,
    )


def test_historical_preview_is_read_only_and_matches_stable_id_and_supplier_alias(
    price_fact_db,
) -> None:
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact

    session_factory, fixture = price_fact_db
    with session_factory() as db:
        before_count = db.scalar(
            select(func.count(SupplierReceiptSettlementPriceFact.id))
        )
        preview = _preview(db)
        after_count = db.scalar(
            select(func.count(SupplierReceiptSettlementPriceFact.id))
        )

        assert before_count == after_count == 0
        assert preview["writes_performed"] is False
        assert preview["eligible_count"] == 2
        assert preview["rejected_count"] == 0
        assert len(preview["plan_hash"]) == 64
        rows = {
            row["incoming_receipt_item_id"]: row for row in preview["eligible"]
        }
        stable = rows[fixture["stable_receipt_item_id"]]
        fallback = rows[fixture["fallback_receipt_item_id"]]
        assert stable["supplier_id"] == fixture["supplier_id"]
        assert stable["supplier_name"] == "佳丰纸板"
        assert stable["match_strategy"] == "stable_material_id"
        assert stable["erp_amount"] == Decimal("14.00")
        assert fallback["supplier_id"] == fixture["supplier_id"]
        assert fallback["match_strategy"] == "supplier_unique_material_code"
        assert fallback["material_code"] == "B516D"
        assert fallback["erp_amount"] == Decimal("20.00")


def test_blank_material_supplier_never_matches_an_arbitrary_supplier(
    price_fact_db,
) -> None:
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.material import Material
    from app.models.stock_replenishment import StockReplenishmentOrderItem

    session_factory, fixture = price_fact_db
    with session_factory() as db:
        receipt_item = db.get(
            IncomingReceiptItem,
            fixture["stable_receipt_item_id"],
        )
        assert receipt_item is not None
        source = db.get(
            StockReplenishmentOrderItem,
            receipt_item.stock_replenishment_item_id,
        )
        assert source is not None
        material = db.get(Material, source.material_id)
        assert material is not None
        material.supplier_name = ""
        db.flush()

        preview = _preview(db)
        assert fixture["stable_receipt_item_id"] not in {
            row["incoming_receipt_item_id"] for row in preview["eligible"]
        }
        rejected = {
            row["incoming_receipt_item_id"]: row for row in preview["rejected"]
        }
        assert rejected[fixture["stable_receipt_item_id"]]["code"] == (
            "SUPPLIER_RECEIPT_MATERIAL_NOT_FOUND"
        )


def test_explicit_hash_adoption_is_idempotent_and_monthly_lines_use_frozen_fact(
    price_fact_db,
) -> None:
    from app.models.supplier_settlement import (
        SupplierMonthlyStatement,
        SupplierMonthlyStatementLine,
        SupplierReceiptSettlementPriceFact,
    )
    from app.models.user import User
    from app.services.supplier_monthly_settlement import generate_or_refresh_settlements
    from app.services.supplier_receipt_price_facts import (
        HISTORICAL_ADOPTION_REASON,
        SupplierReceiptPriceFactError,
        adopt_historical_price_facts,
    )

    session_factory, _fixture = price_fact_db
    start_utc, end_utc = _august_period()
    with session_factory() as db:
        user = db.scalar(select(User).where(User.username == "p0-39-admin"))
        assert user is not None
        preview = _preview(db)
        selections = [
            (row["incoming_receipt_item_id"], row["source_hash"])
            for row in preview["eligible"]
        ]

        with pytest.raises(SupplierReceiptPriceFactError) as confirmation_error:
            adopt_historical_price_facts(
                db,
                settlement_month="2026-08",
                start_utc=start_utc,
                end_utc=end_utc,
                selections=selections,
                confirmation_text="确认",
                plan_hash=preview["plan_hash"],
                backup_reference=BACKUP_REFERENCE,
                user=user,
            )
        assert confirmation_error.value.code == (
            "SUPPLIER_RECEIPT_ADOPTION_CONFIRMATION_REQUIRED"
        )
        assert db.scalar(
            select(func.count(SupplierReceiptSettlementPriceFact.id))
        ) == 0

        adopted = adopt_historical_price_facts(
            db,
            settlement_month="2026-08",
            start_utc=start_utc,
            end_utc=end_utc,
            selections=selections,
            confirmation_text=HISTORICAL_ADOPTION_REASON,
            plan_hash=preview["plan_hash"],
            backup_reference=BACKUP_REFERENCE,
            user=user,
        )
        assert adopted["created_count"] == 2
        assert adopted["reused_count"] == 0
        db.commit()

        replayed = adopt_historical_price_facts(
            db,
            settlement_month="2026-08",
            start_utc=start_utc,
            end_utc=end_utc,
            selections=selections,
            confirmation_text=HISTORICAL_ADOPTION_REASON,
            plan_hash=preview["plan_hash"],
            backup_reference=BACKUP_REFERENCE,
            user=user,
        )
        assert replayed["created_count"] == 0
        assert replayed["reused_count"] == 2

        stale = list(selections)
        stale[0] = (stale[0][0], "0" * 64)
        with pytest.raises(SupplierReceiptPriceFactError) as conflict:
            adopt_historical_price_facts(
                db,
                settlement_month="2026-08",
                start_utc=start_utc,
                end_utc=end_utc,
                selections=stale,
                confirmation_text=HISTORICAL_ADOPTION_REASON,
                plan_hash=preview["plan_hash"],
                backup_reference=BACKUP_REFERENCE,
                user=user,
            )
        assert conflict.value.code == "SUPPLIER_RECEIPT_PRICE_FACT_CONFLICT"

        from app.models.material import Material

        for material in db.scalars(select(Material)).all():
            if material.quote_price is not None:
                material.quote_price = Decimal("99.00")
        from app.models.incoming_receipt import IncomingReceipt, IncomingReceiptItem

        facts = {
            fact.incoming_receipt_item_id: fact
            for fact in db.scalars(select(SupplierReceiptSettlementPriceFact)).all()
        }
        for fact in facts.values():
            receipt_item = db.get(IncomingReceiptItem, fact.incoming_receipt_item_id)
            assert receipt_item is not None
            receipt = db.get(IncomingReceipt, receipt_item.receipt_id)
            assert receipt is not None
            receipt.received_at = datetime(2026, 10, 1, 1, 0, 0)
        db.flush()

        generated = generate_or_refresh_settlements(
            db,
            settlement_month="2026-08",
            user=user,
            business_date=date(2026, 9, 1),
        )
        assert generated["added_line_count"] == 2
        lines = list(
            db.scalars(
                select(SupplierMonthlyStatementLine).order_by(
                    SupplierMonthlyStatementLine.id
                )
            ).all()
        )
        assert len(lines) == 2
        assert all(line.supplier_receipt_price_fact_id is not None for line in lines)
        for line in lines:
            fact = facts[int(line.incoming_receipt_item_id)]
            assert line.supplier_receipt_price_fact_id == fact.id
            assert fact.fact_origin == "historical_master_adoption"
            assert fact.adoption_reason == HISTORICAL_ADOPTION_REASON
            assert fact.adoption_evidence_reference == BACKUP_REFERENCE
            assert fact.shipping_fee_mode == "included"
        assert sorted(line.erp_amount for line in lines) == [
            Decimal("14.00"),
            Decimal("20.00"),
        ]
        assert {line.receipt_date for line in lines} == {date(2026, 8, 5)}
        statement = db.scalar(select(SupplierMonthlyStatement))
        assert statement is not None
        assert statement.supplier_name_snapshot == "佳丰纸板"
        assert statement.erp_amount == Decimal("34.00")


def test_stock_replenishment_freezes_price_and_missing_price_fails_closed(
    price_fact_db,
) -> None:
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.supplier_settlement import SupplierReceiptSettlementPriceFact
    from app.models.user import User
    from app.services.supplier_receipt_price_facts import (
        SupplierReceiptPriceFactError,
        freeze_stock_replenishment_price,
    )

    session_factory, fixture = price_fact_db
    with session_factory() as db:
        user = db.get(User, fixture["user_id"])
        live_item = db.get(IncomingReceiptItem, fixture["live_receipt_item_id"])
        missing_item = db.get(
            IncomingReceiptItem, fixture["missing_receipt_item_id"]
        )
        assert user is not None and live_item is not None and missing_item is not None

        frozen = freeze_stock_replenishment_price(
            db, receipt_item=live_item, user=user
        )
        replayed = freeze_stock_replenishment_price(
            db, receipt_item=live_item, user=user
        )
        assert replayed.id == frozen.id
        assert frozen.fact_origin == "receipt_frozen"
        assert frozen.adoption_reason is None
        assert frozen.match_strategy == "stable_material_id"
        assert frozen.unit_price == Decimal("2.800000")
        assert frozen.report_length_mm == Decimal("1200.000")
        assert frozen.report_width_mm == Decimal("800.000")
        assert frozen.shipping_fee_mode == "included"

        before_missing = db.scalar(
            select(func.count(SupplierReceiptSettlementPriceFact.id))
        )
        with pytest.raises(SupplierReceiptPriceFactError) as missing_price:
            freeze_stock_replenishment_price(
                db, receipt_item=missing_item, user=user
            )
        assert missing_price.value.code == "SUPPLIER_RECEIPT_MASTER_PRICE_INVALID"
        after_missing = db.scalar(
            select(func.count(SupplierReceiptSettlementPriceFact.id))
        )
        assert before_missing == after_missing == 1
        assert db.scalar(
            select(SupplierReceiptSettlementPriceFact.id).where(
                SupplierReceiptSettlementPriceFact.incoming_receipt_item_id
                == missing_item.id
            )
        ) is None

        from app.models.material import Material
        from app.models.stock_replenishment import StockReplenishmentOrderItem

        missing_source = db.get(
            StockReplenishmentOrderItem,
            missing_item.stock_replenishment_item_id,
        )
        assert missing_source is not None
        missing_material = db.get(Material, missing_source.material_id)
        assert missing_material is not None
        missing_material.quote_price = Decimal("3.20")
        missing_material.price_unit = "元/张"
        missing_material.purchase_currency = "CNY"
        missing_material.purchase_tax_included = False
        missing_material.purchase_tax_rate = Decimal("0.13")
        db.flush()
        with pytest.raises(SupplierReceiptPriceFactError) as wrong_tax_contract:
            freeze_stock_replenishment_price(
                db, receipt_item=missing_item, user=user
            )
        assert wrong_tax_contract.value.code == (
            "SUPPLIER_RECEIPT_MASTER_TAX_CONTRACT_INVALID"
        )


def test_finance_only_test_classification_is_the_only_quantity_override(
    price_fact_db,
) -> None:
    """A normal frozen fact still locks to raw receipt quantity; the owner flag is narrow."""
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.user import User
    from app.services.supplier_monthly_settlement import scan_settlement_candidates
    from app.services.supplier_receipt_price_facts import (
        HISTORICAL_ADOPTION_REASON,
        freeze_stock_replenishment_price,
    )

    session_factory, fixture = price_fact_db
    with session_factory() as db:
        user = db.get(User, fixture["user_id"])
        receipt_item = db.get(
            IncomingReceiptItem, fixture["live_receipt_item_id"]
        )
        assert user is not None and receipt_item is not None
        fact = freeze_stock_replenishment_price(
            db, receipt_item=receipt_item, user=user
        )
        fact.received_quantity_snapshot = Decimal("99")
        db.flush()

        _candidates, issues, _start, _end = scan_settlement_candidates(
            db, settlement_month="2026-09"
        )
        assert any(
            row["source_key"] == f"paperboard:{receipt_item.id}"
            and row["code"] == "PAPERBOARD_RECEIPT_PRICE_FACT_MISMATCH"
            for row in issues
        )

        fact.fact_origin = "historical_master_adoption"
        fact.match_strategy = "owner_authorized_finance_test_classification"
        fact.finance_only_test_classification = True
        fact.adoption_reason = HISTORICAL_ADOPTION_REASON
        fact.adoption_evidence_reference = "isolated P0-39 test"
        fact.received_quantity_snapshot = Decimal("100")
        fact.report_length_mm = Decimal("100")
        fact.report_width_mm = Decimal("100")
        fact.unit_price = Decimal("3")
        fact.price_unit = "per_square_meter"
        db.flush()

        candidates, issues, _start, _end = scan_settlement_candidates(
            db, settlement_month="2026-09"
        )
        row = next(
            item
            for item in candidates
            if item.incoming_receipt_item_id == receipt_item.id
        )
        assert row.category_label == "瓦楞纸板（历史测试归类）"
        assert row.received_quantity == Decimal("100")
        assert row.erp_amount == Decimal("3.00")
        assert not any(
            item["source_key"] == f"paperboard:{receipt_item.id}"
            for item in issues
        )
        assert receipt_item.received_quantity == 8


def test_historical_plan_amount_uses_the_same_precision_as_its_fact(
    price_fact_db,
) -> None:
    """A six-decimal fact must never differ from its own dry-run plan total."""
    from app.models.incoming_receipt import IncomingReceiptItem
    from app.models.material import Material
    from app.models.stock_replenishment import StockReplenishmentOrderItem
    from app.models.user import User
    from app.services.purchase_receipt_facts import (
        calculate_purchase_sheet_cost_breakdown,
    )
    from app.services.supplier_receipt_price_facts import (
        _fact_from_plan,
        _price_plan,
    )

    session_factory, fixture = price_fact_db
    with session_factory() as db:
        receipt_item = db.get(
            IncomingReceiptItem, fixture["stable_receipt_item_id"]
        )
        user = db.get(User, fixture["user_id"])
        assert receipt_item is not None and user is not None
        source = db.get(
            StockReplenishmentOrderItem,
            receipt_item.stock_replenishment_item_id,
        )
        assert source is not None
        material = db.get(Material, source.material_id)
        assert material is not None
        material.quote_price = Decimal("1.9700004")
        source.report_length_mm = 500
        source.report_width_mm = 425
        source.quantity = source.stocked_quantity = 200
        receipt_item.planned_quantity = 200
        receipt_item.received_quantity = 200
        receipt_item.cumulative_received_quantity = 200
        db.flush()

        plan = _price_plan(
            db, item=receipt_item, allow_supplier_code_fallback=True
        )
        fact = _fact_from_plan(plan, origin="historical_master_adoption", user=user)
        breakdown = calculate_purchase_sheet_cost_breakdown(
            unit_price=fact.unit_price,
            price_unit=fact.price_unit,
            tax_included=fact.tax_included,
            tax_rate=fact.tax_rate,
            report_length_mm=fact.report_length_mm,
            report_width_mm=fact.report_width_mm,
        )
        from scripts.admin.adopt_supplier_receipt_price_facts import _fact_amount_rows

        fact_amount = _fact_amount_rows([fact])[0]
        assert plan.unit_price == fact.unit_price == Decimal("1.970000")
        assert plan.erp_amount == Decimal("83.73")
        assert fact_amount["erp_amount"] == plan.erp_amount
        assert fact_amount["tax_amount"] == plan.tax_amount
        assert plan.erp_amount == (
            breakdown.gross_per_sheet * fact.received_quantity_snapshot
        ).quantize(Decimal("0.01"), rounding="ROUND_HALF_UP")


def test_adoption_api_is_read_only_and_has_no_web_apply_route(price_fact_db) -> None:
    from app.api.deps import get_db
    from app.api.supplier_settlements import company_read, router
    from app.models.supplier_settlement import (
        SupplierMonthlyStatement,
        SupplierReceiptSettlementPriceFact,
    )
    from app.models.user import User

    session_factory, fixture = price_fact_db

    def database_override():
        with session_factory() as db:
            yield db

    def user_override():
        with session_factory() as db:
            return db.get(User, fixture["user_id"])

    app = FastAPI()
    app.include_router(router, prefix="/api/finance")
    app.dependency_overrides[get_db] = database_override
    app.dependency_overrides[company_read] = user_override

    with TestClient(app) as client:
        preview_response = client.get(
            "/api/finance/supplier-settlements/price-adoptions/preview",
            params={"settlement_month": "2026-09"},
        )
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        eligible = [
            row
            for row in preview["eligible"]
            if row["incoming_receipt_item_id"] == fixture["live_receipt_item_id"]
        ]
        assert len(eligible) == 1
        assert eligible[0]["shipping_fee_mode"] == "included"
        unavailable = client.post(
            "/api/finance/supplier-settlements/price-adoptions/adopt",
            json={},
        )
        assert unavailable.status_code == 404

    with session_factory() as db:
        assert db.scalar(select(func.count(SupplierMonthlyStatement.id))) == 0
        assert db.scalar(
            select(func.count(SupplierReceiptSettlementPriceFact.id))
        ) == 0
