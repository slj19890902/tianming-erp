from __future__ import annotations


MOLD_LABEL_TEMPLATE_40X30 = "mold_40x30_v1"
MOLD_LABEL_TEMPLATE_80X40 = "mold_80x40_v1"
MOLD_LABEL_TEMPLATE_VERSIONS = frozenset(
    {MOLD_LABEL_TEMPLATE_40X30, MOLD_LABEL_TEMPLATE_80X40}
)


def mold_label_template_label(template_version: str) -> str:
    if template_version == MOLD_LABEL_TEMPLATE_40X30:
        return "40×30"
    if template_version == MOLD_LABEL_TEMPLATE_80X40:
        return "40×80"
    raise ValueError("模具标签模板版本无效")
