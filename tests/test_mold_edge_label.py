from copy import deepcopy
import pytest
from app.services import mold_label_layout as layout

def test_edge_default_and_historical_snapshot_are_independent():
    current = layout.normalize_layout(layout.default_layout())
    fields = {e["id"]:e for e in current["elements"]}
    assert current["catalog_version"] == "mold-drawing-orientation-v1"
    assert fields["inventory_code_top"]["font_size_mm"] == fields["drawing_number"]["font_size_mm"] == 6.1
    assert fields["mold_qr"]["width_mm"] == fields["mold_qr"]["height_mm"] == 15
    assert fields["section_rule_bottom"]["y_mm"] + .2 - fields["section_rule_top"]["y_mm"] == 17
    assert fields["customer_name"]["x_mm"] < fields["inventory_code_side"]["x_mm"]
    for key in ("inventory_code_side", "customer_name"):
        e = fields[key]
        assert 16.2 <= e["y_mm"] and e["y_mm"] + e["height_mm"] <= 32.8
    old = layout._default_layout_v8()
    frozen = deepcopy(old)
    assert layout._normalize_snapshot_layout(old)["catalog_version"] == "p1-119-v1"
    assert layout._upgrade_to_current_catalog(old)["catalog_version"] == "mold-drawing-orientation-v1"
    assert layout._normalize_snapshot_layout(layout._default_layout_v9())["catalog_version"] == "mold-edge-v1"
    assert old == frozen

@pytest.mark.parametrize("mutation", ["missing", "hidden", "overlap", "outside", "small_qr"])
def test_edge_rejects_unsafe_layout(mutation):
    value = layout.default_layout()
    if mutation == "missing": value["elements"].pop(0)
    if mutation == "hidden": value["elements"][0]["visible"] = False
    if mutation == "overlap": value["elements"][1]["width_mm"] = 77.6
    if mutation == "outside": value["elements"][3]["y_mm"] = 0
    if mutation == "small_qr": next(e for e in value["elements"] if e["id"] == "mold_qr")["width_mm"] = 14
    with pytest.raises(layout.MoldLabelLayoutError): layout.normalize_layout(value)

from fastapi.testclient import TestClient
from tests.test_mold_tool_workflow import mold_app, _login
from tests.test_p1_103_mold_label_layout import _complete_named_mold

def test_new_jobs_freeze_edge_layout_and_permissions_remain(mold_app):
    app, factory = mold_app
    mold_id = _complete_named_mold(factory)
    with TestClient(app) as client:
        payload = dict(operation_key="edge-layout-publish-20260928", expected_release_version=0, layout=layout.default_layout())
        assert client.post("/api/warehouse/molds/label-layout/admin/publish",json=payload).status_code == 401
        _login(client,"workshop")
        assert client.post("/api/warehouse/molds/label-layout/admin/publish",json=payload).status_code == 403
        _login(client,"admin")
        first=client.post("/api/warehouse/molds/label-layout/admin/publish",json=payload)
        assert first.status_code==200, first.text
        replay=client.post("/api/warehouse/molds/label-layout/admin/publish",json=payload)
        assert replay.status_code==200
        assert replay.json()["replayed"] is True
        assert replay.json()["published"]==first.json()["published"]
        frozen=client.get("/api/warehouse/molds/label-layout").json()
        assert frozen["layout"]["catalog_version"]=="mold-drawing-orientation-v1"
        assert len(frozen["layout"]["elements"])==10

        print_payload=dict(mold_ids=[mold_id],source="single",template_version="mold_80x40_v1",idempotency_key="edge-print-20260928")
        created=client.post("/api/warehouse/molds/label-prints",json=print_payload)
        assert created.status_code==200, created.text
        job_id=created.json()["print_job_id"]
        changed=layout.default_layout()
        changed["elements"][0]["font_size_mm"]=5.8
        assert client.post("/api/warehouse/molds/label-layout/admin/publish",json=dict(operation_key="edge-layout-second",expected_release_version=1,layout=changed)).status_code==200
        repeated=client.post("/api/warehouse/molds/label-prints",json=print_payload)
        assert repeated.status_code==200 and repeated.json()["print_job_id"]==job_id
        assert repeated.json()["label_layout"]["layout"]==created.json()["label_layout"]["layout"]
        original=client.get(f"/api/warehouse/molds/{mold_id}/label",params=dict(template_version="mold_80x40_v1",print_job_id=job_id))
        assert original.status_code==200
        assert original.json()["label_layout"]["layout"]["elements"][0]["font_size_mm"]==6.1


def test_product_drawing_number_is_available_to_new_label(mold_app):
    from app.models.product import Product
    from sqlalchemy import select

    app, factory = mold_app
    mold_id = _complete_named_mold(factory)
    with factory() as db:
        product = db.scalar(select(Product).where(Product.mold_tool_id == mold_id))
        product.customer_drawing_number = "DWG-61452621-A"
        db.commit()
    with TestClient(app) as client:
        _login(client, "admin")
        response = client.get(f"/api/warehouse/molds/{mold_id}/label-preview")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["label_drawing_numbers"] == ["DWG-61452621-A"]
        assert body["label_products"][0]["drawing_number"] == "DWG-61452621-A"
        assert body["label_layout"]["layout"]["catalog_version"] == "mold-drawing-orientation-v1"
