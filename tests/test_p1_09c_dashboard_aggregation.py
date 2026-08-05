from __future__ import annotations

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.orm import sessionmaker


INDEX = Path("static/index.html").read_text(encoding="utf-8")


def _block(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def test_dashboard_cold_entry_uses_overview_embedded_kpi() -> None:
    load_page = _block(INDEX, "async loadPage(page", "refreshCurrent()")
    load_overview = _block(INDEX, "async loadOverview()", "async loadKpi()")

    assert 'if (page === "dashboard") await this.loadOverview();' in load_page
    assert "loadKpi()" not in load_page.split(
        'if (page === "dashboard")', 1
    )[1].split('if (page === "customers")', 1)[0]
    assert "this.kpi = data.kpi" in load_overview
    assert "await this.loadKpi()" in load_overview


def test_overview_embeds_exact_kpi_without_second_workflow_projection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import app.api.dashboard as dashboard_api
    from app.core.database import create_sqlite_engine
    from app.core.security import hash_password
    from app.models import Base
    from app.models.user import User

    engine = create_sqlite_engine(tmp_path / "dashboard-single-aggregate.sqlite3")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        user = User(
            username="admin",
            password_hash=hash_password("RolePass123!"),
            role="admin",
            real_name="管理员",
            must_change_password=False,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        user_id = int(user.id)

    projection_calls: list[dict] = []
    original_projection = dashboard_api._workflow_projection_rows

    def counted_projection(*args, **kwargs):
        projection_calls.append(dict(kwargs))
        return original_projection(*args, **kwargs)

    monkeypatch.setattr(
        dashboard_api,
        "_workflow_projection_rows",
        counted_projection,
    )
    writes: list[str] = []

    def record_writes(_conn, _cursor, statement, _params, _context, _many):
        verb = statement.lstrip().split(None, 1)[0].upper()
        if verb in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(verb)

    event.listen(engine, "before_cursor_execute", record_writes)
    try:
        with factory() as session:
            user = session.get(User, user_id)
            assert user is not None
            overview = dashboard_api.dashboard_overview(db=session, user=user)
            assert len(projection_calls) == 1
            embedded_kpi = overview["kpi"]

            independent_kpi = dashboard_api.dashboard_kpi(db=session, user=user)
            assert len(projection_calls) == 2
    finally:
        event.remove(engine, "before_cursor_execute", record_writes)
        engine.dispose()

    assert embedded_kpi == independent_kpi
    assert writes == []
