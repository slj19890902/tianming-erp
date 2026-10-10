"""Opt-in real mobile page check against fictional, isolated API fixtures."""
import os
from pathlib import Path
import socket
import subprocess
import threading
import time

import pytest
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from test_p1_21b_mobile_admin_product_search import mobile_erp_app


@pytest.mark.skipif(not os.environ.get('ERP_FIELD_SEARCH_BROWSER_EVIDENCE'),
                    reason='explicit isolated Chrome check only')
def test_field_search_real_mobile_page(mobile_erp_app, monkeypatch):
    import uvicorn
    from app.api.product_workbench import router
    from app.api.mobile_stock_use import router as stock_router
    from app.models.product import Product
    from app.models.mold_tool import MoldTool
    app, ids, factory = mobile_erp_app
    app.include_router(router, prefix='/api/product-workbench')
    app.include_router(stock_router, prefix='/api/mobile/stock-use')
    project = Path(__file__).resolve().parents[1]
    app.mount('/static', StaticFiles(directory=project/'static'), name='field-static')
    app.mount('/factory-twin-assets', StaticFiles(directory=project/'static/factory-twin-assets'), name='field-twin')

    @app.get('/mobile/')
    def mobile_page():
        return FileResponse(project/'static/mobile_erp.html')

    # A fictional measured mold cell; production paths still use the normal API.
    from app.services import mold_location
    describe = mold_location.describe_mold_location
    monkeypatch.setattr(mold_location, 'describe_mold_location', lambda value: {
        'kind': 'storage_cell', 'location_id': 'fixture-cell', 'floor': '1F',
        'rack': 'B', 'short_label': 'B7', 'alias': 'B7',
        'prompt': '一楼 · 模具B架 · B7（第3层第1格）',
    } if value == 'MCELL-FIELD-FIXTURE' else describe(value))
    with factory() as db:
        mold = MoldTool(mold_code='FIELD-FIXTURE', mold_name='虚构纸盒模具',
                        rack_location='MCELL-FIELD-FIXTURE')
        db.add(mold); db.flush()
        db.get(Product, ids['product']).mold_tool_id = mold.id
        db.commit()
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                        log_level='error', access_log=False))
    thread = threading.Thread(target=server.run, daemon=True); thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started and time.monotonic() < deadline:
            time.sleep(.05)
        assert server.started
        env = dict(os.environ, ERP_FIELD_SEARCH_BROWSER_BASE=f'http://127.0.0.1:{port}',
                   ERP_FIELD_SEARCH_PRODUCT_ID=str(ids['product']))
        result = subprocess.run(['node', str(project/'tests/ui/mobile_field_search_browser.cjs')],
                                env=env, capture_output=True, text=True, encoding='utf8', timeout=100)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.should_exit = True; thread.join(timeout=10)
        assert not thread.is_alive()
