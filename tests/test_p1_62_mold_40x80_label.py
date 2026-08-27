from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess

from fastapi.testclient import TestClient
from pypdf import PdfReader
import pytest
from sqlalchemy import func, select

from tests.test_mold_tool_workflow import _login, mold_app
from tests.test_mold_label_print_pdf import (
    POINTS_TO_MM,
    _current_print_styles,
    _print_to_pdf,
    _qr_data_url,
    headless_browser,
)
from tests.test_p1_103_mold_label_layout import _legacy_v3_layout


ROOT = Path(__file__).resolve().parents[1]
LABEL = (ROOT / "static" / "mold-label.html").read_text(encoding="utf-8")
LAYOUT_CSS = (ROOT / "static" / "assets" / "mold-label-layout.css").read_text(
    encoding="utf-8"
)
LAYOUT_JS = (ROOT / "static" / "assets" / "mold-label-layout.js").read_text(
    encoding="utf-8"
)
WAREHOUSE = (ROOT / "static" / "warehouse.html").read_text(encoding="utf-8")


def _complete_mold(factory, *, suffix: str = "1") -> int:
    from app.models.mold_tool import MoldTool
    from app.models.product import Product

    with factory() as db:
        mold = MoldTool(
            mold_code=f"P162-M-{suffix}",
            mold_name=f"思迈尔 P162-{suffix}",
            label_name=f"P162-{suffix}",
            chinese_short_name=f"样例{suffix}",
            identity_status="frozen",
            rack_location="1F-M-R01-L1-G01",
        )
        db.add(mold)
        db.flush()
        db.add(
            Product(
                customer_id=1,
                product_code=f"SME-LONG-CODE-{suffix}",
                customer_material_code=f"SME-LONG-CODE-{suffix}",
                product_name=f"五层加强纸箱横向标签样例{suffix}",
                length_mm=520,
                width_mm=350,
                height_mm=300,
                report_length_mm=1100,
                report_width_mm=760,
                flute_type="BC",
                default_cutting_mode="一开二",
                mold_tool_id=mold.id,
            )
        )
        db.commit()
        return mold.id


def _frozen_v2_layout() -> dict:
    """Recreate the pre-P1-103E geometry used by frozen v1/v2 jobs."""

    legacy = deepcopy(_legacy_v3_layout())
    legacy["catalog_version"] = "p1-103-v2"
    geometry = {
        "customer_name": (1.2, 23.2, 21.8, 7.0, 4.2),
        "mold_label_name": (23.4, 23.2, 39.6, 7.0, 5.2),
        "mold_chinese_short_name": (1.2, 30.6, 23.0, 8.0, 4.0),
        "product_specification": (24.6, 30.6, 38.4, 8.0, 4.4),
    }
    for element in legacy["elements"]:
        values = geometry.get(element["id"])
        if values is not None:
            (
                element["x_mm"],
                element["y_mm"],
                element["width_mm"],
                element["height_mm"],
                element["font_size_mm"],
            ) = values
        if element["id"] == "product_specification":
            element["visible"] = True
    return legacy


def _layout_driven_label(index: int, qr: str) -> str:
    return f'''<article class="mold-label-page" data-layout-catalog="p1-112-v1"><div class="label template-80x40 layout-driven">
      <div class="mold-layout-element mold-layout-text" data-layout-id="board_specification" style="left:1.2mm;top:.8mm;width:61.8mm;height:7mm;font-size:5.6mm;font-weight:900">片料 1100 × 760</div>
      <div class="mold-layout-element mold-layout-text" data-layout-id="product_specification" style="left:1.2mm;top:8.7mm;width:48mm;height:7mm;font-size:4.8mm;font-weight:900">产品 520 × 350 × 300</div>
      <div class="mold-layout-element mold-layout-text" data-layout-id="flute_type" style="left:50mm;top:8.7mm;width:13mm;height:7mm;font-size:4mm;font-weight:800">楞型 BC</div>
      <div class="mold-layout-element mold-layout-text" data-layout-id="customer_name" style="left:1.2mm;top:16.6mm;width:61.8mm;height:7.2mm;font-size:4.3mm;font-weight:900">思迈尔</div>
      <div class="mold-layout-element mold-layout-text" data-layout-id="mold_number" style="left:1.2mm;top:24.6mm;width:61.8mm;height:14.2mm;font-size:6mm;font-weight:900">P162-{index:03d}</div>
      <img class="mold-layout-element mold-layout-qr" data-layout-id="mold_qr" style="left:64.4mm;top:24.6mm;width:14.2mm;height:14.2mm" src="{qr}" alt="二维码">
    </div></article>'''


def _dump_rendered_dom(browser: Path, fixture: Path, work_dir: Path) -> str:
    failures: list[str] = []
    for attempt, headless_flag in enumerate(("--headless=new", "--headless"), start=1):
        profile_dir = work_dir / f"dom-browser-profile-{attempt}"
        try:
            result = subprocess.run(
                [
                    str(browser),
                    headless_flag,
                    "--disable-gpu",
                    "--disable-extensions",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--virtual-time-budget=1500",
                    f"--user-data-dir={profile_dir}",
                    "--dump-dom",
                    fixture.resolve().as_uri(),
                ],
                cwd=work_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired:
            failures.append(f"{headless_flag}: 120 秒超时")
            continue
        if result.returncode == 0 and result.stdout:
            return result.stdout
        failures.append(
            f"{headless_flag}: exit={result.returncode}; "
            f"{(result.stderr or result.stdout or '无诊断输出')[-800:]}"
        )
    pytest.fail("Edge/Chromium 无法运行模具标签 DOM 回归：\n" + "\n".join(failures))


def test_layout_editor_frozen_job_and_overflow_preflight_fail_closed(
    headless_browser: Path,
    tmp_path: Path,
) -> None:
    from app.services.mold_label_layout import default_layout

    state = {
        "published": {
            "version": 0,
            "layout": default_layout(),
            "layout_hash": "0" * 64,
        },
        "can_rollback": False,
    }
    sample = {
        "label_customer_name": "超长客户名称联测",
        "label_mold_name": "现场手写模具标签名称",
        "label_mold_chinese_short_name": "短侧板",
        "label_inventory_code": "X" * 500,
        "label_product_specification": "430 × 280 × 160",
        "label_report_specification": "920 × 610",
        "label_flute_type": "AB",
        "label_cutting_mode": "一开二",
    }
    layout_script = LAYOUT_JS.replace("</script>", r"<\/script>")
    fixture = tmp_path / "mold-layout-editor-behavior.html"
    fixture.write_text(
        f'''<!doctype html><html><head><meta charset="utf-8"><style>
        *{{box-sizing:border-box}}{LAYOUT_CSS}
        </style></head><body>
        <main id="result"></main>
        <button id="moldLayoutOpen" hidden type="button">调整</button>
        <section id="moldLayoutEditor" hidden>
          <button id="moldLayoutClose" type="button">关闭</button>
          <span id="moldLayoutVersion"></span>
          <div id="moldLayoutStage"></div>
          <select id="moldLayoutElement"></select>
          <input id="moldLayoutX" data-mold-layout-field="x_mm">
          <input id="moldLayoutY" data-mold-layout-field="y_mm">
          <input id="moldLayoutWidth" data-mold-layout-field="width_mm">
          <input id="moldLayoutHeight" data-mold-layout-field="height_mm">
          <label id="moldLayoutFontField"><input id="moldLayoutFont" data-mold-layout-field="font_size_mm"></label>
          <label id="moldLayoutAlignField"><select id="moldLayoutAlign" data-mold-layout-field="text_align"><option>left</option><option>center</option><option>right</option></select></label>
          <p id="moldLayoutStatus"></p>
          <button id="moldLayoutSave" type="button">保存</button>
          <button id="moldLayoutDefault" type="button">默认</button>
          <button id="moldLayoutRollback" type="button">回滚</button>
        </section>
        <script>{layout_script}</script>
        <script>
        const result=document.getElementById("result");
        const adminState={json.dumps(state, ensure_ascii=False)};
        const sampleRow={json.dumps(sample, ensure_ascii=False)};
        let fetchCalls=0,postCalls=0,confirmCalls=0;
        window.fetch=async(url,options={{}})=>{{
          fetchCalls+=1;
          if((options.method||"GET").toUpperCase()==="POST")postCalls+=1;
          return {{ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(adminState))}};
        }};
        window.confirm=()=>{{confirmCalls+=1;return true}};
        (async()=>{{
          const frozenInit=await TmMoldLabelLayout.initializeEditor({{
            wideTemplate:true,prototypeMode:true,printJobId:77,
            sampleRow:()=>sampleRow,onPublished:async()=>{{}},
          }});
          result.dataset.frozenInit=String(frozenInit);
          result.dataset.frozenFetches=String(fetchCalls);
          result.dataset.frozenOpenHidden=String(document.getElementById("moldLayoutOpen").hidden);

          const editableInit=await TmMoldLabelLayout.initializeEditor({{
            wideTemplate:true,prototypeMode:true,printJobId:null,
            sampleRow:()=>sampleRow,onPublished:async()=>{{}},
          }});
          result.dataset.editableInit=String(editableInit);
          document.getElementById("moldLayoutOpen").click();
          const selector=document.getElementById("moldLayoutElement");
          selector.value="product_specification";
          selector.dispatchEvent(new Event("change",{{bubbles:true}}));
          const width=document.getElementById("moldLayoutWidth");
          width.value="0.5";
          width.dispatchEvent(new Event("input",{{bubbles:true}}));
          document.getElementById("moldLayoutSave").click();
          await new Promise(resolve=>setTimeout(resolve,150));
          result.dataset.postCalls=String(postCalls);
          result.dataset.confirmCalls=String(confirmCalls);
          result.dataset.status=document.getElementById("moldLayoutStatus").textContent;
          result.dataset.done="true";
        }})().catch(error=>{{result.dataset.failure=String(error?.stack||error)}});
        </script></body></html>''',
        encoding="utf-8",
    )

    dom = _dump_rendered_dom(headless_browser, fixture, tmp_path)
    assert 'data-frozen-init="false"' in dom
    assert 'data-frozen-fetches="0"' in dom
    assert 'data-frozen-open-hidden="true"' in dom
    assert 'data-editable-init="true"' in dom
    assert 'data-post-calls="0"' in dom
    assert 'data-confirm-calls="0"' in dom
    assert 'data-done="true"' in dom
    assert "产品尺寸在当前样例中无法完整显示" in dom
    assert "data-failure=" not in dom


def test_80x40_projection_and_print_fact_are_explicit_and_idempotent(mold_app) -> None:
    from app.models.mold_tool import MoldLabelPrintJob

    app, factory = mold_app
    mold_id = _complete_mold(factory)
    with TestClient(app) as client:
        _login(client, "workshop")
        legacy = client.get(f"/api/warehouse/molds/{mold_id}/label")
        payload = {
            "mold_ids": [mold_id],
            "source": "single",
            "template_version": "mold_80x40_v1",
            "idempotency_key": "p1-62-wide-print-0001",
        }
        created = client.post("/api/warehouse/molds/label-prints", json=payload)
        assert created.status_code == 200, created.text
        wide = client.get(
            f"/api/warehouse/molds/{mold_id}/label",
            params={
                "template_version": "mold_80x40_v1",
                "print_job_id": created.json()["print_job_id"],
            },
        )
        assert legacy.status_code == wide.status_code == 200
        assert "template_version" not in legacy.json()
        body = wide.json()
        assert body["template_version"] == "mold_80x40_v1"
        assert body["template_label"] == "40×80"
        assert body["label_inventory_code"] == "SME-LONG-CODE-1"
        assert body["label_product_name"] == "五层加强纸箱横向标签样例1"
        assert body["label_product_specification"] == "520 × 350 × 300"
        assert body["label_report_specification"] == "1100 × 760"
        assert body["label_flute_type"] == "BC"
        assert body["label_cutting_mode"] == "一开二"
        assert body["lookup_url"] == legacy.json()["lookup_url"]
        assert body["qr_data_url"] == legacy.json()["qr_data_url"]

        assert created.json()["template_version"] == "mold_80x40_v1"
        replay = client.post("/api/warehouse/molds/label-prints", json=payload)
        assert replay.status_code == 200
        assert replay.json()["print_job_id"] == created.json()["print_job_id"]
        assert replay.json()["replayed"] is True
        conflict = client.post(
            "/api/warehouse/molds/label-prints",
            json={**payload, "template_version": "mold_40x30_v1"},
        )
        assert conflict.status_code == 409

    with factory() as db:
        assert db.scalar(select(func.count(MoldLabelPrintJob.id))) == 1
        job = db.scalar(select(MoldLabelPrintJob))
        assert job is not None and job.template_version == "mold_80x40_v1"


def test_80x40_prints_shared_mold_summary_without_guessing_one_product(mold_app) -> None:
    from app.models.mold_tool import MoldLabelPrintJob
    from app.models.product import Product

    app, factory = mold_app
    mold_id = _complete_mold(factory, suffix="2")
    with factory() as db:
        db.add(
            Product(
                customer_id=1,
                product_code="SME-SECOND",
                customer_material_code="SME-SECOND",
                product_name="第二款",
                length_mm=400,
                width_mm=300,
                report_length_mm=900,
                report_width_mm=650,
                flute_type="B",
                mold_tool_id=mold_id,
            )
        )
        db.commit()

    with TestClient(app) as client:
        _login(client, "workshop")
        legacy = client.get(f"/api/warehouse/molds/{mold_id}/label")
        assert legacy.status_code == 200
        assert legacy.json()["label_product_specification"] == "多款见扫码"
        created = client.post(
            "/api/warehouse/molds/label-prints",
            json={
                "mold_ids": [mold_id],
                "source": "single",
                "template_version": "mold_80x40_v1",
                "idempotency_key": "p1-98-multi-print-0001",
            },
        )
        assert created.status_code == 200, created.text
        print_job_id = created.json()["print_job_id"]
        wide = client.get(
            f"/api/warehouse/molds/{mold_id}/label",
            params={
                "template_version": "mold_80x40_v1",
                "print_job_id": print_job_id,
            },
        )
        assert wide.status_code == 200, wide.text
        body = wide.json()
        assert body["product_count"] == 2
        assert body["label_projection_mode"] == "shared_mold"
        assert body["label_inventory_code"] == "SME-LONG-CODE-2 等2款"
        assert body["label_inventory_codes"] == [
            "SME-LONG-CODE-2",
            "SME-SECOND",
        ]
        assert body["label_shared_summary"] == "共用 2 款"
        assert body["label_product_specification"] == "多款见扫码"
        assert body["label_report_specification"] == "多款见扫码"
        assert body["label_flute_type"] == "多款见扫码"
        assert body["label_cutting_mode"] == "一开二/一开一"
        assert body["label_products"] == [
            {
                "product_code": "SME-LONG-CODE-2",
                "product_name": "五层加强纸箱横向标签样例2",
            },
            {"product_code": "SME-SECOND", "product_name": "第二款"},
        ]

        batch = client.get(
            "/api/warehouse/molds/labels",
            params={
                "mold_ids": str(mold_id),
                "template_version": "mold_80x40_v1",
                "print_job_id": print_job_id,
            },
        )
        assert batch.status_code == 200, batch.text
        assert batch.json()["items"][0]["label_projection_mode"] == "shared_mold"
        assert batch.json()["items"][0]["label_shared_summary"] == "共用 2 款"

        assert created.json()["template_version"] == "mold_80x40_v1"
    with factory() as db:
        assert db.scalar(select(func.count(MoldLabelPrintJob.id))) == 1


def test_shared_mold_keeps_single_label_multi_product_summary(mold_app) -> None:
    from app.models.product import Product

    app, factory = mold_app
    mold_id = _complete_mold(factory, suffix="majority")
    with factory() as db:
        db.add_all(
            [
                Product(
                    customer_id=1,
                    product_code=f"SME-MAJORITY-{index}",
                    customer_material_code=f"SME-MAJORITY-{index}",
                    product_name=f"多数规格产品{index}",
                    length_mm=400,
                    width_mm=300,
                    report_length_mm=900,
                    report_width_mm=650,
                    flute_type="BC",
                    default_cutting_mode="一开二",
                    mold_tool_id=mold_id,
                )
                for index in (1, 2)
            ]
        )
        db.commit()

    with TestClient(app) as client:
        _login(client, "workshop")
        created = client.post(
            "/api/warehouse/molds/label-prints",
            json={
                "mold_ids": [mold_id],
                "source": "single",
                "template_version": "mold_80x40_v1",
                "idempotency_key": "p1-103c-majority-spec-0001",
            },
        )
        assert created.status_code == 200, created.text
        printed = client.get(
            f"/api/warehouse/molds/{mold_id}/label",
            params={
                "template_version": "mold_80x40_v1",
                "print_job_id": created.json()["print_job_id"],
            },
        )
        assert printed.status_code == 200, printed.text
        assert printed.json()["label_report_specifications"] == [
            "1100 × 760",
            "900 × 650",
        ]
        assert printed.json()["label_report_specification"] == "多款见扫码"
        assert printed.json()["label_inventory_code"] == "SME-MAJORITY-1 等3款"


def test_shared_mold_many_codes_use_one_representative_and_keep_full_qr_facts(
    mold_app,
) -> None:
    from app.models.product import Product

    app, factory = mold_app
    mold_id = _complete_mold(factory, suffix="many-codes")
    with factory() as db:
        db.add_all(
            [
                Product(
                    customer_id=1,
                    product_code=f"SME-CODE-{index:02d}",
                    customer_material_code=f"SME-CODE-{index:02d}",
                    product_name=f"共用模具产品{index:02d}",
                    length_mm=520,
                    width_mm=350,
                    height_mm=300,
                    report_length_mm=1100,
                    report_width_mm=760,
                    flute_type="BC",
                    default_cutting_mode="一开二",
                    mold_tool_id=mold_id,
                )
                for index in range(2, 11)
            ]
        )
        db.commit()

    with TestClient(app) as client:
        _login(client, "workshop")
        created = client.post(
            "/api/warehouse/molds/label-prints",
            json={
                "mold_ids": [mold_id],
                "source": "single",
                "template_version": "mold_80x40_v1",
                "idempotency_key": "p1-103d-many-codes-0001",
            },
        )
        assert created.status_code == 200, created.text
        printed = client.get(
            f"/api/warehouse/molds/{mold_id}/label",
            params={
                "template_version": "mold_80x40_v1",
                "print_job_id": created.json()["print_job_id"],
            },
        )
        assert printed.status_code == 200, printed.text
        body = printed.json()
        assert body["product_count"] == 10
        assert body["label_inventory_code"] == "SME-CODE-02 等10款"
        assert len(body["label_inventory_codes"]) == 10
        assert len(body["label_products"]) == 10
        assert "SME-LONG-CODE-many-codes" in body["label_inventory_codes"]


def test_v3_allows_missing_hidden_product_dimensions(mold_app) -> None:
    from app.models.product import Product

    app, factory = mold_app
    mold_id = _complete_mold(factory, suffix="no-product-size")
    with factory() as db:
        product = db.scalar(select(Product).where(Product.mold_tool_id == mold_id))
        assert product is not None
        product.length_mm = None
        product.width_mm = None
        product.height_mm = None
        db.commit()

    with TestClient(app) as client:
        _login(client, "workshop")
        created = client.post(
            "/api/warehouse/molds/label-prints",
            json={
                "mold_ids": [mold_id],
                "source": "single",
                "template_version": "mold_80x40_v1",
                "idempotency_key": "p1-103c-hidden-product-size-0001",
            },
        )
        assert created.status_code == 200, created.text
        assert created.json()["label_layout"]["layout"]["catalog_version"] == (
            "p1-112-v1"
        )
        printed = client.get(
            f"/api/warehouse/molds/{mold_id}/label",
            params={
                "template_version": "mold_80x40_v1",
                "print_job_id": created.json()["print_job_id"],
            },
        )
        assert printed.status_code == 200, printed.text
        assert printed.json()["label_product_specification"] == ""


def test_rm9_hash_name_uses_verified_short_customer_and_prints_wide_label(mold_app) -> None:
    from app.models.customer import Customer
    from app.models.mold_tool import MoldTool
    from app.models.product import Product

    app, factory = mold_app
    with factory() as db:
        customer = Customer(
            customer_number=9902,
            customer_code="RM",
            name="苏州瑞明香氛科技股份有限公司",
            payment_term_days=30,
            credit_limit=0,
        )
        mold = MoldTool(
            mold_code="RM-9",
            mold_name="瑞明#9",
            label_name="9#",
            chinese_short_name="纸箱",
            identity_status="frozen",
            rack_location="1F-M-R01-L3-G01",
        )
        db.add_all((customer, mold))
        db.flush()
        db.add(
            Product(
                customer_id=customer.id,
                product_code="9#",
                customer_material_code="9#",
                product_name="纸箱19*10.5*13.5",
                length_mm=190,
                width_mm=105,
                height_mm=135,
                report_length_mm=625,
                report_width_mm=500,
                flute_type="A",
                default_cutting_mode="一开二",
                mold_tool_id=mold.id,
            )
        )
        db.commit()
        mold_id = mold.id

    with TestClient(app) as client:
        _login(client, "workshop")
        created = client.post(
            "/api/warehouse/molds/label-prints",
            json={
                "mold_ids": [mold_id],
                "source": "single",
                "template_version": "mold_80x40_v1",
                "idempotency_key": "p1-62-rm9-print-0001",
            },
        )
        assert created.status_code == 200, created.text
        response = client.get(
            f"/api/warehouse/molds/{mold_id}/label",
            params={
                "template_version": "mold_80x40_v1",
                "print_job_id": created.json()["print_job_id"],
            },
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["label_customer_name"] == "待完善"
    assert body["label_mold_name"] == "9#"
    assert body["label_mold_chinese_short_name"] == "纸箱"
    assert body["label_inventory_code"] == "9#"
    assert body["label_product_name"] == "纸箱19*10.5*13.5"
    assert body["label_product_specification"] == "190 × 105 × 135"
    assert body["label_flute_type"] == "A"
    assert body["label_cutting_mode"] == "一开二"


def test_page_and_warehouse_select_one_frozen_paper_template() -> None:
    for marker in (
        'value="mold_40x30_v1"',
        'value="mold_80x40_v1"',
        'title="40×80 标签样式">40×80',
        "template_version:attempt.templateVersion",
        "moldLabelPrintSignature(source,ids,templateVersion)",
        "不能更换模具或纸型",
    ):
        assert marker in WAREHOUSE
    for marker in (
        'const TEMPLATE_40X30="mold_40x30_v1",TEMPLATE_80X40="mold_80x40_v1"',
        "@page{size:${wideTemplate?\"40mm 80mm\":\"40mm 30mm\"};margin:0}",
        "width:13.9mm;height:13.9mm",
        "transform:translateX(40mm) rotate(90deg)!important",
        'WIDE_PRINTER_QUEUE="Gprinter GP-3120TU - 40x80纵向标签"',
        "内容与单个模具“打印标签”一致",
        "调整40×80标签布局",
        "保存并用于以后打印",
        "TmMoldLabelLayout.labelHtml",
        "print_job_id",
        "waitForQrImages",
        "window.print()",
    ):
        assert marker in LABEL
    for marker in (
        ".label.template-80x40.layout-driven",
        ".mold-layout-text",
        ".mold-layout-qr",
        "translateX(40mm) rotate(90deg)",
    ):
        assert marker in LAYOUT_CSS
    for marker in (
        "board_specification",
        "inventory_code",
        "customer_name",
        "mold_label_name",
        "mold_chinese_short_name",
        "product_specification",
        "mold_number",
        "fitAndValidate",
    ):
        assert marker in LAYOUT_JS
    assert "40mm 80mm" in LABEL
    assert "rotate(90deg)" in LABEL
    assert "--print-x-compensation:2mm" in LABEL
    assert 'product=shared?null:products[0]||null' not in LABEL
    assert "多款见扫码" not in LAYOUT_JS
    assert "body,html{width:40mm;height:auto" in LABEL


@pytest.mark.parametrize("product_count", (5, 11))
def test_actual_layout_javascript_fits_shared_mold_facts_without_clipping(
    product_count: int,
    headless_browser: Path,
    tmp_path: Path,
) -> None:
    from app.services.mold_label_layout import default_layout

    product_codes = [
        f"SME-VERY-LONG-CODE-{index:02d}"
        for index in range(1, product_count + 1)
    ]
    product_sizes = ["520 × 350 × 300" for _index in range(product_count)]
    row = {
        "label_report_specification": "1100 × 760",
        "label_inventory_code": " / ".join(product_codes),
        "label_flute_type": "BC",
        "label_cutting_mode": "一开二",
        "label_customer_name": "苏州思迈尔包装科技有限公司",
        "label_mold_name": "3D30268-超长现场手写标签",
        "label_mold_chinese_short_name": "" if product_count == 5 else "加强箱",
        "label_product_specification": "多款见扫码",
        "label_mold_number": "P162-共用模具",
        "qr_data_url": _qr_data_url(),
        "products": [
            {"product_code": code, "product_name": f"共用模具纸箱{index:02d}"}
            for index, code in enumerate(product_codes, start=1)
        ],
    }
    envelope = {"version": 0, "layout": default_layout()}
    fixture = tmp_path / f"p1-103-layout-js-{product_count}.html"
    fixture.write_text(
        '<!doctype html><html><head><meta charset="utf-8">'
        + _current_print_styles()
        + "</head><body><main id=\"labels\"></main><script>"
        + LAYOUT_JS.replace("</script>", "<\\/script>")
        + "</script><script>"
        + f"const row={json.dumps(row, ensure_ascii=False)};"
        + f"const envelope={json.dumps(envelope, ensure_ascii=False)};"
        + 'const labels=document.getElementById("labels");'
        + "labels.innerHTML=TmMoldLabelLayout.labelHtml(row,envelope);"
        + "const failures=TmMoldLabelLayout.fitAndValidate(labels);"
        + 'document.body.dataset.fitFailures=failures.join("|");'
        + 'document.body.dataset.elementCount=String(labels.querySelectorAll("[data-layout-id]").length);'
        + 'document.body.dataset.productSizeCount=String(labels.querySelectorAll("[data-layout-id=product_specification]").length);'
        + 'document.body.dataset.inventoryCount=String(labels.querySelectorAll("[data-layout-id=inventory_code]").length);'
        + "</script></body></html>",
        encoding="utf-8",
    )
    rendered = _dump_rendered_dom(headless_browser, fixture, tmp_path)
    assert 'data-fit-failures=""' in rendered
    assert 'data-element-count="6"' in rendered
    assert 'data-product-size-count="1"' in rendered
    assert 'data-inventory-count="0"' in rendered
    assert "多款见扫码" in rendered
    assert ">待完善</div>" not in rendered


def test_current_identity_order_changes_while_v1_v2_snapshots_stay_frozen(
    headless_browser: Path,
    tmp_path: Path,
) -> None:
    from app.services.mold_label_layout import default_layout

    row = {
        "label_report_specification": "890 × 650",
        "label_inventory_code": "22700002",
        "label_flute_type": "E",
        "label_cutting_mode": "一开一",
        "label_customer_name": "瑞明",
        "label_mold_number": "9#",
        "label_mold_name": "9#",
        "label_mold_chinese_short_name": "防静电单回路",
        "label_product_specification": "290 × 140 × 120",
        "qr_data_url": _qr_data_url(),
        "products": [],
    }
    v3 = default_layout()
    v2 = _frozen_v2_layout()
    v1 = deepcopy(v2)
    v1["catalog_version"] = "p1-103-v1"
    fixture = tmp_path / "p1-103-values-only-versioned-renderer.html"
    fixture.write_text(
        '<!doctype html><html><head><meta charset="utf-8">'
        + _current_print_styles()
        + '</head><body><main id="v3"></main><main id="v2"></main><main id="v1"></main><script>'
        + LAYOUT_JS.replace("</script>", "<\\/script>")
        + "</script><script>"
        + f"const row={json.dumps(row, ensure_ascii=False)};"
        + f"const v3={{version:0,layout:{json.dumps(v3, ensure_ascii=False)}}};"
        + f"const v2={{version:1,layout:{json.dumps(v2, ensure_ascii=False)}}};"
        + f"const v1={{version:1,layout:{json.dumps(v1, ensure_ascii=False)}}};"
        + 'document.getElementById("v3").innerHTML=TmMoldLabelLayout.labelHtml(row,v3);'
        + 'document.getElementById("v2").innerHTML=TmMoldLabelLayout.labelHtml(row,v2);'
        + 'document.getElementById("v1").innerHTML=TmMoldLabelLayout.labelHtml(row,v1);'
        + 'document.body.dataset.v3Text=[...document.querySelectorAll("#v3 .mold-layout-text")].map(node=>node.textContent).join("|");'
        + 'document.body.dataset.v2Text=[...document.querySelectorAll("#v2 .mold-layout-text")].map(node=>node.textContent).join("|");'
        + 'document.body.dataset.v1Text=[...document.querySelectorAll("#v1 .mold-layout-text")].map(node=>node.textContent).join("|");'
        + "</script></body></html>",
        encoding="utf-8",
    )
    rendered = _dump_rendered_dom(headless_browser, fixture, tmp_path)
    assert (
        'data-v3-text="片料 890 × 650|产品 290 × 140 × 120|楞型 E|瑞明|9#"'
    ) in rendered
    assert (
        'data-v2-text="890 × 650|22700002|E|一开一|瑞明|9#|'
        '防静电单回路|290 × 140 × 120"'
    ) in rendered
    assert (
        'data-v1-text="片料 890 × 650|纸箱 22700002|楞 E|开 一开一|瑞明|9#|'
        '中文 防静电单回路|尺寸 290 × 140 × 120"'
    ) in rendered


def test_job52_like_v2_layout_fits_compact_ten_product_projection(
    headless_browser: Path,
    tmp_path: Path,
) -> None:
    from app.services.mold_label_layout import default_layout

    row = {
        "label_report_specification": "1292 × 1061",
        "label_inventory_code": "21301850 等10款",
        "label_flute_type": "BE",
        "label_cutting_mode": "一开一",
        "label_customer_name": "瑞华",
        "label_mold_name": "935*620*23/26",
        "label_mold_chinese_short_name": "24*36天地盒",
        "label_product_specification": "935 × 620 × 25",
        "qr_data_url": _qr_data_url(),
        "products": [],
    }
    v2 = _frozen_v2_layout()
    for element in v2["elements"]:
        if element["id"] == "customer_name":
            element["width_mm"] = 21.0
        if element["id"] == "mold_label_name":
            element["x_mm"] = 22.6
            element["width_mm"] = 40.4
    fixture = tmp_path / "p1-103d-job52-v2-compact.html"
    fixture.write_text(
        '<!doctype html><html><head><meta charset="utf-8">'
        + _current_print_styles()
        + '</head><body><main id="labels"></main><script>'
        + LAYOUT_JS.replace("</script>", "<\\/script>")
        + "</script><script>"
        + f"const row={json.dumps(row, ensure_ascii=False)};"
        + f"const envelope={{version:0,layout:{json.dumps(v2, ensure_ascii=False)}}};"
        + 'const labels=document.getElementById("labels");'
        + "labels.innerHTML=TmMoldLabelLayout.labelHtml(row,envelope);"
        + "const failures=TmMoldLabelLayout.fitAndValidate(labels);"
        + 'document.body.dataset.fitFailures=failures.join("|");'
        + 'document.body.dataset.renderedText=[...labels.querySelectorAll(".mold-layout-text")].map(node=>node.textContent).join("|");'
        + "</script></body></html>",
        encoding="utf-8",
    )
    rendered = _dump_rendered_dom(headless_browser, fixture, tmp_path)
    assert 'data-fit-failures=""' in rendered
    assert "21301850 等10款" in rendered
    assert "935 × 620 × 25" in rendered


@pytest.mark.parametrize("label_count", (1, 2, 100))
def test_40x80_feed_uses_one_portrait_page_with_one_inner_rotation(
    label_count: int,
    headless_browser: Path,
    tmp_path: Path,
) -> None:
    qr = _qr_data_url()
    labels = "".join(
        _layout_driven_label(index, qr)
        for index in range(1, label_count + 1)
    )
    fixture = tmp_path / f"p1-62-{label_count}.html"
    output = tmp_path / f"p1-62-{label_count}.pdf"
    fixture.write_text(
        '<!doctype html><html class="template-80x40"><head><meta charset="utf-8">'
        + _current_print_styles()
        + '<style>@page{size:40mm 80mm;margin:0}</style></head>'
        + f'<body class="template-80x40"><section id="previewContent"><main id="labels" class="labels">{labels}</main></section></body></html>',
        encoding="utf-8",
    )
    _print_to_pdf(headless_browser, fixture, output, tmp_path)
    reader = PdfReader(output)
    assert len(reader.pages) == label_count
    for page_number, page in enumerate(reader.pages, start=1):
        width_mm = float(page.mediabox.width) * POINTS_TO_MM
        height_mm = float(page.mediabox.height) * POINTS_TO_MM
        assert width_mm == pytest.approx(40.0, abs=0.25)
        assert height_mm == pytest.approx(80.0, abs=0.25)
        assert height_mm > width_mm
        text = " ".join((page.extract_text() or "").split())
        compact_text = "".join(text.split())
        for expected in (
            "1100 × 760",
            "520 × 350 × 300",
            "BC",
            "思迈尔",
            f"P162-{page_number:03d}",
        ):
            assert "".join(expected.split()) in compact_text


def test_40x80_portrait_pixels_keep_rotated_content_inside_physical_page(
    headless_browser: Path,
    tmp_path: Path,
) -> None:
    fitz = pytest.importorskip("fitz")
    qr = _qr_data_url()
    fixture = tmp_path / "p1-62-visible-bounds.html"
    output = tmp_path / "p1-62-visible-bounds.pdf"
    fixture.write_text(
        '<!doctype html><html class="template-80x40"><head><meta charset="utf-8">'
        + _current_print_styles()
        + '<style>@page{size:40mm 80mm;margin:0}</style></head>'
        + f'<body class="template-80x40"><section id="previewContent"><main id="labels" class="labels">{_layout_driven_label(257, qr)}</main></section></body></html>',
        encoding="utf-8",
    )
    _print_to_pdf(headless_browser, fixture, output, tmp_path)

    document = fitz.open(output)
    page = document[0]
    pixmap = page.get_pixmap(matrix=fitz.Matrix(300 / 72, 300 / 72), colorspace=fitz.csGRAY)
    dark_pixels = [
        (index % pixmap.width, index // pixmap.width)
        for index, value in enumerate(pixmap.samples)
        if value < 180
    ]
    assert dark_pixels, "40×80 标签渲染后不应为空白"
    left = min(point[0] for point in dark_pixels)
    right = max(point[0] for point in dark_pixels)
    top = min(point[1] for point in dark_pixels)
    bottom = max(point[1] for point in dark_pixels)
    pixels_per_mm = 300 / 25.4
    assert left >= 0.5 * pixels_per_mm
    assert right <= pixmap.width - 0.5 * pixels_per_mm
    assert top >= 0.7 * pixels_per_mm
    assert bottom <= pixmap.height - 0.7 * pixels_per_mm
    assert (bottom - top) / pixels_per_mm >= 60
