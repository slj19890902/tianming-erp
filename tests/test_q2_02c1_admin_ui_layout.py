from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker


PASSWORD = "RolePass123!"


@pytest.fixture()
def ui_layout_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.api.auth import router as auth_router
    from app.api.deps import get_db
    from app.api.system import router as system_router
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.user import User

    database_path = tmp_path / "ui-layout.sqlite3"
    monkeypatch.setenv("ERP_DATABASE_PATH", str(database_path))
    engine = create_sqlite_engine(database_path)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        session.add_all(
            [
                User(username=role, password_hash=hash_password(PASSWORD), role=role, real_name=role)
                for role in ("admin", "finance", "sales", "workshop", "boss")
            ]
        )
        session.commit()

    app = FastAPI()

    def override_get_db():
        with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_get_db
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(system_router, prefix="/api/system")
    return app, session_factory


def _login(client: TestClient, username: str) -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200


def _logout(client: TestClient) -> None:
    client.post("/api/auth/logout")


def _admin_state(client: TestClient, role: str = "finance", mode: str = "standard") -> dict:
    response = client.get(
        "/api/system/ui-layout/admin",
        params={"role_code": role, "display_mode": mode},
    )
    assert response.status_code == 200
    return response.json()


def test_draft_isolated_publish_idempotent_restore_and_rollback(ui_layout_app) -> None:
    from app.models.audit import OperationLog
    from app.models.ui_layout_revision import UiLayoutRevision

    app, session_factory = ui_layout_app
    with TestClient(app) as client:
        _login(client, "admin")
        initial = _admin_state(client)
        assert initial["draft"]["version"] == 0
        assert initial["published"]["version"] == 0
        layout = deepcopy(initial["draft"]["layout"])
        layout["menus"] = list(reversed(layout["menus"]))
        next(item for item in layout["dashboard_cards"] if item["id"] == "pending_invoice")["visible"] = False
        draft_payload = {
            "role_code": "finance",
            "display_mode": "standard",
            "expected_draft_version": 0,
            "operation_key": "q2-draft-0001",
            "layout": layout,
        }
        saved = client.put("/api/system/ui-layout/admin/draft", json=draft_payload)
        assert saved.status_code == 200
        assert saved.json()["draft"]["version"] == 1
        replay = client.put("/api/system/ui-layout/admin/draft", json=draft_payload)
        assert replay.status_code == 200
        assert replay.json()["replayed"] is True

        _logout(client)
        _login(client, "finance")
        before_publish = client.get(
            "/api/system/ui-layout/effective", params={"display_mode": "standard"}
        )
        assert before_publish.status_code == 200
        assert before_publish.json()["version"] == 0
        assert "pending_invoice" in {
            item["id"] for item in before_publish.json()["layout"]["dashboard_cards"]
        }

        _logout(client)
        _login(client, "admin")
        publish_payload = {
            "role_code": "finance",
            "display_mode": "standard",
            "expected_draft_version": 1,
            "expected_release_version": 0,
            "operation_key": "q2-publish-0001",
        }
        published = client.post("/api/system/ui-layout/admin/publish", json=publish_payload)
        assert published.status_code == 200
        assert published.json()["published"]["version"] == 1
        assert published.json()["draft"]["version"] == 2
        assert published.json()["can_rollback"] is False
        publish_replay = client.post("/api/system/ui-layout/admin/publish", json=publish_payload)
        assert publish_replay.status_code == 200
        assert publish_replay.json()["replayed"] is True

        no_previous_release = client.post(
            "/api/system/ui-layout/admin/rollback",
            json={
                "role_code": "finance",
                "display_mode": "standard",
                "expected_draft_version": 2,
                "expected_release_version": 1,
                "operation_key": "q2-rollback-none",
            },
        )
        assert no_previous_release.status_code == 409

        _logout(client)
        _login(client, "finance")
        effective = client.get(
            "/api/system/ui-layout/effective", params={"display_mode": "standard"}
        ).json()
        assert effective["version"] == 1
        assert "pending_invoice" not in {
            item["id"] for item in effective["layout"]["dashboard_cards"]
        }
        assert effective["layout"]["menus"][0]["id"] == "master"

        _logout(client)
        _login(client, "admin")
        restored = client.post(
            "/api/system/ui-layout/admin/restore-default",
            json={
                "role_code": "finance",
                "display_mode": "standard",
                "expected_draft_version": 2,
                "expected_release_version": 1,
                "operation_key": "q2-default-0001",
            },
        )
        assert restored.status_code == 200
        assert restored.json()["published"]["version"] == 2
        assert next(
            item
            for item in restored.json()["published"]["layout"]["dashboard_cards"]
            if item["id"] == "pending_invoice"
        )["visible"] is True

        rolled_back = client.post(
            "/api/system/ui-layout/admin/rollback",
            json={
                "role_code": "finance",
                "display_mode": "standard",
                "expected_draft_version": 3,
                "expected_release_version": 2,
                "operation_key": "q2-rollback-0001",
            },
        )
        assert rolled_back.status_code == 200
        assert rolled_back.json()["published"]["version"] == 3
        assert next(
            item
            for item in rolled_back.json()["published"]["layout"]["dashboard_cards"]
            if item["id"] == "pending_invoice"
        )["visible"] is False

    with session_factory() as session:
        # One saved draft plus three release actions, each release action
        # appending a synchronized draft, yields seven immutable revisions.
        assert session.query(UiLayoutRevision).count() == 7
        logs = session.query(OperationLog).filter(OperationLog.module_code == "system").all()
        actions = [row.action_code for row in logs]
        assert actions.count("ui_layout.save_draft") == 1
        assert actions.count("ui_layout.publish") == 1
        assert actions.count("ui_layout.restore_default") == 1
        assert actions.count("ui_layout.rollback") == 1
        by_action = {row.action_code: row for row in logs}
        assert by_action["ui_layout.save_draft"].object_ref == "finance:standard:draft:v1"
        assert by_action["ui_layout.publish"].object_ref == "finance:standard:release:v1"
        assert by_action["ui_layout.restore_default"].object_ref == "finance:standard:release:v2"
        assert by_action["ui_layout.rollback"].object_ref == "finance:standard:release:v3"
        revision_keys = {
            row.operation_key
            for row in session.query(UiLayoutRevision)
            if row.operation_key is not None
        }
        assert {row.batch_id for row in logs} == revision_keys


def test_layout_rejects_unknown_duplicate_hidden_dashboard_and_stale_versions(ui_layout_app) -> None:
    app, _ = ui_layout_app
    with TestClient(app) as client:
        _login(client, "admin")
        state = _admin_state(client, role="sales")
        base = {
            "role_code": "sales",
            "display_mode": "standard",
            "expected_draft_version": 0,
            "operation_key": "q2-invalid-0001",
            "layout": deepcopy(state["draft"]["layout"]),
        }
        base["layout"]["menus"].append({"id": "arbitrary_html", "visible": True})
        assert client.put("/api/system/ui-layout/admin/draft", json=base).status_code == 422

        hidden = deepcopy(state["draft"]["layout"])
        next(item for item in hidden["menus"] if item["id"] == "dashboard")["visible"] = False
        base["layout"] = hidden
        base["operation_key"] = "q2-invalid-0002"
        assert client.put("/api/system/ui-layout/admin/draft", json=base).status_code == 422

        valid = deepcopy(state["draft"]["layout"])
        first = client.put(
            "/api/system/ui-layout/admin/draft",
            json={**base, "layout": valid, "operation_key": "q2-shared-key"},
        )
        assert first.status_code == 200
        changed = deepcopy(valid)
        changed["quick_actions"].reverse()
        conflict = client.put(
            "/api/system/ui-layout/admin/draft",
            json={
                **base,
                "layout": changed,
                "expected_draft_version": 1,
                "operation_key": "q2-shared-key",
            },
        )
        assert conflict.status_code == 409
        stale = client.put(
            "/api/system/ui-layout/admin/draft",
            json={
                **base,
                "layout": changed,
                "expected_draft_version": 0,
                "operation_key": "q2-stale-0001",
            },
        )
        assert stale.status_code == 409


def test_non_admin_cannot_read_admin_state_or_write_and_modes_are_isolated(ui_layout_app) -> None:
    from app.models.ui_layout_revision import UiLayoutRevision

    app, session_factory = ui_layout_app
    with TestClient(app) as client:
        _login(client, "finance")
        assert client.get(
            "/api/system/ui-layout/admin",
            params={"role_code": "finance", "display_mode": "standard"},
        ).status_code == 403
        response = client.post(
            "/api/system/ui-layout/admin/publish",
            json={
                "role_code": "finance",
                "display_mode": "standard",
                "expected_draft_version": 0,
                "expected_release_version": 0,
                "operation_key": "q2-denied-0001",
            },
        )
        assert response.status_code == 403
        _logout(client)
        _login(client, "admin")
        standard = _admin_state(client, role="admin", mode="standard")
        mobile = _admin_state(client, role="admin", mode="mobile")
        assert standard["published"]["version"] == mobile["published"]["version"] == 0

    with session_factory() as session:
        assert session.query(UiLayoutRevision).count() == 0


def test_mobile_production_requires_both_permissions_and_other_groups_keep_any_semantics(
    ui_layout_app,
) -> None:
    from app.services.ui_layout_settings import effective_layout

    _, session_factory = ui_layout_app
    with session_factory() as session:
        def menu_ids(role: str, mode: str, permissions: set[str]) -> set[str]:
            result = effective_layout(
                session,
                role_code=role,
                display_mode=mode,
                permissions=permissions,
            )
            return {item["id"] for item in result["layout"]["menus"]}

        assert "mobile_production" not in menu_ids(
            "workshop", "mobile", {"orders.view"}
        )
        assert "mobile_production" not in menu_ids(
            "workshop", "mobile", {"incoming.view"}
        )
        assert "mobile_production" not in menu_ids("workshop", "mobile", set())
        assert "mobile_production" in menu_ids(
            "workshop", "mobile", {"orders.view", "incoming.view"}
        )
        assert "workbench" in menu_ids("admin", "standard", {"orders.view"})


def test_operation_key_matches_audit_boundary_without_truncation(ui_layout_app) -> None:
    from app.models.audit import OperationLog
    from app.models.ui_layout_revision import UiLayoutRevision

    app, session_factory = ui_layout_app
    accepted_key = "k" * 64
    rejected_key = "x" * 65
    with TestClient(app) as client:
        _login(client, "admin")
        state = _admin_state(client, role="boss")
        accepted = client.put(
            "/api/system/ui-layout/admin/draft",
            json={
                "role_code": "boss",
                "display_mode": "standard",
                "expected_draft_version": 0,
                "operation_key": accepted_key,
                "layout": state["draft"]["layout"],
            },
        )
        assert accepted.status_code == 200
        replay = client.put(
            "/api/system/ui-layout/admin/draft",
            json={
                "role_code": "boss",
                "display_mode": "standard",
                "expected_draft_version": 0,
                "operation_key": accepted_key,
                "layout": state["draft"]["layout"],
            },
        )
        assert replay.status_code == 200
        assert replay.json()["replayed"] is True
        rejected = client.put(
            "/api/system/ui-layout/admin/draft",
            json={
                "role_code": "boss",
                "display_mode": "standard",
                "expected_draft_version": 1,
                "operation_key": rejected_key,
                "layout": state["draft"]["layout"],
            },
        )
        assert rejected.status_code == 422

    with session_factory() as session:
        revisions = session.query(UiLayoutRevision).all()
        logs = (
            session.query(OperationLog)
            .filter(OperationLog.action_code == "ui_layout.save_draft")
            .all()
        )
        assert len(revisions) == len(logs) == 1
        assert revisions[0].operation_key == accepted_key
        assert logs[0].batch_id == accepted_key
        assert logs[0].object_ref == "boss:standard:draft:v1"
