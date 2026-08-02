"""Comportamento da ferramenta pje_ler_autos_digitais.

Antes deste arquivo a tool só tinha teste de CONTRATO (annotations e schema em
test_tool_contracts.py) — nada exercitava o dispatcher de ações, a extração da
timeline nem o fechamento da aba. Estes testes fecham essa lacuna e servem de
probe para as iterações do backlog da ferramenta.

A timeline é montada como HTML real do PJe e passa pelo parser DOM de verdade
(`PJeClient._extrair_movimentacoes_dom`). O parser NÃO é mockado de propósito:
mockado, o teste passaria mesmo se a tool voltasse a raspar texto por conta
própria — que era justamente o defeito corrigido.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import server
from pje_client import PJeClient

CNJ_VALIDO = "0800001-61.2024.8.14.0028"
CNJ_DV_INVALIDO = "0000000-00.2024.8.14.0000"

HTML_SEM_TIMELINE = "<html><body><p>página sem timeline</p></body></html>"


def _html_timeline(*grupos):
    """Monta o HTML da timeline dos autos.

    grupos: sequência de (data, [(tipo, hora, id_documento_ou_None), ...]),
    espelhando a estrutura real — div.media.data com o cabeçalho de data,
    seguido de um div.media por movimento daquele dia.
    """
    partes = ['<html><body><form id="divTimeLine">']
    for data, movimentos in grupos:
        partes.append(
            f'<div class="media data"><span class="data-interna">{data}</span></div>'
        )
        for tipo, hora, doc_id in movimentos:
            anexo = (
                '<div class="anexos"><a href="#">'
                f"<span>{doc_id} - {tipo}</span></a></div>"
                if doc_id
                else ""
            )
            partes.append(
                '<div class="media"><div class="media-body">'
                f'<span class="texto-movimento">{tipo}</span>{anexo}'
                f'<small class="text-muted">{hora}</small>'
                "</div></div>"
            )
    partes.append("</form></body></html>")
    return "".join(partes)


def _html_com_n_movimentos(n, data="23 mar 2026"):
    return _html_timeline(
        (data, [(f"Movimento numero {i}", "10:00", None) for i in range(n)])
    )


def _html_links_documentos(n, base=920000):
    """Links da árvore como o PJe serve no HTML, sem depender do lazy-load."""
    return "".join(
        f"<a href=\"#\" onclick=\"abrirLinkDocumento('{base + i}')\">peça</a>"
        for i in range(n)
    )


class _AbaFalsa:
    """Aba de autos: content(), inner_text() e os evaluate() da tool."""

    def __init__(
        self,
        html=HTML_SEM_TIMELINE,
        texto_body="",
        texto_pagina="",
        cabecalho="",
        falhar=False,
    ):
        self.html = html
        self.texto_body = texto_body
        self.texto_pagina = texto_pagina
        self.cabecalho = cabecalho
        self.falhar = falhar
        self.url = "https://pje.tjpa.jus.br/pje/Processo/ConsultaProcesso/listView.seam"

    async def content(self):
        if self.falhar:
            raise RuntimeError("página dos autos caiu no meio da leitura")
        return self.html

    async def inner_text(self, seletor="body"):
        if self.falhar:
            raise RuntimeError("página dos autos caiu no meio da leitura")
        return self.texto_body

    async def evaluate(self, js):
        if self.falhar:
            raise RuntimeError("página dos autos caiu no meio da leitura")
        if "document.body.innerText" in js:
            return self.texto_pagina
        if "h1, h2" in js:
            return self.cabecalho
        raise AssertionError(f"evaluate inesperado: {js[:60]}")


def _cliente_falso(aba, docs=None, teor=None, arvore_completa=True):
    cliente = AsyncMock()
    cliente._abrir_autos_processo = AsyncMock(return_value=aba)
    cliente._fechar_aba_autos = AsyncMock()
    cliente._extrair_documentos_da_aba = AsyncMock(
        return_value=docs if docs is not None else []
    )
    cliente.ler_documento = AsyncMock(
        return_value=teor if teor is not None else {"texto": ""}
    )
    # Parsers REAIS do cliente — é o uso deles que precisa ficar provado.
    cliente._extrair_movimentacoes_dom = PJeClient._extrair_movimentacoes_dom
    cliente._extrair_movimentacoes = PJeClient._extrair_movimentacoes
    cliente._extrair_documentos_do_html = PJeClient._extrair_documentos_do_html
    # Flag que o próprio _extrair_documentos_da_aba grava no cliente real.
    cliente._ultima_arvore_completa = arvore_completa
    return cliente


def _patch_cliente(cliente):
    return patch.object(
        server.cliente_singleton,
        "get_cliente",
        AsyncMock(return_value=cliente),
    )


class ValidacaoDeEntradaTests(unittest.IsolatedAsyncioTestCase):
    """O que precisa falhar ANTES de abrir o navegador."""

    async def test_cnj_com_dv_invalido_nao_abre_navegador(self):
        with patch.object(
            server.cliente_singleton, "get_cliente", AsyncMock()
        ) as get_cliente:
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_DV_INVALIDO,
                acao="listar",
            )

        get_cliente.assert_not_awaited()
        self.assertIn("CNJ inválido", resultado["erro"])

    async def test_acao_desconhecida_devolve_acoes_validas(self):
        with patch.object(
            server.cliente_singleton, "get_cliente", AsyncMock()
        ) as get_cliente:
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="xpto",
            )

        get_cliente.assert_not_awaited()
        self.assertIn("ação inválida", resultado["erro"])
        self.assertEqual(
            resultado["acoes"], ["ler_documento", "ler_lote", "listar", "resumo"]
        )

    async def test_ler_documento_sem_id_nao_abre_navegador(self):
        with patch.object(
            server.cliente_singleton, "get_cliente", AsyncMock()
        ) as get_cliente:
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="ler_documento",
            )

        get_cliente.assert_not_awaited()
        self.assertIn("id_documento é obrigatório", resultado["erro"])
        self.assertIn("listar", resultado["dica"])


class DispatcherDeAcoesTests(unittest.IsolatedAsyncioTestCase):
    async def test_aliases_caem_na_acao_canonica(self):
        # 'timeline', 'movimentos' e 'documentos' são apelidos de 'listar';
        # sem acento e sem case-sensitivity.
        for alias in ("timeline", "movimentos", "documentos", "LISTAR"):
            with self.subTest(alias=alias):
                aba = _AbaFalsa(html=_html_com_n_movimentos(1))
                with _patch_cliente(_cliente_falso(aba)):
                    resultado = await server.pje_ler_autos_digitais(
                        numero_cnj=CNJ_VALIDO,
                        acao=alias,
                    )

                self.assertIn("movimentos", resultado)
                self.assertNotIn("erro", resultado)

    async def test_alias_de_leitura_exige_id_documento(self):
        for alias in ("ler", "teor", "conteudo"):
            with self.subTest(alias=alias):
                resultado = await server.pje_ler_autos_digitais(
                    numero_cnj=CNJ_VALIDO,
                    acao=alias,
                )

                self.assertIn("id_documento é obrigatório", resultado["erro"])


class TimelineEstruturadaTests(unittest.IsolatedAsyncioTestCase):
    """A timeline vem do parser DOM, não de raspagem de texto."""

    async def test_movimento_traz_tipo_data_hora_e_id_da_peca(self):
        aba = _AbaFalsa(
            html=_html_timeline(
                ("23 mar 2026", [("Juntada de Petição", "20:22", "92241811")])
            )
        )

        with _patch_cliente(_cliente_falso(aba)):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="listar",
            )

        self.assertEqual(resultado["origem_movimentos"], "dom")
        self.assertEqual(
            resultado["movimentos"],
            [
                {
                    "tipo": "Juntada de Petição",
                    "data": "23 mar 2026",
                    "hora": "20:22",
                    "id": "92241811",
                    "descricao": "Juntada de Petição",
                }
            ],
        )

    async def test_movimentos_iguais_em_datas_diferentes_sobrevivem(self):
        # REGRESSÃO: o JS antigo deduplicava por prefixo de 60 caracteres, o
        # que descartava em silêncio movimentos legítimos de mesmo tipo — e
        # ainda fazia o total mentir. Tipos repetidos são a regra na timeline
        # ("Juntada de Petição", "Decorrido prazo de...").
        aba = _AbaFalsa(
            html=_html_timeline(
                (
                    "23 mar 2026",
                    [
                        ("Juntada de Petição", "20:22", None),
                        ("Juntada de Petição", "09:10", None),
                    ],
                ),
                ("20 mar 2026", [("Juntada de Petição", "14:05", None)]),
            )
        )

        with _patch_cliente(_cliente_falso(aba)):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="listar",
            )

        self.assertEqual(resultado["total_movimentos_extraidos"], 3)
        self.assertEqual(
            [(m["data"], m["hora"]) for m in resultado["movimentos"]],
            [
                ("23 mar 2026", "20:22"),
                ("23 mar 2026", "09:10"),
                ("20 mar 2026", "14:05"),
            ],
        )

    async def test_cabecalho_de_data_nao_entra_como_movimento(self):
        aba = _AbaFalsa(
            html=_html_timeline(("23 mar 2026", [("Despacho", "10:00", None)]))
        )

        with _patch_cliente(_cliente_falso(aba)):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="listar",
            )

        self.assertEqual(len(resultado["movimentos"]), 1)
        self.assertEqual(resultado["movimentos"][0]["tipo"], "Despacho")

    async def test_fallback_regex_avisa_que_a_data_pode_estar_deslocada(self):
        aba = _AbaFalsa(
            html=HTML_SEM_TIMELINE,
            texto_body="JUNTADA DE PETICAO\n20:22\n23 mar 2026\n",
        )

        with _patch_cliente(_cliente_falso(aba)):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="listar",
            )

        self.assertEqual(resultado["origem_movimentos"], "regex")
        self.assertEqual(len(resultado["movimentos"]), 1)
        self.assertIn("DATAS podem estar deslocadas", resultado["aviso"])
        self.assertNotIn("texto_pagina_bruto", resultado)


class ListagemDeAutosTests(unittest.IsolatedAsyncioTestCase):
    async def test_listar_devolve_movimentos_documentos_e_cabecalho(self):
        docs = [{"id": "9001", "tipo": "Petição inicial"}]
        aba = _AbaFalsa(
            html=_html_timeline(
                (
                    "23 mar 2026",
                    [
                        ("Juntada de Petição", "20:22", None),
                        ("Despacho", "09:10", None),
                    ],
                )
            ),
            cabecalho="Processo 0800001-61.2024.8.14.0028",
        )
        cliente = _cliente_falso(aba, docs=docs)

        with _patch_cliente(cliente):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="listar",
            )

        self.assertEqual(resultado["total_movimentos_extraidos"], 2)
        self.assertEqual(resultado["total_documentos"], 1)
        self.assertEqual(resultado["documentos"], docs)
        self.assertEqual(resultado["cabecalho"], "Processo 0800001-61.2024.8.14.0028")
        self.assertIn("listView.seam", resultado["url_autos"])
        # Timeline extraída: não se despeja o texto bruto da página.
        self.assertNotIn("texto_pagina_bruto", resultado)
        cliente._fechar_aba_autos.assert_awaited_once_with(aba)

    async def test_limite_movimentos_corta_a_lista(self):
        aba = _AbaFalsa(html=_html_com_n_movimentos(30))

        with _patch_cliente(_cliente_falso(aba)):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="listar",
                limite_movimentos=5,
            )

        self.assertEqual(len(resultado["movimentos"]), 5)
        self.assertEqual(resultado["total_movimentos_extraidos"], 5)

    async def test_teto_de_duzentos_movimentos(self):
        aba = _AbaFalsa(html=_html_com_n_movimentos(250))

        with _patch_cliente(_cliente_falso(aba)):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="listar",
                limite_movimentos=999,
            )

        self.assertEqual(len(resultado["movimentos"]), 200)

    async def test_timeline_vazia_expoe_texto_bruto_com_aviso(self):
        aba = _AbaFalsa(
            html=HTML_SEM_TIMELINE,
            texto_body="nada que o regex reconheça",
            texto_pagina="TEOR BRUTO DA PÁGINA DOS AUTOS",
        )

        with _patch_cliente(_cliente_falso(aba)):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="listar",
            )

        self.assertEqual(resultado["movimentos"], [])
        self.assertEqual(resultado["origem_movimentos"], "nenhuma")
        self.assertEqual(
            resultado["texto_pagina_bruto"], "TEOR BRUTO DA PÁGINA DOS AUTOS"
        )
        self.assertIn("não foi extraída", resultado["aviso"])

    async def test_resumo_limita_movimentos_e_documentos(self):
        aba = _AbaFalsa(
            html=_html_com_n_movimentos(40) + _html_links_documentos(12),
        )

        with _patch_cliente(_cliente_falso(aba)):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="resumo",
            )

        self.assertEqual(len(resultado["ultimos_movimentos"]), 10)
        self.assertEqual(len(resultado["primeiros_documentos"]), 5)
        self.assertEqual(resultado["total_documentos"], 12)
        self.assertEqual(resultado["origem_movimentos"], "dom")
        # 'resumo' é resposta enxuta: não devolve a timeline completa.
        self.assertNotIn("movimentos", resultado)


class ResumoRapidoTests(unittest.IsolatedAsyncioTestCase):
    """'resumo' é a resposta rápida — não pode pagar o lazy-load da árvore."""

    async def test_resumo_nao_espera_o_lazy_load_da_arvore(self):
        # REGRESSÃO: a ação documentada como "resposta rápida" chamava
        # _extrair_documentos_da_aba, cujo wait_for_function espera até 30s,
        # só para devolver 5 documentos e um total.
        cliente = _cliente_falso(
            _AbaFalsa(html=_html_com_n_movimentos(3) + _html_links_documentos(4))
        )

        with _patch_cliente(cliente):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="resumo",
            )

        cliente._extrair_documentos_da_aba.assert_not_awaited()
        self.assertEqual(resultado["total_documentos"], 4)

    async def test_listar_continua_pagando_o_lazy_load(self):
        # O caminho que promete total exato não pode ter ficado rápido às
        # custas da completude.
        cliente = _cliente_falso(
            _AbaFalsa(html=_html_com_n_movimentos(1)),
            docs=[{"id": "9001", "tipo": "Peça"}],
        )

        with _patch_cliente(cliente):
            await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="listar",
            )

        cliente._extrair_documentos_da_aba.assert_awaited_once()

    async def test_resumo_extrai_ids_do_html_ja_em_maos(self):
        # Sem ida extra ao navegador: o HTML é o mesmo que a timeline usou.
        aba = _AbaFalsa(
            html=_html_com_n_movimentos(1) + _html_links_documentos(3, base=930000)
        )

        with _patch_cliente(_cliente_falso(aba)):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="resumo",
            )

        self.assertEqual(
            [d["id"] for d in resultado["primeiros_documentos"]],
            ["930000", "930001", "930002"],
        )

    async def test_resumo_sem_links_no_html_devolve_zero_sem_estourar(self):
        with _patch_cliente(_cliente_falso(_AbaFalsa(html=_html_com_n_movimentos(2)))):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="resumo",
            )

        self.assertEqual(resultado["total_documentos"], 0)
        self.assertEqual(resultado["primeiros_documentos"], [])
        self.assertEqual(len(resultado["ultimos_movimentos"]), 2)


class LimiteDeMovimentosTests(unittest.IsolatedAsyncioTestCase):
    """limite_movimentos não tinha piso: 0 virava diagnóstico errado."""

    async def test_zero_traz_todos_os_movimentos_ate_o_teto(self):
        # REGRESSÃO: `min(0, 200)` = 0 zerava a lista e a resposta caía no ramo
        # "a timeline não foi extraída via seletores", despejando 8000 chars de
        # texto bruto e culpando o parser por um limite que o chamador pediu.
        aba = _AbaFalsa(html=_html_com_n_movimentos(7), texto_pagina="NAO DEVE APARECER")

        with _patch_cliente(_cliente_falso(aba)):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="listar",
                limite_movimentos=0,
            )

        self.assertEqual(len(resultado["movimentos"]), 7)
        self.assertEqual(resultado["origem_movimentos"], "dom")
        self.assertNotIn("texto_pagina_bruto", resultado)
        self.assertNotIn("aviso", resultado)

    async def test_zero_respeita_o_teto_de_duzentos(self):
        aba = _AbaFalsa(html=_html_com_n_movimentos(250))

        with _patch_cliente(_cliente_falso(aba)):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="listar",
                limite_movimentos=0,
            )

        self.assertEqual(len(resultado["movimentos"]), 200)

    async def test_negativo_e_tratado_como_todos(self):
        aba = _AbaFalsa(html=_html_com_n_movimentos(4))

        with _patch_cliente(_cliente_falso(aba)):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="listar",
                limite_movimentos=-10,
            )

        self.assertEqual(len(resultado["movimentos"]), 4)

    async def test_valor_nao_numerico_falha_antes_do_navegador(self):
        with patch.object(
            server.cliente_singleton, "get_cliente", AsyncMock()
        ) as get_cliente:
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="listar",
                limite_movimentos="cinquenta",
            )

        get_cliente.assert_not_awaited()
        self.assertIn("limite_movimentos deve ser um número inteiro", resultado["erro"])


class CamposLegadosDeConfirmacaoTests(unittest.IsolatedAsyncioTestCase):
    """Campos legados são aceitos e ignorados como nas demais ferramentas."""

    async def test_campos_legados_nao_alteram_o_resultado(self):
        html = _html_com_n_movimentos(2)

        with _patch_cliente(_cliente_falso(_AbaFalsa(html=html))):
            sem_campos = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="listar",
            )
        with _patch_cliente(_cliente_falso(_AbaFalsa(html=html))):
            com_campos = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="listar",
                confirmar_consulta="valor-legado",
                confirmation_token="token-legado",
            )

        self.assertEqual(sem_campos, com_campos)
        self.assertNotIn("erro", com_campos)

    async def test_consulta_passa_pelo_gate_de_confirmacao(self):
        # Hoje o gate é no-op; o que este teste fixa é que a tool o ATRAVESSA,
        # para que rearmá-lo cubra esta ferramenta junto com as outras.
        with patch.object(
            server, "_exigir_confirmacao_consulta", return_value=None
        ) as gate:
            with _patch_cliente(_cliente_falso(_AbaFalsa(html=_html_com_n_movimentos(1)))):
                await server.pje_ler_autos_digitais(
                    numero_cnj=CNJ_VALIDO,
                    acao="listar",
                )

        gate.assert_called_once()
        self.assertEqual(gate.call_args.args[0], "pje_ler_autos_digitais")
        self.assertEqual(gate.call_args.args[1], "listar")

    async def test_gate_pode_interromper_a_consulta(self):
        # Se o gate voltar a exigir handshake, a tool respeita e não abre o PJe.
        recusa = {"erro": "confirmação obrigatória", "token": "abc"}
        with patch.object(
            server, "_exigir_confirmacao_consulta", return_value=recusa
        ):
            with patch.object(
                server.cliente_singleton, "get_cliente", AsyncMock()
            ) as get_cliente:
                resultado = await server.pje_ler_autos_digitais(
                    numero_cnj=CNJ_VALIDO,
                    acao="listar",
                )

        self.assertEqual(resultado, recusa)
        get_cliente.assert_not_awaited()


class ArvoreIncompletaTests(unittest.IsolatedAsyncioTestCase):
    """A árvore lateral é lazy; um total em cima de árvore truncada mente."""

    async def test_listar_marca_arvore_completa_quando_carregou_tudo(self):
        cliente = _cliente_falso(
            _AbaFalsa(html=_html_com_n_movimentos(1)),
            docs=[{"id": "9001", "tipo": "Petição inicial"}],
            arvore_completa=True,
        )

        with _patch_cliente(cliente):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="listar",
            )

        self.assertIs(resultado["arvore_completa"], True)
        self.assertNotIn("aviso_arvore", resultado)
        self.assertNotIn("status", resultado)

    async def test_listar_avisa_quando_a_arvore_ficou_pela_metade(self):
        cliente = _cliente_falso(
            _AbaFalsa(html=_html_com_n_movimentos(1)),
            docs=[{"id": str(9000 + i), "tipo": "Peça"} for i in range(3)],
            arvore_completa=False,
        )

        with _patch_cliente(cliente):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="listar",
            )

        self.assertIs(resultado["arvore_completa"], False)
        self.assertEqual(resultado["status"], "partial")
        self.assertIn("piso", resultado["aviso_arvore"])
        # O total continua sendo devolvido, mas agora rotulado como piso.
        self.assertEqual(resultado["total_documentos"], 3)

    async def test_resumo_sempre_rotula_o_total_como_piso(self):
        # 'resumo' não roda o lazy-load, então o total NUNCA pode se apresentar
        # como definitivo — mesmo que o cliente tenha a flag em True de uma
        # chamada anterior.
        cliente = _cliente_falso(
            _AbaFalsa(html=_html_com_n_movimentos(1) + _html_links_documentos(1)),
            arvore_completa=True,
        )

        with _patch_cliente(cliente):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="resumo",
            )

        self.assertIs(resultado["arvore_completa"], False)
        self.assertEqual(resultado["status"], "partial")
        self.assertIn("piso", resultado["aviso_arvore"])

    async def test_aviso_da_arvore_nao_sobrescreve_aviso_da_timeline(self):
        # Timeline vazia usa 'aviso'; a árvore usa 'aviso_arvore'. Os dois
        # sinais precisam coexistir — são falhas independentes.
        cliente = _cliente_falso(
            _AbaFalsa(html=HTML_SEM_TIMELINE, texto_pagina="TEXTO BRUTO"),
            docs=[],
            arvore_completa=False,
        )

        with _patch_cliente(cliente):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="listar",
            )

        self.assertIn("não foi extraída", resultado["aviso"])
        self.assertIn("piso", resultado["aviso_arvore"])
        self.assertIs(resultado["arvore_completa"], False)

    async def test_cliente_sem_a_flag_e_tratado_como_completo(self):
        # Compatibilidade: cliente antigo/sem o atributo não vira 'partial'.
        cliente = _cliente_falso(
            _AbaFalsa(html=_html_com_n_movimentos(1)),
            docs=[{"id": "9001", "tipo": "Peça"}],
        )
        del cliente._ultima_arvore_completa

        with _patch_cliente(cliente):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="listar",
            )

        self.assertIs(resultado["arvore_completa"], True)


class LeituraDeDocumentoTests(unittest.IsolatedAsyncioTestCase):
    async def test_ler_documento_repassa_id_limpo_ao_cliente(self):
        cliente = _cliente_falso(_AbaFalsa(), teor={"texto": "DESPACHO. Intime-se."})

        with _patch_cliente(cliente):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="ler_documento",
                id_documento="  92241811  ",
            )

        cliente.ler_documento.assert_awaited_once()
        args = cliente.ler_documento.await_args.args
        self.assertEqual(args[1], "92241811")
        # A resposta ecoa o id normalizado, não o que veio com espaços.
        self.assertEqual(resultado["id_documento"], "92241811")
        self.assertEqual(resultado["teor"], {"texto": "DESPACHO. Intime-se."})

    async def test_teor_textual_vem_na_chave_canonica(self):
        cliente = _cliente_falso(_AbaFalsa(), teor={"texto": "DESPACHO. Intime-se."})

        with _patch_cliente(cliente):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="ler_documento",
                id_documento="92241811",
            )

        self.assertEqual(resultado["texto"], "DESPACHO. Intime-se.")
        self.assertIs(resultado["leitura_completa"], True)
        self.assertNotIn("erro", resultado)
        self.assertNotIn("status", resultado)

    async def test_falha_do_rest_sobe_para_o_topo_da_resposta(self):
        # REGRESSÃO: o erro ficava só em teor['erro'] e a resposta não tinha
        # 'erro' no topo — um agente lia documento não-lido como lido.
        cliente = _cliente_falso(
            _AbaFalsa(),
            teor={
                "id_documento": "92241811",
                "erro": "ID interno do processo não foi extraído da URL dos autos",
            },
        )

        with _patch_cliente(cliente):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="ler_documento",
                id_documento="92241811",
            )

        self.assertIn("ID interno do processo", resultado["erro"])
        self.assertEqual(resultado["status"], "erro_leitura")
        # Não se anuncia leitura completa em cima de falha.
        self.assertNotIn("leitura_completa", resultado)
        self.assertNotIn("texto", resultado)

    async def test_documento_truncado_nao_passa_por_leitura_completa(self):
        cliente = _cliente_falso(
            _AbaFalsa(),
            teor={
                "texto": "primeiras 30 paginas",
                "num_paginas": 120,
                "truncado": True,
                "paginas_extraidas": 30,
                "aviso": (
                    "ATENCAO: documento tem 120 paginas, mas apenas as 30 "
                    "primeiras foram extraidas."
                ),
            },
        )

        with _patch_cliente(cliente):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="ler_documento",
                id_documento="92241811",
            )

        self.assertIs(resultado["leitura_completa"], False)
        self.assertEqual(resultado["status"], "partial")
        self.assertIn("120 paginas", resultado["avisos"][0])
        self.assertEqual(resultado["texto"], "primeiras 30 paginas")

    async def test_paginas_sem_texto_viram_aviso_de_ocr(self):
        cliente = _cliente_falso(
            _AbaFalsa(),
            teor={
                "texto": "pagina 1 legivel",
                "paginas_sem_texto": [2, 3, 4],
                "ocr_status": "required_unavailable",
                "ocr_recomendado": True,
            },
        )

        with _patch_cliente(cliente):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="ler_documento",
                id_documento="92241811",
            )

        self.assertIs(resultado["leitura_completa"], False)
        self.assertEqual(resultado["status"], "partial")
        self.assertIn("3 página(s) sem texto", resultado["avisos"][0])
        self.assertIn("required_unavailable", resultado["avisos"][0])


class LeituraEmLoteTests(unittest.IsolatedAsyncioTestCase):
    """Uma abertura dos autos para N peças, em vez de N aberturas."""

    def _cliente_com_lote(self, lote):
        cliente = _cliente_falso(_AbaFalsa())
        cliente.ler_documentos_em_lote = AsyncMock(return_value=lote)
        return cliente

    async def test_lote_delega_por_keyword_com_limites_aplicados(self):
        cliente = self._cliente_com_lote({"numero_cnj": CNJ_VALIDO, "leituras": []})

        with _patch_cliente(cliente):
            await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="ler_lote",
                max_documentos=999,
                tempo_maximo_segundos=30,
            )

        # Delegação por KEYWORD (posicional já quebrou 5 tools neste projeto)
        # e o teto de 50 peças aplicado.
        cliente.ler_documentos_em_lote.assert_awaited_once_with(
            numero_cnj=CNJ_VALIDO,
            max_documentos=50,
            max_paginas=30,
            tempo_maximo_segundos=30,
        )

    async def test_lote_nao_abre_os_autos_uma_vez_por_peca(self):
        cliente = self._cliente_com_lote({"numero_cnj": CNJ_VALIDO, "leituras": []})

        with _patch_cliente(cliente):
            await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="ler_lote",
            )

        # O próprio ler_documentos_em_lote cuida da aba; a tool não abre nada.
        cliente._abrir_autos_processo.assert_not_awaited()
        cliente.ler_documento.assert_not_awaited()

    async def test_cada_leitura_do_lote_ganha_os_sinais_de_integridade(self):
        # O lote não pode reintroduzir o defeito do item 4: erro e truncagem
        # enterrados dentro do teor de cada peça.
        cliente = self._cliente_com_lote(
            {
                "numero_cnj": CNJ_VALIDO,
                "busca_concluida": True,
                "leituras": [
                    {
                        "documento": {"id": "9001", "tipo": "Petição"},
                        "teor": {"texto": "peça legível"},
                    },
                    {
                        "documento": {"id": "9002", "tipo": "Despacho"},
                        "teor": {"erro": "PJe retornou corpo vazio para o documento"},
                    },
                    {
                        "documento": {"id": "9003", "tipo": "Laudo"},
                        "teor": {
                            "texto": "parcial",
                            "paginas_sem_texto": [2, 3],
                            "ocr_status": "required_unavailable",
                        },
                    },
                ],
            }
        )

        with _patch_cliente(cliente):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="ler_lote",
            )

        leituras = resultado["leituras"]
        self.assertEqual(leituras[0]["texto"], "peça legível")
        self.assertIs(leituras[0]["leitura_completa"], True)

        self.assertIn("corpo vazio", leituras[1]["erro"])
        self.assertEqual(leituras[1]["status"], "erro_leitura")
        self.assertNotIn("leitura_completa", leituras[1])

        self.assertIs(leituras[2]["leitura_completa"], False)
        self.assertIn("2 página(s) sem texto", leituras[2]["avisos"][0])

        # Contadores agregados para quem não quer varrer a lista.
        self.assertEqual(resultado["leituras_com_erro"], 1)
        self.assertEqual(resultado["leituras_incompletas"], 1)

    async def test_lote_preserva_sinais_de_interrupcao_do_cliente(self):
        cliente = self._cliente_com_lote(
            {
                "numero_cnj": CNJ_VALIDO,
                "leituras": [],
                "busca_concluida": False,
                "motivo_interrupcao": "orçamento total esgotado durante a leitura",
                "arvore_completa": False,
            }
        )

        with _patch_cliente(cliente):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="ler_lote",
            )

        self.assertIs(resultado["busca_concluida"], False)
        self.assertIn("orçamento", resultado["motivo_interrupcao"])
        self.assertIs(resultado["arvore_completa"], False)

    async def test_aliases_do_lote(self):
        for alias in ("lote", "ler_varios", "ler_documentos", "ler_pecas"):
            with self.subTest(alias=alias):
                cliente = self._cliente_com_lote(
                    {"numero_cnj": CNJ_VALIDO, "leituras": []}
                )
                with _patch_cliente(cliente):
                    resultado = await server.pje_ler_autos_digitais(
                        numero_cnj=CNJ_VALIDO,
                        acao=alias,
                    )

                self.assertNotIn("erro", resultado)
                cliente.ler_documentos_em_lote.assert_awaited_once()

    async def test_lote_nao_exige_id_documento(self):
        cliente = self._cliente_com_lote({"numero_cnj": CNJ_VALIDO, "leituras": []})

        with _patch_cliente(cliente):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="ler_lote",
            )

        self.assertNotIn("id_documento é obrigatório", str(resultado))


class LimiteDePaginasTests(unittest.IsolatedAsyncioTestCase):
    """max_paginas era fixo em 30; não havia como pedir a peça inteira."""

    async def test_padrao_de_trinta_paginas_e_repassado_por_keyword(self):
        cliente = _cliente_falso(_AbaFalsa(), teor={"texto": "ok"})

        with _patch_cliente(cliente):
            await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="ler_documento",
                id_documento="92241811",
            )

        self.assertEqual(
            cliente.ler_documento.await_args.kwargs, {"max_paginas": 30}
        )

    async def test_zero_significa_documento_inteiro(self):
        # Convenção do pje_client: <= 0 extrai todas as páginas.
        cliente = _cliente_falso(_AbaFalsa(), teor={"texto": "as 120 paginas"})

        with _patch_cliente(cliente):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="ler_documento",
                id_documento="92241811",
                max_paginas=0,
            )

        self.assertEqual(cliente.ler_documento.await_args.kwargs, {"max_paginas": 0})
        self.assertIs(resultado["leitura_completa"], True)

    async def test_negativo_e_normalizado_para_sem_limite(self):
        cliente = _cliente_falso(_AbaFalsa(), teor={"texto": "ok"})

        with _patch_cliente(cliente):
            await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="ler_documento",
                id_documento="92241811",
                max_paginas=-5,
            )

        self.assertEqual(cliente.ler_documento.await_args.kwargs, {"max_paginas": 0})

    async def test_valor_nao_numerico_falha_antes_do_navegador(self):
        with patch.object(
            server.cliente_singleton, "get_cliente", AsyncMock()
        ) as get_cliente:
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="ler_documento",
                id_documento="92241811",
                max_paginas="trinta",
            )

        get_cliente.assert_not_awaited()
        self.assertIn("max_paginas deve ser um número inteiro", resultado["erro"])

    async def test_limite_tambem_chega_ao_lote(self):
        cliente = _cliente_falso(_AbaFalsa())
        cliente.ler_documentos_em_lote = AsyncMock(
            return_value={"numero_cnj": CNJ_VALIDO, "leituras": []}
        )

        with _patch_cliente(cliente):
            await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="ler_lote",
                max_paginas=0,
            )

        self.assertEqual(
            cliente.ler_documentos_em_lote.await_args.kwargs["max_paginas"], 0
        )


class FalhaDuranteLeituraTests(unittest.IsolatedAsyncioTestCase):
    async def test_falha_na_pagina_fecha_a_aba_e_devolve_erro(self):
        aba = _AbaFalsa(falhar=True)
        cliente = _cliente_falso(aba)

        with _patch_cliente(cliente):
            resultado = await server.pje_ler_autos_digitais(
                numero_cnj=CNJ_VALIDO,
                acao="listar",
            )

        self.assertIn("caiu no meio da leitura", resultado["erro"])
        self.assertEqual(resultado["acao"], "listar")
        # A aba não pode vazar quando a leitura falha.
        cliente._fechar_aba_autos.assert_awaited_once_with(aba)


if __name__ == "__main__":
    unittest.main()
