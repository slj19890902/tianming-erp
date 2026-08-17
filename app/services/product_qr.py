from __future__ import annotations

import base64
from io import BytesIO
from urllib.parse import urlsplit, urlunsplit

import qrcode

from app.core.config import load_settings


def product_live_url(product_id: int) -> str:
    """Return the stable configured-origin URL for one formal product identity."""

    normalized_id = int(product_id)
    if normalized_id <= 0:
        raise ValueError("正式产品编号必须为正整数")
    configured = urlsplit(load_settings().browser_url)
    return urlunsplit(
        (
            configured.scheme,
            configured.netloc,
            f"/P/{normalized_id}",
            "",
            "",
        )
    )


def product_qr_payload(product_id: int) -> dict[str, object]:
    """Build a QR image whose contents are only the stable product URL."""

    lookup_url = product_live_url(product_id)
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=4,
        border=4,
    )
    qr.add_data(lookup_url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return {
        "product_id": int(product_id),
        "lookup_url": lookup_url,
        "qr_data_url": (
            "data:image/png;base64,"
            + base64.b64encode(buffer.getvalue()).decode("ascii")
        ),
        "error_correction": "M",
        "matrix_size": len(qr.get_matrix()),
    }
