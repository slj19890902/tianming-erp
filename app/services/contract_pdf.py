from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from threading import Lock
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    KeepInFrame,
    LongTable,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from app.core.config import PROJECT_ROOT
from app.models.company_config import CompanyConfig
from app.models.customer_contract import CustomerContract, CustomerContractItem
from app.services.product_specification import dimension_specification


CONTRACT_PDF_TEMPLATE_VERSION = "p1-35a-v1"
_FONT_ENV_NAME = "ERP_CONTRACT_PDF_FONT_PATH"
_TRUSTED_WINDOWS_FONTS = (
    Path(r"C:\Windows\Fonts\simhei.ttf"),
    Path(r"C:\Windows\Fonts\Deng.ttf"),
    Path(r"C:\Windows\Fonts\simsunb.ttf"),
)
_FONT_LOCK = Lock()
_REGISTERED_FONTS: dict[tuple[str, int, int], tuple[str, str]] = {}

_TERMS = (
    (
        "第二条　交货",
        "甲方在乙方确认规格、数量、图稿及交货信息后，原则上于7个工作日内送达约定地点。"
        "乙方变更资料、地点或拒绝按约收货的，交期相应顺延，增加的仓储、运输等合理费用由乙方承担。"
        "乙方授权人员签收后视为交付完成，货物毁损、灭失风险转由乙方承担；因甲方原因造成的除外。",
    ),
    (
        "第三条　结算与发票",
        "双方按{payment_terms}结算。乙方收到对账单后5个工作日内未书面提出具体异议的，视为对账无异议。"
        "乙方应按约支付货款；逾期的，每日按逾期未付款项万分之三支付违约金，甲方有权暂停后续供货。"
        "甲方依法按实际交易及届时适用税率开具发票。",
    ),
    (
        "第四条　验收与质量责任",
        "乙方应在收货时核验数量、规格和外观，并在收货后5个工作日内书面提出异议和证据；"
        "隐蔽质量问题应在发现或应当发现后5个工作日内提出并保留待检货物。经确认属于甲方责任的，"
        "甲方可根据实际情况采取补货、更换、返工、减价或退还不合格部分价款。"
        "因乙方提供或确认的资料、储存、装卸或使用不当造成的问题，由乙方承担。",
    ),
    (
        "第五条　变更、解除与终止",
        "合同或订单变更、取消须经双方书面确认。乙方在甲方已备料、排产或生产后取消、变更或无正当理由拒收的，"
        "应承担甲方已经发生且可证明的材料、生产、仓储、运输及处置损失。一方发生法定解除事由，"
        "或经催告仍不履行主要义务的，守约方可依法解除；结算、质量、违约及争议解决条款不因解除或到期而失效。",
    ),
    (
        "第六条　违约责任",
        "一方违约应继续履行、采取补救措施并赔偿合理损失。因甲方原因迟延交货的，"
        "按迟延部分货款每日万分之三承担违约金，累计不超过该部分货款的10%。除法律另有规定外，"
        "甲方赔偿范围以乙方能够证明的直接实际损失为限，累计不超过涉争货物价款；"
        "因甲方故意或重大过失造成对方人身损害或财产损失的，不适用该限制。",
    ),
    (
        "第七条　生效、附件与争议解决",
        "本合同自双方盖章或有权代表签字之日起生效，一式两份，双方各执一份。双方确认的订单、报价单、样品、"
        "图稿、技术文件、送货单、对账单及补充协议均为合同组成部分；同一事项不一致的，"
        "以后形成且经双方确认的书面文件为准。争议先协商解决；协商不成的，"
        "向甲方住所地有管辖权的人民法院提起诉讼。",
    ),
)


class ContractPdfFontError(RuntimeError):
    """Raised when no trusted embeddable Chinese font is available."""


@dataclass(frozen=True, slots=True)
class ContractPdfDocument:
    content: bytes
    sha256: str
    template_version: str
    font_sha256: str


def _configured_font_path() -> Path:
    configured = os.getenv(_FONT_ENV_NAME, "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path = path.resolve(strict=False)
        if not path.is_file():
            raise ContractPdfFontError(
                f"{_FONT_ENV_NAME} 指定的中文字体不存在"
            )
        return path
    for path in _TRUSTED_WINDOWS_FONTS:
        if path.is_file():
            return path
    raise ContractPdfFontError(
        "未配置可嵌入的合同 PDF 中文字体"
    )


def _registered_font() -> tuple[str, str]:
    path = _configured_font_path()
    stat = path.stat()
    key = (str(path).casefold(), stat.st_size, stat.st_mtime_ns)
    with _FONT_LOCK:
        existing = _REGISTERED_FONTS.get(key)
        if existing is not None:
            return existing
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            font_name = f"ContractCJK_{digest[:16]}"
            pdfmetrics.registerFont(TTFont(font_name, str(path)))
        except Exception as error:  # ReportLab emits several font-specific errors.
            raise ContractPdfFontError(
                "合同 PDF 中文字体无法加载或禁止嵌入"
            ) from error
        result = (font_name, digest)
        _REGISTERED_FONTS[key] = result
        return result


def _xml_text(value: object | None, *, empty: str = "-") -> str:
    text = empty if value is None or value == "" else str(value)
    return escape(text).replace("\r\n", "\n").replace("\r", "\n").replace(
        "\n", "<br/>"
    )


def _plain_text(value: object | None, *, empty: str = "-") -> str:
    return empty if value is None or value == "" else str(value)


def _compact_decimal(value: object | None) -> str:
    if value is None or value == "":
        return ""
    number = Decimal(str(value))
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _money(value: object | None) -> str:
    return f"{Decimal(str(value or 0)):.2f}"


def _date_text(value: date | None) -> str:
    return (
        f"{value.year}年{value.month}月{value.day}日"
        if isinstance(value, date)
        else "-"
    )


def _specification(item: CustomerContractItem) -> str:
    return dimension_specification(
        item.length_mm,
        item.width_mm,
        item.height_mm,
    ) or _plain_text(item.specification)


def _payment_terms(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return "双方书面确认的账期"
    return f"{raw}天账期" if raw.isdigit() else raw


class _ContractCanvas(Canvas):
    def __init__(
        self,
        *args,
        font_name: str,
        contract_version: int,
        **kwargs,
    ) -> None:
        kwargs["invariant"] = 1
        kwargs["pageCompression"] = 1
        super().__init__(*args, **kwargs)
        self._font_name = font_name
        self._contract_version = contract_version
        self._saved_page_states: list[dict] = []

    def showPage(self) -> None:  # noqa: N802 - ReportLab API name
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        page_count = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.saveState()
            self.setFillColor(colors.HexColor("#374151"))
            self.setFont(self._font_name, 7.5)
            footer = (
                f"第 {self._pageNumber} / {page_count} 页　"
                f"合同版本 v{self._contract_version}　"
                f"模板 {CONTRACT_PDF_TEMPLATE_VERSION}"
            )
            self.drawCentredString(A4[0] / 2, 7 * mm, footer)
            self.restoreState()
            super().showPage()
        super().save()


def _styles(font_name: str) -> dict[str, ParagraphStyle]:
    common = {
        "fontName": font_name,
        "textColor": colors.HexColor("#111111"),
        "wordWrap": "CJK",
    }
    return {
        "title": ParagraphStyle(
            "ContractTitle", fontSize=18, leading=22, alignment=TA_CENTER, **common
        ),
        "subtitle": ParagraphStyle(
            "ContractSubtitle",
            fontSize=17,
            leading=21,
            alignment=TA_CENTER,
            spaceAfter=2 * mm,
            **common,
        ),
        "right": ParagraphStyle(
            "ContractRight", fontSize=9.5, leading=13, alignment=TA_RIGHT, **common
        ),
        "body": ParagraphStyle(
            "ContractBody", fontSize=9, leading=13, alignment=TA_LEFT, **common
        ),
        "small": ParagraphStyle(
            "ContractSmall", fontSize=8, leading=11, alignment=TA_LEFT, **common
        ),
        "small_center": ParagraphStyle(
            "ContractSmallCenter",
            fontSize=8,
            leading=11,
            alignment=TA_CENTER,
            **common,
        ),
        "section": ParagraphStyle(
            "ContractSection",
            fontSize=9.5,
            leading=13,
            alignment=TA_LEFT,
            spaceBefore=1.5 * mm,
            spaceAfter=1.5 * mm,
            **common,
        ),
        "terms": ParagraphStyle(
            "ContractTerms",
            fontSize=8.2,
            leading=11.2,
            alignment=TA_JUSTIFY,
            spaceAfter=1.2 * mm,
            **common,
        ),
    }


def _paragraph(value: object | None, style: ParagraphStyle, *, empty: str = "-") -> Paragraph:
    return Paragraph(_xml_text(value, empty=empty), style)


def _header_story(
    contract: CustomerContract,
    company: CompanyConfig | None,
    styles: dict[str, ParagraphStyle],
) -> list:
    company_name = (
        (company.company_name or "").strip()
        if company is not None
        else ""
    ) or "苏州天明包装有限公司"
    company_phone = (
        (company.contact_phone or company.phone)
        if company is not None
        else None
    )
    company_contact = company.contact_person if company is not None else None
    status_label = {
        "draft": "草稿（未盖章）",
        "confirmed": "已确认（未盖章）",
        "converted": "已转订单（未盖章）",
    }.get(contract.status, f"{contract.status}（未盖章）")
    supplier = [
        f"<b>甲方（供方）：{_xml_text(company_name)}</b>",
        f"地址：{_xml_text(company.address if company is not None else None)}",
        f"联系人/电话：{_xml_text(company_contact)}　{_xml_text(company_phone)}",
    ]
    customer = [
        f"<b>乙方（需方）：{_xml_text(contract.customer_name)}</b>",
        f"地址：{_xml_text(contract.customer_address)}",
        f"联系人/电话：{_xml_text(contract.customer_contact)}　{_xml_text(contract.customer_phone)}",
    ]
    parties = Table(
        [
            [
                Paragraph("<br/>".join(supplier), styles["body"]),
                Paragraph("<br/>".join(customer), styles["body"]),
            ]
        ],
        colWidths=[91 * mm, 91 * mm],
    )
    parties.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    meta = Table(
        [
            [
                _paragraph(f"合同日期：{_date_text(contract.contract_date)}", styles["small"]),
                _paragraph(f"交货日期：{_date_text(contract.delivery_date)}", styles["small"]),
                _paragraph(f"客户单号 / PO：{_plain_text(contract.customer_po)}", styles["small"]),
            ]
        ],
        colWidths=[52 * mm, 52 * mm, 78 * mm],
    )
    meta.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return [
        _paragraph(company_name, styles["title"]),
        _paragraph("购货合同", styles["subtitle"]),
        _paragraph(
            f"合同编号：{contract.contract_no}　状态：{status_label}",
            styles["right"],
        ),
        Spacer(1, 2 * mm),
        parties,
        Spacer(1, 2 * mm),
        meta,
        Spacer(1, 2 * mm),
    ]


def _items_table(
    items: list[CustomerContractItem],
    styles: dict[str, ParagraphStyle],
) -> LongTable:
    header = [
        "序号",
        "商品名称",
        "规格",
        "材质 / 楞型",
        "数量",
        "单价",
        "金额",
        "备注",
    ]
    data: list[list] = [
        [_paragraph(value, styles["small_center"]) for value in header]
    ]
    for index, item in enumerate(items, start=1):
        material = " / ".join(
            value for value in (item.material_code, item.flute_type) if value
        )
        data.append(
            [
                _paragraph(index, styles["small_center"]),
                _paragraph(item.product_name, styles["small"]),
                _paragraph(_specification(item), styles["small_center"]),
                _paragraph(material, styles["small_center"]),
                _paragraph(item.quantity, styles["small_center"]),
                _paragraph(_money(item.unit_price), styles["small_center"]),
                _paragraph(_money(item.subtotal), styles["small_center"]),
                _paragraph(item.remarks, styles["small"]),
            ]
        )
    table = LongTable(
        data,
        colWidths=[8 * mm, 34 * mm, 29 * mm, 25 * mm, 15 * mm, 19 * mm, 21 * mm, 31 * mm],
        repeatRows=1,
        splitByRow=1,
        splitInRow=1,
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.55, colors.HexColor("#222222")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
                ("LEFTPADDING", (0, 0), (-1, -1), 1.1 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 1.1 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 1.3 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.3 * mm),
            ]
        )
    )
    return table


def _terms_and_signatures(
    contract: CustomerContract,
    company: CompanyConfig | None,
    styles: dict[str, ParagraphStyle],
) -> list:
    payment_terms = _payment_terms(contract.payment_terms)
    terms = []
    for title, body in _TERMS:
        rendered_body = body.format(payment_terms=payment_terms)
        terms.append(
            Paragraph(
                f"<b>{_xml_text(title)}：</b>{_xml_text(rendered_body)}",
                styles["terms"],
            )
        )
    company_name = (
        (company.company_name or "").strip()
        if company is not None
        else ""
    ) or "苏州天明包装有限公司"
    signatures = Table(
        [
            [
                Paragraph(
                    f"<b>甲方（供方）：{_xml_text(company_name)}</b><br/>"
                    "授权代表：<br/>盖章：<br/>签署日期：",
                    styles["body"],
                ),
                Paragraph(
                    f"<b>乙方（需方）：{_xml_text(contract.customer_name)}</b><br/>"
                    "授权代表：<br/>盖章：<br/>签署日期：",
                    styles["body"],
                ),
            ]
        ],
        colWidths=[84 * mm, 84 * mm],
        hAlign="CENTER",
        rowHeights=[27 * mm],
    )
    signatures.setStyle(
        TableStyle(
            [
                ("LINEABOVE", (0, 0), (-1, 0), 0.7, colors.HexColor("#333333")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 2 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return [
        *terms,
        Spacer(1, 3 * mm),
        signatures,
    ]


def render_contract_pdf(
    contract: CustomerContract,
    company: CompanyConfig | None,
) -> ContractPdfDocument:
    """Render a deterministic, unsealed PDF from persisted contract snapshots."""

    font_name, font_sha256 = _registered_font()
    styles = _styles(font_name)
    output = BytesIO()
    doc = BaseDocTemplate(
        output,
        pagesize=A4,
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=49 * mm,
        bottomMargin=13 * mm,
        title=f"购货合同 {contract.contract_no}",
        author=(company.company_name if company is not None else "") or "天明 ERP",
        subject=(
            f"合同版本 v{contract.version}; "
            f"模板 {CONTRACT_PDF_TEMPLATE_VERSION}; 未盖章"
        ),
    )
    def draw_repeated_header(canvas: Canvas, _doc: BaseDocTemplate) -> None:
        header = KeepInFrame(
            doc.width,
            40 * mm,
            _header_story(contract, company, styles),
            mode="shrink",
        )
        _width, height = header.wrapOn(canvas, doc.width, 40 * mm)
        header.drawOn(
            canvas,
            doc.leftMargin,
            A4[1] - 7 * mm - height,
        )

    doc.addPageTemplates(
        PageTemplate(
            id="contract",
            frames=[
                Frame(
                    doc.leftMargin,
                    doc.bottomMargin,
                    doc.width,
                    doc.height,
                    leftPadding=0,
                    rightPadding=0,
                    topPadding=0,
                    bottomPadding=0,
                )
            ],
            onPage=draw_repeated_header,
        )
    )
    story: list = [
        _paragraph("第一条　货物名称、规格、数量及价款", styles["section"]),
        _items_table(list(contract.items), styles),
        Spacer(1, 2 * mm),
        _paragraph(f"合同总金额：￥{_money(contract.total_amount)}", styles["right"]),
    ]
    if contract.remarks:
        story.extend(
            [
                Spacer(1, 1 * mm),
                Paragraph(
                    f"<b>合同备注：</b>{_xml_text(contract.remarks)}",
                    styles["small"],
                ),
            ]
        )
    if len(contract.items) > 4:
        story.append(PageBreak())
    story.append(
        KeepTogether(_terms_and_signatures(contract, company, styles))
    )

    def canvas_maker(*args, **kwargs) -> _ContractCanvas:
        return _ContractCanvas(
            *args,
            font_name=font_name,
            contract_version=contract.version,
            **kwargs,
        )

    doc.build(story, canvasmaker=canvas_maker)
    content = output.getvalue()
    return ContractPdfDocument(
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
        template_version=CONTRACT_PDF_TEMPLATE_VERSION,
        font_sha256=font_sha256,
    )
