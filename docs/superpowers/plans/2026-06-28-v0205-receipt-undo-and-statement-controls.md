# v0.20.5 Receipt Undo and Statement Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix delivery receipt rollback behavior, monthly statement customer selection, and statement edit/cancel controls, then write user-friendly release notes.

**Architecture:** Keep the current monolithic `static/index.html` Vue shell and FastAPI backend endpoints. Add the smallest possible backend actions for undoing confirmed receipts and editing/canceling statements, then wire the existing delivery/finance views to those endpoints. Update version notes and user-facing docs in plain language only.

**Tech Stack:** FastAPI, SQLAlchemy, Vue 2 in `static/index.html`, Pytest, SQLite.

---

### Task 1: Add failing tests for receipt undo and statement controls

**Files:**
- Modify: `tests/test_phase8_finance.py`
- Modify: `tests/test_phase1_delivery_statusflow.py`
- Modify: `tests/test_phase14_frontend.py`

- [ ] **Step 1: Write the failing test**

```python
def test_cancel_return_receipt_restores_delivery_state(finance_api_app):
    client, _ = finance_api_app
    receipt = _create_receipt(client)
    response = client.post(f"/api/finance/return_receipts/{receipt['id']}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_phase8_finance.py::test_cancel_return_receipt_restores_delivery_state -v`
Expected: FAIL because the endpoint does not exist yet.

- [ ] **Step 3: Write minimal implementation**

No code yet in this task.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_phase8_finance.py::test_cancel_return_receipt_restores_delivery_state -v`
Expected: PASS after implementation.

---

### Task 2: Implement backend receipt undo and statement edit/cancel endpoints

**Files:**
- Modify: `app/api/finance.py`
- Modify: `app/models/finance.py` if any lightweight status helpers are needed

- [ ] **Step 1: Write the failing test**

Add tests that cover:
```python
def test_statement_edit_and_cancel_are_available_for_unsettled_and_settled(finance_api_app):
    ...
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run:
`pytest tests/test_phase8_finance.py tests/test_phase15_requisition_finance_adjustments.py -k "statement or receipt" -v`
Expected: missing endpoint or missing behavior failures.

- [ ] **Step 3: Write minimal implementation**

Implement:
```python
@router.post("/return_receipts/{receipt_id}/cancel")
def cancel_return_receipt(...):
    ...
```

Implement statement operations:
```python
@router.put("/statements/{statement_id}")
def update_statement(...):
    ...

@router.post("/statements/{statement_id}/cancel")
def cancel_statement(...):
    ...
```

- [ ] **Step 4: Run the focused tests to verify they pass**

Run the same pytest command and confirm the new assertions pass.

---

### Task 3: Wire delivery and finance UI to the new actions

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: Write the failing UI assertions**

Add or update frontend tests to assert:
```python
assert '取消回单' in INDEX
assert 'axios.post(`/api/finance/return_receipts/${receiptId}/cancel`)' in INDEX
assert 'axios.put(`/api/finance/statements/${row.id}`' in INDEX
assert 'axios.post(`/api/finance/statements/${row.id}/cancel`)' in INDEX
```

- [ ] **Step 2: Run the focused frontend tests and verify failure**

Run: `pytest tests/test_phase14_frontend.py -k "receipt or statement" -v`
Expected: fail until UI is wired.

- [ ] **Step 3: Write minimal implementation**

Update delivery row actions so confirmed receipts show `取消回单`.
Update statement rows so both unsettled and settled rows show `编辑` and `取消` with confirmations.
Update statement modal customer selection to load eligible customers for the current month and refresh lines on change.

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `pytest tests/test_phase14_frontend.py -k "receipt or statement" -v`
Expected: PASS.

---

### Task 4: Simplify version notes and user-facing release language

**Files:**
- Modify: `app/version.py`
- Modify: `docs/ERP_PROJECT_STATE.md`
- Modify: `docs/CODEX_HANDOFF.md`
- Modify: `docs/BUSINESS_RULES.md` if a plain-language note belongs there

- [ ] **Step 1: Write the failing test**

Add a test that checks the version changelog / release note text uses plain business language and mentions receipt undo, monthly statement customer switching, and safe edit/cancel behavior.

- [ ] **Step 2: Run the test and verify it fails**

Run: `pytest tests/test_factory_update_script.py tests/test_phase12_uat.py -k "version or release or changelog" -v`
Expected: failure until wording is updated.

- [ ] **Step 3: Write minimal implementation**

Rewrite user-facing notes to plain Chinese, avoiding technical jargon.

- [ ] **Step 4: Run the test to verify it passes**

Run the same pytest command and confirm pass.

---

### Task 5: End-to-end verification and commit

**Files:**
- All modified files above

- [ ] **Step 1: Run focused pytest suites**

Run:
`pytest tests/test_phase1_delivery_statusflow.py tests/test_phase8_finance.py tests/test_phase14_frontend.py tests/test_factory_update_script.py -v`

- [ ] **Step 2: Manually verify in the browser**

Check:
- delivery row shows `取消回单` after receipt confirmation
- statement modal can switch customers and refresh pending lines
- statement rows show edit/cancel actions
- release notes are plain-language

- [ ] **Step 3: Commit**

```bash
git add app/api/finance.py app/version.py static/index.html tests/test_phase1_delivery_statusflow.py tests/test_phase8_finance.py tests/test_phase14_frontend.py tests/test_factory_update_script.py docs/ERP_PROJECT_STATE.md docs/CODEX_HANDOFF.md docs/BUSINESS_RULES.md
git commit -m "v0.20.5 improve receipt undo and monthly reconciliation controls"
```

---

## Coverage check

- Delivery receipt undo: Task 1, Task 2, Task 3
- Statement customer selection: Task 2, Task 3
- Statement edit/cancel: Task 2, Task 3
- Plain-language release notes: Task 4
- Verification before completion: Task 5

