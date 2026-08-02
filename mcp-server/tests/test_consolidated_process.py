from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path

import fitz

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

capsules = importlib.import_module("analysis_capsule")
processor = importlib.import_module("consolidated_process")


class ConsolidatedProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "synthetic.pdf"
        document = fitz.open()
        page = document.new_page()
        page.insert_text((72, 72), "ID do documento: 100001 Peticao inicial sintetica " * 3)
        page = document.new_page()
        page.insert_text((72, 72), "ID do documento: 100002 Decisao sintetica fundamentada " * 3)
        document.save(self.source)
        document.close()

    def tearDown(self):
        self.temporary.cleanup()

    def test_integral_processing_and_lossless_sharding(self):
        capsule = capsules.create_capsule(mode="training", root=self.root / "capsules")
        result = processor.process_consolidated_pdf(
            self.source,
            capsule,
            document_mapping=[
                {"document_id": "100001", "start_page": 1, "end_page": 1},
                {"document_id": "100002", "start_page": 2, "end_page": 2},
            ],
        )
        self.assertTrue(result["complete"])
        self.assertEqual(result["page_count"], 2)
        self.assertEqual(result["coverage"]["pages_mapped"], 2)
        self.assertTrue(capsule.file("derived/process-searchable-analysis-only.pdf").exists())
        shards = processor.build_text_shards(result, maximum_characters=10_000)
        self.assertEqual(sum(len(shard["pages"]) for shard in shards), 2)

        receipt = capsules.record_test_result(
            capsule.capsule_id,
            passed=True,
            root=self.root / "capsules",
        )
        self.assertTrue(receipt["verified_absent"])

    def test_mapping_gap_fails_closed(self):
        capsule = capsules.create_capsule(mode="training", root=self.root / "capsules")
        with self.assertRaises(processor.ConsolidatedProcessError):
            processor.process_consolidated_pdf(
                self.source,
                capsule,
                document_mapping=[
                    {"document_id": "100001", "start_page": 1, "end_page": 1}
                ],
            )

    def test_mapping_can_be_derived_from_known_pje_ids(self):
        capsule = capsules.create_capsule(mode="training", root=self.root / "capsules")
        result = processor.process_consolidated_pdf(
            self.source,
            capsule,
            known_documents=[
                {"id": "100001", "tipo": "Petição inicial"},
                {"id": "100002", "tipo": "Decisão"},
            ],
        )
        self.assertEqual(
            [item["document_id"] for item in result["documents"]],
            ["100001", "100002"],
        )

    def test_signature_context_id_is_not_document_id(self):
        text = (
            "Documento ID: 123456789\n"
            "Assinado digitalmente pelo servidor - ID. 999999999"
        )
        self.assertEqual(processor.probable_document_ids(text), ["123456789"])

    def test_image_only_page_gets_local_ocr_and_searchable_layer(self):
        text_source = fitz.open()
        page = text_source.new_page(width=595, height=842)
        page.insert_text(
            (72, 120),
            "CERTIDAO SINTETICA DE INVENTARIO",
            fontsize=24,
        )
        image = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).tobytes("png")
        text_source.close()

        scanned = self.root / "scanned.pdf"
        image_document = fitz.open()
        image_page = image_document.new_page(width=595, height=842)
        image_page.insert_image(image_page.rect, stream=image)
        image_document.save(scanned)
        image_document.close()

        capsule = capsules.create_capsule(mode="training", root=self.root / "capsules")
        result = processor.process_consolidated_pdf(
            scanned,
            capsule,
            document_mapping=[
                {"document_id": "200001", "start_page": 1, "end_page": 1}
            ],
        )
        self.assertEqual(result["pages_with_local_ocr"], 1)
        derivative = fitz.open(
            capsule.file("derived/process-searchable-analysis-only.pdf")
        )
        try:
            self.assertIn("CERTIDAO", derivative[0].get_text("text").upper())
        finally:
            derivative.close()

        receipt = capsules.record_test_result(
            capsule.capsule_id,
            passed=True,
            root=self.root / "capsules",
        )
        self.assertTrue(receipt["verified_absent"])


if __name__ == "__main__":
    unittest.main()
