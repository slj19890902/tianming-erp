"""Sheet measurements: retain hundredths, never truncate a physical dimension."""
from decimal import Decimal, InvalidOperation
from typing import Annotated

from pydantic import BeforeValidator


def sheet_dimension_number(value: object) -> int | float:
    if isinstance(value, bool):
        raise ValueError("尺寸必须是有效毫米数")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("尺寸必须是有效毫米数") from error
    if not number.is_finite() or abs(number) >= Decimal("10000000000"):
        raise ValueError("尺寸必须是有限毫米数")
    if number != number.quantize(Decimal("0.01")):
        raise ValueError("卡纸尺寸最多保留两位小数")
    return int(number) if number == number.to_integral_value() else float(number)


SheetDimension = Annotated[int | float, BeforeValidator(sheet_dimension_number)]


def validate_sheet_dimensions(*values: object, layer_count: object, flute_type: object) -> None:
    fractional = any(
        value is not None and not isinstance(sheet_dimension_number(value), int)
        for value in values
    )
    if fractional and not (layer_count == 1 and str(flute_type or "").upper() == "NONE"):
        raise ValueError("瓦楞纸板报料长宽必须为整数毫米；仅卡纸（单层无楞）可填写小数")
