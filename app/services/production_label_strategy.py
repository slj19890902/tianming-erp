from __future__ import annotations

from app.models.product import Product
from app.models.production import ProductionTask
from app.services.box_type_rules import box_type_code


DEFAULT_UNITS_PER_LABEL_BY_BOX_TYPE = {
    "a1_0201": 5,
    "die_cut_inner_box": 50,
    "liner": 50,
    "divider": 50,
    "die_cut_partition": 50,
}


LEGACY_PRODUCTION_LABEL_TEMPLATE_VERSION = "legacy_65x45_v1"
CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION = "current_40x30_v2"


class ProductionLabelStrategyError(ValueError):
    """Raised when a production packaging-label strategy is incomplete."""


def _positive_integer(value: object, *, field: str) -> int:
    if isinstance(value, bool):
        raise ProductionLabelStrategyError(f"{field}必须是正整数")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ProductionLabelStrategyError(f"{field}必须是正整数") from exc
    if number <= 0 or str(value).strip() not in {str(number), f"{number}.0"}:
        raise ProductionLabelStrategyError(f"{field}必须是正整数")
    return number


def _nonnegative_integer(value: object, *, field: str) -> int:
    if isinstance(value, bool):
        raise ProductionLabelStrategyError(f"{field}必须是非负整数")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ProductionLabelStrategyError(f"{field}必须是非负整数") from exc
    if number < 0 or str(value).strip() not in {str(number), f"{number}.0"}:
        raise ProductionLabelStrategyError(f"{field}必须是非负整数")
    return number


def normalize_production_label_strategy(
    *,
    box_style: str | None,
    enabled: bool,
    units_per_label: object | None,
) -> tuple[bool, int | None]:
    """Normalize one product strategy without guessing unsupported box types."""

    if not enabled:
        return False, None
    if units_per_label is None:
        units_per_label = DEFAULT_UNITS_PER_LABEL_BY_BOX_TYPE.get(
            box_type_code(box_style)
        )
        if units_per_label is None:
            raise ProductionLabelStrategyError(
                "该箱型没有默认每张标签数量，请填写正整数"
            )
    return True, _positive_integer(units_per_label, field="每张标签数量")


def build_new_task_production_label_snapshot(
    product: Product | None,
    *,
    total_quantity: object,
) -> dict[str, object]:
    """Freeze product strategy only when a new ProductionTask is created."""

    frozen_total = _nonnegative_integer(total_quantity, field="标签成品总数")
    product_version = int(product.version or 1) if product is not None else None
    if (
        product is None
        or not bool(product.production_label_enabled)
        or frozen_total == 0
    ):
        return {
            "production_label_enabled_snapshot": False,
            "production_label_units_per_label_snapshot": None,
            "production_label_total_quantity_snapshot": 0,
            "production_label_count_snapshot": 0,
            "production_label_template_version_snapshot": (
                CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION
            ),
            "production_label_product_version_snapshot": product_version,
        }
    enabled, units_per_label = normalize_production_label_strategy(
        box_style=product.box_style,
        enabled=True,
        units_per_label=product.production_label_units_per_label,
    )
    assert units_per_label is not None
    return {
        "production_label_enabled_snapshot": enabled,
        "production_label_units_per_label_snapshot": units_per_label,
        "production_label_total_quantity_snapshot": frozen_total,
        "production_label_count_snapshot": (
            frozen_total + units_per_label - 1
        )
        // units_per_label,
        "production_label_template_version_snapshot": (
            CURRENT_PRODUCTION_LABEL_TEMPLATE_VERSION
        ),
        "production_label_product_version_snapshot": product_version,
    }


def production_label_quantities(task: ProductionTask) -> list[int]:
    """Return frozen per-label quantities; never derive them from live product data."""

    if not bool(task.production_label_enabled_snapshot):
        return []
    units_per_label = _positive_integer(
        task.production_label_units_per_label_snapshot,
        field="生产任务每张标签数量",
    )
    total_quantity = _positive_integer(
        task.production_label_total_quantity_snapshot,
        field="生产任务标签成品总数",
    )
    expected_count = (total_quantity + units_per_label - 1) // units_per_label
    if int(task.production_label_count_snapshot or 0) != expected_count:
        raise ProductionLabelStrategyError("生产任务包装标签快照不完整")
    return [
        min(units_per_label, total_quantity - index * units_per_label)
        for index in range(expected_count)
    ]
