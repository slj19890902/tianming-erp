# Phase 13 Production And Historical Requisition Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Promote the validated SQLite database into production safely, provide reliable Windows startup deployment, and preview historical requisition mappings from the supplied workbook without modifying ERP data.

**Architecture:** Production promotion is an explicit, integrity-checked operation that backs up the current target to NAS before replacement, uses SQLite's backup API for the validated copy, and writes production environment settings atomically. Historical Excel ingestion is a separate two-stage pipeline: repair malformed workbook XML into a temporary copy, extract and deduplicate mappings, then optionally persist them only when `--commit` is supplied.

**Tech Stack:** Python 3.12, SQLite, SQLAlchemy 2.x, Alembic, FastAPI, Uvicorn, pandas, openpyxl, Windows batch/Task Scheduler/NSSM.

---

### Task 1: Lock Deployment Safety With Tests

**Files:**
- Create: `tests/test_phase13_deployment.py`
- Create: `scripts/deploy_production.py`

- [ ] **Step 1: Write failing tests**

Cover source integrity rejection, same-source production finalization, NAS backup before replacement, atomic `.env` update, and explicit source selection.

- [ ] **Step 2: Verify tests fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_phase13_deployment.py -q`

Expected: collection/import failure because `scripts.deploy_production` does not exist.

- [ ] **Step 3: Implement minimal deployment helpers and CLI**

Implement `sqlite_integrity_check`, `write_env_file`, `discover_preview_database`, `promote_database`, and `main`. Use SQLite backup into a temporary target, validate it, then use SQLite backup again to replace the production contents safely on Windows.

- [ ] **Step 4: Verify focused tests pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_phase13_deployment.py -q`

Expected: all Phase 13 deployment tests pass.

### Task 2: Enforce Production FastAPI Security

**Files:**
- Modify: `app/core/config.py`
- Modify: `app/main.py`
- Test: `tests/test_phase13_deployment.py`

- [ ] **Step 1: Add failing tests**

Assert production settings remove `/docs`, `/redoc`, and `/openapi.json`, reject wildcard/public CORS origins, and allow configured loopback/private-LAN origins.

- [ ] **Step 2: Verify expected failures**

Run the focused test file and confirm production route assertions fail.

- [ ] **Step 3: Add environment-aware configuration**

Add `environment`, `is_production`, and a private-LAN origin regex. Remove documentation routes from the inherited legacy app in production and configure restricted methods/headers.

- [ ] **Step 4: Verify focused tests pass**

Run the focused test file again.

### Task 3: Add Windows Startup Assets

**Files:**
- Create: `start_erp.bat`
- Create: `deployment_readme.txt`
- Test: `tests/test_phase13_deployment.py`

- [ ] **Step 1: Add failing content tests**

Assert the batch file loads `.env`, selects `.venv`, starts one Uvicorn worker on `0.0.0.0:8000`, and writes logs.

- [ ] **Step 2: Implement startup script**

Use project-relative paths, no `pause`, and one worker for SQLite.

- [ ] **Step 3: Write deployment guide**

Document deployment command, Task Scheduler hidden startup, NSSM service setup, firewall, log inspection, restart, rollback, and the mapped-drive versus UNC NAS warning.

- [ ] **Step 4: Verify focused tests pass**

Run the Phase 13 deployment tests.

### Task 4: Model Historical Requisition Defaults

**Files:**
- Modify: `app/models/product.py`
- Create: `app/models/historical_requisition.py`
- Modify: `app/models/__init__.py`
- Create: `alembic/versions/e13a6c4d2f40_phase13_historical_requisition_map.py`
- Test: `tests/test_phase13_historical_import.py`

- [ ] **Step 1: Write failing model and migration tests**

Assert Product exposes four nullable default requisition fields, the fallback table has searchable keys, and the migration contains no destructive operation.

- [ ] **Step 2: Verify failures**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_phase13_historical_import.py -q`

- [ ] **Step 3: Implement additive models and migration**

Add only nullable columns, indexes, and the new fallback table. Do not alter historical order tables.

- [ ] **Step 4: Verify focused tests pass**

Run the historical import test file.

### Task 5: Build Workbook Repair, Extraction, Matching, And Preview

**Files:**
- Create: `scripts/import_historical_requisitions.py`
- Modify: `requirements.txt`
- Test: `tests/test_phase13_historical_import.py`

- [ ] **Step 1: Write failing parser tests**

Cover split and combined cardboard dimensions, score-line detection, material-code detection, search-key extraction, latest-record deduplication, and invalid-style XML repair.

- [ ] **Step 2: Verify tests fail**

Run the focused test file and confirm missing parser functions cause failures.

- [ ] **Step 3: Implement parser and CLI**

Use pandas after creating a temporary repaired workbook. Default to `--preview-limit 20`; require `--commit` for product updates and fallback inserts. Produce `import_requisition_report.log`.

- [ ] **Step 4: Preview the supplied workbook**

Run:

```powershell
.\.venv\Scripts\python.exe scripts\import_historical_requisitions.py `
  "D:\360MoveData\Users\Administrator\Desktop\天明原文件\2025年采购单.xlsx" `
  --preview-limit 20
```

Expected: twenty cleaned mappings printed and zero database writes.

### Task 6: Verify And Finalize Production

**Files:**
- Modify: `.env`

- [ ] **Step 1: Run all tests**

Run: `.\.venv\Scripts\python.exe -m pytest -q`

Expected: existing 117 tests plus Phase 13 tests pass.

- [ ] **Step 2: Inspect migration**

Search the new revision for `drop_table`, destructive SQL, and modifications to `sales_order_items`.

- [ ] **Step 3: Run production deployment**

Run:

```powershell
.\.venv\Scripts\python.exe scripts\deploy_production.py
```

Because the validated database is already the production-path file, expected behavior is a final NAS backup, integrity validation, and `.env` update without replacing it from an older file.

- [ ] **Step 4: Smoke-test production startup**

Start the app temporarily on port 8000, assert `/` works and `/docs`, `/redoc`, `/openapi.json` return 404, then stop the temporary process.
