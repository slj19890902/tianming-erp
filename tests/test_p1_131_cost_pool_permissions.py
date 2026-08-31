from __future__ import annotations

from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import select

from tests.test_p1_131_cost_pool import (
    _entry_payload,
    _login,
    p1_131_cost_app,
)


def _seed_finance_and_boss_users(factory) -> None:
    from app.core.security import hash_password
    from app.models.user import User

    with factory() as db:
        db.add_all(
            [
                User(
                    username="p1131-finance",
                    password_hash=hash_password("RolePass123!"),
                    role="finance",
                    real_name="P1-131 Finance",
                    must_change_password=False,
                ),
                User(
                    username="p1131-boss",
                    password_hash=hash_password("RolePass123!"),
                    role="boss",
                    real_name="P1-131 Boss",
                    must_change_password=False,
                ),
            ]
        )
        db.commit()


def test_p1_131_cost_permission_matrix_is_least_privilege(
    p1_131_cost_app,
) -> None:
    app, factory = p1_131_cost_app
    _seed_finance_and_boss_users(factory)

    with TestClient(app) as client:
        _login(client, "p1131-finance")
        created = client.post(
            "/api/finance/cost-pool",
            json=_entry_payload(key="p1131-finance-create"),
        )
        assert created.status_code == 201, created.text
        confirmed = client.post(
            f"/api/finance/cost-pool/{created.json()['id']}/confirm",
            json={
                "expected_version": 1,
                "idempotency_key": "p1131-finance-confirm",
            },
        )
        assert confirmed.status_code == 200, confirmed.text

        boss_confirm_target = client.post(
            "/api/finance/cost-pool",
            json=_entry_payload(
                key="p1131-boss-confirm-target",
                description="老板不可确认的草稿",
            ),
        )
        assert boss_confirm_target.status_code == 201, boss_confirm_target.text

        _login(client, "p1131-boss")
        assert client.get(
            "/api/finance/cost-pool", params={"month": "2026-08"}
        ).status_code == 200
        assert client.get(
            "/api/finance/cost-pool/summary", params={"month": "2026-08"}
        ).status_code == 200
        assert client.get(
            "/api/finance/cost-pool/export", params={"month": "2026-08"}
        ).status_code == 200
        assert client.post(
            "/api/finance/cost-pool",
            json=_entry_payload(key="p1131-boss-create-denied"),
        ).status_code == 403
        assert client.put(
            "/api/finance/cost-centers/1",
            json={
                "name": "老板不可修改",
                "is_active": True,
                "expected_version": 1,
                "idempotency_key": "p1131-boss-center-denied",
            },
        ).status_code == 403
        assert client.post(
            f"/api/finance/cost-pool/{boss_confirm_target.json()['id']}/confirm",
            json={
                "expected_version": 1,
                "idempotency_key": "p1131-boss-confirm-denied",
            },
        ).status_code == 403

        _login(client, "p1131-restricted")
        assert client.get(
            "/api/finance/cost-pool", params={"month": "2026-08"}
        ).status_code == 403
        assert client.get(
            "/api/finance/cost-pool/summary", params={"month": "2026-08"}
        ).status_code == 403
        assert client.get(
            "/api/finance/cost-pool/export", params={"month": "2026-08"}
        ).status_code == 403
        assert client.post(
            "/api/finance/cost-pool",
            json=_entry_payload(key="p1131-selected-create-denied"),
        ).status_code == 403


def test_p1_131_cost_center_rename_preserves_entry_snapshot_everywhere(
    p1_131_cost_app,
) -> None:
    from app.models.finance_cost import FinanceCostCenter, FinanceCostPoolEntry

    app, factory = p1_131_cost_app
    _seed_finance_and_boss_users(factory)

    with TestClient(app) as client:
        _login(client, "p1131-finance")
        created = client.post(
            "/api/finance/cost-pool",
            json=_entry_payload(key="p1131-snapshot-create", amount="123.45"),
        )
        assert created.status_code == 201, created.text
        assert created.json()["cost_center_name"] == "生产成本"
        confirmed = client.post(
            f"/api/finance/cost-pool/{created.json()['id']}/confirm",
            json={
                "expected_version": 1,
                "idempotency_key": "p1131-snapshot-confirm",
            },
        )
        assert confirmed.status_code == 200, confirmed.text

        renamed = client.put(
            "/api/finance/cost-centers/1",
            json={
                "name": "生产制造中心（新名）",
                "is_active": True,
                "expected_version": 1,
                "idempotency_key": "p1131-center-rename",
            },
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["name"] == "生产制造中心（新名）"

        entries = client.get(
            "/api/finance/cost-pool", params={"month": "2026-08"}
        )
        assert entries.status_code == 200, entries.text
        assert entries.json()["items"][0]["cost_center_name"] == "生产成本"

        summary = client.get(
            "/api/finance/cost-pool/summary", params={"month": "2026-08"}
        )
        assert summary.status_code == 200, summary.text
        assert summary.json()["by_center"][0]["center_name"] == "生产成本"

        exported = client.get(
            "/api/finance/cost-pool/export",
            params={"month": "2026-08", "status": "confirmed"},
        )
        assert exported.status_code == 200, exported.text
        workbook = load_workbook(BytesIO(exported.content), data_only=True)
        assert workbook["成本费用明细"]["F2"].value == "生产成本"

    with factory() as db:
        center = db.get(FinanceCostCenter, 1)
        entry = db.scalar(select(FinanceCostPoolEntry))
        assert center is not None
        assert entry is not None
        assert center.name == "生产制造中心（新名）"
        assert entry.cost_center_code_snapshot == "PROD"
        assert entry.cost_center_name_snapshot == "生产成本"
        assert entry.cost_center_type_snapshot == "production"
