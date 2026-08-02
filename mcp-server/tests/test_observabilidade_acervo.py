import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import observabilidade_acervo


class ObservabilidadeAcervoTests(unittest.TestCase):
    def setUp(self):
        observabilidade_acervo.limpar_para_testes()

    def test_percentis_e_minimizacao(self):
        for valor in range(1, 101):
            observabilidade_acervo.registrar(
                {
                    "total_ms": valor,
                    "itens": valor * 10,
                    "ignorado": "CNJ não pode entrar",
                }
            )

        resumo = observabilidade_acervo.resumo()
        self.assertEqual(resumo["amostras_consultas"], 100)
        self.assertEqual(resumo["metricas"]["total_ms"]["p50"], 50)
        self.assertEqual(resumo["metricas"]["total_ms"]["p95"], 95)
        self.assertEqual(resumo["metricas"]["total_ms"]["p99"], 99)
        self.assertNotIn("ignorado", resumo["metricas"])
        self.assertFalse(resumo["dados_processuais_incluidos"])
