from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePath
import re
import time
from typing import Final
from uuid import uuid4
import zipfile

from fastapi import UploadFile


CHUNK_SIZE: Final = 1024 * 1024
UPLOAD_READ_TIMEOUT_SECONDS: Final = 30
TEMP_TOKEN_TTL_SECONDS: Final = 15 * 60
TOKEN_PATTERN: Final = re.compile(r"^[a-f0-9]{32}$")
ACTIVE_EXTENSIONS: Final = {".html", ".htm", ".svg", ".xml", ".js"}


class UploadValidationError(ValueError):
    pass


class UploadTokenError(ValueError):
    pass


@dataclass(frozen=True)
class UploadPolicy:
    extensions: frozenset[str]
    max_bytes: int
    allowed_mime_types: frozenset[str]
    label: str


DRAWING_POLICY = UploadPolicy(
    extensions=frozenset({".jpg", ".jpeg", ".png", ".webp", ".pdf"}),
    max_bytes=20 * 1024 * 1024,
    allowed_mime_types=frozenset(
        {"image/jpeg", "image/png", "image/webp", "application/pdf"}
    ),
    label="图纸",
)
PDF_POLICY = UploadPolicy(
    extensions=frozenset({".pdf"}),
    max_bytes=30 * 1024 * 1024,
    allowed_mime_types=frozenset({"application/pdf"}),
    label="PDF",
)
EXCEL_POLICY = UploadPolicy(
    extensions=frozenset({".xlsx"}),
    max_bytes=20 * 1024 * 1024,
    allowed_mime_types=frozenset(
        {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
    ),
    label="Excel",
)
IMAGE_POLICY = UploadPolicy(
    extensions=frozenset({".jpg", ".jpeg", ".png"}),
    max_bytes=12 * 1024 * 1024,
    allowed_mime_types=frozenset({"image/jpeg", "image/png"}),
    label="图片",
)


@dataclass(frozen=True)
class ValidatedUpload:
    content: bytes
    original_filename: str
    extension: str
    content_type: str
    size: int
    sha256: str


@dataclass(frozen=True)
class StoredUpload:
    reference: str
    path: Path
    original_filename: str
    content_type: str
    size: int
    sha256: str


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def private_upload_root() -> Path:
    configured = os.getenv("ERP_FILE_STORAGE_DIR")
    root = Path(configured) if configured else _repo_root() / "data" / "private_uploads"
    return root.resolve()


def temporary_upload_root() -> Path:
    configured = os.getenv("ERP_UPLOAD_TEMP_DIR")
    root = Path(configured) if configured else private_upload_root() / "_temporary"
    return root.resolve()


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _safe_original_filename(filename: str | None) -> str:
    value = PurePath((filename or "upload").replace("\\", "/")).name
    value = "".join(character for character in value if ord(character) >= 32).strip()
    return value[:255] or "upload"


def _normalized_content_type(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def _detected_content_type(content: bytes, extension: str) -> str | None:
    if content.startswith(b"%PDF-"):
        return "application/pdf"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if extension == ".xlsx" and content.startswith(b"PK\x03\x04"):
        try:
            from io import BytesIO

            with zipfile.ZipFile(BytesIO(content)) as archive:
                names = set(archive.namelist())
            if "[Content_Types].xml" in names and "xl/workbook.xml" in names:
                return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        except (OSError, zipfile.BadZipFile):
            return None
    return None


def _validate_content(
    *,
    content: bytes,
    filename: str | None,
    content_type: str | None,
    policy: UploadPolicy,
) -> ValidatedUpload:
    original_filename = _safe_original_filename(filename)
    extension = Path(original_filename).suffix.lower()
    if extension in ACTIVE_EXTENSIONS or extension not in policy.extensions:
        allowed = "、".join(sorted(policy.extensions))
        raise UploadValidationError(f"{policy.label}文件类型不允许，仅支持 {allowed}")
    if not content:
        raise UploadValidationError(f"{policy.label}文件不能为空")
    if len(content) > policy.max_bytes:
        raise UploadValidationError(
            f"{policy.label}单文件不能超过 {policy.max_bytes // (1024 * 1024)}MB"
        )
    supplied_type = _normalized_content_type(content_type)
    if supplied_type not in policy.allowed_mime_types:
        raise UploadValidationError(f"{policy.label} MIME 类型不允许")
    detected_type = _detected_content_type(content, extension)
    if detected_type is None or detected_type not in policy.allowed_mime_types:
        raise UploadValidationError(f"{policy.label}文件签名无法识别")
    expected_by_extension = {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }[extension]
    if supplied_type != expected_by_extension or detected_type != expected_by_extension:
        raise UploadValidationError(f"{policy.label}扩展名、MIME 与文件签名不一致")
    return ValidatedUpload(
        content=content,
        original_filename=original_filename,
        extension=extension,
        content_type=detected_type,
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


async def read_validated_upload(
    upload: UploadFile,
    policy: UploadPolicy,
) -> ValidatedUpload:
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = await asyncio.wait_for(
                upload.read(CHUNK_SIZE), timeout=UPLOAD_READ_TIMEOUT_SECONDS
            )
        except TimeoutError as error:
            raise UploadValidationError(f"{policy.label}上传读取超时") from error
        if not chunk:
            break
        total += len(chunk)
        if total > policy.max_bytes:
            raise UploadValidationError(
                f"{policy.label}单文件不能超过 {policy.max_bytes // (1024 * 1024)}MB"
            )
        chunks.append(chunk)
    return _validate_content(
        content=b"".join(chunks),
        filename=upload.filename,
        content_type=upload.content_type,
        policy=policy,
    )


def _metadata_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.metadata.json")


def _write_metadata(path: Path, metadata: dict[str, object]) -> None:
    metadata_path = _metadata_path(path)
    temporary = metadata_path.with_name(f".{metadata_path.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(metadata, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    temporary.replace(metadata_path)


def store_private_upload(
    validated: ValidatedUpload,
    *,
    category: str,
    content: bytes | None = None,
    extension: str | None = None,
    content_type: str | None = None,
) -> StoredUpload:
    if not re.fullmatch(r"[a-z0-9_-]+", category):
        raise ValueError("invalid private upload category")
    root = private_upload_root()
    target_dir = (root / category).resolve()
    if not _is_within(target_dir, root):
        raise ValueError("private upload category escaped root")
    target_dir.mkdir(parents=True, exist_ok=True)
    payload = validated.content if content is None else content
    final_extension = extension or validated.extension
    final_content_type = content_type or validated.content_type
    filename = f"{uuid4().hex}{final_extension}"
    target = (target_dir / filename).resolve()
    if not _is_within(target, target_dir):
        raise ValueError("private upload path escaped category")
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    with temporary.open("xb") as handle:
        for offset in range(0, len(payload), CHUNK_SIZE):
            handle.write(payload[offset : offset + CHUNK_SIZE])
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(target)
    digest = hashlib.sha256(payload).hexdigest()
    metadata = {
        "original_filename": validated.original_filename,
        "content_type": final_content_type,
        "size": len(payload),
        "sha256": digest,
        "stored_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_metadata(target, metadata)
    reference = f"private:{category}/{filename}"
    return StoredUpload(
        reference=reference,
        path=target,
        original_filename=validated.original_filename,
        content_type=final_content_type,
        size=len(payload),
        sha256=digest,
    )


def cleanup_expired_temporary_uploads(*, now: float | None = None) -> int:
    root = temporary_upload_root()
    if not root.exists():
        return 0
    cutoff = (now if now is not None else time.time()) - TEMP_TOKEN_TTL_SECONDS
    removed = 0
    active_files: set[str] = set()
    current_time = now if now is not None else time.time()
    for metadata_path in root.glob("*.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            expired = float(metadata.get("expires_at", 0)) <= current_time
            stale = metadata_path.stat().st_mtime <= cutoff
            if not expired and not stale:
                active_files.add(str(metadata.get("stored_name", "")))
                continue
            stored_name = str(metadata.get("stored_name", ""))
            candidate = (root / stored_name).resolve()
            if stored_name and _is_within(candidate, root):
                candidate.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            removed += 1
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    # A process crash may leave a claimed token or a temporary payload without
    # metadata.  Only age-based cleanup is allowed; active token payloads stay.
    for candidate in root.iterdir():
        if (
            not candidate.is_file()
            or candidate.name in active_files
            or candidate.suffix == ".json"
            or candidate.stat().st_mtime > cutoff
        ):
            continue
        try:
            candidate.unlink(missing_ok=True)
            removed += 1
        except OSError:
            continue
    return removed


def create_temporary_token(validated: ValidatedUpload, *, owner_id: int) -> str:
    cleanup_expired_temporary_uploads()
    root = temporary_upload_root()
    root.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    stored_name = f"{uuid4().hex}{validated.extension}"
    target = (root / stored_name).resolve()
    if not _is_within(target, root):
        raise ValueError("temporary upload path escaped root")
    with target.open("xb") as handle:
        for offset in range(0, len(validated.content), CHUNK_SIZE):
            handle.write(validated.content[offset : offset + CHUNK_SIZE])
        handle.flush()
        os.fsync(handle.fileno())
    now = time.time()
    metadata = {
        "owner_id": owner_id,
        "stored_name": stored_name,
        "original_filename": validated.original_filename,
        "content_type": validated.content_type,
        "size": validated.size,
        "sha256": validated.sha256,
        "created_at": now,
        "expires_at": now + TEMP_TOKEN_TTL_SECONDS,
    }
    metadata_path = (root / f"{token}.json").resolve()
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    return token


def _load_token(token: str, *, owner_id: int) -> tuple[Path, Path, dict[str, object]]:
    if not TOKEN_PATTERN.fullmatch(token or ""):
        raise UploadTokenError("图纸上传 token 无效")
    root = temporary_upload_root()
    metadata_path = (root / f"{token}.json").resolve()
    if not _is_within(metadata_path, root) or not metadata_path.is_file():
        raise UploadTokenError("图纸上传 token 不存在、已使用或已过期")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise UploadTokenError("图纸上传 token 无法读取") from error
    if int(metadata.get("owner_id", -1)) != owner_id:
        raise UploadTokenError("图纸上传 token 不属于当前用户")
    if float(metadata.get("expires_at", 0)) <= time.time():
        cleanup_expired_temporary_uploads()
        raise UploadTokenError("图纸上传 token 已过期")
    stored_name = str(metadata.get("stored_name", ""))
    source = (root / stored_name).resolve()
    if not stored_name or not _is_within(source, root) or not source.is_file():
        raise UploadTokenError("图纸临时文件不存在")
    return metadata_path, source, metadata


def temporary_token_file(token: str, *, owner_id: int) -> StoredUpload:
    _, source, metadata = _load_token(token, owner_id=owner_id)
    return StoredUpload(
        reference=token,
        path=source,
        original_filename=str(metadata["original_filename"]),
        content_type=str(metadata["content_type"]),
        size=int(metadata["size"]),
        sha256=str(metadata["sha256"]),
    )


def consume_temporary_token(
    token: str,
    *,
    owner_id: int,
    category: str = "drawings",
) -> StoredUpload:
    metadata_path, source, metadata = _load_token(token, owner_id=owner_id)
    claimed = metadata_path.with_suffix(".claimed")
    try:
        metadata_path.replace(claimed)
    except OSError as error:
        raise UploadTokenError("图纸上传 token 已被使用") from error
    validated = ValidatedUpload(
        content=source.read_bytes(),
        original_filename=str(metadata["original_filename"]),
        extension=source.suffix.lower(),
        content_type=str(metadata["content_type"]),
        size=int(metadata["size"]),
        sha256=str(metadata["sha256"]),
    )
    if (
        len(validated.content) != validated.size
        or hashlib.sha256(validated.content).hexdigest() != validated.sha256
    ):
        claimed.replace(metadata_path)
        raise UploadTokenError("图纸临时文件校验失败")
    try:
        stored = store_private_upload(validated, category=category)
    except Exception:
        claimed.replace(metadata_path)
        raise
    source.unlink(missing_ok=True)
    claimed.unlink(missing_ok=True)
    return stored


def resolve_stored_reference(reference: str) -> Path:
    if reference.startswith("private:"):
        relative = reference.removeprefix("private:")
        root = private_upload_root()
        candidate = (root / relative).resolve()
        if not _is_within(candidate, root):
            raise FileNotFoundError("private reference escaped storage root")
        return candidate
    legacy_prefix = "/static/uploads/"
    if reference.startswith(legacy_prefix):
        root = (_repo_root() / "static" / "uploads").resolve()
        candidate = (root / reference.removeprefix(legacy_prefix)).resolve()
        if not _is_within(candidate, root):
            raise FileNotFoundError("legacy upload reference escaped storage root")
        return candidate
    raise FileNotFoundError("unsupported stored upload reference")


def remove_stored_reference(reference: str) -> list[str]:
    try:
        path = resolve_stored_reference(reference)
    except FileNotFoundError as error:
        return [str(error)]
    try:
        path.unlink(missing_ok=True)
        _metadata_path(path).unlink(missing_ok=True)
    except OSError as error:
        return [f"{reference}: {error}"]
    return []


def stored_file_metadata(path: Path) -> dict[str, object]:
    try:
        return json.loads(_metadata_path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def copy_legacy_to_private(reference: str, *, category: str = "drawings") -> StoredUpload:
    source = resolve_stored_reference(reference)
    content = source.read_bytes()
    extension = source.suffix.lower()
    detected = _detected_content_type(content, extension)
    if detected is None:
        raise UploadValidationError("历史图纸文件签名无法识别")
    validated = ValidatedUpload(
        content=content,
        original_filename=source.name,
        extension=extension,
        content_type=detected,
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    return store_private_upload(validated, category=category)
