"""Test suite for domain extractors integration & contracts."""

import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import analise_processual_completa as complete
import server
from domain_analyzers import extract_domain_data


class TestDomainIntegration(unittest.IsolatedAsyncioTestCase):
    def test_factory_dispatcher(self):
        docs = [{"texto": "Petição de inventário do falecido João Santos."}]
        res_inv = extract_domain_data("inventory/v1", docs)
        self.assertEqual(res_inv.get("schema_version"), "inventory/v1")

        docs_usuc = [{"texto": "Usucapião especial com área de 300 m2."}]
        res_usuc = extract_domain_data("adverse-possession/v1", docs_usuc)
        self.assertEqual(res_usuc.get("schema_version"), "adverse-possession/v1")

        res_unknown = extract_domain_data("unsupported_profile", docs)
        self.assertEqual(res_unknown, {})

    def test_dossier_auto_detection_inventory(self):
        base = {
            "case_class": "Inventário e Partilha",
            "subject": "Sucessões",
            "parties": [{"nome": "Maria Santos", "polo": "AT"}]
        }
        docs = [
            {
                "id": "doc-1",
                "titulo": "Inicial",
                "texto": "Inventariado: João da Silva. Herdeiro: Pedro. ITCD recolhido."
            }
        ]
        manifest = [{"id": "doc-1", "titulo": "Inicial", "schema_version": "pje.document-manifest/v2"}]

        dossier = complete.build_dossier(
            process_number="0001234-56.2025.8.14.0001",
            base=base,
            manifest=manifest,
            documents=docs,
            document_states_value=[{"id": "doc-1", "status": "processed"}],
            tree_complete=True,
            expedients=[],
            collected_at="2026-07-31T20:00:00Z",
            domain_profile="auto",
        )

        domain_dim = dossier["completeness"]["domain_analysis"]
        self.assertTrue(domain_dim["required"])
        self.assertEqual(domain_dim["profile"], "inventory/v1")
        self.assertEqual(domain_dim["schema_version"], "inventory/v1")
        self.assertEqual(domain_dim["status"], "complete")
        self.assertIn("parties", domain_dim["data"])

    def test_dossier_auto_detection_usucapiao(self):
        base = {
            "case_class": "Usucapião Urbana",
            "subject": "Propriedade",
        }
        docs = [
            {
                "id": "doc-2",
                "titulo": "Inicial Usucapião",
                "texto": "Usucapião de imóvel com área de 200 m2. Confrontante: José."
            }
        ]
        manifest = [{"id": "doc-2", "titulo": "Inicial Usucapião", "schema_version": "pje.document-manifest/v2"}]

        dossier = complete.build_dossier(
            process_number="0009876-54.2025.8.14.0001",
            base=base,
            manifest=manifest,
            documents=docs,
            document_states_value=[{"id": "doc-2", "status": "processed"}],
            tree_complete=True,
            expedients=[],
            collected_at="2026-07-31T20:00:00Z",
            domain_profile="auto",
        )

        domain_dim = dossier["completeness"]["domain_analysis"]
        self.assertTrue(domain_dim["required"])
        self.assertEqual(domain_dim["profile"], "adverse-possession/v1")
        self.assertEqual(domain_dim["schema_version"], "adverse-possession/v1")
        self.assertEqual(domain_dim["status"], "complete")

    async def test_server_standalone_domain_tools(self):
        docs_inv = [
            {
                "texto": "Falecido: Carlos Eduardo. Meeira: Ana Lúcia. ITCD isento."
            }
        ]
        res_inv = await server.extrair_dados_inventory(documentos=docs_inv)
        self.assertEqual(res_inv["schema_version"], "inventory/v1")
        self.assertEqual(res_inv["parties"]["deceased"]["name"], "Carlos Eduardo")

        docs_usuc = [
            {
                "texto": "Usucapião rural com 5 hectares. Memorial descritivo presente."
            }
        ]
        res_usuc = await server.extrair_dados_usucapiao(documentos=docs_usuc)
        self.assertEqual(res_usuc["schema_version"], "adverse-possession/v1")
        self.assertEqual(res_usuc["land_area"]["area_hectares"], 5.0)


if __name__ == "__main__":
    unittest.main()
