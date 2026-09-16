"""Read-only label identities. QR identities never grant access or reserve stock."""
import hashlib
import json
import os
import re
from ipaddress import ip_address, ip_network
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

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
    browser_url = shelf_label_origin() or browser_url
    origin = urlsplit(browser_url)
    if origin.scheme not in ("https", "http") or not origin.netloc:
        raise HTTPException(409, "请配置手机访问地址")
    return f"{origin.scheme}://{origin.netloc}/q/{location_id}" + (f"/{key}" if key else "")


def shelf_label_origin():
    """Opt-in factory QR origin; never change the global remote ERP entrance."""
    value = os.getenv("ERP_SHELF_LABEL_ORIGIN", "").strip().rstrip("/")
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        address = ip_address(parsed.hostname or "")
        if (parsed.scheme != "http" or not parsed.port or parsed.username is not None
                or parsed.password is not None or parsed.path or parsed.query or parsed.fragment
                or not any(address in ip_network(net) for net in
                           ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))):
            raise ValueError()
    except ValueError:
        raise HTTPException(409, "货架扫码地址必须是明确的内网 HTTP IP 和端口")
    return value


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


def print_address(label):
    """Separate the published human area/rack names, never derive from internal codes."""
    title, position, _ = readable_address(label)
    parts = [p.strip() for p in re.split(r"[·・]", label.get("display_path", "")) if p.strip()]
    head = [p for p in parts if not re.fullmatch(r"\d+层|\d+格", p)]
    if label.get("level_no") and label.get("slot_no") and len(head) >= 3:
        floor, area, rack = head[0], head[1], head[2]
        rack = rack if rack.endswith("架") else rack + "架"
        return dict(print_title=f"{area}-{rack}", print_floor=floor,
                    print_position=position.replace("-", " "))
    return dict(print_title=title, print_floor="", print_position=position.replace("-", " "))


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
