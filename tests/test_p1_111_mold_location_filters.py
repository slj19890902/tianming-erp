from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
import re

from fastapi.testclient import TestClient

from test_mold_tool_workflow import _login, mold_app


def _seed_filter_matrix(factory) -> dict[str, int]:
    from app.models.customer import Customer
    from app.models.mold_tool import (
        MoldLabelPrintJob,
        MoldLabelPrintJobItem,
        MoldTool,
        MoldToolCustomer,
    )
    from app.models.product import Product
    from app.models.user import User

    with factory() as db:
        first_customer = db.get(Customer, 1)
        assert first_customer is not None
        first_customer.chinese_short_name = "模联"
        second_customer = Customer(
            customer_number=9902,
            customer_code="JSD",
            name="聚晟达纸品有限公司",
            chinese_short_name="聚晟达",
            payment_term_days=30,
            credit_limit=Decimal("100000"),
        )
        db.add(second_customer)
        db.flush()
        admin = db.query(User).filter(User.username == "admin").one()

        rows = {
            "printed": MoldTool(
                mold_code="P111-PRINTED",
                mold_name="聚晟达组合筛选模具",
                rack_location="1F-M-R02-L1-G01",
                created_at=datetime(2026, 8, 20, 8, 0),
                updated_at=datetime(2026, 8, 25, 9, 0),
            ),
            "unprinted": MoldTool(
                mold_code="P111-UNPRINTED",
                mold_name="聚晟达未打印模具",
                rack_location="1F-M-R02-L1-G02",
                created_at=datetime(2026, 8, 20, 8, 0),
                updated_at=datetime(2026, 8, 26, 9, 0),
            ),
            "repair": MoldTool(
                mold_code="P111-REPAIR",
                mold_name="聚晟达待维修模具",
                rack_location="1F-M-R02-L1-G03",
                repair_status="needs_repair",
                created_at=datetime(2026, 8, 20, 8, 0),
                updated_at=datetime(2026, 8, 27, 9, 0),
            ),
            "archived": MoldTool(
                mold_code="P111-ARCHIVED",
                mold_name="聚晟达封存模具",
                rack_location="3F-M-ARCHIVE-AB2-N",
                is_active=False,
                archive_status="archived",
                archived_at=datetime(2026, 8, 24, 9, 0),
                archived_by=admin.id,
                archive_reason="unbound",
                pre_archive_location="1F-M-R02-L1-G04",
                created_at=datetime(2026, 8, 20, 8, 0),
                updated_at=datetime(2026, 8, 24, 9, 0),
            ),
            "other": MoldTool(
                mold_code="P111-OTHER",
                mold_name="模联正常模具",
                rack_location="1F-M-R01-L1-G01",
                created_at=datetime(2026, 8, 21, 8, 0),
                updated_at=None,
            ),
        }
        db.add_all(rows.values())
        db.flush()

        for key in ("printed", "unprinted", "repair", "archived"):
            db.add(
                MoldToolCustomer(
                    mold_tool_id=rows[key].id,
                    customer_id=second_customer.id,
                    display_order=1,
                )
            )
        db.add(
            MoldToolCustomer(
                mold_tool_id=rows["other"].id,
                customer_id=first_customer.id,
                display_order=1,
            )
        )

        product_specs = (
            ("printed", second_customer.id, "INV-778-A"),
            ("unprinted", second_customer.id, "INV-778-B"),
            ("repair", second_customer.id, "INV-778-C"),
            ("archived", second_customer.id, "INV-778-D"),
            ("other", first_customer.id, "OTHER-001"),
        )
        for key, customer_id, code in product_specs:
            db.add(
                Product(
                    customer_id=customer_id,
                    product_code=code,
                    customer_material_code=f"CM-{code}",
                    product_name=f"{code} 常用箱",
                    mold_tool_id=rows[key].id,
                )
            )

        printed_keys = ("printed", "repair", "archived", "other")
        job = MoldLabelPrintJob(
            idempotency_key="p1-111-printed-matrix",
            source="batch",
            item_count=len(printed_keys),
            template_version="mold_80x40_v1",
            printed_by=admin.id,
            printed_by_username=admin.username,
            printed_at=datetime(2026, 8, 27, 10, 0),
        )
        db.add(job)
        db.flush()
        for order, key in enumerate(printed_keys, start=1):
            mold = rows[key]
            db.add(
                MoldLabelPrintJobItem(
                    print_job_id=job.id,
                    mold_tool_id=mold.id,
                    item_order=order,
                    mold_code_snapshot=mold.mold_code,
                    rack_location_snapshot=mold.rack_location,
                )
            )
        db.commit()
        return {
            **{key: row.id for key, row in rows.items()},
            "customer_1": first_customer.id,
            "customer_2": second_customer.id,
        }


def _list_ids(client: TestClient, **params) -> list[int]:
    response = client.get(
        "/api/warehouse/molds",
        params={"page": 1, "page_size": 100, **params},
    )
    assert response.status_code == 200, response.text
    return [row["id"] for row in response.json()["items"]]


def test_mold_page_filters_hide_exception_states_by_default_and_opt_in(
    mold_app,
) -> None:
    app, factory = mold_app
    seeded = _seed_filter_matrix(factory)
    defaults = {
        "include_unprinted": False,
        "include_repair": False,
        "include_inactive": False,
        "include_archived": False,
        "sort_by": "updated_desc",
    }

    with TestClient(app) as client:
        _login(client, "admin")
        assert _list_ids(client, **defaults) == [
            seeded["printed"],
            seeded["other"],
        ]
        assert _list_ids(client, **{**defaults, "include_unprinted": True}) == [
            seeded["unprinted"],
            seeded["printed"],
            seeded["other"],
        ]
        assert _list_ids(client, **{**defaults, "include_repair": True}) == [
            seeded["repair"],
            seeded["printed"],
            seeded["other"],
        ]
        assert _list_ids(
            client,
            **{
                **defaults,
                "include_inactive": True,
                "include_archived": True,
            },
        ) == [seeded["printed"], seeded["archived"], seeded["other"]]


def test_mold_combined_dimensions_are_intersected_and_customer_scoped(
    mold_app,
) -> None:
    from app.models.access_control import UserCustomerScope
    from app.models.user import User

    app, factory = mold_app
    seeded = _seed_filter_matrix(factory)
    params = {
        "customer_keyword": "聚晟达",
        "customer_ids": str(seeded["customer_2"]),
        "product_code": "INV-778-A",
        "rack_location": "R02-L1-G01",
        "include_unprinted": False,
        "include_repair": False,
        "include_inactive": False,
        "include_archived": False,
        "sort_by": "updated_desc",
    }
    with TestClient(app) as client:
        _login(client, "admin")
        assert _list_ids(client, **params) == [seeded["printed"]]
        assert _list_ids(client, **{**params, "customer_keyword": "JSD"}) == [
            seeded["printed"]
        ]
        assert set(
            _list_ids(
                client,
                q="jushengda",
                customer_ids=str(seeded["customer_2"]),
            )
        ) == {
            seeded["printed"],
            seeded["unprinted"],
            seeded["repair"],
            seeded["archived"],
        }

    with factory() as db:
        sales = db.query(User).filter(User.username == "sales").one()
        sales.customer_access_mode = "selected"
        db.add(
            UserCustomerScope(
                user_id=sales.id,
                customer_id=seeded["customer_1"],
            )
        )
        db.commit()

    with TestClient(app) as client:
        _login(client, "sales")
        assert _list_ids(client, **params) == []


def test_mold_location_frontend_uses_compact_intersection_filters_and_two_lines() -> None:
    source = (Path(__file__).resolve().parents[1] / "static" / "warehouse.html").read_text(
        encoding="utf-8"
    )
    for marker in (
        'id="moldCustomerFilter"',
        'id="moldProductCodeFilter"',
        'id="moldRackFilter"',
        'id="moldIncludeUnprinted"',
        'id="moldIncludeInactive"',
        'id="moldIncludeRepair"',
        "customer_keyword=",
        "product_code=",
        "rack_location=",
        "include_unprinted=",
        "include_repair=",
        "include_archived=",
        "sort_by=updated_desc",
        'value="mold_80x40_v1" selected',
        "compact-lines-2",
        "最近编辑",
        "row.updated_at||row.created_at",
        'class="mold-action-group"',
    ):
        assert marker in source

    for checkbox in (
        "moldIncludeUnprinted",
        "moldIncludeInactive",
        "moldIncludeRepair",
    ):
        tag = re.search(rf'<input[^>]+id="{checkbox}"[^>]*>', source)
        assert tag is not None
        assert "checked" not in tag.group(0)

    customer_candidates = source.split(
        "async function renderMoldCustomerCandidates(){", 1
    )[1].split("function setMoldFormMode", 1)[0]
    assert "if(!keyword)" in customer_candidates
    assert "输入客户中文、简称、编码或拼音后显示候选" in customer_candidates
    assert "if(!keyword)return true" not in customer_candidates

    render = source.split("function renderMolds(){", 1)[1].split(
        "function renderMoldPager", 1
    )[0]
    assert render.count("compact-lines-2") >= 3
    assert '<span class="tag blue">启用</span>' not in render
    assert "'<span class=\"tag\">正常</span>'" not in render
    assert render.index("${detail}${repairAction}${archiveAction}") < render.index(
        "${label}${edit}${legacyRestore}"
    )
