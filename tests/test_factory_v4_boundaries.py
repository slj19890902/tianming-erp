from __future__ import annotations

from pathlib import Path

import ezdxf

from scripts.build_factory_v4_pd_boundaries import (
    BOUNDARY_LAYER,
    PROPOSALS,
    create_overlay_dxf,
)


def test_pd_boundary_overlay_is_valid_and_uses_zone_layer(tmp_path: Path) -> None:
    output = tmp_path / "pd-boundary-overlay.dxf"

    create_overlay_dxf(output)

    document = ezdxf.readfile(output)
    entities = list(document.modelspace())
    boundaries = [entity for entity in entities if entity.dxftype() == "LWPOLYLINE"]
    labels = [entity.dxf.text for entity in entities if entity.dxftype() == "TEXT"]
    assert len(boundaries) == 5
    assert all(entity.closed for entity in boundaries)
    assert all(entity.dxf.layer == BOUNDARY_LAYER for entity in boundaries)
    assert {proposal["kind"] for proposal in PROPOSALS} == {"P", "D"}
    assert "1F-P-001" in labels
    assert "1F-D-002" in labels
    assert "P/D BOUNDARIES CONFIRMED 2026-08-03" in labels
