from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from test_p1_09b_query_scaling import _login, delivery_scaling_app


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _method_body(name: str) -> str:
    pattern = rf"(?:async\s+)?{re.escape(name)}\([^)]*\)\s*\{{"
    match = re.search(pattern, INDEX)
    assert match is not None, f"missing Vue method: {name}"
    next_method = re.search(
        r"\n\s{10,}(?:async\s+)?[A-Za-z_$][\w$]*\([^)]*\)\s*\{",
        INDEX[match.end() :],
    )
    assert next_method is not None, f"cannot delimit Vue method: {name}"
    body = INDEX[match.end() : match.end() + next_method.start()]
    return re.sub(r"\n\s*},\s*$", "", body)


def _run_node(tmp_path: Path, name: str, source: str) -> None:
    node = shutil.which("node")
    assert node, "Node.js is required for delivery picker behavior validation"
    target = tmp_path / name
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_delivery_picker_browses_without_keyword_and_uses_ten_rows() -> None:
    loader = _method_body("loadDeliveryBatchItems")

    assert "list_all: true" in loader
    assert "page_size: this.deliveryBatchPicker.page_size" in loader
    assert "每页 {{ deliveryBatchPicker.page_size }} 行" in INDEX
    assert INDEX.count(
        'this.deliveryBatchPicker = { visible:false, loading:false, keyword:"", items:[], selected:{}, page:1, page_size:10'
    ) >= 3
    assert 'deliveryBatchPicker: { visible: false, loading: false, keyword: "", items: [], selected: {}, page:1, page_size:10' in INDEX


def test_small_and_tianhua_sized_candidate_sets_are_visible_without_search(
    tmp_path: Path,
) -> None:
    loader = _method_body("loadDeliveryBatchItems")
    source = f"""
const AsyncFunction = Object.getPrototypeOf(async function(){{}}).constructor;
const requests = [];
global.axios = {{ get(url, config) {{ return new Promise((resolve, reject) => requests.push({{url, config, resolve, reject}})); }} }};
const vm = {{
  deliveryForm: {{customer_id:7}},
  deliveryBatchPicker: {{visible:true,loading:false,keyword:"",items:[],selected:{{}},page:1,page_size:10,total:0,total_pages:1,request_token:0,message:"",error:false}},
  deliveryBatchRequestSequence: 0,
  deliveryKitSummary() {{ return ""; }},
  errorMessage(error) {{ return error.message || String(error); }},
  showToast() {{}},
}};
vm.load = new AsyncFunction("page", {json.dumps(loader, ensure_ascii=False)}).bind(vm);
const items = (start, count) => Array.from({{length:count}}, (_, index) => ({{order_item_id:start + index, product_code:`TH-${{start + index}}`}}));
(async () => {{
  const small = vm.load(1);
  if (requests[0].config.params.list_all !== true || requests[0].config.params.page_size !== 10 || requests[0].config.params.q !== undefined) throw new Error("blank browse contract missing");
  requests[0].resolve({{data:{{items:items(1,3),page:1,page_size:10,total:3,total_pages:1}}}});
  await small;
  if (vm.deliveryBatchPicker.items.length !== 3 || vm.deliveryBatchPicker.total_pages !== 1) throw new Error("small customer did not show all rows");

  const seen = [];
  for (const [page, start, count] of [[1,1,10],[2,11,10],[3,21,3]]) {{
    const pending = vm.load(page);
    const request = requests[requests.length - 1];
    if (request.config.params.page !== page || request.config.params.page_size !== 10 || request.config.params.list_all !== true) throw new Error("ten-row paging parameters changed");
    request.resolve({{data:{{items:items(start,count),page,page_size:10,total:23,total_pages:3}}}});
    await pending;
    if (vm.deliveryBatchPicker.items.length !== count) throw new Error(`page ${{page}} row count mismatch`);
    seen.push(...vm.deliveryBatchPicker.items.map(item => item.order_item_id));
  }}
  if (seen.length !== 23 || new Set(seen).size !== 23 || seen[0] !== 1 || seen[22] !== 23) throw new Error("Tianhua pages lost or duplicated candidates");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(tmp_path, "p1-38a-delivery-picker.js", source)


def test_prefetched_rows_are_reused_when_picker_opens() -> None:
    toggle = _method_body("toggleDeliveryBatchPicker")
    assert "!this.deliveryBatchPicker.items.length" in toggle
    assert "!this.deliveryBatchPicker.loading" in toggle
    assert "await this.loadDeliveryBatchItems()" in toggle


def test_real_pending_api_returns_complete_ten_row_pages_without_keyword(
    delivery_scaling_app,
) -> None:
    app, _engine, ids = delivery_scaling_app
    with TestClient(app) as client:
        _login(client, "p109b-admin")
        pages = [
            client.get(
                "/api/deliveries/pending-items/search",
                params={
                    "customer_id": ids["customer_id"],
                    "list_all": "true",
                    "page": page,
                    "page_size": 10,
                },
            )
            for page in (1, 2, 3, 4, 5)
        ]

    assert all(response.status_code == 200 for response in pages)
    bodies = [response.json() for response in pages]
    assert [body["page"] for body in bodies] == [1, 2, 3, 4, 5]
    assert [len(body["items"]) for body in bodies] == [10, 10, 10, 10, 8]
    assert {body["total"] for body in bodies} == {48}
    assert {body["total_pages"] for body in bodies} == {5}
    identities = [
        item["order_item_id"]
        for body in bodies
        for item in body["items"]
    ]
    assert len(identities) == len(set(identities)) == 48