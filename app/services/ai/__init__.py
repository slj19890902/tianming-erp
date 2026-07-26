"""Strictly read-only AI assistant building blocks."""

from app.services.ai.inventory_assistant import (
    InventoryAssistantError,
    build_inventory_snapshot,
    generate_inventory_interpretation,
    inventory_snapshot_hash,
    validate_inventory_interpretation,
)
from app.services.ai.providers import (
    DisabledInventoryInsightProvider,
    InventoryInsightProvider,
    MockInventoryInsightProvider,
    ProviderLimits,
    ProviderUnavailable,
)

__all__ = [
    "DisabledInventoryInsightProvider",
    "InventoryAssistantError",
    "InventoryInsightProvider",
    "MockInventoryInsightProvider",
    "ProviderLimits",
    "ProviderUnavailable",
    "build_inventory_snapshot",
    "generate_inventory_interpretation",
    "inventory_snapshot_hash",
    "validate_inventory_interpretation",
]
