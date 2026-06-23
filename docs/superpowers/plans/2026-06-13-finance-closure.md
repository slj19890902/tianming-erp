# Finance Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add return receipt, monthly statement and real database KPI workflows.

**Architecture:** New finance tables reference Phase 7 deliveries while remaining isolated from legacy finance tables. Return receipt and statement creation are all-or-nothing transactions with unique constraints preventing duplicates. Dashboard metrics use SQL aggregates.

**Tech Stack:** FastAPI, SQLAlchemy 2.x, SQLite, Alembic, Decimal.

---

### Task 1: Failing tests
- Create `tests/test_phase8_finance.py`.
- Cover short receipt reason, duplicate receipt, permissions, pending statements, statement amount snapshots and duplicate prevention.
- Create `tests/test_phase8_dashboard.py`.
- Cover monthly revenue, receivables, profit and today's task counts.

### Task 2: Models and migration
- Create `app/models/finance.py`.
- Modify `app/models/__init__.py`.
- Create a non-destructive Phase 8 Alembic revision.
- Verify legacy finance rows are unchanged.

### Task 3: Finance API
- Create `app/api/finance.py`.
- Implement return receipt creation, pending statement lines and statement creation.
- Use Decimal and `ROUND_HALF_UP`.
- Write operation logs in the same transaction.

### Task 4: Dashboard API
- Create `app/api/dashboard.py`.
- Implement SQL aggregate KPI endpoint.
- Register finance and dashboard routers in `app/main.py`.

### Task 5: Preview verification
- Back up preview database locally and to NAS.
- Upgrade explicit preview path.
- Run integrity check and all tests.
- Exercise the 80 delivered / 78 received API flow.
