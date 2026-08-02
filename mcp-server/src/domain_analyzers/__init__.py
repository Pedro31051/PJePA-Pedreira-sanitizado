"""Domain Analyzers Package for PJePA / Pedreira.

Provides specialized extractors for inventory/v1 and adverse-possession/v1.
"""

from typing import Any, Dict, List, Optional

from .adverse_possession_v1 import extract_adverse_possession_v1
from .inventory_v1 import extract_inventory_v1

SUPPORTED_PROFILES = {
    "inventory/v1": extract_inventory_v1,
    "inventario": extract_inventory_v1,
    "inventario/v1": extract_inventory_v1,
    "arrolamento": extract_inventory_v1,
    "partilha": extract_inventory_v1,
    "adverse-possession/v1": extract_adverse_possession_v1,
    "usucapiao": extract_adverse_possession_v1,
    "usucapiao/v1": extract_adverse_possession_v1,
    "usucapião": extract_adverse_possession_v1,
}


def extract_domain_data(
    domain_profile: str,
    documents: List[Dict[str, Any]],
    case_base: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Despacha a extração de domínio especializada conforme o perfil solicitado."""
    if not domain_profile:
        return {}
    extractor = SUPPORTED_PROFILES.get(domain_profile.strip().lower())
    if extractor:
        return extractor(documents, case_base)
    return {}
