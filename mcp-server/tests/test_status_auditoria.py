"""Regressões de saúde, sondas e inventário da superferramenta de status."""

import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import server

ARMAZENAMENTO_OK = {
    "1g": {
        "permissao_escrita": True,
        "espaco_livre_gb": 10,
        "pasta": "/tmp/pje-tests/1g",
    },
    "2g": {
        "permissao_escrita": True,
        "espaco_livre_gb": 10,
        "pasta": "/tmp/pje-tests/2g",
    },
}

JOBS_VAZIOS = {
    "em_andamento": 0,
    "concluidos": 0,
    "com_erro": 0,
    "jobs": [],
}


class StatusAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_credencial_ausente_reprova_saude_sem_expor_segredo(self):
        sessao = {
            "credenciais_keychain": False,
            "recursos_processo": {
                "uso_descritores_percentual": 1,
                "descritores_abertos": 10,
                "limite_descritores_soft": 1024,
                "subprocessos_filhos": 0,
            },
        }

        with (
            patch.object(server.cliente_singleton, "status_sessao", return_value=sessao),
            patch.object(
                server.pje_downloader,
                "checar_escrita_storage",
                return_value=ARMAZENAMENTO_OK,
            ),
            patch.object(
                server.pje_downloader,
                "listar_jobs",
                return_value=JOBS_VAZIOS,
            ),
            patch.object(
                server.asyncio,
                "to_thread",
                new=AsyncMock(return_value={"status_code": 200, "acessivel": True}),
            ),
        ):
            result = await server.auditoria_mcp_pje()

        self.assertFalse(result["saudavel"])
        self.assertTrue(
            any("credenciais ausentes" in alerta for alerta in result["alertas"])
        )
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("totp_seed", serialized)
        self.assertNotIn("senha", serialized)

    async def test_as_tres_sondas_sao_agendadas_concorrentemente(self):
        chamadas = []
        ativas = 0
        pico_ativas = 0

        async def sonda_lenta(_func, url):
            nonlocal ativas, pico_ativas
            chamadas.append(url)
            ativas += 1
            pico_ativas = max(pico_ativas, ativas)
            try:
                await asyncio.sleep(0.02)
                return {"status_code": 200, "acessivel": True}
            finally:
                ativas -= 1

        sessao = {
            "credenciais_keychain": True,
            "recursos_processo": {
                "uso_descritores_percentual": 1,
                "subprocessos_filhos": 0,
            },
        }

        with (
            patch.object(server.cliente_singleton, "status_sessao", return_value=sessao),
            patch.object(
                server.pje_downloader,
                "checar_escrita_storage",
                return_value=ARMAZENAMENTO_OK,
            ),
            patch.object(
                server.pje_downloader,
                "listar_jobs",
                return_value=JOBS_VAZIOS,
            ),
            patch.object(server.asyncio, "to_thread", new=sonda_lenta),
        ):
            inicio = time.perf_counter()
            result = await server.auditoria_mcp_pje()
            duracao = time.perf_counter() - inicio

        self.assertEqual(len(chamadas), 3)
        self.assertEqual(pico_ativas, 3)
        self.assertLess(duracao, 0.5)
        self.assertTrue(result["saudavel"])

    async def test_capacidades_reflete_todas_as_acoes_publicadas(self):
        result = await server.inventario_capacidades()
        tools = await server.mcp.list_tools()
        expected_total = sum(
            len(actions)
            for actions in (
                server._ACOES_STATUS,
                server._ACOES_PAINEL,
                server._ACOES_BUSCAR,
                server._ACOES_ANALISAR,
                server._ACOES_DOCUMENTOS,
                server._ACOES_DOWNLOAD,
                server._ACOES_PRODUCAO,
                server._ACOES_AUDITORIA_PROCESSUAL,
                server._ACOES_ANALISE_COMPLETA,
                server._ACOES_AUTOS_DIGITAIS,
                server._MODOS_RASTREIO_AR,
                server._ACOES_ATUACAO,
                server._ACOES_EVIDENCIA,
                server._ACOES_CONSULTAR_PROCESSO,
                server._ACOES_ANALISAR_AUTOS_LOTE,
                server._ACOES_AUTOMACAO_NAVEGADOR,
                server._ACOES_ETIQUETA_PROCESSO,
                server._ACOES_RETIFICACAO_AUTUACAO,
            )
        )

        tool_names = {tool.name for tool in tools}
        inventory_names = {
            ferramenta["ferramenta"] for ferramenta in result["ferramentas"]
        }
        self.assertEqual(result["total_ferramentas"], len(tool_names))
        self.assertEqual(result["ferramentas_com_resultado"], len(tool_names))
        self.assertEqual(inventory_names, tool_names)
        self.assertEqual(result["total_acoes"], expected_total)
        rastreio = next(
            ferramenta
            for ferramenta in result["ferramentas"]
            if ferramenta["ferramenta"] == "pje_rastrear_ar_correios"
        )
        self.assertNotEqual(rastreio["seletor"], "acao")
        self.assertNotIn("\x00", json.dumps(result, ensure_ascii=False))

    async def test_capacidades_filtra_pelo_nome_da_ferramenta(self):
        result = await server.inventario_capacidades(filtro="rastrear")

        self.assertEqual(result["ferramentas_com_resultado"], 1)
        self.assertEqual(
            result["ferramentas"][0]["ferramenta"],
            "pje_rastrear_ar_correios",
        )
        self.assertEqual(
            result["ferramentas"][0]["total_acoes"],
            len(server._MODOS_RASTREIO_AR),
        )

    async def test_jobs_delega_filtro_sem_abrir_browser(self):
        with patch.object(
            server.pje_downloader,
            "listar_jobs",
            return_value=JOBS_VAZIOS,
        ) as listar:
            result = await server.jobs_em_andamento(incluir_concluidos=False)

        listar.assert_called_once_with(incluir_concluidos=False)
        self.assertEqual(result, JOBS_VAZIOS)


if __name__ == "__main__":
    unittest.main()
