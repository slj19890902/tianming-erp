from urllib.parse import parse_qs, urlsplit
from pathlib import Path
import base64

import pytest
from fastapi import HTTPException

from app.services.mobile_qr import (
    incoming_mobile_url,
    location_mobile_url,
    lot_mobile_url,
    mobile_absolute_url,
    mobile_qr_origin,
    mold_mobile_url,
    product_mobile_url,
    production_task_mobile_url,
    rack_mobile_url,
    qr_data_url,
)


def _decode_qr(data_url: str) -> str:
    import cv2
    import numpy as np

    image = cv2.imdecode(
        np.frombuffer(base64.b64decode(data_url.split(",", 1)[1]), dtype=np.uint8),
        cv2.IMREAD_GRAYSCALE,
    )
    detector = cv2.QRCodeDetector()
    for scale in (1.0, 0.5, 2.0):
        candidate = image if scale == 1.0 else cv2.resize(
            image, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST
        )
        decoded, _points, _straight = detector.detectAndDecode(candidate)
        if decoded:
            return decoded
    return ""


def test_all_static_object_urls_use_one_configured_origin(monkeypatch):
    monkeypatch.setenv("ERP_MOBILE_QR_ORIGIN", "https://mobile.erp.example:9443/")
    urls = [
        location_mobile_url(12),
        location_mobile_url(12, "a" * 24),
        rack_mobile_url("3f", "rack-A"),
        lot_mobile_url(21),
        product_mobile_url(31),
        mold_mobile_url(41),
        production_task_mobile_url(51),
        incoming_mobile_url(),
        mobile_absolute_url("/mobile/tianhua-pick", query={"token": "safe-token"}),
    ]
    assert {f"{urlsplit(url).scheme}://{urlsplit(url).netloc}" for url in urls} == {
        "https://mobile.erp.example:9443"
    }
    assert urlsplit(urls[2]).path == "/scan/rack"
    assert parse_qs(urlsplit(urls[2]).query) == {"floor": ["3F"], "rack_id": ["rack-A"]}
    assert urlsplit(urls[6]).fragment == "production"
    assert urlsplit(urls[7]).fragment == "incoming"
    assert [_decode_qr(qr_data_url(url)) for url in urls] == urls


@pytest.mark.parametrize(
    "origin",
    [
        "javascript:alert(1)",
        "https://user:secret@example.test",
        "https://example.test/path",
        "//example.test",
    ],
)
def test_invalid_mobile_origin_is_rejected(monkeypatch, origin):
    monkeypatch.setenv("ERP_MOBILE_QR_ORIGIN", origin)
    with pytest.raises(HTTPException):
        mobile_qr_origin()


def test_mobile_paths_reject_untrusted_identity_text(monkeypatch):
    monkeypatch.setenv("ERP_MOBILE_QR_ORIGIN", "https://mobile.erp.example")
    with pytest.raises(ValueError):
        rack_mobile_url("3F", "../../admin")
    with pytest.raises(ValueError):
        location_mobile_url(1, "bad")
    with pytest.raises(ValueError):
        mobile_absolute_url("//evil.example/path")


def test_login_return_and_legacy_mobile_redirects_are_allowlisted():
    root = Path(__file__).resolve().parents[1]
    desktop = (root / "static" / "index.html").read_text(encoding="utf-8")
    mobile = (root / "static" / "mobile_erp.html").read_text(encoding="utf-8")
    product = (root / "static" / "mobile_product_live.html").read_text(encoding="utf-8")
    finished = (root / "static" / "finished-goods-label.html").read_text(encoding="utf-8")
    assert 'target.origin !== window.location.origin' in desktop
    assert r'^\/(?:P|M|I)\/[1-9]\d*\/?$' in desktop
    assert 'redirect: `${window.location.pathname}${window.location.search}${window.location.hash}`' in mobile
    assert 'location.pathname+location.search+location.hash' in product
    assert 'location.replace(`/I/${encodeURIComponent(lotId)}`)' in finished
