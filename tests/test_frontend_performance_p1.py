from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware.performance import PerformanceObservabilityMiddleware


ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "static" / "index.html"
INDEX = INDEX_PATH.read_text(encoding="utf-8")
VENDOR = ROOT / "static" / "vendor"
EXPECTED_VENDOR = {
    "vue-3.5.40.global.prod.js": (
        "9E0039A3F6ED0E85308E24D737447F1AF6AF83D229D69E1267A32B29BC2A1337"
    ),
    "axios-1.18.1.min.js": (
        "DE2511864B48B3A371A9C789A9CF624B3BCDE628FB6572B6B936A5CC2C48C26F"
    ),
    "pinyin-pro-3.26.0.js": (
        "83F5C34BC94B1E4F0E0CA8BE6AB28C095CEA38D8CF70E5FD45E9656B5608B041"
    ),
}
EXPECTED_LICENSES = {
    "vue-3.5.40.LICENSE",
    "axios-1.18.1.LICENSE",
    "pinyin-pro-3.26.0.LICENSE",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def test_index_uses_only_versioned_local_runtime_dependencies() -> None:
    sources = re.findall(r'<script\s+src="([^"]+)"', INDEX)
    time_utils_hash = _sha256(ROOT / "static" / "assets" / "time-utils.js").lower()[:12]
    # Feature workspaces may load between Vue and the shared helpers.  Keep
    # the runtime contract strict without freezing an incidental script order.
    assert sources[0] == "/static/vendor/vue-3.5.40.global.prod.js"
    assert "/static/vendor/axios-1.18.1.min.js" in sources
    assert f"/static/assets/time-utils.js?v={time_utils_hash}" in sources
    assert "/static/vendor/pinyin-pro-3.26.0.js" not in sources
    assert not any(source.startswith(("http://", "https://")) for source in sources)
    for filename, expected_hash in EXPECTED_VENDOR.items():
        assert _sha256(VENDOR / filename) == expected_hash
    assert all((VENDOR / filename).is_file() for filename in EXPECTED_LICENSES)


def test_local_vendor_bundles_expose_expected_runtime_contract(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        return
    script = tmp_path / "vendor-contract.cjs"
    paths = {
        name: str(VENDOR / name)
        for name in EXPECTED_VENDOR
    }
    script.write_text(
        "\n".join(
                [
                    f"const paths = {json.dumps(paths, ensure_ascii=False)};",
                    "const fs = require('fs');",
                    "const vm = require('vm');",
                    "const context = {console, setTimeout, clearTimeout, URLSearchParams};",
                    "context.globalThis = context; context.self = context; context.window = context;",
                    "vm.createContext(context);",
                    "for (const path of Object.values(paths)) vm.runInContext(fs.readFileSync(path, 'utf8'), context);",
                    "if (typeof context.Vue?.createApp !== 'function') throw new Error('Vue global build invalid');",
                    "if (typeof context.axios?.get !== 'function') throw new Error('Axios build invalid');",
                    "if (typeof context.pinyinPro?.pinyin !== 'function') throw new Error('pinyin-pro build invalid');",
                    "const initials = context.pinyinPro.pinyin('\\u5929\\u660e', {pattern:'first', toneType:'none', type:'array'}).join('');",
                "if (initials !== 'tm') throw new Error(`pinyin result invalid: ${initials}`);",
            ]
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_login_loads_only_current_page_and_page_switches_use_short_cache() -> None:
    assert "await this.loadBase()" not in INDEX
    assert "async loadBase()" not in INDEX
    assert "PAGE_CACHE_TTL_MS = 30_000" in INDEX
    assert "pageCacheFresh(page)" in INDEX
    assert "loadPage(this.activePage, { force:true })" in INDEX
    assert "return this.loadPage(this.activePage, { force:true })" in INDEX


def test_list_searches_are_debounced_and_old_requests_are_cancelled() -> None:
    assert "queuePageSearch('customers')" in INDEX
    assert "queuePageSearch('products')" in INDEX
    assert "queuePageSearch('orders')" in INDEX
    assert "new AbortController()" in INDEX
    for request_key in (
        "customers:list",
        "products:list",
        "materials:list",
        "orders:list",
    ):
        literal_begin = f'beginLatestRequest("{request_key}")'
        variable_begin = re.search(
            rf'const requestKey = "{re.escape(request_key)}";\s*'
            r"const controller = this\.beginLatestRequest\(requestKey\)",
            INDEX,
        )
        assert literal_begin in INDEX or variable_begin is not None
        assert (
            f'finishLatestRequest("{request_key}", controller)' in INDEX
            or (
                variable_begin is not None
                and "finishLatestRequest(requestKey, controller)" in INDEX
            )
        )
    assert "signal:controller.signal" in INDEX


def test_performance_middleware_adds_timing_cache_and_slow_api_log(caplog) -> None:
    application = FastAPI()
    application.add_middleware(
        PerformanceObservabilityMiddleware,
        slow_request_ms=0,
    )

    @application.get("/api/demo")
    def api_demo() -> dict[str, bool]:
        return {"ok": True}

    @application.get("/static/vendor/demo.js")
    def vendor_demo() -> str:
        return "demo"

    with caplog.at_level(logging.WARNING, logger="erp.performance"):
        with TestClient(application) as client:
            api_response = client.get("/api/demo")
            vendor_response = client.get("/static/vendor/demo.js")

    assert api_response.status_code == 200
    assert api_response.headers["server-timing"].startswith("app;dur=")
    assert ", db;dur=" in api_response.headers["server-timing"]
    assert "slow_api method=GET path=/api/demo status=200" in caplog.text
    assert "query_count=0" in caplog.text
    assert "db_duration_ms=0.0" in caplog.text
    assert "response_bytes=" in caplog.text
    assert (
        vendor_response.headers["cache-control"]
        == "public, max-age=31536000, immutable"
    )
