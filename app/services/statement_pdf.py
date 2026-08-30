from __future__ import annotations

from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.services.contract_pdf import _registered_font


def render_customer_statement_pdf(
    *,
    title: str,
    subtitle: str,
    price_label: str,
    amount_label: str,
    rows: list[dict],
    quantity_total: object,
    amount_total: object,
) -> bytes:
    font_name, _font_hash = _registered_font()
    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        leftMargin=10 * mm,
        rightMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
    )
    normal = ParagraphStyle("statement-normal", fontName=font_name, fontSize=8, leading=10)
    heading = ParagraphStyle(
        "statement-heading", fontName=font_name, fontSize=15, leading=18, alignment=1
    )
    story = [Paragraph(title, heading), Paragraph(subtitle, normal), Spacer(1, 4 * mm)]
    headers = ["送货日期", "送货单号", "客户单号", "存货编码", "产品名称", "数量", price_label, amount_label]
    data = [headers]
    for row in rows:
        data.append(
            [
                str(row.get("delivery_date") or ""),
                str(row.get("delivery_number") or ""),
                str(row.get("customer_po") or ""),
                str(row.get("product_code") or ""),
                Paragraph(str(row.get("product_name") or ""), normal),
                str(row.get("quantity") or 0),
                f"{row.get('unit_price', 0):.4f}",
                f"{row.get('amount', 0):.2f}",
            ]
        )
    data.append(["", "", "", "", "合计", str(quantity_total), "", f"{amount_total:.2f}"])
    table = Table(
        data,
        repeatRows=1,
        colWidths=[22 * mm, 29 * mm, 31 * mm, 31 * mm, 55 * mm, 20 * mm, 25 * mm, 28 * mm],
    )
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), font_name),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E7F2F0")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#123B3A")),
                ("ALIGN", (0, 0), (-1, 0), "CENTER"),
                ("ALIGN", (5, 1), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#B8C9C7")),
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#F5F8F8")),
                ("FONTNAME", (0, -1), (-1, -1), font_name),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(table)
    document.build(story)
    return output.getvalue()
