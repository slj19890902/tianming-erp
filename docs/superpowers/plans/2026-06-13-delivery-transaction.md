# Delivery Transaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build draft delivery creation, atomic dispatch, force-close handling and a money-free standard delivery print view.

**Architecture:** New delivery tables remain isolated from legacy delivery tables. Draft creation validates customer and quantities; dispatch performs conditional item updates and status changes in one transaction. Printing uses a dedicated field-whitelisted endpoint and standalone HTML.

**Tech Stack:** FastAPI, SQLAlchemy 2.x, SQLite, Alembic, native HTML/JS.

---

### Task 1: Failing delivery tests

**Files:**
- Create: `tests/test_phase7_deliveries.py`

- [ ] Test strict pending-item filtering and remaining quantity.
- [ ] Test one delivery combines multiple orders for one customer.
- [ ] Test partial dispatch increments quantities once and rejects repeat dispatch.
- [ ] Test an invalid line rolls back every quantity and delivery status.
- [ ] Test force close removes an item from pending and writes an audit log.
- [ ] Test workshop is read-only.
- [ ] Test print JSON recursively contains no financial field names.

### Task 2: Models and Alembic

**Files:**
- Create: `app/models/delivery.py`
- Modify: `app/models/order.py`
- Modify: `app/models/__init__.py`
- Create: `alembic/versions/<revision>_phase7_deliveries.py`

- [ ] Add `delivered_quantity` and `is_force_closed` to order items.
- [ ] Add delivery sequence, master and item models with constraints.
- [ ] Add a non-destructive migration and verify legacy delivery rows remain unchanged.

### Task 3: Delivery API

**Files:**
- Create: `app/api/deliveries.py`
- Modify: `app/main.py`

- [ ] Implement pending-item list.
- [ ] Implement draft creation with daily atomic numbering.
- [ ] Implement conditional, all-or-nothing dispatch.
- [ ] Implement force close with required reason and audit logging.
- [ ] Implement delivery detail and print-safe response.
- [ ] Register routes and run focused tests.

### Task 4: Print page

**Files:**
- Create: `static/delivery-print.html`
- Modify: `main.py`

- [ ] Implement fixed Suzhou Tianming title and factory address.
- [ ] Render only approved non-financial fields.
- [ ] Add triplicate note, signature lines and print-only CSS.
- [ ] Serve `/delivery-print.html`.

### Task 5: Preview migration and verification

- [ ] Back up the preview database locally and to NAS.
- [ ] Upgrade the explicit preview path with Alembic.
- [ ] Run integrity check and all tests.
- [ ] Browser-test print page identity, empty/error handling, populated print layout and print CSS.
