"""Adversarial Stress Test Suite for Milestone M4 (test_adversarial_m4_stress.py).

Subject inventory_v1, adverse_possession_v1, and domain_rules to boundary conditions,
corrupted inputs, missing keys, extreme numbers, empty dossiers, unexpected data types, and conflicting data.
Verifies that zero unhandled exceptions (uncaught KeyError, AttributeError, TypeError, ValueError)
occur and that DomainValidationResult gracefully reports errors/risks.
"""

from __future__ import annotations

from domain_profiles.adverse_possession_v1 import (
    DomainValidationResult as AdversePossessionValidationResult,
)
from domain_profiles.adverse_possession_v1 import (
    extract_adverse_possession_v1,
    validate_adverse_possession_v1,
)
from domain_profiles.inventory_v1 import (
    DomainValidationResult as InventoryValidationResult,
)
from domain_profiles.inventory_v1 import (
    extract_inventory_v1,
    validate_inventory_v1,
)
from domain_rules import validate_domain_profile


class TestDomainRulesDispatcherStress:
    """Stress tests for domain_rules.py dispatcher function `validate_domain_profile`."""

    def test_non_string_profile_name_graceful_handling(self):
        """Verify that non-string profile names (int, float, list, dict, bool, None) do not cause AttributeError or crash."""
        invalid_profiles = [None, 123, 45.6, True, False, [], ["inventory/v1"], {"profile": "inventory/v1"}, object()]
        for invalid_name in invalid_profiles:
            res = validate_domain_profile({}, invalid_name)
            assert isinstance(res, dict)
            assert res.get("status") == "NON_COMPLIANT"
            assert res.get("completeness_score") == 0.0

    def test_empty_and_whitespace_profile_name(self):
        """Verify empty string or whitespace profile names return unhandled error risk status cleanly."""
        for empty_name in ["", "   ", "\t\n"]:
            res = validate_domain_profile({}, empty_name)
            assert isinstance(res, dict)
            assert res.get("status") == "NON_COMPLIANT"

    def test_unregistered_profile_name(self):
        """Verify unregistered profile names return UNSUPPORTED_PROFILE status cleanly."""
        res = validate_domain_profile({"some": "data"}, "unknown/profile_v999")
        assert isinstance(res, dict)
        assert res.get("status") == "NON_COMPLIANT"
        assert any("UNSUPPORTED_PROFILE" in risk for risk in res.get("procedural_risks", []))

    def test_non_dict_dossier_input(self):
        """Verify passing non-dict dossier types (None, int, str, list, bool) doesn't crash validate_domain_profile."""
        invalid_dossiers = [None, 123, "invalid_dossier_string", [1, 2, 3], True, False, object()]
        for inv_dossier in invalid_dossiers:
            res_inv = validate_domain_profile(inv_dossier, "inventory/v1")
            assert isinstance(res_inv, dict)
            res_adv = validate_domain_profile(inv_dossier, "adverse-possession/v1")
            assert isinstance(res_adv, dict)

    def test_corrupted_dossier_inner_structure(self):
        """Verify corrupted nested keys (completeness, domain_analysis, data as non-dicts) don't crash validate_domain_profile."""
        corrupted_dossiers = [
            {"completeness": "not_a_dict"},
            {"completeness": {"domain_analysis": 12345}},
            {"completeness": {"domain_analysis": {"data": "not_a_dict"}}},
            {"completeness": {"domain_analysis": {"data": None}}},
        ]
        for c_dossier in corrupted_dossiers:
            res = validate_domain_profile(c_dossier, "inventory/v1")
            assert isinstance(res, dict)
            assert "completeness_score" in res


class TestInventoryV1ExtractorStress:
    """Stress tests for `extract_inventory_v1` with corrupted, malformed, and adversarial inputs."""

    def test_dossier_data_unexpected_types(self):
        """Verify extract_inventory_v1 handles dossier_data as None, str, int, list, object without crashing."""
        invalid_inputs = [None, "", "some raw string text", 123, 45.6, True, False, [1, 2, "str"], object()]
        for inp in invalid_inputs:
            res = extract_inventory_v1(inp)
            assert isinstance(res, dict)
            assert res.get("schema_version") == "inventory/v1"

    def test_case_base_unexpected_types(self):
        """Verify extract_inventory_v1 handles case_base as str, int, list, object without AttributeError on .get()."""
        invalid_case_bases = ["str_cb", 123, 45.6, True, [1, 2], object()]
        for cb in invalid_case_bases:
            res = extract_inventory_v1({}, case_base=cb)
            assert isinstance(res, dict)

    def test_documents_list_with_corrupted_elements(self):
        """Verify list of documents containing None, ints, invalid dicts, or None text fields parses without crash."""
        corrupted_docs = [
            None,
            123,
            "just a string doc",
            {"texto": None},
            {"teor": 456},
            {"conteudo": [1, 2, 3]},
            {"pages": [None, 123, {"text": None}, {"text": 789}]},
        ]
        res = extract_inventory_v1({"documents": corrupted_docs})
        assert isinstance(res, dict)

    def test_massive_string_input(self):
        """Verify extract_inventory_v1 handles a massive text input (1MB+) with high repetition cleanly."""
        huge_text = ("Inventariado: João da Silva Santos. Óbito em 10/10/2020. Herdeiro: Pedro. " * 5000)
        res = extract_inventory_v1([{"texto": huge_text}])
        assert isinstance(res, dict)
        assert res.get("parties", {}).get("deceased", {}).get("name") is not None

    def test_extreme_and_malformed_monetary_values(self):
        """Verify handling of extreme, negative, NaN, Inf, and malformed numbers in monetary regex parsing."""
        texts = [
            "Imóvel urbano avaliado em R$ 999.999.999.999.999.999,99",
            "Saldo bancário R$ -5.000,00",
            "Dívida cobrada no valor de R$ 0,0000001",
            "Imóvel rural R$ ABC.DEF,GH",
        ]
        for txt in texts:
            res = extract_inventory_v1([{"texto": txt}])
            assert isinstance(res, dict)


class TestInventoryV1ValidatorStress:
    """Stress tests for `validate_inventory_v1` with corrupted profile_data dicts."""

    def test_profile_data_unexpected_types(self):
        """Verify validate_inventory_v1 handles profile_data as None, str, int, list, object gracefully."""
        invalid_data = [None, "", 123, 45.6, True, [], object()]
        for inv in invalid_data:
            res = validate_inventory_v1(inv)
            assert isinstance(res, InventoryValidationResult)
            assert res.status in ("INCOMPLETE", "NON_COMPLIANT", "COMPLETE")

    def test_corrupted_parties_and_deceased(self):
        """Verify corrupted types in parties/deceased (str, int, list instead of dict) do not raise AttributeError."""
        corrupted_profiles = [
            {"parties": "not_a_dict"},
            {"parties": 12345},
            {"parties": {"deceased": "not_a_dict"}},
            {"parties": {"deceased": {"death_certificate": "not_a_dict"}}},
        ]
        for p in corrupted_profiles:
            res = validate_inventory_v1(p)
            assert isinstance(res, InventoryValidationResult)

    def test_corrupted_heirs_and_shares(self):
        """Verify corrupted types in heirs/shares fields do not raise AttributeError."""
        corrupted_profiles = [
            {"heirs": "not_a_dict"},
            {"heirs": 12345},
            {"shares": "not_a_dict"},
            {"shares": 12345},
        ]
        for p in corrupted_profiles:
            res = validate_inventory_v1(p)
            assert isinstance(res, InventoryValidationResult)

    def test_corrupted_properties_and_total_value_formatting(self):
        """Verify non-numeric values in properties.total_declared_value do not raise TypeError/ValueError during string formatting."""
        corrupted_values = [
            "1000000.0",  # string instead of float
            None,
            [100.0],
            {"val": 50},
            "invalid_str",
        ]
        for val in corrupted_values:
            p_data = {
                "rite_type": "arrolamento_comum",
                "properties": {
                    "total_declared_value": val,
                    "properties": [{"description": "Imóvel", "matricula_number": "123"}],
                },
            }
            res = validate_inventory_v1(p_data)
            assert isinstance(res, InventoryValidationResult)

    def test_corrupted_registrations_and_conflicts_items(self):
        """Verify registrations and conflicts containing non-dict items (None, str, int) do not raise AttributeError."""
        p_data = {
            "registrations": [None, 123, "invalid_reg", {"matricula_number": "100", "real_encumbrances": ["usufruto"]}],
            "conflicts": {
                "conflicts": [None, 123, "invalid_conflict", {"conflict_type": "partition_disagreement", "description": "Desc"}],
            },
        }
        res = validate_inventory_v1(p_data)
        assert isinstance(res, InventoryValidationResult)


class TestAdversePossessionV1ExtractorStress:
    """Stress tests for `extract_adverse_possession_v1` with corrupted, malformed, and adversarial inputs."""

    def test_dossier_data_unexpected_types(self):
        """Verify extract_adverse_possession_v1 handles dossier_data as None, str, int, list, object without crashing."""
        invalid_inputs = [None, "", "some raw string text", 123, 45.6, True, False, [1, 2, "str"], object()]
        for inp in invalid_inputs:
            res = extract_adverse_possession_v1(inp)
            assert isinstance(res, dict)
            assert res.get("schema_version") == "adverse-possession/v1"

    def test_case_base_unexpected_types(self):
        """Verify extract_adverse_possession_v1 handles case_base as str, int, list, object without AttributeError on .get()."""
        invalid_case_bases = ["str_cb", 123, 45.6, True, [1, 2], object()]
        for cb in invalid_case_bases:
            res = extract_adverse_possession_v1({}, case_base=cb)
            assert isinstance(res, dict)

    def test_malformed_area_and_years_regex_inputs(self):
        """Verify handling of malformed years and land area text snippets."""
        texts = [
            "Posse mansa por 10.5.2 anos no local",
            "Área total de 9999999999999999999999999999 m²",
            "Posse contínua por -15 anos",
            "Confrontante ao norte: ",
        ]
        for txt in texts:
            res = extract_adverse_possession_v1([{"texto": txt}])
            assert isinstance(res, dict)


class TestAdversePossessionV1ValidatorStress:
    """Stress tests for `validate_adverse_possession_v1` with corrupted profile_data dicts."""

    def test_profile_data_unexpected_types(self):
        """Verify validate_adverse_possession_v1 handles profile_data as None, str, int, list, object gracefully."""
        invalid_data = [None, "", 123, 45.6, True, [], object()]
        for inv in invalid_data:
            res = validate_adverse_possession_v1(inv)
            assert isinstance(res, AdversePossessionValidationResult)
            assert res.status in ("INCOMPLETE", "NON_COMPLIANT", "COMPLETE")

    def test_corrupted_confrontantes_and_land_area(self):
        """Verify corrupted types in confrontantes/land_area do not raise AttributeError."""
        corrupted_profiles = [
            {"confrontantes": "not_a_dict"},
            {"confrontantes": 12345},
            {"land_area": "not_a_dict"},
            {"land_area": {"technical_responsibility": "not_a_dict"}},
        ]
        for p in corrupted_profiles:
            res = validate_adverse_possession_v1(p)
            assert isinstance(res, AdversePossessionValidationResult)

    def test_corrupted_possession_years_and_area_comparison_operators(self):
        """Verify string, list, dict, or None in possession_time_years_claimed, area_m2, area_hectares do not cause TypeError during comparison operators (<, >)."""
        corrupted_years = ["10.0", [10.0], {"years": 10}, "invalid"]
        for y in corrupted_years:
            p_data = {
                "possession_type": "extraordinaria",
                "possession_time_years_claimed": y,
                "land_area": {"area_m2": y, "area_hectares": y},
            }
            res = validate_adverse_possession_v1(p_data)
            assert isinstance(res, AdversePossessionValidationResult)

    def test_corrupted_public_notices_and_registered_owner(self):
        """Verify corrupted types in public_notices and registered_owner do not raise AttributeError."""
        corrupted_profiles = [
            {"public_notices": "not_a_dict"},
            {"public_notices": {"uniao": "not_a_dict"}},
            {"registered_owner": "not_a_dict"},
            {"registered_owner": 12345},
        ]
        for p in corrupted_profiles:
            res = validate_adverse_possession_v1(p)
            assert isinstance(res, AdversePossessionValidationResult)

    def test_corrupted_conflicts_list(self):
        """Verify conflicts containing non-dict items (None, str, int) do not raise AttributeError."""
        p_data = {
            "conflicts": {
                "conflicts": [None, 123, "invalid_conflict", {"conflict_type": "public_domain_claim", "description": "Desc"}],
            }
        }
        res = validate_adverse_possession_v1(p_data)
        assert isinstance(res, AdversePossessionValidationResult)
