from __future__ import annotations

from contextlib import asynccontextmanager
from ipaddress import ip_address
import os
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.httpsredirect import (
    HTTPSRedirectMiddleware as StarletteHTTPSRedirectMiddleware,
)
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import main as legacy
from app.api.audit import router as audit_router
from app.api.auth import router as auth_router
from app.api.ai_assistant import router as ai_assistant_router
from app.api.ai_assistant import router as ai_assistant_router
from app.api.customers import router as customers_router
from app.api.deliveries import (
    order_actions_router,
    pick_router,
    router as deliveries_router,
)
from app.api.dashboard import router as dashboard_router
from app.api.finance import router as finance_router
from app.api.supplier_settlements import router as supplier_settlements_router
from app.api.cost_accounting import router as cost_accounting_router
from app.api.finance_simplified import router as finance_simplified_router
from app.api.processing_cost import router as processing_cost_router
from app.api.invoice_tasks import (
    customer_router as invoice_customer_router,
    router as invoice_tasks_router,
)
from app.api.incoming import router as incoming_router
from app.api.mobile_erp import router as mobile_erp_router
from app.api.master_data_versions import router as master_data_versions_router
from app.api.materials import router as materials_router
from app.api.suppliers import router as suppliers_router
from app.api.external_packaging_prices import router as external_packaging_prices_router
from app.api.external_packaging_components import router as external_packaging_components_router
from app.api.external_packaging_purchases import router as external_packaging_purchases_router
from app.api.orders import router as orders_router
from app.api.pricing import router as pricing_router
from app.api.products import (
    box_type_rules_router,
    router as products_router,
)
from app.api.product_import import router as product_import_router
from app.api.requisition import router as requisition_router
from app.api.quotations import router as quotations_router
from app.api.contracts import router as contracts_router
from app.api.pdf_training import router as pdf_training_router
from app.api.production import router as production_router
from app.api.system import router as system_router
from app.api.warehouse import router as warehouse_router
from app.api.fixed_shelf import router as fixed_shelf_router
from app.api.stocktake import router as stocktake_router
from app.api.inventory_onboarding import router as inventory_onboarding_router
from app.api.tianhua_pre_delivery import (
    mobile_router as tianhua_mobile_router,
    router as tianhua_pre_delivery_router,
)
from app.core.config import is_lan_http_scope, load_settings
from app.core.uat_isolation import write_uat_attestation
from app.middleware.performance import (
    PerformanceObservabilityMiddleware,
    slow_request_threshold_ms,
)
from app.middleware.mold_private import MoldPrivateNoStoreMiddleware
from app.middleware.private_uploads import PrivateUploadGuardMiddleware
from app.web_assets import SelectiveGZipMiddleware, conditional_file_response


def _conditional_file_endpoint(path: Path):
    async def endpoint(request: Request):
        return conditional_file_response(request, path)

    return endpoint


def _private_no_store_file_endpoint(path: Path):
    async def endpoint(request: Request):
        return conditional_file_response(
            request,
            path,
            headers={
                "Cache-Control": "private, no-store, max-age=0",
                "Pragma": "no-cache",
                "Referrer-Policy": "no-referrer",
                "X-Robots-Tag": "noindex, nofollow",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return endpoint


@asynccontextmanager
async def phase2_lifespan(_: FastAPI):
    # Alembic and init_db.py own schema/user initialization from Phase 2 onward.
    current = load_settings()
    print(f"BoxERP database: {current.database_path}")
    # 确保 PDF 训练样本存储目录存在（不进入 Git，.gitkeep 已追踪目录结构）
    _pdf_dir = Path(
        os.getenv(
            "ERP_PDF_TRAINING_DIR",
            str(Path(__file__).resolve().parent.parent / "data" / "pdf_training_samples"),
        )
    )
    _pdf_dir.mkdir(parents=True, exist_ok=True)
    if os.getenv("ERP_UAT_ROOT"):
        from app.core.uat_isolation import validate_uat_process_ownership

        validate_uat_process_ownership()
    write_uat_attestation(current)
    # Only the formal runtime starts the draft scheduler. Test/UAT apps stay
    # explicit; the job uses the same configured DB, never a guessed path.
    import asyncio
    from app.core.database import SessionLocal
    from app.services.supplier_settlement_automation import settlement_loop
    from app.services.email_intake import automatic_sync_loop
    from app.services.email_pdf_queue import recognition_loop
    stop = asyncio.Event()
    jobs = []
    if current.is_production and not os.getenv("ERP_UAT_ROOT"):
        jobs.append(asyncio.create_task(settlement_loop(stop, SessionLocal)))
        jobs.append(asyncio.create_task(automatic_sync_loop(stop, SessionLocal)))
        jobs.append(asyncio.create_task(recognition_loop(stop, SessionLocal)))
    try:
        yield
    finally:
        stop.set()
        if jobs:
            await asyncio.gather(*jobs)


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
    def __init__(
        self, app, *, include_hsts: bool = True, lan_http_origin: str | tuple[str, ...] = ""
    ) -> None:
        super().__init__(app)
        self.include_hsts = include_hsts
        self.lan_http_origin = lan_http_origin

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if self.include_hsts and not is_lan_http_scope(request.scope, self.lan_http_origin):
            response.headers["Strict-Transport-Security"] = "max-age=63072000"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        same_origin_embedded_paths = {
            "/warehouse.html",
            "/incoming.html",
            "/mobile/delivery-pick.html",
            "/mobile/stocktake.html",
        }
        same_origin_embedded = (
            request.url.path in same_origin_embedded_paths
            and request.query_params.get("embedded") == "1"
        )
        if same_origin_embedded:
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
            response.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
        else:
            response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = (
            "camera=(self), microphone=(), geolocation=()"
            if request.url.path == "/mobile/scan"
            else "camera=(), microphone=(), geolocation=()"
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
        require_same_origin: bool = False,
    ) -> None:
        super().__init__(app)
        self.allowed_origins = frozenset(
            origin.rstrip("/") for origin in allowed_origins
        )
        self.session_cookie_name = session_cookie_name
        self.require_same_origin = require_same_origin

    async def dispatch(self, request, call_next):
        if request.method in self.SAFE_METHODS:
            return await call_next(request)
        if request.cookies.get(self.session_cookie_name) is None:
            # Login and other unauthenticated requests cannot use an existing
            # browser session as a CSRF credential.
            return await call_next(request)

        origin = (request.headers.get("origin") or "").rstrip("/")
        request_origin = f"{request.url.scheme}://{request.url.netloc}"
        if origin not in self.allowed_origins or (
            self.require_same_origin and origin != request_origin
        ):
            return JSONResponse(
                status_code=403,
                content={"detail": "安全校验失败：请求来源无效，请刷新页面后重试。"},
            )
        return await call_next(request)


class HTTPSRedirectMiddleware(StarletteHTTPSRedirectMiddleware):
    """Keep the loopback liveness probe HTTP-only without weakening public HTTPS."""

    def __init__(self, app, *, lan_http_origin: str | tuple[str, ...] = "") -> None:
        super().__init__(app)
        self.lan_http_origin = lan_http_origin

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
        if self._is_loopback_health(scope) or is_lan_http_scope(scope, self.lan_http_origin):
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
    # Starlette wraps the last-added middleware outermost. HTTPS redirect is
    # registered before TrustedHost so an untrusted HTTP Host is rejected
    # instead of becoming the target of an open redirect.
    application.add_middleware(
        CookieOriginCSRFMiddleware,
        allowed_origins=current.allowed_origins,
        session_cookie_name=current.session_cookie_name,
        require_same_origin=bool(current.private_http_origins),
    )
    if current.uses_https_proxy:
        application.add_middleware(
            HTTPSRedirectMiddleware, lan_http_origin=current.private_http_origins
        )
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[
            *current.trusted_hosts,
            "127.0.0.1",
            "localhost",
            "[::1]",
        ],
    )
    application.add_middleware(
        HSTSMiddleware,
        include_hsts=current.uses_https_proxy,
        lan_http_origin=current.private_http_origins,
    )
    if current.trusted_proxy_ips:
        application.add_middleware(
            ProxyHeadersMiddleware,
            trusted_hosts=list(current.trusted_proxy_ips),
        )


def create_app() -> FastAPI:
    application = legacy.app
    current = load_settings()
    # Settings and UAT path identities are immutable for a worker lifetime.
    # Keep the startup-validated snapshot on the app so authentication does
    # not repeat expensive Windows path/handle validation on every request.
    application.state.erp_settings = current
    application.router.lifespan_context = phase2_lifespan
    index_path = Path(__file__).resolve().parents[1] / "static" / "index.html"
    spa_page_paths = {
        "/dashboard",
        "/customers",
        "/contracts",
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
                _conditional_file_endpoint(index_path),
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
    if not any(
        route.path == "/requisition-production-print.html"
        for route in application.routes
    ):
        requisition_production_print_path = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "requisition-production-print.html"
        )
        application.add_api_route(
            "/requisition-production-print.html",
            _conditional_file_endpoint(requisition_production_print_path),
            methods=["GET"],
            include_in_schema=False,
        )
    if not any(
        route.path == "/incoming-production-card.html"
        for route in application.routes
    ):
        # Historical receipt-card links stay valid, while both paper phases
        # are rendered by the single production-task template.
        incoming_production_card_path = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "requisition-production-print.html"
        )
        application.add_api_route(
            "/incoming-production-card.html",
            _conditional_file_endpoint(incoming_production_card_path),
            methods=["GET"],
            include_in_schema=False,
        )
    if not any(
        route.path == "/production-packaging-label.html"
        for route in application.routes
    ):
        production_packaging_label_path = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "production-packaging-label.html"
        )
        application.add_api_route(
            "/production-packaging-label.html",
            _conditional_file_endpoint(production_packaging_label_path),
            methods=["GET"],
            include_in_schema=False,
        )
    for customer_print_asset in ('operation-key.js', 'customer-delivery-print.js', 'customer-delivery-print.css'):
        asset_path = Path(__file__).resolve().parents[1] / 'static' / customer_print_asset
        application.add_api_route('/' + customer_print_asset, _conditional_file_endpoint(asset_path),
                                  methods=['GET'], include_in_schema=False)
    if not any(
        route.path == "/delivery-print-designer.html"
        for route in application.routes
    ):
        delivery_print_designer_path = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "delivery-print-designer.html"
        )
        application.add_api_route(
            "/delivery-print-designer.html",
            _conditional_file_endpoint(delivery_print_designer_path),
            methods=["GET"],
            include_in_schema=False,
        )
    if not any(
        route.path == "/external-purchase-print.html"
        for route in application.routes
    ):
        external_purchase_print_path = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "external-purchase-print.html"
        )
        application.add_api_route(
            "/external-purchase-print.html",
            lambda: FileResponse(external_purchase_print_path),
            methods=["GET"],
            include_in_schema=False,
        )
    if not any(route.path == "/email-intake.html" for route in application.routes):
        email_path = Path(__file__).resolve().parents[1] / "static" / "email-intake.html"
        application.add_api_route("/email-intake.html", lambda: FileResponse(email_path, headers={"Cache-Control": "no-store"}), methods=["GET"], include_in_schema=False)

    if not any(route.path == "/inventory-assistant.html" for route in application.routes):
        assistant_path = Path(__file__).resolve().parents[1] / "static" / "inventory-assistant.html"
        application.add_api_route("/inventory-assistant.html", lambda: FileResponse(assistant_path, headers={"Cache-Control": "no-store"}), methods=["GET"], include_in_schema=False)
    if not any(route.path == "/customer-statement-check.html" for route in application.routes):
        customer_check_path = Path(__file__).resolve().parents[1] / "static" / "customer-statement-check.html"
        application.add_api_route("/customer-statement-check.html", lambda: FileResponse(customer_check_path, headers={"Cache-Control": "no-store"}), methods=["GET"], include_in_schema=False)
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
    if not any(route.path == "/contract-print.html" for route in application.routes):
        contract_print_path = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "contract-print.html"
        )
        application.add_api_route(
            "/contract-print.html",
            lambda: FileResponse(contract_print_path),
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
    if not any(route.path == "/M/{mold_id}" for route in application.routes):
        mold_live_path = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "mobile_mold_live.html"
        )
        application.add_api_route(
            "/M/{mold_id}",
            _private_no_store_file_endpoint(mold_live_path),
            methods=["GET"],
            include_in_schema=False,
        )
    if not any(route.path == "/P/{product_id}" for route in application.routes):
        product_live_path = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "mobile_product_live.html"
        )
        application.add_api_route(
            "/P/{product_id}",
            _private_no_store_file_endpoint(product_live_path),
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
            lambda: FileResponse(mobile_stocktake_path, headers={"Cache-Control": "private, no-store, max-age=0", "Pragma": "no-cache", "Expires": "0"}),
            methods=["GET"],
            include_in_schema=False,
        )
    if not any(route.path == "/mobile/initial-stocktake-runtime.js" for route in application.routes):
        application.add_api_route(
            "/mobile/initial-stocktake-runtime.js",
            lambda: FileResponse(Path(__file__).resolve().parents[1] / "static" / "mobile_initial_stocktake_runtime.js",
                                 media_type="text/javascript", headers={"Cache-Control": "no-store"}),
            methods=["GET"], include_in_schema=False,
        )
    if not any(route.path == "/mobile/initial-stocktake.js" for route in application.routes):
        application.add_api_route(
            "/mobile/initial-stocktake.js",
            lambda: FileResponse(Path(__file__).resolve().parents[1] / "static" / "mobile_initial_stocktake.js",
                                 media_type="text/javascript", headers={"Cache-Control": "no-store"}),
            methods=["GET"], include_in_schema=False,
        )
    if not any(route.path == "/mobile/delivery-pick.html" for route in application.routes):
        generic_mobile_pick_path = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "mobile_delivery_pick.html"
        )
        application.add_api_route(
            "/mobile/delivery-pick.html",
            lambda: FileResponse(generic_mobile_pick_path),
            methods=["GET"],
            include_in_schema=False,
        )
    if not any(route.path == '/sp/{location_id}/{product_id}/{version}/{address_version}' for route in application.routes):
        application.add_api_route('/sp/{location_id}/{product_id}/{version}/{address_version}',
            lambda: FileResponse(Path(__file__).resolve().parents[1] / 'static' / 'shelf-pick-scan.html'),
            methods=['GET'], include_in_schema=False)
    mobile_erp_path = (
        Path(__file__).resolve().parents[1]
        / "static"
        / "mobile_erp.html"
    )
    if not any(route.path == "/mobile/" for route in application.routes):
        application.add_api_route(
            "/mobile/",
            lambda: FileResponse(mobile_erp_path),
            methods=["GET"],
            include_in_schema=False,
        )
    if not any(route.path == "/mobile/erp.html" for route in application.routes):
        application.add_api_route(
            "/mobile/erp.html",
            lambda: FileResponse(mobile_erp_path),
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
    if not any(route.path == "/location-label.html" for route in application.routes):
        location_label_path = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "location-label.html"
        )
        application.add_api_route(
            "/location-label.html",
            lambda: FileResponse(location_label_path),
            methods=["GET"],
            include_in_schema=False,
        )
    if not any(
        route.path == "/warehouse-rack-level-label.html"
        for route in application.routes
    ):
        rack_level_label_path = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "warehouse-rack-level-label.html"
        )
        application.add_api_route(
            "/warehouse-rack-level-label.html",
            lambda: FileResponse(rack_level_label_path),
            methods=["GET"],
            include_in_schema=False,
        )
    if not any(route.path == "/warehouse-ledger.html" for route in application.routes):
        warehouse_ledger_path = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "warehouse.html"
        )
        application.add_api_route(
            "/warehouse-ledger.html",
            lambda: FileResponse(warehouse_ledger_path),
            methods=["GET"],
            include_in_schema=False,
        )
    if not any(route.path == "/mobile/scan" for route in application.routes):
        def mobile_camera_scan_entry():
            from app.services.mobile_shelf_labels import camera_page_html
            return HTMLResponse(camera_page_html(), headers={"Cache-Control": "no-store"})
        application.add_api_route("/mobile/scan", mobile_camera_scan_entry, methods=["GET"], include_in_schema=False)
    if not any(route.path == "/q/{location_id}" for route in application.routes):
        def shelf_scan_entry(request: Request, location_id: int, product: str | None = None):
            from app.services.mobile_shelf_labels import scan_page_html, legacy_scan_redirect
            target = legacy_scan_redirect(request.url.hostname, location_id, product)
            if target:
                return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
            return HTMLResponse(scan_page_html(), headers={"Cache-Control": "no-store"})
        application.add_api_route("/q/{location_id}", shelf_scan_entry, methods=["GET"], include_in_schema=False)
        application.add_api_route("/q/{location_id}/{product}", shelf_scan_entry, methods=["GET"], include_in_schema=False)
    if not any(route.path == "/warehouse.html" for route in application.routes):
        warehouse_twin_path = (
            Path(__file__).resolve().parents[1]
            / "static"
            / "factory-twin-assets"
            / "warehouse-twin.html"
        )
        def warehouse_entry(request: Request):
            location_id = request.query_params.get("location_id", "")
            if (request.query_params.get("tab") == "locations"
                    and location_id.isascii() and location_id.isdigit() and int(location_id) > 0
                    and any(marker in request.headers.get("user-agent", "").lower()
                            for marker in ("iphone", "ipad", "android", "mobile"))):
                from app.services.mobile_shelf_labels import legacy_scan_redirect
                target = legacy_scan_redirect(request.url.hostname, int(location_id), request.query_params.get("product"))
                if target:
                    return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
                return RedirectResponse(f"/static/shelf-scan.html?location_id={int(location_id)}",
                                        status_code=307, headers={"Cache-Control": "no-store"})
            return FileResponse(warehouse_twin_path, headers={"Cache-Control": "no-store"})
        application.add_api_route(
            "/warehouse.html",
            warehouse_entry,
            methods=["GET"],
            include_in_schema=False,
        )
    if not any(getattr(route, "path", None) == "/factory-twin-assets" for route in application.routes):
        factory_twin_assets = Path(__file__).resolve().parents[1] / "static" / "factory-twin-assets"
        application.mount(
            "/factory-twin-assets",
            StaticFiles(directory=factory_twin_assets, check_dir=False),
            name="factory-twin-assets",
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
    if not any(route.path == "/api/audit/logs" for route in application.routes):
        application.include_router(
            audit_router,
            prefix="/api/audit",
            tags=["audit"],
        )
    if not any(
        route.path == "/api/orders/{order_id}/external-packaging-purchase"
        for route in application.routes
    ):
        application.include_router(
            external_packaging_purchases_router,
            prefix="/api",
            tags=["external-packaging-purchases"],
        )
    router_specs = (
        ("/api/customers", customers_router, "customers"),
        ("/api/master/customers", customers_router, "master-customers"),
        ("/api/master/materials", materials_router, "master-materials"),
        ("/api/master/suppliers", suppliers_router, "master-suppliers"),
        (
            "/api/master/external-packaging",
            external_packaging_prices_router,
            "external-packaging-prices",
        ),
        ("/api/master/products", product_import_router, "master-product-import"),
        (
            "/api/master/products",
            external_packaging_components_router,
            "external-packaging-components",
        ),
        ("/api/master/products", products_router, "master-products"),
    )
    existing_paths = {route.path for route in application.routes}
    for prefix, router, tag in router_specs:
        if prefix not in existing_paths:
            application.include_router(router, prefix=prefix, tags=[tag])
    if not any(
        route.path == "/api/products/box-type-rules"
        for route in application.routes
    ):
        application.include_router(
            box_type_rules_router,
            prefix="/api/products",
            tags=["product-box-type-rules"],
        )
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
    if not any(route.path == "/api/delivery-picks" for route in application.routes):
        application.include_router(
            pick_router,
            prefix="/api/delivery-picks",
            tags=["delivery-picks"],
        )
    if not any(route.path == "/api/mobile/tianhua-pick" for route in application.routes):
        application.include_router(
            tianhua_mobile_router,
            prefix="/api/mobile",
            tags=["tianhua-mobile-pick"],
        )
    if not any(route.path == "/api/mobile/erp/products" for route in application.routes):
        application.include_router(
            mobile_erp_router,
            prefix="/api/mobile/erp",
            tags=["mobile-erp"],
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
    if not any(route.path == "/api/finance/cost-pool" for route in application.routes):
        application.include_router(
            cost_accounting_router,
            prefix="/api/finance",
            tags=["finance-cost-accounting"],
        )
    if not any(
        route.path == "/api/finance/supplier-settlements"
        for route in application.routes
    ):
        application.include_router(
            supplier_settlements_router,
            prefix="/api/finance",
            tags=["supplier-monthly-settlements"],
        )
    if not any(
        route.path == "/api/finance/simple-finance/summary"
        for route in application.routes
    ):
        application.include_router(
            finance_simplified_router,
            prefix="/api/finance",
            tags=["finance-simplified"],
        )
    if not any(
        route.path == "/api/finance/processing-settings"
        for route in application.routes
    ):
        application.include_router(
            processing_cost_router,
            prefix="/api/finance",
            tags=["finance-processing-cost"],
        )
    if not any(
        route.path == "/api/finance/invoice-tasks"
        for route in application.routes
    ):
        application.include_router(
            invoice_tasks_router,
            prefix="/api/finance",
            tags=["invoice-tasks"],
        )
    if not any(
        route.path == "/api/customers/{customer_id}/invoice-profile"
        for route in application.routes
    ):
        application.include_router(
            invoice_customer_router,
            prefix="/api/customers",
            tags=["customer-invoice-profiles"],
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
    if not any(route.path == "/api/contracts" for route in application.routes):
        application.include_router(
            contracts_router,
            prefix="/api/contracts",
            tags=["contracts"],
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
    if not any(route.path == "/api/warehouse/fixed-shelf/products" for route in application.routes):
        application.include_router(fixed_shelf_router, prefix="/api/warehouse/fixed-shelf", tags=["fixed-shelf"])
        from app.api.shelf_pick_scan import router as shelf_pick_scan_router
        application.include_router(shelf_pick_scan_router, prefix='/api/shelf-pick-scan', tags=['fixed-shelf'])
    if not any(route.path == "/api/email-intake" for route in application.routes):
        from app.api.email_intake import router as email_intake_router
        application.include_router(email_intake_router, prefix="/api/email-intake", tags=["orders"])

    if not any(route.path == "/api/inventory-assistant" for route in application.routes):
        from app.api.inventory_assistant import router as inventory_assistant_router
        application.include_router(inventory_assistant_router, prefix="/api/inventory-assistant", tags=["warehouse"])
    if not any(
        route.path == "/api/ai/inventory-insights/runs"
        for route in application.routes
    ):
        application.include_router(
            ai_assistant_router,
            prefix="/api/ai",
            tags=["ai-assistant"],
        )
    if not any(route.path == "/api/warehouse/locations" for route in application.routes):
        from app.api.material_candidates import router as material_candidates_router
        application.include_router(material_candidates_router, prefix="/api/warehouse/lots", tags=["warehouse"])
        from app.api.warehouse_goods import router as warehouse_goods_router
        application.include_router(warehouse_goods_router, prefix="/api/warehouse/goods", tags=["warehouse"])
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
    if not any(
        route.path == "/api/warehouse/inventory-onboarding/batches"
        for route in application.routes
    ):
        application.include_router(
            inventory_onboarding_router,
            prefix="/api/warehouse",
            tags=["warehouse-inventory-onboarding"],
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
        SelectiveGZipMiddleware,
        PrivateUploadGuardMiddleware,
        PerformanceObservabilityMiddleware,
        MoldPrivateNoStoreMiddleware,
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
    application.add_middleware(
        SelectiveGZipMiddleware,
        minimum_size=1024,
        compresslevel=6,
    )
    application.add_middleware(
        PerformanceObservabilityMiddleware,
        slow_request_ms=slow_request_threshold_ms(),
    )
    application.add_middleware(PrivateUploadGuardMiddleware)
    application.add_middleware(MoldPrivateNoStoreMiddleware)
    return application


app = create_app()
legacy.app = app
