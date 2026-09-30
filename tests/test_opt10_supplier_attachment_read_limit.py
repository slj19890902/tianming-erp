from __future__ import annotations

import asyncio

from fastapi import HTTPException
import pytest

from app.services.invoice_attachments import DEFAULT_MAX_INVOICE_ATTACHMENT_BYTES
from tests.test_phase11_requisition import requisition_app
from tests.test_opt10_supplier_attachment_atomicity import (
    _originals,
    _user,
    attachment_context,
)


class BoundedOversizeUpload:
    """Synthetic upload that records the handler's requested byte limit."""

    filename = "over-limit.pdf"

    def __init__(self) -> None:
        self.read_sizes: list[int] = []
        self._content = b"%PDF-1.7\n" + b"x" * (
            DEFAULT_MAX_INVOICE_ATTACHMENT_BYTES + 1 - len(b"%PDF-1.7\n")
        )

    async def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self._content


def test_supplier_invoice_attachment_reads_at_most_validation_limit_before_rejecting(
    attachment_context, monkeypatch
) -> None:
    """The boundary is application-memory input, not an ASGI body-size claim."""

    api, session_factory, ids, root = attachment_context
    upload = BoundedOversizeUpload()
    monkeypatch.setattr(
        api,
        "store_original_invoice_pdf",
        lambda **_kwargs: pytest.fail("oversized input must not reach storage"),
    )
    monkeypatch.setattr(
        api,
        "_audit",
        lambda *_args, **_kwargs: pytest.fail("oversized input must not be audited"),
    )

    with session_factory() as db:
        with pytest.raises(HTTPException) as error:
            asyncio.run(
                api.upload_supplier_invoice_attachment(
                    ids["statement_id"],
                    ids["invoice_a"],
                    upload,
                    db,
                    _user(db, ids["admin_id"]),
                )
            )

    assert error.value.status_code == 422
    assert upload.read_sizes == [DEFAULT_MAX_INVOICE_ATTACHMENT_BYTES + 1]
    assert _originals(root) == []


def test_supplier_attachment_scope_rejects_before_even_bounded_read(attachment_context) -> None:
    """The existing scope gate must precede any input consumption."""

    api, session_factory, ids, _root = attachment_context
    upload = BoundedOversizeUpload()

    with session_factory() as db:
        with pytest.raises(HTTPException) as error:
            asyncio.run(
                api.upload_supplier_invoice_attachment(
                    ids["statement_id"],
                    ids["invoice_a"],
                    upload,
                    db,
                    _user(db, ids["sales_id"]),
                )
            )

    assert error.value.status_code == 403
    assert upload.read_sizes == []
