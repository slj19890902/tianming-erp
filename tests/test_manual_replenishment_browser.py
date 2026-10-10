"""Installed Chrome, full ERP HTML and API, fictional fixture database only."""
import os
import json
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
from tests.test_stock_replenishment_flow import stock_replenishment_app


@pytest.mark.skipif(not os.environ.get('ERP_MANUAL_REPLENISHMENT_BROWSER'), reason='explicit isolated Chrome only')
def test_manual_replenishment_real_page(stock_replenishment_app):
    import uvicorn
    from app.main import create_app
    fixture_app, factory = stock_replenishment_app
    from app.models.product import Product
    with factory() as db:
        source = db.get(Product, 1)
        extra_products = [Product(customer_id=1, product_code=f'FICTION-{i:03}',
            customer_material_code=f'FICTION-{i:03}', product_name=f'虚构选款{i}',
            material_id=source.material_id, layer_count=source.layer_count,
            flute_type=source.flute_type, report_length_mm=source.report_length_mm,
            report_width_mm=source.report_width_mm, is_active=True) for i in range(25)]
        db.add_all(extra_products); db.commit()
        extra_ids = [p.id for p in extra_products]
    app = create_app()
    app.dependency_overrides.update(fixture_app.dependency_overrides)
    sock = socket.socket(); sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, lifespan='off', log_level='error', access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True); thread.start()
    try:
        deadline = time.monotonic()+15
        while not server.started and time.monotonic()<deadline: time.sleep(.05)
        assert server.started
        run = subprocess.run(['node', str(Path(__file__).parent/'ui/manual_replenishment_entry.browser.cjs'),str(port),json.dumps(extra_ids)],
            capture_output=True,text=True,encoding='utf-8',timeout=100)
        assert run.returncode==0, run.stdout+run.stderr
    finally:
        server.should_exit=True; thread.join(timeout=10); sock.close()
        assert not thread.is_alive()
