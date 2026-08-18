from __future__ import annotations

import re


SUPPLIER_MATERIAL_FLUTES = {"AAA", "ABC", "AB", "E", "BE", "B", "C", "A"}


def clean_supplier_material_code(
    value: str | None,
    layer_count: int | None,
) -> str:
    """Return the paper-combination code without legacy flute suffixes."""

    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    expected_length = {3: 3, 5: 5, 7: 7}.get(layer_count)
    if expected_length:
        for candidate in re.split(r"\s*/\s*|\s*\|\s*", raw):
            compact = re.sub(r"\s+", "", candidate)
            if (
                len(compact) == expected_length
                and all(char.isprintable() and not char.isspace() for char in compact)
            ):
                return compact
    tokens = re.findall(r"[A-Z0-9]+", raw)
    candidates = [token for token in tokens if any(char.isalpha() for char in token)]
    if not candidates:
        return ""
    if len(candidates) > 1 and all(
        token in SUPPLIER_MATERIAL_FLUTES for token in candidates
    ):
        return ""
    code = candidates[0]
    return code[:expected_length] if expected_length else code
