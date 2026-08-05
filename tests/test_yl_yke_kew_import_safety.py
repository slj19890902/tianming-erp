from __future__ import annotations

import pytest

from scripts.admin.import_yl_yke_kew_common_boxes import _customer_master


def _payload(*, yke_number=None, kew_number=None) -> dict:
    return {
        "scope": {
            "customers": [
                {
                    "code": "YKE",
                    "name": "研光",
                    "customer_number": yke_number,
                },
                {
                    "code": "KEW",
                    "name": "光洋",
                    "customer_number": kew_number,
                },
            ]
        }
    }


def test_customer_master_assigns_frozen_positive_numbers_when_manifest_is_blank() -> None:
    customers = _customer_master(_payload())

    assert customers == [
        {"customer_code": "YKE", "name": "研光", "customer_number": 135},
        {"customer_code": "KEW", "name": "光洋", "customer_number": 136},
    ]


def test_customer_master_accepts_exact_frozen_numbers() -> None:
    customers = _customer_master(_payload(yke_number=135, kew_number=136))

    assert [row["customer_number"] for row in customers] == [135, 136]


def test_customer_master_rejects_conflicting_customer_number() -> None:
    with pytest.raises(RuntimeError, match="客户编号与冻结值不一致"):
        _customer_master(_payload(yke_number=999))
