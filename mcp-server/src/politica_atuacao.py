"""Política de contenção da atuação assistida.

Autoridade única sobre o que o Playwright pode fazer fora do modo leitura:
- estados OBSERVACAO / PREPARACAO_ASSISTIDA / AGUARDANDO_COMMIT_HUMANO;
- kill-switch por variável de ambiente (default de fábrica: desligado);
- posse de sessão assistida (uma única por processo, fail-closed);
- gate de reversibilidade de transição (nada INDETERMINADO ou COMMIT_NA_SELECAO
  chega à UI);
- allowlist de seletores + denylist de commit para cliques programáticos;
- trilha append-only que é PRÉ-CONDIÇÃO da atuação: se o log não puder ser
  escrito, a preparação não acontece.
"""

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

# Estados da Política
OBSERVACAO = "OBSERVACAO"
PREPARACAO_ASSISTIDA = "PREPARACAO_ASSISTIDA"
AGUARDANDO_COMMIT_HUMANO = "AGUARDANDO_COMMIT_HUMANO"

# Classificações de reversibilidade de uma transição de fluxo.
REVERSIVEL_ANTES_COMMIT = "REVERSIVEL_ANTES_COMMIT"
COMMIT_NA_SELECAO = "COMMIT_NA_SELECAO"
INDETERMINADA = "INDETERMINADA"

# Só estas fontes autorizam tratar uma transição como reversível. A heurística
# por tipo de elemento NUNCA autoriza: ela apenas sugere onde investigar.
FONTES_REVERSIBILIDADE_CONFIAVEIS = frozenset(
    {"confirmacao_humana", "prova_empirica"}
)

# Expressão regular compilada com os termos proibidos (denylist)
# que indicam confirmação, movimentação, protocolo ou assinatura.
DENYLIST_COMMIT = re.compile(
    r"\b("
    r"encaminhar|confirmar|mover|assinar|concluir|enviar|finalizar|lançar|"
    r"protocolar|confirmar_e_mover|comitar|gravar|salvar|ok|registrar|emitir|"
    r"aplicar|distribuir|despachar|decidir|sentenciar|cumprir|certificar|executar|"
    r"publicar|avançar|prosseguir"
    r")\b",
    re.IGNORECASE
)

# Allowlist (default-deny) de seletores CSS que a preparação assistida pode
# clicar programaticamente. Tudo fora daqui é bloqueado antes do clique.
ALLOWLIST_SELECTORS = re.compile(
    r"^("
    r"input\[type=['\"]?checkbox['\"]?\]|"
    r"input\[type=['\"]?radio['\"]?\]|"
    r"select|"
    r"option|"
    r"a\.aba-link|"
    r"a\[class\*=['\"]?aba-link['\"]?\]|"
    r"\.dropdown-item|"
    r"li\.dropdown-item|"
    r"div\.card-header|"
    r"span\.nome|"
    r"a\[href\*=['\"]?lista-processos-tarefa['\"]?\]"
    r")$",
    re.IGNORECASE
)

# Caminhos (sem query string) de requisições mutantes que fazem parte da
# leitura/autenticação normal e continuam permitidos em qualquer modo.
SAFE_MUTATING_PATH = re.compile(
    r"(recuperarProcessos|kc[-_]?login|login|sso|openid|token|"
    r"agenda|status|confirmar_consulta|schema)",
    re.IGNORECASE,
)


class BloqueioSegurancaException(PermissionError):
    """Exceção levantada quando uma ação viola as políticas de contenção."""
    pass


class PoliticaAtuacao:
    def __init__(self):
        self.modo_ativo = OBSERVACAO
        self._lock = threading.Lock()
        # Token do cliente que detém a única sessão assistida permitida.
        self._sessao_dona: str | None = None
        self.configurar_storage(
            os.environ.get("PJE_STORAGE_DIR", "/var/lib/pjepa-mcp/storage")
        )
        # Kill-switch: o processo nasce em OBSERVACAO a menos que o ambiente
        # habilite explicitamente a atuação assistida.
        if not self._kill_switch_habilitado():
            self.modo_ativo = OBSERVACAO

    # ------------------------------------------------------------------ infra

    def configurar_storage(self, diretorio: str | os.PathLike) -> None:
        """Define (ou redefine, em testes) onde a trilha append-only vive."""
        self.storage_dir = Path(diretorio)
        self.log_file = self.storage_dir / "logs" / "atuacao_log.jsonl"

    @staticmethod
    def _kill_switch_habilitado() -> bool:
        return os.environ.get("PJE_ATUACAO_ASSISTIDA", "0") == "1"

    def _log_disponivel(self) -> bool:
        """Prova, agora, que a trilha pode ser escrita. Sem prova, sem atuação."""
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_file, "a", encoding="utf-8"):
                pass
            return True
        except Exception as exc:
            logging.error(
                "Trilha de atuação indisponível em %s: %s", self.log_file, exc
            )
            return False

    # ------------------------------------------------------------------ modos

    def set_modo(self, modo: str):
        """Altera o modo ativo. Sem kill-switch ou sem trilha, força OBSERVACAO."""
        if modo not in (OBSERVACAO, PREPARACAO_ASSISTIDA, AGUARDANDO_COMMIT_HUMANO):
            raise ValueError(f"Modo inválido: {modo}")

        if modo != OBSERVACAO and (
            not self._kill_switch_habilitado() or not self._log_disponivel()
        ):
            self.modo_ativo = OBSERVACAO
        else:
            self.modo_ativo = modo

    def modo_para(self, token: str | None) -> str:
        """Modo efetivo para um cliente específico.

        Só o dono da sessão assistida enxerga o modo global; qualquer outro
        cliente concorrente opera como OBSERVACAO. Isso impede que uma sessão
        paralela herde AGUARDANDO_COMMIT_HUMANO e rode sem guarda.
        """
        with self._lock:
            if self._sessao_dona is not None and token == self._sessao_dona:
                return self.modo_ativo
        return OBSERVACAO

    # --------------------------------------------------- posse da sessão

    def iniciar_sessao_assistida(
        self, token: str, perfil: Dict[str, Any] | None = None
    ) -> None:
        """Reivindica a única sessão assistida do processo (fail-closed)."""
        if not self._kill_switch_habilitado():
            raise BloqueioSegurancaException(
                "Atuação assistida desabilitada: PJE_ATUACAO_ASSISTIDA != 1."
            )
        if not self._log_disponivel():
            raise BloqueioSegurancaException(
                "Atuação assistida bloqueada: trilha de auditoria "
                f"indisponível em {self.log_file}."
            )
        with self._lock:
            if self._sessao_dona is not None and self._sessao_dona != token:
                raise BloqueioSegurancaException(
                    "Já existe uma sessão assistida ativa neste servidor; "
                    "apenas uma é permitida por vez."
                )
            self._sessao_dona = token
            self.modo_ativo = PREPARACAO_ASSISTIDA
        self.registrar_log(perfil, None, {"status": "sessao_assistida_iniciada"})

    def encerrar_sessao_assistida(self, token: str) -> None:
        """Devolve o processo a OBSERVACAO. Idempotente para o dono."""
        with self._lock:
            if self._sessao_dona is not None and self._sessao_dona != token:
                return
            self._sessao_dona = None
            self.modo_ativo = OBSERVACAO

    def transicionar_para_commit_humano(self, token: str) -> None:
        """Congela a sessão do dono em AGUARDANDO_COMMIT_HUMANO."""
        with self._lock:
            if self._sessao_dona != token:
                raise BloqueioSegurancaException(
                    "Somente o dono da sessão assistida pode congelá-la "
                    "para o commit humano."
                )
            self.modo_ativo = AGUARDANDO_COMMIT_HUMANO

    # ------------------------------------------------------------------ log

    def registrar_log(
        self,
        perfil: Dict[str, Any] | None,
        lote_hash: str | None,
        detalhes: Dict[str, Any],
    ):
        """Grava a trilha append-only. Fora de OBSERVACAO, falha de escrita
        é falha da operação — auditoria é pré-condição da atuação."""
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "perfil": perfil,
            "lote_hash": lote_hash,
            "modo": self.modo_ativo,
            "detalhes": detalhes,
        }
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logging.error(f"Falha ao registrar log de atuação assistida: {e}")
            if self.modo_ativo != OBSERVACAO:
                raise BloqueioSegurancaException(
                    "Operação abortada: trilha de auditoria não pôde ser "
                    f"escrita ({e})."
                ) from e

    # ------------------------------------------------------------- validações

    def validar_transicao(
        self,
        destino: Dict[str, Any] | None,
        perfil: Dict[str, Any] | None = None,
        lote_hash: str | None = None,
    ) -> None:
        """Gate duro de reversibilidade, avaliado ANTES de qualquer UI.

        Bloqueia quando o destino não foi mapeado, quando a classificação não
        é REVERSIVEL_ANTES_COMMIT ou quando a classificação veio apenas de
        heurística (fonte não confiável). Levanta BloqueioSegurancaException.
        """
        if destino is None:
            detalhes = {
                "status": "abortado_gate_transicao",
                "motivo": (
                    "destino_id não consta no mapa de transições deste perfil "
                    "e tarefa; rode mapear_transicoes_tarefa primeiro."
                ),
            }
            self.registrar_log(perfil, lote_hash, detalhes)
            raise BloqueioSegurancaException(
                "Transição bloqueada: destino não mapeado para este perfil/tarefa."
            )

        reversivel = str(destino.get("reversivel") or INDETERMINADA)
        fonte = str(destino.get("fonte") or "heuristica_tipo_elemento")

        if reversivel != REVERSIVEL_ANTES_COMMIT:
            detalhes = {
                "status": "abortado_gate_transicao",
                "destino_id": destino.get("destino_id"),
                "reversibilidade": reversivel,
                "motivo": (
                    "somente transições REVERSIVEL_ANTES_COMMIT podem ser "
                    "preparadas; esta está classificada como " + reversivel
                ),
            }
            self.registrar_log(perfil, lote_hash, detalhes)
            raise BloqueioSegurancaException(
                f"Transição bloqueada: classificada como {reversivel}."
            )

        if fonte not in FONTES_REVERSIBILIDADE_CONFIAVEIS:
            detalhes = {
                "status": "abortado_gate_transicao",
                "destino_id": destino.get("destino_id"),
                "reversibilidade": reversivel,
                "fonte": fonte,
                "motivo": (
                    "classificação sem prova: fonte de reversibilidade "
                    f"'{fonte}' não é confiável; confirme com "
                    "confirmar_reversibilidade_transicao."
                ),
            }
            self.registrar_log(perfil, lote_hash, detalhes)
            raise BloqueioSegurancaException(
                "Transição bloqueada: reversibilidade presumida por "
                f"heurística ('{fonte}'), não provada."
            )

        self.registrar_log(
            perfil,
            lote_hash,
            {
                "status": "gate_transicao_aprovado",
                "destino_id": destino.get("destino_id"),
                "fonte": fonte,
            },
        )

    def validar_clique(
        self,
        text: str,
        title: str,
        aria_label: str,
        value: str,
        selector: str,
        perfil: Dict[str, Any] | None = None,
        lote_hash: str | None = None,
    ) -> None:
        """Valida um clique programático. Levanta BloqueioSegurancaException.

        Em OBSERVACAO nada de escrita é permitido. Em PREPARACAO_ASSISTIDA o
        seletor precisa constar na allowlist (default-deny) e nenhum atributo
        do elemento pode conter termo da denylist de commit.
        """
        if self.modo_ativo == OBSERVACAO:
            detalhes = {
                "status": "abortado_somente_leitura",
                "seletor": selector,
                "texto": text,
                "motivo": (
                    "Bloqueado porque a política está configurada como "
                    "OBSERVACAO (somente leitura)."
                ),
            }
            self.registrar_log(perfil, lote_hash, detalhes)
            raise BloqueioSegurancaException(
                "Ação bloqueada: o servidor está configurado no modo OBSERVACAO."
            )

        # Verificar texto e atributos contra a denylist
        for campo, val in [
            ("text", text),
            ("title", title),
            ("aria-label", aria_label),
            ("value", value),
        ]:
            if val and DENYLIST_COMMIT.search(val):
                detalhes = {
                    "status": "abortado_denylist",
                    "seletor": selector,
                    "campo_violador": campo,
                    "valor_violador": val,
                    "motivo": (
                        "Ação bloqueada por conter termo de commit/"
                        f"finalização no campo '{campo}': {val}"
                    ),
                }
                self.registrar_log(perfil, lote_hash, detalhes)
                raise BloqueioSegurancaException(
                    f"Ação bloqueada: o elemento contém o termo proibido "
                    f"'{val}' (violou política de contenção)."
                )

        # Allowlist default-deny de seletores clicáveis na preparação.
        if not ALLOWLIST_SELECTORS.match(selector or ""):
            detalhes = {
                "status": "abortado_allowlist",
                "seletor": selector,
                "texto": text,
                "motivo": (
                    "Seletor fora da allowlist de preparação assistida "
                    "(default-deny)."
                ),
            }
            self.registrar_log(perfil, lote_hash, detalhes)
            raise BloqueioSegurancaException(
                f"Ação bloqueada: seletor '{selector}' não consta na "
                "allowlist da preparação assistida."
            )

        # Log do clique autorizado
        detalhes = {
            "status": "permitido",
            "seletor": selector,
            "texto": text,
        }
        self.registrar_log(perfil, lote_hash, detalhes)


# Singleton global para a política de atuação
politica_atuacao = PoliticaAtuacao()
