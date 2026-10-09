import base64
import hashlib
from io import BytesIO
import os
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import select, func

from tests.test_p1_35a_contract_pdf import contract_pdf_app, _login, _pdf_text


def _test_image():
    image = Image.new("RGBA", (400, 400), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, 392, 392), outline="red", width=12)
    draw.text((66, 174), "TEST - VOID", fill="red", font_size=42)
    output = BytesIO(); image.save(output, "PNG")
    return output.getvalue()


def _upload(client, version=0):
    return client.post("/api/contracts/seal/settings", json=dict(expected_version=version,
        image_base64=base64.b64encode(_test_image()).decode(), size_mm=40))


def _export(client, cid, version=3, seal_version=1, key="seal-test-operation-001"):
    return client.post(f"/api/contracts/seal/{cid}/pdf", json=dict(expected_version=version,
        seal_version=seal_version, operation_key=key))


def test_admin_opt_in_export_and_replay(contract_pdf_app):
    from app.models.contract_seal import ContractSealedExport
    from app.models.customer_contract import CustomerContract
    client, factory, ids = contract_pdf_app
    _login(client, "p135a-admin")
    assert client.get("/api/contracts/seal/settings").json()["available"] is False
    assert _export(client, ids["own"]).status_code == 409
    uploaded = _upload(client)
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["version"] == 1
    image = client.get("/api/contracts/seal/image?version=1")
    assert image.status_code == 200 and "no-store" in image.headers["cache-control"]
    before = client.get(f'/api/contracts/{ids["own"]}').json()
    unsigned = client.get(f'/api/contracts/{ids["own"]}/pdf?expected_version=3')
    signed = _export(client, ids["own"])
    assert signed.status_code == 200, signed.text
    assert signed.headers["x-contract-pdf-sha256"] == hashlib.sha256(signed.content).hexdigest()
    assert "seal-v1" in signed.headers["x-contract-pdf-template-version"]
    reader, pages = _pdf_text(signed.content)
    assert "甲方已盖章" in "".join(pages) and "未盖章" not in "".join(pages)
    assert "未盖章" in "".join(_pdf_text(unsigned.content)[1])
    assert len(reader.pages[-1].images) == 1
    replay = _export(client, ids["own"])
    assert replay.content == signed.content
    assert _export(client, ids["own"], version=99).status_code == 409
    assert client.get(f'/api/contracts/{ids["own"]}').json() == before
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(ContractSealedExport)) == 1
        assert db.get(CustomerContract, ids["own"]).status == "draft"
    assert client.get(f'/api/contracts/{ids["own"]}/pdf?expected_version=3').content == unsigned.content
    target = os.getenv("ERP_SEAL_EVIDENCE")
    if target:
        Path(target).mkdir(parents=True, exist_ok=True)
        (Path(target)/"fictional-sealed.pdf").write_bytes(signed.content)
        (Path(target)/"fictional-seal.png").write_bytes(_test_image())


@pytest.mark.parametrize("username", ["p135a-scoped", "p135a-denied"])
def test_non_admin_cannot_read_asset_manage_or_export(contract_pdf_app, username):
    client, _, ids = contract_pdf_app
    _login(client, "p135a-admin"); assert _upload(client).status_code == 200
    _login(client, username)
    assert client.get("/api/contracts/seal/settings").status_code == 403
    assert client.get("/api/contracts/seal/image?version=1").status_code == 403
    assert _upload(client, 1).status_code == 403
    assert client.post("/api/contracts/seal/disable", json={"expected_version": 1}).status_code == 403
    assert _export(client, ids["own"]).status_code == 403


def test_versions_disable_and_long_document(contract_pdf_app):
    client, _, ids = contract_pdf_app
    _login(client, "p135a-admin")
    assert _upload(client).status_code == 200
    assert _upload(client).status_code == 409
    assert _export(client, ids["long"], version=4).status_code == 409
    result = _export(client, ids["long"], version=5)
    assert result.status_code == 200, result.text
    reader, pages = _pdf_text(result.content)
    assert len(reader.pages) > 2
    assert len(reader.pages[-1].images) == 1
    assert all(len(page.images) == 0 for page in reader.pages[:-1])
    assert "签署日期" in pages[-1] and "乙方（需方）" in pages[-1]
    assert client.post("/api/contracts/seal/disable", json={"expected_version":1}).status_code == 200
    assert client.get("/api/contracts/seal/image?version=1").status_code == 409
    assert _export(client, ids["own"], seal_version=2, key="seal-test-after-disable").status_code == 409
    assert _export(client, ids["long"], version=5).content == result.content
    if os.getenv("ERP_SEAL_EVIDENCE"):
        (Path(os.environ["ERP_SEAL_EVIDENCE"])/"fictional-long-sealed.pdf").write_bytes(result.content)


@pytest.mark.parametrize("data", [b"not an image", b"<svg><script/></svg>", b"%PDF-1.7", b""])
def test_invalid_upload_does_not_change_state(contract_pdf_app, data):
    client, _, _ = contract_pdf_app
    _login(client, "p135a-admin")
    response = client.post("/api/contracts/seal/settings", json=dict(expected_version=0,
        image_base64=base64.b64encode(data).decode(), size_mm=40))
    assert response.status_code == 422
    assert client.get("/api/contracts/seal/settings").json()["version"] == 0


def test_audit_failure_rolls_back_asset_and_pdf(contract_pdf_app, monkeypatch):
    from app.api import contract_seals
    from app.models.contract_seal import ContractSeal, ContractSealState, ContractSealedExport
    client, factory, ids = contract_pdf_app
    _login(client, "p135a-admin")
    audit = contract_seals._audit
    def fail(*args, **kwargs):
        raise RuntimeError("injected audit failure")
    monkeypatch.setattr(contract_seals, "_audit", fail)
    with pytest.raises(Exception, match="injected audit failure"):
        _upload(client)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(ContractSeal)) == 0
        assert db.get(ContractSealState, 1) is None
    monkeypatch.setattr(contract_seals, "_audit", audit)
    assert _upload(client).status_code == 200
    monkeypatch.setattr(contract_seals, "_audit", fail)
    with pytest.raises(Exception, match="injected audit failure"):
        _export(client, ids["own"])
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(ContractSealedExport)) == 0


def test_seal_ui_is_explicit_admin_opt_in():
    source = (Path(__file__).parents[1]/"static/index.html").read_text(encoding="utf-8")
    assert '@click="exportContractPdf(contractDraft)"' in source
    assert '@click="exportContractPdf(contractDraft,true)"' in source
    assert 'async exportContractPdf(row,sealed=false)' in source
    assert 'if (this.user?.role!=="admin") throw' in source
