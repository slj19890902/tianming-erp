"""Loopback-only synthetic visual fixture; no app import or database access.

Serves the real built shell and current legacy template. Only the root startup
is replaced with explicit synthetic records. All business writes are rejected.
Use Chrome through the approved UI tooling, not against the formal ERP.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse
import time


ROOT = Path(__file__).resolve().parents[2]
CUSTOMER = {"id": 990001, "customer_code": "UIFIX", "name": "隔离演示客户（合成资料）", "chinese_short_name": "隔离演示", "is_active": True}
EMPTY_CUSTOMER = {"id": 990002, "customer_code": "UIEMPTY", "name": "隔离空客户（合成资料，延迟读取）", "is_active": True}
USER = {"id": 990001, "username": "isolated-ui", "role": "admin", "real_name": "隔离布局检查", "must_change_password": False}
PERMISSIONS = ["dashboard.view", "customers.view", "products.view", "products.edit", "products.create", "orders.view", "orders.create", "orders.edit", "requisition.view", "incoming.view", "warehouse.view", "deliveries.view", "finance.view"]
PRODUCTS = [{
    "id": 990001 + i, "customer_id": CUSTOMER["id"], "product_code": f"DEMO-{i + 1:04}",
    "product_name": "隔离演示纸箱长名称—仅验证布局和查询，不是正式业务资料",
    "unit": "只", "unit_needs_review": i % 3 == 0, "length_mm": 575, "width_mm": 475, "height_mm": 150,
    "customer_specification": "575×475×150mm", "sale_unit_price": 7.4,
    "material_id": None, "material_structure": "120/190/135/55/170/190",
    "classification": {"label": "A1/0201 普通开槽箱", "legacy_name": "WC 五层钉箱"},
    "is_active": True, "version": 2, "readiness": {"ready": False, "missing_fields": ["材质"]},
} for i in range(24)]


def fixture_html(mode: str, page: str) -> bytes:
    html = (ROOT / "static/index.html").read_text()
    startup = "        async mounted() {"
    mount = 'const formalWorkspaceInstance = app.mount("#app");'
    if html.count(startup) != 1 or html.count(mount) != 1:
        raise RuntimeError("Root lifecycle fixture anchors changed; review before use")
    html = html.replace(startup, "        async fixtureDisabledMounted() {")
    fixture = {"user": {**USER, "ui_mode": mode, "permissions": PERMISSIONS, "unrestricted_customer_access": True},
               "customer": CUSTOMER, "customers": [CUSTOMER, EMPTY_CUSTOMER], "products": PRODUCTS[:2], "mode": mode, "page": page}
    injection = """
      const isolatedFixture = __DATA__;
      app.mixin({beforeMount() {
        if (this.$parent) return;
        this.user=isolatedFixture.user;
        this.activePage=isolatedFixture.page; this.productTab='products';
        this.selectedProductCustomer=isolatedFixture.customer;
        this.customerOptions=isolatedFixture.customers; this.allCustomers=isolatedFixture.customers;
        this.products=isolatedFixture.products; this.productsTotal=24;
        this.productsListIdentity=`${this.user.id}:${this.authGeneration}:${this.selectedProductCustomer.id}`;
        this.sessionRestoring=false;
        window.erpCheckingSession=false;
        window.erpRequestIdentity=()=>({actor:this.user?.id ?? null,generation:this.authGeneration});
        window.erpPageReadIdentity=()=>({page:this.activePage,sequence:this.pageLoadSequence,reading:this.pageLoading});
      }});
      const formalWorkspaceInstance = app.mount("#app");
      if(isolatedFixture.page!=='products')formalWorkspaceInstance.loadPage();
      document.title='ERP 3.0 隔离布局验证（合成数据）';
    """.replace("__DATA__", json.dumps(fixture, ensure_ascii=False))
    return html.replace(mount, injection).encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # No query text, cookies, or record payloads in diagnostic output.
        print(json.dumps({"method": self.command, "path": urlparse(self.path).path,
                          "status": str(args[1]) if len(args) > 1 else ""}), flush=True)

    def send_body(self, body: bytes, mime="application/json", status=200):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Expected when a read is cancelled by navigating away.

    def json(self, value, status=200):
        self.send_body(json.dumps(value, ensure_ascii=False).encode(), status=status)

    def do_POST(self):
        self.json({"detail": "隔离布局夹具禁止业务写入"}, 405)

    do_PUT = do_PATCH = do_DELETE = do_POST

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        path = unquote(url.path)
        if path == "/frontend-v2/formal-workspace":
            return self.send_body(fixture_html(self.server.fixture_mode, self.server.fixture_page), "text/html; charset=utf-8")
        if path == "/api/auth/me":
            return self.json({"user": {**USER, "ui_mode": self.server.fixture_mode}, "permissions": PERMISSIONS, "customer_scope": [], "unrestricted_customer_access": True})
        if path == "/api/master/products":
            keyword = " ".join(query.get(key, [""])[0] for key in ("keyword", "product_code", "product_name"))
            if query.get("customer_id", [""])[0] == str(EMPTY_CUSTOMER["id"]):
                time.sleep(3)
                return self.json({"items": [], "total": 0, "page": 1, "page_size": 12})
            if "慢" in keyword:
                time.sleep(3)
            if "失败" in keyword:
                return self.json({"detail": "隔离模拟：读取失败，请重试"}, 503)
            rows = [] if "无结果" in keyword else PRODUCTS
            size = min(50, max(1, int(query.get("page_size", [12])[0])))
            page = max(1, int(query.get("page", [1])[0]))
            return self.json({"items": rows[(page-1)*size:page*size], "total": len(rows), "page": page, "page_size": size})
        if path in ("/api/master/customers", "/api/customers"):
            return self.json({"items": [CUSTOMER, EMPTY_CUSTOMER], "total": 2, "page": 1, "page_size": 20})
        if path == "/api/products/box-type-rules":
            return self.json({"rules": [{"code": "A1/0201", "display_name": "普通开槽箱"}]})
        if path in ("/api/dashboard/overview", "/api/dashboard/sections"):
            group = query.get("group", [""])[0]
            summary = path.endswith("overview")
            if not summary and self.server.section_behavior == "slow":
                time.sleep(3)
            if not summary and self.server.section_behavior == "fail":
                return self.json({"detail": "隔离模拟：工作清单读取失败"}, 503)
            card = {"key": "pending_material", "title": "待报料", "count": None if summary else 1,
                    "count_unit": "项", "target": "requisition", "button_label": "去报料", "target_filter": {}}
            task = {"id": "pending_material:source:990001", "key": "pending_material", "type": "待报料",
                    "target": "requisition", "customer_id": CUSTOMER["id"], "customer_ids": [CUSTOMER["id"]],
                    "customer_name": CUSTOMER["name"], "customer_label": "隔离演示客户", "product_code": "DEMO-0001",
                    "product_name": "合成纸箱资料", "order_id": 990001, "customer_po": "DEMO-PO-001",
                    "order_number": "DEMO-ORDER-001", "due_date": "2026-10-22", "urgency": "today",
                    "urgency_label": "今日交期", "message": "合成演示：订单100只 · 待安排报料",
                    "action_text": "去报料", "target_filter": {}, "attention_state": "active", "source_hash": "fixture-only"}
            workbench = {"today": "2026-10-22", "customers": [{"id": CUSTOMER["id"], "name": CUSTOMER["name"], "label": "隔离演示客户"}],
                         "tasks": [task] if group == "production" else [], "counts": {}, "warnings": [],
                         "can_review": True, "can_view_approvals": True, "can_manage_team_reminders": True, "attention_reasons": []}
            value = {"as_of": "2026-10-22T17:00:00+08:00", "cards": [card] if summary or group == "production" else [],
                     "workbench": workbench, "month": "2026-10", "timezone": "Asia/Shanghai"}
            value.update({"mode": "summary", "available_groups": ["stock", "production", "delivery", "finance", "approval"]}
                         if summary else {"group": group, "low_stock_warnings": []})
            return self.json(value)
        if path == "/api/home-tools/memos":
            return self.json({"items": [{"id": "fixture-memo", "text": "隔离演示：核对今天的待办安排", "version": 1, "completed": False}],
                              "total": 1, "active_count": 1, "completed_count": 0})
        if path.startswith("/api/"):
            return self.json({"items": [], "total": 0, "counts": {}, "permissions": [], "layout": {}})
        if path.startswith("/static/"):
            base, relative = ROOT / "static", path.removeprefix("/static/")
        elif path.startswith("/frontend-v2/assets/"):
            base, relative = ROOT / "frontend-v2/dist/assets", path.removeprefix("/frontend-v2/assets/")
        elif path.startswith("/frontend-v2/") or path == "/":
            return self.send_body((ROOT / "frontend-v2/dist/index.html").read_bytes(), "text/html; charset=utf-8")
        else:
            return self.json({"detail": "Fixture route unavailable"}, 404)
        target = (base / relative).resolve()
        if not target.is_relative_to(base.resolve()) or not target.is_file():
            return self.json({"detail": "Fixture asset unavailable"}, 404)
        self.send_body(target.read_bytes(), mimetypes.guess_type(target)[0] or "application/octet-stream")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18486)
    parser.add_argument("--mode", choices=["standard", "large"], default="standard")
    parser.add_argument("--page", choices=["products", "dashboard", "customers", "orders"], default="products")
    parser.add_argument("--section-behavior", choices=["normal", "slow", "fail"], default="normal")
    args = parser.parse_args()
    if not 18001 <= args.port <= 19999:
        parser.error("Use an isolated port in 18001..19999")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.fixture_mode = args.mode
    server.fixture_page = args.page
    server.section_behavior = args.section_behavior
    print(f"Synthetic UI only: http://127.0.0.1:{args.port}/frontend-v2/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
