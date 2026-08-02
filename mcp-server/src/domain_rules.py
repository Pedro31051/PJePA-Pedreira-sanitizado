"""Domain Rules Engine: Unified registry and dispatcher for domain profile validation.

Registers domain profiles ("inventory/v1", "adverse-possession/v1") and provides
the central dispatcher `validate_domain_profile` to extract and validate procedural rite rules.
"""

from typing import Any, Dict

from domain_profiles.adverse_possession_v1 import (
    extract_adverse_possession_v1,
    validate_adverse_possession_v1,
)
from domain_profiles.inventory_v1 import (
    extract_inventory_v1,
    validate_inventory_v1,
)

REGISTERED_PROFILES = {
    "inventory/v1": (extract_inventory_v1, validate_inventory_v1),
    "inventario": (extract_inventory_v1, validate_inventory_v1),
    "inventario/v1": (extract_inventory_v1, validate_inventory_v1),
    "arrolamento": (extract_inventory_v1, validate_inventory_v1),
    "partilha": (extract_inventory_v1, validate_inventory_v1),
    "adverse-possession/v1": (extract_adverse_possession_v1, validate_adverse_possession_v1),
    "usucapiao": (extract_adverse_possession_v1, validate_adverse_possession_v1),
    "usucapiao/v1": (extract_adverse_possession_v1, validate_adverse_possession_v1),
    "usucapião": (extract_adverse_possession_v1, validate_adverse_possession_v1),
}


def validate_domain_profile(dossier: Dict[str, Any], profile_name: str) -> Dict[str, Any]:
    """Valida as regras de negócio e de rito para o perfil de domínio especificado no dossiê.

    Args:
        dossier: Dicionário contendo dados estruturados ou brutos do dossiê processual.
        profile_name: Nome do perfil de domínio (ex: "inventory/v1", "adverse-possession/v1").

    Returns:
        Dicionário estruturado com completeness_score, missing_mandatory_docs,
        missing_citations, detected_conflicts, procedural_risks, status e profile_data.
    """
    if not isinstance(profile_name, str) or not profile_name.strip():
        return {
            "profile_name": str(profile_name) if profile_name is not None else "unknown",
            "completeness_score": 0.0,
            "status": "NON_COMPLIANT",
            "missing_mandatory_docs": [],
            "missing_citations": [],
            "detected_conflicts": [],
            "procedural_risks": ["PROFILE_UNSPECIFIED: Nenhum perfil de domínio foi informado"],
            "profile_data": {},
            "validation_details": {},
        }

    norm_profile = profile_name.strip().lower()
    entry = REGISTERED_PROFILES.get(norm_profile)
    if not entry:
        return {
            "profile_name": str(profile_name),
            "completeness_score": 0.0,
            "status": "NON_COMPLIANT",
            "missing_mandatory_docs": [],
            "missing_citations": [],
            "detected_conflicts": [],
            "procedural_risks": [f"UNSUPPORTED_PROFILE: O perfil '{profile_name}' não está registrado no sistema"],
            "profile_data": {},
            "validation_details": {},
        }

    extractor_fn, validator_fn = entry

    # Extract profile data from dossier if not already extracted
    profile_data = {}
    if isinstance(dossier, dict):
        comp = dossier.get("completeness")
        if isinstance(comp, dict):
            dom_dim = comp.get("domain_analysis")
            if isinstance(dom_dim, dict):
                data = dom_dim.get("data")
                if isinstance(data, dict):
                    profile_data = data

    if not profile_data:
        profile_data = extractor_fn(dossier if isinstance(dossier, (dict, list)) else {})

    val_res = validator_fn(profile_data)
    res_dict = val_res.model_dump() if hasattr(val_res, "model_dump") else dict(val_res)

    res_dict["profile_data"] = profile_data
    return res_dict
