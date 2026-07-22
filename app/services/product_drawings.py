from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import warnings
from uuid import uuid4

import jwt
from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, StreamObject

from app.core.config import load_settings


ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
PDF_CONTENT_TYPE = "application/pdf"
ALLOWED_DRAWING_TYPES = {*ALLOWED_IMAGE_TYPES, PDF_CONTENT_TYPE}
MAX_DRAWING_BYTES = 20 * 1024 * 1024
MAX_DRAWING_IMAGE_SIDE = 16_384
MAX_DRAWING_IMAGE_PIXELS = 25_000_000
DRAWING_URL_PREFIX = "/static/uploads/drawings"
ORDER_DRAFT_URL_PREFIX = "/static/uploads/order_drafts"
PRIVATE_STORAGE_ROOT = Path(__file__).resolve().parents[2] / "data" / "private"
LEGACY_DRAWING_ROOT = (
    Path(__file__).resolve().parents[2] / "static" / "uploads" / "drawings"
)
_DRAFT_TOKEN_TYPE = "order_drawing_draft"
_DRAFT_FILENAME = re.compile(r"draft_[0-9a-f]{32}\.(?:pdf|webp)")
_SAFE_DRAWING_FILENAME = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}\.(?:jpe?g|png|webp|pdf)",
    re.IGNORECASE,
)
_WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_BROAD_WINDOWS_SIDS = frozenset(
    {
        "AN",
        "AU",
        "BG",
        "BU",
        "IU",
        "WD",
        "S-1-1-0",  # Everyone
        "S-1-5-4",  # Interactive
        "S-1-5-7",  # Anonymous
        "S-1-5-11",  # Authenticated Users
        "S-1-5-32-545",  # BUILTIN\Users
        "S-1-5-32-546",  # BUILTIN\Guests
    }
)
_READ_ONLY_SDDL_RIGHTS = frozenset({"FR", "FX", "GR", "GX", "RC"})
_SDDL_ALLOW_ACE = re.compile(
    r"\((?P<type>A|OA);(?P<flags>[^;]*);(?P<rights>[^;]*);[^;]*;[^;]*;(?P<sid>[^;)]+)\)"
)
_IMAGE_FORMAT_BY_CONTENT_TYPE = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
}
_PDF_DISALLOWED_KEYS = {
    "/AA",
    "/AcroForm",
    "/AF",
    "/EF",
    "/EmbeddedFiles",
    "/JavaScript",
    "/JS",
    "/OpenAction",
    "/RichMedia",
    "/XFA",
}
# Static drawings may still contain ordinary page content and internal page
# destinations. Reject actions that execute code, leave the document, mutate
# forms/visibility, or start interactive and multimedia behavior.
_PDF_DISALLOWED_ACTIONS = {
    "/GoTo3DView",
    "/GoToE",
    "/GoToR",
    "/Hide",
    "/ImportData",
    "/JavaScript",
    "/Launch",
    "/Movie",
    "/Named",
    "/Rendition",
    "/ResetForm",
    "/SetOCGState",
    "/Sound",
    "/SubmitForm",
    "/Thread",
    "/Trans",
    "/URI",
}
_PDF_DISALLOWED_ANNOTATION_SUBTYPES = {
    "/3D",
    "/FileAttachment",
    "/Movie",
    "/Projection",
    "/RichMedia",
    "/Screen",
    "/Sound",
    "/Widget",
}
_PDF_MAX_GRAPH_OBJECTS = 50_000


class DrawingValidationError(ValueError):
    pass


@dataclass(frozen=True)
class SavedDrawing:
    image_path: str
    thumbnail_path: str


@dataclass(frozen=True)
class ResolvedDraftDrawing:
    path: Path
    content_type: str
    content_sha256: str


@dataclass(frozen=True)
class LoadedDrawing:
    filename: str
    content: bytes
    content_type: str


def drawing_dir() -> Path:
    configured = os.getenv("ERP_DRAWING_DIR")
    if configured:
        return Path(configured)
    return PRIVATE_STORAGE_ROOT / "drawings"


def order_draft_drawing_dir() -> Path:
    configured = os.getenv("ERP_ORDER_DRAFT_DRAWING_DIR")
    if configured:
        return Path(configured)
    return PRIVATE_STORAGE_ROOT / "order_drafts"


def legacy_drawing_dir() -> Path:
    configured = os.getenv("ERP_LEGACY_DRAWING_DIR")
    if configured:
        return Path(configured)
    return LEGACY_DRAWING_ROOT


def _absolute_without_resolving(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = Path(__file__).resolve().parents[2] / expanded
    return Path(os.path.abspath(expanded))


def _is_windows_reparse_point(file_stat: os.stat_result) -> bool:
    return bool(
        getattr(file_stat, "st_file_attributes", 0)
        & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
    )


def _existing_ancestor_chain(path: Path) -> list[Path]:
    absolute = _absolute_without_resolving(path)
    chain = [absolute, *absolute.parents]
    return [
        candidate
        for candidate in reversed(chain)
        if candidate.exists() or candidate.is_symlink()
    ]


def assert_no_reparse_ancestors(path: Path) -> Path:
    """Return an absolute path only after checking every existing ancestor.

    ``Path.resolve`` is intentionally not used before the checks: resolving a
    Windows junction first would hide the reparse point that must be rejected.
    """
    absolute = _absolute_without_resolving(path)
    for candidate in _existing_ancestor_chain(absolute):
        try:
            candidate_stat = candidate.lstat()
        except OSError as error:
            raise DrawingValidationError("图纸存储路径无法安全检查") from error
        if candidate.is_symlink() or _is_windows_reparse_point(candidate_stat):
            raise DrawingValidationError("图纸存储路径包含符号链接或联接点")
    return absolute


def _sddl_rights_include_write(rights: str) -> bool:
    if not rights:
        return False
    if rights.lower().startswith("0x"):
        try:
            access_mask = int(rights, 16)
        except ValueError:
            return True
        write_mask = (
            0x0002  # FILE_WRITE_DATA
            | 0x0004  # FILE_APPEND_DATA
            | 0x0010  # FILE_WRITE_EA
            | 0x0040  # FILE_DELETE_CHILD
            | 0x0100  # FILE_WRITE_ATTRIBUTES
            | 0x00010000  # DELETE
            | 0x00040000  # WRITE_DAC
            | 0x00080000  # WRITE_OWNER
            | 0x10000000  # GENERIC_ALL
            | 0x40000000  # GENERIC_WRITE
        )
        return bool(access_mask & write_mask)
    if len(rights) % 2:
        return True
    tokens = {rights[index : index + 2] for index in range(0, len(rights), 2)}
    return bool(tokens - _READ_ONLY_SDDL_RIGHTS)


def _sddl_rights_allow_boundary_replacement(rights: str) -> bool:
    """Return whether rights can delete/replace or take over an existing child."""
    if not rights:
        return False
    if rights.lower().startswith("0x"):
        try:
            access_mask = int(rights, 16)
        except ValueError:
            return True
        boundary_mask = (
            0x0040  # FILE_DELETE_CHILD
            | 0x00010000  # DELETE
            | 0x00040000  # WRITE_DAC
            | 0x00080000  # WRITE_OWNER
            | 0x10000000  # GENERIC_ALL
        )
        return bool(access_mask & boundary_mask)
    if len(rights) % 2:
        return True
    tokens = {rights[index : index + 2] for index in range(0, len(rights), 2)}
    return bool(tokens & {"DC", "FA", "GA", "SD", "WD", "WO"})


def _windows_directory_sddl(path: Path) -> str:
    descriptor_path: Path | None = None
    try:
        descriptor_fd, raw_descriptor_path = tempfile.mkstemp(suffix=".acl")
        os.close(descriptor_fd)
        descriptor_path = Path(raw_descriptor_path)
        descriptor_path.unlink(missing_ok=True)
        result = subprocess.run(
            ["icacls", str(path), "/save", str(descriptor_path), "/c"],
            capture_output=True,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0 or not descriptor_path.is_file():
            raise DrawingValidationError("无法验证私有图纸目录 ACL")
        raw = descriptor_path.read_bytes()
        try:
            rendered = raw.decode("utf-16")
        except UnicodeError:
            rendered = raw.decode("utf-8-sig")
        for line in rendered.splitlines():
            marker = line.find("D:")
            if marker >= 0:
                return line[marker:].strip()
    except OSError as error:
        raise DrawingValidationError("无法验证私有图纸目录 ACL") from error
    finally:
        if descriptor_path is not None:
            descriptor_path.unlink(missing_ok=True)
    raise DrawingValidationError("无法读取私有图纸目录 ACL")


def verify_private_storage_acl(path: Path) -> None:
    """Fail closed when a Windows private root inherits or is broadly writable."""
    if os.name != "nt" or load_settings().environment == "test":
        return
    sddl = _windows_directory_sddl(path)
    control = sddl[2 : sddl.find("(") if "(" in sddl else len(sddl)]
    if "P" not in control:
        raise DrawingValidationError("私有图纸目录必须关闭 ACL 继承")
    for match in _SDDL_ALLOW_ACE.finditer(sddl):
        sid = match.group("sid").upper()
        if sid in _BROAD_WINDOWS_SIDS and _sddl_rights_include_write(
            match.group("rights").upper()
        ):
            raise DrawingValidationError("私有图纸目录仍允许普通登录用户写入")


def verify_storage_ancestor_acls(path: Path) -> None:
    """Reject any broad ACE that can replace a protected storage boundary."""
    if os.name != "nt" or load_settings().environment == "test":
        return
    absolute = _absolute_without_resolving(path)
    for ancestor in absolute.parents:
        sddl = _windows_directory_sddl(ancestor)
        for match in _SDDL_ALLOW_ACE.finditer(sddl):
            if "IO" in match.group("flags").upper():
                continue
            sid = match.group("sid").upper()
            if sid in _BROAD_WINDOWS_SIDS and _sddl_rights_allow_boundary_replacement(
                match.group("rights").upper()
            ):
                raise DrawingValidationError(
                    f"私有图纸可信边界的祖先目录可被普通用户替换：{ancestor}"
                )


def ensure_private_storage_root(path: Path) -> Path:
    root = assert_no_reparse_ancestors(path)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise DrawingValidationError("无法创建私有图纸目录") from error
    root = assert_no_reparse_ancestors(root)
    if not root.is_dir():
        raise DrawingValidationError("私有图纸存储路径不是目录")
    verify_private_storage_acl(root)
    return root


def verify_private_drawing_storage() -> None:
    """Startup gate for the writable roots and the historical read-only root."""
    current = load_settings()
    configured_roots = (drawing_dir(), order_draft_drawing_dir())
    if current.environment != "test":
        required_names = (
            "ERP_DRAWING_DIR",
            "ERP_ORDER_DRAFT_DRAWING_DIR",
            "ERP_PRIVATE_STORAGE_TRUST_ROOT",
        )
        missing = [name for name in required_names if not os.getenv(name, "").strip()]
        if missing:
            raise DrawingValidationError(
                "正式运行环境必须显式配置私有图纸目录和可信边界："
                + ", ".join(missing)
            )
        trust_root = ensure_private_storage_root(
            Path(os.environ["ERP_PRIVATE_STORAGE_TRUST_ROOT"])
        )
        verify_storage_ancestor_acls(trust_root)
        project_root = _absolute_without_resolving(Path(__file__).resolve().parents[2])
        absolute_configured_roots = [
            _absolute_without_resolving(root) for root in configured_roots
        ]
        if (
            len({os.path.normcase(root) for root in absolute_configured_roots})
            != len(absolute_configured_roots)
        ):
            raise DrawingValidationError("正式图纸和草稿目录必须相互独立")
        for absolute_root in absolute_configured_roots:
            if os.path.normcase(absolute_root) == os.path.normcase(trust_root):
                raise DrawingValidationError("私有图纸目录必须位于可信边界的子目录")
            try:
                absolute_root.relative_to(trust_root)
            except ValueError as error:
                raise DrawingValidationError("私有图纸目录必须位于可信边界内") from error
            try:
                absolute_root.relative_to(project_root)
            except ValueError:
                pass
            else:
                raise DrawingValidationError("正式私有图纸目录必须位于应用项目树之外")
    for configured_root in configured_roots:
        ensure_private_storage_root(configured_root)

    configured_legacy = os.getenv("ERP_LEGACY_DRAWING_DIR", "").strip()
    if configured_legacy:
        checked_legacy_root = assert_no_reparse_ancestors(Path(configured_legacy))
        if not checked_legacy_root.is_dir():
            raise DrawingValidationError("历史图纸路径不是目录")
        verify_private_storage_acl(checked_legacy_root)
        verify_storage_ancestor_acls(checked_legacy_root)


def _drawing_url(filename: str, *, url_prefix: str) -> str:
    return f"{url_prefix}/{filename}"


def _resolve_pdf_graph_value(value: object) -> object:
    indirect_chain: set[tuple[int, int]] = set()
    while isinstance(value, IndirectObject):
        identity = (value.idnum, value.generation)
        if identity in indirect_chain:
            raise DrawingValidationError("PDF 图纸结构损坏或无法解析")
        indirect_chain.add(identity)
        try:
            value = value.get_object()
        except Exception as error:
            raise DrawingValidationError("PDF 图纸结构损坏或无法解析") from error
    return value


def _validate_pdf_graph(reader: PdfReader) -> None:
    stack: list[object] = [reader.trailer]
    seen_indirect: set[tuple[int, int]] = set()
    seen_containers: set[int] = set()
    visited = 0
    while stack:
        current = stack.pop()
        visited += 1
        if visited > _PDF_MAX_GRAPH_OBJECTS:
            raise DrawingValidationError("PDF 图纸结构过于复杂")
        if isinstance(current, IndirectObject):
            identity = (current.idnum, current.generation)
            if identity in seen_indirect:
                continue
            seen_indirect.add(identity)
            current = current.get_object()
        if isinstance(current, DictionaryObject):
            identity = id(current)
            if identity in seen_containers:
                continue
            seen_containers.add(identity)
            object_type = _resolve_pdf_graph_value(current.get("/Type"))
            if str(object_type) == "/Filespec":
                raise DrawingValidationError("PDF 图纸包含外部文件引用")
            for key, value in current.items():
                key_text = str(key)
                comparison_value = _resolve_pdf_graph_value(value)
                value_text = str(comparison_value)
                if key_text in _PDF_DISALLOWED_KEYS:
                    raise DrawingValidationError("PDF 图纸包含禁止的主动内容或附件")
                if key_text == "/F" and isinstance(current, StreamObject):
                    raise DrawingValidationError("PDF 图纸包含外部文件引用")
                if key_text == "/S" and value_text in _PDF_DISALLOWED_ACTIONS:
                    raise DrawingValidationError("PDF 图纸包含禁止的主动内容或附件")
                if (
                    key_text == "/Subtype"
                    and value_text in _PDF_DISALLOWED_ANNOTATION_SUBTYPES
                ):
                    raise DrawingValidationError("PDF 图纸包含禁止的主动内容或附件")
                stack.append(value)
        elif isinstance(current, ArrayObject):
            identity = id(current)
            if identity in seen_containers:
                continue
            seen_containers.add(identity)
            stack.extend(current)


def _validate_pdf(content: bytes) -> None:
    if not re.match(rb"%PDF-(?:1\.[0-7]|2\.0)", content[:8]):
        raise DrawingValidationError("PDF 图纸文件无法识别")
    stripped = content.rstrip(b"\x00\t\n\x0c\r ")
    if not stripped.endswith(b"%%EOF") or b"startxref" not in stripped[-4096:]:
        raise DrawingValidationError("PDF 图纸结构不完整")
    try:
        reader = PdfReader(BytesIO(content), strict=True)
        if reader.is_encrypted:
            raise DrawingValidationError("PDF 图纸不能使用加密文件")
        if reader.trailer.get("/Root") is None or len(reader.pages) == 0:
            raise DrawingValidationError("PDF 图纸结构不完整")
        for page in reader.pages:
            # Force every page dictionary and media box to resolve so truncated
            # page trees cannot pass merely because the header looks valid.
            _ = page.mediabox
        _validate_pdf_graph(reader)
    except DrawingValidationError:
        raise
    except (PdfReadError, OSError, ValueError, TypeError, KeyError) as error:
        raise DrawingValidationError("PDF 图纸结构损坏或无法解析") from error
    except Exception as error:
        # pypdf can surface malformed object graphs as implementation-specific
        # exceptions. Do not expose parser details to the API caller.
        raise DrawingValidationError("PDF 图纸结构损坏或无法解析") from error


def _validated_image(content: bytes, content_type: str | None) -> Image.Image | None:
    if content_type not in ALLOWED_DRAWING_TYPES:
        raise DrawingValidationError("图纸仅支持 JPG、PNG、WEBP、PDF")
    if not content or len(content) > MAX_DRAWING_BYTES:
        raise DrawingValidationError("图纸文件不能为空且不能超过 20MB")
    if content_type == PDF_CONTENT_TYPE:
        _validate_pdf(content)
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(BytesIO(content))
            expected_format = _IMAGE_FORMAT_BY_CONTENT_TYPE.get(content_type)
            if image.format != expected_format:
                raise DrawingValidationError("图纸声明类型与实际内容不一致")
            width, height = image.size
            if (
                width <= 0
                or height <= 0
                or width > MAX_DRAWING_IMAGE_SIDE
                or height > MAX_DRAWING_IMAGE_SIDE
                or width * height > MAX_DRAWING_IMAGE_PIXELS
            ):
                raise DrawingValidationError("图纸图片尺寸或总像素过大")
            image.load()
    except DrawingValidationError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ) as error:
        raise DrawingValidationError("图纸文件无法识别") from error
    return image


def _save_drawing_files(
    *,
    target: Path,
    url_prefix: str,
    stem: str,
    content: bytes,
    content_type: str | None,
    include_thumbnail: bool,
) -> SavedDrawing:
    image = _validated_image(content, content_type)
    target = ensure_private_storage_root(target)
    created_paths: list[Path] = []
    try:
        if content_type == PDF_CONTENT_TYPE:
            pdf_name = f"{stem}.pdf"
            pdf_file = target / pdf_name
            pending_pdf = target / f".{pdf_name}.{uuid4().hex}.pending"
            created_paths.extend([pending_pdf, pdf_file])
            with pending_pdf.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(pending_pdf, pdf_file)
            pdf_path = _drawing_url(pdf_name, url_prefix=url_prefix)
            return SavedDrawing(image_path=pdf_path, thumbnail_path=pdf_path)

        assert image is not None
        if image.mode not in {"RGB", "L"}:
            background = Image.new("RGB", image.size, "white")
            if "A" in image.getbands():
                background.paste(image, mask=image.getchannel("A"))
            else:
                background.paste(image)
            image = background
        else:
            image = image.convert("RGB")

        high_name = f"{stem}.webp"
        high_file = target / high_name
        pending_high = target / f".{high_name}.{uuid4().hex}.pending"
        high = image.copy()
        high.thumbnail((2000, 2000), Image.Resampling.LANCZOS)
        created_paths.extend([pending_high, high_file])
        high.save(pending_high, "WEBP", quality=82, method=6)
        os.replace(pending_high, high_file)
        image_path = _drawing_url(high_name, url_prefix=url_prefix)
        thumbnail_path = image_path
        if include_thumbnail:
            thumb_name = f"{stem}_thumb.webp"
            thumb_file = target / thumb_name
            pending_thumb = target / f".{thumb_name}.{uuid4().hex}.pending"
            thumb = image.copy()
            thumb.thumbnail((480, 480), Image.Resampling.LANCZOS)
            created_paths.extend([pending_thumb, thumb_file])
            thumb.save(pending_thumb, "WEBP", quality=76, method=6)
            os.replace(pending_thumb, thumb_file)
            thumbnail_path = _drawing_url(thumb_name, url_prefix=url_prefix)
        return SavedDrawing(
            image_path=image_path,
            thumbnail_path=thumbnail_path,
        )
    except Exception:
        for created_path in created_paths:
            try:
                created_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def save_product_drawing_files(
    *,
    product_id: int,
    content: bytes,
    content_type: str | None,
    include_thumbnail: bool = True,
) -> SavedDrawing:
    return _save_drawing_files(
        target=drawing_dir(),
        url_prefix=DRAWING_URL_PREFIX,
        stem=f"product_{product_id}_{uuid4().hex}",
        content=content,
        content_type=content_type,
        include_thumbnail=include_thumbnail,
    )


def save_order_draft_drawing_files(
    *,
    content: bytes,
    content_type: str | None,
    owner_user_id: int,
    owner_auth_version: int,
) -> SavedDrawing:
    """Store a private draft and return a signed, owner-bound reference."""
    if (
        isinstance(owner_user_id, bool)
        or not isinstance(owner_user_id, int)
        or owner_user_id < 1
        or isinstance(owner_auth_version, bool)
        or not isinstance(owner_auth_version, int)
        or owner_auth_version < 1
    ):
        raise DrawingValidationError("订单图纸草稿所有者无效")
    draft_id = uuid4().hex
    saved = _save_drawing_files(
        target=order_draft_drawing_dir(),
        url_prefix=ORDER_DRAFT_URL_PREFIX,
        stem=f"draft_{draft_id}",
        content=content,
        content_type=content_type,
        include_thumbnail=False,
    )
    filename = Path(saved.image_path).name
    try:
        stored_content = _read_checked_file(
            ensure_private_storage_root(order_draft_drawing_dir()) / filename,
            label="订单图纸草稿",
        )
    except Exception:
        _remove_private_file(order_draft_drawing_dir(), filename)
        raise
    try:
        ttl_seconds = int(os.getenv("ERP_ORDER_DRAFT_TTL_SECONDS", "3600"))
    except ValueError as error:
        _remove_private_file(order_draft_drawing_dir(), filename)
        raise DrawingValidationError("订单图纸草稿有效期配置无效") from error
    if ttl_seconds < 60 or ttl_seconds > 86_400:
        _remove_private_file(order_draft_drawing_dir(), filename)
        raise DrawingValidationError("订单图纸草稿有效期必须为 60 至 86400 秒")
    now = datetime.now(timezone.utc)
    payload = {
        "type": _DRAFT_TOKEN_TYPE,
        "filename": filename,
        "owner_user_id": owner_user_id,
        "owner_auth_version": owner_auth_version,
        "content_type": PDF_CONTENT_TYPE if filename.endswith(".pdf") else "image/webp",
        "content_sha256": hashlib.sha256(stored_content).hexdigest(),
        "iat": now,
        "exp": now + timedelta(seconds=ttl_seconds),
    }
    try:
        token = jwt.encode(
            payload,
            load_settings().secret_key,
            algorithm="HS256",
        )
    except Exception:
        _remove_private_file(order_draft_drawing_dir(), filename)
        raise
    reference = f"{ORDER_DRAFT_URL_PREFIX}/{token}/{filename}"
    return SavedDrawing(image_path=reference, thumbnail_path=reference)


def _draft_token_from_reference(reference: str) -> tuple[str, str]:
    if not isinstance(reference, str):
        raise DrawingValidationError("订单图纸草稿标识无效")
    if "\\" in reference or "?" in reference or "#" in reference or "%" in reference:
        raise DrawingValidationError("订单图纸草稿标识无效")
    prefix = f"{ORDER_DRAFT_URL_PREFIX}/"
    if not reference.startswith(prefix):
        raise DrawingValidationError("订单图纸必须先通过草稿上传接口上传")
    remainder = reference[len(prefix) :]
    if remainder.count("/") != 1 or len(remainder) > 2304:
        raise DrawingValidationError("订单图纸草稿标识无效")
    token, filename = remainder.split("/", 1)
    if not token or len(token) > 2048 or not _DRAFT_FILENAME.fullmatch(filename):
        raise DrawingValidationError("订单图纸草稿标识无效")
    return token, filename


def _decode_draft_reference(
    reference: str,
    *,
    owner_user_id: int,
    owner_auth_version: int,
) -> dict[str, object]:
    token, reference_filename = _draft_token_from_reference(reference)
    try:
        payload = jwt.decode(
            token,
            load_settings().secret_key,
            algorithms=["HS256"],
            options={"require": ["type", "filename", "exp", "iat"]},
        )
    except jwt.ExpiredSignatureError as error:
        raise DrawingValidationError("订单图纸草稿已过期") from error
    except jwt.PyJWTError as error:
        raise DrawingValidationError("订单图纸草稿标识无效") from error
    if payload.get("type") != _DRAFT_TOKEN_TYPE:
        raise DrawingValidationError("订单图纸草稿标识无效")
    token_user_id = payload.get("owner_user_id")
    token_auth_version = payload.get("owner_auth_version")
    if (
        isinstance(token_user_id, bool)
        or isinstance(token_auth_version, bool)
        or token_user_id != owner_user_id
        or token_auth_version != owner_auth_version
    ):
        raise DrawingValidationError("订单图纸草稿不属于当前登录账号")
    filename = payload.get("filename")
    content_type = payload.get("content_type")
    content_sha256 = payload.get("content_sha256")
    if (
        not isinstance(filename, str)
        or not _DRAFT_FILENAME.fullmatch(filename)
        or filename != reference_filename
        or content_type not in {"image/webp", PDF_CONTENT_TYPE}
        or not isinstance(content_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", content_sha256)
    ):
        raise DrawingValidationError("订单图纸草稿标识无效")
    return payload


def resolve_order_draft_drawing(
    reference: str,
    *,
    owner_user_id: int,
    owner_auth_version: int,
) -> ResolvedDraftDrawing:
    """Resolve a signed reference only for the session that uploaded it."""
    payload = _decode_draft_reference(
        reference,
        owner_user_id=owner_user_id,
        owner_auth_version=owner_auth_version,
    )
    root = ensure_private_storage_root(order_draft_drawing_dir())
    filename = str(payload["filename"])
    candidate = root / filename
    try:
        candidate_stat = candidate.lstat()
    except OSError as error:
        raise DrawingValidationError("订单图纸草稿不存在或已失效") from error
    if (
        candidate.is_symlink()
        or _is_windows_reparse_point(candidate_stat)
        or not stat.S_ISREG(candidate_stat.st_mode)
    ):
        raise DrawingValidationError("订单图纸草稿路径不安全")
    return ResolvedDraftDrawing(
        path=candidate,
        content_type=str(payload["content_type"]),
        content_sha256=str(payload["content_sha256"]),
    )


def _validate_open_file(
    *,
    file_stat: os.stat_result,
    path_stat: os.stat_result,
    label: str,
) -> None:
    if not stat.S_ISREG(file_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise DrawingValidationError(f"{label}不是普通文件")
    if _is_windows_reparse_point(file_stat) or _is_windows_reparse_point(path_stat):
        raise DrawingValidationError(f"{label}路径不安全")
    if getattr(file_stat, "st_nlink", 1) != 1 or getattr(path_stat, "st_nlink", 1) != 1:
        raise DrawingValidationError(f"{label}不能使用硬链接")
    try:
        same_file = os.path.samestat(file_stat, path_stat)
    except OSError:
        same_file = False
    if not same_file:
        raise DrawingValidationError(f"{label}在读取时发生变化")


def _read_checked_file(
    path: Path,
    *,
    label: str,
    expected_sha256: str | None = None,
) -> bytes:
    assert_no_reparse_ancestors(path.parent)
    try:
        with path.open("rb") as handle:
            opened_stat = os.fstat(handle.fileno())
            path_stat = path.stat(follow_symlinks=False)
            _validate_open_file(
                file_stat=opened_stat,
                path_stat=path_stat,
                label=label,
            )
            content = handle.read(MAX_DRAWING_BYTES + 1)
            finished_stat = os.fstat(handle.fileno())
            finished_path_stat = path.stat(follow_symlinks=False)
            _validate_open_file(
                file_stat=finished_stat,
                path_stat=finished_path_stat,
                label=label,
            )
            if (
                opened_stat.st_size != finished_stat.st_size
                or opened_stat.st_mtime_ns != finished_stat.st_mtime_ns
            ):
                raise DrawingValidationError(f"{label}在读取时发生变化")
    except DrawingValidationError:
        raise
    except (OSError, FileNotFoundError) as error:
        raise DrawingValidationError(f"{label}不存在或已失效") from error
    if len(content) > MAX_DRAWING_BYTES:
        raise DrawingValidationError("图纸文件不能为空且不能超过 20MB")
    if expected_sha256 and hashlib.sha256(content).hexdigest() != expected_sha256:
        raise DrawingValidationError(f"{label}完整性校验失败")
    return content


def load_order_draft_drawing(
    reference: str,
    *,
    owner_user_id: int,
    owner_auth_version: int,
) -> tuple[bytes, str]:
    """Read and revalidate a controlled draft before promoting it to an order."""
    resolved = resolve_order_draft_drawing(
        reference,
        owner_user_id=owner_user_id,
        owner_auth_version=owner_auth_version,
    )
    content = _read_checked_file(
        resolved.path,
        label="订单图纸草稿",
        expected_sha256=resolved.content_sha256,
    )
    content_type = resolved.content_type
    _validated_image(content, content_type)
    return content, content_type


def remove_order_draft_drawing(
    reference: str,
    *,
    owner_user_id: int,
    owner_auth_version: int,
) -> list[str]:
    try:
        resolved = resolve_order_draft_drawing(
            reference,
            owner_user_id=owner_user_id,
            owner_auth_version=owner_auth_version,
        )
    except DrawingValidationError as error:
        return [str(error)]
    try:
        resolved.path.unlink(missing_ok=True)
    except OSError as error:
        return [str(error)]
    return []


def _validated_stored_filename(filename: str) -> str:
    if (
        not isinstance(filename, str)
        or not filename
        or len(filename) > 255
        or filename in {".", ".."}
        or filename[-1] in {" ", "."}
        or any(character in filename for character in ("/", "\\", ":", "\x00"))
        or any(ord(character) < 32 for character in filename)
        or Path(filename).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".pdf"}
    ):
        raise DrawingValidationError("图纸文件名无效")
    return filename


def _stored_content_type(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".pdf": PDF_CONTENT_TYPE,
    }[suffix]


def load_stored_drawing(filename: str) -> LoadedDrawing:
    """Load one private or historical formal drawing without a path reopen."""
    filename = _validated_stored_filename(filename)
    roots: list[Path] = []
    candidate_roots = [drawing_dir()]
    if (
        load_settings().environment == "test"
        or os.getenv("ERP_LEGACY_DRAWING_DIR", "").strip()
    ):
        candidate_roots.append(legacy_drawing_dir())
    for configured_root in candidate_roots:
        absolute_root = _absolute_without_resolving(configured_root)
        if any(os.path.normcase(root) == os.path.normcase(absolute_root) for root in roots):
            continue
        if not absolute_root.exists():
            continue
        checked_root = assert_no_reparse_ancestors(absolute_root)
        if not checked_root.is_dir():
            raise DrawingValidationError("图纸存储路径不是目录")
        verify_private_storage_acl(checked_root)
        roots.append(checked_root)
    candidates: list[Path] = []
    for root in roots:
        candidate = root / filename
        try:
            candidate_stat = candidate.lstat()
        except OSError:
            continue
        if (
            candidate.is_symlink()
            or _is_windows_reparse_point(candidate_stat)
            or not stat.S_ISREG(candidate_stat.st_mode)
        ):
            raise DrawingValidationError("图纸文件路径不安全")
        candidates.append(candidate)
    if not candidates:
        raise DrawingValidationError("图纸文件不存在")
    if len(candidates) != 1:
        raise DrawingValidationError("图纸文件存储位置不唯一")
    content_type = _stored_content_type(filename)
    content = _read_checked_file(candidates[0], label="图纸文件")
    _validated_image(content, content_type)
    return LoadedDrawing(
        filename=filename,
        content=content,
        content_type=content_type,
    )


def _remove_private_file(root: Path, filename: str) -> str | None:
    try:
        checked_root = ensure_private_storage_root(root)
        filename = _validated_stored_filename(filename)
        candidate = checked_root / filename
        if candidate.exists() or candidate.is_symlink():
            candidate_stat = candidate.lstat()
            if candidate.is_symlink() or _is_windows_reparse_point(candidate_stat):
                return f"unsafe path: {filename}"
        candidate.unlink(missing_ok=True)
    except (DrawingValidationError, OSError) as error:
        return str(error)
    return None


def remove_drawing_files(image_path: str, thumbnail_path: str) -> list[str]:
    errors: list[str] = []
    for stored_path in {image_path, thumbnail_path}:
        prefix = f"{DRAWING_URL_PREFIX}/"
        if not isinstance(stored_path, str) or not stored_path.startswith(prefix):
            errors.append(f"unsafe path: {stored_path}")
            continue
        filename = stored_path[len(prefix) :]
        error = _remove_private_file(drawing_dir(), filename)
        if error:
            errors.append(f"{stored_path}: {error}")
    return errors
