"""Conservative Jushengda purchase-order text adapter.

The text layer loses the boundary between an empty material-code cell and the
remarks cell. Keep that tail as evidence, never manufacture a product identity.
This adapter produces a reviewable draft; manual source review remains required.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, localcontext


CUSTOMER = "苏州聚晟达电子材料科技有限公司"
TEMPLATE = "jushengda_purchase_v1"
NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
ROW = re.compile(
    rf"(?P<line>\d+)\s+(?P<description>.+?)\s+"
    rf"(?P<quantity>{NUMBER})\s+(?P<unit>PCS|个|只|片|套|件|张)\s+"
    rf"(?P<price>{NUMBER})\s+(?P<amount>{NUMBER})(?:\s+(?P<tail>.*))?",
    re.IGNORECASE,
)
HEADER = "序号品名及规格数量单位含税单价金额(RMB)料号备注"


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value).replace("（", "(").replace("）", ")")


def _number(value: str) -> Decimal:
    return Decimal(value.replace(",", ""))


def _date_field(text: str, label: str, errors: list[str]) -> str | None:
    values = re.findall(re.escape(label) + r"\s*[:：]\s*([^\n]*)", text)
    parsed = []
    for value in values:
        match = re.fullmatch(
            r"(20\d{2})(?:年|[-/.])(\d{1,2})(?:月|[-/.])(\d{1,2})日?",
            _compact(value),
        )
        try:
            parsed.append(date(*map(int, match.groups())).isoformat() if match else None)
        except ValueError:
            parsed.append(None)
    if len(parsed) != 1 or parsed[0] is None:
        errors.append(f"{label}缺失、重复或无效，请核对原单。")
        return None
    return parsed[0]


def parse_jushengda_order(text: str, source_name: str | None, route: dict) -> dict | None:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]
    # Buyer must be the document heading, never the supplier/footer or filename.
    if not lines or _compact(lines[0]) != CUSTOMER:
        return None
    if len(lines) < 2 or _compact(lines[1]) != "采购单":
        return None

    errors: list[str] = []
    warnings = ["料号与备注的文本列边界未验证，请对照原单逐行选择常用箱，不按规格或单价自动匹配。"]
    final_route = dict(route)
    route_conflict = route.get("status") == "needs_confirmation" or (
        route.get("status") == "locked"
        and _compact(str(route.get("customer_name") or "")) != CUSTOMER
    )
    if route_conflict:
        errors.append("原单买方与客户模板绑定冲突，请核对客户；供应商不能作为买方。")
        final_route.update(status="needs_confirmation", template_customer_id=None)
    else:
        final_route.update(status="locked", customer_name=CUSTOMER,
                           customer_type="jushengda", parser_key="jushengda")

    po_values = re.findall(r"订单号码\s*[:：]\s*([^\s]+)", text)
    po = po_values[0] if len(po_values) == 1 and re.fullmatch(r"JSD\d{11}", po_values[0]) else None
    if po is None:
        errors.append("订单号码缺失、重复或不符合 JSD 原单格式，不能从文件名补造。")
    order_date = _date_field(text, "下单日期", errors)
    delivery_date = _date_field(text, "交货日期", errors)
    if order_date and delivery_date and delivery_date < order_date:
        errors.append("交货日期早于下单日期，请核对原单。")

    supplier = re.search(r"供应商名称\s*[:：]\s*(.*?)\s+订单号码", text)
    rates = re.findall(r"税率\s*[:：]\s*(\d+(?:\.\d+)?)\s*%", text)
    currencies = re.findall(r"币别\s*[:：]\s*(\S+)", text)
    tax_rate = rates[0] if len(rates) == 1 and Decimal(rates[0]) <= 100 else None
    currency = currencies[0] if len(currencies) == 1 and currencies[0] == "RMB" else None
    if tax_rate is None or currency is None:
        errors.append("税率或币别缺失、冲突或不受支持，请核对原单。")

    headers = [index for index, line in enumerate(lines) if _compact(line) == HEADER]
    totals = [index for index, line in enumerate(lines)
              if re.match(r"^总计\(金额\)[:：]", _compact(line))]
    if len(headers) != 1 or len(totals) != 1 or (headers and totals and totals[0] <= headers[0]):
        errors.append("明细表头或总计边界缺失、重复或顺序异常，不能确认整单完整。")
    body = lines[headers[0] + 1:totals[0]] if len(headers) == len(totals) == 1 and totals[0] > headers[0] else []
    notes = [line for line in body if re.match(r"^备注\s*[:：]", line)]
    source_lines: list[int] = []
    unparsed: list[str] = []
    items: list[dict] = []
    for line in body:
        if line in notes:
            continue
        sequence = re.match(r"^(\d+)\s+", line)
        if sequence:
            source_lines.append(int(sequence.group(1)))
        match = ROW.fullmatch(line)
        if not match:
            unparsed.append(line)
            errors.append(f"明细无法完整解析，可能漏行或字段断行：{line[:100]}")
            continue
        values = match.groupdict()
        line_no = int(values["line"])
        if any(len(values[key]) > 24 for key in ("quantity", "price", "amount")):
            unparsed.append(line)
            errors.append(f"第{line_no}行数字过长，无法可靠核验，请核对原单。")
            continue
        quantity, price, amount = (_number(values[key]) for key in ("quantity", "price", "amount"))
        if quantity <= 0 or price <= 0 or amount <= 0:
            errors.append(f"第{line_no}行数量、含税单价或金额必须大于零。")
        # Verify the printed monetary value; never replace it with a calculation.
        with localcontext() as context:
            context.prec = 64
            expected = (quantity * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if amount != expected:
            errors.append(f"第{line_no}行数量×含税单价与金额不符：{quantity}×{price}≠{amount}。")
        description = values["description"]
        dimension = re.fullmatch(r"\d+(?:\.\d+)?(?:\s*[*×xX]\s*\d+(?:\.\d+)?){1,2}\s*(?:MM|CM)", description, re.I)
        if not dimension:
            errors.append(f"第{line_no}行品名及规格布局尚未验证，请对照原单核对。")
        tail = values["tail"] or ""
        if ROW.search(tail):
            errors.append(f"第{line_no}行尾部含另一条疑似明细，可能文本粘行，请对照原单补齐。")
        production_notes = "\n".join(filter(None, [f"原单料号/备注：{tail}" if tail else "", *notes]))
        items.append({
            "line_no": line_no, "product_code": "", "raw_product_code": "",
            "product_name": description, "raw_product_name": description,
            "specification": description, "raw_spec_model": description, "size_spec": description,
            "quantity": int(quantity) if quantity == quantity.to_integral_value() else str(quantity),
            "raw_quantity": values["quantity"], "unit": values["unit"],
            "unit_price": str(price), "amount": str(amount), "raw_unit_price": values["price"],
            "raw_amount": values["amount"], "delivery_date": delivery_date,
            "raw_code_and_remark": tail, "production_notes": production_notes,
            "raw_lines": [line], "preserve_pdf_price": True,
            "matched_product_id": None, "warnings": [warnings[0]],
        })
    if not items:
        errors.append("未能完整识别任何明细，请核对原单。")
    if source_lines != list(range(1, len(source_lines) + 1)):
        errors.append("原单明细序号重复、不连续或缺失，请核对全部明细。")
    if len(source_lines) != len(items):
        errors.append("原文明细行数与已解析明细行数不一致。")
    if totals and any(ROW.fullmatch(line) for line in lines[totals[0] + 1:]):
        errors.append("总计之后仍有疑似订单明细，可能跨页或合单，请核对全部页面。")

    total_amount = None
    if len(totals) == 1:
        total_text = re.split(r"[:：]", _compact(lines[totals[0]]), maxsplit=1)[1]
        if total_text:
            if len(total_text) <= 24 and re.fullmatch(NUMBER, total_text):
                total_amount = _number(total_text)
            else:
                errors.append("原单总计金额无法可靠解析，请核对。")
    parsed_quantity = sum((_number(str(item["quantity"])) for item in items), Decimal("0"))
    parsed_amount = sum((_number(item["amount"]) for item in items), Decimal("0"))
    if total_amount is None:
        warnings.append("原单总计金额为空或未可靠识别，不能把明细加总冒充原单总计，请人工核对。")
    elif total_amount != parsed_amount:
        errors.append("原单总计金额与识别明细金额之和不符。")
    warnings.append("原单未列独立合计数量；本格式须对照 PDF 完成人工核对后保存。")
    return {
        "source_name": source_name or "uploaded.pdf", "source_type": "purchase_order_pdf",
        "source_format": "jushengda_purchase", "template": TEMPLATE,
        "customer_name_raw": CUSTOMER, "customer_name": CUSTOMER, "customer_type": "jushengda",
        "supplier_name_raw": supplier.group(1).strip() if supplier else None,
        "customer_po": po, "order_date": order_date, "delivery_date": delivery_date,
        "tax_rate": tax_rate, "currency": currency, "notes": "\n".join(notes),
        "items": items, "item_count": len(items), "is_tianhua": False,
        "recognition_status": "needs_confirmation", "parse_status": "needs_confirmation",
        "message": "聚晟达采购单已提取，请核对原单并选择常用箱。",
        "duplicate_status": None, "duplicate_reason": None, "warnings": errors + warnings,
        "customer_route": final_route, "raw_unparsed_lines": unparsed,
        "integrity_check": {
            "source_detail_count": len(source_lines), "parsed_detail_count": len(items),
            "source_line_numbers": source_lines, "parsed_line_numbers": [item["line_no"] for item in items],
            "source_total_quantity": None, "parsed_total_quantity": str(parsed_quantity),
            "quantity_total_diff": None,
            "source_total_amount": str(total_amount) if total_amount is not None else None,
            "parsed_total_amount": str(parsed_amount),
            "amount_total_diff": str(abs(total_amount - parsed_amount)) if total_amount is not None else None,
            "integrity_status": "failed" if errors else "unknown",
            "integrity_errors": errors, "integrity_warnings": warnings,
        },
    }
