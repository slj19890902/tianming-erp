from __future__ import annotations


STANDARD_PALLET_CONTRACT_VERSION = "standard-pallet-v1"
STANDARD_PALLET_WIDTH_MM = 1200
STANDARD_PALLET_DEPTH_MM = 1000
STANDARD_PALLET_HEIGHT_MM = 150


def standard_pallet_contract() -> dict[str, int | str]:
    """Return the only physical standard-pallet size used by warehouse maps."""

    return {
        "contract_version": STANDARD_PALLET_CONTRACT_VERSION,
        "width_mm": STANDARD_PALLET_WIDTH_MM,
        "depth_mm": STANDARD_PALLET_DEPTH_MM,
        "height_mm": STANDARD_PALLET_HEIGHT_MM,
    }
