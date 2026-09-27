from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from app.services.requisition_quantities import (
    DEFAULT_CUTTING_MODE,
    CuttingModeError,
    normalize_cutting_mode,
)


DEFAULT_FLAP_MM = 30
SPLICE_MODES = ("single", "double")


class BoxTypeRuleError(ValueError):
    """Raised when a recognized box type receives an invalid structural value."""


@dataclass(frozen=True)
class BoxTypeRule:
    code: str
    display_name: str
    aliases: tuple[str, ...]
    required_dimensions: tuple[str, ...]
    formula_version: str | None
    is_component: bool
    uses_flap: bool
    supports_splice: bool
    supports_cutting_mode: bool
    supported_crease_types: tuple[str, ...]
    auto_report_formula: bool

    def public_dict(self) -> dict[str, object]:
        data = asdict(self)
        data.update(
            supported_splice_modes=(
                list(SPLICE_MODES) if self.supports_splice else ["single"]
            ),
            pieces_per_box_by_splice=(
                {"single": 1, "double": 2}
                if self.supports_splice
                else {"single": 1}
            ),
            supported_cutting_modes=(
                [] if self.supports_cutting_mode else [DEFAULT_CUTTING_MODE]
            ),
            cutting_mode_input=("positive_integer" if self.supports_cutting_mode else None),
            cutting_mode_min=(1 if self.supports_cutting_mode else None),
        )
        for key in (
            "aliases",
            "required_dimensions",
            "supported_crease_types",
        ):
            data[key] = list(data[key])
        return data


BOX_TYPE_RULES: tuple[BoxTypeRule, ...] = (
    BoxTypeRule(
        code="a1_0201",
        display_name="A1/0201 普通开槽箱",
        aliases=(
            "A1",
            "0201",
            "A1/0201",
            "A1型纸箱",
            "A1 型纸箱",
            "普通开槽箱",
            "A1/0201 普通开槽箱",
            "WCX1五层箱1",
            "WC 五层钉箱",
            "WCZX 五层粘箱",
            "SCX 单瓦箱",
            "QCX 七层纸箱",
            "001 思展钉箱",
            "008 外箱无钉",
            "006 华元外箱",
            "WZX 外纸箱",
            "NZX 内纸箱",
            "THWX 腾华外箱",
            "007 华元内盒",
            "009 内盒无钉",
            "004 豪迪纸箱",
            "005 粘合成型",
            "THYW 天华压外",
        ),
        required_dimensions=("length_mm", "width_mm", "height_mm"),
        formula_version="tm_a1_20260729_v1",
        is_component=False,
        uses_flap=True,
        supports_splice=True,
        supports_cutting_mode=False,
        supported_crease_types=("净料", "毛片", "压线", "其他"),
        auto_report_formula=True,
    ),
    BoxTypeRule(
        code="a3_set",
        display_name="A3 天地盖",
        aliases=("A3", "天地盖", "A3天地盖", "A3 天地盖", "TDG 天地盖", "013 半截天地盖"),
        required_dimensions=("length_mm", "width_mm", "height_mm"),
        formula_version="tm_a3_set_legacy_v1",
        is_component=False,
        uses_flap=False,
        supports_splice=False,
        supports_cutting_mode=False,
        supported_crease_types=("净料", "毛片", "压线", "其他"),
        auto_report_formula=True,
    ),
    BoxTypeRule(
        code="top_cover",
        display_name="独立天盖",
        aliases=("天盖", "独立天盖", "TDGG 天地盖盖", "模切盖 模切盖"),
        required_dimensions=("length_mm", "width_mm", "height_mm"),
        formula_version=None,
        is_component=True,
        uses_flap=False,
        supports_splice=False,
        supports_cutting_mode=False,
        supported_crease_types=("净料", "毛片", "压线", "其他"),
        auto_report_formula=False,
    ),
    BoxTypeRule(
        code="bottom_base",
        display_name="独立底",
        aliases=("底", "独立底", "TDGD 天地盖底"),
        required_dimensions=("length_mm", "width_mm", "height_mm"),
        formula_version=None,
        is_component=True,
        uses_flap=False,
        supports_splice=False,
        supports_cutting_mode=False,
        supported_crease_types=("净料", "毛片", "压线", "其他"),
        auto_report_formula=False,
    ),
    BoxTypeRule(
        code="surround_panel",
        display_name="围板",
        aliases=("围板", "围套", "WB 围板"),
        required_dimensions=("length_mm", "width_mm", "height_mm"),
        formula_version="tm_surround_panel_20260729_v1",
        is_component=True,
        uses_flap=True,
        supports_splice=True,
        supports_cutting_mode=False,
        supported_crease_types=("净料", "毛片", "压线", "其他"),
        auto_report_formula=True,
    ),
    BoxTypeRule(
        code="full_flap_carton",
        display_name="满摇盖纸箱",
        aliases=(
            "满摇盖",
            "满摇盖纸箱",
            "全搭盖",
            "全搭盖箱",
            "MYG 满摇盖",
            "010 单瓦满摇盖",
        ),
        required_dimensions=("length_mm", "width_mm", "height_mm"),
        formula_version="tm_full_flap_20260729_v1",
        is_component=False,
        uses_flap=True,
        supports_splice=True,
        supports_cutting_mode=False,
        supported_crease_types=("净料", "毛片", "压线", "其他"),
        auto_report_formula=True,
    ),
    BoxTypeRule(
        code="half_slotted_carton",
        display_name="半开槽箱",
        aliases=("半开槽", "半开槽箱", "BJX 半截箱", "WGX 无盖箱", "WDX 无底箱"),
        required_dimensions=("length_mm", "width_mm", "height_mm"),
        formula_version="tm_half_slotted_20260801_v2",
        is_component=False,
        uses_flap=True,
        supports_splice=True,
        supports_cutting_mode=False,
        supported_crease_types=("净料", "毛片", "压线", "其他"),
        auto_report_formula=True,
    ),
    BoxTypeRule(
        code="liner",
        display_name="衬板",
        aliases=("衬板", "CB 单瓦衬板", "SC 双瓦衬板", "QCB 七层板"),
        required_dimensions=("length_mm", "width_mm"),
        formula_version="tm_liner_20260729_v1",
        is_component=True,
        uses_flap=False,
        supports_splice=False,
        supports_cutting_mode=True,
        supported_crease_types=("净料", "毛片"),
        auto_report_formula=True,
    ),
    BoxTypeRule(
        code="divider",
        display_name="隔板",
        aliases=("隔板", "GD2 格挡2", "016 井字架"),
        required_dimensions=("length_mm", "width_mm"),
        formula_version=None,
        is_component=True,
        uses_flap=False,
        supports_splice=False,
        supports_cutting_mode=True,
        supported_crease_types=("净料", "毛片", "其他"),
        auto_report_formula=False,
    ),
    BoxTypeRule(
        code="die_cut_partition",
        display_name="刀卡",
        aliases=("刀卡", "JB 简包", "HP01 恒鹏模切1", "HP02 恒鹏模切2"),
        required_dimensions=("length_mm", "width_mm"),
        formula_version=None,
        is_component=True,
        uses_flap=False,
        supports_splice=False,
        supports_cutting_mode=True,
        supported_crease_types=("净料", "毛片", "其他"),
        auto_report_formula=False,
    ),
    BoxTypeRule(
        code="die_cut_inner_box",
        display_name="模切内盒",
        aliases=("模切内盒", "平卡", "FJH 飞机盒", "MQXX 模切小箱",
                 "SC AB白卡", "XH 鞋盒", "015 白卡内盒", "003 思展模切",
                 "011 佩特罗模", "014 恒鹏模切3", "LXBG 模切日本黄", "SAT 驶安特模", "TBM 天宝模"),
        required_dimensions=("length_mm", "width_mm", "height_mm"),
        formula_version=None,
        is_component=False,
        uses_flap=False,
        supports_splice=False,
        supports_cutting_mode=True,
        supported_crease_types=("净料", "毛片", "其他"),
        auto_report_formula=False,
    ),
    BoxTypeRule(
        code="irregular",
        display_name="异形箱",
        aliases=("异形", "异形箱", "YXX 异型箱", "NH 天华内盒1", "THNH1 腾华内盒1", "CTSNH 抽屉式内盒"),
        required_dimensions=("length_mm", "width_mm", "height_mm"),
        formula_version=None,
        is_component=False,
        uses_flap=False,
        supports_splice=False,
        supports_cutting_mode=False,
        supported_crease_types=("净料", "毛片", "压线", "其他"),
        auto_report_formula=False,
    ),
    BoxTypeRule(
        code="other",
        display_name="其他",
        aliases=("其他",),
        required_dimensions=(),
        formula_version=None,
        is_component=False,
        uses_flap=False,
        supports_splice=False,
        supports_cutting_mode=False,
        supported_crease_types=("净料", "毛片", "压线", "其他"),
        auto_report_formula=False,
    ),
)


def _alias_key(value: str | None) -> str:
    return "".join(str(value or "").strip().upper().split())


_RULE_BY_ALIAS = {
    _alias_key(alias): rule
    for rule in BOX_TYPE_RULES
    for alias in (rule.code, rule.display_name, *rule.aliases)
}


def get_box_type_rule(box_style: str | None) -> BoxTypeRule | None:
    """Resolve only explicit codes/names/aliases; never guess from substrings."""
    key = _alias_key(box_style)
    return _RULE_BY_ALIAS.get(key) if key else None


def box_type_code(box_style: str | None) -> str | None:
    rule = get_box_type_rule(box_style)
    return rule.code if rule is not None else None


def canonical_box_style(box_style: str | None) -> str | None:
    value = str(box_style or "").strip()
    if not value:
        return None
    rule = get_box_type_rule(value)
    return rule.display_name if rule is not None else value


def box_type_uses_splice(box_style: str | None) -> bool:
    rule = get_box_type_rule(box_style)
    return bool(rule and rule.supports_splice)


def box_type_uses_flap(box_style: str | None) -> bool:
    rule = get_box_type_rule(box_style)
    return bool(rule and rule.uses_flap)


def box_type_supports_cutting_mode(box_style: str | None) -> bool:
    rule = get_box_type_rule(box_style)
    return bool(rule and rule.supports_cutting_mode)


def normalize_box_configuration(
    *,
    box_style: str | None,
    splice_mode: str | None,
    pieces_per_box: int | None,
    flap_mm: int | None,
    default_cutting_mode: str | None,
    crease_type: str | None = None,
) -> dict[str, object]:
    """Normalize new writes without recalculating report dimensions or history."""
    rule = get_box_type_rule(box_style)
    normalized_style = canonical_box_style(box_style)
    normalized_splice = str(splice_mode or "single").strip().lower()
    if normalized_splice not in SPLICE_MODES:
        raise BoxTypeRuleError("拼箱方式仅允许：single 或 double")

    if rule is not None and not rule.supports_splice:
        normalized_splice = "single"
    if rule is None and pieces_per_box in {1, 2}:
        # Unknown historical names remain readable/editable.  They never gain
        # an automatic formula, but a full-form edit must not erase manually
        # stored structure values merely because the registry cannot classify it.
        normalized_pieces = int(pieces_per_box)
    else:
        normalized_pieces = 2 if normalized_splice == "double" else 1

    if rule is not None and rule.uses_flap:
        normalized_flap = DEFAULT_FLAP_MM if flap_mm is None else int(flap_mm)
        if normalized_flap <= 0:
            raise BoxTypeRuleError("舌头(mm)必须大于0")
    elif rule is None:
        normalized_flap = flap_mm
    else:
        normalized_flap = None

    normalized_cutting = normalize_cutting_mode(default_cutting_mode)
    if rule is not None and not rule.supports_cutting_mode:
        normalized_cutting = DEFAULT_CUTTING_MODE
    else:
        try:
            normalized_cutting = normalize_cutting_mode(
                default_cutting_mode,
                strict=True,
            )
        except CuttingModeError as error:
            raise BoxTypeRuleError(str(error)) from error

    normalized_crease = str(crease_type or "").strip() or None
    if (
        rule is not None
        and normalized_crease is not None
        and normalized_crease not in rule.supported_crease_types
    ):
        allowed = "、".join(rule.supported_crease_types)
        raise BoxTypeRuleError(
            f"{rule.display_name}的压线类型仅允许：{allowed}"
        )

    return {
        "recognized": rule is not None,
        "code": rule.code if rule is not None else None,
        "box_style": normalized_style,
        "splice_mode": normalized_splice,
        "pieces_per_box": normalized_pieces,
        "flap_mm": normalized_flap,
        "default_cutting_mode": normalized_cutting,
        "crease_type": normalized_crease,
    }


def _empty_recommendation(
    *,
    rule: BoxTypeRule | None,
    box_style: str | None,
    configuration: dict[str, object],
    message: str,
) -> dict[str, object]:
    return {
        "recognized": rule is not None,
        "auto_calculated": False,
        "manual_required": True,
        "code": rule.code if rule is not None else None,
        "box_style": (
            rule.display_name if rule is not None else canonical_box_style(box_style)
        ),
        "formula_version": rule.formula_version if rule is not None else None,
        "splice_mode": configuration["splice_mode"],
        "pieces_per_box": configuration["pieces_per_box"],
        "flap_mm": configuration["flap_mm"],
        "report_length_mm": None,
        "report_width_mm": None,
        "crease_type": configuration["crease_type"],
        "crease_left_mm": None,
        "crease_middle_mm": None,
        "crease_right_mm": None,
        "base_report_length_mm": None,
        "base_report_width_mm": None,
        "base_crease_type": None,
        "base_crease_left_mm": None,
        "base_crease_middle_mm": None,
        "base_crease_right_mm": None,
        "message": message,
    }


def recommend_box_type(
    *,
    box_style: str | None,
    length_mm: int | None,
    width_mm: int | None,
    height_mm: int | None,
    splice_mode: str | None = "single",
    flap_mm: int | None = None,
    crease_type: str | None = None,
) -> dict[str, object]:
    """Return only confirmed recommendations; unknown formulas fail closed."""
    rule = get_box_type_rule(box_style)
    configuration = normalize_box_configuration(
        box_style=box_style,
        splice_mode=splice_mode,
        pieces_per_box=None,
        flap_mm=flap_mm,
        default_cutting_mode=DEFAULT_CUTTING_MODE,
        crease_type=crease_type,
    )
    if rule is None:
        return _empty_recommendation(
            rule=None,
            box_style=box_style,
            configuration=configuration,
            message="未识别该箱型，系统不会套用通用公式，请人工填写报料尺寸",
        )
    if not rule.auto_report_formula:
        return _empty_recommendation(
            rule=rule,
            box_style=box_style,
            configuration=configuration,
            message=f"{rule.display_name}暂无已确认的自动报料公式，请人工填写并保留报料尺寸",
        )

    dimensions = {
        "length_mm": length_mm,
        "width_mm": width_mm,
        "height_mm": height_mm,
    }
    missing = [name for name in rule.required_dimensions if dimensions[name] is None]
    if missing:
        labels = {
            "length_mm": "长",
            "width_mm": "宽",
            "height_mm": "高",
        }
        return _empty_recommendation(
            rule=rule,
            box_style=box_style,
            configuration=configuration,
            message="请先填写" + "、".join(labels[name] for name in missing),
        )

    length = int(length_mm or 0)
    width = int(width_mm or 0)
    height = int(height_mm or 0)
    if min(length, width) <= 0 or (
        "height_mm" in rule.required_dimensions and height <= 0
    ):
        raise BoxTypeRuleError("箱型尺寸必须大于0")

    result = _empty_recommendation(
        rule=rule,
        box_style=box_style,
        configuration=configuration,
        message="已按当前箱型的确认规则生成推荐值",
    )
    result.update(auto_calculated=True, manual_required=False)
    chosen_crease = str(crease_type or "").strip() or None
    splice = str(configuration["splice_mode"])
    flap = int(configuration["flap_mm"] or 0)
    report_length = (
        length + width + flap
        if splice == "double"
        else 2 * (length + width) + flap
    )

    if rule.code == "a1_0201":
        if width % 2:
            side = (width + 1) // 2 + 2
            middle = height - 1
            if middle <= 0:
                raise BoxTypeRuleError("A1奇数宽推荐要求成品高度至少为2mm")
        else:
            side = width // 2
            middle = height
        result.update(
            report_length_mm=report_length,
            report_width_mm=side + middle + side,
            crease_type=chosen_crease or "压线",
        )
        if result["crease_type"] == "压线":
            result.update(
                crease_left_mm=side,
                crease_middle_mm=middle,
                crease_right_mm=side,
            )
    elif rule.code == "a3_set":
        base_length = max(length - 25, 1)
        base_width = max(width - 25, 1)
        result.update(
            splice_mode="single",
            pieces_per_box=1,
            flap_mm=None,
            report_length_mm=length + 2 * height,
            report_width_mm=width + 2 * height,
            crease_type=chosen_crease or "压线",
            base_report_length_mm=base_length + 2 * height,
            base_report_width_mm=base_width + 2 * height,
            base_crease_type="压线",
            base_crease_left_mm=height,
            base_crease_middle_mm=base_width,
            base_crease_right_mm=height,
        )
        if result["crease_type"] == "压线":
            result.update(
                crease_left_mm=height,
                crease_middle_mm=width,
                crease_right_mm=height,
            )
    elif rule.code == "surround_panel":
        result.update(
            report_length_mm=report_length,
            report_width_mm=height,
            crease_type=chosen_crease,
            manual_required=True,
            message=(
                "围板报料长宽已推荐；面板压线在料长方向，"
                "本轮不写入宽向三段压线，请人工核对报料备注"
            ),
        )
    elif rule.code == "full_flap_carton":
        result.update(
            report_length_mm=report_length,
            report_width_mm=height + 2 * length,
            crease_type=chosen_crease or "压线",
        )
        if result["crease_type"] == "压线":
            result.update(
                crease_left_mm=length,
                crease_middle_mm=height,
                crease_right_mm=length,
            )
    elif rule.code == "half_slotted_carton":
        # The factory rule uses one half flap only: odd widths round that
        # physical flap upward, while even widths remain an exact half.
        half_width = (width + 1) // 2
        result.update(
            report_length_mm=report_length,
            report_width_mm=height + half_width,
            crease_type=chosen_crease or "压线",
        )
        if result["crease_type"] == "压线":
            result.update(
                crease_left_mm=half_width,
                crease_middle_mm=height,
                crease_right_mm=0,
            )
    elif rule.code == "liner":
        result.update(
            report_length_mm=length,
            report_width_mm=width,
            crease_type=chosen_crease,
        )
        if chosen_crease is None:
            result.update(
                manual_required=True,
                message="衬板报料长宽已推荐，请人工选择净或毛",
            )
    else:  # Defensive: new rules must opt in with an explicit implementation.
        return _empty_recommendation(
            rule=rule,
            box_style=box_style,
            configuration=configuration,
            message=f"{rule.display_name}的自动公式尚未接入，请人工填写报料尺寸",
        )

    if result["crease_type"] != "压线":
        result.update(
            crease_left_mm=None,
            crease_middle_mm=None,
            crease_right_mm=None,
        )
    return result
