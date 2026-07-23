from __future__ import annotations

import asyncio
from io import BytesIO
import json
from pathlib import Path
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.staticfiles import StaticFiles
from PIL import Image
import pytest
from starlette.datastructures import Headers, UploadFile

from app.api.orders import OrderItemCreate
from app.middleware.private_uploads import PrivateUploadGuardMiddleware
from app.services.secure_uploads import (
    DRAWING_POLICY,
    EXCEL_POLICY,
    UploadPolicy,
    UploadTokenError,
    UploadValidationError,
    cleanup_expired_temporary_uploads,
    consume_temporary_token,
    create_temporary_token,
    read_validated_upload,
    temporary_token_file,
)


def _png_bytes() -> bytes:
    image = Image.new("RGB", (24, 24), "white")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _upload(filename: str, content: bytes, content_type: str) -> UploadFile:
    return UploadFile(
        filename=filename,
        file=BytesIO(content),
        headers=Headers({"content-type": content_type}),
    )


def _read(filename: str, content: bytes, content_type: str, policy=DRAWING_POLICY):
    return asyncio.run(
        read_validated_upload(_upload(filename, content, content_type), policy)
    )


def test_extension_mime_and_signature_must_all_match() -> None:
    validated = _read("drawing.png", _png_bytes(), "image/png")
    assert validated.extension == ".png"
    assert validated.content_type == "image/png"

    with pytest.raises(UploadValidationError, match="签名"):
        _read("fake.png", b"<html>active</html>", "image/png")
    with pytest.raises(UploadValidationError, match="类型不允许"):
        _read("active.svg", b"<svg></svg>", "image/svg+xml")
    with pytest.raises(UploadValidationError, match="不一致"):
        _read("renamed.jpg", _png_bytes(), "image/jpeg")


def test_xlsx_signature_requires_real_office_zip_members() -> None:
    valid = BytesIO()
    with zipfile.ZipFile(valid, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("xl/workbook.xml", "<workbook />")
    validated = _read(
        "orders.xlsx",
        valid.getvalue(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        EXCEL_POLICY,
    )
    assert validated.extension == ".xlsx"

    with pytest.raises(UploadValidationError, match="签名"):
        _read(
            "orders.xlsx",
            b"PK\x03\x04not-an-office-workbook",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            EXCEL_POLICY,
        )


def test_streamed_reader_stops_at_policy_limit() -> None:
    tiny_policy = UploadPolicy(
        extensions=frozenset({".png"}),
        max_bytes=16,
        allowed_mime_types=frozenset({"image/png"}),
        label="测试图片",
    )
    with pytest.raises(UploadValidationError, match="不能超过"):
        _read("large.png", _png_bytes(), "image/png", tiny_policy)


def test_draft_token_is_owner_bound_one_time_and_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_root = tmp_path / "private"
    token_root = tmp_path / "tokens"
    monkeypatch.setenv("ERP_FILE_STORAGE_DIR", str(private_root))
    monkeypatch.setenv("ERP_UPLOAD_TEMP_DIR", str(token_root))
    validated = _read("客户原图.png", _png_bytes(), "image/png")
    token = create_temporary_token(validated, owner_id=7)

    assert set(token) <= set("0123456789abcdef")
    assert len(token) == 32
    assert temporary_token_file(token, owner_id=7).path.parent == token_root.resolve()
    with pytest.raises(UploadTokenError, match="不属于当前用户"):
        temporary_token_file(token, owner_id=8)

    stored = consume_temporary_token(token, owner_id=7)
    assert stored.reference.startswith("private:drawings/")
    assert stored.path.is_relative_to(private_root.resolve())
    metadata = json.loads(
        stored.path.with_name(f"{stored.path.name}.metadata.json").read_text(
            encoding="utf-8"
        )
    )
    assert metadata["original_filename"] == "客户原图.png"
    with pytest.raises(UploadTokenError, match="已使用或已过期"):
        consume_temporary_token(token, owner_id=7)


def test_order_payload_rejects_legacy_client_path() -> None:
    with pytest.raises(ValueError, match="temp_drawing_file 已停用"):
        OrderItemCreate.model_validate(
            {
                "product_id": 1,
                "quantity": 1,
                "unit_price": "1.00",
                "temp_drawing_file": "C:/Windows/win.ini",
            }
        )


def test_expired_temporary_upload_is_cleaned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_root = tmp_path / "tokens"
    monkeypatch.setenv("ERP_FILE_STORAGE_DIR", str(tmp_path / "private"))
    monkeypatch.setenv("ERP_UPLOAD_TEMP_DIR", str(token_root))
    token = create_temporary_token(
        _read("drawing.png", _png_bytes(), "image/png"), owner_id=1
    )
    metadata_path = token_root / f"{token}.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload_path = token_root / metadata["stored_name"]
    metadata["expires_at"] = 0
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    assert cleanup_expired_temporary_uploads() == 1
    assert not metadata_path.exists()
    assert not payload_path.exists()


def test_generic_static_mount_cannot_publish_uploads(tmp_path: Path) -> None:
    static_root = tmp_path / "static"
    upload_root = static_root / "uploads"
    upload_root.mkdir(parents=True)
    (upload_root / "secret.pdf").write_bytes(b"secret")
    (static_root / "asset.js").write_text("ok", encoding="utf-8")

    app = FastAPI()
    app.mount("/static", StaticFiles(directory=static_root), name="static")
    app.add_middleware(PrivateUploadGuardMiddleware)
    with TestClient(app) as client:
        assert client.get("/static/asset.js").status_code == 200
        blocked = client.get("/static/uploads/secret.pdf")
    assert blocked.status_code == 404
    assert blocked.json()["detail"] == "文件不存在"
