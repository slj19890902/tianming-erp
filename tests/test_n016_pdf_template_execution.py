from __future__ import annotations

import json
import time

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.pdf_training import TemplateCreate, _validate_template_rule_payload
from app.models import Base
from app.models.customer import Customer
from app.models.pdf_training import PdfOrderCustomerTemplate
from app.services.order_pdf_import import parse_purchase_order_text, resolve_pdf_customer_route
from app.services.pdf_customer_templates import load_active_pdf_template_rules
from app.services.template_regex import safe_regex_finditer, safe_regex_search


CONFIGURED_TEXT = """昆山新客户有限公司
客户单号=NX@2026@0717@09
下单日期=2026年7月17日
10|ZX-1001|防静电纸箱|300*200*150|个|12.000|3.25|39.00|2026/07/20
20|ZX-1002|隔板|280*180|片|6|0.80|4.80|2026/07/22
合计 18 43.80
"""


def _configured_rule(**overrides) -> dict:
    rule = {
        "template_id": 91,
        "template_name": "新客户配置模板",
        "customer_id": 51,
        "customer_name": "昆山新客户有限公司",
        "customer_type": "generic",
        "template_customer_valid": True,
        "order_no_pattern": r"客户单号=(?P<order_no>[^\s]+)",
        "date_pattern": r"下单日期=(?P<order_date>20\d{2}年\d{1,2}月\d{1,2}日)",
        "item_row_pattern": (
            r"^(?P<row>\d+)\|(?P<sku>[^|\r\n]+)\|(?P<name>[^|\r\n]+)\|"
            r"(?P<size>[^|\r\n]+)\|(?P<uom>[^|\r\n]+)\|(?P<qty>[^|\r\n]+)\|"
            r"(?P<price>[^|\r\n]+)\|(?P<total>[^|\r\n]+)\|(?P<due>[^|\r\n]+)$"
        ),
        "field_mapping": {
            "line_no": "row",
            "product_code": "sku",
            "product_name": "name",
            "spec": "size",
            "unit": "uom",
            "quantity": "qty",
            "unit_price": "price",
            "amount": "total",
            "delivery_date": "due",
        },
    }
    rule.update(overrides)
    return rule


def test_active_generic_template_extracts_header_date_mapped_items_and_totals() -> None:
    result = parse_purchase_order_text(CONFIGURED_TEXT, template_rules=[_configured_rule()])

    assert result["customer_route"]["status"] == "locked"
    assert result["customer_route"]["template_customer_id"] == 51
    assert result["template_name"] == "新客户配置模板"
    assert result["customer_po"] == "NX@2026@0717@09"
    assert result["order_date"] == "2026-07-17"
    assert result["item_count"] == 2
    assert result["recognition_status"] == "recognized"
    assert result["integrity_check"]["integrity_status"] == "passed"
    assert result["integrity_check"]["source_total_quantity"] == "18"
    assert result["integrity_check"]["parsed_total_amount"] == "43.80"

    first = result["items"][0]
    assert first["line_no"] == 10
    assert first["product_code"] == first["raw_product_code"] == "ZX-1001"
    assert first["product_name"] == "防静电纸箱"
    assert first["specification"] == "300*200*150"
    assert first["quantity"] == 12
    assert first["raw_quantity"] == "12.000"
    assert first["unit_price"] == "3.2500"
    assert first["amount"] == "39.00"
    assert first["delivery_date"] == "2026-07-20"


def test_configured_template_without_independent_totals_stays_integrity_unknown() -> None:
    result = parse_purchase_order_text(
        CONFIGURED_TEXT.replace("合计 18 43.80\n", ""),
        template_rules=[_configured_rule()],
    )

    assert result["item_count"] == 2
    assert result["integrity_check"]["integrity_status"] == "unknown"
    assert result["recognition_status"] == "needs_confirmation"
    assert any("独立的合计数量和合计金额" in warning for warning in result["warnings"])


def test_configured_template_with_page_subtotals_never_self_certifies_integrity() -> None:
    text = CONFIGURED_TEXT.replace(
        "合计 18 43.80\n",
        "合计 12 39.00\n分页\n合计 6 4.80\n",
    )

    result = parse_purchase_order_text(text, template_rules=[_configured_rule()])

    assert result["item_count"] == 2
    assert result["integrity_check"]["integrity_status"] == "unknown"
    assert result["recognition_status"] == "needs_confirmation"
    assert result["integrity_check"]["source_detail_count"] == 2
    assert result["integrity_check"]["source_line_numbers"] == ["10", "20"]
    assert any("多个合计或分页小计" in warning for warning in result["warnings"])


def test_single_subtotal_before_unmatched_source_row_fails_integrity() -> None:
    text = CONFIGURED_TEXT + (
        "30 ZX-1003 后续漏识别纸箱 320*220*160 个 2 1.00 2.00 2026/07/25\n"
    )

    result = parse_purchase_order_text(text, template_rules=[_configured_rule()])

    assert result["item_count"] == 2
    assert result["integrity_check"]["integrity_status"] == "failed"
    assert result["integrity_check"]["missing_line_numbers"] == ["30"]
    assert any("疑似分页小计或漏行" in warning for warning in result["warnings"])


def test_split_source_row_after_subtotal_cannot_escape_integrity_check() -> None:
    text = CONFIGURED_TEXT + (
        "30 ZX-1003 后续漏识别纸箱 320*220*160 个 2 1.00 2.00\n"
        "2026/07/25\n"
    )

    result = parse_purchase_order_text(text, template_rules=[_configured_rule()])

    assert result["item_count"] == 2
    assert result["integrity_check"]["integrity_status"] == "failed"
    assert result["integrity_check"]["missing_line_numbers"] == ["30"]


def test_configured_template_rejects_impossible_calendar_date() -> None:
    text = CONFIGURED_TEXT.replace("2026/07/20", "2026/02/31")

    result = parse_purchase_order_text(text, template_rules=[_configured_rule()])

    assert result["integrity_check"]["integrity_status"] == "failed"
    assert any("交货日期 2026/02/31 无法解析" in warning for warning in result["warnings"])


def test_legacy_numeric_capture_group_mapping_remains_executable() -> None:
    rule = _configured_rule(
        item_row_pattern=(
            r"^(\d+)\|([^|\r\n]+)\|([^|\r\n]+)\|([^|\r\n]+)\|"
            r"([^|\r\n]+)\|([^|\r\n]+)\|([^|\r\n]+)\|([^|\r\n]+)\|([^|\r\n]+)$"
        ),
        field_mapping={
            "line_no": 1,
            "product_code": 2,
            "product_name": 3,
            "spec": 4,
            "unit": 5,
            "quantity": 6,
            "unit_price": 7,
            "amount": 8,
            "delivery_date": 9,
        },
    )

    result = parse_purchase_order_text(CONFIGURED_TEXT, template_rules=[rule])

    assert result["integrity_check"]["integrity_status"] == "passed"
    assert [item["product_code"] for item in result["items"]] == ["ZX-1001", "ZX-1002"]


def test_locked_configured_template_failure_never_falls_back_to_generic_rows() -> None:
    result = parse_purchase_order_text(
        CONFIGURED_TEXT,
        template_rules=[_configured_rule(item_row_pattern=r"^NEVER-MATCH$")],
    )

    assert result["recognition_status"] == "needs_confirmation"
    assert result["parse_status"] == "needs_confirmation"
    assert result["items"] == []
    assert result["customer_route"]["status"] == "locked"
    assert result["integrity_check"]["integrity_status"] == "failed"
    assert any("客户模板" in warning for warning in result["warnings"])


@pytest.mark.parametrize(
    ("text", "rule", "expected_type", "expected_po"),
    [
        (
            "采购订单\n苏州天华超净科技有限公司\nPO2026071701\n2026-07-17\n"
            "行号 料品编码 物料名称 规格型号 单位 数量 含税单价 价税合计 交货日期\n"
            "10 21301010 纸箱 30*20*10cm 个 10 1.000000 10.00 2026.07.20\n"
            "合计 10 10.00\n",
            {"customer_name": "苏州天华超净科技有限公司", "customer_type": "tianhua_chao"},
            "tianhua_chao",
            "PO2026071701",
        ),
        (
            "苏州高泰电子技术股份有限公司 采购合同\n合同号/PO: 0100-CG260717-01\n"
            "序号 产品编号 名称 规格 数量 单位 单价(含税) 金额 税率 交货期 备注\n"
            "3090078 纸箱 SSII 50 510.00 13.00 2026-7-20\n合计\n",
            {"customer_name": "苏州高泰电子技术股份有限公司", "customer_type": "gaotai"},
            "gaotai",
            "0100-CG260717-01",
        ),
        (
            "苏州思迈尔电子设备有限公司\nP-0029425-2\n"
            "10 18.11 CPN084557 6.00 CNY Pcs 美卡纸箱 73cm*35cm*24.5cm 22/07/2026 108.66 CNY\n",
            {"customer_name": "苏州思迈尔电子设备有限公司", "customer_type": "simair"},
            "simair",
            "P-0029425-2",
        ),
    ],
)
def test_configured_execution_never_overrides_specialized_customer_parsers(
    text: str,
    rule: dict,
    expected_type: str,
    expected_po: str,
) -> None:
    specialized_rule = {
        "template_id": 100,
        "customer_id": 60,
        "template_customer_valid": True,
        "order_no_pattern": r"THIS-WILL-NOT-MATCH",
        "date_pattern": r"THIS-WILL-NOT-MATCH",
        "item_row_pattern": r"THIS-WILL-NOT-MATCH",
        **rule,
    }

    result = parse_purchase_order_text(text, template_rules=[specialized_rule])

    assert result["customer_type"] == expected_type
    assert result["customer_po"] == expected_po
    assert result["item_count"] >= 1


@pytest.mark.parametrize(
    ("text", "expected_type"),
    [
        (
            "苏州高泰电子技术股份有限公司 采购合同\n合同号/PO: 0100-CG260717-01\n"
            "3090078 纸箱 SSII 50 510.00 13.00 2026-7-20\n合计\n",
            "gaotai",
        ),
        (
            "苏州思迈尔电子设备有限公司 采购订单\nP-0029425-2\n"
            "10 18.11 CPN084557 6.00 CNY Pcs 美卡纸箱 73cm*35cm*24.5cm "
            "22/07/2026 108.66 CNY\n",
            "simair",
        ),
    ],
)
def test_broad_generic_template_cannot_steal_specialized_customer(
    text: str,
    expected_type: str,
) -> None:
    broad_generic_rule = {
        **_configured_rule(),
        "template_id": 777,
        "customer_id": 778,
        "customer_name": "其它通用客户",
        "customer_type": "generic",
        "customer_name_pattern": r"采购订单|采购合同",
    }

    result = parse_purchase_order_text(text, template_rules=[broad_generic_rule])

    assert result["customer_type"] == expected_type
    assert result["customer_route"]["parser_key"] != "generic"
    assert result["item_count"] >= 1


def test_tianhua_energy_never_binds_chao_template_customer() -> None:
    text = (
        "采购订单\n苏州天华新能源科技有限公司\nPO2026071701\n2026-07-17\n"
        "行号 料品编码 物料名称 规格型号 单位 数量 含税单价 价税合计 交货日期\n"
        "10 21301010 纸箱 30*20*10cm 个 10 1.000000 10.00 2026.07.20\n"
        "合计 10 10.00\n"
    )
    chao_only_rule = {
        "template_id": 880,
        "customer_id": 881,
        "template_customer_valid": True,
        "customer_name": "苏州天华超净科技有限公司",
        "customer_type": "tianhua_chao",
        "customer_name_pattern": r"天华",
    }

    result = parse_purchase_order_text(text, template_rules=[chao_only_rule])

    assert result["customer_type"] == "tianhua_energy"
    assert result["customer_route"]["template_customer_id"] is None
    assert result["customer_name"] != "苏州天华超净科技有限公司"
    assert result["item_count"] == 1


def test_specialized_order_number_selects_parser_without_binding_typed_template() -> None:
    text = (
        "P-0029425-2\n"
        "10 18.11 CPN084557 6.00 CNY Pcs 美卡纸箱 73cm*35cm*24.5cm "
        "22/07/2026 108.66 CNY\n"
    )
    unrelated_simair_template = {
        "template_id": 920,
        "customer_id": 921,
        "template_customer_valid": True,
        "customer_name": "另一个同解析类型客户",
        "customer_type": "simair",
    }

    route = resolve_pdf_customer_route(text, [unrelated_simair_template])

    assert route["status"] == "locked"
    assert route["parser_key"] == "simair"
    assert route["template_customer_id"] is None
    assert route["customer_name"] is None


def test_isolated_regex_worker_hard_times_out_catastrophic_pattern() -> None:
    started = time.monotonic()

    match, error = safe_regex_search(r"^(a+){10}$", "a" * 5000 + "!")

    assert match is None
    assert error is not None and "执行超时" in error
    assert time.monotonic() - started < 5


def test_customer_route_timeout_never_locks_later_broad_template() -> None:
    slow_pattern = r"^a+a+a+a+a+a+a+a+a+a+b$"
    rules = [
        {
            "template_id": 901,
            "customer_id": 902,
            "customer_name": "正确客户但名称依赖规则",
            "customer_type": "generic",
            "customer_name_pattern": slow_pattern,
        },
        {
            "template_id": 903,
            "customer_id": 904,
            "customer_name": "错误宽泛客户",
            "customer_type": "generic",
            "customer_name_pattern": r"a{10}",
        },
    ]
    started = time.monotonic()

    route = resolve_pdf_customer_route("a" * 5000, rules)

    assert route["status"] == "needs_confirmation"
    assert route["template_customer_id"] is None
    assert route["errors"]
    assert time.monotonic() - started < 5


def test_isolated_regex_worker_caps_total_capture_output() -> None:
    matches, error = safe_regex_finditer(
        r"(?=(?P<blob>.{5000}))",
        "a" * 10_000,
        limit=501,
    )

    assert matches == []
    assert error is not None and "safety limit" in error


def test_only_lifecycle_active_template_is_loaded_for_execution(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'active-only.sqlite3'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as session:
        customer = Customer(name="昆山新客户有限公司")
        session.add(customer)
        session.flush()
        templates = [
            PdfOrderCustomerTemplate(
                customer_id=customer.id,
                template_name="active",
                status="active",
                is_active=True,
                version=1,
                customer_name_pattern="昆山新客户",
                column_map_json=json.dumps({"field_mapping": {"product_code": 1}}),
            ),
            PdfOrderCustomerTemplate(
                customer_id=customer.id,
                template_name="draft",
                status="draft",
                is_active=False,
                version=2,
                customer_name_pattern="草稿客户",
            ),
            PdfOrderCustomerTemplate(
                customer_id=customer.id,
                template_name="retired",
                status="retired",
                is_active=True,
                version=3,
                customer_name_pattern="退役客户",
            ),
        ]
        session.add_all(templates)
        session.commit()
        rules = load_active_pdf_template_rules(session)

    custom_rules = [rule for rule in rules if rule.get("_source") == "database"]
    assert [rule["template_name"] for rule in custom_rules] == ["active"]
    assert custom_rules[0]["field_mapping"] == {"product_code": 1}
    engine.dispose()


def test_template_api_rejects_invalid_regex_json_and_capture_group() -> None:
    with pytest.raises(HTTPException, match="订单号规则无效"):
        _validate_template_rule_payload(
            TemplateCreate(template_name="bad-regex", order_no_pattern="(")
        )
    with pytest.raises(HTTPException, match="字段映射 JSON 无效"):
        _validate_template_rule_payload(
            TemplateCreate(template_name="bad-json", column_map_json="{bad")
        )
    with pytest.raises(HTTPException, match="不存在的命名组 missing"):
        _validate_template_rule_payload(
            TemplateCreate(
                template_name="bad-group",
                item_row_pattern=r"(?P<sku>\S+)",
                column_map_json=json.dumps({"field_mapping": {"product_code": "missing"}}),
            )
        )
    with pytest.raises(HTTPException, match="field_mapping 必须是对象"):
        _validate_template_rule_payload(
            TemplateCreate(
                template_name="bad-mapping-type",
                column_map_json=json.dumps({"field_mapping": []}),
            )
        )
    with pytest.raises(HTTPException, match="不安全正则结构"):
        _validate_template_rule_payload(
            TemplateCreate(template_name="catastrophic", customer_name_pattern=r"^(a+)+$")
        )
    with pytest.raises(HTTPException, match="不安全正则结构"):
        _validate_template_rule_payload(
            TemplateCreate(template_name="bounded-catastrophic", order_no_pattern=r"^(a+){10}$")
        )


def test_shared_pdf_pipeline_executes_same_configured_template(monkeypatch) -> None:
    from app.services import pdf_parse_pipeline

    rules = [_configured_rule()]
    direct = parse_purchase_order_text(CONFIGURED_TEXT, template_rules=rules)
    monkeypatch.setattr(pdf_parse_pipeline, "extract_text_from_pdf_bytes", lambda _content: CONFIGURED_TEXT)
    monkeypatch.setattr(pdf_parse_pipeline, "should_use_ocr", lambda _text, _draft: False)

    pipeline = pdf_parse_pipeline.parse_pdf_bytes(b"not-a-real-pdf", "configured.pdf", rules)

    assert pipeline.draft["customer_po"] == direct["customer_po"]
    assert pipeline.draft["order_date"] == direct["order_date"]
    assert pipeline.draft["items"] == direct["items"]
    assert pipeline.draft["customer_route"] == direct["customer_route"]
