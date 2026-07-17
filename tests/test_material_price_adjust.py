"""Tests for supplier price-adjustment + board-cost reference (v0.19.2-B).

Covers app/services/material_price_adjust.py and the pricing reuse:
  * percent parsing (+5% / -10% / 5 / -3)
  * new-price rounding to 2 places
  * affected-material selection (active + product-referenced, skip inactive-unreferenced)
  * preview never writes; apply backs up + writes batch + history + updates materials
  * board-cost reuses pricing.calculate_price (normal A1 formula)
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from app.services import material_price_adjust as pa
from app.services.pricing import calculate_price


@pytest.fixture()
def db(tmp_path: Path):
    from app.core.database import create_sqlite_engine
    from app.models import Base

    engine = create_sqlite_engine(tmp_path / "test_pa.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session


def _mat(session, code, supplier, price, *, active=True, layer=3, flute="B"):
    from app.models.material import Material

    m = Material(
        code=code, supplier_name=supplier, quote_price=Decimal(str(price)),
        is_active=active, layer_count=layer, flute_type=flute,
    )
    session.add(m)
    session.flush()
    return m


def _admin(session):
    from app.models.user import User

    user = User(
        username="price-adjust-admin",
        password_hash="not-used",
        role="admin",
        real_name="调价测试管理员",
        is_active=True,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


class TestParsePercent:
    def test_plus_percent(self):
        assert pa.parse_adjust_percent("+5%") == Decimal("5")

    def test_minus_percent(self):
        assert pa.parse_adjust_percent("-10%") == Decimal("-10")

    def test_bare_number_is_percent(self):
        assert pa.parse_adjust_percent("5") == Decimal("5")

    def test_negative_bare(self):
        assert pa.parse_adjust_percent("-3") == Decimal("-3")

    def test_empty_raises(self):
        with pytest.raises(pa.PriceAdjustError):
            pa.parse_adjust_percent("")

    def test_below_minus_100_raises(self):
        with pytest.raises(pa.PriceAdjustError):
            pa.parse_adjust_percent("-150")


class TestComputeNewPrice:
    def test_up_5pct(self):
        assert pa.compute_new_price(Decimal("1.00"), Decimal("5")) == Decimal("1.05")

    def test_down_10pct(self):
        assert pa.compute_new_price(Decimal("2.00"), Decimal("-10")) == Decimal("1.80")

    def test_rounds_two_places(self):
        # 1.234 * 1.05 = 1.2957 -> 1.30
        assert pa.compute_new_price(Decimal("1.234"), Decimal("5")) == Decimal("1.30")


class TestAffectedSelection:
    def test_skips_inactive_unreferenced(self, db):
        _mat(db, "A1", "昆山鸣朋", "1.0", active=True)
        _mat(db, "A2", "昆山鸣朋", "2.0", active=False)
        db.commit()
        affected = pa.select_affected_materials(db, "昆山鸣朋")
        assert {m.code for m in affected} == {"A1"}

    def test_includes_referenced_inactive(self, db):
        from app.models.product import Product
        from app.models.customer import Customer

        cust = Customer(name="客户A")
        db.add(cust)
        db.flush()
        m = _mat(db, "A3", "昆山鸣朋", "3.0", active=False)
        db.add(Product(customer_id=cust.id, product_code="P1", customer_material_code="CM1", product_name="x", material_id=m.id))
        db.commit()
        affected = pa.select_affected_materials(db, "昆山鸣朋")
        assert "A3" in {x.code for x in affected}


class TestPreviewNoWrite:
    def test_preview_does_not_change_prices(self, db):
        _mat(db, "B1", "苏州佳丰", "1.00")
        _mat(db, "B2", "苏州佳丰", "3.00")
        db.commit()
        out = pa.preview(db, supplier_name="苏州佳丰", adjust_percent_raw="+10%", effective_date=None)
        assert out["affected_count"] == 2
        assert out["old_min"] == 1.0 and out["new_min"] == 1.1
        from app.models.material import Material
        prices = {m.code: float(m.quote_price) for m in db.scalars(__import__("sqlalchemy").select(Material)).all()}
        assert prices == {"B1": 1.0, "B2": 3.0}  # unchanged


class TestApply:
    def test_apply_backs_up_and_writes_history(self, db, monkeypatch, tmp_path):
        marker = {"backed_up": False}

        def fake_backup():
            marker["backed_up"] = True
            p = tmp_path / "backup.sqlite3"
            p.write_text("x")
            return p

        monkeypatch.setattr(pa, "backup_database", fake_backup)
        _mat(db, "C1", "苏州嘉林亿", "1.00")
        _mat(db, "C2", "苏州嘉林亿", "2.00")
        admin = _admin(db)
        db.commit()
        eff = dt.date(2026, 7, 1)
        preview = pa.preview(
            db,
            supplier_name="苏州嘉林亿",
            adjust_percent_raw="5",
            effective_date=eff,
        )
        result = pa.apply(
            db, supplier_name="苏州嘉林亿", adjust_percent_raw="5",
            effective_date=eff,
            expected_versions=preview["expected_versions"],
            change_reason="季度调价",
            confirmation_tokens={},
            user=admin,
            operator="admin",
        )
        assert db.in_transaction()
        db.commit()
        assert marker["backed_up"] is True
        assert result["affected_count"] == 2
        from app.models.material import Material
        from app.models.material_price_history import MaterialPriceHistory, MaterialPriceAdjustmentBatch
        import sqlalchemy as sa
        c1 = db.scalar(sa.select(Material).where(Material.code == "C1"))
        assert float(c1.quote_price) == 1.05
        assert c1.quote_date == eff
        hist = db.scalars(sa.select(MaterialPriceHistory)).all()
        assert len(hist) == 2
        batches = db.scalars(sa.select(MaterialPriceAdjustmentBatch)).all()
        assert len(batches) == 1 and batches[0].affected_count == 2

    def test_apply_empty_raises(self, db, monkeypatch):
        monkeypatch.setattr(pa, "backup_database", lambda: Path("nope"))
        admin = _admin(db)
        with pytest.raises(pa.PriceAdjustError):
            pa.apply(db, supplier_name="不存在", adjust_percent_raw="5",
                     effective_date=None, expected_versions={},
                     change_reason="季度调价", confirmation_tokens={},
                     user=admin, operator="admin")


class TestBoardCostReuse:
    def test_normal_a1_formula(self):
        # area = (L+W+8)*(W+H+4)*2 / 1e6
        r = calculate_price(
            box_category="normal", board_square_price=Decimal("2.00"),
            length_mm=Decimal("300"), width_mm=Decimal("200"), height_mm=Decimal("150"),
        )
        # (300+200+8)*(200+150+4)*2 = 508*354*2 = 359664 mm^2 = 0.359664 m^2
        assert abs(float(r.area_m2) - 0.359664) < 1e-6
        assert float(r.unit_price) == round(0.359664 * 2.0, 2)
