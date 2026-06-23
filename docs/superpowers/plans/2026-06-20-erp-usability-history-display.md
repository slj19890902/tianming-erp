# ERP Usability and History Display Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hide old system names from daily ERP usage, add TM-style history display, improve pagination/search UX, fix new-order customer/product selection, and rehearse history renumber/status archiving in a sandbox without batch-changing the main database.

**Architecture:** Keep main-database historical identifiers unchanged for now and introduce a display-layer compatibility model in the order APIs/UI. Use focused API pagination/search improvements and frontend grouping modes for low-risk UX changes. Rehearse actual history renumbering and archive-status rewrites only in a sandbox copy with exported mappings and verification reports.

**Tech Stack:** FastAPI, SQLAlchemy, SQLite, Vue-in-static-index pattern, pytest, local migration rehearsal scripts

---

### Task 1: Audit-facing tests for hidden legacy naming and trial checklist wording

**Files:**
- Modify: `D:\纸箱厂erp软件搭建\tests\test_phase10_frontend.py`
- Modify: `D:\纸箱厂erp软件搭建\docs\go_live_checklists\MANUAL_ACCEPTANCE_CHECKLIST.md`
- Modify: `D:\纸箱厂erp软件搭建\docs\go_live_checklists\DAILY_OPERATION_GUIDE.md`
- Modify: `D:\纸箱厂erp软件搭建\docs\go_live_checklists\GO_LIVE_READINESS_SUMMARY.md`

- [ ] **Step 1: Write the failing tests**

```python
def test_order_search_placeholder_hides_legacy_name() -> None:
    source = Path("static/index.html").read_text(encoding="utf-8")
    assert "RUIDA" not in source
    assert "瑞达" not in source
    assert "TM" in source


def test_trial_checklist_uses_first_real_login_and_hides_legacy_name() -> None:
    checklist = Path("docs/go_live_checklists/MANUAL_ACCEPTANCE_CHECKLIST.md").read_text(encoding="utf-8")
    assert "四个岗位各自先登录一次" in checklist
    assert "RUIDA" not in checklist
    assert "瑞达" not in checklist
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_phase10_frontend.py -q`
Expected: FAIL because current placeholder/checklist still contains `RUIDA`

- [ ] **Step 3: Write minimal implementation**

Update the user-facing checklist wording and static placeholder text to use `TM` / `历史订单` only.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_phase10_frontend.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_phase10_frontend.py docs/go_live_checklists/MANUAL_ACCEPTANCE_CHECKLIST.md docs/go_live_checklists/DAILY_OPERATION_GUIDE.md docs/go_live_checklists/GO_LIVE_READINESS_SUMMARY.md static/index.html
git commit -m "test: lock hidden legacy naming in user-facing docs"
```

### Task 2: Order API display model and TM-history search contract

**Files:**
- Modify: `D:\纸箱厂erp软件搭建\app\api\orders.py`
- Modify: `D:\纸箱厂erp软件搭建\tests\test_phase5_orders.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_order_list_returns_display_order_number_without_legacy_prefix(client, seeded_history_order):
    response = client.get("/api/orders", params={"keyword": "TM"})
    assert response.status_code == 200
    row = response.json()["items"][0]
    assert row["display_order_number"].startswith("TM")
    assert "RUIDA" not in json.dumps(row, ensure_ascii=False)
    assert "瑞达" not in json.dumps(row, ensure_ascii=False)


def test_order_detail_hides_internal_legacy_order_number(client, seeded_history_order):
    response = client.get(f"/api/orders/{seeded_history_order.id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["display_order_number"].startswith("TM")
    assert "RUIDA" not in json.dumps(payload, ensure_ascii=False)
    assert "瑞达" not in json.dumps(payload, ensure_ascii=False)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_phase5_orders.py -q`
Expected: FAIL because API still returns raw `order_number` and search assumes `RUIDA`

- [ ] **Step 3: Write minimal implementation**

Implement:

```python
def _is_history_order(order: Order) -> bool: ...
def _history_display_order_number(order: Order) -> str: ...
def _display_order_number(order: Order) -> str: ...
```

Return `display_order_number` in list/detail payloads, remove raw internal history number from normal API output, and let `keyword`/`order_number` search match TM-style display values plus existing customer/date/product filters.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_phase5_orders.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/api/orders.py tests/test_phase5_orders.py
git commit -m "feat: add TM display numbers for history orders"
```

### Task 3: Finance and related business APIs stop exposing old-system names

**Files:**
- Modify: `D:\纸箱厂erp软件搭建\app\api\finance.py`
- Modify: `D:\纸箱厂erp软件搭建\app\api\incoming.py`
- Modify: `D:\纸箱厂erp软件搭建\app\api\requisition.py`
- Modify: `D:\纸箱厂erp软件搭建\tests\test_phase8_finance.py`
- Modify: `D:\纸箱厂erp软件搭建\tests\test_phase6_incoming.py`
- Modify: `D:\纸箱厂erp软件搭建\tests\test_phase11_requisition.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_finance_payload_hides_legacy_names(client, finance_seed):
    response = client.get("/api/finance/statements")
    assert response.status_code == 200
    assert "RUIDA" not in json.dumps(response.json(), ensure_ascii=False)
    assert "瑞达" not in json.dumps(response.json(), ensure_ascii=False)


def test_requisition_and_incoming_payloads_hide_legacy_names(client, requisition_seed):
    assert "RUIDA" not in client.get("/api/requisition/search_history").text
    assert "RUIDA" not in client.get("/api/incoming/pending").text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_phase8_finance.py tests/test_phase6_incoming.py tests/test_phase11_requisition.py -q`
Expected: FAIL where raw order numbers or remarks still expose old-system text

- [ ] **Step 3: Write minimal implementation**

Normalize finance/requisition/incoming payloads to use shared display order number and sanitized history labels/remarks for user-facing responses.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_phase8_finance.py tests/test_phase6_incoming.py tests/test_phase11_requisition.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/api/finance.py app/api/incoming.py app/api/requisition.py tests/test_phase8_finance.py tests/test_phase6_incoming.py tests/test_phase11_requisition.py
git commit -m "feat: hide old-system naming in business APIs"
```

### Task 4: Customer and product pagination/search upgrades

**Files:**
- Modify: `D:\纸箱厂erp软件搭建\app\api\customers.py`
- Modify: `D:\纸箱厂erp软件搭建\app\api\products.py`
- Modify: `D:\纸箱厂erp软件搭建\tests\test_phase3_api.py`
- Modify: `D:\纸箱厂erp软件搭建\tests\test_excel_history_import.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_customer_list_defaults_to_25_and_supports_keyword(client, seeded_customers):
    response = client.get("/api/customers")
    payload = response.json()
    assert payload["page_size"] == 25
    assert "total_pages" in payload


def test_product_list_supports_customer_code_name_spec_material_filters(client, seeded_products):
    response = client.get("/api/master/products", params={
        "customer_id": 1,
        "product_code": "A01",
        "product_name": "纸箱",
        "spec": "380",
        "material": "K=A",
    })
    assert response.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_phase3_api.py tests/test_excel_history_import.py -q`
Expected: FAIL because default page sizes / filters are not aligned yet

- [ ] **Step 3: Write minimal implementation**

Set customer page default to 25 and add consistent pagination fields. Extend products list filtering to support customer, code, name, spec/size, material, with paged return shape.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_phase3_api.py tests/test_excel_history_import.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/api/customers.py app/api/products.py tests/test_phase3_api.py tests/test_excel_history_import.py
git commit -m "feat: paginate and search customers and products"
```

### Task 5: Frontend product/customer pickers, customer product table, and new-order validation

**Files:**
- Modify: `D:\纸箱厂erp软件搭建\static\index.html`
- Modify: `D:\纸箱厂erp软件搭建\tests\test_phase10_frontend.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_order_form_requires_customer_before_product_selection() -> None:
    source = Path("static/index.html").read_text(encoding="utf-8")
    assert "未选择客户前" in source or "先选择客户" in source
    assert "quantity must be > 0" or "数量必须大于 0"


def test_customer_product_table_has_required_columns() -> None:
    source = Path("static/index.html").read_text(encoding="utf-8")
    for label in ["存货编码", "产品名称", "规格", "材质", "单价", "图纸版本", "状态", "操作"]:
        assert label in source
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_phase10_frontend.py -q`
Expected: FAIL because UI still loads legacy-style search hints and weaker order-form flow

- [ ] **Step 3: Write minimal implementation**

Implement:
- customer-first order form
- paged customer/product loading
- per-customer product search
- blank default quantities
- save disabled / blocked until valid
- customer product list columns and paging controls

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_phase10_frontend.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add static/index.html tests/test_phase10_frontend.py
git commit -m "feat: improve order form and customer product UX"
```

### Task 6: Frontend order grouping/sorting modes without breaking base list

**Files:**
- Modify: `D:\纸箱厂erp软件搭建\static\index.html`
- Modify: `D:\纸箱厂erp软件搭建\tests\test_phase10_frontend.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_frontend_contains_group_modes_for_orders() -> None:
    source = Path("static/index.html").read_text(encoding="utf-8")
    for token in ["按客户名称分组", "按客户单号分组", "普通列表", "按日期排序", "子订单"]:
        assert token in source
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_phase10_frontend.py -q`
Expected: FAIL because grouping controls are not present

- [ ] **Step 3: Write minimal implementation**

Add view mode controls and frontend grouping renderers using existing order payload and `display_order_number`, preserving search/pagination/detail behavior.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_phase10_frontend.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add static/index.html tests/test_phase10_frontend.py
git commit -m "feat: add grouped order list views"
```

### Task 7: Sandbox rehearsal for history renumbering and archive-status normalization

**Files:**
- Create: `D:\纸箱厂erp软件搭建\scripts\migration\rehearse_history_display_and_status.py`
- Create: `D:\纸箱厂erp软件搭建\tests\migration\test_rehearse_history_display_and_status.py`

- [ ] **Step 1: Write the failing test**

```python
def test_rehearsal_converts_history_numbers_to_tm_and_preserves_uniqueness(tmp_path):
    result = run_rehearsal_copy(tmp_path)
    assert result["tm_orders"] > 0
    assert result["remaining_ruida"] == 0
    assert result["duplicate_order_numbers"] == 0
    assert result["integrity_check"] == "ok"
    assert result["foreign_key_check"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/migration/test_rehearse_history_display_and_status.py -q`
Expected: FAIL because script does not exist yet

- [ ] **Step 3: Write minimal implementation**

Create a script that:
- copies main DB to sandbox
- identifies history orders from migration maps
- writes TM-style numbers in sandbox only
- exports `sales_order_id / old_order_number / new_order_number / customer / order_date`
- normalizes sandbox history display/archive-related fields
- verifies pending queues exclude history rows

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/migration/test_rehearse_history_display_and_status.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/migration/rehearse_history_display_and_status.py tests/migration/test_rehearse_history_display_and_status.py
git commit -m "feat: add history display and status rehearsal script"
```

### Task 8: Full regression, report, and handoff updates

**Files:**
- Create: `D:\纸箱厂erp软件搭建\docs\go_live_checklists\ERP_USABILITY_AND_HISTORY_DISPLAY_OPTIMIZATION_YYYYMMDD_HHMMSS.md`
- Modify: `D:\纸箱厂erp软件搭建\docs\CODEX_HANDOFF.md`
- Modify: `D:\纸箱厂erp软件搭建\docs\MIGRATION_CHECKLIST.md`
- Modify: `D:\纸箱厂erp软件搭建\docs\MIGRATION_RUNBOOK.md`
- Modify: `D:\纸箱厂erp软件搭建\docs\go_live_checklists\GO_LIVE_READINESS_SUMMARY.md`
- Modify: `D:\纸箱厂erp软件搭建\docs\go_live_checklists\MANUAL_ACCEPTANCE_CHECKLIST.md`
- Modify: `D:\纸箱厂erp软件搭载\docs\go_live_checklists\DAILY_OPERATION_GUIDE.md`

- [ ] **Step 1: Run targeted and full regression tests**

Run:

```bash
pytest tests/test_phase3_api.py tests/test_phase5_orders.py tests/test_phase6_incoming.py tests/test_phase8_finance.py tests/test_phase10_frontend.py tests/test_phase11_requisition.py tests/migration/test_rehearse_history_display_and_status.py -q
```

Then:

```bash
pytest -q
```

Expected: PASS

- [ ] **Step 2: Run sandbox rehearsal and capture outputs**

Run:

```bash
python scripts/migration/rehearse_history_display_and_status.py --sqlite-path data/carton_erp.sqlite3
```

Expected:
- sandbox path emitted
- TM conversion counts emitted
- remaining legacy-visible count 0 in sandbox business layer
- integrity/fk checks clean

- [ ] **Step 3: Write the report**

Document:
- no history migration run
- no direct main-db renumber/status rewrite
- no `legacy_*` changes
- first real login included in manual trial step 1
- code optimizations completed
- sandbox rehearsal results
- legacy-name cleanup status across UI/API/docs
- remaining items requiring explicit main-db authorization

- [ ] **Step 4: Update handoff and go-live docs**

Add concise execution summary and new manual-trial ordering.

- [ ] **Step 5: Commit**

```bash
git add docs/ app/ static/ tests/ scripts/
git commit -m "feat: improve ERP usability and hide old-system history naming"
```

