from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool


def make_client():
    from phase1_postgres.database import get_session
    from phase1_postgres.main import create_app
    from phase1_postgres.models import Base

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    app = create_app(engine=engine, seed_demo_data=True)

    def override_session():
        from sqlalchemy.orm import Session

        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    return TestClient(app), engine


def test_import_excel_history_maps_super_k_column_to_product_formula(tmp_path: Path):
    from sqlalchemy.orm import Session

    from phase1_postgres.models import Product
    from scripts.import_excel_history import import_csv_to_products, parse_csv

    client, engine = make_client()
    csv_path = tmp_path / "2025年采购单.xlsx - 2020.1-2025.csv"
    csv_path.write_text(
        "\n".join(
            [
                "纸长,纸宽,压线,数量,材质,备注,公用款号,空列,I备注,空列,K列",
                "159,78.5,33.8*10.9*33.8,200,D212D,,001A,,,,'21301028 88*67*11 天华超净'",
                "160,79,34*11*34,180,D212D,,001A,,,,'21301028 88*67*11 天华超净'",
                "120,60,30*20*30,50,W7B/E,,MP001,,,,'洛普格 38*26*20'",
            ]
        ),
        encoding="utf-8-sig",
    )

    records = parse_csv(csv_path)
    assert len(records) == 2
    assert records[0].search_keyword == "21301028 88*67*11 天华超净"
    assert records[0].paper_length_mm == 160
    assert records[0].paper_width_mm == 79
    assert records[0].score_line == "34*11*34"
    assert records[0].material_code == "D212D"

    with Session(engine) as session:
        summary = import_csv_to_products(csv_path, session, commit=True)
        assert summary["created"] == 1
        assert summary["updated"] == 1

        imported = (
            session.query(Product)
            .filter(Product.historical_search_key.like("%21301028%"))
            .one()
        )
        assert imported.customer.name == "天华超净"
        assert imported.historical_style_no == "001A"
        assert imported.default_cardboard_length_mm == 160
        assert imported.default_cardboard_width_mm == 79
        assert imported.default_score_line == "34*11*34"
        assert imported.default_material_text == "D212D"

    response = client.get("/api/master/products/history-search", params={"keyword": "213"})
    assert response.status_code == 200
    body = response.json()
    assert body["matched"] is True
    assert len(body["items"]) >= 1
    assert body["item"]["historical_search_key"] == "21301028 88*67*11 天华超净"
    assert body["items"][0]["paper_length_mm"] == "160.00"
    assert body["item"]["paper_length_mm"] == "160.00"
    assert body["item"]["paper_width_mm"] == "79.00"
    assert body["item"]["score_line"] == "34*11*34"
    assert body["item"]["material"] == "D212D"
