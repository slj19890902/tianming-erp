"""Tests for supplier flute price rules + effective material price + compare (v0.19.2-B).

Covers app/services/material_pricing.py:
  * get_flute_delta：按 供应商+层数+楞型 取加价，最新生效优先，无规则=0
  * get_effective_material_price：base + delta，B/E=0、三层A瓦+0.04/0.05
  * compare_materials：同克重多供应商分组、最终可比价含楞型加价、缺字段不报错、B/E≠BE
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from app.services import material_pricing as mp


@pytest.fixture()
def db(tmp_path: Path):
    from app.core.database import create_sqlite_engine
    from app.models import Base

    engine = create_sqlite_engine(tmp_path / "test_flute.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session


def _rule(session, supplier, layer, flute, delta, *, active=True, eff=None):
    from app.models.supplier_flute_price_rule import SupplierFlutePriceRule

    r = SupplierFlutePriceRule(
        supplier_name=supplier, layer_count=layer, flute_type=flute,
        price_delta=Decimal(str(delta)), is_active=active, effective_date=eff,
    )
    session.add(r)
    session.flush()
    return r


def _mat(session, code, supplier, price, *, layer=3, flute="B", weight="150g/130g/130g",
         paper=None, active=True):
    from app.models.material import Material

    m = Material(
        code=code, supplier_name=supplier,
        quote_price=None if price is None else Decimal(str(price)),
        is_active=active, layer_count=layer, flute_type=flute,
        basis_weight_description=weight, paper_composition=paper,
    )
    session.add(m)
    session.flush()
    return m


class TestGetFluteDelta:
    def test_a_flute_jialin(self, db):
        _rule(db, "苏州嘉林亿", 3, "A", "0.04")
        db.commit()
        delta, rid = mp.get_flute_delta(db, supplier_name="苏州嘉林亿", layer_count=3, flute_type="A")
        assert delta == Decimal("0.04") and rid is not None

    def test_be_zero(self, db):
        _rule(db, "昆山鸣朋", 3, "B", "0")
        db.commit()
        delta, rid = mp.get_flute_delta(db, supplier_name="昆山鸣朋", layer_count=3, flute_type="B")
        assert delta == Decimal("0")

    def test_no_rule_returns_zero_none(self, db):
        delta, rid = mp.get_flute_delta(db, supplier_name="无此供应商", layer_count=3, flute_type="A")
        assert delta == Decimal("0") and rid is None

    def test_inactive_rule_ignored(self, db):
        _rule(db, "苏州佳丰", 3, "A", "0.05", active=False)
        db.commit()
        delta, rid = mp.get_flute_delta(db, supplier_name="苏州佳丰", layer_count=3, flute_type="A")
        assert delta == Decimal("0") and rid is None

    def test_latest_effective_wins(self, db):
        _rule(db, "苏州嘉林亿", 3, "A", "0.04", eff=dt.date(2026, 1, 1))
        _rule(db, "苏州嘉林亿", 3, "A", "0.06", eff=dt.date(2026, 6, 1))
        db.commit()
        delta, rid = mp.get_flute_delta(db, supplier_name="苏州嘉林亿", layer_count=3, flute_type="A")
        assert delta == Decimal("0.06")

    def test_be_not_treated_as_three_layer(self, db):
        # 五层 BE 规则不应被三层 B/E 命中
        _rule(db, "苏州嘉林亿", 5, "BE", "0.10")
        db.commit()
        delta, _ = mp.get_flute_delta(db, supplier_name="苏州嘉林亿", layer_count=3, flute_type="B")
        assert delta == Decimal("0")


class TestEffectivePrice:
    def test_a_flute_adds_delta(self, db):
        _rule(db, "苏州嘉林亿", 3, "A", "0.04")
        m = _mat(db, "A6D-B/E", "苏州嘉林亿", "1.52", layer=3)
        db.commit()
        out = mp.get_effective_material_price(db, material=m, flute_type="A")
        assert out["base_price"] == 1.52
        assert out["flute_delta"] == 0.04
        assert out["effective_price"] == 1.56

    def test_b_flute_no_delta(self, db):
        _rule(db, "苏州嘉林亿", 3, "B", "0")
        m = _mat(db, "A6D-B/E", "苏州嘉林亿", "1.52", layer=3)
        db.commit()
        out = mp.get_effective_material_price(db, material=m, flute_type="B")
        assert out["effective_price"] == 1.52

    def test_none_price(self, db):
        m = _mat(db, "X", "昆山鸣朋", None, layer=3)
        db.commit()
        out = mp.get_effective_material_price(db, material=m, flute_type="A")
        assert out["effective_price"] is None


class TestCompare:
    def test_groups_by_weight_with_delta(self, db):
        _rule(db, "苏州嘉林亿", 3, "A", "0.04")
        _rule(db, "昆山鸣朋", 3, "A", "0.05")
        _rule(db, "苏州佳丰", 3, "A", "0.05")
        m1 = _mat(db, "A6D-B/E", "苏州嘉林亿", "1.52", weight="150g/130g/130g")
        m2 = _mat(db, "D6D-B/E", "昆山鸣朋", "1.50", weight="150g/130g/130g")
        m3 = _mat(db, "D3D-B/E", "苏州佳丰", "1.48", weight="150g/130g/130g")
        db.commit()
        groups = mp.compare_materials(db, candidates=[m1, m2, m3], layer_count=3, flute_type="A")
        assert len(groups) == 1
        g = groups[0]
        # 最终价：嘉林亿 1.56 / 鸣朋 1.55 / 佳丰 1.53 → 最低 1.53
        effs = {r["supplier_name"]: r["effective_price"] for r in g["rows"]}
        assert effs["苏州嘉林亿"] == 1.56
        assert effs["昆山鸣朋"] == 1.55
        assert effs["苏州佳丰"] == 1.53
        assert g["min_effective"] == 1.53

    def test_missing_paper_does_not_raise(self, db):
        m = _mat(db, "NOPAPER", "昆山鸣朋", "1.0", paper=None, weight="100g/100g")
        db.commit()
        groups = mp.compare_materials(db, candidates=[m], layer_count=3, flute_type="B")
        assert len(groups) == 1
        assert groups[0]["status"] == "纸种未完整维护，待人工确认"

    def test_same_weight_requires_same_paper_type_for_automatic_comparison(self, db):
        first = _mat(
            db,
            "A6A",
            "供应商甲",
            "1.80",
            weight="120g/100g/120g",
            paper="面纸:A=120g 国产A级牛卡 | 瓦楞:6=100g 国产高强瓦 | 里纸:A=120g 国产A级牛卡",
        )
        same = _mat(
            db,
            "B7B",
            "供应商乙",
            "1.70",
            weight="120g/100g/120g",
            paper="面纸:B=120g 国产A级牛卡 | 瓦楞:7=100g 国产高强瓦 | 里纸:B=120g 国产A级牛卡",
        )
        different = _mat(
            db,
            "C8C",
            "供应商丙",
            "1.60",
            weight="120g/100g/120g",
            paper="面纸:C=120g 国产A级牛卡 | 瓦楞:8=100g 国产普瓦 | 里纸:C=120g 国产A级牛卡",
        )
        db.commit()
        groups = mp.compare_materials(
            db,
            candidates=[first, same, different],
            layer_count=3,
            flute_type="B",
        )
        assert len(groups) == 2
        comparable = next(group for group in groups if len(group["rows"]) == 2)
        assert comparable["same_weight_and_paper"] is True
        assert {row["material_code"] for row in comparable["rows"]} == {"A6A", "B7B"}
