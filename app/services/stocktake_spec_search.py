"""Ordered product dimensions for identification, not material substitution."""
import re
from decimal import Decimal, ROUND_DOWN


def parse_dimensions(keyword):
    value = re.sub(r"\s+", "", keyword).lower()
    value = value.removesuffix("毫米").removesuffix("mm")
    if not re.fullmatch(r"\d+(?:\.\d+)?(?:[x×*＊]\d+(?:\.\d+)?){1,2}", value):
        return None
    dimensions = tuple(Decimal(part) for part in re.split(r"[x×*＊]", value))
    return dimensions if all(dimension > 0 for dimension in dimensions) else None


def dimension_score(dimensions, product):
    actual = (product.length_mm, product.width_mm, product.height_mm)[:len(dimensions)]
    if any(value is None or value <= 0 for value in actual):
        return None
    score = sum(min(wanted, value) / max(wanted, value) for wanted, value in zip(dimensions, actual)) / len(dimensions) * 100
    return float(score.quantize(Decimal("0.01"), rounding=ROUND_DOWN))
