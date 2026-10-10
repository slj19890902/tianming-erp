"""Real isolated Chrome + real product-workbench API; fictional fixture only."""
import json,os,subprocess,threading,time
from pathlib import Path
import pytest
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from test_stock_replenishment_flow import stock_replenishment_app,_customer_replenishment_payload
from test_stock_processing_auto import receive
from app.models.product import Product


@pytest.mark.skipif(not os.environ.get('ERP_LIFECYCLE_BROWSER_EVIDENCE'),reason='explicit isolated Chrome run only')
def test_product_lifecycle_in_real_chrome(stock_replenishment_app):
    import uvicorn
    from app.api.product_workbench import router
    app,factory=stock_replenishment_app
    app.include_router(router,prefix='/api/product-workbench')
    project=Path(__file__).resolve().parents[1]
    app.mount('/static',StaticFiles(directory=project/'static'),name='lifecycle-static')
    @app.get('/lifecycle-acceptance',response_class=HTMLResponse)
    def browser_page():
        return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/ui/product-workbench.css"><body><h1>隔离验收 · 虚构资料</h1><main id="workbench"></main><script src="/static/ui/product-workbench.js"></script><script>ERPProductWorkbench.mount({container:document.getElementById('workbench'),request:async path=>{const r=await fetch(path);const data=await r.json();if(!r.ok)throw Error(data.detail||'读取失败');return data;}});</script></body></html>'''
    with factory() as db:
        p=db.get(Product,1);p.product_code='80011965';p.product_name='虚构四模产品';db.commit()
    with TestClient(app) as client:
        payload=_customer_replenishment_payload(2500);payload['items'][0]['stock_yield_per_sheet']=4
        receive(client,payload,quantity=2500)
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=18994,log_level='error',access_log=False))
    thread=threading.Thread(target=server.run,daemon=True);thread.start()
    try:
        deadline=time.monotonic()+15
        while not server.started and time.monotonic()<deadline:time.sleep(.05)
        assert server.started
        completed=subprocess.run(['node',str(project/'tests/ui/product_lifecycle_browser.cjs')],capture_output=True,text=True,encoding='utf8',timeout=70)
        assert completed.returncode==0,completed.stdout+completed.stderr
    finally:
        server.should_exit=True;thread.join(timeout=10)
        assert not thread.is_alive()
