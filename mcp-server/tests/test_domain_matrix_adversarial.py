"""Adversarial Matrix Test Suite for Milestone 4 (Domain Profiles & Validation).

Tests complex Brazilian procedural legal scenarios for Inventory and Usucapião:
- Inventory: Arrolamento Sumário/Comum value limits, invalid ITCMD exoneration, missing death cert, conflicting heirs, non-existent assets.
- Usucapião: Constitutional area limit violations (>250m² urban, >50ha rural), insufficient possession years, missing citations (Union/State/Municipality/Confrontantes/Owner), extrajudicial opposition.
- validate_domain_profile dispatcher: completeness score accuracy, missing requirements flagging, alias normalization, error handling.
"""

import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from domain_profiles.adverse_possession_v1 import (
    extract_adverse_possession_v1,
    validate_adverse_possession_v1,
)
from domain_profiles.inventory_v1 import (
    extract_inventory_v1,
    validate_inventory_v1,
)
from domain_rules import REGISTERED_PROFILES, validate_domain_profile


class TestInventoryAdversarialMatrix(unittest.TestCase):
    """Matrix tests for complex Inventory (inventário e arrolamento) legal scenarios."""

    def test_arrolamento_comum_exceeding_1000_salarios_minimos_limit(self):
        """Test Arrolamento Comum when declared estate value exceeds 1.000 salários mínimos (R$ 1.500.000,00)."""
        documents = [
            {
                "id": "doc-inv-limit-1",
                "texto": (
                    "Ação de Arrolamento Comum dos bens deixados por Alberto de Oliveira.\n"
                    "Certidão de Óbito termo 88877, livro A-5, óbito em 12/03/2025.\n"
                    "Herdeiro Carlos de Oliveira, maior e capaz.\n"
                    "Bens a inventariar: Imóvel urbano, Matrícula 55443, Cartório 1º CRI, valor declarado R$ 2.800.000,00.\n"
                    "ITCD quitado conforme guia fiscal.\n"
                    "Certidões negativas da União, Estado e Município juntadas.\n"
                    "Plano de partilha amigável apresentado."
                ),
            }
        ]
        profile_data = extract_inventory_v1(documents)
        self.assertEqual(profile_data["rite_type"], "arrolamento_comum")
        self.assertGreater(profile_data["properties"]["total_declared_value"], 1500000.0)

        val_res = validate_inventory_v1(profile_data)
        self.assertEqual(val_res.status, "NON_COMPLIANT")
        self.assertTrue(
            any("INCOMPATIBILIDADE_RITO_ARROLAMENTO_COMUM" in risk for risk in val_res.procedural_risks),
            f"Expected INCOMPATIBILIDADE_RITO_ARROLAMENTO_COMUM in risks, got: {val_res.procedural_risks}"
        )

    def test_arrolamento_sumario_incapable_heirs_and_partition_disagreement(self):
        """Test Arrolamento Sumário with incapable heirs and active partition disagreement."""
        documents = [
            {
                "id": "doc-inv-sumario-incapable",
                "texto": (
                    "Petição de Arrolamento Sumário do falecido Benedito Ramos.\n"
                    "Certidão de Óbito termo 11223, livro A-2, óbito 05/01/2025.\n"
                    "Herdeiro menor incapaz Lucas Ramos, representado por sua tutora.\n"
                    "Há divergência entre herdeiros sobre o plano de partilha apresentado.\n"
                    "Partilha impugnada pelos demais sucessores.\n"
                    "Bens: Matrícula 99887 do 2º CRI. ITCD pago. Certidões negativas ok."
                ),
            }
        ]
        profile_data = extract_inventory_v1(documents)
        self.assertEqual(profile_data["rite_type"], "arrolamento_sumario")
        self.assertTrue(profile_data["heirs"]["has_incapable_heirs"])
        self.assertEqual(profile_data["shares"]["partition_status"], "impugnada")

        val_res = validate_inventory_v1(profile_data)
        self.assertEqual(val_res.status, "NON_COMPLIANT")
        sumario_risks = [r for r in val_res.procedural_risks if "INCOMPATIBILIDADE_RITO_ARROLAMENTO_SUMARIO" in r]
        self.assertGreaterEqual(len(sumario_risks), 1, "Must detect arrolamento sumário incompatibility")

    def test_invalid_itcmd_unpaid_no_exemption_and_tax_dispute(self):
        """Test inventory with invalid ITCMD: uncollected tax, no exemption proof, and tax dispute."""
        documents = [
            {
                "id": "doc-inv-itcmd-invalid",
                "texto": (
                    "Inventário Judicial de Geraldo Medeiros.\n"
                    "Certidão de Óbito termo 33445, óbito 10/10/2024.\n"
                    "Herdeiro Fernando Medeiros.\n"
                    "Imóvel Matrícula 12345.\n"
                    "O imposto ITCD ainda não foi pago nem recolhido.\n"
                    "Petição de impugnação ao valor do imposto ITCD calculado pela SEFAZ."
                ),
            }
        ]
        profile_data = extract_inventory_v1(documents)
        self.assertFalse(profile_data["itcd"]["paid"])
        self.assertFalse(profile_data["itcd"]["exempt"])

        val_res = validate_inventory_v1(profile_data)
        self.assertIn("Guia / Comprovante de Quitação ou Isenção do ITCMD", val_res.missing_mandatory_docs)
        self.assertTrue(any("PENDENCIA_QUITACAO_ITCMD" in r for r in val_res.procedural_risks))
        self.assertTrue(any(c.get("conflict_type") == "itcmd_tax_dispute" for c in val_res.detected_conflicts))
        self.assertIn(val_res.status, ("INCOMPLETE", "NON_COMPLIANT"))

    def test_missing_death_certificate_and_missing_negative_certificates(self):
        """Test inventory with missing death certificate and missing fiscal negative certificates."""
        documents = [
            {
                "id": "doc-inv-no-death-cert",
                "texto": (
                    "Inventário Judicial de Raimundo Nonato.\n"
                    "Não juntou certidão de óbito nos autos.\n"
                    "Herdeira Maria Nonato.\n"
                    "Matrícula 77665 do 1º CRI.\n"
                    "ITCD quitado."
                ),
            }
        ]
        profile_data = extract_inventory_v1(documents)
        val_res = validate_inventory_v1(profile_data)

        # Confirm death cert is correctly flagged as missing mandatory document
        self.assertIn("Certidão de Óbito do Falecido", val_res.missing_mandatory_docs)
        self.assertIn("Certidões Negativas de Débitos Fiscais (União, Estado, Município)", val_res.missing_mandatory_docs)
        self.assertLess(val_res.completeness_score, 0.75)

    def test_conflicting_heirs_and_creditor_opposition(self):
        """Test inventory with heir impugnation, unrecognized heir conflict, and creditor opposition."""
        documents = [
            {
                "id": "doc-inv-conflicts",
                "texto": (
                    "Inventário Judicial de Sebastião Paes.\n"
                    "Certidão de Óbito termo 55443, óbito 01/01/2025.\n"
                    "Herdeiro Pedro Paes.\n"
                    "Habilitação de herdeiro impugnada pelos demais herdeiros.\n"
                    "Habilitação de crédito rejeitada com dívida impugnada por credor Banco X.\n"
                    "Divergência de partilha instalada nos autos.\n"
                    "Matrícula 44332. ITCD quitado. Certidões negativas ok."
                ),
            }
        ]
        profile_data = extract_inventory_v1(documents)
        val_res = validate_inventory_v1(profile_data)

        self.assertTrue(val_res.validation_details["total_heirs"] > 0)
        self.assertGreaterEqual(len(val_res.detected_conflicts), 2)
        conflict_types = [c.get("conflict_type") for c in val_res.detected_conflicts]
        self.assertIn("partition_disagreement", conflict_types)
        self.assertIn("heir_impugnation", conflict_types)

    def test_non_existent_assets_and_unverified_property_title(self):
        """Test inventory with no declared assets or assets lacking registration/matrícula."""
        # Case A: completely empty property list
        docs_empty_props = [
            {
                "id": "doc-no-props",
                "texto": (
                    "Inventário Judicial de Joaquim Barbosa.\n"
                    "Certidão de Óbito termo 12345, óbito 01/01/2025.\n"
                    "Herdeiro João Barbosa. ITCD isento. Certidões negativas ok."
                ),
            }
        ]
        res_empty = extract_inventory_v1(docs_empty_props)
        val_empty = validate_inventory_v1(res_empty)
        self.assertIn("Declaração de Bens e Direitos", val_empty.missing_mandatory_docs)

        # Case B: property mentioned but lacking matrícula/titulação
        docs_no_matricula = [
            {
                "id": "doc-no-mat",
                "texto": (
                    "Inventário Judicial de Joaquim Barbosa.\n"
                    "Certidão de Óbito termo 12345, óbito 01/01/2025.\n"
                    "Bens: Posse de um terreno sem matrícula imobiliária ou registro em cartório.\n"
                    "ITCD isento. Certidões negativas ok."
                ),
            }
        ]
        res_no_mat = extract_inventory_v1(docs_no_matricula)
        val_no_mat = validate_inventory_v1(res_no_mat)
        self.assertIn("Titulação / Comprovação de Propriedade dos Bens Imóveis", val_no_mat.missing_mandatory_docs)

    def test_extrajudicial_inventory_with_testament_incapable_and_litigation(self):
        """Test Extrajudicial Inventory violating all 3 core legal restrictions (testament, incapaz, litígio)."""
        documents = [
            {
                "id": "doc-extra-all-violations",
                "texto": (
                    "Escritura Pública de Inventário Extrajudicial em Cartório de Notas.\n"
                    "Certidão de Óbito termo 999, óbito 10/01/2025.\n"
                    "Existe testamento público deixado pelo de cujus.\n"
                    "Herdeiro menor incapaz Gabriel Silva.\n"
                    "Partilha impugnada com forte divergência entre herdeiros.\n"
                    "Matrícula 11223. ITCD quitado. Certidões negativas ok."
                ),
            }
        ]
        profile_data = extract_inventory_v1(documents)
        self.assertEqual(profile_data["rite_type"], "extrajudicial")

        val_res = validate_inventory_v1(profile_data)
        self.assertEqual(val_res.status, "NON_COMPLIANT")
        extra_risks = [r for r in val_res.procedural_risks if "INCOMPATIBILIDADE_RITO_EXTRAJUDICIAL" in r]
        self.assertEqual(len(extra_risks), 3, "Must flag testament, incapable heir, and litigation extrajudicial violations")


class TestUsucapiaoAdversarialMatrix(unittest.TestCase):
    """Matrix tests for complex Usucapião legal scenarios."""

    def test_urban_usucapion_area_limit_violation_350m2(self):
        """Test Urban Usucapião (Art. 183 CF) exceeding 250 m² constitutional limit (e.g. 350 m²)."""
        documents = [
            {
                "id": "doc-u-area-exceeded",
                "texto": (
                    "Ação de Usucapião Especial Urbana de lote de terreno urbano com área de 350,00 m2.\n"
                    "Posse contínua e ininterrupta por 8 anos.\n"
                    "Confrontante ao norte: Marcos Silva, citado pessoalmente.\n"
                    "Confrontante ao sul: Antonio Souza, citado pessoalmente.\n"
                    "Planta topográfica e Memorial descritivo com ART n° 998877-PA.\n"
                    "Notificação da União, Estado do Pará e Município realizadas sem oposição.\n"
                    "Citação do proprietário registral efetuada."
                ),
            }
        ]
        profile_data = extract_adverse_possession_v1(documents)
        self.assertEqual(profile_data["possession_type"], "urbana")
        self.assertEqual(profile_data["land_area"]["area_m2"], 350.0)

        val_res = validate_adverse_possession_v1(profile_data)
        self.assertEqual(val_res.status, "NON_COMPLIANT")
        self.assertTrue(
            any("EXCESSO_DE_AREA_URBANA" in r for r in val_res.procedural_risks),
            f"Expected EXCESSO_DE_AREA_URBANA in risks, got: {val_res.procedural_risks}"
        )

    def test_rural_usucapion_area_limit_violation_80ha(self):
        """Test Rural Usucapião (Art. 191 CF) exceeding 50 hectares constitutional limit (e.g. 80 ha)."""
        documents = [
            {
                "id": "doc-r-area-exceeded",
                "texto": (
                    "Ação de Usucapião Especial Rural de imóvel no interior do Estado com área de 80 ha.\n"
                    "Posse mansa e pacífica por 10 anos.\n"
                    "Confrontantes citados pessoalmente.\n"
                    "Planta topográfica e Memorial descritivo com ART n° 554433-PA.\n"
                    "Notificação da União, Estado do Pará e Município efetuadas.\n"
                    "Proprietário registral citado."
                ),
            }
        ]
        profile_data = extract_adverse_possession_v1(documents)
        self.assertEqual(profile_data["possession_type"], "rural")
        self.assertEqual(profile_data["land_area"]["area_hectares"], 80.0)

        val_res = validate_adverse_possession_v1(profile_data)
        self.assertEqual(val_res.status, "NON_COMPLIANT")
        self.assertTrue(
            any("EXCESSO_DE_AREA_RURAL" in r for r in val_res.procedural_risks),
            f"Expected EXCESSO_DE_AREA_RURAL in risks, got: {val_res.procedural_risks}"
        )

    def test_insufficient_possession_years_matrix(self):
        """Matrix test for insufficient possession years across all 5 modalities."""
        scenarios = [
            ("urbana", 4.0, 5.0, "Usucapião Urbana com 4 anos de posse"),
            ("rural", 3.0, 5.0, "Usucapião Rural com 3 anos de posse"),
            ("ordinaria", 6.0, 10.0, "Usucapião Ordinária com 6 anos de posse"),
            ("extraordinaria", 8.0, 15.0, "Usucapião Extraordinária com 8 anos de posse"),
            ("extrajudicial", 2.0, 5.0, "Usucapião Extrajudicial com 2 anos de posse"),
        ]

        for modality, claimed_yrs, req_yrs, desc in scenarios:
            with self.subTest(modality=modality, claimed_yrs=claimed_yrs):
                documents = [
                    {
                        "id": f"doc-time-{modality}",
                        "texto": (
                            f"Ação de Usucapião {modality.capitalize()}.\n"
                            f"Autor alega posse por {int(claimed_yrs)} anos ininterruptos.\n"
                            "Confrontante ao norte: João Silva, citado pessoalmente.\n"
                            "Memorial descritivo com ART n° 100200.\n"
                            "Notificação da União, Estado do Pará e Município efetuadas."
                        ),
                    }
                ]
                profile_data = extract_adverse_possession_v1(documents)
                val_res = validate_adverse_possession_v1(profile_data)

                self.assertEqual(val_res.status, "NON_COMPLIANT", f"Failed for {desc}")
                self.assertTrue(
                    any("TEMPO_DE_POSSE_INSUFICIENTE" in r for r in val_res.procedural_risks),
                    f"Expected TEMPO_DE_POSSE_INSUFICIENTE for {desc}, got: {val_res.procedural_risks}"
                )
                self.assertEqual(val_res.validation_details["claimed_possession_years"], claimed_yrs)
                self.assertEqual(val_res.validation_details["required_possession_years"], req_yrs)

    def test_missing_citations_matrix(self):
        """Matrix test for missing mandatory citations (Union, State, Municipality, Confrontantes, Owner)."""
        documents = [
            {
                "id": "doc-missing-cits",
                "texto": (
                    "Ação de Usucapião Extraordinária com posse há 15 anos.\n"
                    "Imóvel residencial com 200 m2. Memorial com ART 887766.\n"
                    "Confrontante ao norte: Paulo Santos (pendente citação).\n"
                    "Ausente notificação da União.\n"
                    "Ausente notificação do Estado do Pará.\n"
                    "Ausente notificação do Município.\n"
                    "Proprietário registral não citado nos autos."
                ),
            }
        ]
        profile_data = extract_adverse_possession_v1(documents)
        val_res = validate_adverse_possession_v1(profile_data)

        self.assertIn("Citação Pessoal / Editalícia dos Confrontantes (Vizinhos Lindeiros)", val_res.missing_citations)
        self.assertIn("Notificação / Citação da União (Fazenda Nacional)", val_res.missing_citations)
        self.assertIn("Notificação / Citação do Estado do Pará (Fazenda Estadual)", val_res.missing_citations)
        self.assertIn("Notificação / Citação do Município (Fazenda Municipal)", val_res.missing_citations)
        self.assertGreaterEqual(len(val_res.missing_citations), 4)

    def test_extrajudicial_opposition_and_public_domain_claim(self):
        """Test extrajudicial usucapião with public domain opposition (terreno de marinha) and confrontante contestation."""
        documents = [
            {
                "id": "doc-extra-opp",
                "texto": (
                    "Requerimento de Usucapião Extrajudicial perante o Cartório do 1º Ofício.\n"
                    "Posse alegada de 10 anos.\n"
                    "União opôs contestação alegando tratar-se de terreno de marinha e imóvel público.\n"
                    "Confrontante ao sul apresentou contestação impugnando a divisa."
                ),
            }
        ]
        profile_data = extract_adverse_possession_v1(documents)
        self.assertEqual(profile_data["possession_type"], "extrajudicial")
        self.assertTrue(profile_data["public_notices"]["any_public_domain_opposition"])
        self.assertTrue(profile_data["confrontantes"]["any_contested"])

        val_res = validate_adverse_possession_v1(profile_data)
        self.assertEqual(val_res.status, "NON_COMPLIANT")
        self.assertTrue(any("OPOSICAO_FAZENDA_PUBLICA" in r for r in val_res.procedural_risks))
        self.assertTrue(any("INCOMPATIBILIDADE_EXTRAJUDICIAL_LITIGIO" in r for r in val_res.procedural_risks))
        self.assertGreaterEqual(len(val_res.detected_conflicts), 2)

    def test_missing_technical_responsibility_and_ata_notarial(self):
        """Test usucapião with missing ART/RRT technical responsibility and missing Ata Notarial."""
        # Case A: Extrajudicial Usucapião without Ata Notarial and without ART
        documents_extra = [
            {
                "id": "doc-no-art-ata-extra",
                "texto": (
                    "Requerimento de Usucapião Extrajudicial perante o Cartório.\n"
                    "Posse de 6 anos.\n"
                    "Planta simples sem ART ou RRT de engenheiro.\n"
                    "Sem juntada de documento notarial."
                ),
            }
        ]
        profile_data_extra = extract_adverse_possession_v1(documents_extra)
        val_res_extra = validate_adverse_possession_v1(profile_data_extra)

        self.assertIn("Planta Topográfica e Memorial Descritivo com ART/RRT quitada", val_res_extra.missing_mandatory_docs)
        self.assertIn("Ata Notarial Lavrada por Tabelião de Notas (Art. 216-A LRP)", val_res_extra.missing_mandatory_docs)


class TestValidateDomainProfileDispatcher(unittest.TestCase):
    """Test unified validate_domain_profile dispatcher, score accuracy, aliases, and edge cases."""

    def test_unsupported_and_empty_profile_handling(self):
        """Test dispatcher response when profile is empty, unsupported, or None."""
        dossier = {"documents": [{"texto": "Algum texto"}]}

        # Case 1: Empty profile
        res_empty = validate_domain_profile(dossier, "")
        self.assertEqual(res_empty["status"], "NON_COMPLIANT")
        self.assertEqual(res_empty["completeness_score"], 0.0)
        self.assertTrue(any("PROFILE_UNSPECIFIED" in r for r in res_empty["procedural_risks"]))

        # Case 2: Unsupported profile
        res_unsupp = validate_domain_profile(dossier, "unknown/profile/v99")
        self.assertEqual(res_unsupp["status"], "NON_COMPLIANT")
        self.assertEqual(res_unsupp["completeness_score"], 0.0)
        self.assertTrue(any("UNSUPPORTED_PROFILE" in r for r in res_unsupp["procedural_risks"]))

    def test_registered_profile_aliases(self):
        """Test that canonical aliases map to correct domain validators."""
        aliases_inv = ["inventory/v1", "inventario", "inventario/v1", "arrolamento", "partilha"]
        aliases_usuc = ["adverse-possession/v1", "usucapiao", "usucapiao/v1", "usucapião"]

        for alias in aliases_inv:
            with self.subTest(alias=alias):
                self.assertIn(alias, REGISTERED_PROFILES)
                res = validate_domain_profile({"documents": []}, alias)
                self.assertIn("status", res)
                self.assertIn("completeness_score", res)

        for alias in aliases_usuc:
            with self.subTest(alias=alias):
                self.assertIn(alias, REGISTERED_PROFILES)
                res = validate_domain_profile({"documents": []}, alias)
                self.assertIn("status", res)
                self.assertIn("completeness_score", res)

    def test_completeness_score_calculation_accuracy(self):
        """Test precision and range of completeness_score in validate_domain_profile (0.0 to 1.0)."""
        # Fully compliant complete inventory
        doc_complete = [
            {
                "texto": (
                    "Inventariante: Maria Santos. Falecido: João Santos.\n"
                    "Certidão de Óbito termo 999111, livro A-1, óbito 01/01/2025.\n"
                    "Herdeiro Pedro Santos, maior e capaz.\n"
                    "Matrícula 123456 do 1º CRI.\n"
                    "ITCD recolhido e quitado.\n"
                    "Certidões negativas da União, Estado e Município juntadas.\n"
                    "Plano de partilha amigável apresentado."
                )
            }
        ]
        res_comp = validate_domain_profile({"documents": doc_complete}, "inventory/v1")
        self.assertEqual(res_comp["status"], "COMPLETE")
        self.assertEqual(res_comp["completeness_score"], 1.0)

        # Incomplete inventory missing ITCMD & negative certs
        doc_incomp = [
            {
                "texto": (
                    "Inventariante: Maria Santos. Falecido: João Santos.\n"
                    "Certidão de Óbito termo 999111, livro A-1, óbito 01/01/2025.\n"
                    "Herdeiro Pedro Santos, maior e capaz.\n"
                    "Matrícula 123456 do 1º CRI."
                )
            }
        ]
        res_incomp = validate_domain_profile({"documents": doc_incomp}, "inventory/v1")
        self.assertEqual(res_incomp["status"], "INCOMPLETE")
        self.assertGreater(res_incomp["completeness_score"], 0.0)
        self.assertLess(res_incomp["completeness_score"], 1.0)

        # Non-compliant usucapião with area violation
        doc_noncomp = [
            {
                "texto": (
                    "Ação de Usucapião Especial Urbana de lote com área de 500 m2.\n"
                    "Posse por 3 anos."
                )
            }
        ]
        res_noncomp = validate_domain_profile({"documents": doc_noncomp}, "adverse-possession/v1")
        self.assertEqual(res_noncomp["status"], "NON_COMPLIANT")
        self.assertLessEqual(res_noncomp["completeness_score"], 0.5)


if __name__ == "__main__":
    unittest.main()
