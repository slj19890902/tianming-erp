"""FIN-001 invoice PDF storage primitives.

Database associations and download permissions belong to later API work.  This
module only validates an incoming PDF and keeps immutable original and arranged
copies with a content hash trace between them.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePath
from uuid import uuid4


DEFAULT_MAX_INVOICE_ATTACHMENT_BYTES = 20 * 1024 * 1024


class InvoiceAttachmentError(ValueError):
    """Raised when an invoice attachment violates the FIN-001 storage gate."""


@dataclass(frozen=True)
class StoredInvoicePdf:
    original_filename: str
    path: Path
    sha256: str
    size: int
    reused: bool


@dataclass(frozen=True)
class OrganizedInvoicePdf:
    original_path: Path
    path: Path
    source_sha256: str
    size: int
    reused: bool


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest().upper()


def _safe_filename(filename: str) -> str:
    if not isinstance(filename, str) or not filename.strip():
        raise InvoiceAttachmentError("附件文件名不能为空")
    if "\x00" in filename or "/" in filename or "\\" in filename or ".." in filename:
        raise InvoiceAttachmentError("附件文件名不安全")
    value = PurePath(filename).name.strip()
    if value != filename.strip() or value in {"", ".", ".."}:
        raise InvoiceAttachmentError("附件文件名不安全")
    if Path(value).suffix.lower() != ".pdf":
        raise InvoiceAttachmentError("附件仅支持 PDF 文件")
    return value


def validate_invoice_pdf(
    *,
    filename: str,
    content: bytes,
    max_bytes: int = DEFAULT_MAX_INVOICE_ATTACHMENT_BYTES,
) -> str:
    """Require a safe ``.pdf`` filename and the PDF magic header."""

    _safe_filename(filename)
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if not content:
        raise InvoiceAttachmentError("PDF 附件不能为空")
    if len(content) > max_bytes:
        raise InvoiceAttachmentError("PDF 附件超过大小限制")
    if not content.startswith(b"%PDF-"):
        raise InvoiceAttachmentError("PDF 附件文件头无效")
    return _sha256(content)


def _within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _root(path: str | Path) -> Path:
    root = Path(path).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_immutable(target: Path, content: bytes) -> bool:
    """Write once, or confirm an existing content-addressed target is identical."""

    if target.exists():
        if target.read_bytes() != content:
            raise InvoiceAttachmentError("同名附件已存在且内容不一致，拒绝覆盖")
        return True
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.read_bytes() != content:
                raise InvoiceAttachmentError("同名附件已存在且内容不一致，拒绝覆盖")
            return True
        finally:
            temporary.unlink(missing_ok=True)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return False


def store_original_invoice_pdf(
    *,
    storage_root: str | Path,
    original_filename: str,
    content: bytes,
    max_bytes: int = DEFAULT_MAX_INVOICE_ATTACHMENT_BYTES,
) -> StoredInvoicePdf:
    """Store the immutable original under a SHA-256 filename."""

    safe_name = _safe_filename(original_filename)
    digest = validate_invoice_pdf(
        filename=safe_name, content=content, max_bytes=max_bytes
    )
    root = _root(storage_root)
    originals = (root / "originals").resolve()
    if not _within(originals, root):
        raise InvoiceAttachmentError("原件目录越界")
    originals.mkdir(parents=True, exist_ok=True)
    target = (originals / f"{digest}.pdf").resolve()
    if not _within(target, originals):
        raise InvoiceAttachmentError("原件存储路径越界")
    reused = _write_immutable(target, content)
    return StoredInvoicePdf(
        original_filename=safe_name,
        path=target,
        sha256=digest,
        size=len(content),
        reused=reused,
    )


def create_organized_invoice_pdf(
    *,
    storage_root: str | Path,
    original: StoredInvoicePdf,
    organized_filename: str,
) -> OrganizedInvoicePdf:
    """Create a traceable arranged copy without changing the immutable original."""

    safe_name = _safe_filename(organized_filename)
    root = _root(storage_root)
    originals = (root / "originals").resolve()
    source = original.path.resolve()
    if not _within(source, originals) or not source.is_file():
        raise InvoiceAttachmentError("原始 PDF 不在受管原件目录中")
    content = source.read_bytes()
    digest = validate_invoice_pdf(filename=source.name, content=content)
    if digest != original.sha256 or len(content) != original.size:
        raise InvoiceAttachmentError("原始 PDF 的内容哈希或大小不匹配")
    arranged = (root / "arranged").resolve()
    if not _within(arranged, root):
        raise InvoiceAttachmentError("整理目录越界")
    arranged.mkdir(parents=True, exist_ok=True)
    target = (arranged / safe_name).resolve()
    if not _within(target, arranged):
        raise InvoiceAttachmentError("整理副本路径越界")
    reused = _write_immutable(target, content)
    return OrganizedInvoicePdf(
        original_path=source,
        path=target,
        source_sha256=digest,
        size=len(content),
        reused=reused,
    )
