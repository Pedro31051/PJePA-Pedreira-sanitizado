"""Cobertura, persistência e paginação do inventário de caixas."""

import base64
import gzip
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import caixas_tarefas
import pje_client


class InventarioCaixasTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {
                "PJE_STORAGE_DIR": self.tmp.name,
                "PJE_AUDIT_MASTER_KEY": base64.urlsafe_b64encode(b"a" * 32).decode(
                    "ascii"
                ),
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_snapshot_completo_exige_contagens_iguais(self):
        sid = caixas_tarefas.novo_snapshot(
            "1g",
            "advogado",
            [{"grupo": "tarefas", "nome": "Minutar", "quantidade": 2}],
        )
        resultado = caixas_tarefas.salvar_caixa(
            sid,
            {"grupo": "tarefas", "nome": "Minutar", "quantidade": 2},
            2,
            [
                {"idTaskInstance": 1, "idProcesso": 10},
                {"idTaskInstance": 2, "idProcesso": 11},
            ],
        )
        resumo = caixas_tarefas.finalizar_snapshot(sid)
        self.assertEqual(resultado["status"], "completa")
        self.assertEqual(resumo["status"], "completo")
        self.assertEqual(resumo["cobertura_percentual"], 100.0)

    def test_inicializacao_unica_preserva_wal_permissao_e_concorrencia(self):
        def abrir_e_fechar():
            con = caixas_tarefas._conectar()
            try:
                return con.execute("PRAGMA journal_mode").fetchone()[0]
            finally:
                con.close()

        with ThreadPoolExecutor(max_workers=8) as executor:
            modos = list(executor.map(lambda _: abrir_e_fechar(), range(32)))

        self.assertEqual(set(modos), {"wal"})
        modo_arquivo = caixas_tarefas.caminho_banco().stat().st_mode & 0o777
        self.assertEqual(modo_arquivo, 0o600)

    def test_divergencia_nunca_e_marcada_completa(self):
        sid = caixas_tarefas.novo_snapshot(
            "1g",
            "advogado",
            [{"grupo": "tarefas", "nome": "Audiência", "quantidade": 3}],
        )
        caixas_tarefas.salvar_caixa(
            sid,
            {"grupo": "tarefas", "nome": "Audiência", "quantidade": 3},
            2,
            [
                {"idTaskInstance": 1, "idProcesso": 10},
                {"idTaskInstance": 2, "idProcesso": 11},
            ],
        )
        resumo = caixas_tarefas.finalizar_snapshot(sid)
        self.assertEqual(resumo["status"], "incompleto")
        self.assertEqual(resumo["caixas_divergentes"], 1)
        self.assertLess(resumo["cobertura_percentual"], 100)

    def test_mesmo_processo_em_duas_tarefas_preserva_duas_ocorrencias(self):
        caixas = [
            {"grupo": "tarefas", "nome": "A", "quantidade": 1},
            {"grupo": "tarefas", "nome": "B", "quantidade": 1},
        ]
        sid = caixas_tarefas.novo_snapshot("1g", "advogado", caixas)
        for indice, caixa in enumerate(caixas, start=1):
            caixas_tarefas.salvar_caixa(
                sid,
                caixa,
                1,
                [
                    {
                        "idTaskInstance": indice,
                        "idProcesso": 99,
                        "numeroProcesso": "0800001-61.2024.8.14.0028",
                    }
                ],
            )
        resumo = caixas_tarefas.finalizar_snapshot(sid)
        consulta = caixas_tarefas.consultar_ocorrencias(
            snapshot_id=sid, itens_por_pagina=10
        )
        self.assertEqual(resumo["ocorrencias_coletadas"], 2)
        self.assertEqual(resumo["processos_unicos"], 1)
        self.assertEqual(consulta["paginacao"]["total_itens"], 2)

    def test_consulta_paginada_e_json_integral_opcional(self):
        sid = caixas_tarefas.novo_snapshot(
            "1g",
            "advogado",
            [{"grupo": "tarefas", "nome": "X", "quantidade": 3}],
        )
        itens = [
            {
                "idTaskInstance": i,
                "numeroProcesso": f"000{i}",
                "classeJudicial": "Alimentos",
                "campoNovoFuturo": {"valor": i},
            }
            for i in range(3)
        ]
        caixas_tarefas.salvar_caixa(
            sid,
            {"grupo": "tarefas", "nome": "X", "quantidade": 3},
            3,
            itens,
        )
        caixas_tarefas.finalizar_snapshot(sid)
        pagina = caixas_tarefas.consultar_ocorrencias(
            snapshot_id=sid,
            pagina=2,
            itens_por_pagina=2,
            incluir_metadados_origem=True,
        )
        self.assertEqual(len(pagina["ocorrencias"]), 1)
        self.assertEqual(pagina["paginacao"]["total_paginas"], 2)
        self.assertIn(
            "campoNovoFuturo",
            pagina["ocorrencias"][0]["metadados_origem"],
        )

    def test_consultas_repetidas_nao_vazam_descritores_sqlite(self):
        if not Path("/proc/self/fd").exists():
            self.skipTest("contagem de descritores requer /proc")
        sid = caixas_tarefas.novo_snapshot(
            "1g",
            "advogado",
            [{"grupo": "tarefas", "nome": "X", "quantidade": 0}],
        )
        caixas_tarefas.salvar_caixa(
            sid,
            {"grupo": "tarefas", "nome": "X", "quantidade": 0},
            0,
            [],
        )
        caixas_tarefas.finalizar_snapshot(sid)

        def descritores_do_banco():
            total = 0
            for fd in Path("/proc/self/fd").iterdir():
                try:
                    if "caixas_tarefas.sqlite3" in os.readlink(fd):
                        total += 1
                except OSError:
                    pass
            return total

        antes = descritores_do_banco()
        for _ in range(100):
            caixas_tarefas.obter_snapshot(snapshot_id=sid)
            caixas_tarefas.consultar_ocorrencias(snapshot_id=sid)
        depois = descritores_do_banco()
        self.assertEqual(depois, antes)

    def test_acervo_estruturado_preserva_polos_capacidades_e_extras(self):
        sid = caixas_tarefas.novo_snapshot(
            "1g",
            "advogado",
            [{"grupo": "tarefas", "nome": "Minutar", "quantidade": 1}],
        )
        caixas_tarefas.salvar_caixa(
            sid,
            {"grupo": "tarefas", "nome": "Minutar", "quantidade": 1},
            1,
            [
                {
                    "idTaskInstance": 10,
                    "idProcesso": 99,
                    "numeroProcesso": "0800001-61.2024.8.14.0028",
                    "classeJudicial": "CumSen",
                    "classeJudicialDescricao": "Cumprimento de Sentença",
                    "codigoClasseProcessual": 156,
                    "assuntoPrincipal": "Obrigação de Fazer",
                    "poloAtivo": "AUTORA PRINCIPAL",
                    "poloPassivo": "RÉU PRINCIPAL",
                    "dataChegada": 1704067200000,
                    "ultimoMovimento": 1704153600000,
                    "podeMinutarEmLote": True,
                    "nomeResponsavelTarefa": "SERVIDOR TESTE",
                    "campoNovoFuturo": {"valor": 7},
                }
            ],
        )
        caixas_tarefas.finalizar_snapshot(sid)

        resposta = caixas_tarefas.consultar_acervo_estruturado(
            snapshot_id=sid,
            incluir_facetas=True,
        )
        registro = resposta["registros"][0]
        self.assertEqual(resposta["schema_version"], "pje.acervo-tarefas/v2")
        self.assertEqual(
            registro["processo"]["classe"]["descricao_completa"],
            "Cumprimento de Sentença",
        )
        self.assertEqual(registro["processo"]["classe"]["codigo_tpu"], "156")
        self.assertEqual(
            registro["partes"]["polos"]["ativo"][0]["nome"],
            "AUTORA PRINCIPAL",
        )
        self.assertEqual(registro["partes"]["polos"]["passivo"][0]["polo"], "PASSIVO")
        self.assertFalse(registro["partes"]["completude"]["partes_completas"])
        self.assertTrue(registro["capacidades_operacao_em_lote"]["minutar"])
        self.assertEqual(
            registro["campos_extras_origem"]["campoNovoFuturo"],
            {"valor": 7},
        )

    def test_acervo_estruturado_resolve_descricao_sem_inventar_codigo(self):
        sid = caixas_tarefas.novo_snapshot(
            "1g",
            "advogado",
            [{"grupo": "tarefas", "nome": "X", "quantidade": 1}],
        )
        caixas_tarefas.salvar_caixa(
            sid,
            {"grupo": "tarefas", "nome": "X", "quantidade": 1},
            1,
            [
                {
                    "idTaskInstance": 1,
                    "numeroProcesso": "0001",
                    "classeJudicial": "DivLit",
                }
            ],
        )
        caixas_tarefas.finalizar_snapshot(sid)
        registro = caixas_tarefas.consultar_acervo_estruturado(snapshot_id=sid)[
            "registros"
        ][0]
        classe = registro["processo"]["classe"]
        self.assertEqual(classe["descricao_completa"], "Divórcio Litigioso")
        self.assertIsNone(classe["codigo_tpu"])
        self.assertTrue(classe["codigo_tpu_ambiguo"])
        self.assertEqual(
            classe["codigos_tpu_candidatos"],
            ["12541", "12373", "99"],
        )
        self.assertEqual(classe["resolucao"], "catalogo_tpu_cnj_com_codigo_ambiguo")

    def test_acervo_estruturado_filtra_e_calcula_facetas_globais(self):
        sid = caixas_tarefas.novo_snapshot(
            "1g",
            "advogado",
            [{"grupo": "tarefas", "nome": "Triar", "quantidade": 2}],
        )
        caixas_tarefas.salvar_caixa(
            sid,
            {"grupo": "tarefas", "nome": "Triar", "quantidade": 2},
            2,
            [
                {
                    "idTaskInstance": 1,
                    "numeroProcesso": "0001",
                    "classeJudicial": "CumSen",
                    "poloAtivo": "MARIA",
                    "prioridade": True,
                    "podeMovimentarEmLote": True,
                },
                {
                    "idTaskInstance": 2,
                    "numeroProcesso": "0002",
                    "classeJudicial": "DivLit",
                    "poloAtivo": "JOÃO",
                    "prioridade": False,
                },
            ],
        )
        caixas_tarefas.finalizar_snapshot(sid)
        resposta = caixas_tarefas.consultar_acervo_estruturado(
            snapshot_id=sid,
            filtro_parte="maria",
            prioridade=True,
            incluir_facetas=True,
        )
        self.assertEqual(resposta["paginacao"]["total_itens"], 1)
        self.assertEqual(resposta["registros"][0]["processo"]["numero_cnj"], "0001")
        opcao_classe = resposta["facetas"]["classes"]["valores"][0]
        self.assertEqual(opcao_classe["valor"], "CumSen")
        self.assertEqual(opcao_classe["quantidade"], 1)
        self.assertEqual(
            opcao_classe["rotulo"],
            "Cumprimento de sentença (CumSen)",
        )
        self.assertEqual(
            opcao_classe["filtro"],
            {"parametro": "filtro_classe", "valor": "CumSen"},
        )
        todos = caixas_tarefas.consultar_acervo_estruturado(
            snapshot_id=sid,
            incluir_facetas=False,
            ordenar_por="numero_processo",
        )
        capacidade_propagada = todos["registros"][1]["capacidades_operacao_em_lote"]
        self.assertTrue(capacidade_propagada["movimentar"])
        self.assertEqual(
            capacidade_propagada["resolucao"],
            "propagada_do_bloco_de_capacidades_da_tarefa",
        )

    def test_facetas_completas_e_tarefa_normalizada_preservam_origem(self):
        sid = caixas_tarefas.novo_snapshot(
            "1g",
            "advogado",
            [
                {
                    "grupo": "tarefas",
                    "nome": "Aavaliar ato proferido de julgamento",
                    "quantidade": 1,
                }
            ],
        )
        caixas_tarefas.salvar_caixa(
            sid,
            {
                "grupo": "tarefas",
                "nome": "Aavaliar ato proferido de julgamento",
                "quantidade": 1,
            },
            1,
            [
                {
                    "idTaskInstance": 1,
                    "numeroProcesso": "0001",
                    "classeJudicial": "ProceComCiv",
                    "poloAtivo": "MARIA",
                    "poloPassivo": "JOÃO",
                }
            ],
        )
        caixas_tarefas.finalizar_snapshot(sid)

        resposta = caixas_tarefas.consultar_acervo_estruturado(
            snapshot_id=sid,
            filtro_classe="Procedimento Comum Civel",
        )
        tarefa = resposta["registros"][0]["ocorrencia"]["tarefa"]
        self.assertEqual(tarefa["nome"], "Avaliar ato proferido de julgamento")
        self.assertEqual(
            tarefa["nome_original_pje"],
            "Aavaliar ato proferido de julgamento",
        )
        self.assertEqual(
            tarefa["valor_filtro"],
            "Aavaliar ato proferido de julgamento",
        )
        self.assertTrue(tarefa["foi_normalizada"])

        faceta = resposta["facetas"]["tarefas"]
        self.assertTrue(faceta["completa"])
        self.assertFalse(faceta["truncada"])
        self.assertEqual(faceta["retornados"], 1)
        self.assertEqual(
            faceta["valores"][0]["filtro"],
            {
                "parametro": "nome_tarefa",
                "valor": "Aavaliar ato proferido de julgamento",
            },
        )
        self.assertEqual(
            resposta["qualidade_dados"]["classes"]["descricao_resolvida"],
            1,
        )

    def test_acervo_compacto_usa_dicionarios_flags_e_projecao(self):
        sid = caixas_tarefas.novo_snapshot(
            "1g",
            "advogado",
            [{"grupo": "tarefas", "nome": "Minutar", "quantidade": 1}],
        )
        caixas_tarefas.salvar_caixa(
            sid,
            {"grupo": "tarefas", "nome": "Minutar", "quantidade": 1},
            1,
            [
                {
                    "idTaskInstance": 10,
                    "numeroProcesso": "0800001-61.2024.8.14.0028",
                    "classeJudicial": "CumSen",
                    "assuntoPrincipal": "Obrigação de Fazer",
                    "poloAtivo": "MARIA",
                    "poloPassivo": "JOÃO",
                    "sigiloso": True,
                    "prioridade": True,
                    "conferido": True,
                    "tagsProcessoList": [{"nomeTag": "PP+120"}],
                }
            ],
        )
        caixas_tarefas.finalizar_snapshot(sid)

        resposta = caixas_tarefas.consultar_acervo_estruturado(
            snapshot_id=sid,
            formato="compacto",
            itens_por_pagina=9000,
            campos="cnj,tarefa,classe,assunto,flags,ocorrencia_chave",
        )
        registro = resposta["registros"][0]
        self.assertEqual(
            resposta["schema_version"],
            "pje.acervo-tarefas/v2-compacto",
        )
        self.assertEqual(resposta["paginacao"]["itens_por_pagina"], 5000)
        self.assertEqual(
            resposta["dicionarios"]["tarefas"][registro["tarefa"]],
            "Minutar",
        )
        self.assertEqual(
            resposta["dicionarios"]["classes"][registro["classe"]],
            "CumSen",
        )
        self.assertEqual(registro["flags"], 1 | 2 | 8)
        self.assertEqual(registro["ocorrencia_chave"], "Minutar:10")
        self.assertNotIn("partes", registro)

    def test_acervo_compacto_rejeita_campo_desconhecido(self):
        sid = caixas_tarefas.novo_snapshot("1g", "advogado", [])
        caixas_tarefas.finalizar_snapshot(sid)
        with self.assertRaisesRegex(ValueError, "Campos inválidos"):
            caixas_tarefas.consultar_acervo_estruturado(
                snapshot_id=sid,
                formato="compacto",
                campos="cnj,inexistente",
            )

    def test_envelope_minimo_restringe_dicionarios_a_pagina(self):
        sid = caixas_tarefas.novo_snapshot(
            "1g",
            "advogado",
            [{"grupo": "tarefas", "nome": "Triar", "quantidade": 2}],
        )
        caixas_tarefas.salvar_caixa(
            sid,
            {"grupo": "tarefas", "nome": "Triar", "quantidade": 2},
            2,
            [
                {
                    "idTaskInstance": 1,
                    "numeroProcesso": "0001",
                    "classeJudicial": "CumSen",
                    "assuntoPrincipal": "Assunto A",
                },
                {
                    "idTaskInstance": 2,
                    "numeroProcesso": "0002",
                    "classeJudicial": "DivLit",
                    "assuntoPrincipal": "Assunto B",
                },
            ],
        )
        caixas_tarefas.finalizar_snapshot(sid)

        resposta = caixas_tarefas.consultar_acervo_estruturado(
            snapshot_id=sid,
            formato="compacto",
            campos="tarefa,classe,assunto",
            itens_por_pagina=1,
            incluir_facetas=False,
            envelope="minimo",
        )
        self.assertEqual(resposta["status"], "ok")
        self.assertEqual(len(resposta["registros"]), 1)
        self.assertEqual(len(resposta["dicionarios"]["classes"]), 1)
        self.assertEqual(len(resposta["dicionarios"]["assuntos"]), 1)
        self.assertNotIn("catalogos", resposta)
        self.assertNotIn("qualidade_dados", resposta)

    def test_if_revision_retorna_not_modified_sem_registros(self):
        sid = caixas_tarefas.novo_snapshot(
            "1g",
            "advogado",
            [{"grupo": "tarefas", "nome": "Triar", "quantidade": 0}],
        )
        caixas_tarefas.salvar_caixa(
            sid,
            {"grupo": "tarefas", "nome": "Triar", "quantidade": 0},
            0,
            [],
        )
        caixas_tarefas.finalizar_snapshot(sid)
        inicial = caixas_tarefas.consultar_acervo_estruturado(
            snapshot_id=sid,
            incluir_facetas=False,
            envelope="minimo",
        )

        repetida = caixas_tarefas.consultar_acervo_estruturado(
            snapshot_id=sid,
            incluir_facetas=False,
            envelope="minimo",
            if_revision=inicial["snapshot"]["revision"],
        )
        self.assertEqual(repetida["status"], "not_modified")
        self.assertEqual(repetida["registros"], [])
        self.assertEqual(
            repetida["snapshot"]["revision"],
            inicial["snapshot"]["revision"],
        )

    def test_envelope_invalido_falha_fechado(self):
        with self.assertRaisesRegex(ValueError, "envelope deve ser"):
            caixas_tarefas.consultar_acervo_estruturado(envelope="reduzido")

    def test_cursor_fixa_snapshot_e_rejeita_alteracao_de_filtro(self):
        sid = caixas_tarefas.novo_snapshot(
            "1g",
            "advogado",
            [{"grupo": "tarefas", "nome": "Triar", "quantidade": 2}],
        )
        caixas_tarefas.salvar_caixa(
            sid,
            {"grupo": "tarefas", "nome": "Triar", "quantidade": 2},
            2,
            [
                {"idTaskInstance": 1, "numeroProcesso": "0001"},
                {"idTaskInstance": 2, "numeroProcesso": "0002"},
            ],
        )
        caixas_tarefas.finalizar_snapshot(sid)

        primeira = caixas_tarefas.consultar_acervo_estruturado(
            snapshot_id=sid,
            formato="compacto",
            campos="cnj,ocorrencia_chave",
            itens_por_pagina=1,
            ordenar_por="numero_processo",
            incluir_facetas=False,
            envelope="minimo",
        )
        cursor = primeira["paginacao"]["next_cursor"]
        segunda = caixas_tarefas.consultar_acervo_estruturado(
            formato="compacto",
            campos="cnj,ocorrencia_chave",
            itens_por_pagina=1,
            ordenar_por="numero_processo",
            incluir_facetas=False,
            envelope="minimo",
            cursor=cursor,
        )
        self.assertEqual(segunda["snapshot"]["id"], sid)
        self.assertEqual(segunda["paginacao"]["pagina"], 2)
        self.assertNotEqual(
            primeira["registros"][0]["ocorrencia_chave"],
            segunda["registros"][0]["ocorrencia_chave"],
        )
        self.assertIsNone(segunda["paginacao"]["next_cursor"])

        with self.assertRaisesRegex(ValueError, "não corresponde aos filtros"):
            caixas_tarefas.consultar_acervo_estruturado(
                formato="compacto",
                campos="cnj,ocorrencia_chave",
                itens_por_pagina=1,
                ordenar_por="numero_processo",
                incluir_facetas=False,
                envelope="minimo",
                filtro_classe="DivLit",
                cursor=cursor,
            )

    def test_cursor_adulterado_falha_fechado(self):
        with self.assertRaisesRegex(ValueError, "cursor inválido"):
            caixas_tarefas.consultar_acervo_estruturado(cursor="abc.def")

    def test_fast_path_compacto_preserva_resultado_e_snapshot(self):
        sid = caixas_tarefas.novo_snapshot(
            "1g",
            "advogado",
            [{"grupo": "tarefas", "nome": "Triar", "quantidade": 2}],
        )
        caixas_tarefas.salvar_caixa(
            sid,
            {"grupo": "tarefas", "nome": "Triar", "quantidade": 2},
            2,
            [
                {
                    "idTaskInstance": 1,
                    "numeroProcesso": "0002",
                    "classeJudicial": "DivLit",
                    "dataChegada": 1704153600000,
                },
                {
                    "idTaskInstance": 2,
                    "numeroProcesso": "0001",
                    "classeJudicial": "CumSen",
                    "dataChegada": 1704067200000,
                },
            ],
        )
        caixas_tarefas.finalizar_snapshot(sid)
        argumentos = {
            "snapshot_id": sid,
            "formato": "compacto",
            "campos": "cnj,tarefa,classe,dias,flags",
            "itens_por_pagina": 2,
            "ordenar_por": "numero_processo",
            "envelope": "minimo",
        }

        rapida = caixas_tarefas.consultar_acervo_estruturado(
            **argumentos,
            incluir_facetas=False,
        )
        completa = caixas_tarefas.consultar_acervo_estruturado(
            **argumentos,
            incluir_facetas=True,
        )

        self.assertEqual(
            rapida["consulta"]["caminho_execucao"],
            "sql_compacto_sem_filtros",
        )
        self.assertEqual(rapida["snapshot"]["id"], completa["snapshot"]["id"])
        self.assertEqual(rapida["registros"], completa["registros"])
        self.assertEqual(rapida["dicionarios"], completa["dicionarios"])
        self.assertEqual(
            rapida["paginacao"]["total_itens"],
            completa["paginacao"]["total_itens"],
        )

    def test_export_ndjson_e_estatisticas_equivalem_ao_snapshot(self):
        sid = caixas_tarefas.novo_snapshot(
            "1g",
            "advogado",
            [{"grupo": "tarefas", "nome": "Triar", "quantidade": 3}],
        )
        caixas_tarefas.salvar_caixa(
            sid,
            {"grupo": "tarefas", "nome": "Triar", "quantidade": 3},
            3,
            [
                {
                    "idTaskInstance": 1,
                    "numeroProcesso": "0001",
                    "classeJudicial": "CumSen",
                    "dataChegada": 1704067200000,
                    "prioridade": True,
                },
                {
                    "idTaskInstance": 2,
                    "numeroProcesso": "0002",
                    "classeJudicial": "CumSen",
                    "dataChegada": 1735689600000,
                    "sigiloso": True,
                },
                {
                    "idTaskInstance": 3,
                    "numeroProcesso": "0003",
                    "classeJudicial": "DivLit",
                    "dataChegada": 1767225600000,
                },
            ],
        )
        caixas_tarefas.finalizar_snapshot(sid)

        manifesto = caixas_tarefas.exportar_acervo(
            snapshot_id=sid,
            formato_arquivo="ndjson",
            modo="compacto",
            autorizacao_ref="autorizacao-acervo",
        )
        arquivo = Path(manifesto["arquivo"])
        self.assertEqual(arquivo.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            hashlib.sha256(arquivo.read_bytes()).hexdigest(),
            manifesto["sha256"],
        )
        self.assertTrue(manifesto["encrypted_at_rest"])
        self.assertNotEqual(arquivo.read_bytes()[:2], b"\x1f\x8b")
        decrypted = caixas_tarefas.ler_exportacao(
            manifesto["export_id"],
            "autorizacao-acervo",
        )
        with gzip.open(io.BytesIO(decrypted), "rt", encoding="utf-8") as fh:
            linhas = [json.loads(linha) for linha in fh]
        self.assertEqual(len(linhas), manifesto["registros"])
        self.assertEqual(manifesto["registros"], 3)
        self.assertTrue(manifesto["contem_sigilosos"])
        self.assertEqual(
            caixas_tarefas.caminho_exportacao(manifesto["export_id"]),
            arquivo,
        )
        with self.assertRaises(PermissionError):
            caixas_tarefas.ler_exportacao(
                manifesto["export_id"],
                "autorizacao-incorreta",
            )
        expirado = datetime.now().timestamp() - 8 * 86400
        os.utime(arquivo, (expirado, expirado))
        with self.assertRaises(FileNotFoundError):
            caixas_tarefas.caminho_exportacao(manifesto["export_id"])
        self.assertFalse(arquivo.exists())

        estatisticas = caixas_tarefas.estatisticas_acervo(
            snapshot_id=sid,
            dimensao="classe",
        )
        self.assertEqual(estatisticas["global"]["quantidade"], 3)
        por_classe = {linha["valor"]: linha for linha in estatisticas["linhas"]}
        self.assertEqual(por_classe["CumSen"]["quantidade"], 2)
        self.assertEqual(por_classe["CumSen"]["prioritarios"], 1)
        self.assertEqual(por_classe["CumSen"]["sigilosos"], 1)

    def test_hash_detecta_alteracao_de_conteudo_com_mesma_chave(self):
        original = [
            {
                "idTaskInstance": 1,
                "numeroProcesso": "0001",
                "prioridade": False,
            }
        ]
        alterado = [
            {
                "idTaskInstance": 1,
                "numeroProcesso": "0001",
                "prioridade": True,
            }
        ]
        self.assertNotEqual(
            caixas_tarefas.hash_entidades(original),
            caixas_tarefas.hash_entidades(alterado),
        )

    def test_reaproveitamento_preserva_hash_contagem_e_proveniencia(self):
        caixa = {"grupo": "tarefas", "nome": "Triar", "quantidade": 1}
        origem = caixas_tarefas.novo_snapshot("1g", "advogado", [caixa])
        caixas_tarefas.salvar_caixa(
            origem,
            caixa,
            1,
            [
                {
                    "idTaskInstance": 1,
                    "numeroProcesso": "0001",
                    "classeJudicial": "CumSen",
                }
            ],
        )
        caixas_tarefas.finalizar_snapshot(origem)

        destino = caixas_tarefas.novo_snapshot("1g", "advogado", [caixa])
        reutilizada = caixas_tarefas.reaproveitar_caixa(destino, origem, caixa)
        resumo = caixas_tarefas.finalizar_snapshot(destino)
        registro = caixas_tarefas.consultar_acervo_estruturado(snapshot_id=destino)[
            "registros"
        ][0]

        self.assertTrue(reutilizada["reaproveitado"])
        self.assertEqual(resumo["status"], "completo")
        self.assertEqual(
            reutilizada["hash_sha256"],
            caixas_tarefas.obter_hash_caixa(origem, "tarefas", "Triar")["hash_sha256"],
        )
        self.assertEqual(registro["proveniencia"]["reaproveitado_de"], origem)


class ColetaIncrementalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"PJE_STORAGE_DIR": self.tmp.name})
        self.env.start()

    async def asyncTearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    async def test_incremental_e_integral_produzem_hashes_iguais(self):
        caixa = {"grupo": "tarefas", "nome": "Triar", "quantidade": 1}
        entidades = [
            {
                "idTaskInstance": 1,
                "numeroProcesso": "0001",
                "classeJudicial": "CumSen",
            }
        ]
        anterior = caixas_tarefas.novo_snapshot("1g", "advogado", [caixa])
        caixas_tarefas.salvar_caixa(anterior, caixa, 1, entidades)
        caixas_tarefas.finalizar_snapshot(anterior)
        atual = caixas_tarefas.novo_snapshot("1g", "advogado", [caixa])

        cliente = object.__new__(pje_client.PJeClient)

        async def buscar(caixa_recebida, _semaforo, _retentativas):
            return {
                "caixa": caixa_recebida,
                "count": 1,
                "entities": entidades,
                "tentativas": 1,
                "erro": None,
            }

        cliente._buscar_caixa_api = buscar
        resultados = await cliente._coletar_e_persistir_caixas(
            [caixa],
            atual,
            concorrencia=1,
            max_retentativas=1,
            modo="incremental",
            snapshot_anterior=anterior,
        )
        caixas_tarefas.finalizar_snapshot(atual)

        self.assertTrue(resultados[0]["reaproveitado"])
        self.assertEqual(
            caixas_tarefas.obter_hash_caixa(anterior, "tarefas", "Triar")[
                "hash_sha256"
            ],
            caixas_tarefas.obter_hash_caixa(atual, "tarefas", "Triar")["hash_sha256"],
        )


if __name__ == "__main__":
    unittest.main()
