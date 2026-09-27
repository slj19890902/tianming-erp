import pytest
from fastapi import HTTPException
from app.services.mobile_shelf_labels import mobile_url, legacy_scan_redirect, shelf_label_origin


def test_origin_opt_in(monkeypatch):
    monkeypatch.delenv('ERP_MOBILE_QR_ORIGIN', raising=False)
    monkeypatch.delenv('ERP_SHELF_LABEL_ORIGIN', raising=False)
    assert mobile_url('https://example.com/', 1203) == 'https://example.com/q/1203'
    assert legacy_scan_redirect('tianmingerp0909.share.zrok.io', 1203).endswith('/q/1203')


def test_new_label_and_old_redirect_keep_identity(monkeypatch):
    monkeypatch.setenv('ERP_SHELF_LABEL_ORIGIN', 'http://172.16.1.26:8000')
    key = 'a' * 24
    expected = 'http://172.16.1.26:8000/q/1203/' + key
    assert mobile_url('https://example.com/', 1203, key) == expected
    assert legacy_scan_redirect('tianmingerp0909.share.zrok.io', 1203, key) == expected
    for host in ['172.16.1.26', '192.168.3.80', 'example.com', 'tianmingerp0909.share.zrok.io.evil.test']:
        assert legacy_scan_redirect(host, 1203, key) is None


@pytest.mark.parametrize('origin', ['http://172.16.1.26', 'http://127.0.0.1:8000',
    'http://8.8.8.8:8000', 'http://user:pass@172.16.1.26:8000',
    'http://172.16.1.26:8000/?next=evil', 'http://172.16.1.26:8000/path',
    'http://172.16.1.26:8000/#x', 'javascript:alert(1)'])
def test_invalid_config_rejected(monkeypatch, origin):
    monkeypatch.setenv('ERP_SHELF_LABEL_ORIGIN', origin)
    with pytest.raises(HTTPException):
        shelf_label_origin()


def test_invalid_product_rejected(monkeypatch):
    monkeypatch.setenv('ERP_SHELF_LABEL_ORIGIN', 'http://172.16.1.26:8000')
    with pytest.raises(HTTPException):
        legacy_scan_redirect('tianmingerp0909.share.zrok.io', 1203, '../../login')
