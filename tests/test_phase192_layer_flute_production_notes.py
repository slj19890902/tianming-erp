"""tests/test_phase192_layer_flute_production_notes.py

v0.19.2-A + v0.19.2-B 测试套件。

覆盖：
  A-1: 全角/半角括号不截断品名
  A-2: 斜杠厚度规格不截断
  A-3/A-5: 规格列拆分 + 结构化失败原因
  A-4: snapshot_production_notes 数据链 + 各单据显示（订单详情/编辑/送货单/报料单）
  B-1: 材质库层数过滤
  B-3/B-5: 层数 × 楞型合法性校验（复用 validate_flute_consistency）

运行命令:
    python -X utf8 -m pytest tests/test_phase192_layer_flute_production_notes.py -q
"""
from __future__ import annotations

import sys
from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, ".")


# ===========================================================================
# 第一部分：PDF 解析单元测试（无 DB）
# ===========================================================================

from app.services.flute_mapping import validate_flute_consistency
from app.services.order_pdf_import import (
    PARSE_STATUS_LABELS,
    PdfParseError,
    _enrich_tianhua_item,
    _extract_spec_dimensions,
    parse_purchase_order_text,
)


class TestA1FullWidthParens:
    """A-1: 全角/半角括号不得截断品名。"""

    def test_fullwidth_parens_kept_in_name(self):
        # 全角括号尺寸必须完整进入品名，不能被切入 spec
        from app.services.order_pdf_import import _find_spec_start_outside_parens

        name = '白底黑字内箱（18"*36"）'
        idx = _find_spec_start_outside_parens(name)
        # 括号内不应被识别为 spec 起点
        if idx is not None:
            assert "（" not in name[:idx] or "）" in name[:idx]

    def test_halfwidth_parens_balanced(self):
        from app.services.order_pdf_import import _find_spec_start_outside_parens

        name = "内箱(600*900)印刷"
        idx = _find_spec_start_outside_parens(name)
        if idx is not None:
            # 括号内的 600*900 不应被当作规格起点
            assert idx == 0 or "(" not in name[:idx] or ")" in name[:idx]


class TestA2SlashSpecNotTruncated:
    """A-2: 斜杠厚度规格不截断。"""

    def test_slash_thickness_preserved(self):
        assert _extract_spec_dimensions("115*67*2.5/2.8cm") != "115*67*2.5"

    def test_slash_thickness_contains_both(self):
        result = _extract_spec_dimensions("93.5*47*2.3/2.6cm")
        assert "2.3" in result
        assert "2.6" in result


class TestA3ProductionNotesSplit:
    """A-3: 规格列拆分，生产说明含 CJK，排除包装/旧码/客户型号。"""

    def test_production_notes_extracted(self):
        item = {"product_code": "X", "product_name": "内箱"}
        spec_raw = "93.5*47*2.3/2.6cm W535A/BE THH10 在白色处打勾 縦置き厳禁 5盒/箱"
        enriched = _enrich_tianhua_item(item, spec_raw)
        notes = enriched.get("production_notes") or ""
        # 生产说明应包含日文/中文警示
        assert "打勾" in notes or "厳禁" in notes
        # 不应包含旧材质码 / 客户型号 / 包装注记
        assert "W535A" not in notes
        assert "THH10" not in notes
        assert "盒/箱" not in notes

    def test_size_spec_excludes_packaging(self):
        item = {"product_code": "X", "product_name": "内箱"}
        spec_raw = "93.5*47*2.3/2.6cm W535A/BE THH10 5盒/箱"
        enriched = _enrich_tianhua_item(item, spec_raw)
        assert "盒/箱" not in enriched["size_spec"]

    def test_old_material_and_model_split(self):
        item = {"product_code": "X", "product_name": "内箱"}
        spec_raw = "93.5*47*2.3/2.6cm W535A/BE THH10"
        enriched = _enrich_tianhua_item(item, spec_raw)
        assert enriched["old_material_code"] == "W535A/BE"
        assert enriched["customer_model"] == "THH10"

    def test_packaging_kept_as_debug_only(self):
        item = {"product_code": "X", "product_name": "内箱"}
        spec_raw = "93.5*47cm 5盒/箱"
        enriched = _enrich_tianhua_item(item, spec_raw)
        assert enriched.get("packaging_note")


class TestA5StructuredFailures:
    """A-5: 非天华 PDF 结构化失败原因。"""

    def test_empty_text_raises_structured(self):
        with pytest.raises(PdfParseError) as exc:
            parse_purchase_order_text("")
        assert exc.value.parse_status in (
            "ocr_required",
            "ocr_unavailable",
            "empty",
        )

    def test_no_order_number_structured(self):
        text = "某某公司\n这是一段没有订单号也没有表头的随便文字\n谢谢"
        with pytest.raises(PdfParseError) as exc:
            parse_purchase_order_text(text)
        assert exc.value.parse_status in (
            "order_no_not_recognized",
            "customer_not_recognized",
            "header_not_recognized",
            "items_not_split",
        )

    def test_status_labels_present(self):
        for key in (
            "customer_not_recognized",
            "order_no_not_recognized",
            "header_not_recognized",
            "items_not_split",
        ):
            assert key in PARSE_STATUS_LABELS
            assert PARSE_STATUS_LABELS[key]


# ===========================================================================
# 第二部分：层数 × 楞型合法性（复用 validate_flute_consistency）
# ===========================================================================

class TestFluteConsistencyReuse:
    """B-5: 复用 validate_flute_consistency 的合法性矩阵。"""

    def test_3_plus_ab_invalid(self):
        assert validate_flute_consistency("AB", 3) is not None

    def test_3_plus_be_invalid(self):
        assert validate_flute_consistency("BE", 3) is not None

    def test_5_plus_a_invalid(self):
        assert validate_flute_consistency("A", 5) is not None

    def test_5_plus_b_invalid(self):
        assert validate_flute_consistency("B", 5) is not None

    def test_5_plus_e_invalid(self):
        assert validate_flute_consistency("E", 5) is not None

    def test_3_plus_a_valid(self):
        assert validate_flute_consistency("A", 3) is None

    def test_3_plus_b_valid(self):
        assert validate_flute_consistency("B", 3) is None

    def test_3_plus_e_valid(self):
        assert validate_flute_consistency("E", 3) is None

    def test_5_plus_ab_valid(self):
        assert validate_flute_consistency("AB", 5) is None

    def test_5_plus_be_valid(self):
        assert validate_flute_consistency("BE", 5) is None


# ===========================================================================
# 第三部分：集成测试 fixture（材质 / 订单 / 送货 / 报料）
# ===========================================================================

@pytest.fixture()
def v192_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.api.auth import router as auth_router
    from app.api.deliveries import router as deliveries_router
    from app.api.deps import get_db
    from app.api.materials import router as materials_router
    from app.api.orders import router as orders_router
    from app.api.requisition import router as requisition_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.delivery import Delivery, DeliveryItem
    from app.models.material import Material
    from app.models.order import Order, OrderItem
    from app.models.product import Product
    from app.models.requisition import Requisition, RequisitionItem
    from app.models.user import User

    monkeypatch.setenv("ERP_DRAWING_DIR", str(tmp_path / "uploads"))
    engine = create_sqlite_engine(tmp_path / "v192.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    with session_factory() as session:
        session.add_all(
            User(
                username=role,
                password_hash=hash_password("RolePass123!"),
                role=role,
                real_name=role,
                display_name=role,
                must_change_password=False,
            )
            for role in ("admin", "sales", "workshop")
        )
        customer = Customer(
            customer_number=1,
            customer_code="ACTIVE",
            name="苏州正常客户",
            payment_term_days=30,
            credit_limit=0,
            delivery_method="配送",
        )
        # 三层 + B（合法）、五层 + AB（合法）、三层 + BE（历史异常）
        m3 = Material(code="D4B-B", paper_composition="D4B", layer_count=3, flute_type="B")
        m5 = Material(code="W535A-AB", paper_composition="W535A", layer_count=5, flute_type="AB")
        m_dirty = Material(code="CCC-B/E", paper_composition="CCC", layer_count=3, flute_type="BE")
        session.add_all([customer, m3, m5, m_dirty])
        session.flush()
        product = Product(
            customer_id=customer.id,
            product_code="21301028",
            customer_material_code="21301028",
            product_name="中性外箱",
            material_id=m5.id,
            box_category="normal",
            cost_unit_price=Decimal("2.00"),
        )
        session.add(product)
        session.flush()
        order = Order(
            order_number="PO-20260625-001",
            customer_id=customer.id,
            order_date=date(2026, 6, 25),
            delivery_date=date(2026, 7, 2),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("300"),
        )
        session.add(order)
        session.flush()
        item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=100,
            delivered_quantity=80,
            unit_price=Decimal("3.00"),
            subtotal=Decimal("300"),
            material_status="received",
            requisition_status="已报料",
            snapshot_product_code="21301028",
            snapshot_product_name="中性外箱",
            snapshot_spec="93.5脳47脳2.3mm",
            snapshot_material="W535A/AB",
            snapshot_customer_model="THH10",
            snapshot_production_notes="在白色处打勾 縦置き厳禁",
        )
        session.add(item)
        session.flush()
        delivery = Delivery(
            delivery_number="DH-20260625-001",
            customer_id=customer.id,
            delivery_date=date(2026, 6, 25),
            status="dispatched",
            total_quantity=80,
            dispatched_at=datetime(2026, 6, 25, 9, 0),
        )
        session.add(delivery)
        session.flush()
        session.add(
            DeliveryItem(
                delivery_id=delivery.id,
                order_item_id=item.id,
                delivered_quantity=80,
            )
        )
        requisition = Requisition(
            requisition_number="RQ-20260625-001",
            requisition_date=date(2026, 6, 25),
            supplier_name="鸣朋",
            status="有效",
        )
        session.add(requisition)
        session.flush()
        session.add(
            RequisitionItem(
                requisition_id=requisition.id,
                order_item_id=item.id,
                product_code_snapshot="21301028",
                product_name_snapshot="中性外箱",
                material_snapshot="W535A/AB",
                requisition_qty=100,
                cardboard_len=Decimal("935"),
                cardboard_width=Decimal("470"),
                special_process="无",
                status="有效",
            )
        )
        # 第二个未发货订单，用于编辑测试（首个已发货明细禁止修改）
        order2 = Order(
            order_number="PO-20260625-002",
            customer_id=customer.id,
            order_date=date(2026, 6, 25),
            status="pending_production",
            payment_status="unpaid",
            total_amount=Decimal("90"),
        )
        session.add(order2)
        session.flush()
        item2 = OrderItem(
            order_id=order2.id,
            product_id=product.id,
            quantity=30,
            delivered_quantity=0,
            unit_price=Decimal("3.00"),
            subtotal=Decimal("90"),
            material_status="pending",
            requisition_status="未报料",
            snapshot_product_code="21301028",
            snapshot_product_name="中性外箱",
            snapshot_production_notes="初始说明",
        )
        session.add(item2)
        session.commit()
        ids = {
            "order_id": order.id,
            "order2_id": order2.id,
            "delivery_id": delivery.id,
            "requisition_id": requisition.id,
            "item_id": item.id,
            "item2_id": item2.id,
        }

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(materials_router, prefix="/api/master/materials")
    app.include_router(orders_router, prefix="/api/orders")
    app.include_router(deliveries_router, prefix="/api/deliveries")
    app.include_router(requisition_router, prefix="/api/requisition")

    def override_get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return app, session_factory, ids


def _login(client: TestClient, role: str = "admin") -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": role, "password": "RolePass123!"},
    )
    assert response.status_code == 200, response.text


class TestB1MaterialLayerFilter:
    """B-1: 材质库层数过滤。"""

    def test_filter_3_layer(self, v192_app):
        app, _, _ = v192_app
        with TestClient(app) as client:
            _login(client)
            res = client.get("/api/master/materials", params={"layer_count": 3})
        assert res.status_code == 200, res.text
        items = res.json()["items"]
        assert items
        assert all(it["layer_count"] == 3 for it in items)

    def test_filter_5_layer(self, v192_app):
        app, _, _ = v192_app
        with TestClient(app) as client:
            _login(client)
            res = client.get("/api/master/materials", params={"layer_count": 5})
        assert all(it["layer_count"] == 5 for it in res.json()["items"])

    def test_filter_7_layer_empty(self, v192_app):
        app, _, _ = v192_app
        with TestClient(app) as client:
            _login(client)
            res = client.get("/api/master/materials", params={"layer_count": 7})
        assert res.json()["items"] == []


class TestB5MaterialDictionaryBoundary:
    """材质字典只保存材质代码/层数，不绑定产品实际楞型。"""

    @pytest.mark.parametrize(
        ("code", "layer_count", "submitted_flute"),
        (("DICT3", 3, "AB"), ("DICT5", 5, "A")),
    )
    def test_create_and_update_discard_product_flute_dimension(
        self,
        v192_app,
        code: str,
        layer_count: int,
        submitted_flute: str,
    ):
        app, _, _ = v192_app
        with TestClient(app) as client:
            _login(client)
            created = client.post(
                "/api/master/materials",
                json={
                    "code": code,
                    "layer_count": layer_count,
                    "flute_type": submitted_flute,
                },
            )
            assert created.status_code == 201, created.text
            assert created.json()["flute_type"] is None

            updated = client.put(
                f"/api/master/materials/{created.json()['id']}",
                json={
                    "code": code,
                    "layer_count": layer_count,
                    "flute_type": "BE" if submitted_flute == "A" else "B",
                },
            )
        assert updated.status_code == 200, updated.text
        assert updated.json()["flute_type"] is None


class TestA4ProductionNotesDisplay:
    """A-4: snapshot_production_notes 在订单详情/编辑/送货单/报料单显示。"""

    def test_order_detail_shows_production_notes(self, v192_app):
        app, _, ids = v192_app
        with TestClient(app) as client:
            _login(client)
            res = client.get(f"/api/orders/{ids['order_id']}")
        assert res.status_code == 200, res.text
        item = res.json()["items"][0]
        assert item["snapshot_production_notes"] == "在白色处打勾 縦置き厳禁"

    def test_order_item_update_sets_production_notes(self, v192_app):
        app, _, ids = v192_app
        with TestClient(app) as client:
            _login(client)
            res = client.put(
                f"/api/orders/items/{ids['item2_id']}",
                json={
                    "quantity": 30,
                    "unit_price": "3.00",
                    "product_code": "21301028",
                    "product_name": "中性外箱",
                    "production_notes": "改为印刷日文",
                },
            )
            assert res.status_code == 200, res.text
            detail = client.get(f"/api/orders/{ids['order2_id']}").json()
        assert detail["items"][0]["snapshot_production_notes"] == "改为印刷日文"

    def test_delivery_print_excludes_internal_production_notes(self, v192_app):
        app, _, ids = v192_app
        with TestClient(app) as client:
            _login(client)
            res = client.get(f"/api/deliveries/{ids['delivery_id']}/print")
        assert res.status_code == 200, res.text
        assert "production_notes" not in res.json()["items"][0]

    def test_requisition_print_includes_production_notes(self, v192_app):
        app, _, ids = v192_app
        with TestClient(app) as client:
            _login(client)
            res = client.get(
                f"/api/requisition/batches/{ids['requisition_id']}/print"
            )
        assert res.status_code == 200, res.text
        assert res.json()["items"][0]["production_notes"] == "在白色处打勾 縦置き厳禁"
