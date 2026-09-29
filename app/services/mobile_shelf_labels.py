"""Read-only label identities. QR identities never grant access or reserve stock."""
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException


@lru_cache(maxsize=1)
def scan_page_html():
    """Bundle the small static scanner, avoiding one serial public-network trip.

    Only public shell/code is cached; no identity or inventory is embedded.
    A release restarts the worker, so this cache cannot survive a code upgrade.
    """
    root = Path(__file__).resolve().parents[2] / "static"
    shell = (root / "shelf-scan.html").read_text(encoding="utf-8")
    script = (root / "shelf-scan.js").read_text(encoding="utf-8")
    return shell.replace('<script src="/static/shelf-scan.js"></script>',
                         '<script>' + script.replace('</script', '<\\/script') + '</script>')


@lru_cache(maxsize=1)
def camera_page_html():
    root = Path(__file__).resolve().parents[2] / "static"
    shell = scan_page_html()
    panel = (root / "shelf-camera.html").read_text(encoding="utf-8")
    script = (root / "shelf-camera.js").read_text(encoding="utf-8")
    return shell.replace('<div id="message"', panel + '<div id="message"', 1).replace(
        '</body>', '<script>' + script.replace('</script', '<\\/script') + '</script></body>')


def product_key(lot):
    detail = lot.finished_detail or lot.semi_finished_detail
    if detail is None:
        return f"lot-{lot.id}"
    fields = ("owner_customer_id", "product_id", "inventory_code_snapshot",
              "length_mm", "width_mm", "height_mm", "material_code_snapshot", "flute_type_snapshot")
    identity = [lot.inventory_type, lot.unit] + [getattr(detail, field, None) for field in fields]
    return hashlib.sha256(json.dumps(identity, default=str, ensure_ascii=False).encode()).hexdigest()[:24]


def readable_address(label):
    # The published employee path carries the real rack name, not internal rack A.
    path = label.get("display_path") or label.get("employee_location_name") or ""
    if re.search(r"EDIT[-_]\d+|\b\dF-|待完善|待确认", path, re.I):
        raise HTTPException(409, "请先补充货位中文名称")
    parts = [part.strip() for part in re.split(r"[·・]", path) if part.strip()]
    if not parts:
        raise HTTPException(409, "请先补充货位中文名称")
    level, slot = label.get("level_no"), label.get("slot_no")
    if level and slot:
        head = [p for p in parts if not re.fullmatch(r"\d+层|\d+格", p)]
        # e.g. 三楼·北货架G1·G1 -> 三楼 北货架G1
        if len(head) >= 3 and head[-2].endswith(head[-1]):
            head.pop()
        title = " ".join(head)
        position = f"{int(level):02d}层-{int(slot):02d}格"
        return title, position, f"{title} -{position}"
    return path, "", path


def mobile_url(browser_url, location_id, key=None):
    from app.services.mobile_qr import location_mobile_url

    return location_mobile_url(location_id, key, origin=browser_url)


def shelf_label_origin():
    """Backward-compatible alias for the unified mobile QR origin."""
    from app.services.mobile_qr import mobile_qr_origin

    return mobile_qr_origin()


def legacy_scan_redirect(hostname, location_id, key=None):
    # No query/auth tokens forwarded, and no user-controlled redirect target.
    if hostname != "tianmingerp0909.share.zrok.io":
        return None
    origin = shelf_label_origin()
    if not origin:
        return None
    if location_id <= 0 or (key and not re.fullmatch(r"[a-f0-9]{24}", key)):
        raise HTTPException(400, "二维码无效")
    return mobile_url(origin, location_id, key)


def compact_rack_title(value):
    """Keep the published rack name, removing only a code-style trailing 架."""
    title = str(value or "").strip()
    if re.search(r"[A-Za-z0-9]架$", title):
        return title[:-1]
    return title


def print_address(label):
    """Separate the published human area/rack names, never derive from internal codes."""
    title, position, _ = readable_address(label)
    parts = [p.strip() for p in re.split(r"[·・]", label.get("display_path", "")) if p.strip()]
    head = [p for p in parts if not re.fullmatch(r"\d+层|\d+格", p)]
    if label.get("level_no") and label.get("slot_no") and len(head) >= 3:
        floor, area, rack = head[0], head[1], head[2]
        print_rack = rack if rack.endswith("架") else rack + "架"
        return dict(print_title=f"{area}-{print_rack}", print_floor=floor,
                    print_position=position,
                    compact_title=compact_rack_title(rack),
                    compact_position=f"{int(label['level_no'])}层-{int(label['slot_no'])}格")
    return dict(print_title=title, print_floor="", print_position=position,
                compact_title=title, compact_position=position)


def product_fields(db, lot):
    from app.services.bom_inventory_contract import display_name
    from app.models.customer import Customer
    detail = lot.finished_detail or lot.semi_finished_detail
    customer = db.get(Customer, detail.owner_customer_id) if detail and detail.owner_customer_id else None
    return {
        "customer": (customer.chinese_short_name or customer.name) if customer else "通用库存",
        "customer_name": customer.name if customer else "通用库存",
        "code": getattr(detail, "inventory_code_snapshot", None) or "待补充",
        "name": display_name(lot, getattr(detail, "product_name_snapshot", None) or "待补充"),
        "specification": "×".join(format(value, "g") for value in
            (getattr(detail, field, None) for field in ("length_mm", "width_mm", "height_mm")) if value is not None),
    }
