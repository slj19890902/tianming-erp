"""Explain rectangular nesting without inventing die-cut waste or stock facts."""
from decimal import Decimal, InvalidOperation


def explain_sheet(row):
    plan = row.get('cut_plan') or {}
    # An exact processed blank can have unknown die-cut shape. A percentage
    # must come from the same geometric plan used for production.
    result = dict(utilization_percent=None, waste_percent=None,
                  waste_area_cm2=None, waste_basis='unknown', match_color='unknown')
    try:
        source = Decimal(str(plan['source_length_mm'])) * Decimal(str(plan['source_width_mm']))
        used = (Decimal(str(plan['target_length_mm'])) * Decimal(str(plan['target_width_mm']))
                * int(plan['rows']) * int(plan['columns']))
        if source <= 0 or used <= 0 or used > source:
            return result
        score = used * 100 / source
        waste = source - used
        result.update(utilization_percent=float(score.quantize(Decimal('.01'))),
                      waste_percent=float((100-score).quantize(Decimal('.01'))),
                      waste_area_cm2=float((waste/100).quantize(Decimal('.01'))),
                      waste_basis='rectangular_nesting',
                      match_color='green' if score >= 90 else 'yellow' if score >= 70 else 'orange')
    except (KeyError, TypeError, ValueError, InvalidOperation, ArithmeticError):
        pass
    return result
