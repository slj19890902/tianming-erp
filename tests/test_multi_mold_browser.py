"""Installed Chrome, actual editor and API, disposable fictional records only."""
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from tests.test_mold_tool_workflow import mold_app


@pytest.mark.skipif(not os.environ.get("ERP_MULTI_MOLD_BROWSER_EVIDENCE"), reason="explicit isolated Chrome run")
def test_multi_mold_editor_in_chrome(mold_app):
    import uvicorn
    from app.api.materials import router as materials
    from app.api.products import box_type_rules_router
    from app.api.requisition import router as requisition
    from app.models.product import Product
    from app.models.mold_tool import MoldTool, MoldToolCustomer
    from app.models.user import User
    from app.services.composite_bom import replace_product_bom
    from app.services.mold_label_content import canonical_label_overrides
    app, factory = mold_app
    project = Path(__file__).resolve().parents[1]
    app.include_router(materials, prefix="/api/master/materials")
    app.include_router(box_type_rules_router, prefix="/api/products")
    app.include_router(requisition, prefix="/api/requisition")
    app.mount("/static", StaticFiles(directory=project / "static"), name="multi-mold-static")
    # Suppress unrelated dashboard startup, while keeping the actual editor,
    # Vue template, permission checks, loaders and all business APIs unchanged.
    html = (project / "static/index.html").read_text(encoding="utf-8")
    html = html.replace("        async mounted() {", "        async isolatedDashboardStartup() {")
    html = html.replace('app.mount("#app");', 'window.erpAcceptance=app.mount("#app");')

    @app.get("/multi-mold-acceptance", response_class=HTMLResponse)
    def page():
        return html

    with factory() as db:
        db.add(Product(id=1, customer_id=1, product_code="UAT-PARENT", customer_material_code="UAT-PARENT",
                       product_name="虚构双模组合", box_style="BOM组合", unit="套",
                       combination_mode="parent_priced_set", composite_fulfillment_mode="parent_delivery"))
        rows = []
        for pid, purpose, count in [(2, "A模", 2), (3, "B模", 4)]:
            mold = MoldTool(id=pid, mold_code=f"UAT-MOLD-{pid}", mold_name=f"隔离测试 {purpose}",
                            label_name=purpose, identity_status="frozen", rack_location="1F-M-R01-L2",
                            label_overrides_json=canonical_label_overrides({"product_name": purpose}))
            db.add(mold)
            db.flush()
            db.add(MoldToolCustomer(mold_tool_id=pid, customer_id=1, display_order=1))
            db.add(Product(id=pid, customer_id=1, product_code=f"UAT-CHILD-{pid}",
                           customer_material_code=f"UAT-CHILD-{pid}", product_name=f"虚构子件 {purpose}",
                           unit="片", box_category="die_cut", box_style="刀卡", production_process="模切,无需结合",
                           mold_tool_id=pid, report_length_mm=800, report_width_mm=600,
                           length_mm=400, width_mm=300, layer_count=3, flute_type="B",
                           sheet_cutting_settings={"schema_version": 2, "whole": {
                               "length_parts": 1, "width_parts": 1, "mold_count": count, "is_die_cut": True}}))
            rows.append(dict(component_product_id=pid, quantity_per_set=1, inventory_relation="assembly",
                             is_die_cut=True, mold_tool_id=pid))
        db.flush()
        actor = db.scalar(select(User).where(User.role == "admin"))
        replace_product_bom(db, parent_product_id=1, inventory_mode="assembled", components=rows,
                            material_mode="expand_children", delivery_mode="parent", expected_version=1, user=actor)
        db.commit()
    port = int(os.environ["ERP_PORT"])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started and time.monotonic() < deadline:
            time.sleep(.05)
        assert server.started
        result = subprocess.run(["node", str(project / "tests/ui/multi_mold_browser.cjs")],
                                capture_output=True, text=True, encoding="utf-8", timeout=85)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()
