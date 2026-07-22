from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ALLOWED_FLUTES = {3: {"A", "B", "E"}, 5: {"AB", "BE"}, 7: {"AAA", "ABC"}}
QUOTE_SUFFIX = re.compile(r"-(?:AB/EB|BA/EB|B/E|AAA|ABC|AB|BE|A|B|E)$", re.I)
FLUTE_SUFFIX = re.compile(r"(?:/|-)(AAA|ABC|AB|BE|A|B|E)$", re.I)


def exact_flute(value: object, layer_count: int | None) -> str | None:
    flute = str(value or "").strip().upper()
    return flute if flute in ALLOWED_FLUTES.get(layer_count, set()) else None


def code_flute(value: object, layer_count: int | None) -> str | None:
    match = FLUTE_SUFFIX.search(str(value or "").strip().upper().replace(" ", ""))
    return exact_flute(match.group(1), layer_count) if match else None


def canonical_code(value: object, layer_count: int | None) -> str:
    code = str(value or "").strip().upper().replace(" ", "")
    code = code.split("｜", 1)[0]
    code = QUOTE_SUFFIX.sub("", code)
    if layer_count in ALLOWED_FLUTES:
        allowed = "|".join(sorted(ALLOWED_FLUTES[layer_count], key=len, reverse=True))
        code = re.sub(rf"/(?:{allowed})$", "", code, flags=re.I)
    return code


def valid_code(code: str, layer_count: int | None) -> bool:
    return bool(
        layer_count in ALLOWED_FLUTES
        and re.fullmatch(r"[A-Z0-9]+", code)
        and len(code) == layer_count
    )


def resolve_target(row: sqlite3.Row) -> tuple[str, int, str, str]:
    material_layer = row["material_layer"]
    product_layer = row["product_layer"]
    raw_code = row["row_code"]
    material_code = row["material_code"]

    candidates: list[tuple[str, int | None, str]] = []
    if material_code:
        candidates.append(("material", material_layer, canonical_code(material_code, material_layer)))
    if raw_code:
        raw_base = canonical_code(raw_code, product_layer)
        candidates.append(("snapshot", product_layer, raw_base))
        compact = re.sub(r"[^A-Z0-9]", "", raw_base)
        if len(compact) in ALLOWED_FLUTES:
            candidates.append(("snapshot_length", len(compact), compact))
    for source, layer_count, code in candidates:
        if not valid_code(code, layer_count):
            continue
        flute_sources = (
            ("existing", row["existing_flute"]),
            ("product", row["product_flute"]),
            ("product_code", code_flute(row["default_material_code"], layer_count)),
            ("snapshot_code", code_flute(raw_code, layer_count)),
            ("material", row["material_flute"]),
        )
        for flute_source, value in flute_sources:
            flute = exact_flute(value, layer_count)
            if flute:
                return code, int(layer_count), flute, f"{source}:{flute_source}"
    raise ValueError(
        f"无法唯一规范化：row={row['id']} product={row['product_code']} "
        f"code={raw_code!r} layer={row['product_layer']!r} flute={row['existing_flute']!r}"
    )


def needs_cleanup(code: object, layer_count: object, flute: object) -> bool:
    try:
        layer = int(layer_count)
    except (TypeError, ValueError):
        return True
    normalized = canonical_code(code, layer)
    return (
        not valid_code(normalized, layer)
        or str(code or "").strip().upper() != normalized
        or exact_flute(flute, layer) is None
    )


def load_order_item_plan(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute(
        """
        SELECT soi.id, soi.snapshot_material AS row_code,
               soi.layer_count AS product_layer, soi.flute_type AS existing_flute,
               m.code AS material_code, m.layer_count AS material_layer,
               m.flute_type AS material_flute,
               p.product_code, p.default_material_code,
               p.layer_count AS current_product_layer,
               p.flute_type AS product_flute
        FROM sales_order_items soi
        LEFT JOIN materials m ON m.id = soi.material_id
        LEFT JOIN products p ON p.id = soi.product_id
        ORDER BY soi.id
        """
    ).fetchall()
    plan = []
    for row in rows:
        if not needs_cleanup(row["row_code"], row["product_layer"], row["existing_flute"]):
            continue
        target = resolve_target(row)
        plan.append(
            {
                "table": "sales_order_items",
                "id": row["id"],
                "product_code": row["product_code"],
                "before": [row["row_code"], row["product_layer"], row["existing_flute"]],
                "after": list(target[:3]),
                "source": target[3],
            }
        )
    return plan


def load_supplier_item_plan(connection: sqlite3.Connection) -> list[dict]:
    rows = connection.execute(
        """
        SELECT sroi.id, sroi.material_code_snapshot AS own_code,
               sroi.layer_count_snapshot AS own_layer,
               sroi.flute_type_snapshot AS own_flute,
               COALESCE(sroi.material_code_snapshot, m.code, soi.snapshot_material) AS row_code,
               COALESCE(sroi.layer_count_snapshot, sro.layer_count, soi.layer_count) AS product_layer,
               COALESCE(sroi.flute_type_snapshot, sro.flute_type, soi.flute_type) AS existing_flute,
               m.code AS material_code, m.layer_count AS material_layer,
               m.flute_type AS material_flute,
               p.product_code, p.default_material_code,
               p.layer_count AS current_product_layer,
               p.flute_type AS product_flute
        FROM supplier_requisition_order_items sroi
        JOIN supplier_requisition_orders sro ON sro.id = sroi.supplier_order_id
        LEFT JOIN sales_order_items soi ON soi.id = sroi.order_item_id
        LEFT JOIN products p ON p.id = COALESCE(sroi.product_id, soi.product_id)
        LEFT JOIN materials m
          ON m.id = COALESCE(sroi.material_id, soi.material_id, sro.material_id)
        ORDER BY sroi.id
        """
    ).fetchall()
    plan = []
    for row in rows:
        target = resolve_target(row)
        own = [row["own_code"], row["own_layer"], row["own_flute"]]
        if own == list(target[:3]):
            continue
        plan.append(
            {
                "table": "supplier_requisition_order_items",
                "id": row["id"],
                "product_code": row["product_code"],
                "before": own,
                "effective_before": [row["row_code"], row["product_layer"], row["existing_flute"]],
                "after": list(target[:3]),
                "source": target[3],
            }
        )
    return plan


def load_material_requisition_plan(
    connection: sqlite3.Connection, order_targets: dict[int, tuple[str, int, str]]
) -> list[dict]:
    rows = connection.execute(
        """
        SELECT mri.id, mri.material_snapshot, mri.order_item_id,
               soi.snapshot_material, soi.layer_count, soi.flute_type
        FROM material_requisition_items mri
        JOIN sales_order_items soi ON soi.id = mri.order_item_id
        ORDER BY mri.id
        """
    ).fetchall()
    plan = []
    for row in rows:
        target = order_targets.get(row["order_item_id"])
        if target is None:
            layer = row["layer_count"]
            code = canonical_code(row["snapshot_material"], layer)
            target = (code, layer, row["flute_type"])
        if row["material_snapshot"] != target[0]:
            plan.append(
                {
                    "table": "material_requisition_items",
                    "id": row["id"],
                    "before": row["material_snapshot"],
                    "after": target[0],
                }
            )
    return plan


def load_supplier_order_plan(
    connection: sqlite3.Connection, supplier_item_plan: list[dict]
) -> list[dict]:
    item_targets = {row["id"]: tuple(row["after"]) for row in supplier_item_plan}
    rows = connection.execute(
        """
        SELECT sro.id AS order_id, sro.order_number,
               sro.layer_count AS header_layer, sro.flute_type AS header_flute,
               sroi.id AS item_id,
               COALESCE(sroi.layer_count_snapshot, sro.layer_count,
                        soi.layer_count, m.layer_count) AS layer_count_snapshot,
               COALESCE(sroi.flute_type_snapshot, sro.flute_type,
                        soi.flute_type) AS flute_type_snapshot
        FROM supplier_requisition_orders sro
        LEFT JOIN supplier_requisition_order_items sroi
          ON sroi.supplier_order_id = sro.id
        LEFT JOIN sales_order_items soi ON soi.id = sroi.order_item_id
        LEFT JOIN materials m
          ON m.id = COALESCE(sroi.material_id, soi.material_id, sro.material_id)
        ORDER BY sro.id, sroi.id
        """
    ).fetchall()
    grouped: dict[int, dict] = {}
    for row in rows:
        group = grouped.setdefault(
            row["order_id"],
            {
                "order_number": row["order_number"],
                "before": [row["header_layer"], row["header_flute"]],
                "layers": set(),
                "flutes": set(),
            },
        )
        if row["item_id"] is None:
            continue
        target = item_targets.get(row["item_id"])
        layer = target[1] if target else row["layer_count_snapshot"]
        flute = target[2] if target else row["flute_type_snapshot"]
        if layer is not None:
            group["layers"].add(int(layer))
        if flute:
            group["flutes"].add(str(flute).strip().upper())
    plan = []
    for order_id, group in grouped.items():
        if len(group["layers"]) == 1 and len(group["flutes"]) == 1:
            layer = next(iter(group["layers"]))
            flute = next(iter(group["flutes"]))
            after = [layer, flute] if exact_flute(flute, layer) else [None, None]
        else:
            # Mixed supplier orders must rely on their exact item snapshots.
            after = [None, None]
        if group["before"] != after:
            plan.append(
                {
                    "table": "supplier_requisition_orders",
                    "id": order_id,
                    "order_number": group["order_number"],
                    "before": group["before"],
                    "after": after,
                }
            )
    return plan


def build_plan(connection: sqlite3.Connection) -> dict:
    connection.row_factory = sqlite3.Row
    order_items = load_order_item_plan(connection)
    order_targets = {row["id"]: tuple(row["after"]) for row in order_items}
    supplier_items = load_supplier_item_plan(connection)
    legacy_items = load_material_requisition_plan(connection, order_targets)
    supplier_orders = load_supplier_order_plan(connection, supplier_items)
    return {
        "sales_order_items": order_items,
        "material_requisition_items": legacy_items,
        "supplier_requisition_order_items": supplier_items,
        "supplier_requisition_orders": supplier_orders,
    }


def apply_plan(connection: sqlite3.Connection, plan: dict) -> None:
    for row in plan["sales_order_items"]:
        connection.execute(
            "UPDATE sales_order_items SET snapshot_material=?, layer_count=?, flute_type=? WHERE id=?",
            (*row["after"], row["id"]),
        )
    for row in plan["material_requisition_items"]:
        connection.execute(
            "UPDATE material_requisition_items SET material_snapshot=? WHERE id=?",
            (row["after"], row["id"]),
        )
    for row in plan["supplier_requisition_order_items"]:
        connection.execute(
            """
            UPDATE supplier_requisition_order_items
            SET material_code_snapshot=?, layer_count_snapshot=?, flute_type_snapshot=?
            WHERE id=?
            """,
            (*row["after"], row["id"]),
        )
    for row in plan["supplier_requisition_orders"]:
        connection.execute(
            "UPDATE supplier_requisition_orders SET layer_count=?, flute_type=? WHERE id=?",
            (*row["after"], row["id"]),
        )
    connection.execute(
        """
        INSERT INTO operation_logs
            (user_id, username, role, action, resource, entity_type,
             description, details, created_at)
        VALUES (1, 'admin', 'admin', 'historical_material_cleanup',
                'requisition_history', 'database_maintenance', ?, ?, ?)
        """,
        (
            "规范化历史订单与报料材质代码、层数和楞型",
            json.dumps(
                {key: len(value) for key, value in plan.items()},
                ensure_ascii=False,
            ),
            datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" "),
        ),
    )


def verify(connection: sqlite3.Connection) -> dict:
    remaining = build_plan(connection)
    return {
        "remaining": {key: len(value) for key, value in remaining.items()},
        "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_key_errors": len(connection.execute("PRAGMA foreign_key_check").fetchall()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    database = args.database.resolve()
    uri = database.as_uri() + ("?mode=rw" if args.apply else "?mode=ro")
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        plan = build_plan(connection)
        payload = {
            "database": str(database),
            "apply": args.apply,
            "counts": {key: len(value) for key, value in plan.items()},
            "changes": plan,
        }
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in payload.items() if key != "changes"}, ensure_ascii=False))
        if not args.apply:
            return 0
        if args.confirm != "CLEAN_HISTORICAL_REQUISITION_MATERIALS":
            raise SystemExit("缺少确认短语")
        connection.execute("BEGIN IMMEDIATE")
        apply_plan(connection, plan)
        verification = verify(connection)
        if any(verification["remaining"].values()):
            raise RuntimeError(f"清洗后仍有异常：{verification['remaining']}")
        if verification["integrity_check"] != "ok" or verification["foreign_key_errors"]:
            raise RuntimeError(f"数据库校验失败：{verification}")
        connection.commit()
        print(json.dumps({"applied": True, "verification": verification}, ensure_ascii=False))
        return 0
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
