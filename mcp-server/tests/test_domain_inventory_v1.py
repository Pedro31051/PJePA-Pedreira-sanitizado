"""Test suite for Domain Profile inventory/v1: extraction, schema serialization, and rite validation."""

import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from domain_profiles.inventory_v1 import (
    DeathCertificateInfo,
    DeceasedInfo,
    HeirItem,
    InventoryProfileData,
    ITCDInfo,
    PropertyItem,
    extract_inventory_v1,
    validate_inventory_v1,
)
from domain_rules import validate_domain_profile


class TestDomainInventoryV1(unittest.TestCase):
    def test_model_serialization(self):
        """Test schema-first entity model instantiation and dict serialization."""
        death_cert = DeathCertificateInfo(
            number="123456",
            book="A-12",
            page="34",
            registry_office="Cartório do 1º Ofício",
            death_date="2025-01-15",
        )
        deceased = DeceasedInfo(
            name="João da Silva",
            cpf="111.222.333-44",
            marital_status="casado(a)",
            matrimonial_regime="comunhão_parcial",
            death_certificate=death_cert,
        )
        heir = HeirItem(
            name="Pedro da Silva",
            cpf="222.333.444-55",
            role="herdeiro",
            kinship="filho(a)",
        )
        prop = PropertyItem(
            description="Casa Residencial",
            property_type="imovel_urbano",
            matricula_number="99887",
            declared_value=350000.0,
        )
        itcd = ITCDInfo(paid=True, evaluation_completed=True)

        data = InventoryProfileData(
            parties={"deceased": deceased},
            heirs={"heirs": [heir], "total_heirs_count": 1},
            properties={"properties": [prop], "total_declared_value": 350000.0},
            itcd=itcd,
            negative_certificates_present=True,
        )

        d = data.model_dump()
        self.assertEqual(d["schema_version"], "inventory/v1")
        self.assertEqual(d["parties"]["deceased"]["name"], "João da Silva")
        self.assertEqual(d["parties"]["deceased"]["death_certificate"]["number"], "123456")
        self.assertTrue(d["itcd"]["paid"])

    def test_complete_inventory_extraction(self):
        """Test extraction from raw documents and case base."""
        documents = [
            {
                "id": "doc-1",
                "titulo": "Petição Inicial de Inventário",
                "tipo": "Petição Inicial",
                "texto": (
                    "EXCELENTÍSSIMO SENHOR DOUTOR JUIZ DE DIREITO...\n"
                    "INVENTARIANTE: Maria de Souza Santos, CPF 111.222.333-44, viúva meeira.\n"
                    "INVENTARIADO: Falecido João da Silva Santos, CPF 000.111.222-33.\n"
                    "Certidão de Óbito: termo 12345, livro A-10, folha 50, Cartório do 1º Ofício de Belém, óbito em 10/05/2025.\n"
                    "HERDEIROS: Herdeiro Pedro Santos, filho; Herdeiro Ana Santos, filha.\n"
                    "Bens a inventariar: Imóvel urbano residencial, Matrícula 98765, Cartório 2º CRI de Belém, valor declared R$ 500.000,00.\n"
                    "Consta averbação de usufruto vidual e penhora trabalhista.\n"
                    "ITCD recolhido conforme guia DAE quitada.\n"
                    "Certidões negativas de débitos fiscais apresentadas.\n"
                    "Não deixou testamento."
                ),
            }
        ]
        case_base = {
            "polo_ativo": "Maria de Souza Santos",
            "polo_passivo": "Espólio de João da Silva Santos",
        }

        res = extract_inventory_v1(documents, case_base)

        self.assertEqual(res["schema_version"], "inventory/v1")
        self.assertEqual(res["parties"]["deceased"]["name"], "João da Silva Santos")
        self.assertEqual(res["parties"]["deceased"]["cpf"], "111.222.333-44")
        self.assertIsNotNone(res["parties"]["deceased"]["death_certificate"])
        self.assertEqual(res["parties"]["deceased"]["death_certificate"]["number"], "12345")

        self.assertIsNotNone(res["heirs"]["meeiro"])
        self.assertEqual(res["heirs"]["meeiro"]["name"], "Maria de Souza Santos")
        self.assertEqual(len(res["heirs"]["heirs"]), 2)

        self.assertTrue(len(res["properties"]["properties"]) > 0)
        self.assertEqual(res["properties"]["properties"][0]["matricula_number"], "98765")
        self.assertEqual(res["properties"]["properties"][0]["declared_value"], 500000.0)

        self.assertTrue(len(res["registrations"]) > 0)
        self.assertIn("usufruto", res["registrations"][0]["real_encumbrances"])

        self.assertTrue(res["itcd"]["paid"])
        self.assertFalse(res["testament_exists"])

    def test_full_validation_on_compliant_inventory_dossier(self):
        """Test validation on fully compliant inventory profile."""
        documents = [
            {
                "id": "doc-1",
                "texto": (
                    "Inventariante: Maria Santos. Falecido: João Santos.\n"
                    "Certidão de Óbito termo 999111, livro A-1, óbito 01/01/2025.\n"
                    "Herdeiro Pedro Santos, maior e capaz.\n"
                    "Matrícula 123456 do 1º CRI.\n"
                    "ITCD recolhido e quitado.\n"
                    "Certidões negativas da União, Estado e Município juntadas.\n"
                    "Plano de partilha amigável apresentado."
                ),
            }
        ]
        profile_data = extract_inventory_v1(documents)
        val_res = validate_inventory_v1(profile_data)

        self.assertEqual(val_res.profile_name, "inventory/v1")
        self.assertEqual(val_res.status, "COMPLETE")
        self.assertEqual(val_res.completeness_score, 1.0)
        self.assertEqual(len(val_res.missing_mandatory_docs), 0)

        # Test via unified domain_rules dispatcher
        dossier = {"documents": documents}
        dispatcher_res = validate_domain_profile(dossier, "inventory/v1")
        self.assertEqual(dispatcher_res["status"], "COMPLETE")
        self.assertEqual(dispatcher_res["completeness_score"], 1.0)

    def test_validation_on_missing_death_certificate(self):
        """Test validation when death certificate is missing."""
        documents = [
            {
                "texto": (
                    "Inventário dos bens deixados por Marcos Lima.\n"
                    "Não juntou certidão de óbito aos autos.\n"
                    "Imóvel matrícula 54321. ITCD pago."
                )
            }
        ]
        profile_data = extract_inventory_v1(documents)
        val_res = validate_inventory_v1(profile_data)

        self.assertIn("Certidão de Óbito do Falecido", val_res.missing_mandatory_docs)
        self.assertIn(val_res.status, ("INCOMPLETE", "NON_COMPLIANT"))
        self.assertLess(val_res.completeness_score, 1.0)

    def test_validation_on_unpaid_itcmd(self):
        """Test validation when ITCMD is not paid/exempt."""
        documents = [
            {
                "texto": (
                    "Inventariado: Carlos Eduardo, falecido em 01/02/2024.\n"
                    "Certidão de Óbito termo 888.\n"
                    "Imóvel matrícula 111222.\n"
                    "O imposto ITCD ainda não foi recolhido nem houve avaliação fiscal."
                )
            }
        ]
        profile_data = extract_inventory_v1(documents)
        val_res = validate_inventory_v1(profile_data)

        self.assertIn("Guia / Comprovante de Quitação ou Isenção do ITCMD", val_res.missing_mandatory_docs)
        self.assertTrue(any("ITCMD" in r for r in val_res.procedural_risks))

    def test_arrolamento_sumario_incapable_heirs_thresholds(self):
        """Test arrolamento sumário incompatibility when incapable heirs are present."""
        documents = [
            {
                "texto": (
                    "Ação de Arrolamento Sumário de bens de Antonio Ferreira.\n"
                    "Certidão de Óbito termo 555.\n"
                    "Herdeiro menor incapaz Gabriel Ferreira.\n"
                    "Imóvel matrícula 777. ITCD quitado."
                )
            }
        ]
        profile_data = extract_inventory_v1(documents)
        val_res = validate_inventory_v1(profile_data)

        self.assertEqual(val_res.status, "NON_COMPLIANT")
        self.assertTrue(any("INCOMPATIBILIDADE_RITO_ARROLAMENTO_SUMARIO" in r for r in val_res.procedural_risks))

    def test_extrajudicial_rite_violation_with_testament(self):
        """Test extrajudicial rite validation when testament is present."""
        documents = [
            {
                "texto": (
                    "Inventário Extrajudicial em Cartório.\n"
                    "Certidão de Óbito termo 444.\n"
                    "Consta testamento público deixado pelo falecido."
                )
            }
        ]
        profile_data = extract_inventory_v1(documents)
        val_res = validate_inventory_v1(profile_data)

        self.assertEqual(val_res.status, "NON_COMPLIANT")
        self.assertTrue(any("EXTRAJUDICIAL" in r for r in val_res.procedural_risks))

    def test_renunciation_local_scope_isolation(self):
        """Test local scope isolation for heir renunciation."""
        docs = [
            {
                "texto": (
                    "Inventariado: João Ferreira. "
                    "Herdeiro Pedro Ferreira renunciou expressamente à herança. "
                    "Herdeiro Ana Ferreira aceitou o quinhão e não renunciou."
                )
            }
        ]
        res = extract_inventory_v1(docs)
        heirs = res["heirs"]["heirs"]
        self.assertEqual(len(heirs), 2)
        pedro = next(h for h in heirs if "Pedro" in h["name"])
        ana = next(h for h in heirs if "Ana" in h["name"])
        self.assertTrue(pedro["renunciation"])
        self.assertFalse(ana["renunciation"])

    def test_separacao_total_bens_spouse_classified_as_herdeiro(self):
        """Test spouse regime classification as heir in separação total."""
        docs = [
            {
                "texto": (
                    "Inventariado: Marcos Souza, falecido no estado de casado sob o regime de separação total de bens. "
                    "Viúva: Ana Souza. Herdeiros: Bruno Souza, filho."
                )
            }
        ]
        res = extract_inventory_v1(docs)
        self.assertEqual(res["parties"]["deceased"]["matrimonial_regime"], "separação_total")
        self.assertIsNone(res["heirs"]["meeiro"])
        heir_names = [h["name"] for h in res["heirs"]["heirs"]]
        self.assertIn("Ana Souza", heir_names)


if __name__ == "__main__":
    unittest.main()
