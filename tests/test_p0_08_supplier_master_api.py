from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker


def _supplier(
    standard_name: str,
    display_name: str,
    *,
    is_active: bool,
    sort_order: int,
    aliases: tuple[str, ...] = (),
):
    from app.models.supplier import Supplier, SupplierAlias
    from app.services.supplier_master import normalize_supplier_identity

    return Supplier(
        standard_name=standard_name,
        normalized_name=normalize_supplier_identity(standard_name),
        display_name=display_name,
        is_active=is_active,
        sort_order=sort_order,
        version=1,
        aliases=[
            SupplierAlias(
                alias_name=alias,
                normalized_alias=normalize_supplier_identity(alias),
            )
            for alias in aliases
        ],
    )


@pytest.fixture()
def supplier_app(tmp_path):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.materials import router as materials_router
    from app.api.suppliers import router as suppliers_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.customer import Customer
    from app.models.material import Material
    from app.models.supplier_paper_code import SupplierPaperCode
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "supplier-master.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add_all(
            [
                User(
                    username="supplier-admin",
                    password_hash=hash_password("123456"),
                    role="admin",
                    real_name="供应商管理员",
                    must_change_password=False,
                ),
                Customer(
                    customer_number=9801,
                    customer_code="INACTIVE-MATERIAL-UAT",
                    name="停用材质门禁测试客户",
                ),
                User(
                    username="supplier-sales",
                    password_hash=hash_password("123456"),
                    role="sales",
                    real_name="销售",
                    must_change_password=False,
                ),
                _supplier(
                    "苏州嘉林亿",
                    "嘉林亿",
                    is_active=True,
                    sort_order=10,
                    aliases=("嘉林亿",),
                ),
                _supplier(
                    "昆山鸣朋",
                    "鸣朋",
                    is_active=True,
                    sort_order=20,
                    aliases=("鸣朋",),
                ),
                _supplier(
                    "苏州佳丰",
                    "佳丰",
                    is_active=False,
                    sort_order=90,
                    aliases=("佳丰",),
                ),
                _supplier(
                    "胜源",
                    "胜源",
                    is_active=True,
                    sort_order=30,
                ),
                _supplier(
                    "森林阳光",
                    "森林阳光",
                    is_active=True,
                    sort_order=40,
                ),
                Material(
                    code="LEGACY-SUP",
                    supplier_name="旧历史供应商",
                    layer_count=3,
                    is_active=True,
                    version=1,
                ),
                Material(
                    code="7RIR6",
                    supplier_name="森林阳光",
                    layer_count=5,
                    is_active=False,
                    version=2,
                ),
                SupplierPaperCode(
                    supplier_name="旧历史供应商",
                    code_char="Z",
                    paper_name="历史纸种",
                    gram_weight=120,
                    is_active=True,
                ),
            ]
        )
        db.commit()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(materials_router, prefix="/api/master/materials")
    app.include_router(suppliers_router, prefix="/api/master/suppliers")

    def override_get_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    app.state.session_factory = factory
    yield app
    engine.dispose()


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "123456"},
    )
    assert response.status_code == 200


def test_enabled_candidates_are_dynamic_and_keep_jiafeng_historical(
    supplier_app: FastAPI,
) -> None:
    with TestClient(supplier_app) as client:
        _login(client, "supplier-sales")
        candidates = client.get("/api/master/suppliers/candidates")
        assert candidates.status_code == 200
        assert [
            item["display_name"] for item in candidates.json()["items"]
        ] == ["嘉林亿", "鸣朋", "胜源", "森林阳光"]
        assert "佳丰" not in {
            item["display_name"] for item in candidates.json()["items"]
        }
        assert client.get("/api/master/suppliers").status_code == 403

        _login(client, "supplier-admin")
        all_rows = client.get("/api/master/suppliers").json()["items"]
        jiafeng = next(item for item in all_rows if item["display_name"] == "佳丰")
        assert jiafeng["standard_name"] == "苏州佳丰"
        assert jiafeng["is_active"] is False


def test_inactive_material_is_hidden_by_default_but_remains_auditable(
    supplier_app: FastAPI,
) -> None:
    with TestClient(supplier_app) as client:
        _login(client, "supplier-admin")
        active_only = client.get(
            "/api/master/materials",
            params={"keyword": "7RIR6"},
        )
        assert active_only.status_code == 200
        assert active_only.json()["total"] == 0
        assert active_only.json()["items"] == []

        historical = client.get(
            "/api/master/materials",
            params={"keyword": "7RIR6", "include_inactive": True},
        )
        assert historical.status_code == 200
        assert historical.json()["total"] == 1
        row = historical.json()["items"][0]
        assert row["code"] == "7RIR6"
        assert row["is_active"] is False
        assert row["version"] == 2


def test_new_common_box_rejects_inactive_material_but_historical_link_is_readable(
    supplier_app: FastAPI,
) -> None:
    from fastapi import HTTPException

    from app.api.products import _validate_references
    from app.models.customer import Customer
    from app.models.material import Material

    with supplier_app.state.session_factory() as db:
        customer_id = db.scalar(
            select(Customer.id).where(Customer.customer_code == "INACTIVE-MATERIAL-UAT")
        )
        material_id = db.scalar(select(Material.id).where(Material.code == "7RIR6"))

        with pytest.raises(HTTPException, match="所选材质已停用"):
            _validate_references(
                db,
                customer_id=customer_id,
                material_id=material_id,
                mold_tool_id=None,
            )

        # 历史常用箱维持原关联时仍可打开和保存其它字段；只禁止新增引用。
        _validate_references(
            db,
            customer_id=customer_id,
            material_id=material_id,
            mold_tool_id=None,
            historical_material_id=material_id,
        )


def test_admin_crud_conflicts_version_status_and_audit(
    supplier_app: FastAPI,
) -> None:
    from app.models.audit import OperationLog

    with TestClient(supplier_app) as client:
        _login(client, "supplier-sales")
        assert (
            client.post(
                "/api/master/suppliers",
                json={"standard_name": "无权新增"},
            ).status_code
            == 403
        )

        _login(client, "supplier-admin")
        created = client.post(
            "/api/master/suppliers",
            json={
                "standard_name": "苏州测试纸板有限公司",
                "display_name": "测试纸板",
                "business_code": "  cs-01 ",
                "contact_name": "测试联系人",
                "phone": "123",
                "remarks": "仅测试",
                "sort_order": 35,
                "aliases": ["测试供应商"],
            },
        )
        assert created.status_code == 201
        row = created.json()
        assert row["business_code"] == "CS-01"
        assert row["aliases"] == ["测试供应商"]
        assert row["version"] == 1

        duplicate_alias = client.post(
            "/api/master/suppliers",
            json={
                "standard_name": "另一个供应商",
                "aliases": ["测试供应商"],
            },
        )
        assert duplicate_alias.status_code == 409
        assert "别名" in duplicate_alias.json()["detail"]
        duplicate_code = client.post(
            "/api/master/suppliers",
            json={
                "standard_name": "代码冲突供应商",
                "business_code": "cs-01",
            },
        )
        assert duplicate_code.status_code == 409
        assert "业务代码" in duplicate_code.json()["detail"]

        no_change_payload = {
            "standard_name": row["standard_name"],
            "display_name": row["display_name"],
            "business_code": row["business_code"],
            "contact_name": row["contact_name"],
            "phone": row["phone"],
            "remarks": row["remarks"],
            "sort_order": row["sort_order"],
            "aliases": row["aliases"],
            "expected_version": row["version"],
        }
        unchanged = client.put(
            f"/api/master/suppliers/{row['id']}",
            json=no_change_payload,
        )
        assert unchanged.status_code == 200
        assert unchanged.json()["version"] == 1

        updated = client.put(
            f"/api/master/suppliers/{row['id']}",
            json={
                **no_change_payload,
                "standard_name": "苏州测试纸板新名称",
                "aliases": ["测试供应商"],
            },
        )
        assert updated.status_code == 200
        updated_row = updated.json()
        assert updated_row["version"] == 2
        assert "苏州测试纸板有限公司" in updated_row["aliases"]
        stale = client.put(
            f"/api/master/suppliers/{row['id']}",
            json=no_change_payload,
        )
        assert stale.status_code == 409
        assert "请刷新" in stale.json()["detail"]

        disabled = client.put(
            f"/api/master/suppliers/{row['id']}/status",
            json={"expected_version": 2, "is_active": False},
        )
        assert disabled.status_code == 200
        assert disabled.json()["version"] == 3
        candidate_ids = {
            item["id"]
            for item in client.get(
                "/api/master/suppliers/candidates"
            ).json()["items"]
        }
        assert row["id"] not in candidate_ids

    with supplier_app.state.session_factory() as db:
        logs = db.scalar(
            select(func.count(OperationLog.id)).where(
                OperationLog.resource == "Supplier",
                OperationLog.entity_id == row["id"],
            )
        )
        assert logs == 3


def test_supplier_standard_name_conflicts_with_other_alias(
    supplier_app: FastAPI,
) -> None:
    with TestClient(supplier_app) as client:
        _login(client, "supplier-admin")
        response = client.post(
            "/api/master/suppliers",
            json={"standard_name": "嘉林亿"},
        )
        assert response.status_code == 409
        assert "标准名称或别名" in response.json()["detail"]


def test_supplier_conditional_update_rejects_second_same_version_writer(
    supplier_app: FastAPI,
) -> None:
    from fastapi import HTTPException

    from app.api.suppliers import _atomic_supplier_update
    from app.models.supplier import Supplier

    factory = supplier_app.state.session_factory
    with factory() as seed:
        supplier_id = seed.scalar(
            select(Supplier.id).where(Supplier.standard_name == "胜源")
        )

    first = factory()
    second = factory()
    try:
        first_row = first.get(Supplier, supplier_id)
        second_row = second.get(Supplier, supplier_id)
        assert first_row.version == second_row.version == 1

        _atomic_supplier_update(
            first,
            supplier=first_row,
            expected_version=1,
            values={"remarks": "第一位操作人保存"},
        )
        first.commit()

        with pytest.raises(HTTPException) as error:
            _atomic_supplier_update(
                second,
                supplier=second_row,
                expected_version=1,
                values={"remarks": "第二位操作人覆盖"},
            )
        assert error.value.status_code == 409
        assert "请刷新" in error.value.detail
    finally:
        first.close()
        second.close()

    with factory() as verify:
        current = verify.get(Supplier, supplier_id)
        assert current.version == 2
        assert current.remarks == "第一位操作人保存"


def test_supplier_unique_races_are_returned_as_409_and_rolled_back(
    supplier_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.api.suppliers as supplier_api
    from app.models.supplier import Supplier

    monkeypatch.setattr(
        supplier_api,
        "_raise_identity_conflicts",
        lambda *args, **kwargs: None,
    )

    with TestClient(supplier_app) as client:
        _login(client, "supplier-admin")
        duplicate_create = client.post(
            "/api/master/suppliers",
            json={"standard_name": "胜源"},
        )
        assert duplicate_create.status_code == 409

        suppliers = client.get("/api/master/suppliers").json()["items"]
        forest = next(
            row for row in suppliers if row["standard_name"] == "森林阳光"
        )
        alias_race = client.put(
            f"/api/master/suppliers/{forest['id']}",
            json={
                "standard_name": forest["standard_name"],
                "display_name": forest["display_name"],
                "business_code": forest["business_code"],
                "contact_name": forest["contact_name"],
                "phone": forest["phone"],
                "remarks": "该修改必须整体回滚",
                "sort_order": forest["sort_order"],
                "aliases": ["嘉林亿"],
                "expected_version": forest["version"],
            },
        )
        assert alias_race.status_code == 409

    with supplier_app.state.session_factory() as db:
        forest = db.scalar(
            select(Supplier).where(Supplier.standard_name == "森林阳光")
        )
        assert forest.version == 1
        assert forest.remarks is None
        assert forest.aliases == []


def test_material_write_entrances_reject_unknown_or_inactive_suppliers(
    supplier_app: FastAPI,
) -> None:
    from app.models.material import Material
    from app.models.supplier_paper_code import SupplierPaperCode

    with TestClient(supplier_app) as client:
        _login(client, "supplier-admin")

        for supplier_name, message in (
            ("从未建档供应商", "尚未建档"),
            ("佳丰", "已停用"),
        ):
            material = client.post(
                "/api/master/materials",
                json={
                    "code": f"BLOCK-{supplier_name}",
                    "supplier_name": supplier_name,
                    "layer_count": 3,
                },
            )
            assert material.status_code == 400
            assert message in material.json()["detail"]

            paper = client.post(
                "/api/master/materials/paper-codes",
                json={
                    "supplier_name": supplier_name,
                    "code_char": "X",
                    "paper_name": "禁止写入测试纸",
                    "gram_weight": 120,
                },
            )
            assert paper.status_code == 400
            assert message in paper.json()["detail"]

            composed = client.post(
                "/api/master/materials/compose/save",
                json={
                    "supplier_name": supplier_name,
                    "material_code": "XXX",
                    "layer_count": 3,
                    "quote_price": 1.2,
                    "parsed_supplier_name": supplier_name,
                    "parsed_material_code": "XXX",
                    "parsed_layer_count": 3,
                    "price_source": "manual",
                },
            )
            assert composed.status_code == 400
            assert message in composed.json()["detail"]

            adjustment = client.post(
                "/api/master/materials/price-adjustments/preview",
                json={
                    "supplier_name": supplier_name,
                    "adjust_percent": "5",
                },
            )
            assert adjustment.status_code == 400
            assert message in adjustment.json()["detail"]

            rule = client.post(
                "/api/master/materials/flute-price-rules",
                json={
                    "supplier_name": supplier_name,
                    "layer_count": 3,
                    "flute_type": "A",
                    "price_delta": 0.05,
                },
            )
            assert rule.status_code == 400
            assert message in rule.json()["detail"]

        alias_material = client.post(
            "/api/master/materials",
            json={
                "code": "ACTIVE-ALIAS",
                "supplier_name": "嘉林亿",
                "layer_count": 3,
                "quote_price": 1.2,
            },
        )
        assert alias_material.status_code == 201, alias_material.text
        assert alias_material.json()["supplier_name"] == "苏州嘉林亿"

        alias_paper = client.post(
            "/api/master/materials/paper-codes",
            json={
                "supplier_name": "鸣朋",
                "code_char": "M",
                "paper_name": "鸣朋别名测试纸",
                "gram_weight": 120,
            },
        )
        assert alias_paper.status_code == 201, alias_paper.text
        assert alias_paper.json()["supplier_name"] == "昆山鸣朋"

        inactive_move = client.put(
            f"/api/master/materials/{alias_material.json()['id']}",
            json={
                "code": "ACTIVE-ALIAS",
                "supplier_name": "佳丰",
                "layer_count": 3,
                "quote_price": 1.2,
                "is_active": True,
                "expected_version": alias_material.json()["version"],
                "change_reason": "测试停用供应商门禁",
            },
        )
        assert inactive_move.status_code == 400
        assert "已停用" in inactive_move.json()["detail"]

        with supplier_app.state.session_factory() as db:
            legacy_material = db.scalar(
                select(Material).where(Material.code == "LEGACY-SUP")
            )
            legacy_paper = db.scalar(
                select(SupplierPaperCode).where(
                    SupplierPaperCode.code_char == "Z"
                )
            )
            legacy_material_id = legacy_material.id
            legacy_material_version = legacy_material.version
            legacy_paper_id = legacy_paper.id

        legacy_material_update = client.put(
            f"/api/master/materials/{legacy_material_id}",
            json={
                "code": "LEGACY-SUP",
                "supplier_name": "旧历史供应商",
                "layer_count": 3,
                "remarks": "仅维护旧资料备注",
                "is_active": True,
                "expected_version": legacy_material_version,
                "change_reason": "维护历史资料但不改供应商",
            },
        )
        assert legacy_material_update.status_code == 200, (
            legacy_material_update.text
        )
        assert (
            legacy_material_update.json()["supplier_name"]
            == "旧历史供应商"
        )

        legacy_paper_update = client.put(
            f"/api/master/materials/paper-codes/{legacy_paper_id}",
            json={
                "supplier_name": "旧历史供应商",
                "code_char": "Z",
                "paper_name": "历史纸种备注更新",
                "gram_weight": 120,
                "is_active": True,
            },
        )
        assert legacy_paper_update.status_code == 200
        assert (
            legacy_paper_update.json()["supplier_name"]
            == "旧历史供应商"
        )

    with supplier_app.state.session_factory() as db:
        assert db.scalar(
            select(Material).where(Material.code.like("BLOCK-%"))
        ) is None
        assert db.scalar(
            select(SupplierPaperCode).where(
                SupplierPaperCode.paper_name == "禁止写入测试纸"
            )
        ) is None
