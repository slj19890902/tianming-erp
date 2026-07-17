from __future__ import annotations

from copy import deepcopy

from app.api.orders import _finalize_pdf_preview_for_user
from app.models.user import User
from app.services.pdf_preview_redaction import redact_pdf_preview_for_user


def _draft() -> dict:
    return {
        "source_name": "客户订单.pdf",
        "items": [
            {
                "product_name": "客户纸箱 A",
                "specification": "300×200×150mm",
                "unit_price": "18.11",
                "product_default_price": "17.50",
                "customer_unit_price": "18.11",
                "estimated_cost": "8.20",
                "cost_status": "calculated",
                "estimated_gross_profit": "9.91",
                "board_square_price": "2.45",
                "supplier_quote_price": "2.50",
                "price_components": [{"paper": "1.20"}],
                "cost_candidates": [{"estimated_cost": "8.10"}],
                "product_candidates": [
                    {
                        "product_name": "匹配候选 A",
                        "sale_unit_price": "17.50",
                        "cost_unit_price": "8.00",
                    }
                ],
                "match_evidence": {"decision": "matched", "top_score": 100},
                "standard_match": {
                    "matched": True,
                    "standard_product_name": "标准纸箱 A",
                    "standard_material_label": "D4B｜供应商甲｜120/120/120｜2.45",
                },
                "nested": {
                    "ordinary_price": "仍是客户价格字段",
                    "supplier_price": "2.30",
                    "supplier": {"quote": "2.25"},
                    "children": [
                        {
                            "cost_status": "pending",
                            "product_default_price": "17.50",
                        }
                    ],
                },
            }
        ],
    }


def test_redacts_internal_costs_recursively_but_keeps_pdf_and_sales_evidence() -> None:
    redacted = redact_pdf_preview_for_user(_draft(), can_view_cost=False)
    item = redacted["items"][0]

    for key in (
        "estimated_cost",
        "cost_status",
        "estimated_gross_profit",
        "board_square_price",
        "supplier_quote_price",
        "price_components",
        "cost_candidates",
    ):
        assert key not in item
    assert "cost_unit_price" not in item["product_candidates"][0]
    assert "supplier_price" not in item["nested"]
    assert "quote" not in item["nested"]["supplier"]
    assert "cost_status" not in item["nested"]["children"][0]

    assert item["unit_price"] == "18.11"
    assert item["product_default_price"] == "17.50"
    assert item["customer_unit_price"] == "18.11"
    assert item["nested"]["ordinary_price"] == "仍是客户价格字段"
    assert item["nested"]["children"][0]["product_default_price"] == "17.50"
    assert item["product_name"] == "客户纸箱 A"
    assert item["specification"] == "300×200×150mm"
    assert item["match_evidence"] == {"decision": "matched", "top_score": 100}
    assert item["product_candidates"][0]["sale_unit_price"] == "17.50"
    assert item["standard_match"]["standard_material_label"] == "D4B｜供应商甲｜120/120/120"


def test_authorized_user_gets_an_equal_independent_copy() -> None:
    draft = _draft()

    visible = redact_pdf_preview_for_user(draft, can_view_cost=True)

    assert visible == draft
    assert visible is not draft
    assert visible["items"] is not draft["items"]
    assert visible["items"][0] is not draft["items"][0]


def test_redaction_never_mutates_the_source_draft() -> None:
    draft = _draft()
    original = deepcopy(draft)

    redacted = redact_pdf_preview_for_user(draft, can_view_cost=False)

    assert draft == original
    assert draft["items"][0]["estimated_cost"] == "8.20"
    assert draft["items"][0]["standard_match"]["standard_material_label"].endswith("2.45")
    assert redacted is not draft


def _user(role: str) -> User:
    return User(
        username=f"pdf-{role}",
        password_hash="unused",
        role=role,
        real_name=f"PDF {role}",
        is_active=True,
        must_change_password=False,
        customer_access_mode="all",
        ui_mode="standard",
    )


def test_api_finalizer_enforces_cost_permission_and_keeps_safety_token() -> None:
    draft = _draft()

    sales_payload = _finalize_pdf_preview_for_user(draft, _user("sales"))
    finance_payload = _finalize_pdf_preview_for_user(draft, _user("finance"))

    assert sales_payload["preview_safety_token"]
    assert "estimated_cost" not in sales_payload["items"][0]
    assert sales_payload["items"][0]["unit_price"] == "18.11"
    assert finance_payload["preview_safety_token"]
    assert finance_payload["items"][0]["estimated_cost"] == "8.20"
    assert draft["items"][0]["estimated_cost"] == "8.20"
