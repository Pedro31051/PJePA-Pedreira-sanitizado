"""Domain Profiles Package for PJePA / Pedreira.

Provides specialized domain profile extractors and rite validators for inventory/v1 and adverse-possession/v1.
"""

from .adverse_possession_v1 import (
    AdversePossessionProfileData,
    AdversePossessionV1Result,
    extract_adverse_possession_v1,
    validate_adverse_possession_v1,
)
from .inventory_v1 import (
    InventoryProfileData,
    InventoryV1Result,
    extract_inventory_v1,
    validate_inventory_v1,
)

__all__ = [
    "extract_inventory_v1",
    "validate_inventory_v1",
    "extract_adverse_possession_v1",
    "validate_adverse_possession_v1",
    "InventoryV1Result",
    "InventoryProfileData",
    "AdversePossessionV1Result",
    "AdversePossessionProfileData",
]
