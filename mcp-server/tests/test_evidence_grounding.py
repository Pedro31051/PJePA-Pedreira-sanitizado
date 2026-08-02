"""Testes do aterramento literal de provas, somente com dados sintéticos."""

import hashlib
import importlib
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

grounding = importlib.import_module("evidence_grounding")

PAGE_1 = (
    "A inventariante Joana Sintética prestou compromisso em 10/04/2025 "
    "perante o juízo da vara única, conforme termo lavrado nos autos."
)
PAGE_2 = (
    "As primeiras declarações foram apresentadas em 13/05/2025 e relacionam "
    "seis matrículas imobiliárias do espólio sintético."
)
OTHER_PAGE = (
    "Certidão exclusiva: o oficial de justiça certificou a diligência única "
    "no endereço declinado e devolveu o mandado cumprido."
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _documents():
    return [
        {
            "document_id": "doc-1",
            "sha256": _digest(PAGE_1 + PAGE_2),
            "pages": [
                {"page": 1, "text": PAGE_1},
                {"page": 2, "text": PAGE_2},
            ],
        },
        {
            "document_id": "doc-2",
            "sha256": _digest(OTHER_PAGE),
            "pages": [{"page": 1, "text": OTHER_PAGE}],
        },
    ]


class EvidenceGroundingTests(unittest.TestCase):
    def setUp(self):
        self.documents = _documents()
        self.index = grounding.build_page_index(self.documents)
        self.doc1_sha = self.documents[0]["sha256"]
        self.doc2_sha = self.documents[1]["sha256"]

    def test_literal_evidence_intacta(self):
        result = grounding.ground_evidence(
            {
                "document_id": "doc-1",
                "page": 1,
                "excerpt": "prestou compromisso em 10/04/2025",
                "sha256": self.doc1_sha,
            },
            self.index,
        )
        self.assertTrue(result.grounded)
        self.assertEqual(result.corrections, ())

    def test_corrige_hash_alterado_pelo_modelo(self):
        result = grounding.ground_evidence(
            {
                "document_id": "doc-1",
                "page": 1,
                "excerpt": "prestou compromisso em 10/04/2025",
                "sha256": "f" * 64,
            },
            self.index,
        )
        self.assertTrue(result.grounded)
        self.assertIn("sha256_corrigido", result.corrections)
        self.assertEqual(result.sha256, self.doc1_sha)

    def test_corrige_pagina_trocada(self):
        result = grounding.ground_evidence(
            {
                "document_id": "doc-1",
                "page": 1,
                "excerpt": "primeiras declarações foram apresentadas em 13/05/2025",
                "sha256": self.doc1_sha,
            },
            self.index,
        )
        self.assertTrue(result.grounded)
        self.assertIn("pagina_corrigida", result.corrections)
        self.assertEqual(result.page, 2)

    def test_remapeia_documento_quando_unico(self):
        result = grounding.ground_evidence(
            {
                "document_id": "doc-1",
                "page": 1,
                "excerpt": "o oficial de justiça certificou a diligência única",
                "sha256": self.doc1_sha,
            },
            self.index,
        )
        self.assertTrue(result.grounded)
        self.assertIn("documento_corrigido", result.corrections)
        self.assertEqual(result.document_id, "doc-2")
        self.assertEqual(result.sha256, self.doc2_sha)

    def test_recupera_trecho_parafraseado(self):
        result = grounding.ground_evidence(
            {
                "document_id": "doc-1",
                "page": 1,
                "excerpt": (
                    "A inventariante Joana prestou o compromisso legal em "
                    "10/04/2025 perante o juízo"
                ),
                "sha256": self.doc1_sha,
            },
            self.index,
        )
        self.assertTrue(result.grounded)
        self.assertIn("trecho_aterrado", result.corrections)
        self.assertIn(result.excerpt.casefold(), PAGE_1.casefold())
        self.assertGreaterEqual(len(result.excerpt), grounding.MIN_EXCERPT_CHARS)

    def test_nao_inventa_prova_irrecuperavel(self):
        original = {
            "document_id": "doc-1",
            "page": 1,
            "excerpt": "o tribunal condenou o réu ao pagamento de multa diária",
            "sha256": self.doc1_sha,
        }
        result = grounding.ground_evidence(original, self.index)
        self.assertFalse(result.grounded)
        self.assertEqual(result.excerpt, original["excerpt"])

    def test_documento_desconhecido_sem_fuzzy(self):
        result = grounding.ground_evidence(
            {
                "document_id": "doc-999",
                "page": 1,
                "excerpt": "trecho que não existe em lugar nenhum dos autos",
                "sha256": "a" * 64,
            },
            self.index,
        )
        self.assertFalse(result.grounded)

    def test_lista_agrega_contadores(self):
        items = [
            {
                "document_id": "doc-1",
                "page": 1,
                "excerpt": "prestou compromisso em 10/04/2025",
                "sha256": "f" * 64,
            },
            {
                "document_id": "doc-1",
                "page": 1,
                "excerpt": "primeiras declarações foram apresentadas em 13/05/2025",
                "sha256": self.doc1_sha,
            },
        ]
        grounded_items, stats = grounding.ground_evidence_list(items, self.index)
        self.assertEqual(stats["evidence_total"], 2)
        self.assertEqual(stats["hashes_corrected"], 1)
        self.assertEqual(stats["pages_corrected"], 1)
        self.assertEqual(grounded_items[0]["sha256"], self.doc1_sha)
        self.assertEqual(grounded_items[1]["page"], 2)


if __name__ == "__main__":
    unittest.main()
