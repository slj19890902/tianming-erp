"""Full ERP page, installed Chrome, fictional database; never formal service."""
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
import pytest
from tests.test_stock_replenishment_flow import stock_replenishment_app


@pytest.mark.skipif(not os.environ.get('ERP_SUPPLIER_SHEET_BROWSER'),reason='explicit isolated Chrome only')
@pytest.mark.parametrize('component',['whole','base'])
def test_common_box_supplier_default_real_page(stock_replenishment_app,component):
    import uvicorn
    from app.main import app
    from app.models.product import Product
    from app.services.sheet_cutting_settings import SheetCuttingSettings
    fixture, factory=stock_replenishment_app
    with factory() as db:
        product=db.get(Product,1)
        product.product_name='虚构修边测试纸箱';product.box_style='A1/0201 普通开槽箱'
        product.length_mm=150;product.width_mm=100;product.height_mm=50
        product.report_length_mm=375;product.report_width_mm=226
        product.production_process='无需结合';product.crease_type='净料'
        product.crease_left_mm=product.crease_middle_mm=product.crease_right_mm=None
        product.sheet_cutting_settings={'schema_version':2,'whole':SheetCuttingSettings(2,3).to_dict()}
        if component=='base':
            product.box_style='A3 天地盖'
            product.base_report_length_mm=375;product.base_report_width_mm=226
            product.base_crease_type='压线';product.base_crease_left_mm=50;product.base_crease_middle_mm=126;product.base_crease_right_mm=50
            product.sheet_cutting_settings={'schema_version':2,'cover':SheetCuttingSettings(2,3).to_dict(),'base':SheetCuttingSettings(2,3).to_dict()}
        product.default_cutting_mode='一开六';db.commit()
    original_overrides=dict(app.dependency_overrides)
    app.dependency_overrides.update(fixture.dependency_overrides)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,lifespan='off',log_level='error',access_log=False))
    thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
    try:
        deadline=time.monotonic()+15
        while not server.started and time.monotonic()<deadline:time.sleep(.05)
        assert server.started
        run=subprocess.run(['node',str(Path(__file__).parent/'ui/supplier_sheet_default.browser.cjs'),str(port),component],capture_output=True,text=True,encoding='utf-8',timeout=110)
        assert run.returncode==0,run.stdout+run.stderr
    finally:
        server.should_exit=True;thread.join(timeout=10);sock.close()
        assert not thread.is_alive()
        app.dependency_overrides.clear();app.dependency_overrides.update(original_overrides)
