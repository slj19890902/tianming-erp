"""Stable, permission-neutral mobile QR identities.

The payload only identifies an ERP object.  Authentication, customer scope and
write permissions remain the responsibility of the destination endpoints.
"""
from __future__ import annotations

import base64
import os
import re
from ipaddress import ip_address, ip_network
from io import BytesIO
from urllib.parse import urlencode, urlsplit, urlunsplit

import qrcode
from fastapi import HTTPException

from app.core.config import load_settings


def _normalized_origin(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(status_code=409, detail="请配置有效的手机二维码访问地址")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, "", "", ""))


def mobile_qr_origin(configured_default: str | None = None) -> str:
    """Return the configured origin; never infer it from a request or network card."""

    mobile_origin = os.getenv("ERP_MOBILE_QR_ORIGIN", "").strip()
    shelf_origin = os.getenv("ERP_SHELF_LABEL_ORIGIN", "").strip()
    configured = mobile_origin or shelf_origin or str(configured_default or "").strip() or load_settings().browser_url
    if shelf_origin and not mobile_origin:
        parsed = urlsplit(shelf_origin)
        try:
            address = ip_address(parsed.hostname or "")
            private = any(
                address in ip_network(network)
                for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail="货架扫码地址必须是明确的内网 HTTP IP 和端口") from error
        if parsed.scheme != "http" or not parsed.port or not private:
            raise HTTPException(status_code=409, detail="货架扫码地址必须是明确的内网 HTTP IP 和端口")
    return _normalized_origin(configured)


def mobile_absolute_url(path: str, *, query: dict[str, object] | None = None, fragment: str = "", origin: str | None = None) -> str:
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("手机二维码路径必须是站内绝对路径")
    return urlunsplit(
        (
            *urlsplit(mobile_qr_origin(origin))[:2],
            path,
            urlencode({key: value for key, value in (query or {}).items() if value is not None}),
            fragment,
        )
    )


def location_mobile_url(location_id: int, product_key: str | None = None, *, origin: str | None = None) -> str:
    location_id = int(location_id)
    if location_id <= 0 or (product_key and not re.fullmatch(r"[a-f0-9]{24}", product_key)):
        raise ValueError("货位二维码身份无效")
    suffix = f"/{product_key}" if product_key else ""
    return mobile_absolute_url(f"/q/{location_id}{suffix}", origin=origin)


def rack_mobile_url(floor_code: str, rack_id: str, *, origin: str | None = None) -> str:
    floor_code = str(floor_code or "").strip().upper()
    rack_id = str(rack_id or "").strip()
    if not re.fullmatch(r"\d{1,2}F", floor_code) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", rack_id):
        raise ValueError("货架二维码身份无效")
    return mobile_absolute_url("/scan/rack", query={"floor": floor_code, "rack_id": rack_id}, origin=origin)


def lot_mobile_url(lot_id: int, *, origin: str | None = None) -> str:
    lot_id = int(lot_id)
    if lot_id <= 0:
        raise ValueError("库存批次二维码身份无效")
    return mobile_absolute_url(f"/I/{lot_id}", origin=origin)


def product_mobile_url(product_id: int, *, origin: str | None = None) -> str:
    product_id = int(product_id)
    if product_id <= 0:
        raise ValueError("产品二维码身份无效")
    return mobile_absolute_url(f"/P/{product_id}", origin=origin)


def mold_mobile_url(mold_id: int, *, origin: str | None = None) -> str:
    mold_id = int(mold_id)
    if mold_id <= 0:
        raise ValueError("模具二维码身份无效")
    return mobile_absolute_url(f"/M/{mold_id}", origin=origin)


def production_task_mobile_url(task_id: int) -> str:
    task_id = int(task_id)
    if task_id <= 0:
        raise ValueError("生产任务二维码身份无效")
    return mobile_absolute_url(
        "/mobile/", query={"mobile_page": "production", "task_id": task_id}, fragment="production"
    )


def incoming_mobile_url() -> str:
    return mobile_absolute_url("/mobile/", query={"mobile_page": "incoming"}, fragment="incoming")


def qr_data_url(value: str, *, error_correction: int = qrcode.constants.ERROR_CORRECT_M, box_size: int = 10, border: int = 4) -> str:
    qr = qrcode.QRCode(
        version=None,
        error_correction=error_correction,
        box_size=box_size,
        border=border,
    )
    qr.add_data(value)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
