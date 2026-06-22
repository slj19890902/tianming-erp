# Phase 8 Closeout and Phase 9 Disaster Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add traceable invoicing and settlement records, accurate outstanding balances, and an admin-only NAS backup/restore workflow with a mandatory pre-restore snapshot.

**Architecture:** Finance operations append immutable invoice/payment records and update statement summary fields in the same SQLAlchemy transaction. Backup and restore safety lives in `app/core/database.py`; the API validates authorization and filenames, invokes the core operation, and records an audit event after restore.

**Tech Stack:** FastAPI, SQLAlchemy 2.x, SQLite backup API, Alembic, pytest.

---

### Task 1: Finance closeout

**Files:**
- Modify: `app/models/finance.py`
- Modify: `app/models/__init__.py`
- Modify: `app/api/finance.py`
- Modify: `app/api/dashboard.py`
- Test: `tests/test_phase8_finance.py`

- [ ] Add failing tests for cumulative invoicing, partial settlement, exact automatic settlement, overpayment rejection, role isolation, and audit records.
- [ ] Add `invoiced_amount` and `settled_amount` to `Statement`.
- [ ] Add `Invoice` and `SettlementRecord` tables so every operation remains traceable.
- [ ] Implement `/api/finance/invoices` and `/api/finance/statements/{id}/settle` as single transactions.
- [ ] Calculate dashboard outstanding receivables as `total_receivable - settled_amount`.
- [ ] Run `python -m pytest tests/test_phase8_finance.py tests/test_phase8_dashboard.py -q`.

### Task 2: Backup and restore core

**Files:**
- Modify: `app/core/database.py`
- Test: `tests/test_phase9_system.py`

- [ ] Add a failing test for named backup suffixes and verified restore.
- [ ] Extend `backup_to_nas()` with a sanitized suffix.
- [ ] Implement `restore_from_backup()` with source integrity check, mandatory `_pre_restore` backup, SQLite online restore, target integrity check, and automatic rollback from the emergency backup if verification fails.
- [ ] Run `python -m pytest tests/test_phase9_system.py -q`.

### Task 3: Admin system API and deployment hardening

**Files:**
- Create: `app/api/system.py`
- Modify: `app/core/config.py`
- Modify: `app/main.py`
- Test: `tests/test_phase9_system.py`
- Test: `tests/test_phase1_foundation.py`
- Test: `tests/test_phase2_auth.py`

- [ ] Add failing tests for admin-only access, backup listing, path traversal rejection, and LAN-only CORS origins.
- [ ] Implement backup list/create/restore endpoints.
- [ ] After restore, open a fresh database session and append `RESTORE_DATABASE` to `operation_logs`.
- [ ] Reject wildcard, public-host, credential-bearing, and non-HTTP CORS origins.
- [ ] Explicitly disable debug mode and restrict CORS methods/headers.
- [ ] Run the targeted system, config, and auth tests.

### Task 4: Non-destructive schema migration and preview verification

**Files:**
- Create: `alembic/versions/<revision>_phase9_finance_backup_closeout.py`

- [ ] Add only the two statement columns and the two new finance record tables.
- [ ] Make upgrade idempotent by inspecting existing tables and columns.
- [ ] Keep downgrade non-destructive.
- [ ] Confirm the migration contains no `drop_table` or legacy-table mutation.
- [ ] Run the full test suite.
- [ ] Back up the preview database before migration, run `alembic upgrade head`, verify the revision, row counts, and `PRAGMA integrity_check`.
- [ ] Trigger one real manual NAS backup and verify its integrity, hash, and size.
