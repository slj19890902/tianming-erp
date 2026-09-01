from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
import re
from typing import Any

from sqlalchemy.orm import Session

from app.models.order import OrderItem
from app.models.processing_cost import ProcessingCostSettings, ProductProcessingProfile
from app.models.product import Product
from app.services.box_type_rules import box_type_code


RULE_VERSION = "p1-131-standard-processing-v1"
HOUR = Decimal("0.000001")
DAY = Decimal("0.000001")
MONEY = Decimal("0.01")
WORKER_DAY_MONEY = Decimal("0.000001")
_NO_PRINT = frozenset({"", "无", "否", "无印刷", "不印刷"})
_DIE_CUT_CODES = frozenset({"die_cut_inner_box", "die_cut_partition", "divider"})
_JOINING_CODES = frozenset(
    {"a1_0201", "full_flap_carton", "half_slotted_carton", "surround_panel"}
)
_JOINING_MARKERS = ("打钉", "钉箱", "粘贴", "粘箱", "糊箱", "结合")
_NO_JOINING_MARKERS = ("无需结合", "不结合", "无需打钉", "无需粘贴")
_COLOR_SPLIT = re.compile(r"[,，、/＋+;；\s]+")
_ARABIC_COLOR_COUNT = re.compile(r"([1-9]\d*)\s*色")
_CHINESE_COLOR_COUNTS = {
    "单色": 1,
    "一色": 1,
    "双色": 2,
    "二色": 2,
    "三色": 3,
    "四色": 4,
    "五色": 5,
    "六色": 6,
}


def get_processing_cost_settings(db: Session) -> ProcessingCostSettings:
    row = db.get(ProcessingCostSettings, 1)
    if row is None:
        raise ValueError("加工成本参数尚未初始化")
    return row


def get_product_processing_profile(
    db: Session, product_id: int
) -> ProductProcessingProfile | None:
    return db.query(ProductProcessingProfile).filter_by(product_id=product_id).one_or_none()


def calculate_worker_day_cost(
    settings: ProcessingCostSettings,
) -> Decimal | None:
    salary = settings.average_worker_monthly_salary
    if salary is None:
        return None
    monthly = Decimal(salary) + Decimal(settings.average_worker_monthly_social_cost)
    return (monthly / Decimal(settings.working_days_per_month)).quantize(
        WORKER_DAY_MONEY, rounding=ROUND_HALF_UP
    )


def _decimal_text(value: Decimal | None, quantum: Decimal = HOUR) -> str | None:
    if value is None:
        return None
    return str(value.quantize(quantum, rounding=ROUND_HALF_UP))


def _printing_color_count(product: Product) -> tuple[int | None, bool]:
    content = str(product.print_content or "").strip()
    colors = str(product.printing_colors or "").strip()
    plate_count = sum(
        value is not None
        for value in (
            product.printing_plate_1_id,
            product.printing_plate_2_id,
            product.printing_plate_3_id,
        )
    )
    explicitly_no_print = content in _NO_PRINT and not colors and plate_count == 0
    if explicitly_no_print:
        return 0, False
    for marker, count in _CHINESE_COLOR_COUNTS.items():
        if marker in content or marker in colors:
            return count, True
    for value in (colors, content):
        match = _ARABIC_COLOR_COUNT.search(value)
        if match:
            return int(match.group(1)), True
    if colors:
        parts = [part for part in _COLOR_SPLIT.split(colors) if part]
        if parts:
            return len(parts), True
    if plate_count:
        return plate_count, True
    return None, True


def _resolved_printer_mode(
    product: Product,
    profile: ProductProcessingProfile | None,
    settings: ProcessingCostSettings,
    *,
    printing_required: bool,
    supply_mode: str,
) -> str:
    configured = profile.printer_mode if profile else "auto"
    if configured != "auto":
        return configured
    if supply_mode == "external_purchase" or not printing_required:
        return "none"
    return settings.default_printer


def _resolved_die_cut_mode(
    product: Product,
    profile: ProductProcessingProfile | None,
    *,
    supply_mode: str,
) -> str:
    configured = profile.die_cut_mode if profile else "auto"
    if configured != "auto":
        return configured
    if supply_mode == "external_purchase":
        return "none"
    process = str(product.production_process or "")
    code = box_type_code(product.box_style)
    if code in _DIE_CUT_CODES:
        return "small_normal"
    if "超大" in process or "往返" in process or "两次模切" in process:
        return "oversize"
    if "小模切" in process:
        return "small_normal"
    if (
        code == "irregular"
        or product.box_category == "die_cut"
        or "异形" in process
        or "模切" in process
    ):
        # Unknown die-cut products must not silently receive the much faster
        # small-die standard.  A product override can narrow this to a small
        # normal/complex tier after an administrator confirms it.
        return "large"
    return "none"


def _printing_breakdown(
    *,
    product: Product,
    profile: ProductProcessingProfile | None,
    settings: ProcessingCostSettings,
    quantity: int,
    splice_mode: str | None,
    supply_mode: str,
) -> tuple[dict[str, Any], Decimal | None, list[str]]:
    color_count, printing_required = _printing_color_count(product)
    printing_required = printing_required or box_type_code(product.box_style) == "a1_0201"
    mode = _resolved_printer_mode(
        product,
        profile,
        settings,
        printing_required=printing_required,
        supply_mode=supply_mode,
    )
    sheets_per_piece = 2 if str(splice_mode or "").strip().lower() == "double" else 1
    sheet_quantity = quantity * sheets_per_piece if mode != "none" else 0
    if mode == "none":
        return (
            {
                "printer_mode": "none",
                "sheet_quantity": 0,
                "sheets_per_finished_piece": sheets_per_piece,
                "color_count": 0,
                "passes": 0,
                "normal_speed_sheets_per_minute": None,
                "setup_hours": "0.000000",
                "run_hours": "0.000000",
                "machine_hours": "0.000000",
                "crew_size": 0,
                "worker_hours": "0.000000",
                "worker_days": "0.000000",
            },
            Decimal("0"),
            [],
        )
    if color_count is None:
        return (
            {
                "printer_mode": mode,
                "sheet_quantity": sheet_quantity,
                "sheets_per_finished_piece": sheets_per_piece,
                "color_count": None,
                "passes": None,
                "normal_speed_sheets_per_minute": None,
                "setup_hours": None,
                "run_hours": None,
                "machine_hours": None,
                "crew_size": settings.printing_crew_size,
                "worker_hours": None,
                "worker_days": None,
            },
            None,
            ["印刷颜色数量待确认"],
        )
    passes = max(
        1,
        int(
            (Decimal(color_count) / Decimal(settings.colors_per_pass)).to_integral_value(
                rounding=ROUND_CEILING
            )
        ),
    )
    speed = Decimal(
        settings.new_printer_normal_sheets_per_minute
        if mode == "new"
        else settings.old_printer_normal_sheets_per_minute
    )
    setup_hours = Decimal(settings.printing_setup_minutes) * passes / Decimal("60")
    run_hours = Decimal(sheet_quantity * passes) / (speed * Decimal("60"))
    machine_hours = setup_hours + run_hours
    worker_hours = machine_hours * Decimal(settings.printing_crew_size)
    worker_days = worker_hours / Decimal(settings.working_hours_per_day)
    return (
        {
            "printer_mode": mode,
            "sheet_quantity": sheet_quantity,
            "sheets_per_finished_piece": sheets_per_piece,
            "color_count": color_count,
            "passes": passes,
            "normal_speed_sheets_per_minute": _decimal_text(speed, Decimal("0.0001")),
            "setup_hours": _decimal_text(setup_hours),
            "run_hours": _decimal_text(run_hours),
            "machine_hours": _decimal_text(machine_hours),
            "crew_size": settings.printing_crew_size,
            "worker_hours": _decimal_text(worker_hours),
            "worker_days": _decimal_text(worker_days, DAY),
        },
        worker_days,
        [],
    )


def _die_cut_breakdown(
    *,
    product: Product,
    profile: ProductProcessingProfile | None,
    settings: ProcessingCostSettings,
    quantity: int,
    supply_mode: str,
) -> tuple[dict[str, Any], Decimal]:
    mode = _resolved_die_cut_mode(product, profile, supply_mode=supply_mode)
    if mode == "none":
        return (
            {
                "die_cut_mode": "none",
                "piece_quantity": 0,
                "normal_speed_pieces_per_minute": None,
                "seconds_per_piece": None,
                "setup_hours": "0.000000",
                "run_hours": "0.000000",
                "machine_hours": "0.000000",
                "crew_size": 0,
                "separate_stripping_workers": 0,
                "worker_hours": "0.000000",
                "worker_days": "0.000000",
            },
            Decimal("0"),
        )
    if mode == "small_normal":
        speed = Decimal(settings.small_die_normal_pieces_per_minute)
        crew = settings.small_die_crew_size
        seconds_per_piece = None
        separate_stripping = max(crew - 1, 0)
    elif mode == "small_complex":
        speed = Decimal(settings.small_die_normal_pieces_per_minute)
        crew = settings.small_complex_die_crew_size
        seconds_per_piece = None
        separate_stripping = max(crew - 1, 0)
    elif mode == "large":
        speed = Decimal(settings.large_die_pieces_per_minute)
        crew = settings.large_die_crew_size
        seconds_per_piece = None
        separate_stripping = 0
    else:
        speed = None
        crew = settings.oversize_die_crew_size
        seconds_per_piece = Decimal(settings.oversize_die_seconds_per_piece)
        separate_stripping = 0
    setup_hours = Decimal(settings.die_setup_minutes) / Decimal("60")
    if speed is not None:
        run_hours = Decimal(quantity) / (speed * Decimal("60"))
    else:
        run_hours = Decimal(quantity) * seconds_per_piece / Decimal("3600")
    machine_hours = setup_hours + run_hours
    worker_hours = machine_hours * Decimal(crew)
    worker_days = worker_hours / Decimal(settings.working_hours_per_day)
    return (
        {
            "die_cut_mode": mode,
            "piece_quantity": quantity,
            "normal_speed_pieces_per_minute": (
                _decimal_text(speed, Decimal("0.0001")) if speed is not None else None
            ),
            "seconds_per_piece": (
                _decimal_text(seconds_per_piece, Decimal("0.0001"))
                if seconds_per_piece is not None
                else None
            ),
            "setup_hours": _decimal_text(setup_hours),
            "run_hours": _decimal_text(run_hours),
            "machine_hours": _decimal_text(machine_hours),
            "crew_size": crew,
            # Large and oversize crew sizes are total headcounts: no extra
            # stripping labour is added on top of them.
            "separate_stripping_workers": separate_stripping,
            "worker_hours": _decimal_text(worker_hours),
            "worker_days": _decimal_text(worker_days, DAY),
        },
        worker_days,
    )


def _joining_breakdown(
    *,
    product: Product,
    profile: ProductProcessingProfile | None,
    settings: ProcessingCostSettings,
    quantity: int,
    splice_mode: str | None,
    supply_mode: str,
) -> tuple[dict[str, Any], Decimal]:
    process = str(product.production_process or "")
    code = box_type_code(product.box_style)
    double_splice = (
        str(splice_mode or "").strip().lower() == "double" or "双拼" in process
    )
    if supply_mode == "external_purchase":
        double_splice = False
    explicitly_no_joining = supply_mode == "external_purchase" or any(
        marker in process for marker in _NO_JOINING_MARKERS
    )
    if double_splice:
        seconds_per_piece = Decimal(settings.double_splice_seconds_per_piece)
        elapsed_hours = Decimal(quantity) * seconds_per_piece / Decimal("3600")
        worker_hours = elapsed_hours * Decimal(settings.joining_crew_size)
        worker_days = worker_hours / Decimal(settings.working_hours_per_day)
        mode = "double_splice"
        speed = None
    elif not explicitly_no_joining and (
        any(marker in process for marker in _JOINING_MARKERS) or code in _JOINING_CODES
    ):
        speed = Decimal(settings.joining_normal_pieces_per_second)
        elapsed_hours = Decimal(quantity) / (speed * Decimal("3600"))
        worker_hours = elapsed_hours * Decimal(settings.joining_crew_size)
        worker_days = worker_hours / Decimal(settings.working_hours_per_day)
        mode = "ordinary"
        seconds_per_piece = Decimal("1") / speed
    else:
        return (
            {
                "joining_mode": "none",
                "piece_quantity": 0,
                "assembly_worker_days_per_1000": None,
                "normal_speed_pieces_per_second": None,
                "seconds_per_piece": None,
                "elapsed_hours": "0.000000",
                "crew_size": 0,
                "worker_hours": "0.000000",
                "worker_days": "0.000000",
            },
            Decimal("0"),
        )
    return (
        {
            "joining_mode": mode,
            "piece_quantity": quantity,
            "assembly_worker_days_per_1000": None,
            "normal_speed_pieces_per_second": (
                _decimal_text(speed, Decimal("0.0001")) if speed is not None else None
            ),
            "seconds_per_piece": (
                _decimal_text(seconds_per_piece, Decimal("0.0001"))
                if seconds_per_piece is not None
                else None
            ),
            "elapsed_hours": _decimal_text(elapsed_hours),
            # Joining and packing run as one line.  This is the total line crew,
            # not a joining crew plus another packing crew.
            "crew_size": settings.joining_crew_size,
            "worker_hours": _decimal_text(worker_hours),
            "worker_days": _decimal_text(worker_days, DAY),
        },
        worker_days,
    )


def _extra_assembly_breakdown(
    *,
    profile: ProductProcessingProfile | None,
    settings: ProcessingCostSettings,
    quantity: int,
) -> tuple[dict[str, Any], Decimal]:
    override = profile.assembly_worker_days_per_1000 if profile else None
    if override is None:
        return (
            {
                "assembly_mode": "none",
                "piece_quantity": 0,
                "assembly_worker_days_per_1000": None,
                "worker_hours": "0.000000",
                "worker_days": "0.000000",
            },
            Decimal("0"),
        )
    worker_days = Decimal(quantity) * Decimal(override) / Decimal("1000")
    worker_hours = worker_days * Decimal(settings.working_hours_per_day)
    return (
        {
            "assembly_mode": "product_override",
            "piece_quantity": quantity,
            "assembly_worker_days_per_1000": _decimal_text(Decimal(override), DAY),
            "worker_hours": _decimal_text(worker_hours),
            "worker_days": _decimal_text(worker_days, DAY),
        },
        worker_days,
    )


def estimate_standard_processing_cost(
    db: Session,
    *,
    product: Product,
    quantity: int,
    splice_mode: str | None = None,
    supply_mode: str | None = None,
) -> dict[str, Any]:
    """Calculate advisory standard labour without writing actual cost facts."""

    if quantity <= 0:
        raise ValueError("加工估算数量必须大于 0")
    settings = get_processing_cost_settings(db)
    profile = get_product_processing_profile(db, int(product.id))
    effective_splice_mode = splice_mode if splice_mode is not None else product.splice_mode
    effective_supply_mode = (
        str(supply_mode).strip()
        if supply_mode is not None
        else str(product.supply_mode or "corrugated_production").strip()
    )
    printing, printing_days, missing = _printing_breakdown(
        product=product,
        profile=profile,
        settings=settings,
        quantity=quantity,
        splice_mode=effective_splice_mode,
        supply_mode=effective_supply_mode,
    )
    die_cut, die_cut_days = _die_cut_breakdown(
        product=product,
        profile=profile,
        settings=settings,
        quantity=quantity,
        supply_mode=effective_supply_mode,
    )
    joining, joining_days = _joining_breakdown(
        product=product,
        profile=profile,
        settings=settings,
        quantity=quantity,
        splice_mode=effective_splice_mode,
        supply_mode=effective_supply_mode,
    )
    extra_assembly, extra_assembly_days = _extra_assembly_breakdown(
        profile=profile,
        settings=settings,
        quantity=quantity,
    )
    total_worker_days = (
        printing_days + die_cut_days + joining_days + extra_assembly_days
        if printing_days is not None
        else None
    )
    worker_day_cost = calculate_worker_day_cost(settings)
    if total_worker_days is not None and total_worker_days > 0 and worker_day_cost is None:
        missing.append("平均生产月薪待维护")
    missing = list(dict.fromkeys(missing))
    complete = not missing and total_worker_days is not None
    processing_cost = (
        (total_worker_days * worker_day_cost).quantize(MONEY, rounding=ROUND_HALF_UP)
        if complete and worker_day_cost is not None
        else Decimal("0.00")
        if complete and total_worker_days == 0
        else None
    )
    status = "calculated" if complete else "incomplete"
    return {
        "rule_version": RULE_VERSION,
        "calculation_status": status,
        "estimated_processing_cost": _decimal_text(processing_cost, MONEY),
        "missing_items": missing,
        "settings_id": settings.id,
        "settings_version": settings.version,
        "working_hours_per_day": _decimal_text(
            Decimal(settings.working_hours_per_day), Decimal("0.0001")
        ),
        "working_days_per_month": _decimal_text(
            Decimal(settings.working_days_per_month), Decimal("0.0001")
        ),
        "average_worker_monthly_salary": (
            _decimal_text(Decimal(settings.average_worker_monthly_salary), MONEY)
            if settings.average_worker_monthly_salary is not None
            else None
        ),
        "average_worker_monthly_social_cost": _decimal_text(
            Decimal(settings.average_worker_monthly_social_cost), MONEY
        ),
        "product_processing_profile_id": profile.id if profile else None,
        "product_processing_profile_version": profile.version if profile else 0,
        "worker_day_cost": _decimal_text(worker_day_cost, WORKER_DAY_MONEY),
        "printing": printing,
        "die_cut": die_cut,
        "joining": joining,
        "extra_assembly": extra_assembly,
        "total_worker_days": _decimal_text(total_worker_days, DAY),
    }


def estimate_order_item_processing_cost(
    db: Session, item: OrderItem
) -> dict[str, Any]:
    return estimate_standard_processing_cost(
        db,
        product=item.product,
        quantity=max(int(item.quantity or 0), 1),
        splice_mode=getattr(item, "snapshot_splice_mode", None),
        supply_mode=getattr(item, "supply_mode_snapshot", None),
    )
