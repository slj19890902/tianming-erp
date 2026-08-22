from __future__ import annotations

import base64
from io import BytesIO
from urllib.parse import urlencode, urlsplit, urlunsplit

import qrcode

from app.core.config import load_settings


def production_task_live_url(task_id: int) -> str:
    """Return the configured-origin mobile URL for one concrete task."""

    normalized_id = int(task_id)
    if normalized_id <= 0:
        raise ValueError("生产任务编号必须为正整数")
    configured = urlsplit(load_settings().browser_url)
    return urlunsplit(
        (
            configured.scheme,
            configured.netloc,
            "/mobile/",
            urlencode({"task": normalized_id}),
            "production",
        )
    )


def production_task_qr_payload(task_id: int) -> dict[str, object]:
    """Build a QR containing only one concrete production-task URL."""

    normalized_id = int(task_id)
    lookup_url = production_task_live_url(normalized_id)
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
        "production_task_id": normalized_id,
        "lookup_url": lookup_url,
        "qr_data_url": (
            "data:image/png;base64,"
            + base64.b64encode(buffer.getvalue()).decode("ascii")
        ),
        "error_correction": "M",
        "matrix_size": len(qr.get_matrix()),
    }
