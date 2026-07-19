from __future__ import annotations

from contextlib import asynccontextmanager
from ipaddress import ip_address
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.httpsredirect import (
    HTTPSRedirectMiddleware as StarletteHTTPSRedirectMiddleware,
)
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

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
from app.api.master_data_versions import router as master_data_versions_router
from app.api.materials import router as materials_router
from app.api.orders import router as orders_router
from app.api.pricing import router as pricing_router
from app.api.products import router as products_router
from app.api.requisition import router as requisition_router
from app.api.quotations import router as quotations_router
from app.api.pdf_training import router as pdf_training_router
from app.api.production import router as production_router
from app.api.system import router as system_router
from app.api.warehouse import router as warehouse_router
from app.api.stocktake import router as stocktake_router
from app.api.tianhua_pre_delivery import (
    mobile_router as tianhua_mobile_router,
    router as tianhua_pre_delivery_router,
)
from app.core.config import load_settings


@asynccontextmanager
async def phase2_lifespan(_: FastAPI):
    # Alembic and init_db.py own schema/user initialization from Phase 2 onward.
    current = load_settings()
    print(f"BoxERP database: {current.database_path}")
    # 确保 PDF 训练样本存储目录存在（不进入 Git，.gitkeep 已追踪目录结构）
    _pdf_dir = Path(__file__).resolve().parent.parent / "data" / "pdf_training_samples"
    _pdf_dir.mkdir(parents=True, exist_ok=True)
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


class HSTSMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Strict-Transport-Security"] = "max-age=63072000"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        return response


class CookieOriginCSRFMiddleware(BaseHTTPMiddleware):
    """Reject cross-site production writes that carry the ERP session cookie."""

    SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

    def __init__(
        self,
        app,
        *,
        allowed_origins: tuple[str, ...],
        session_cookie_name: str,
    ) -> None:
        super().__init__(app)
        self.allowed_origins = frozenset(
            origin.rstrip("/") for origin in allowed_origins
        )
        self.session_cookie_name = session_cookie_name

    async def dispatch(self, request, call_next):
        if request.method in self.SAFE_METHODS:
            return await call_next(request)
        if request.cookies.get(self.session_cookie_name) is None:
            # Login and other unauthenticated requests cannot use an existing
            # browser session as a CSRF credential.
            return await call_next(request)

        origin = (request.headers.get("origin") or "").rstrip("/")
        if origin not in self.allowed_origins:
            return JSONResponse(
                status_code=403,
                content={"detail": "安全校验失败：请求来源无效，请刷新页面后重试。"},
            )
        return await call_next(request)


class HTTPSRedirectMiddleware(StarletteHTTPSRedirectMiddleware):
    """Keep the loopback liveness probe HTTP-only without weakening public HTTPS."""

    @staticmethod
    def _is_loopback_health(scope) -> bool:
        if (
            scope.get("type") != "http"
            or scope.get("path") != "/api/health"
            or scope.get("method") not in {"GET", "HEAD"}
        ):
            return False
        client = scope.get("client")
        if not client:
            return False
        try:
            if not ip_address(client[0]).is_loopback:
                return False
        except ValueError:
            return False
        headers = dict(scope.get("headers", ()))
        host_header = headers.get(b"host", b"").decode("latin-1")
        try:
            host = urlsplit(f"//{host_header}").hostname
            return host is not None and ip_address(host).is_loopback
        except ValueError:
            return False

    async def __call__(self, scope, receive, send):
        if self._is_loopback_health(scope):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


def database_health(current) -> JSONResponse:
    """Return an intentionally minimal public liveness result."""
    database_path = current.database_path
    if not database_path.is_file():
        return JSONResponse({"ok": False}, status_code=503)
    try:
        uri = f"file:{database_path.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as connection:
            connection.execute("SELECT 1")
    except sqlite3.Error:
        return JSONResponse({"ok": False}, status_code=503)
    return JSONResponse({"ok": True})


def apply_transport_security(application: FastAPI, current) -> None:
    """Install production-only transport controls around the API boundary."""
    if not current.is_production:
        return
    # Starlette wraps the last-added middleware outermost.  HTTPS redirect
    # therefore has to be registered before TrustedHost so an untrusted HTTP
    # Host is rejected instead of becoming the target of an open redirect.
    application.add_middleware(
        CookieOriginCSRFMiddleware,
        allowed_origins=current.allowed_origins,
        session_cookie_name=current.session_cookie_name,
    )
    application.add_middleware(HTTPSRedirectMiddleware)
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[
            *current.trusted_hosts,
            "127.0.0.1",
            "localhost",
            "[::1]",
        ],
    )
    application.add_middleware(HSTSMiddleware)
    if current.trusted_proxy_ips:
        application.add_middleware(
            ProxyHeadersMiddleware,
            trusted_hosts=list(current.trusted_proxy_ips),
        )


def create_app() -> FastAPI:
    application = legacy.app
    current = load_settings()
    application.router.lifespan_context = phase2_lifespan
    index_path = Path(__file__).resolve().parents[1] / "static" / "index.html"
    spa_page_paths = {
        "/dashboard",
        "/customers",
        "/quotations",
        "/products",
        "/orders",
        "/orders_legacy",
        "/production",
        "/requisition",
        "/incoming",
        "/deliveries",
        "/finance",
        "/system",
    }
    for page_path in spa_page_paths:
        if not any(route.path == page_path for route in application.routes):
            application.add_api_route(
                page_path,
                lambda path=index_path: FileResponse(path),
                methods=["GET"],
                include_in_schema=False,
            )
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
    if not any(route.path == "/quotation-print.html" for route in application.routes):
        quotation_print_path = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "quotation-print.html"
        )
        application.add_api_route(
            "/quotation-print.html",
            lambda: FileResponse(quotation_print_path),
            methods=["GET"],
            include_in_schema=False,
        )
    if not any(route.path == "/mobile/tianhua-pick" for route in application.routes):
        mobile_pick_path = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "mobile_tianhua_pick.html"
        )
        application.add_api_route(
            "/mobile/tianhua-pick",
            lambda: FileResponse(mobile_pick_path),
            methods=["GET"],
            include_in_schema=False,
        )
    if not any(route.path == "/mobile/mold-lookup" for route in application.routes):
        mobile_mold_path = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "mobile_mold_lookup.html"
        )
        application.add_api_route(
            "/mobile/mold-lookup",
            lambda: FileResponse(mobile_mold_path),
            methods=["GET"],
            include_in_schema=False,
        )
    if not any(route.path == "/mobile/stocktake.html" for route in application.routes):
        mobile_stocktake_path = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "mobile_stocktake.html"
        )
        application.add_api_route(
            "/mobile/stocktake.html",
            lambda: FileResponse(mobile_stocktake_path),
            methods=["GET"],
            include_in_schema=False,
        )
    if not any(route.path == "/mold-label.html" for route in application.routes):
        mold_label_path = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "mold-label.html"
        )
        application.add_api_route(
            "/mold-label.html",
            lambda: FileResponse(mold_label_path),
            methods=["GET"],
            include_in_schema=False,
        )
    if not any(route.path == "/warehouse.html" for route in application.routes):
        warehouse_path = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "warehouse.html"
        )
        application.add_api_route(
            "/warehouse.html",
            lambda: FileResponse(warehouse_path),
            methods=["GET"],
            include_in_schema=False,
        )

    # ``legacy.app`` is a shared application instance.  Its API routes do not
    # participate in the authenticated router layer, so rebuild the complete
    # API boundary below instead of removing individual legacy paths.
    application.router.routes[:] = [
        route
        for route in application.router.routes
        if not (
            getattr(route, "path", "") == "/api"
            or getattr(route, "path", "").startswith("/api/")
        )
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
    if not any(
        route.path == "/api/master-data/{object_type}/{object_id}/versions"
        for route in application.routes
    ):
        application.include_router(
            master_data_versions_router,
            prefix="/api/master-data",
            tags=["master-data-versions"],
        )

    application.add_api_route(
        "/api/health",
        lambda: database_health(current),
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
    if not any(route.path == "/api/production/tasks" for route in application.routes):
        application.include_router(
            production_router,
            prefix="/api/production",
            tags=["production"],
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
    if not any(route.path == "/api/deliveries/tianhua-preimport/upload" for route in application.routes):
        application.include_router(
            tianhua_pre_delivery_router,
            prefix="/api/deliveries",
            tags=["tianhua-pre-delivery"],
        )
    if not any(route.path == "/api/mobile/tianhua-pick" for route in application.routes):
        application.include_router(
            tianhua_mobile_router,
            prefix="/api/mobile",
            tags=["tianhua-mobile-pick"],
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
    if not any(route.path == "/api/quotations" for route in application.routes):
        application.include_router(
            quotations_router,
            prefix="/api/quotations",
            tags=["quotations"],
        )
    if not any(route.path == "/api/system/backups" for route in application.routes):
        application.include_router(
            system_router,
            prefix="/api/system",
            tags=["system"],
        )
    if not any(route.path == "/api/pdf-training/stats" for route in application.routes):
        application.include_router(
            pdf_training_router,
            prefix="/api/pdf-training",
            tags=["pdf-training"],
        )
    if not any(route.path == "/api/warehouse/locations" for route in application.routes):
        application.include_router(
            warehouse_router,
            prefix="/api/warehouse",
            tags=["warehouse"],
        )
    if not any(
        route.path == "/api/warehouse/stocktake/locations"
        for route in application.routes
    ):
        application.include_router(
            stocktake_router,
            prefix="/api/warehouse",
            tags=["warehouse-stocktake"],
        )

    # create_app() reconfigures the legacy singleton.  TestClient builds and
    # caches middleware_stack, so invalidate it before calling add_middleware
    # again; doing this afterwards raises "Cannot add middleware...".
    application.middleware_stack = None
    transport_middleware = {
        CORSMiddleware,
        TrustedHostMiddleware,
        HTTPSRedirectMiddleware,
        HSTSMiddleware,
        CookieOriginCSRFMiddleware,
        ProxyHeadersMiddleware,
    }
    application.user_middleware = [
        middleware
        for middleware in application.user_middleware
        if middleware.cls not in transport_middleware
    ]
    legacy.db_path = lambda: current.database_path
    application.debug = False
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(current.allowed_origins),
        allow_origin_regex=current.allowed_origin_regex,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
    apply_production_security(application, current)
    apply_transport_security(application, current)
    return application


app = create_app()
legacy.app = app
