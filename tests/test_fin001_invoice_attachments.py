from __future__ import annotations

from pathlib import Path

import pytest

from app.services.invoice_attachments import (
    InvoiceAttachmentError,
    create_organized_invoice_pdf,
    store_original_invoice_pdf,
    validate_invoice_pdf,
)


PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"


def test_original_and_arranged_pdf_are_immutable_and_traceable(tmp_path: Path) -> None:
    storage = tmp_path / "invoice-files"
    original = store_original_invoice_pdf(
        storage_root=storage,
        original_filename="苏州思迈尔-202607.pdf",
        content=PDF_BYTES,
    )
    original_again = store_original_invoice_pdf(
        storage_root=storage,
        original_filename="另一份原始名称.pdf",
        content=PDF_BYTES,
    )
    arranged = create_organized_invoice_pdf(
        storage_root=storage,
        original=original,
        organized_filename="2026-07-苏州思迈尔-发票.pdf",
    )
    arranged_again = create_organized_invoice_pdf(
        storage_root=storage,
        original=original,
        organized_filename="2026-07-苏州思迈尔-发票.pdf",
    )

    assert original.path.parent.name == "originals"
    assert original.path.name == f"{original.sha256}.pdf"
    assert original.path.read_bytes() == PDF_BYTES
    assert original_again.path == original.path
    assert original_again.reused is True
    assert arranged.path.parent.name == "arranged"
    assert arranged.path.read_bytes() == PDF_BYTES
    assert arranged.original_path == original.path
    assert arranged.source_sha256 == original.sha256
    assert arranged_again.reused is True
    assert original.path.read_bytes() == PDF_BYTES


@pytest.mark.parametrize(
    ("filename", "content", "max_bytes"),
    [
        ("invoice.txt", PDF_BYTES, 1024),
        ("../invoice.pdf", PDF_BYTES, 1024),
        ("invoice.pdf", b"not a pdf", 1024),
        ("invoice.pdf", b"", 1024),
        ("invoice.pdf", PDF_BYTES, 4),
    ],
)
def test_pdf_validation_rejects_extension_path_header_and_size(
    filename: str, content: bytes, max_bytes: int
) -> None:
    with pytest.raises(InvoiceAttachmentError):
        validate_invoice_pdf(filename=filename, content=content, max_bytes=max_bytes)


def test_arranged_copy_rejects_tampered_or_unmanaged_original(tmp_path: Path) -> None:
    storage = tmp_path / "invoice-files"
    original = store_original_invoice_pdf(
        storage_root=storage,
        original_filename="invoice.pdf",
        content=PDF_BYTES,
    )
    original.path.write_bytes(PDF_BYTES + b"tampered")

    with pytest.raises(InvoiceAttachmentError, match="哈希"):
        create_organized_invoice_pdf(
            storage_root=storage,
            original=original,
            organized_filename="arranged.pdf",
        )
