from __future__ import annotations

import os
import socket
from decimal import Decimal
from sqlalchemy import inspect, text

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from phase1_postgres.api_engine import router as engine_router
from phase1_postgres.api_deliveries import router as deliveries_router
from phase1_postgres.api_master import router as master_router
from phase1_postgres.api_orders import router as orders_router
from phase1_postgres.api_receipts import router as receipts_router
from phase1_postgres.api_requisitions import router as requisitions_router
from phase1_postgres.api_statements import router as statements_router
from phase1_postgres.api_wms import router as wms_router
from phase1_postgres.database import engine as default_engine
from phase1_postgres.models import Base, Customer, FluteType, Material, Product


def allowed_cors_origins() -> list[str]:
    configured = os.getenv("ERP_ALLOWED_ORIGINS") or os.getenv("TM_ERP_ALLOWED_ORIGINS")
    if configured:
        return [origin.strip() for origin in configured.split(",") if origin.strip()]

    origins = {
        "http://127.0.0.1:5177",
        "http://localhost:5177",
        "http://127.0.0.1:5178",
        "http://localhost:5178",
    }
    try:
        hostname = socket.gethostname()
        for _, _, _, _, sockaddr in socket.getaddrinfo(hostname, None, family=socket.AF_INET):
            ip = sockaddr[0]
            if ip and not ip.startswith("127."):
                origins.add(f"http://{ip}:5177")
                origins.add(f"http://{ip}:5178")
    except OSError:
        pass
    return sorted(origins)


def ensure_sqlite_phase5_columns(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return
    inspector = inspect(engine)
    if "order_items" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("order_items")}
    if "production_source" not in columns:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE order_items ADD COLUMN production_source "
                    "VARCHAR(30) NOT NULL DEFAULT 'ORDER_REQUISITION'"
                )
            )
    if "products" in inspector.get_table_names():
        product_columns = {column["name"] for column in inspector.get_columns("products")}
        with engine.begin() as connection:
            if "historical_search_key" not in product_columns:
                connection.execute(text("ALTER TABLE products ADD COLUMN historical_search_key VARCHAR(500)"))
            if "historical_style_no" not in product_columns:
                connection.execute(text("ALTER TABLE products ADD COLUMN historical_style_no VARCHAR(120)"))
            if "historical_material_code" not in product_columns:
                connection.execute(text("ALTER TABLE products ADD COLUMN historical_material_code VARCHAR(120)"))


def seed_demo_master_data(engine: Engine) -> None:
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    with SessionLocal() as session:
        if session.scalar(select(Customer.id).limit(1)) is not None:
            return
        customer = Customer(
            customer_number=1,
            customer_code="THCJ",
            name="天华超净",
            short_name="天华",
            contact_person="王经理",
            phone="0512-88888888",
            address="苏州工业园区",
            payment_term_days=30,
            delivery_method="配送",
            default_tax_rate=Decimal("0.13"),
            note="Phase 3 演示客户",
            is_active=True,
        )
        flute = FluteType(
            code="AB",
            name="AB楞",
            add_width_mm=Decimal("8"),
            basis_weight_gsm=Decimal("120"),
            freight_rate=Decimal("0.035"),
            loss_rate=Decimal("0.03"),
            note="五层常用楞型",
            is_active=True,
        )
        material = Material(
            code="K=A",
            name="五层加强纸板",
            paper_composition="K纸=A纸",
            basis_weight_description="170g/130g/80g/170g/150g",
            layer_count=5,
            flute_type=flute,
            customer_square_price=Decimal("3.5000"),
            supplier_square_price=Decimal("2.8500"),
            note="Phase 3 演示材质",
            is_active=True,
        )
        product = Product(
            customer=customer,
            product_code="001A",
            customer_material_code="001A",
            product_name="001A外箱",
            material=material,
            flute_type=flute,
            length_mm=Decimal("450"),
            width_mm=Decimal("340"),
            height_mm=Decimal("300"),
            box_category="normal",
            box_style="0201",
            production_process="钉箱",
            default_score_line="340*110*340",
            default_cardboard_length_mm=Decimal("916"),
            default_cardboard_width_mm=Decimal("644"),
            default_unit_price=Decimal("3.6500"),
            note="Phase 3 演示常用箱",
            is_active=True,
        )
        session.add_all([customer, flute, material, product])
        session.commit()


def create_app(*, engine: Engine | None = None, seed_demo_data: bool = True) -> FastAPI:
    app_engine = engine or default_engine
    Base.metadata.create_all(app_engine)
    ensure_sqlite_phase5_columns(app_engine)
    if seed_demo_data:
        seed_demo_master_data(app_engine)

    app = FastAPI(title="天明 ERP Phase 3 Master Data API", version="0.3.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_cors_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
    app.include_router(engine_router)
    app.include_router(master_router)
    app.include_router(orders_router)
    app.include_router(requisitions_router)
    app.include_router(wms_router)
    app.include_router(deliveries_router)
    app.include_router(receipts_router)
    app.include_router(statements_router)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
