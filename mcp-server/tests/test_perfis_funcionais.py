"""Regressões de isolamento por localização + papel do usuário interno."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import caixas_tarefas
import perfil_contexto
import pje_client
import pje_downloader

FAMILIA = (
    "Vara de Família, Sucessões e Registros Públicos de Marabá / "
    "Secretaria Vara Cível / Diretor de Secretaria"
)
CRIMINAL = (
    "1ª Vara Criminal de Marabá / Secretaria Vara Criminal / "
    "Diretor de Secretaria"
)


class PerfisFuncionaisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {"PJE_STORAGE_DIR": self.tmp.name},
        )
        self.env.start()
        caixas_tarefas._BANCOS_INICIALIZADOS.clear()
        perfil_contexto.limpar_catalogo_perfis()
        perfil_contexto.definir_contexto("advogado", "1g", "")

    def tearDown(self):
        perfil_contexto.definir_contexto("advogado", "1g", "")
        perfil_contexto.limpar_catalogo_perfis()
        caixas_tarefas._BANCOS_INICIALIZADOS.clear()
        self.env.stop()
        self.tmp.cleanup()

    def _snapshot_servidor(self, rotulo, orgao_id, orgao_nome):
        contexto = perfil_contexto.definir_contexto("servidor", "1g", rotulo)
        sid = caixas_tarefas.novo_snapshot(
            "1g",
            "servidor",
            [{"grupo": "tarefas", "nome": "Minutar", "quantidade": 1}],
        )
        caixas_tarefas.salvar_caixa(
            sid,
            {"grupo": "tarefas", "nome": "Minutar", "quantidade": 1},
            1,
            [
                {
                    "idTaskInstance": 1,
                    "idProcesso": 10,
                    "numeroProcesso": "0800001-61.2024.8.14.0028",
                    "idOrgaoJulgador": orgao_id,
                    "orgaoJulgador": orgao_nome,
                    "cargoJudicial": "Diretor de Secretaria",
                }
            ],
        )
        return contexto, caixas_tarefas.finalizar_snapshot(sid)

    def test_servidor_nao_e_convertido_em_advogado(self):
        self.assertEqual(
            perfil_contexto.normalizar_persona("servidor"),
            "servidor",
        )
        with self.assertRaises(ValueError):
            perfil_contexto.normalizar_persona("perfil inventado")

    def test_perfis_de_varas_diferentes_tem_ids_distintos(self):
        familia = perfil_contexto.criar_contexto("servidor", "1g", FAMILIA)
        criminal = perfil_contexto.criar_contexto("servidor", "1g", CRIMINAL)
        self.assertNotEqual(familia["perfil_id"], criminal["perfil_id"])
        self.assertEqual(familia["papel"], "Diretor de Secretaria")
        self.assertIn("Família", familia["unidade"])

    def test_hash_do_rotulo_nao_vira_pje_id(self):
        contexto = perfil_contexto.definir_contexto("servidor", "1g", FAMILIA)
        self.assertTrue(contexto["perfil_id"])
        self.assertEqual(contexto["pje_id"], "")
        self.assertEqual(contexto["vinculo_status"], "provisorio")
        self.assertEqual(
            perfil_contexto.erro_identificador_estavel("servidor")["codigo"],
            "PERFIL_SEM_IDENTIFICADOR_ESTAVEL",
        )

    def test_catalogo_separa_pje_id_e_rotulo(self):
        perfil = perfil_contexto.PerfilFuncional(
            pje_id="perfil-real-916",
            rotulo=FAMILIA,
            unidade_id="916",
            papel_id="diretor",
            unidade="Vara de Família",
            localizacao="Secretaria Vara Cível",
            papel="Diretor de Secretaria",
            persona="servidor",
            grau="1g",
            fonte_id="data-perfilId",
        )
        perfil_contexto.registrar_perfis_funcionais("servidor", "1g", [perfil])

        contexto = perfil_contexto.definir_contexto(
            "servidor", "1g", "pje_id:perfil-real-916"
        )

        self.assertEqual(contexto["pje_id"], "perfil-real-916")
        self.assertEqual(contexto["rotulo"], FAMILIA)
        self.assertEqual(contexto["unidade_id"], "916")
        self.assertEqual(contexto["vinculo_status"], "confirmado_pje")

    def test_extrator_prioriza_option_value_e_rejeita_jsf_gerado(self):
        option = pje_client.extrair_identidade_perfil_pje(
            {
                "tag": "option",
                "value": "perfil-real-1",
                "dataset": {"perfilId": "perfil-data-2"},
                "href": "/trocar?perfilId=perfil-request-3",
            }
        )
        jsf = pje_client.extrair_identidade_perfil_pje(
            {"tag": "a", "id": "form:j_idt123", "onclick": ""}
        )

        self.assertEqual(option["pje_id"], "perfil-real-1")
        self.assertEqual(option["fonte_id"], "option.value")
        self.assertEqual(jsf["pje_id"], "")

    def test_extrator_usa_data_antes_de_parametro_de_requisicao(self):
        identidade = pje_client.extrair_identidade_perfil_pje(
            {
                "tag": "a",
                "dataset": {
                    "perfilId": "perfil-data",
                    "unidadeId": "916",
                    "papelId": "diretor",
                },
                "href": "/trocar?perfilId=perfil-request",
            }
        )

        self.assertEqual(identidade["pje_id"], "perfil-data")
        self.assertEqual(identidade["unidade_id"], "916")
        self.assertEqual(identidade["papel_id"], "diretor")

    def test_snapshot_so_fica_completo_apos_vinculo_de_orgao_unico(self):
        contexto, snapshot = self._snapshot_servidor(
            FAMILIA,
            916,
            "Vara de Família, Sucessões e Registros Públicos de Marabá",
        )
        self.assertEqual(snapshot["status"], "completo")
        self.assertEqual(snapshot["escopo_status"], "validado")
        self.assertEqual(snapshot["perfil_id"], contexto["perfil_id"])

    def test_snapshot_com_orgaos_multiplos_falha_fechado(self):
        perfil_contexto.definir_contexto("servidor", "1g", FAMILIA)
        sid = caixas_tarefas.novo_snapshot(
            "1g",
            "servidor",
            [{"grupo": "tarefas", "nome": "X", "quantidade": 2}],
        )
        caixas_tarefas.salvar_caixa(
            sid,
            {"grupo": "tarefas", "nome": "X", "quantidade": 2},
            2,
            [
                {
                    "idTaskInstance": 1,
                    "idOrgaoJulgador": 916,
                    "orgaoJulgador": "Vara de Família",
                },
                {
                    "idTaskInstance": 2,
                    "idOrgaoJulgador": 999,
                    "orgaoJulgador": "1ª Vara Criminal",
                },
            ],
        )
        snapshot = caixas_tarefas.finalizar_snapshot(sid)
        self.assertEqual(snapshot["status"], "incompleto")
        self.assertEqual(snapshot["escopo_status"], "perfil_nao_validado")

    def test_papel_divergente_impede_validacao(self):
        perfil_contexto.definir_contexto(
            "servidor",
            "1g",
            "Vara de Família, Sucessões e Registros Públicos de Marabá / Gabinete / Juiz de Direito"
        )
        sid = caixas_tarefas.novo_snapshot(
            "1g",
            "servidor",
            [{"grupo": "tarefas", "nome": "X", "quantidade": 1}],
        )
        caixas_tarefas.salvar_caixa(
            sid,
            {"grupo": "tarefas", "nome": "X", "quantidade": 1},
            1,
            [
                {
                    "idTaskInstance": 1,
                    "idOrgaoJulgador": 916,
                    "orgaoJulgador": (
                        "Vara de Família, Sucessões e Registros Públicos "
                        "de Marabá"
                    ),
                    "cargoJudicial": "Servidor Geral",
                }
            ],
        )
        snapshot = caixas_tarefas.finalizar_snapshot(sid)
        self.assertEqual(snapshot["status"], "incompleto")
        self.assertEqual(snapshot["escopo_status"], "perfil_nao_validado")

    def test_consulta_e_incremental_nao_cruzam_perfis(self):
        familia_ctx, familia = self._snapshot_servidor(
            FAMILIA,
            916,
            "Vara de Família, Sucessões e Registros Públicos de Marabá",
        )
        perfil_contexto.definir_contexto("servidor", "1g", CRIMINAL)
        divergente = caixas_tarefas.obter_snapshot(
            snapshot_id=familia["snapshot_id"],
            grau="1g",
            persona="servidor",
        )
        self.assertEqual(divergente["codigo"], "PERFIL_DIVERGENTE")
        self.assertIsNone(
            caixas_tarefas.snapshot_anterior_completo(
                "1g", "servidor", "novo"
            )
        )

        perfil_contexto.definir_contexto("servidor", "1g", FAMILIA)
        self.assertEqual(
            caixas_tarefas.snapshot_anterior_completo(
                "1g", "servidor", "novo"
            ),
            familia["snapshot_id"],
        )
        self.assertEqual(
            caixas_tarefas.obter_snapshot(
                grau="1g", persona="servidor"
            )["perfil_id"],
            familia_ctx["perfil_id"],
        )

    def test_snapshot_legado_permanece_preservado_e_ambiguo(self):
        perfil_contexto.definir_contexto("advogado", "1g", "")
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
        snapshot = caixas_tarefas.finalizar_snapshot(sid)
        self.assertEqual(snapshot["escopo_status"], "legado_ambiguo")
        self.assertTrue(caixas_tarefas.caminho_banco().exists())

    def test_downloads_ficam_em_raizes_fisicas_distintas(self):
        bases = {
            "1g": Path(self.tmp.name) / "Processos TJPA 1 Grau",
            "2g": Path(self.tmp.name) / "Processos TJPA 2 Grau",
        }
        with patch.object(pje_downloader, "_PASTAS", bases):
            familia = perfil_contexto.criar_contexto_fixado(
                usuario_id="usuario-sintetico",
                persona="servidor",
                grau="1g",
                unidade="Vara de Família",
                localizacao="Secretaria Vara Cível",
                localizacao_status="PRESENTE",
                papel="Diretor de Secretaria",
                titulo="PJe - Painel",
                rota="/pje/home.seam",
                localizador_nao_confiavel=FAMILIA,
            )
            perfil_contexto.definir_contexto_fixado(familia)
            pasta_familia = pje_downloader.pasta_processo(
                "0800001-61.2024.8.14.0028", criar=False
            )
            criminal = perfil_contexto.criar_contexto_fixado(
                usuario_id="usuario-sintetico",
                persona="servidor",
                grau="1g",
                unidade="1ª Vara Criminal",
                localizacao="Secretaria Vara Criminal",
                localizacao_status="PRESENTE",
                papel="Diretor de Secretaria",
                titulo="PJe - Painel",
                rota="/pje/home.seam",
                localizador_nao_confiavel=CRIMINAL,
            )
            perfil_contexto.definir_contexto_fixado(criminal)
            pasta_criminal = pje_downloader.pasta_processo(
                "0800001-61.2024.8.14.0028", criar=False
            )
        self.assertNotEqual(pasta_familia, pasta_criminal)
        self.assertIn(familia.contexto_validado_id, str(pasta_familia))
        self.assertIn(criminal.contexto_validado_id, str(pasta_criminal))

    def test_cache_interno_falha_antes_de_criar_diretorio_sem_fixacao(self):
        bases = {"1g": Path(self.tmp.name) / "nao-criar"}
        perfil_contexto.definir_contexto("servidor", "1g", FAMILIA)
        with (
            patch.object(pje_downloader, "_PASTAS", bases),
            self.assertRaisesRegex(
                perfil_contexto.PerfilObrigatorioError,
                "CONTEXTO_NAO_FIXADO",
            ),
        ):
            pje_downloader.pasta_processo("processo-sintetico")
        self.assertFalse(bases["1g"].exists())
