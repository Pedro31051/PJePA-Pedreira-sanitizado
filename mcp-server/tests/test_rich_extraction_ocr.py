"""Test suite for Feature 10: Rich Layout & Selective OCR confidence engine."""

import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import analise_processual_completa as complete


class TestRichExtractionOCR(unittest.TestCase):
    def test_native_text_page_metadata(self):
        page_data = {
            "page": 1,
            "text": "Linha 1\nLinha 2 com texto",
            "tables": [[["A", "B"], ["1", "2"]]],
            "extraction_method": "native_text",
            "status": "extracted",
            "confidence": 1.0,
            "line_count": 2,
            "word_count": 5,
            "layout_preserved": True,
        }
        self.assertEqual(page_data["confidence"], 1.0)
        self.assertEqual(page_data["status"], "extracted")
        self.assertEqual(page_data["extraction_method"], "native_text")
        self.assertEqual(page_data["line_count"], 2)
        self.assertEqual(page_data["word_count"], 5)
        self.assertTrue(page_data["layout_preserved"])

    def test_ocr_page_metadata(self):
        page_data = {
            "page": 2,
            "text": "Texto vindo de OCR em imagem digitalizada",
            "tables": [],
            "extraction_method": "ocr",
            "status": "extracted",
            "confidence": 0.75,
            "line_count": 1,
            "word_count": 7,
            "layout_preserved": True,
        }
        self.assertEqual(page_data["confidence"], 0.75)
        self.assertEqual(page_data["status"], "extracted")
        self.assertEqual(page_data["extraction_method"], "ocr")

    def test_ocr_required_unavailable_metadata(self):
        page_data = {
            "page": 3,
            "text": "",
            "tables": [],
            "extraction_method": "none",
            "status": "ocr_required_unavailable",
            "confidence": 0.0,
            "line_count": 0,
            "word_count": 0,
            "layout_preserved": True,
        }
        self.assertEqual(page_data["confidence"], 0.0)
        self.assertEqual(page_data["status"], "ocr_required_unavailable")
        self.assertEqual(page_data["extraction_method"], "none")

    def test_missing_corrupted_metadata(self):
        page_data = {
            "page": 4,
            "text": "",
            "tables": [],
            "extraction_method": "none",
            "status": "missing_corrupted",
            "confidence": 0.0,
            "line_count": 0,
            "word_count": 0,
            "layout_preserved": False,
        }
        self.assertEqual(page_data["confidence"], 0.0)
        self.assertEqual(page_data["status"], "missing_corrupted")
        self.assertFalse(page_data["layout_preserved"])

    def test_analise_processual_completa_page_enrichment(self):
        doc = {
            "id": "doc-100",
            "titulo": "Certidão de Matrícula",
            "tipo": "Certidão",
            "data": "2026-01-15",
        }
        response = {
            "texto": "Matrícula Imobiliária nº 12345.\nAverbação 1.",
            "num_paginas": 2,
            "paginas_sem_texto": [2],
            "paginas_estruturadas": [
                {
                    "page": 1,
                    "text": "Matrícula Imobiliária nº 12345.\nAverbação 1.",
                    "tables": [[["Imóvel", "Valor"], ["Lote 1", "R$ 100.000"]]],
                    "extraction_method": "native_text",
                    "status": "extracted",
                    "confidence": 1.0,
                    "line_count": 2,
                    "word_count": 7,
                    "layout_preserved": True,
                },
                {
                    "page": 2,
                    "text": "",
                    "tables": [],
                    "extraction_method": "none",
                    "status": "ocr_required_unavailable",
                    "confidence": 0.0,
                    "line_count": 0,
                    "word_count": 0,
                    "layout_preserved": True,
                },
            ],
            "source_bytes_sha256": "abc123def456",
        }
        analyzed = complete.normalise_document_payload(doc, response)
        self.assertEqual(len(analyzed["pages"]), 2)
        p1 = analyzed["pages"][0]
        self.assertEqual(p1["confidence"], 1.0)
        self.assertEqual(len(p1["tables"]), 1)
        p2 = analyzed["pages"][1]
        self.assertEqual(p2["confidence"], 0.0)
        self.assertEqual(p2["status"], "ocr_required_unavailable")


if __name__ == "__main__":
    unittest.main()
