from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "admin"
    / "sync_mixed_sample_catalog_20260810.py"
)
SPEC = spec_from_file_location("sync_mixed_sample_catalog_20260810", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
sync = module_from_spec(SPEC)
sys.modules[SPEC.name] = sync
SPEC.loader.exec_module(sync)


def test_frozen_exclusion_scope_is_exact() -> None:
    assert len(sync.EXCLUDED_SAMPLES) == 22
    assert sync.EXCLUDED_SAMPLES["YP149"] == "Z+B 材质主档未建立"
    assert sync.EXCLUDED_SAMPLES["YP201"] == "450克灰底白板材质主档未建立"


def test_material_mapping_uses_confirmed_stable_ids() -> None:
    def material_id(text: str) -> int:
        return sync._material_id_for(
            {
                "customer_code": "YKE",
                "product_code": "X",
                "legacy_material_text": text,
            }
        )

    assert material_id("VIK（B）") == 609
    assert material_id("B+Z/B") == 609
    assert material_id("DRD（A）") == 619
    assert material_id("VSNIV/AB") == 610
    assert material_id("D+D（A）") == 622


def test_process_mapping_keeps_print_separate_and_normalizes_glue() -> None:
    registration = {
        "forming_method": "模切",
        "joining_method": "粘合",
        "printed": True,
        "printing_colors": "红色",
        "secondary_gluing": True,
    }
    reference = {"base_process": "轧贴", "box_category": "die_cut"}
    category, style, printed, color, tokens = sync._canonical_process(
        [registration], [reference]
    )
    assert category == "die_cut"
    assert style == "模切内盒"
    assert printed is True
    assert color == "红色"
    assert tokens == ["模切", "粘贴", "二次粘合"]


def test_legacy_red_nail_infers_joining_without_inventing_color() -> None:
    registration = {
        "forming_method": None,
        "joining_method": None,
        "printed": True,
        "printing_colors": "红色",
        "secondary_gluing": False,
    }
    reference = {"base_process": "红钉", "box_category": "normal"}
    category, _style, printed, color, tokens = sync._canonical_process(
        [registration], [reference]
    )
    assert category == "normal"
    assert printed is True
    assert color == "红色"
    assert tokens == ["开槽", "打钉"]


def test_special_reference_rows_are_frozen() -> None:
    candidates = [
        {"customer_code": "YKE", "source_row": 74},
        {"customer_code": "YKE", "source_row": 75},
        {"customer_code": "KEW", "source_row": 44},
    ]
    selected = sync._special_candidates("YP133", candidates)
    assert [(row["customer_code"], row["source_row"]) for row in selected] == [
        ("YKE", 75),
        ("KEW", 44),
    ]
    assert sync._special_candidates(
        "YP148",
        [
            {"customer_code": "YL", "source_row": 19},
            {"customer_code": "YL", "source_row": 20},
        ],
    ) == [{"customer_code": "YL", "source_row": 19}]
