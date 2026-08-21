from __future__ import annotations

from typing import Literal

from app.services.box_type_rules import box_type_code


ProductionStation = Literal["printing", "die_cut"]
PRODUCTION_STATION_ROUTING_RULE_VERSION = "p1-84-v1"

_NO_PRINT_CONTENT = frozenset({"", "无印刷", "无", "否", "不印刷"})
_PRINTING_STATION_BOX_TYPE_CODES = frozenset(
    {
        "a1_0201",
        "a3_set",
        "top_cover",
        "bottom_base",
        "surround_panel",
        "full_flap_carton",
        "half_slotted_carton",
    }
)


def production_station_memberships(
    *,
    print_content_snapshot: str | None,
    box_style: str | None,
    die_cut_required: bool,
) -> frozenset[ProductionStation]:
    """Project workstation membership from explicit production facts only.

    The caller must resolve ``die_cut_required`` from the formal boolean or
    category fact for the concrete source.  Product names, codes, remarks,
    drawings, molds and printing facts are deliberately not accepted here.
    """

    if not isinstance(die_cut_required, bool):
        raise ValueError("die_cut_required must be an explicit boolean fact")
    stations: set[ProductionStation] = set()
    content = str(print_content_snapshot or "").strip()
    if (
        content not in _NO_PRINT_CONTENT
        or box_type_code(box_style) in _PRINTING_STATION_BOX_TYPE_CODES
    ):
        stations.add("printing")
    if die_cut_required:
        stations.add("die_cut")
    return frozenset(stations)
