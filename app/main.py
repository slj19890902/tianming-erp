from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import sqlite3

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

import main as legacy
from app.api.auth import router as auth_router
from app.api.customers import router as customers_router
from app.api.deliveries import (
    order_actions_router,
    router as deliveries_router,
)
from app.api.dashboard import router as dashboard_router
from app.api.finance import router as finance_router
from app.api.incoming import router as incoming_router
from app.api.materials import router as materials_router
from app.api.orders import router as orders_router
from app.api.pricing import router as pricing_router
from app.api.products import router as products_router
from app.api.requisition import router as requisition_router
from app.api.system import router as system_router
from app.core.config import load_settings


@asynccontextmanager
async def phase2_lifespan(_: FastAPI):
    # Alembic and init_db.py own schema/user initialization from Phase 2 onward.
    current = load_settings()
    print(f"BoxERP database: {current.database_path}")
    yield


def apply_production_security(application: FastAPI, current) -> None:
    if not current.is_production:
        return
    hidden_paths = {"/docs", "/redoc", "/openapi.json"}
    application.router.routes[:] = [
        route
        for route in application.router.routes
        if getattr(route, "path", None) not in hidden_paths
    ]
    application.openapi_url = None
    application.docs_url = None
    application.redoc_url = None


def database_health(current) -> dict[str, object]:
    database_path = current.database_path.resolve()
    result: dict[str, object] = {
        "ok": True,
        "database": str(database_path),
        "orders_table": "sales_orders",
        "orders_count": None,
        "order_items_count": None,
    }
    if not database_path.is_file():
        return {**result, "ok": False, "database_error": "database file not found"}
    try:
        uri = f"file:{database_path.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "sales_orders" in tables:
                result["orders_count"] = int(
                    connection.execute("SELECT COUNT(*) FROM sales_orders").fetchone()[0]
                )
            if "sales_order_items" in tables:
                result["order_items_count"] = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM sales_order_items"
                    ).fetchone()[0]
                )
    except sqlite3.Error as error:
        return {**result, "ok": False, "database_error": str(error)}
    return result


def create_app() -> FastAPI:
    application = legacy.app
    current = load_settings()
    application.router.lifespan_context = phase2_lifespan
    if not any(
        route.path == "/requisition-print.html"
        for route in application.routes
    ):
        print_path = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "requisition-print.html"
        )
        application.add_api_route(
            "/requisition-print.html",
            lambda: FileResponse(print_path),
            methods=["GET"],
            include_in_schema=False,
        )

    application.router.routes[:] = [
        route
        for route in application.router.routes
        if route.path not in {"/api/orders", "/api/health", "/api/customers"}
    ]

    if not any(route.path == "/api/auth/login" for route in application.routes):
        application.include_router(
            auth_router,
            prefix="/api/auth",
            tags=["auth"],
        )
    router_specs = (
        ("/api/customers", customers_router, "customers"),
        ("/api/master/customers", customers_router, "master-customers"),
        ("/api/master/materials", materials_router, "master-materials"),
        ("/api/master/products", products_router, "master-products"),
    )
    existing_paths = {route.path for route in application.routes}
    for prefix, router, tag in router_specs:
        if prefix not in existing_paths:
            application.include_router(router, prefix=prefix, tags=[tag])

    application.add_api_route(
        "/api/health",
        lambda: JSONResponse(database_health(current)),
        methods=["GET"],
        include_in_schema=True,
    )
    if not any(
        route.path == "/api/orders"
        and "POST" in getattr(route, "methods", set())
        for route in application.routes
    ):
        application.include_router(
            orders_router,
            prefix="/api/orders",
            tags=["orders"],
        )
    if not any(route.path == "/api/pricing/calculate" for route in application.routes):
        application.include_router(
            pricing_router,
            prefix="/api/pricing",
            tags=["pricing"],
        )
    if not any(route.path == "/api/incoming/pending" for route in application.routes):
        application.include_router(
            incoming_router,
            prefix="/api/incoming",
            tags=["incoming"],
        )
    if not any(route.path == "/api/requisition/pending" for route in application.routes):
        application.include_router(
            requisition_router,
            prefix="/api/requisition",
            tags=["requisition"],
        )
    if not any(
        route.path == "/api/deliveries/pending_items"
        for route in application.routes
    ):
        application.include_router(
            deliveries_router,
            prefix="/api/deliveries",
            tags=["deliveries"],
        )
    if not any(
        route.path == "/api/orders/items/{item_id}/force_close"
        for route in application.routes
    ):
        application.include_router(
            order_actions_router,
            prefix="/api/orders",
            tags=["order-delivery-actions"],
        )
    if not any(
        route.path == "/api/finance/return_receipts"
        for route in application.routes
    ):
        application.include_router(
            finance_router,
            prefix="/api/finance",
            tags=["finance"],
        )
    if not any(route.path == "/api/dashboard/kpi" for route in application.routes):
        application.include_router(
            dashboard_router,
            prefix="/api/dashboard",
            tags=["dashboard"],
        )
    if not any(route.path == "/api/system/backups" for route in application.routes):
        application.include_router(
            system_router,
            prefix="/api/system",
            tags=["system"],
        )

    application.user_middleware = [
        middleware
        for middleware in application.user_middleware
        if middleware.cls is not CORSMiddleware
    ]
    legacy.db_path = lambda: current.database_path
    application.debug = False
    apply_production_security(application, current)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(current.allowed_origins),
        allow_origin_regex=current.allowed_origin_regex,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
    application.middleware_stack = None
    return application


app = create_app()
