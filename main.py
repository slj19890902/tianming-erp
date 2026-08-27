from __future__ import annotations

import os
import hashlib
import re
import socket
import sqlite3
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.web_assets import conditional_file_response


APP_NAME = "三级纸箱厂极简ERP"
DB_FILENAME = "carton_erp.sqlite3"
SERVER_PORT = 8000


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_base_dir() -> Path:
    """Return a writable base dir both in source mode and PyInstaller exe mode."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundled_resource_path(relative_path: str) -> Path:
    """Resolve read-only bundled resources when packaged by PyInstaller."""
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / relative_path  # type: ignore[attr-defined]
    return app_base_dir() / relative_path


def static_dir() -> Path:
    return bundled_resource_path("static")


def index_html_path() -> Path:
    return static_dir() / "index.html"


def data_dir() -> Path:
    path = app_base_dir() / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / DB_FILENAME


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('boss', 'workshop')),
    display_name TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS operation_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT,
    role TEXT,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    description TEXT,
    ip_address TEXT,
    user_agent TEXT,
    extra_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    contact_person TEXT,
    phone TEXT,
    address TEXT,
    billing_note TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    contact_person TEXT,
    phone TEXT,
    address TEXT,
    payment_note TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS product_archives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL,
    style_no TEXT NOT NULL,
    customer_po TEXT,
    product_name TEXT,
    unit TEXT NOT NULL DEFAULT '只',
    length_mm REAL,
    width_mm REAL,
    height_mm REAL,
    material TEXT,
    flute_type TEXT,
    layer_count INTEGER,
    color_count INTEGER,
    process_note TEXT,
    last_sale_unit_price REAL,
    sale_unit_price_no_tax REAL,
    last_cost_unit_price REAL,
    last_supplier_id INTEGER,
    drawing_path TEXT,
    die_cut_path TEXT,
    remark TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT,
    UNIQUE (customer_id, style_no),
    FOREIGN KEY (customer_id) REFERENCES customers(id),
    FOREIGN KEY (last_supplier_id) REFERENCES suppliers(id)
);

CREATE TABLE IF NOT EXISTS quotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_no TEXT NOT NULL UNIQUE,
    customer_id INTEGER NOT NULL,
    product_archive_id INTEGER,
    customer_po TEXT,
    style_no TEXT,
    product_name TEXT,
    unit TEXT NOT NULL DEFAULT '只',
    customer_requirement TEXT,
    length_mm REAL,
    width_mm REAL,
    height_mm REAL,
    material TEXT,
    flute_type TEXT,
    layer_count INTEGER,
    color_count INTEGER,
    process_note TEXT,
    quote_quantity INTEGER NOT NULL,
    sale_unit_price REAL NOT NULL,
    sale_unit_price_no_tax REAL,
    cost_unit_price REAL,
    quote_amount REAL NOT NULL,
    quote_pdf_path TEXT,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'confirmed', 'converted', 'cancelled')),
    created_by INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT,
    FOREIGN KEY (customer_id) REFERENCES customers(id),
    FOREIGN KEY (product_archive_id) REFERENCES product_archives(id),
    FOREIGN KEY (created_by) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('quotation', 'order', 'delivery', 'finance')),
    entity_id INTEGER NOT NULL,
    file_kind TEXT NOT NULL CHECK (
        file_kind IN (
            'drawing',
            'die_cut',
            'quote_pdf',
            'material_pdf',
            'delivery_pdf',
            'receipt_image',
            'other'
        )
    ),
    original_name TEXT,
    file_path TEXT NOT NULL,
    mime_type TEXT,
    uploaded_by INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (uploaded_by) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT NOT NULL UNIQUE,
    quotation_id INTEGER,
    customer_id INTEGER NOT NULL,
    supplier_id INTEGER,
    product_archive_id INTEGER,
    customer_po TEXT,
    style_no TEXT,
    product_name TEXT,
    unit TEXT NOT NULL DEFAULT '只',
    length_mm REAL,
    width_mm REAL,
    height_mm REAL,
    material TEXT,
    flute_type TEXT,
    layer_count INTEGER,
    color_count INTEGER,
    process_note TEXT,
    order_quantity INTEGER NOT NULL,
    sale_unit_price REAL NOT NULL,
    sale_unit_price_no_tax REAL,
    cost_unit_price REAL NOT NULL,
    drawing_path TEXT,
    die_cut_path TEXT,
    is_return_remake INTEGER NOT NULL DEFAULT 0,
    remake_source_order_id INTEGER,
    amount_included INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending_material'
        CHECK (
            status IN (
                'pending_material',
                'material_arrived',
                'delivered',
                'receipt_confirmed',
                'cancelled'
            )
        ),
    warning_confirmed INTEGER NOT NULL DEFAULT 0,
    warning_reason TEXT,
    material_arrived_at TEXT,
    material_arrived_by INTEGER,
    created_by INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT,
    FOREIGN KEY (quotation_id) REFERENCES quotations(id),
    FOREIGN KEY (customer_id) REFERENCES customers(id),
    FOREIGN KEY (supplier_id) REFERENCES suppliers(id),
    FOREIGN KEY (product_archive_id) REFERENCES product_archives(id),
    FOREIGN KEY (remake_source_order_id) REFERENCES orders(id),
    FOREIGN KEY (material_arrived_by) REFERENCES users(id),
    FOREIGN KEY (created_by) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS material_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_no TEXT NOT NULL UNIQUE,
    supplier_id INTEGER NOT NULL,
    report_pdf_path TEXT,
    status TEXT NOT NULL DEFAULT 'reported'
        CHECK (status IN ('reported', 'cancelled')),
    created_by INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (supplier_id) REFERENCES suppliers(id),
    FOREIGN KEY (created_by) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS material_report_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_report_id INTEGER NOT NULL,
    order_id INTEGER NOT NULL,
    material TEXT,
    flute_type TEXT,
    quantity INTEGER NOT NULL,
    cost_unit_price REAL NOT NULL,
    payable_amount REAL NOT NULL,
    FOREIGN KEY (material_report_id) REFERENCES material_reports(id),
    FOREIGN KEY (order_id) REFERENCES orders(id)
);

CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_no TEXT NOT NULL UNIQUE,
    customer_id INTEGER NOT NULL,
    delivery_date TEXT NOT NULL,
    print_width_mm REAL NOT NULL DEFAULT 241,
    print_layout TEXT NOT NULL DEFAULT 'half_page',
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'printed', 'delivered', 'receipt_confirmed', 'cancelled')),
    receipt_confirmed_at TEXT,
    receipt_confirmed_by INTEGER,
    remark TEXT,
    created_by INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT,
    FOREIGN KEY (customer_id) REFERENCES customers(id),
    FOREIGN KEY (receipt_confirmed_by) REFERENCES users(id),
    FOREIGN KEY (created_by) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS delivery_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id INTEGER NOT NULL,
    order_id INTEGER NOT NULL,
    customer_po TEXT,
    style_no TEXT,
    product_name TEXT,
    unit TEXT NOT NULL DEFAULT '只',
    order_quantity INTEGER NOT NULL,
    actual_quantity INTEGER NOT NULL,
    sale_unit_price REAL NOT NULL,
    sale_unit_price_no_tax REAL,
    cost_unit_price REAL NOT NULL,
    line_amount REAL NOT NULL,
    line_amount_no_tax REAL,
    remark TEXT,
    FOREIGN KEY (delivery_id) REFERENCES deliveries(id),
    FOREIGN KEY (order_id) REFERENCES orders(id)
);

CREATE TABLE IF NOT EXISTS accounts_receivable (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ar_no TEXT NOT NULL UNIQUE,
    customer_id INTEGER NOT NULL,
    delivery_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    paid_amount REAL NOT NULL DEFAULT 0,
    balance_amount REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'unpaid'
        CHECK (status IN ('unpaid', 'partial_paid', 'paid', 'cancelled')),
    confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (customer_id) REFERENCES customers(id),
    FOREIGN KEY (delivery_id) REFERENCES deliveries(id)
);

CREATE TABLE IF NOT EXISTS receivable_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL,
    accounts_receivable_id INTEGER,
    payment_date TEXT NOT NULL,
    amount REAL NOT NULL,
    payment_method TEXT,
    note TEXT,
    created_by INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (customer_id) REFERENCES customers(id),
    FOREIGN KEY (accounts_receivable_id) REFERENCES accounts_receivable(id),
    FOREIGN KEY (created_by) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS accounts_payable (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ap_no TEXT NOT NULL UNIQUE,
    supplier_id INTEGER NOT NULL,
    material_report_id INTEGER,
    amount REAL NOT NULL,
    paid_amount REAL NOT NULL DEFAULT 0,
    balance_amount REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'unpaid'
        CHECK (status IN ('unpaid', 'partial_paid', 'paid', 'cancelled')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (supplier_id) REFERENCES suppliers(id),
    FOREIGN KEY (material_report_id) REFERENCES material_reports(id)
);

CREATE TABLE IF NOT EXISTS payable_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id INTEGER NOT NULL,
    accounts_payable_id INTEGER,
    payment_date TEXT NOT NULL,
    amount REAL NOT NULL,
    payment_method TEXT,
    note TEXT,
    created_by INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (supplier_id) REFERENCES suppliers(id),
    FOREIGN KEY (accounts_payable_id) REFERENCES accounts_payable(id),
    FOREIGN KEY (created_by) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS order_warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER,
    customer_id INTEGER NOT NULL,
    style_no TEXT,
    warning_type TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    confirmed_by INTEGER,
    confirmed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (order_id) REFERENCES orders(id),
    FOREIGN KEY (customer_id) REFERENCES customers(id),
    FOREIGN KEY (confirmed_by) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_style ON orders(style_no);
CREATE INDEX IF NOT EXISTS idx_orders_customer_po ON orders(customer_po);
CREATE INDEX IF NOT EXISTS idx_orders_material_arrived ON orders(material_arrived_at);
CREATE INDEX IF NOT EXISTS idx_product_archives_customer_style ON product_archives(customer_id, style_no);
CREATE INDEX IF NOT EXISTS idx_product_archives_customer_po ON product_archives(customer_po);
CREATE INDEX IF NOT EXISTS idx_deliveries_customer ON deliveries(customer_id);
CREATE INDEX IF NOT EXISTS idx_delivery_items_order ON delivery_items(order_id);
CREATE INDEX IF NOT EXISTS idx_delivery_items_customer_po ON delivery_items(customer_po);
CREATE INDEX IF NOT EXISTS idx_ar_customer ON accounts_receivable(customer_id);
CREATE INDEX IF NOT EXISTS idx_ar_status ON accounts_receivable(status);
CREATE INDEX IF NOT EXISTS idx_ap_supplier ON accounts_payable(supplier_id);
CREATE INDEX IF NOT EXISTS idx_ap_status ON accounts_payable(status);
CREATE INDEX IF NOT EXISTS idx_logs_entity ON operation_logs(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_logs_created_at ON operation_logs(created_at);
"""


def init_database() -> None:
    with get_db_connection() as conn:
        conn.executescript(SCHEMA_SQL)
        run_database_migrations(conn)
        seed_default_users(conn)
        conn.commit()


WORKFLOW_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS database_migrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    migration_key TEXT NOT NULL UNIQUE,
    description TEXT,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS system_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    setting_key TEXT NOT NULL UNIQUE,
    setting_value TEXT,
    description TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS permissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    module TEXT NOT NULL,
    action TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS role_permissions (
    role_id INTEGER NOT NULL,
    permission_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (role_id, permission_id),
    FOREIGN KEY (role_id) REFERENCES roles(id),
    FOREIGN KEY (permission_id) REFERENCES permissions(id)
);

CREATE TABLE IF NOT EXISTS order_status_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    action TEXT NOT NULL,
    operator_id INTEGER,
    remark TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (order_id) REFERENCES orders(id),
    FOREIGN KEY (operator_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS return_confirmations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    confirmation_no TEXT NOT NULL UNIQUE,
    delivery_id INTEGER NOT NULL,
    delivery_item_id INTEGER,
    order_id INTEGER,
    customer_id INTEGER NOT NULL,
    original_quantity INTEGER NOT NULL,
    confirmed_quantity INTEGER NOT NULL,
    difference_quantity INTEGER NOT NULL DEFAULT 0,
    return_date TEXT NOT NULL,
    return_status TEXT NOT NULL DEFAULT 'confirmed'
        CHECK (return_status IN ('pending_return', 'confirmed', 'quantity_exception', 'cancelled')),
    exception_note TEXT,
    confirmed_by INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT,
    UNIQUE (delivery_id, delivery_item_id),
    FOREIGN KEY (delivery_id) REFERENCES deliveries(id),
    FOREIGN KEY (delivery_item_id) REFERENCES delivery_items(id),
    FOREIGN KEY (order_id) REFERENCES orders(id),
    FOREIGN KEY (customer_id) REFERENCES customers(id),
    FOREIGN KEY (confirmed_by) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS statements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    statement_no TEXT NOT NULL UNIQUE,
    customer_id INTEGER NOT NULL,
    statement_month TEXT NOT NULL,
    statement_amount REAL NOT NULL DEFAULT 0,
    statement_status TEXT NOT NULL DEFAULT 'draft'
        CHECK (statement_status IN ('draft', 'confirmed', 'cancelled')),
    invoice_status TEXT NOT NULL DEFAULT 'uninvoiced'
        CHECK (invoice_status IN ('uninvoiced', 'invoiced')),
    payment_status TEXT NOT NULL DEFAULT 'unpaid'
        CHECK (payment_status IN ('unpaid', 'partial_paid', 'paid')),
    created_by INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    confirmed_at TEXT,
    remark TEXT,
    FOREIGN KEY (customer_id) REFERENCES customers(id),
    FOREIGN KEY (created_by) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS statement_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    statement_id INTEGER NOT NULL,
    return_confirmation_id INTEGER,
    delivery_id INTEGER NOT NULL,
    delivery_item_id INTEGER,
    order_id INTEGER,
    delivery_date TEXT,
    delivery_no TEXT,
    customer_po TEXT,
    style_no TEXT,
    product_name TEXT,
    specification TEXT,
    material TEXT,
    confirmed_quantity INTEGER NOT NULL,
    unit_price REAL NOT NULL,
    amount REAL NOT NULL,
    remark TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (statement_id) REFERENCES statements(id),
    FOREIGN KEY (return_confirmation_id) REFERENCES return_confirmations(id),
    FOREIGN KEY (delivery_id) REFERENCES deliveries(id),
    FOREIGN KEY (delivery_item_id) REFERENCES delivery_items(id),
    FOREIGN KEY (order_id) REFERENCES orders(id)
);

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_record_no TEXT NOT NULL UNIQUE,
    customer_id INTEGER NOT NULL,
    statement_id INTEGER NOT NULL,
    statement_month TEXT,
    statement_amount REAL NOT NULL,
    invoice_status TEXT NOT NULL DEFAULT 'uninvoiced'
        CHECK (invoice_status IN ('uninvoiced', 'invoiced')),
    invoice_number TEXT,
    invoice_date TEXT,
    payment_status TEXT NOT NULL DEFAULT 'unpaid'
        CHECK (payment_status IN ('unpaid', 'paid')),
    payment_date TEXT,
    remark TEXT,
    created_by INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT,
    FOREIGN KEY (customer_id) REFERENCES customers(id),
    FOREIGN KEY (statement_id) REFERENCES statements(id),
    FOREIGN KEY (created_by) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_return_confirmations_delivery ON return_confirmations(delivery_id);
CREATE INDEX IF NOT EXISTS idx_return_confirmations_order ON return_confirmations(order_id);
CREATE INDEX IF NOT EXISTS idx_statements_customer_month ON statements(customer_id, statement_month);
CREATE INDEX IF NOT EXISTS idx_statement_items_statement ON statement_items(statement_id);
CREATE INDEX IF NOT EXISTS idx_statement_items_delivery ON statement_items(delivery_id);
CREATE INDEX IF NOT EXISTS idx_invoices_statement ON invoices(statement_id);
CREATE INDEX IF NOT EXISTS idx_order_status_events_order ON order_status_events(order_id);
"""


BUSINESS_COLUMN_MIGRATIONS = {
    "customers": [
        ("customer_code", "TEXT"),
        ("credit_terms", "TEXT"),
        ("default_tax_rate", "REAL NOT NULL DEFAULT 0.13"),
        ("invoice_title", "TEXT"),
        ("tax_no", "TEXT"),
        ("bank_account", "TEXT"),
        ("remark", "TEXT"),
        ("status", "TEXT NOT NULL DEFAULT 'active'"),
    ],
    "product_archives": [
        ("press_line", "TEXT"),
        ("print_color", "TEXT"),
        ("craft_requirements", "TEXT"),
    ],
    "orders": [
        ("delivered_quantity", "INTEGER NOT NULL DEFAULT 0"),
        ("remaining_quantity", "INTEGER"),
        ("delivery_due_date", "TEXT"),
        ("salesperson", "TEXT"),
        ("remark", "TEXT"),
        ("business_status", "TEXT NOT NULL DEFAULT 'pending_material'"),
        ("return_status", "TEXT NOT NULL DEFAULT 'pending_return'"),
        ("statement_status", "TEXT NOT NULL DEFAULT 'not_statemented'"),
        ("invoice_status", "TEXT NOT NULL DEFAULT 'uninvoiced'"),
        ("payment_status", "TEXT NOT NULL DEFAULT 'unpaid'"),
    ],
    "deliveries": [
        ("driver_name", "TEXT"),
        ("vehicle_no", "TEXT"),
        ("contact_person", "TEXT"),
        ("delivery_address", "TEXT"),
        ("return_status", "TEXT NOT NULL DEFAULT 'pending_return'"),
        ("statement_status", "TEXT NOT NULL DEFAULT 'not_statemented'"),
    ],
    "delivery_items": [
        ("confirmed_quantity", "INTEGER"),
        ("difference_quantity", "INTEGER NOT NULL DEFAULT 0"),
        ("statement_status", "TEXT NOT NULL DEFAULT 'not_statemented'"),
    ],
}


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]) for row in rows}


def ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
    if column_name in table_columns(conn, table_name):
        return
    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def seed_database_metadata(conn: sqlite3.Connection) -> None:
    roles = [
        ("admin", "管理员", "拥有全部菜单、状态操作、打印和导出权限"),
        ("clerk", "文员", "负责订单、报料、送货和回单录入"),
        ("production", "生产", "查看生产任务并更新生产状态"),
        ("delivery", "送货", "查看送货单并更新送货和回单状态"),
        ("finance", "财务", "负责对账、开票和收款状态"),
        ("readonly", "只读账号", "只能查看业务数据"),
    ]
    for code, name, description in roles:
        conn.execute(
            """
            INSERT INTO roles (code, name, description)
            VALUES (?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                description = excluded.description,
                is_active = 1,
                updated_at = CURRENT_TIMESTAMP
            """,
            (code, name, description),
        )

    permissions = [
        ("orders.view", "查看订单", "orders", "view"),
        ("orders.create", "新增订单", "orders", "create"),
        ("orders.update", "编辑订单", "orders", "update"),
        ("orders.status", "订单状态操作", "orders", "status"),
        ("materials.status", "报料和到料操作", "materials", "status"),
        ("deliveries.create", "生成送货单", "deliveries", "create"),
        ("deliveries.status", "送货状态操作", "deliveries", "status"),
        ("returns.confirm", "回单确认", "returns", "confirm"),
        ("statements.manage", "月结对账", "statements", "manage"),
        ("invoices.manage", "开票收款", "invoices", "manage"),
        ("exports.use", "导出", "exports", "use"),
        ("prints.use", "打印", "prints", "use"),
        ("settings.manage", "系统设置", "settings", "manage"),
    ]
    for code, name, module, action in permissions:
        conn.execute(
            """
            INSERT INTO permissions (code, name, module, action)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                module = excluded.module,
                action = excluded.action
            """,
            (code, name, module, action),
        )

    settings = [
        ("company_name", "三级纸箱厂ERP", "公司名称"),
        ("print_title", "送货单", "默认打印抬头"),
        ("default_tax_rate", "0.13", "默认税率"),
        ("delivery_print_template", "half_page", "送货单打印模板"),
        ("statement_print_template", "monthly_statement", "对账单打印模板"),
    ]
    for key, value, description in settings:
        conn.execute(
            """
            INSERT INTO system_settings (setting_key, setting_value, description)
            VALUES (?, ?, ?)
            ON CONFLICT(setting_key) DO NOTHING
            """,
            (key, value, description),
        )


def run_database_migrations(conn: sqlite3.Connection) -> None:
    conn.executescript(WORKFLOW_SCHEMA_SQL)
    for table_name, columns in BUSINESS_COLUMN_MIGRATIONS.items():
        for column_name, definition in columns:
            ensure_column(conn, table_name, column_name, definition)
    conn.execute(
        """
        UPDATE orders
        SET remaining_quantity = order_quantity - delivered_quantity
        WHERE remaining_quantity IS NULL
        """
    )
    seed_database_metadata(conn)
    conn.execute(
        """
        INSERT INTO database_migrations (migration_key, description)
        VALUES ('20260605_workflow_database_shape', '补齐回单、月结对账、开票、权限、系统设置和状态流转数据库结构')
        ON CONFLICT(migration_key) DO NOTHING
        """
    )


def password_hash(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def seed_default_users(conn: sqlite3.Connection) -> None:
    default_users = [
        ("boss", password_hash("123456"), "boss", "老板端"),
        ("workshop", password_hash("123456"), "workshop", "车间端"),
    ]
    # The live DB may use the app-overlay schema, which diverges from the legacy
    # users DDL: it adds a NOT NULL real_name column and a role CHECK that only
    # allows admin/finance/sales/workshop (no 'boss'). Introspect the actual table
    # so seeding adapts instead of crashing on boot.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    has_real_name = "real_name" in columns
    for username, hashed, role, display_name in default_users:
        cols = ["username", "password_hash", "role", "display_name"]
        vals = [username, hashed, role, display_name]
        if has_real_name:
            cols.append("real_name")
            vals.append(display_name)
        placeholders = ", ".join("?" for _ in cols)
        update_cols = [c for c in cols if c != "username"]
        update_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols)
        sql = (
            f"INSERT INTO users ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(username) DO UPDATE SET {update_clause}, "
            "is_active = 1, updated_at = CURRENT_TIMESTAMP"
        )
        # Use a savepoint so a default user the live schema rejects (e.g. the
        # legacy 'boss' role, which the migrated CHECK constraint forbids) is
        # skipped cleanly without aborting the whole boot transaction.
        conn.execute("SAVEPOINT seed_user")
        try:
            conn.execute(sql, vals)
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK TO seed_user")
        finally:
            conn.execute("RELEASE seed_user")


class LoginRequest(BaseModel):
    username: str
    password: str


class OrderCreateRequest(BaseModel):
    customer_name: str | None = None
    customer_id: int | None = None
    product_archive_id: int | None = None
    customer_po: str | None = None
    style_no: str
    product_name: str | None = None
    material: str | None = None
    flute_type: str | None = None
    process_note: str | None = None
    delivery_due_date: str | None = None
    remark: str | None = None
    unit: str = "只"
    length_mm: float | None = None
    width_mm: float | None = None
    height_mm: float | None = None
    order_quantity: int
    sale_unit_price: float
    sale_unit_price_no_tax: float | None = None
    cost_unit_price: float | None = None
    warning_confirmed: bool = False
    created_by: int | None = None


class CustomerUpsertRequest(BaseModel):
    customer_code: str | None = None
    name: str
    contact_person: str | None = None
    phone: str | None = None
    address: str | None = None
    credit_terms: str | None = None
    default_tax_rate: float = 0.13
    invoice_title: str | None = None
    tax_no: str | None = None
    bank_account: str | None = None
    remark: str | None = None
    status: str = "active"


def find_active_user(username: str) -> sqlite3.Row | None:
    with get_db_connection() as conn:
        return conn.execute(
            """
            SELECT id, username, password_hash, role, display_name
            FROM users
            WHERE username = ? AND is_active = 1
            """,
            (username,),
        ).fetchone()


def write_login_log(user: sqlite3.Row) -> None:
    with get_db_connection() as conn:
        cols = ["user_id", "username", "role", "action", "entity_type", "entity_id", "description"]
        vals = [user["id"], user["username"], user["role"], "login", "user", user["id"], "用户登录"]
        # The app-overlay schema adds a NOT NULL `resource` column the legacy
        # insert never supplied; provide it when present so login logging works.
        table_cols = {row["name"] for row in conn.execute("PRAGMA table_info(operation_logs)")}
        if "resource" in table_cols:
            cols.append("resource")
            vals.append("user")
        placeholders = ", ".join("?" for _ in cols)
        conn.execute(
            f"INSERT INTO operation_logs ({', '.join(cols)}) VALUES ({placeholders})",
            vals,
        )
        conn.commit()


def verify_login(username: str, password: str) -> sqlite3.Row:
    user = find_active_user(username.strip())
    if user is None or user["password_hash"] != password_hash(password):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return user


def repair_legacy_text(value: str) -> str:
    """Repair common UTF-8-as-Latin1 mojibake from legacy SQL exports."""
    if not value:
        return value
    if not any(marker in value for marker in ("Ã", "Â", "ä", "å", "ç", "è", "é")):
        return value
    try:
        return value.encode("latin1").decode("utf-8")
    except UnicodeError:
        return value


def row_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    return {key: repair_legacy_text(value) if isinstance(value, str) else value for key, value in data.items()}


def normalize_text(value: str | None) -> str:
    return (value or "").strip()


def product_style_key(value: str | None) -> str:
    text = normalize_text(value).lower()
    if not text:
        return ""
    return text.split("/", 1)[0].strip()


PRODUCT_DIMENSION_RE = re.compile(
    r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?(?:\s*(?:\*|x|X|×)\s*\d+(?:\.\d+)?){1,2})(?!\d)"
)
PRODUCT_CODE_RE = re.compile(r"^\s*([A-Za-z0-9_-]{4,})\s*(?:/|／)?\s*(.*)$")


def format_dimension(value: float) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:.2f}".rstrip("0").rstrip(".")


def parse_product_label(
    style_no: str | None,
    length_mm: float | None = None,
    width_mm: float | None = None,
    height_mm: float | None = None,
) -> dict:
    raw = normalize_text(style_no)
    code_match = PRODUCT_CODE_RE.match(raw)
    product_code = (
        normalize_text(code_match.group(1))
        if code_match
        else normalize_text(raw.split("/", 1)[0])
    )
    body = (
        normalize_text(code_match.group(2))
        if code_match
        else normalize_text(raw.split("/", 1)[-1])
    )
    dimension_match = PRODUCT_DIMENSION_RE.search(body)
    product_name = body
    specification = ""
    feature_note = ""
    source = "name"
    confidence = "manual"

    if dimension_match:
        specification = re.sub(r"\s*(?:\*|x|X|×)\s*", "×", dimension_match.group(1))
        before = body[: dimension_match.start()].strip(" /-_")
        after = body[dimension_match.end() :].strip(" /-_")
        if before:
            product_name = before
            feature_note = after
            confidence = "high"
        else:
            name_match = re.match(r"^([^\d*×xX]+)", after)
            product_name = normalize_text(name_match.group(1)) if name_match else ""
            feature_note = normalize_text(after[len(product_name) :]) if product_name else after
            confidence = "medium" if product_name else "manual"
    else:
        dimensions = [
            value
            for value in (length_mm, width_mm, height_mm)
            if value not in (None, 0, "")
        ]
        if len(dimensions) >= 2:
            specification = "×".join(
                format_dimension(float(value) / 10) for value in dimensions
            )
            source = "database"
            confidence = "medium" if product_name else "manual"

    return {
        "product_code": product_code,
        "display_product_name": re.sub(r"\s+", " ", product_name).strip(),
        "specification": specification,
        "feature_note": feature_note,
        "parse_source": source,
        "parse_confidence": confidence,
    }


def normalize_float(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 4)


def values_different(history_value, current_value) -> bool:
    if history_value is None or current_value is None:
        return False
    if isinstance(history_value, (int, float)) or isinstance(current_value, (int, float)):
        return normalize_float(history_value) != normalize_float(current_value)
    return normalize_text(str(history_value)) != normalize_text(str(current_value))


def next_order_no() -> str:
    return "SO" + datetime.now().strftime("%Y%m%d%H%M%S%f")[:-3]


def archive_search_rows(keyword: str) -> list[dict]:
    like_keyword = f"%{keyword}%"
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                pa.id,
                pa.customer_id,
                c.name AS customer_name,
                pa.style_no,
                pa.customer_po,
                pa.product_name,
                pa.unit,
                pa.length_mm,
                pa.width_mm,
                pa.height_mm,
                pa.material,
                pa.flute_type,
                pa.layer_count,
                pa.color_count,
                pa.process_note,
                pa.last_sale_unit_price AS sale_unit_price,
                pa.sale_unit_price_no_tax,
                pa.last_cost_unit_price AS cost_unit_price,
                pa.drawing_path,
                pa.die_cut_path,
                pa.updated_at
            FROM product_archives pa
            JOIN customers c ON c.id = pa.customer_id
            WHERE c.name LIKE ?
               OR pa.style_no LIKE ?
               OR pa.customer_po LIKE ?
            ORDER BY pa.updated_at DESC, pa.id DESC
            LIMIT 20
            """,
            (like_keyword, like_keyword, like_keyword),
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def customer_rows() -> list[dict]:
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, name, contact_person, phone, address, billing_note
            FROM customers
            WHERE is_active = 1
            ORDER BY name COLLATE NOCASE, id
            """
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def customer_management_rows(
    keyword: str = "",
    status: str = "",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    safe_page = max(1, int(page or 1))
    safe_page_size = max(10, min(int(page_size or 20), 100))
    filters = []
    params: list[object] = []
    clean_keyword = normalize_text(keyword)
    if clean_keyword:
        filters.append(
            """
            (
                c.name LIKE ?
                OR COALESCE(c.customer_code, '') LIKE ?
                OR COALESCE(c.contact_person, '') LIKE ?
                OR COALESCE(c.phone, '') LIKE ?
                OR COALESCE(c.address, '') LIKE ?
            )
            """
        )
        like_keyword = f"%{clean_keyword}%"
        params.extend([like_keyword] * 5)
    if status in {"active", "inactive"}:
        filters.append("c.status = ?")
        params.append(status)
    where_sql = "WHERE " + " AND ".join(filters) if filters else ""
    offset = (safe_page - 1) * safe_page_size

    with get_db_connection() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS count FROM customers c {where_sql}",
            tuple(params),
        ).fetchone()["count"]
        rows = conn.execute(
            f"""
            SELECT
                c.id,
                c.customer_code,
                c.name,
                c.contact_person,
                c.phone,
                c.address,
                c.credit_terms,
                c.default_tax_rate,
                c.invoice_title,
                c.tax_no,
                c.bank_account,
                c.remark,
                c.status,
                c.is_active,
                c.created_at,
                c.updated_at,
                (SELECT COUNT(*) FROM product_archives p WHERE p.customer_id = c.id) AS product_count,
                (SELECT COUNT(*) FROM orders o WHERE o.customer_id = c.id) AS order_count,
                (SELECT COUNT(*) FROM company_file_index f WHERE f.customer_id = c.id) AS file_count
            FROM customers c
            {where_sql}
            ORDER BY
                CASE WHEN c.status = 'active' THEN 0 ELSE 1 END,
                c.name COLLATE NOCASE,
                c.id
            LIMIT ? OFFSET ?
            """,
            tuple([*params, safe_page_size, offset]),
        ).fetchall()
        summary = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN status = 'inactive' THEN 1 ELSE 0 END) AS inactive,
                SUM(CASE WHEN contact_person IS NULL OR TRIM(contact_person) = '' THEN 1 ELSE 0 END) AS missing_contact,
                SUM(CASE WHEN phone IS NULL OR TRIM(phone) = '' THEN 1 ELSE 0 END) AS missing_phone
            FROM customers
            """
        ).fetchone()
    return {
        "items": [row_to_dict(row) for row in rows],
        "pagination": {
            "page": safe_page,
            "page_size": safe_page_size,
            "total": total,
            "pages": max(1, (total + safe_page_size - 1) // safe_page_size),
        },
        "summary": dict(summary),
    }


def customer_management_detail(customer_id: int) -> dict:
    with get_db_connection() as conn:
        customer = conn.execute(
            """
            SELECT
                c.*,
                (SELECT COUNT(*) FROM product_archives p WHERE p.customer_id = c.id) AS product_count,
                (SELECT COUNT(*) FROM orders o WHERE o.customer_id = c.id) AS order_count,
                (SELECT COUNT(*) FROM company_file_index f WHERE f.customer_id = c.id) AS file_count
            FROM customers c
            WHERE c.id = ?
            """,
            (customer_id,),
        ).fetchone()
        if customer is None:
            raise HTTPException(status_code=404, detail="客户不存在")
        products = conn.execute(
            """
            SELECT
                id,
                style_no,
                customer_po,
                product_name,
                length_mm,
                width_mm,
                height_mm,
                material,
                flute_type,
                last_sale_unit_price
            FROM product_archives
            WHERE customer_id = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT 20
            """,
            (customer_id,),
        ).fetchall()
    return {
        "customer": row_to_dict(customer),
        "products": [row_to_dict(row) for row in products],
    }


def customer_product_rows(
    customer_id: int,
    keyword: str = "",
    sort: str = "frequent",
    limit: int = 50,
) -> list[dict]:
    safe_limit = max(10, min(int(limit or 50), 200))
    clean_keyword = normalize_text(keyword).lower()
    with get_db_connection() as conn:
        customer = conn.execute(
            "SELECT id FROM customers WHERE id = ?",
            (customer_id,),
        ).fetchone()
        if customer is None:
            raise HTTPException(status_code=404, detail="客户不存在")
        archive_rows = conn.execute(
            """
            SELECT
                id,
                customer_id,
                style_no,
                customer_po,
                product_name,
                unit,
                length_mm,
                width_mm,
                height_mm,
                material,
                flute_type,
                process_note,
                last_sale_unit_price AS sale_unit_price,
                sale_unit_price_no_tax,
                last_cost_unit_price AS cost_unit_price,
                updated_at
            FROM product_archives
            WHERE customer_id = ?
            ORDER BY updated_at DESC, id DESC
            """,
            (customer_id,),
        ).fetchall()
    archives = [row_to_dict(row) for row in archive_rows]
    items: list[dict] = []
    for archive in archives:
        archive_id = int(archive["id"])
        item = {
            **archive,
            "product_archive_id": archive_id,
            "order_count": 0,
            "total_quantity": 0,
            "last_order_date": None,
            "source": "product_archive",
        }
        item.update(
            parse_product_label(
                item["style_no"],
                item["length_mm"],
                item["width_mm"],
                item["height_mm"],
            )
        )
        items.append(item)

    if clean_keyword:
        items = [
            item
            for item in items
            if clean_keyword
            in " ".join(
                str(item.get(field) or "").lower()
                for field in (
                    "style_no",
                    "product_code",
                    "display_product_name",
                    "specification",
                    "customer_po",
                    "product_name",
                    "material",
                )
            )
        ]
    items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return items[:safe_limit]


def save_customer_record(payload: CustomerUpsertRequest, customer_id: int | None = None) -> dict:
    name = normalize_text(payload.name)
    if not name:
        raise HTTPException(status_code=400, detail="客户名称不能为空")
    status = payload.status if payload.status in {"active", "inactive"} else "active"
    tax_rate = max(0, min(float(payload.default_tax_rate or 0), 1))
    values = (
        normalize_text(payload.customer_code) or None,
        name,
        normalize_text(payload.contact_person) or None,
        normalize_text(payload.phone) or None,
        normalize_text(payload.address) or None,
        normalize_text(payload.credit_terms) or None,
        tax_rate,
        normalize_text(payload.invoice_title) or None,
        normalize_text(payload.tax_no) or None,
        normalize_text(payload.bank_account) or None,
        normalize_text(payload.remark) or None,
        status,
        1 if status == "active" else 0,
    )
    with get_db_connection() as conn:
        duplicate = conn.execute(
            "SELECT id FROM customers WHERE name = ? AND (? IS NULL OR id <> ?)",
            (name, customer_id, customer_id),
        ).fetchone()
        if duplicate:
            raise HTTPException(status_code=409, detail="已存在同名客户，请检查后再保存")
        if customer_id is None:
            cursor = conn.execute(
                """
                INSERT INTO customers (
                    customer_code, name, contact_person, phone, address, credit_terms,
                    default_tax_rate, invoice_title, tax_no, bank_account, remark,
                    status, is_active
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            customer_id = int(cursor.lastrowid)
            action = "create_customer"
        else:
            exists = conn.execute("SELECT id FROM customers WHERE id = ?", (customer_id,)).fetchone()
            if exists is None:
                raise HTTPException(status_code=404, detail="客户不存在")
            conn.execute(
                """
                UPDATE customers
                SET
                    customer_code = ?,
                    name = ?,
                    contact_person = ?,
                    phone = ?,
                    address = ?,
                    credit_terms = ?,
                    default_tax_rate = ?,
                    invoice_title = ?,
                    tax_no = ?,
                    bank_account = ?,
                    remark = ?,
                    status = ?,
                    is_active = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (*values, customer_id),
            )
            action = "update_customer"
        conn.execute(
            """
            INSERT INTO operation_logs (
                username, role, action, entity_type, entity_id, description
            )
            VALUES ('system', 'boss', ?, 'customer', ?, ?)
            """,
            (action, customer_id, f"维护客户资料：{name}"),
        )
        conn.commit()
    return customer_management_detail(customer_id)["customer"]


def set_customer_status(customer_id: int, status: str) -> dict:
    if status not in {"active", "inactive"}:
        raise HTTPException(status_code=400, detail="无效的客户状态")
    with get_db_connection() as conn:
        customer = conn.execute("SELECT name FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if customer is None:
            raise HTTPException(status_code=404, detail="客户不存在")
        conn.execute(
            """
            UPDATE customers
            SET status = ?, is_active = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, 1 if status == "active" else 0, customer_id),
        )
        conn.execute(
            """
            INSERT INTO operation_logs (
                username, role, action, entity_type, entity_id, description
            )
            VALUES ('system', 'boss', 'change_customer_status', 'customer', ?, ?)
            """,
            (customer_id, f"客户 {customer['name']} 状态变更为 {status}"),
        )
        conn.commit()
    return customer_management_detail(customer_id)["customer"]


def customer_style_rows(customer_id: int, limit: int = 100) -> list[dict]:
    safe_limit = max(1, min(int(limit or 100), 300))
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                customer_id,
                style_no,
                customer_po,
                product_name,
                unit,
                length_mm,
                width_mm,
                height_mm,
                material,
                flute_type,
                layer_count,
                color_count,
                process_note,
                last_sale_unit_price AS sale_unit_price,
                sale_unit_price_no_tax,
                last_cost_unit_price AS cost_unit_price,
                drawing_path,
                die_cut_path,
                updated_at
            FROM product_archives
            WHERE customer_id = ?
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (customer_id, safe_limit),
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def order_rows(customer_id: int | None = None) -> list[dict]:
    sql = """
        SELECT
            o.id,
            o.order_no,
            o.customer_id,
            c.name AS customer_name,
            o.customer_po,
            o.style_no,
            o.product_name,
            o.unit,
            o.length_mm,
            o.width_mm,
            o.height_mm,
            o.material,
            o.order_quantity,
            o.sale_unit_price,
            o.status,
            o.created_at
        FROM orders o
        JOIN customers c ON c.id = o.customer_id
    """
    params: tuple = ()
    if customer_id:
        sql += " WHERE o.customer_id = ?"
        params = (customer_id,)
    sql += " ORDER BY o.created_at DESC, o.id DESC LIMIT 300"
    with get_db_connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [row_to_dict(row) for row in rows]


def get_or_create_customer(conn: sqlite3.Connection, customer_name: str) -> int:
    name = normalize_text(customer_name)
    if not name:
        raise HTTPException(status_code=400, detail="客户名不能为空")

    row = conn.execute("SELECT id FROM customers WHERE name = ?", (name,)).fetchone()
    if row:
        return int(row["id"])

    cursor = conn.execute("INSERT INTO customers (name) VALUES (?)", (name,))
    return int(cursor.lastrowid)


def resolve_customer_id(conn: sqlite3.Connection, payload: OrderCreateRequest) -> int:
    if payload.customer_id:
        row = conn.execute(
            "SELECT id FROM customers WHERE id = ? AND is_active = 1",
            (payload.customer_id,),
        ).fetchone()
        if row:
            return int(row["id"])
        raise HTTPException(status_code=400, detail="选择的客户不存在")
    return get_or_create_customer(conn, payload.customer_name or "")


def find_product_archive(
    conn: sqlite3.Connection, customer_id: int, style_no: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM product_archives
        WHERE customer_id = ? AND style_no = ?
        """,
        (customer_id, style_no),
    ).fetchone()


def build_archive_differences(archive: sqlite3.Row, payload: OrderCreateRequest) -> list[dict]:
    checks = [
        ("material", "材质", archive["material"], payload.material),
        ("flute_type", "楞型", archive["flute_type"], payload.flute_type),
        ("length_mm", "长度", archive["length_mm"], payload.length_mm),
        ("width_mm", "宽度", archive["width_mm"], payload.width_mm),
        ("height_mm", "高度", archive["height_mm"], payload.height_mm),
        ("sale_unit_price", "含税单价", archive["last_sale_unit_price"], payload.sale_unit_price),
    ]
    differences = []
    for field, label, old_value, new_value in checks:
        if values_different(old_value, new_value):
            differences.append(
                {
                    "field": field,
                    "label": label,
                    "old_value": old_value,
                    "new_value": new_value,
                    "message": f"注意：{label}与历史不一致，历史为 {old_value}，当前为 {new_value}",
                }
            )
    return differences


def upsert_product_archive(
    conn: sqlite3.Connection,
    customer_id: int,
    payload: OrderCreateRequest,
    sale_unit_price_no_tax: float,
    cost_unit_price: float,
    archive_id: int | None = None,
) -> int:
    style_no = normalize_text(payload.style_no)
    if not style_no:
        raise HTTPException(status_code=400, detail="款号不能为空")

    values = (
        customer_id,
        style_no,
        normalize_text(payload.customer_po) or None,
        normalize_text(payload.product_name) or None,
        normalize_text(payload.unit) or "只",
        payload.length_mm,
        payload.width_mm,
        payload.height_mm,
        normalize_text(payload.material) or None,
        normalize_text(payload.flute_type) or None,
        normalize_text(payload.process_note) or None,
        payload.sale_unit_price,
        sale_unit_price_no_tax,
        cost_unit_price,
    )

    if archive_id:
        conn.execute(
            """
            UPDATE product_archives
            SET
                customer_po = ?,
                product_name = ?,
                unit = ?,
                length_mm = ?,
                width_mm = ?,
                height_mm = ?,
                material = ?,
                flute_type = ?,
                process_note = ?,
                last_sale_unit_price = ?,
                sale_unit_price_no_tax = ?,
                last_cost_unit_price = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                values[2],
                values[3],
                values[4],
                values[5],
                values[6],
                values[7],
                values[8],
                values[9],
                values[10],
                values[11],
                values[12],
                values[13],
                archive_id,
            ),
        )
        return archive_id

    cursor = conn.execute(
        """
        INSERT INTO product_archives (
            customer_id,
            style_no,
            customer_po,
            product_name,
            unit,
            length_mm,
            width_mm,
            height_mm,
            material,
            flute_type,
            process_note,
            last_sale_unit_price,
            sale_unit_price_no_tax,
            last_cost_unit_price
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        values,
    )
    return int(cursor.lastrowid)


def insert_order_warnings(
    conn: sqlite3.Connection,
    order_id: int,
    customer_id: int,
    style_no: str,
    differences: list[dict],
    confirmed_by: int | None,
) -> None:
    for diff in differences:
        conn.execute(
            """
            INSERT INTO order_warnings (
                order_id,
                customer_id,
                style_no,
                warning_type,
                old_value,
                new_value,
                confirmed_by,
                confirmed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                order_id,
                customer_id,
                style_no,
                diff["field"],
                str(diff["old_value"]),
                str(diff["new_value"]),
                confirmed_by,
            ),
        )


def create_order_record(payload: OrderCreateRequest) -> dict:
    if payload.order_quantity <= 0:
        raise HTTPException(status_code=400, detail="下单数量必须大于 0")
    if payload.sale_unit_price < 0:
        raise HTTPException(status_code=400, detail="单价不能为负数")

    style_no = normalize_text(payload.style_no)
    with get_db_connection() as conn:
        customer_id = resolve_customer_id(conn, payload)
        archive = None
        if payload.product_archive_id:
            archive = conn.execute(
                """
                SELECT *
                FROM product_archives
                WHERE id = ? AND customer_id = ?
                """,
                (payload.product_archive_id, customer_id),
            ).fetchone()
        if archive is None:
            archive = find_product_archive(conn, customer_id, style_no)
        differences = build_archive_differences(archive, payload) if archive else []
        sale_unit_price_no_tax = round(payload.sale_unit_price / 1.13, 4)
        cost_unit_price = (
            float(archive["last_cost_unit_price"] or 0)
            if archive
            else float(payload.cost_unit_price or 0)
        )

        if differences and not payload.warning_confirmed:
            return {
                "conflict": True,
                "differences": differences,
                "message": "当前订单与历史档案存在差异，请老板确认后再生成订单。",
            }

        archive_id = (
            int(archive["id"])
            if archive
            else upsert_product_archive(
                conn,
                customer_id,
                payload,
                sale_unit_price_no_tax,
                cost_unit_price,
            )
        )
        warning_reason = "；".join(diff["message"] for diff in differences) if differences else None
        cursor = conn.execute(
            """
            INSERT INTO orders (
                order_no,
                customer_id,
                product_archive_id,
                customer_po,
                style_no,
                product_name,
                unit,
                length_mm,
                width_mm,
                height_mm,
                material,
                flute_type,
                process_note,
                order_quantity,
                remaining_quantity,
                sale_unit_price,
                sale_unit_price_no_tax,
                cost_unit_price,
                delivery_due_date,
                remark,
                status,
                warning_confirmed,
                warning_reason,
                created_by
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                'pending_material', ?, ?, ?
            )
            """,
            (
                next_order_no(),
                customer_id,
                archive_id,
                normalize_text(payload.customer_po) or None,
                style_no,
                normalize_text(payload.product_name) or None,
                normalize_text(payload.unit) or "只",
                payload.length_mm,
                payload.width_mm,
                payload.height_mm,
                normalize_text(payload.material) or None,
                normalize_text(payload.flute_type) or None,
                normalize_text(payload.process_note) or None,
                payload.order_quantity,
                payload.order_quantity,
                payload.sale_unit_price,
                sale_unit_price_no_tax,
                cost_unit_price,
                normalize_text(payload.delivery_due_date) or None,
                normalize_text(payload.remark) or None,
                1 if payload.warning_confirmed else 0,
                warning_reason,
                payload.created_by,
            ),
        )
        order_id = int(cursor.lastrowid)
        if differences:
            insert_order_warnings(
                conn,
                order_id,
                customer_id,
                style_no,
                differences,
                payload.created_by,
            )

        row = conn.execute(
            """
            SELECT o.*, c.name AS customer_name
            FROM orders o
            JOIN customers c ON c.id = o.customer_id
            WHERE o.id = ?
            """,
            (order_id,),
        ).fetchone()
        conn.commit()
        return {"conflict": False, "order": row_to_dict(row)}


def ensure_static_files_exist() -> None:
    if not index_html_path().exists():
        raise RuntimeError(
            f"前端文件不存在: {index_html_path()}。请确认 static/index.html 已放入程序目录。"
        )


def mount_static_files(app_instance: FastAPI) -> None:
    ensure_static_files_exist()
    app_instance.mount("/static", StaticFiles(directory=static_dir()), name="static")


def get_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def print_startup_banner() -> None:
    lan_ip = get_lan_ip()
    url = f"http://{lan_ip}:{SERVER_PORT}"
    print("\n" + "=" * 56)
    print(f"{APP_NAME} 已启动")
    print(f"局域网访问地址: {url}")
    print(f"数据库文件: {db_path()}")
    print("=" * 56)

    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception:
        print("二维码模块未安装；安装 qrcode 后可在终端显示扫码二维码。")

    print("=" * 56 + "\n")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_database()
    yield


app = FastAPI(title=APP_NAME, version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health_check() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "app": APP_NAME,
            "database": str(db_path()),
        }
    )


@app.get("/api/meta")
def app_meta() -> JSONResponse:
    return JSONResponse(
        {
            "app_name": APP_NAME,
            "version": "0.1.0",
            "lan_ip": get_lan_ip(),
            "port": SERVER_PORT,
        }
    )


@app.post("/api/login")
def login(payload: LoginRequest) -> JSONResponse:
    user = verify_login(payload.username, payload.password)
    write_login_log(user)
    return JSONResponse(
        {
            "ok": True,
            "user": {
                "id": user["id"],
                "username": user["username"],
                "role": user["role"],
                "display_name": user["display_name"],
            },
        }
    )


@app.get("/api/archives/search")
def search_archives(keyword: str = "") -> JSONResponse:
    clean_keyword = normalize_text(keyword)
    if not clean_keyword:
        return JSONResponse({"ok": True, "items": []})
    return JSONResponse({"ok": True, "items": archive_search_rows(clean_keyword)})


@app.get("/api/customers")
def list_customers() -> JSONResponse:
    return JSONResponse({"ok": True, "items": customer_rows()})


@app.get("/api/customer-management")
def list_customer_management(
    keyword: str = "",
    status: str = "",
    page: int = 1,
    page_size: int = 20,
) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            **customer_management_rows(keyword, status, page, page_size),
        }
    )


@app.get("/api/customer-management/{customer_id}")
def get_customer_management(customer_id: int) -> JSONResponse:
    return JSONResponse({"ok": True, **customer_management_detail(customer_id)})


@app.get("/api/customer-management/{customer_id}/products")
def list_customer_management_products(
    customer_id: int,
    keyword: str = "",
    sort: str = "frequent",
    limit: int = 50,
) -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "items": customer_product_rows(customer_id, keyword, sort, limit),
        }
    )


@app.post("/api/customer-management")
def create_customer_management(payload: CustomerUpsertRequest) -> JSONResponse:
    return JSONResponse(
        status_code=201,
        content={"ok": True, "customer": save_customer_record(payload)},
    )


@app.put("/api/customer-management/{customer_id}")
def update_customer_management(
    customer_id: int,
    payload: CustomerUpsertRequest,
) -> JSONResponse:
    return JSONResponse(
        {"ok": True, "customer": save_customer_record(payload, customer_id)}
    )


@app.post("/api/customer-management/{customer_id}/status")
def update_customer_management_status(customer_id: int, status: str) -> JSONResponse:
    return JSONResponse(
        {"ok": True, "customer": set_customer_status(customer_id, status)}
    )


@app.get("/api/customers/{customer_id}/styles")
def list_customer_styles(customer_id: int, limit: int = 100) -> JSONResponse:
    return JSONResponse({"ok": True, "items": customer_style_rows(customer_id, limit)})


@app.get("/api/orders")
def list_orders(customer_id: int | None = None) -> JSONResponse:
    return JSONResponse({"ok": True, "items": order_rows(customer_id)})


@app.post("/api/orders")
def create_order(payload: OrderCreateRequest) -> JSONResponse:
    result = create_order_record(payload)
    if result["conflict"]:
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "code": "ORDER_HISTORY_CONFLICT",
                "message": result["message"],
                "differences": result["differences"],
            },
        )
    return JSONResponse({"ok": True, "order": result["order"]})


@app.get("/")
def index(request: Request):
    return conditional_file_response(request, index_html_path())


@app.get("/customers")
def customer_management_page() -> FileResponse:
    return FileResponse(static_dir() / "customers.html")


@app.get("/incoming.html")
def incoming_management_page() -> FileResponse:
    return FileResponse(
        static_dir() / "incoming.html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/delivery-print.html")
def delivery_print_page() -> FileResponse:
    return FileResponse(
        static_dir() / "delivery-print.html",
        headers={"Cross-Origin-Opener-Policy": "noopener-allow-popups"},
    )


mount_static_files(app)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    print_startup_banner()
    from app.main import app as secure_app
    from app.core.config import load_settings

    runtime_settings = load_settings()

    uvicorn.run(
        secure_app,
        host=runtime_settings.bind_host,
        port=runtime_settings.port,
        reload=False,
        access_log=True,
    )
elif "app.main" not in sys.modules:
    # Preserve the historical ``main:app`` ASGI target without exposing the
    # pre-router legacy application and its weaker middleware configuration.
    from app.main import create_app as _create_secure_app

    app = _create_secure_app()
