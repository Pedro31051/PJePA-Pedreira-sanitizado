"""Regressões dos parsers e análises processuais."""

import sys
import unittest
import warnings
from datetime import datetime as RealDateTime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import pje_client
import server


class _DataFixa(RealDateTime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 7, 27, tzinfo=tz)


class _ClienteRelatorio:
    def __init__(self, relatorio):
        self.relatorio_processo = AsyncMock(return_value=relatorio)


class AnaliseLinhaTempoTests(unittest.IsolatedAsyncioTestCase):
    def test_parser_aceita_formatos_reais_do_pje(self):
        casos = {
            "23 mar 2026": RealDateTime(2026, 3, 23),
            "7 março 2024": RealDateTime(2024, 3, 7),
            "02 Dec 2025": RealDateTime(2025, 12, 2),
            "10 Apr 2024": RealDateTime(2024, 4, 10),
            "16/01/2024 13:44": RealDateTime(2024, 1, 16),
            "2025-12-09": RealDateTime(2025, 12, 9),
        }
        for texto, esperado in casos.items():
            with self.subTest(texto=texto):
                self.assertEqual(
                    server._parse_data_movimentacao(texto), esperado
                )

    async def test_linha_tempo_nao_substitui_datas_textuais_por_hoje(self):
        cliente = _ClienteRelatorio({
            "dados_basicos": {"data_distribuicao": "16/01/2024 13:44"},
            "movimentacoes": [
                {"data": "27 jul 2026", "tipo": "SUSPENSÃO DO PROCESSO"},
                {"data": "16 jan 2024", "tipo": "DISTRIBUIÇÃO"},
                {"data": "20 fev 2024", "tipo": "CITAÇÃO"},
            ],
        })

        with (
            patch.object(
                server.cliente_singleton,
                "get_cliente",
                AsyncMock(return_value=cliente),
            ),
            patch.object(server, "datetime", _DataFixa),
        ):
            resultado = await server.analisar_linha_do_tempo_processo(
                "0800001-61.2024.8.14.0028"
            )

        self.assertEqual(resultado["data_distribuicao"], "16/01/2024")
        self.assertEqual(resultado["origem_data_distribuicao"], "dados_basicos")
        self.assertEqual(resultado["duracao_total_dias"], 923)
        self.assertEqual(resultado["movimentacoes_com_data_valida"], 3)
        self.assertTrue(resultado["calculo_datas_confiavel"])

    async def test_sem_datas_retorna_indisponivel_em_vez_de_duracao_zero(self):
        cliente = _ClienteRelatorio({
            "dados_basicos": {},
            "movimentacoes": [{"data": "data desconhecida", "tipo": "ATO"}],
        })

        with (
            patch.object(
                server.cliente_singleton,
                "get_cliente",
                AsyncMock(return_value=cliente),
            ),
            patch.object(server, "datetime", _DataFixa),
        ):
            resultado = await server.analisar_linha_do_tempo_processo(
                "0800001-61.2024.8.14.0028"
            )

        self.assertIsNone(resultado["data_distribuicao"])
        self.assertIsNone(resultado["duracao_total_dias"])
        self.assertIsNone(resultado["dias_sem_movimentacao_atual"])
        self.assertFalse(resultado["calculo_datas_confiavel"])
        self.assertIn("não foram inventadas", resultado["aviso_datas"])


class AnaliseDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_cnj_com_dv_invalido_nao_abre_browser(self):
        numero = "0000000-00.2024.8.14.0000"
        with patch.object(
            server.cliente_singleton,
            "get_cliente",
            AsyncMock(),
        ) as get_client:
            resultado = await server.analisar_processo_pje(
                acao="movimentacoes",
                numero_cnj=numero,
                persona="advogado",
            )

        get_client.assert_not_awaited()
        self.assertIn("CNJ inválido", resultado["erro"])
        self.assertNotIn(numero, str(resultado))
        self.assertEqual(
            resultado["valor_recebido_mascarado"],
            "0000000-**.****.*.**.0000",
        )

    async def test_comparacao_duplicada_falha_antes_do_browser(self):
        numero = "0800001-61.2024.8.14.0028"
        with patch.object(
            server.cliente_singleton,
            "get_cliente",
            AsyncMock(),
        ) as get_client:
            resultado = await server.analisar_processo_pje(
                acao="comparar",
                numero_cnj=f"{numero};{numero}",
                persona="advogado",
            )

        get_client.assert_not_awaited()
        self.assertIn("duplicados", resultado["erro"])
        self.assertNotIn(numero, str(resultado))

    async def test_diagnostico_reutiliza_relatorio_e_explica_completude(self):
        cliente = AsyncMock()
        cliente.relatorio_processo.return_value = {
            "dados_basicos": {
                "data_distribuicao": "16/01/2024",
                "classe": "Procedimento sintético",
            },
            "partes": [{"nome": "Parte sintética"}],
            "movimentacoes": [
                {"data": "16/01/2024", "tipo": "Distribuição"},
                {"data": "20/02/2024", "tipo": "Citação"},
            ],
            "total_movimentacoes_encontradas": 2,
        }
        cliente.ler_documento_filtrado.return_value = {
            "encontrado": False,
        }
        cliente.expedientes_pendentes.return_value = {
            "expedientes": [],
        }
        with (
            patch.object(
                server.cliente_singleton,
                "get_cliente",
                AsyncMock(return_value=cliente),
            ) as get_client,
            patch.object(
                server.caixas_tarefas,
                "consultar_ocorrencias",
                return_value={"ocorrencias": []},
            ),
            patch.object(
                server.pje_downloader,
                "estatisticas_e_limpeza_storage",
                return_value={},
            ),
        ):
            resultado = await server.diagnosticar_saude_processual(
                "0800001-61.2024.8.14.0028",
            )

        get_client.assert_awaited_once()
        cliente.relatorio_processo.assert_awaited_once()
        self.assertIn("completude", resultado)
        self.assertEqual(
            resultado["completude"]["cobertura_datas_percentual"],
            100.0,
        )
        self.assertTrue(resultado["completude"]["linha_tempo_confiavel"])


class AudienciasContractTests(unittest.TestCase):
    def test_toda_mencao_cai_em_exatamente_um_balde(self):
        movimentos = [
            {
                "titulo": (
                    "Audiência de conciliação designada para 30/07/2026 às 10:00"
                ),
            },
            {
                "titulo": (
                    "Audiência de instrução realizada em 10/07/2026 às 09:00"
                ),
            },
            {
                "titulo": (
                    "Audiência de mediação realizada em 11/07/2026 às 11:00"
                ),
            },
            {
                "titulo": (
                    "Audiência una cancelada para 31/07/2026 às 14:00"
                ),
            },
        ]

        resultado = server._extrair_audiencias(
            movimentos,
            hoje=RealDateTime(2026, 7, 27),
        )

        self.assertEqual(resultado["total_mencoes_audiencia"], 4)
        self.assertEqual(resultado["contadores"]["futuras"], 1)
        self.assertEqual(resultado["contadores"]["passadas"], 2)
        self.assertEqual(resultado["contadores"]["canceladas"], 1)
        self.assertEqual(resultado["contadores"]["sem_data"], 0)
        self.assertTrue(resultado["contadores_fecham"])


class DocumentDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_documentos_recusa_dv_invalido_sem_browser(self):
        numero = "0000000-00.2024.8.14.0000"
        with patch.object(
            server.cliente_singleton,
            "get_cliente",
            AsyncMock(),
        ) as get_client:
            resultado = await server.gerir_documentos_pje(
                acao="listar",
                numero_cnj=numero,
                persona="advogado",
            )

        get_client.assert_not_awaited()
        self.assertIn("CNJ inválido", resultado["erro"])
        self.assertNotIn(numero, str(resultado))

    async def test_documentos_valida_limites_e_termo_antes_do_browser(self):
        numero = "0800001-61.2024.8.14.0028"
        casos = [
            {"acao": "listar", "max_paginas": 0},
            {"acao": "listar", "max_documentos": 101},
            {"acao": "listar", "tempo_maximo_segundos": 9},
            {"acao": "listar", "limite": 501},
            {
                "acao": "pesquisar_texto",
                "termo_busca": "x" * 201,
            },
            {"acao": "ler", "id_doc": "../segredo"},
        ]
        with patch.object(
            server.cliente_singleton,
            "get_cliente",
            AsyncMock(),
        ) as get_client:
            for parametros in casos:
                with self.subTest(parametros=parametros):
                    resultado = await server.gerir_documentos_pje(
                        numero_cnj=numero,
                        persona="advogado",
                        **parametros,
                    )
                    self.assertIn("erro", resultado)

        get_client.assert_not_awaited()

    def test_parcialidade_da_arvore_e_fail_closed_e_transitiva(self):
        resultado = server._propagar_arvore(
            {"total_documentos": 3},
            {"documentos": [{"id": "1"}]},
        )

        self.assertFalse(resultado["arvore_completa"])
        self.assertEqual(resultado["status"], "partial")
        self.assertIn("piso", resultado["aviso_arvore"])


class ParserPartesTests(unittest.TestCase):
    def test_selector_scrapling_nao_repassa_strip_cdata_obsoleto(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            seletor = pje_client.Selector("<html><body>ok</body></html>")

        self.assertEqual(seletor.css("body::text").get(), "ok")

    def test_partes_sem_sigla_de_classe_antes_do_cnj(self):
        texto = (
            "0800001-61.2024.8.14.0028\n"
            "MINISTÉRIO PÚBLICO DO ESTADO DO PARÁ "
            "X FULANO DE TAL\n"
            "Detalhes do processo"
        )

        partes = pje_client.PJeClient._extrair_partes(texto)

        self.assertEqual(
            partes["polo_ativo"], "MINISTÉRIO PÚBLICO DO ESTADO DO PARÁ"
        )
        self.assertEqual(partes["polo_passivo"], "FULANO DE TAL")

    def test_partes_em_polos_rotulados(self):
        texto = (
            "Pólo Ativo (Autor)\n"
            "MINISTÉRIO PÚBLICO DO ESTADO DO PARÁ\n"
            "Polo Passivo (Réu)\n"
            "FULANO DE TAL\n"
        )

        partes = pje_client.PJeClient._extrair_partes(texto)

        self.assertEqual(
            partes["polo_ativo"], "MINISTÉRIO PÚBLICO DO ESTADO DO PARÁ"
        )
        self.assertEqual(partes["polo_passivo"], "FULANO DE TAL")

    def test_partes_por_elementos_semanticos_do_dom(self):
        html = """
        <section>
          <div class="processo-poloAtivo">
            <span>MINISTÉRIO PÚBLICO DO ESTADO DO PARÁ</span>
          </div>
          <div class="processo-poloPassivo">
            <span>FULANO DE TAL</span>
          </div>
        </section>
        """

        partes = pje_client.PJeClient._extrair_partes("", html)

        self.assertEqual(
            partes["polo_ativo"], "MINISTÉRIO PÚBLICO DO ESTADO DO PARÁ"
        )
        self.assertEqual(partes["polo_passivo"], "FULANO DE TAL")

    def test_dom_separa_parte_de_representante(self):
        html = """
        <section>
          <div class="processo-poloAtivo">
            <strong>Polo ativo</strong>
            <span class="nome-parte">POLÍCIA CIVIL - MARABÁ (AUTORIDADE)</span>
            <span class="representante">POLÍCIA CIVIL DO ESTADO DO PARÁ</span>
          </div>
          <div class="processo-poloPassivo">
            <strong>Polo passivo</strong>
            <span class="nome-parte">FULANO DE TAL (AUTOR DO FATO)</span>
            <span class="representante">DEFENSORIA PÚBLICA DO ESTADO DO PARÁ</span>
          </div>
        </section>
        """

        partes = pje_client.PJeClient._extrair_partes("", html)

        self.assertEqual(
            partes["polo_ativo"], "POLÍCIA CIVIL - MARABÁ (AUTORIDADE)"
        )
        self.assertEqual(
            partes["polo_passivo"], "FULANO DE TAL (AUTOR DO FATO)"
        )

    def test_dom_agregado_usa_papel_como_fim_do_nome(self):
        html = """
        <section>
          <div class="processo-poloAtivo">
            Polo ativo POLÍCIA CIVIL - MARABÁ (AUTORIDADE)
            POLÍCIA CIVIL DO ESTADO DO PARÁ
          </div>
          <div class="processo-poloPassivo">
            Polo passivo FULANO DE TAL - CPF: 000.000.000-00 (AUTOR DO FATO)
            DEFENSORIA PÚBLICA DO ESTADO DO PARÁ
          </div>
        </section>
        """

        partes = pje_client.PJeClient._extrair_partes("", html)

        self.assertEqual(
            partes["polo_ativo"], "POLÍCIA CIVIL - MARABÁ (AUTORIDADE)"
        )
        self.assertEqual(
            partes["polo_passivo"],
            "FULANO DE TAL - CPF: 000.000.000-00 (AUTOR DO FATO)",
        )

    def test_polo_passivo_nao_encontrado_continua_explicito(self):
        texto = "REQUERENTE X Não encontrado"

        partes = pje_client.PJeClient._extrair_partes(texto)

        self.assertIsNone(partes["polo_passivo"])
        self.assertIn("nao cadastrado", partes["observacao_partes"])


class QuadroPartesTests(unittest.IsolatedAsyncioTestCase):
    async def test_quadro_normaliza_retorno_textual_legado(self):
        cliente = AsyncMock()
        cliente.buscar_processo.return_value = {
            "partes": "MINISTÉRIO PÚBLICO X FULANO DE TAL",
            "polo_ativo": "MINISTÉRIO PÚBLICO",
            "polo_passivo": "FULANO DE TAL",
        }
        with patch.object(
            server.cliente_singleton,
            "get_cliente",
            AsyncMock(return_value=cliente),
        ):
            resultado = await server.quadro_partes_advogados(
                "0800001-61.2024.8.14.0028"
            )

        self.assertEqual(resultado["total_partes"], 2)
        self.assertEqual(
            resultado["quadro_polos"]["polo_ativo"][0]["nome"],
            "MINISTÉRIO PÚBLICO",
        )
        self.assertEqual(
            resultado["quadro_polos"]["polo_passivo"][0]["nome"],
            "FULANO DE TAL",
        )


class ClassificacaoDecisaoTests(unittest.TestCase):
    def test_suspensao_do_artigo_366_cpp_nao_e_despacho_ordinario(self):
        categoria, frases = server._classificar_texto_decisao(
            "Ante o exposto, DECRETO a suspensão do processo e do lapso "
            "prescricional, nos termos do art. 366 do CPP."
        )

        self.assertEqual(
            categoria,
            "SUSPENSÃO DO PROCESSO E DO PRAZO PRESCRICIONAL (ART. 366 CPP)",
        )
        self.assertTrue(frases)


class ParserPainelTests(unittest.TestCase):
    def test_fallback_extrai_card_sem_tabela_legada(self):
        html = """
        <article class="card-expediente" id="expediente-987">
          <strong>0800001-61.2024.8.14.0028</strong>
          <span>Destinatário: FULANO DE TAL</span>
          <span>ID do documento (145317793)</span>
          <span>Data limite prevista para resposta: 31/07/2026 23:59</span>
          <button>RESPONDER</button>
        </article>
        """

        expedientes = pje_client.PJeClient._extrair_painel_dom(html)

        self.assertEqual(len(expedientes), 1)
        self.assertEqual(expedientes[0]["id_expediente"], "987")
        self.assertEqual(
            expedientes[0]["numero_processo"],
            "0800001-61.2024.8.14.0028",
        )
        self.assertEqual(expedientes[0]["data_limite"], "31/07/2026 23:59")
        self.assertEqual(expedientes[0]["acao_disponivel"], "responder")


class _LinkAgrupador:
    def __init__(self, pagina, chave, vazio=False):
        self.pagina = pagina
        self.chave = chave
        self.vazio = vazio

    async def is_visible(self):
        return True

    async def evaluate(self, _script):
        return {
            "chave": self.chave,
            "idLinha2": self.chave.replace(":linhaN1", ":linhaN2"),
            "vazioDeclarado": self.vazio,
            "assinatura": "0:0",
        }

    async def click(self, **_kwargs):
        self.pagina.clicados.append(self.chave)


class _ListaAgrupadores:
    def __init__(self, pagina):
        self.pagina = pagina

    async def count(self):
        return len(self.pagina.links)

    def nth(self, indice):
        return self.pagina.links[indice]


class _PaginaAgrupadores:
    def __init__(self):
        self.clicados = []
        self.links = [
            _LinkAgrupador(self, "form:lista:0:linhaN1"),
            _LinkAgrupador(self, "form:lista:4:linhaN1", vazio=True),
        ]
        self.esperas = []

    def locator(self, _seletor):
        return _ListaAgrupadores(self)

    async def wait_for_function(self, _script, arg, timeout):
        self.esperas.append((arg["idLinha2"], timeout))


class ExpansaoAgrupadoresTests(unittest.IsolatedAsyncioTestCase):
    async def test_abre_linha_vazia_e_pula_agrupador_sem_expedientes(self):
        cliente = pje_client.PJeClient("1", "2", "JBSWY3DPEHPK3PXP")
        pagina = _PaginaAgrupadores()
        cliente._page = pagina

        total = await cliente._expandir_agrupadores_expedientes()

        self.assertEqual(total, 1)
        self.assertEqual(pagina.clicados, ["form:lista:0:linhaN1"])
        self.assertEqual(
            pagina.esperas,
            [("form:lista:0:linhaN2", 10_000)],
        )


class FamiliaTests(unittest.IsolatedAsyncioTestCase):
    async def test_destaques_leem_campos_reais_das_movimentacoes(self):
        cliente = AsyncMock()
        cliente.ultimas_movimentacoes.return_value = {
            "movimentacoes": [
                {
                    "tipo": "581 - Juntada de Relatório",
                    "descricao": "Estudo de Caso",
                },
                {
                    "tipo": "Redistribuído por cumprimento administrativo",
                },
            ]
        }
        with patch.object(
            server.cliente_singleton,
            "get_cliente",
            AsyncMock(return_value=cliente),
        ):
            resultado = await server.ultimas_movimentacoes(
                "0800001-61.2024.8.14.0028"
            )

        self.assertEqual(
            resultado["destaques_familia"],
            ["LAUDO/ESTUDO PSICOSSOCIAL"],
        )
        self.assertEqual(
            resultado["movimentacoes"][0]["alerta_vara_familia"],
            "LAUDO/ESTUDO PSICOSSOCIAL",
        )
        self.assertNotIn(
            "alerta_vara_familia",
            resultado["movimentacoes"][1],
        )

    async def test_resumo_reconhece_familia_pelo_snapshot_quando_dom_vazio(self):
        cliente = AsyncMock()
        cliente.relatorio_processo.return_value = {
            "dados_basicos": {},
            "partes": [],
            "movimentacoes": [],
            "total_movimentacoes_encontradas": 0,
        }
        cliente.ler_documento_filtrado.return_value = {"encontrado": False}
        cliente.expedientes_pendentes.return_value = {"expedientes": []}
        contexto = {
            "ocorrencias": [{
                "classe_judicial": "GuardaFam",
                "assunto_principal": "Regulamentação de Visitas",
                "orgao_julgador": (
                    "Vara de Família, Sucessões e Registros Públicos de Marabá"
                ),
            }]
        }
        with (
            patch.object(
                server.cliente_singleton,
                "get_cliente",
                AsyncMock(return_value=cliente),
            ),
            patch.object(
                server.caixas_tarefas,
                "consultar_ocorrencias",
                return_value=contexto,
            ),
            patch.object(
                server.pje_downloader,
                "estatisticas_e_limpeza_storage",
                return_value={},
            ),
        ):
            resultado = await server.resumo_executivo_processo(
                "0800001-61.2024.8.14.0028"
            )

        sintese = resultado["sintese_inteligente"]
        self.assertTrue(sintese["materia_familia"])
        self.assertTrue(
            sintese["diagnostico_vara_familia"]["tem_guarda_visitas"]
        )
        self.assertEqual(
            sintese["diagnostico_vara_familia"]["fonte_contexto"],
            "snapshot_painel_interno",
        )


class _RespostaCapturada:
    url = "https://pje.test/documento/download/145317793?ca=segredo"
    status = 200

    async def body(self):
        raise RuntimeError("No resource with given identifier found")

    async def all_headers(self):
        return {"content-type": "text/html"}


class _RespostaRepetida:
    status = 200
    ok = True
    headers = {"content-type": "text/html;charset=UTF-8"}

    def __init__(self):
        self.dispose = AsyncMock()

    async def body(self):
        return b"<html>Denuncia</html>"


class _PaginaComResposta:
    url = "https://pje.test/autos?id=1&ca=segredo"

    def __init__(self, resposta):
        self._resposta = resposta
        self._listener = None
        self._link = _LinkComResposta(self)

    def is_closed(self):
        return False

    def locator(self, _seletor):
        return self._link

    def on(self, _evento, listener):
        self._listener = listener

    def remove_listener(self, _evento, _listener):
        self._listener = None


class _LinkComResposta:
    def __init__(self, pagina):
        self._pagina = pagina
        self.first = self

    def filter(self, **_kwargs):
        return self

    async def count(self):
        return 1

    async def click(self, **_kwargs):
        self._pagina._listener(self._pagina._resposta)


class LeituraDocumentoTests(unittest.IsolatedAsyncioTestCase):
    async def test_jsf_repete_url_quando_chromium_perde_o_corpo(self):
        cliente = pje_client.PJeClient("1", "2", "JBSWY3DPEHPK3PXP")
        repetida = _RespostaRepetida()
        request = SimpleNamespace(get=AsyncMock(return_value=repetida))
        cliente._context = SimpleNamespace(request=request)
        pagina = _PaginaComResposta(_RespostaCapturada())

        resultado = await cliente._obter_documento_via_jsf(
            pagina, "145317793"
        )

        self.assertEqual(resultado["corpo"], b"<html>Denuncia</html>")
        self.assertEqual(resultado["status"], 200)
        self.assertEqual(
            resultado["fonte_jsf"],
            "repetição autenticada após seleção JSF",
        )
        self.assertNotIn("segredo", resultado["url"])
        request.get.assert_awaited_once()
        repetida.dispose.assert_awaited_once()


class LeituraDocumentosLoteTests(unittest.IsolatedAsyncioTestCase):
    async def test_lote_abre_e_fecha_autos_uma_unica_vez(self):
        cliente = pje_client.PJeClient(
            "1", "2", "JBSWY3DPEHPK3PXP"
        )
        aba = object()
        cliente._abrir_autos_processo = AsyncMock(return_value=aba)
        cliente._extrair_documentos_da_aba = AsyncMock(return_value=[
            {"id": "3", "titulo": "3 - Decisão", "tipo": "Decisão"},
            {"id": "2", "titulo": "2 - Petição", "tipo": "Petição"},
        ])
        cliente._ler_documento_rest = AsyncMock(side_effect=[
            {"id_documento": "3", "texto": "primeiro teor"},
            {"id_documento": "2", "texto": "segundo teor"},
        ])
        cliente._fechar_aba_autos = AsyncMock()

        resultado = await cliente.ler_documentos_em_lote(
            "0800001-61.2024.8.14.0028",
            max_documentos=2,
            tempo_maximo_segundos=10,
        )

        cliente._abrir_autos_processo.assert_awaited_once()
        cliente._extrair_documentos_da_aba.assert_awaited_once_with(aba)
        self.assertEqual(cliente._ler_documento_rest.await_count, 2)
        cliente._fechar_aba_autos.assert_awaited_once_with(aba)
        self.assertTrue(resultado["busca_concluida"])
        self.assertEqual(len(resultado["leituras"]), 2)

    async def test_pesquisa_propaga_cobertura_parcial_do_lote(self):
        cliente = AsyncMock()
        cliente.ler_documentos_em_lote.return_value = {
            "arvore_completa": True,
            "documentos_planejados": 3,
            "documentos_tentados": 2,
            "documentos_nao_tentados": 1,
            "leituras": [{
                "documento": {
                    "id": "3",
                    "titulo": "3 - Sentença",
                    "tipo": "Sentença",
                },
                "teor": {"texto": "Decreto a suspensão do processo."},
            }],
            "falhas": [{
                "documento_id": "2",
                "titulo": "2 - Anexo",
                "erro": "leitura excedeu 15.0s e foi interrompida",
            }],
            "busca_concluida": False,
            "motivo_interrupcao": (
                "orçamento total esgotado durante a leitura"
            ),
            "tempo_maximo_segundos": 45.0,
            "tempo_decorrido_segundos": 45.0,
        }
        with patch.object(
            server.cliente_singleton,
            "get_cliente",
            AsyncMock(return_value=cliente),
        ):
            resultado = await server.pesquisar_autos_texto(
                "0800001-61.2024.8.14.0028",
                "suspensão",
            )

        cliente.ler_documentos_em_lote.assert_awaited_once()
        self.assertFalse(resultado["busca_concluida"])
        self.assertEqual(resultado["documentos_analisados"], 2)
        self.assertEqual(resultado["documentos_nao_tentados"], 1)
        self.assertEqual(resultado["cobertura_percentual"], 33.33)
        self.assertEqual(resultado["total_documentos_com_ocorrencia"], 1)
        self.assertIn(
            "suspensão",
            resultado["resultados"][0]["trechos_encontrados"][0].lower(),
        )


if __name__ == "__main__":
    unittest.main()
