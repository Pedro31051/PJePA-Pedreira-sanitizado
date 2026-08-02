import os
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import server


class ComarcaCalendarsTests(unittest.TestCase):
    def setUp(self):
        self.original_env = os.environ.get("PJE_FERIADOS_LOCAIS")
        self.dados_dir = Path(__file__).resolve().parents[1] / "resources" / "calendars"

    def tearDown(self):
        if self.original_env is not None:
            os.environ["PJE_FERIADOS_LOCAIS"] = self.original_env
        else:
            os.environ.pop("PJE_FERIADOS_LOCAIS", None)

    def test_carregar_comarca_belem(self):
        # Excluir para testar a criação dinâmica
        belem_file = self.dados_dir / "feriados_belem.txt"
        if belem_file.exists():
            belem_file.unlink()

        res = server.carregar_comarca("belem")
        self.assertEqual(res["status"], "sucesso")
        self.assertTrue(belem_file.exists())
        self.assertEqual(os.environ["PJE_FERIADOS_LOCAIS"], str(belem_file))
        self.assertGreater(res["total_datas"], 0)
        self.assertIn("2026-01-12", res["datas_carregadas"])

    def test_carregar_comarca_invalida(self):
        res = server.carregar_comarca("")
        self.assertIn("erro", res)
