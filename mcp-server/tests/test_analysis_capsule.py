"""Testa descarte somente em raiz temporária pertencente ao teste."""

from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

capsules = importlib.import_module("analysis_capsule")


class AnalysisCapsuleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "owned-capsules"

    def tearDown(self):
        self.temporary.cleanup()

    def test_test_passed_purges_every_process_file(self):
        capsule = capsules.create_capsule(mode="training", root=self.root)
        capsule.file("source/process.pdf").write_bytes(b"synthetic-pdf")
        capsule.file("text/page-1.txt").write_text("synthetic", encoding="utf-8")

        receipt = capsules.record_test_result(
            capsule.capsule_id, passed=True, root=self.root
        )

        self.assertTrue(receipt["purged"])
        self.assertTrue(receipt["verified_absent"])
        self.assertEqual(receipt["reason"], "test_passed")
        self.assertFalse(capsule.path.exists())

    def test_failed_test_is_bounded_to_twenty_four_hours(self):
        capsule = capsules.create_capsule(
            mode="training", root=self.root, failed_retention=timedelta(hours=24)
        )
        result = capsules.record_test_result(
            capsule.capsule_id, passed=False, root=self.root
        )
        self.assertFalse(result["purged"])
        self.assertEqual(result["maximum_retention_hours"], 24)
        self.assertTrue(capsule.path.exists())

        with self.assertRaises(capsules.CapsuleError):
            capsules.create_capsule(
                mode="training",
                root=self.root,
                failed_retention=timedelta(hours=24, seconds=1),
            )

    def test_rejects_traversal_and_symlink_targets(self):
        with self.assertRaises(capsules.CapsuleError):
            capsules.purge_capsule(
                "../../outside", reason="test_passed", root=self.root
            )
        capsule = capsules.create_capsule(mode="training", root=self.root)
        with self.assertRaises(capsules.CapsuleError):
            capsule.file("../outside.txt")


if __name__ == "__main__":
    unittest.main()
