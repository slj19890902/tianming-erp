# WMS Incoming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a mobile WMS receiving workflow for pending carton-board order items.

**Architecture:** Extend Phase 5 order items with receiver attribution, expose a focused FastAPI router using conditional SQL updates, and add a standalone mobile HTML client. Preserve legacy order tables and use the existing cookie authentication and audit model.

**Tech Stack:** FastAPI, SQLAlchemy 2.x, SQLite, Alembic, native HTML/JS, Tailwind CDN, qrcode.

---

### Task 1: API behavior tests

**Files:**
- Create: `tests/test_phase6_incoming.py`

- [ ] Add fixtures containing pending, recently received, stale received and delivered order items.
- [ ] Test all four roles can read while only admin/workshop can mutate.
- [ ] Test receive changes exactly one pending row, records user and audit log, and rejects a repeated receive.
- [ ] Test revert requires a reason, clears receiving fields and rejects delivered orders.
- [ ] Run `python -m pytest -q tests/test_phase6_incoming.py` and confirm failures are caused by missing Phase 6 code.

### Task 2: Schema and migration

**Files:**
- Modify: `app/models/order.py`
- Create: `alembic/versions/<revision>_phase6_wms_receiver.py`
- Modify: `tests/test_phase6_incoming.py`

- [ ] Add nullable `material_received_by` with `users.id` foreign key and index.
- [ ] Create a non-destructive Alembic upgrade that only adds the field/index.
- [ ] Test migration preserves the legacy `orders` table and its rows.

### Task 3: Incoming API

**Files:**
- Create: `app/api/incoming.py`
- Modify: `app/main.py`

- [ ] Implement pending and last-24-hours received list queries.
- [ ] Implement conditional receive update and audit insertion in one transaction.
- [ ] Implement reason-required revert with delivery-state guard and audit insertion.
- [ ] Mount the router at `/api/incoming`.
- [ ] Run focused tests until green.

### Task 4: Mobile page

**Files:**
- Create: `static/incoming.html`
- Modify: `main.py`

- [ ] Add the required locked mobile viewport and large card controls.
- [ ] Implement auth check, two tabs, detail modal, receive action and reason-based revert.
- [ ] Serve the page at `/incoming.html`.
- [ ] Verify loading, empty/error states and touch-sized controls in a mobile viewport.

### Task 5: QR generator

**Files:**
- Create: `scripts/generate_wms_qr.py`
- Create or modify: `tests/test_phase6_qr.py`

- [ ] Implement explicit `--ip`, `--port` and `--output` options plus local IPv4 discovery.
- [ ] Generate `http://<IP>:<port>/incoming.html` with qrcode.
- [ ] Test URL construction and PNG generation.

### Task 6: Preview database and regression verification

**Files:**
- Modify: preview SQLite schema via Alembic only.

- [ ] Back up the preview database locally and to NAS.
- [ ] Run `alembic upgrade head` against the explicit preview path.
- [ ] Run `PRAGMA integrity_check`.
- [ ] Run `python -m pytest -q`.
- [ ] Start Uvicorn and verify `/incoming.html` in the in-app browser using a mobile viewport.
