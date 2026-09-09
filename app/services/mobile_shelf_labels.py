"""Read-only label identities. QR identities never grant access or reserve stock."""
import hashlib
import json
import re
from urllib.parse import urlsplit

from fastapi import HTTPException


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
    origin = urlsplit(browser_url)
    if origin.scheme not in ("https", "http") or not origin.netloc:
        raise HTTPException(409, "请配置手机访问地址")
    return f"{origin.scheme}://{origin.netloc}/q/{location_id}" + (f"/{key}" if key else "")


def product_fields(db, lot):
    from app.models.customer import Customer
    detail = lot.finished_detail or lot.semi_finished_detail
    customer = db.get(Customer, detail.owner_customer_id) if detail and detail.owner_customer_id else None
    return {
        "customer": (customer.chinese_short_name or customer.name) if customer else "通用库存",
        "code": getattr(detail, "inventory_code_snapshot", None) or "待补充",
        "name": getattr(detail, "product_name_snapshot", None) or "待补充",
        "specification": "×".join(format(value, "g") for value in
            (getattr(detail, field, None) for field in ("length_mm", "width_mm", "height_mm")) if value is not None),
    }
