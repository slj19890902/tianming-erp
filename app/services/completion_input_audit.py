"""Read-only quantity evidence. Never compare sheet, component and set quantities."""
from collections import defaultdict
from fractions import Fraction

RULE_VERSION = "receipt-source-units-v2"


def input_evidence(receipts, completions, allocations, snapshots, *, independent_cutting=False):
    evidence = {"rule_version": RULE_VERSION, "receipt_ids": [r["id"] for r in receipts],
                "completion_ids": [c["id"] for c in completions]}
    def review(reason):
        return dict(severity="review", summary="完工投入与来料来源单位需核对，未据此判定超量。",
                    evidence={**evidence, "basis_missing": reason})
    def compare(used, allowed, unit):
        if used <= allowed:
            return None
        return dict(severity="error", summary="正式完工累计投入超过同来源、同单位的有效来料数量。",
                    evidence={**evidence, "completion_material_input": int(used),
                              "allowed_production_input": int(allowed), "comparison_unit": unit})
    if independent_cutting:
        return review("independent_cutting_requires_frozen_component_consumption")
    active = [a for r in receipts for a in allocations.get(r["id"], []) if a.get("status") == "posted"]
    origins = {c["origin"] for c in completions}
    if origins == {"receipt_auto"}:
        linked = {a.get("production_completion_id"): a for a in active if a.get("production_completion_id")}
        if any(c["id"] not in linked for c in completions):
            return review("receipt_auto_allocation_link_missing")
        # Auto-completion records a finished-unit delta, not component sheets.
        capacity = defaultdict(Fraction)
        for a in active:
            frozen = snapshots.get(a.get("purchase_purpose_source_snapshot_id"))
            if not frozen or not frozen.get("pieces_per_finished_snapshot") or not frozen.get("yield_per_sheet_snapshot"):
                return review("frozen_yield_or_component_unit_missing")
            capacity[a["component_type"]] += Fraction(a["receipt_order_purpose_sheet_qty"] * frozen["yield_per_sheet_snapshot"], frozen["pieces_per_finished_snapshot"])
        components = set(capacity)
        if components not in ({"whole"}, {"cover", "base"}):
            return review("component_receipt_set_incomplete")
        for c in completions:
            if c["actual_output_quantity"] != linked[c["id"]]["finished_output_qty_delta"]:
                return review("completion_output_differs_from_allocation_delta")
        evidence["component_capacity"] = {key: str(value) for key,value in capacity.items()}
        return compare(sum(c["actual_output_quantity"] for c in completions), min(capacity.values()), "finished_units")
    if origins != {"manual"}:
        return review("mixed_completion_origins")
    if active:
        if len(active) != len(receipts) or any(a["component_type"] != "whole" for a in active):
            return review("mixed_or_component_manual_input")
        allowed = sum(a["receipt_order_purpose_sheet_qty"] for a in active)
    else:
        grouped = defaultdict(list)
        for r in receipts:
            source = ("supplier", r.get("supplier_order_item_id")) if r.get("supplier_order_item_id") else ("requisition", r.get("requisition_item_id"))
            if not source[1]:
                return review("legacy_source_identity_missing")
            grouped[source].append(r)
        if len(grouped) != 1:
            return review("legacy_multi_source_component_units_unknown")
        source_rows = next(iter(grouped.values()))
        received = sum(r["received_quantity"] for r in source_rows)
        latest = max(source_rows, key=lambda r:r["id"])
        planned = max(r["planned_quantity"] or 0 for r in source_rows)
        allowed = received if latest["resolution_action"] == "all_to_production" else min(received, planned)
    return compare(sum(c["material_input_quantity"] for c in completions), allowed, "sheets")
