"""Pure, shared PDF parse/OCR orchestration for training and order preview.

This module deliberately has no SQLAlchemy Session dependency and never calls
order matching or writes training/order records.  Keeping the orchestration in
one place prevents a template from being trained with a different parser than
the one used to preview an order.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.order_pdf_import import (
    PdfParseError,
    extract_text_from_pdf_bytes,
    merge_simair_text_and_ocr_drafts,
    parse_purchase_order_text,
    resolve_pdf_customer_route,
)
from app.services.pdf_ocr import analyze_pdf_text_quality, ocr_pdf_bytes, should_use_ocr


@dataclass
class PdfParsePipelineResult:
    draft: dict
    extracted_text: str
    ocr_text_raw: str | None
    parse_method: str
    text_quality: str
    ocr_attempted: bool = False
    ocr_method: str | None = None


class PdfParsePipelineError(PdfParseError):
    """A preview-compatible parse error that retains non-persistent evidence."""

    def __init__(
        self,
        error: PdfParseError,
        *,
        extracted_text: str = "",
        ocr_text_raw: str | None = None,
        parse_method: str = "failed",
        text_quality: str = "image_only",
    ) -> None:
        super().__init__(error.message, error.parse_status)
        self.extracted_text = extracted_text
        self.ocr_text_raw = ocr_text_raw
        self.parse_method = parse_method
        self.text_quality = text_quality


def parse_pdf_bytes(
    content: bytes,
    source_name: str,
    template_rules: list[dict],
) -> PdfParsePipelineResult:
    """Run the exact preview parser choreography without touching the database."""
    text = ""
    ocr_text_raw: str | None = None
    parse_method = "failed"
    text_quality = "image_only"
    ocr_attempted = False
    ocr_method: str | None = None
    try:
        text = extract_text_from_pdf_bytes(content)
        text_quality = str(analyze_pdf_text_quality(text)["status"])
        customer_route = resolve_pdf_customer_route(text or "", template_rules)
        draft: dict | None = None
        text_draft: dict | None = None
        parse_error: PdfParseError | None = None

        if text and text.strip():
            try:
                draft = parse_purchase_order_text(
                    text,
                    source_name=source_name,
                    template_rules=template_rules,
                    customer_route=customer_route,
                )
                customer_route = draft.get("customer_route") or customer_route
                parse_method = "text"
                if (
                    text_quality == "garbled_text_layer"
                    and draft.get("customer_type") == "simair"
                ):
                    text_draft = draft
                    draft = None
            except PdfParseError as error:
                parse_error = error

        if text_quality == "garbled_text_layer" or should_use_ocr(text, draft):
            ocr_attempted = True
            ocr_text, ocr_method = (
                ocr_pdf_bytes(content, dpi=300)
                if text_draft is not None
                and text_draft.get("customer_type") == "simair"
                else ocr_pdf_bytes(content)
            )
            parse_method = ocr_method if draft is None else parse_method
            if ocr_text and ocr_method not in {"ocr_unavailable", "ocr_failed"}:
                ocr_text_raw = ocr_text
                try:
                    if customer_route.get("status") == "unmatched":
                        customer_route = resolve_pdf_customer_route(ocr_text, template_rules)
                    ocr_parse_text = ocr_text
                    exact_po = str((text_draft or {}).get("customer_po") or "").strip()
                    if exact_po and exact_po.casefold() not in ocr_text.casefold():
                        ocr_parse_text = f"{exact_po}\n{ocr_text}"
                    ocr_draft = parse_purchase_order_text(
                        ocr_parse_text,
                        source_name=source_name,
                        template_rules=template_rules,
                        customer_route=customer_route,
                    )
                    result = (
                        merge_simair_text_and_ocr_drafts(text_draft, ocr_draft)
                        if text_draft is not None
                        and ocr_draft.get("customer_type") == "simair"
                        else ocr_draft
                    )
                    result["source_text_quality"] = text_quality
                    result["parse_method"] = "mixed" if text_draft else ocr_method
                    return PdfParsePipelineResult(
                        draft=result,
                        extracted_text=text,
                        ocr_text_raw=ocr_text_raw,
                        parse_method=result["parse_method"],
                        text_quality=text_quality,
                        ocr_attempted=ocr_attempted,
                        ocr_method=ocr_method,
                    )
                except PdfParseError as error:
                    parse_error = error
                    parse_method = ocr_method

        if draft is not None:
            draft["source_text_quality"] = text_quality
            draft.setdefault("parse_method", "text")
            return PdfParsePipelineResult(
                draft=draft,
                extracted_text=text,
                ocr_text_raw=ocr_text_raw,
                parse_method=str(draft["parse_method"]),
                text_quality=text_quality,
                ocr_attempted=ocr_attempted,
                ocr_method=ocr_method,
            )
        if text_draft is not None:
            text_draft["source_text_quality"] = text_quality
            text_draft["parse_method"] = parse_method or "text_fallback"
            text_draft["recognition_status"] = "needs_confirmation"
            text_draft["parse_status"] = "needs_confirmation"
            text_draft.setdefault("warnings", []).append(
                "思迈尔 PDF 文本层异常，OCR 未能补全；已保留精确客户订单号，产品需人工确认。"
            )
            return PdfParsePipelineResult(
                draft=text_draft,
                extracted_text=text,
                ocr_text_raw=ocr_text_raw,
                parse_method=str(text_draft["parse_method"]),
                text_quality=text_quality,
                ocr_attempted=ocr_attempted,
                ocr_method=ocr_method,
            )
        if parse_error is not None:
            raise parse_error
        fallback = parse_purchase_order_text(
            text or "",
            source_name=source_name,
            template_rules=template_rules,
            customer_route=customer_route,
        )
        fallback["source_text_quality"] = text_quality
        fallback.setdefault("parse_method", parse_method or "text")
        return PdfParsePipelineResult(
            draft=fallback,
            extracted_text=text,
            ocr_text_raw=ocr_text_raw,
            parse_method=str(fallback["parse_method"]),
            text_quality=text_quality,
            ocr_attempted=ocr_attempted,
            ocr_method=ocr_method,
        )
    except PdfParsePipelineError:
        raise
    except PdfParseError as error:
        raise PdfParsePipelineError(
            error,
            extracted_text=text,
            ocr_text_raw=ocr_text_raw,
            parse_method=parse_method,
            text_quality=text_quality,
        ) from error
    except Exception as error:
        raise PdfParsePipelineError(
            PdfParseError("文件识别失败，请检查文件内容后重试", "failed"),
            extracted_text=text,
            ocr_text_raw=ocr_text_raw,
            parse_method=parse_method,
            text_quality=text_quality,
        ) from error
