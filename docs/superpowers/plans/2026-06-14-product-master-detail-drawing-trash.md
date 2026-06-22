# Product Master-Detail, Drawing Versions, and Trash Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace destructive product deletion with a recoverable 30-day trash workflow, add versioned product drawings, preserve all historical foreign-key relationships, and prepare the backend contract for the approved customer-first master-detail UI.

**Architecture:** Extend `products` with lifecycle timestamps and add a normalized `product_drawings` child table. Product deletion becomes a soft transition into trash; permanent cleanup physically deletes only unreferenced products and archives referenced products. Drawing migration runs in two revisions: create/copy first, remove the deprecated column only after verification.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x, Alembic, SQLite, Pydantic, Pillow, pytest, FastAPI TestClient.

---

## Execution Safety Amendment

The legacy-column removal revision is intentionally deferred and is not placed
in the current Alembic revision chain. Adding it now would make
`alembic upgrade head` remove `products.drawing_path` while the compatibility
frontend and ORM model still use that field.

The current release therefore implements a true compatibility window:

1. Revision `f26b7d4a9c10` creates `product_drawings`, copies every legacy path,
   and retains `products.drawing_path`.
2. New uploads write both a `product_drawings` version and the latest legacy
   `drawing_path`.
3. The removal revision will be generated only after the frontend and ORM stop
   reading or writing the legacy field.

## File Map

**Create**

- `app/models/product_drawing.py`: normalized drawing version model.
- `app/services/product_lifecycle.py`: trash, restore, purge, and historical-reference checks.
- `app/services/product_drawings.py`: image compression, file naming, version creation, and safe file cleanup.
- `alembic/versions/f26b7d4a9c10_product_trash_and_drawing_versions.py`: additive product lifecycle fields and drawing table; copies old paths.
- `tests/test_phase14_product_lifecycle.py`: API and database behavior tests.
- `tests/test_phase14_product_drawings.py`: drawing upload, ordering, deletion, and file behavior.
- `tests/test_phase14_migration.py`: migration safety and data-preservation tests.
- `tests/test_phase14_frontend.py`: master-detail and independent-field regression assertions.

**Modify**

- `app/models/__init__.py`: register `ProductDrawing`.
- `app/models/product.py`: lifecycle columns and `drawings` relationship.
- `app/api/products.py`: filtering, detail response, trash APIs, drawing APIs, compatibility endpoint.
- `static/index.html`: approved customer-first master-detail UI, independent product editor, drawing gallery, trash drawer.
- `tests/test_phase3_api.py`: replace old destructive-delete expectations with trash semantics.
- `tests/test_phase12_uat.py`: update old single-drawing expectation to drawing-version response.

## Task 1: Lock the Current Regression with Failing Lifecycle Tests

**Files:**

- Create: `tests/test_phase14_product_lifecycle.py`
- Modify: `tests/test_phase3_api.py`

- [ ] **Step 1: Add a fixture with one unreferenced and one historically referenced product**

Use the existing FastAPI/TestClient fixture pattern and create:

```python
@pytest.fixture()
def product_lifecycle_app(tmp_path: Path):
    engine = create_sqlite_engine(tmp_path / "phase14.sqlite3")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    # Seed admin/sales/workshop, one customer, one material,
    # product 1 referenced by sales_order_items and product 2 unreferenced.
```

Expose `session_factory` through `app.state.session_factory`.

- [ ] **Step 2: Write the failing test for referenced product deletion**

```python
def test_delete_referenced_product_moves_it_to_trash_without_breaking_order(
    product_lifecycle_app,
) -> None:
    with TestClient(product_lifecycle_app) as client:
        _login(client, "admin")
        response = client.delete("/api/master/products/1")
        active = client.get("/api/master/products")
        detail = client.get("/api/master/products/1")

    assert response.status_code == 200
    assert response.json()["deleted_at"] is not None
    assert active.json()["total"] == 1
    assert detail.status_code == 200
    with product_lifecycle_app.state.session_factory() as session:
        assert session.query(OrderItem).filter(OrderItem.product_id == 1).count() == 1
```

- [ ] **Step 3: Write failing tests for restore and default filtering**

```python
def test_restore_product_returns_it_to_active_search(product_lifecycle_app) -> None:
    with TestClient(product_lifecycle_app) as client:
        _login(client, "admin")
        client.delete("/api/master/products/2")
        hidden = client.get("/api/master/products")
        restored = client.put("/api/master/products/2/restore")
        visible = client.get("/api/master/products")

    assert hidden.json()["total"] == 1
    assert restored.status_code == 200
    assert restored.json()["deleted_at"] is None
    assert visible.json()["total"] == 2
```

- [ ] **Step 4: Write failing tests for purge behavior**

```python
def test_purge_unreferenced_product_physically_deletes_it(product_lifecycle_app):
    ...
    assert client.delete("/api/master/products/2/purge").status_code == 204
    assert session.get(Product, 2) is None


def test_purge_referenced_product_archives_without_physical_delete(
    product_lifecycle_app,
):
    ...
    assert response.status_code == 200
    assert response.json()["purged_at"] is not None
    assert session.get(Product, 1) is not None
    assert session.get(Product, 1).deleted_at is not None
```

- [ ] **Step 5: Write failing tests for permissions and audit logs**

Verify:

- `sales` cannot purge or empty trash.
- `workshop` cannot delete or restore.
- delete, restore, purge, and empty-trash create `OperationLog` rows.

- [ ] **Step 6: Run lifecycle tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_phase14_product_lifecycle.py -q
```

Expected: failures because `deleted_at`, trash endpoints, and lifecycle service do not exist.

## Task 2: Implement Product Lifecycle Model and Service

**Files:**

- Modify: `app/models/product.py`
- Create: `app/services/product_lifecycle.py`
- Modify: `app/api/products.py`

- [ ] **Step 1: Add lifecycle columns to the model**

```python
deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
deleted_by: Mapped[int | None] = mapped_column(
    ForeignKey("users.id", ondelete="SET NULL"),
    nullable=True,
)
purged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
```

Add indexes for `deleted_at` and `purged_at`.

- [ ] **Step 2: Implement lifecycle helpers**

```python
def has_historical_references(db: Session, product_id: int) -> bool:
    checks = (
        select(func.count()).select_from(OrderItem).where(OrderItem.product_id == product_id),
        select(func.count()).select_from(MigrationEntityMap).where(
            MigrationEntityMap.new_entity_id == product_id,
            MigrationEntityMap.entity_type == "product",
        ),
    )
    return any((db.scalar(query) or 0) > 0 for query in checks)


def move_product_to_trash(product: Product, user: User) -> None:
    now = datetime.now()
    product.deleted_at = now
    product.deleted_by = user.id
    product.is_active = False
    product.purged_at = None


def restore_product(product: Product) -> None:
    product.deleted_at = None
    product.deleted_by = None
    product.purged_at = None
    product.is_active = True
```

- [ ] **Step 3: Change default product filtering**

The list query must always apply:

```python
query = query.where(Product.deleted_at.is_(None))
```

`include_inactive` controls only `is_active`; it must never expose trash records.

- [ ] **Step 4: Replace destructive DELETE with soft-delete**

```python
@router.delete("/{product_id}")
def delete_product(...):
    product = _product_or_404(db, product_id)
    move_product_to_trash(product, user)
    audit_master_change(..., action="MOVE_TO_TRASH", ...)
    db.commit()
    db.refresh(product)
    return _response(product, user)
```

- [ ] **Step 5: Add trash, restore, purge, and empty endpoints**

Register static routes before `/{product_id}`:

```python
@router.get("/trash")
def list_product_trash(...): ...


@router.post("/trash/empty")
def empty_product_trash(...): ...


@router.put("/{product_id}/restore")
def restore_product_from_trash(...): ...


@router.delete("/{product_id}/purge")
def purge_product(...): ...
```

Only records with `deleted_at IS NOT NULL` are valid restore/purge targets.

- [ ] **Step 6: Run lifecycle tests and verify GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_phase14_product_lifecycle.py tests\test_phase3_api.py -q
```

Expected: all lifecycle and adjusted legacy product API tests pass.

## Task 3: Add Failing Drawing-Version Tests

**Files:**

- Create: `tests/test_phase14_product_drawings.py`
- Modify: `tests/test_phase12_uat.py`

- [ ] **Step 1: Test upload creates a new version instead of overwriting**

```python
def test_each_upload_creates_a_new_drawing_version(drawing_app):
    first = _upload(client, "v1.png")
    second = _upload(client, "v2.png")
    detail = client.get("/api/master/products/1")

    assert first.status_code == 201
    assert second.status_code == 201
    assert len(detail.json()["drawings"]) == 2
    assert detail.json()["drawings"][0]["id"] == second.json()["id"]
```

- [ ] **Step 2: Test files are compressed and paths are relative URLs**

Assert each record has:

```python
assert drawing["image_path"].startswith("/static/uploads/drawings/")
assert drawing["thumbnail_path"].startswith("/static/uploads/drawings/")
assert "base64" not in str(drawing).lower()
```

- [ ] **Step 3: Test deleting one drawing does not affect other versions**

```python
def test_delete_drawing_removes_only_selected_version_and_files(drawing_app):
    ...
    response = client.delete(f"/api/master/products/drawings/{first_id}")
    assert response.status_code == 204
    assert len(client.get("/api/master/products/1").json()["drawings"]) == 1
    assert not first_high.exists()
    assert not first_thumb.exists()
```

- [ ] **Step 4: Test role restrictions**

Verify `sales` may upload, `admin` may delete, and `workshop`/`finance` cannot mutate drawings.

- [ ] **Step 5: Run drawing tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_phase14_product_drawings.py -q
```

Expected: failures because `ProductDrawing` and versioned endpoints do not exist.

## Task 4: Implement Drawing Versions

**Files:**

- Create: `app/models/product_drawing.py`
- Create: `app/services/product_drawings.py`
- Modify: `app/models/product.py`
- Modify: `app/models/__init__.py`
- Modify: `app/api/products.py`

- [ ] **Step 1: Create the drawing model**

```python
class ProductDrawing(Base):
    __tablename__ = "product_drawings"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    image_path: Mapped[str] = mapped_column(Text, nullable=False)
    thumbnail_path: Mapped[str] = mapped_column(Text, nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        nullable=False,
    )
    uploaded_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    product: Mapped["Product"] = relationship(back_populates="drawings")
```

- [ ] **Step 2: Add ordered relationship**

```python
drawings: Mapped[list["ProductDrawing"]] = relationship(
    back_populates="product",
    order_by="desc(ProductDrawing.uploaded_at), desc(ProductDrawing.id)",
)
```

- [ ] **Step 3: Extract file processing service**

Move Pillow validation/compression from `app/api/products.py` into:

```python
def save_product_drawing_files(product_id: int, content: bytes) -> SavedDrawing:
    ...


def remove_drawing_files(image_path: str, thumbnail_path: str) -> list[str]:
    ...
```

Resolve stored URLs to files only beneath the configured drawing directory; reject path traversal.

- [ ] **Step 4: Return drawings from product response**

Add:

```python
class ProductDrawingResponse(BaseModel):
    id: int
    image_path: str
    thumbnail_path: str
    uploaded_at: datetime


class ProductResponse(ProductPayload):
    drawings: list[ProductDrawingResponse] = []
```

- [ ] **Step 5: Add new upload and delete endpoints**

```python
@router.post("/{product_id}/drawings", status_code=201)
async def upload_product_drawing_version(...): ...


@router.delete("/drawings/{drawing_id}", status_code=204)
def delete_product_drawing(...): ...
```

Keep `POST /{product_id}/drawing` temporarily and route it through the same service.

- [ ] **Step 6: Run drawing tests and verify GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_phase14_product_drawings.py tests\test_phase12_uat.py -q
```

Expected: version order, file cleanup, compatibility upload, and permissions pass.

## Task 5: Write Migration Tests Before Alembic Revisions

**Files:**

- Create: `tests/test_phase14_migration.py`

- [ ] **Step 1: Build a pre-migration SQLite database**

Create a temporary database at revision `e13a6c4d2f40`, insert:

- two products,
- one non-null `drawing_path`,
- one order item referencing product 1.

- [ ] **Step 2: Test additive migration safety**

```python
def test_phase14_additive_migration_preserves_products_orders_and_old_path(...):
    _upgrade(db_path, "f26b7d4a9c10")
    assert scalar("SELECT COUNT(*) FROM products") == 2
    assert scalar("SELECT COUNT(*) FROM sales_order_items") == 1
    assert scalar("SELECT COUNT(*) FROM product_drawings") == 1
    assert scalar("SELECT drawing_path FROM products WHERE id = 1") == old_path
```

- [ ] **Step 3: Assert the first revision contains no destructive operations**

```python
source = MIGRATION.read_text(encoding="utf-8")
assert "drop_table" not in source
assert 'drop_column("products", "drawing_path")' not in source
assert "DELETE FROM sales_order_items" not in source
```

- [ ] **Step 4: Test second revision removes only the legacy column**

Upgrade a copied database to `g37c8e5b0d21`, then assert:

- `products.drawing_path` no longer exists,
- `product_drawings` count remains one,
- products and order items counts remain unchanged.

- [ ] **Step 5: Run migration tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_phase14_migration.py -q
```

Expected: failures because both revisions do not exist.

## Task 6: Implement the First Track and Compatibility Dual-Write

**Files:**

- Create: `alembic/versions/f26b7d4a9c10_product_trash_and_drawing_versions.py`

- [ ] **Step 1: Add lifecycle fields and drawing table**

Revision `f26b7d4a9c10`, down revision `e13a6c4d2f40`:

```python
op.add_column("products", sa.Column("deleted_at", sa.DateTime(), nullable=True))
op.add_column("products", sa.Column("deleted_by", sa.Integer(), nullable=True))
op.add_column("products", sa.Column("purged_at", sa.DateTime(), nullable=True))
op.create_table("product_drawings", ...)
```

Use inspector guards so reruns cannot duplicate columns or tables.

- [ ] **Step 2: Copy legacy drawing paths**

```sql
INSERT INTO product_drawings (
    product_id, image_path, thumbnail_path, uploaded_at
)
SELECT
    id,
    drawing_path,
    drawing_path,
    COALESCE(updated_at, created_at, CURRENT_TIMESTAMP)
FROM products
WHERE drawing_path IS NOT NULL
  AND TRIM(drawing_path) <> ''
  AND NOT EXISTS (
      SELECT 1
      FROM product_drawings d
      WHERE d.product_id = products.id
        AND d.image_path = products.drawing_path
  )
```

- [ ] **Step 3: Keep compatibility dual-write active**

Every new drawing upload creates a `product_drawings` record and updates
`products.drawing_path` to the latest high-resolution path. The later removal
revision is prohibited until the ORM model and frontend no longer reference
the old field.

- [ ] **Step 4: Run migration tests and verify GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_phase14_migration.py -q
```

- [ ] **Step 5: Generate offline SQL for review**

```powershell
$env:ERP_DATABASE_PATH = "migration-workfiles\phase14_review.sqlite3"
.\.venv\Scripts\python.exe -m alembic upgrade f26b7d4a9c10 --sql |
  Set-Content migration-reports\phase14_additive.sql -Encoding utf8
```

Review must confirm no `DROP TABLE sales_order_items`, `DROP TABLE products`, or historical-data deletion.

## Task 7: Run Backup, Preview Migration, and Integrity Verification

**Files:**

- No production code change.
- Output: `migration-reports/phase14_backend_verification.txt`

- [ ] **Step 1: Trigger NAS backup through the running API**

Authenticate as admin and call:

```powershell
Invoke-RestMethod http://127.0.0.1:8001/api/system/backups `
  -Method Post -WebSession $session
```

Verify the returned filename exists under:

```text
Z:\sata1-18015598002\BoxERP\backups
```

- [ ] **Step 2: Copy the current database to a Phase 14 preview database**

Use SQLite backup API, not raw file copy while the application may be writing:

```python
with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
    src.backup(dst)
```

- [ ] **Step 3: Apply only the additive revision first**

```powershell
$env:ERP_DATABASE_PATH = "migration-workfiles\phase14_preview.sqlite3"
.\.venv\Scripts\python.exe -m alembic upgrade f26b7d4a9c10
```

- [ ] **Step 4: Verify counts and integrity**

Record:

```sql
PRAGMA integrity_check;
SELECT COUNT(*) FROM products;
SELECT COUNT(*) FROM sales_order_items;
SELECT COUNT(*) FROM product_drawings;
SELECT COUNT(*) FROM products WHERE drawing_path IS NOT NULL AND TRIM(drawing_path) <> '';
```

The product and order-item counts must match pre-migration counts. Drawing count must be at least the old non-empty path count.

- [ ] **Step 5: Run the full test suite**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: existing suite plus Phase 14 tests pass with no unhandled 500 response.

- [ ] **Step 6: Delay the second revision**

Do not create or apply a legacy-column removal revision in this release. Keep
the old column for one compatibility window and generate the removal revision
only after browser/UAT verification confirms all clients use `drawings[]`.

## Task 8: Add Frontend Regression Tests Before UI Changes

**Files:**

- Create: `tests/test_phase14_frontend.py`

- [ ] **Step 1: Assert master-detail state and customer-first entry**

```python
assert "selectedProductCustomer" in INDEX
assert "productCustomerSearch" in INDEX
assert "返回客户列表" in INDEX
assert "customer-product-master-list" in INDEX
```

- [ ] **Step 2: Assert field decoupling**

```python
assert 'split(" / "' not in INDEX
assert "cleanProductName" not in INDEX
assert 'v-model="productForm.customer_material_code"' in INDEX
assert 'v-model="productForm.product_name"' in INDEX
```

- [ ] **Step 3: Assert drawing gallery and trash controls**

```python
assert "drawings" in INDEX
assert "当前生效版" in INDEX
assert "历史版本" in INDEX
assert "产品垃圾站" in INDEX
assert "restoreProduct" in INDEX
assert "emptyProductTrash" in INDEX
```

- [ ] **Step 4: Run frontend test and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_phase14_frontend.py -q
```

Expected: failures because the approved UI has not yet been implemented.

## Task 9: Implement Approved Master-Detail UI

**Files:**

- Modify: `static/index.html`

- [ ] **Step 1: Remove slash-based product-name transformation**

Delete `cleanProductName` and all code that derives `product_name` from a combined `code / name` string.

- [ ] **Step 2: Add customer master-list state**

```javascript
productCustomerSearch: "",
selectedProductCustomer: null,
productTrashOpen: false,
productTrashItems: [],
```

Render customers first; fetch products only after selecting a customer.

- [ ] **Step 3: Render the customer-specific product table**

Columns:

- 存货编码
- 规格
- 材质
- 楞型
- 默认单价
- 图纸版本
- 状态
- 操作

Do not render customer name or product-name columns in this table.

- [ ] **Step 4: Keep edit fields independent**

Bind each input directly to its matching API field. No input watcher may modify another field.

- [ ] **Step 5: Add drawing gallery**

Display newest drawing first with `当前生效版`; older records display `历史版本`. Delete calls `/api/master/products/drawings/{id}` and refreshes the product detail.

- [ ] **Step 6: Add trash drawer**

Load `/api/master/products/trash`; support restore, individual purge, and admin-only empty.

- [ ] **Step 7: Run frontend and full tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_phase14_frontend.py -q
.\.venv\Scripts\python.exe -m pytest -q
```

## Task 10: Browser UAT for Tianhua 001A

**Files:**

- No code changes unless a failing browser scenario produces a new RED test.

- [ ] **Step 1: Open the customer master list and search for 天华**

Verify the high-density list shows 天华 and 天华超净 as separate customers.

- [ ] **Step 2: Open 天华 and edit `001A外箱`**

Perform:

1. Clear 存货编码 and type it again.
2. Clear 产品名称 and type a different value.
3. Enter `/` in either field.
4. Change dimensions and material independently.

The editor must remain open and unrelated fields must not change.

- [ ] **Step 3: Exercise lifecycle**

1. Disable and re-enable `001A外箱`.
2. Move it to trash.
3. Confirm it disappears from active search and remains available in historical order detail.
4. Restore it.

- [ ] **Step 4: Exercise drawing versions**

Upload two drawings, verify current/history order, delete the older version, and confirm the current version remains.

- [ ] **Step 5: Record final database checks**

```sql
PRAGMA integrity_check;
SELECT COUNT(*) FROM products;
SELECT COUNT(*) FROM sales_order_items;
SELECT COUNT(*) FROM product_drawings;
```

Save the final results to `migration-reports/phase14_backend_verification.txt` using `utf-8-sig`.

## Execution Notes

- Current shell does not expose a `git` executable. Test and database checkpoints are mandatory; no commit success may be claimed until Git becomes available.
- The first production-compatible release stops at revision `f26b7d4a9c10`.
- Revision `g37c8e5b0d21` is intentionally delayed until the compatibility window closes.
- Any unexpected migration count change blocks deployment and requires restoring the preview copy, not altering the source database.
