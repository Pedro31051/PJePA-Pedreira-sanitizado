"""Servidor MCP para consultas no PJe-TJPA - 1o E 2o GRAUS.

Um unico servidor atende as duas instancias do Tribunal de Justica do
Para: 1g (varas, pje.tjpa.jus.br/pje) e 2g (camaras/turmas,
pje.tjpa.jus.br/pje-2g). Toda tool aceita o parametro 'grau' ('1'/'2').

Tools disponiveis (24):
  CONSULTA:
    - expedientes_pendentes, verificar_prazos_urgentes
    - consultar_processo, ultimas_movimentacoes, relatorio_processo
    - buscar_por_nome_parte, buscar_por_nome_advogado
    - buscar_por_cpf, buscar_por_cnpj, buscar_por_oab
    - listar_documentos, ler_documento

  CONSULTA PONTUAL (sem download):
    - ultima_decisao, ultimo_despacho, pendencias_processo
    - expedientes_do_processo (historico completo, inclui fechados)

  DOWNLOAD:
    - baixar_documento (1 doc especifico)
    - baixar_processo (autos completos via download nativo do PJe)
    - status_download (acompanha download em background)
    - preparar_processo (baixa + decide leitura direta vs ingestão local)

  MODELOS E MINUTAS:
    - listar_modelos_peticao, ler_modelo_peticao
    - salvar_peticao_processo, salvar_relatorio_processo

As ferramentas autenticadas suportam personas internas (servidor/magistrado)
e externas (advogado/procurador). Usuários internos devem informar o perfil
funcional completo (localização/unidade + papel).
Sessao do Chrome eh reusada por ate 5min entre tool calls (singleton).
"""

import asyncio
import base64
import difflib
import functools
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).parent))

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

import analise_processual_completa
import analysis_capsule
import auditoria_processual
import batch_engine
import browser_bridge_client
import caixas_tarefas
import cliente_singleton
import consolidated_process
import document_security
import minutas
import modelos
import observabilidade_acervo
import pdf_scan
import perfil_contexto
import pje_downloader
import report_format
import retention_policy
import vertex_process_agent
from api.extension_security import cors_headers, origin_allowed, token_status
from contracts import (
    ErrorCategory,
    ErrorCode,
    ErrorDetail,
    OperationResult,
)
from mapa_pje import mask_process_search_value
from observability import (
    LatencyTracker,
    _current_latency_tracker,
    set_current_latency_tracker,
)
from operation_context import (
    OperationContext,
)


async def _watchdog_inatividade():
    """Fecha a sessao do PJe (Chromium) DE FATO apos o timeout de inatividade.

    Sem isso o browser ficava vivo indefinidamente entre chamadas - o
    timeout do singleton so era avaliado lazy, na tool call seguinte.
    """
    while True:
        await asyncio.sleep(60)
        try:
            await cliente_singleton.fechar_se_ocioso()
            await asyncio.to_thread(analysis_capsule.purge_expired, mode="production")
            await asyncio.to_thread(analysis_capsule.purge_expired, mode="training")
        except Exception as e:
            print(f"[WATCHDOG] erro ignorado: {e}", file=sys.stderr, flush=True)


@asynccontextmanager
async def _runtime_pje():
    """Recursos globais do processo HTTP, executados uma única vez.

    O lifespan do servidor MCP de baixo nível roda uma vez por ``app.run()``.
    Em Streamable HTTP stateless, o SDK cria um ``app.run()`` para cada POST;
    portanto colocar warm-up ali criava um Playwright por requisição. Este
    contexto é acoplado ao lifespan da aplicação ASGI, que dura o processo.
    """
    tarefas = []
    # Fail-closed: nem autenticação preventiva acessa o PJe antes de uma
    # consulta confirmada pelo usuário.
    if os.environ.get("PJE_WARMUP", "0") == "1":
        tarefas.append(asyncio.create_task(cliente_singleton.warmup()))
    tarefas.append(asyncio.create_task(_watchdog_inatividade()))
    tarefas.append(asyncio.create_task(_retomar_analises_completas()))
    tarefas.append(asyncio.create_task(_retomar_agentes_vertex()))
    tarefas.append(asyncio.create_task(batch_engine._processar_fila_de_lotes_loop(_agendar_analise_completa, _complete_analysis_tasks)))
    try:
        yield {}
    finally:
        for t in tarefas:
            t.cancel()
        for t in list(_audit_background_tasks):
            t.cancel()
        for t in list(_complete_analysis_tasks.values()):
            t.cancel()
        for t in list(_vertex_agent_tasks.values()):
            t.cancel()
        for t in list(_ephemeral_pdf_tasks.values()):
            t.cancel()
        for t in list(_ephemeral_agent_tasks.values()):
            t.cancel()
        await asyncio.gather(*tarefas, return_exceptions=True)
        if _audit_background_tasks:
            await asyncio.gather(
                *_audit_background_tasks,
                return_exceptions=True,
            )
        if _complete_analysis_tasks:
            await asyncio.gather(
                *_complete_analysis_tasks.values(),
                return_exceptions=True,
            )
        if _vertex_agent_tasks:
            await asyncio.gather(
                *_vertex_agent_tasks.values(),
                return_exceptions=True,
            )
        if _ephemeral_pdf_tasks:
            await asyncio.gather(
                *_ephemeral_pdf_tasks.values(),
                return_exceptions=True,
            )
        if _ephemeral_agent_tasks:
            await asyncio.gather(
                *_ephemeral_agent_tasks.values(),
                return_exceptions=True,
            )
        try:
            await cliente_singleton.fechar_cliente()
        except Exception:
            pass


mcp = FastMCP(
    "PJePA Pedreira",
    host=os.getenv("MCP_HOST", "127.0.0.1"),
    port=int(os.getenv("MCP_PORT", "8001")),
    stateless_http=True,
    json_response=True,
)

_audit_background_tasks: set[asyncio.Task] = set()
_complete_analysis_tasks: dict[str, asyncio.Task] = {}
_vertex_agent_tasks: dict[str, asyncio.Task] = {}
_ephemeral_pdf_tasks: dict[str, asyncio.Task] = {}
_ephemeral_pdf_jobs: dict[str, dict[str, Any]] = {}
_ephemeral_agent_tasks: dict[str, asyncio.Task] = {}
_ephemeral_agent_runs: dict[str, dict[str, Any]] = {}


def _criar_app_http():
    """Cria a aplicação ASGI com runtime PJe no lifespan do processo."""
    app = mcp.streamable_http_app()
    lifespan_sessoes_mcp = app.router.lifespan_context

    async def analise_extensao(request):
        import hashlib
        import json
        from datetime import datetime

        from starlette.responses import JSONResponse

        import analise_processual_completa
        import auditoria_processual

        origin = request.headers.get("origin", "")
        headers = cors_headers(origin)
        if not origin_allowed(origin):
            return JSONResponse(
                {"erro": "Origem da extensão não autorizada"},
                status_code=403,
                headers=headers,
            )
        if request.method == "OPTIONS":
            return JSONResponse({}, headers=headers)

        auth_status = token_status(
            request.headers.get("authorization", ""),
            request.headers.get("x-extension-token", ""),
        )
        if auth_status == "misconfigured":
            return JSONResponse(
                {"erro": "Extensão indisponível: autenticação não configurada"},
                status_code=503,
                headers=headers,
            )
        if auth_status == "invalid":
            return JSONResponse(
                {"erro": "Não autorizado: token inválido ou ausente"},
                status_code=401,
                headers=headers,
            )

        try:
            data = await request.json()
            numero_cnj = data["numero_cnj"]
            grau = data.get("grau", "1g")
            persona = data.get("persona", "servidor")
            base_info = data.get("base", {})
            documents = data.get("documents", [])
            expedientes = data.get("expedientes", [])

            # Cria o job de auditoria no SQLite
            job = await asyncio.to_thread(
                analise_processual_completa.create_job,
                process_number=numero_cnj,
                grau=grau,
                persona=persona,
                mode="integral",
                semantic_enabled=False,
                force_reread=True,
                authorisation_ref="extensao_chrome"
            )
            job_id = job["job_id"]

            await asyncio.to_thread(
                analise_processual_completa.update_job,
                job_id,
                status="running",
                phase="manifest"
            )

            manifest = []
            processed_documents = []

            for order, doc in enumerate(documents, start=1):
                doc_id = str(doc["id"])
                tipo = doc.get("tipo", "Outro")
                titulo = doc.get("titulo", f"{tipo} ({doc_id})")
                texto = doc.get("texto", "")

                fingerprint = hashlib.sha256(f"extensao:{numero_cnj}:{doc_id}".encode("utf-8")).hexdigest()
                content_hash = hashlib.sha256(texto.encode("utf-8")).hexdigest()

                doc_payload = {
                    "document_id": doc_id,
                    "type": tipo,
                    "title": titulo,
                    "content_sha256": content_hash,
                    "pages": [{"page": 1, "text": texto}],
                    "pages_without_text": 0 if texto else 1,
                    "truncated": False
                }

                # Salva no cache do SQLite
                await asyncio.to_thread(
                    auditoria_processual.save_cached_document,
                    numero_cnj,
                    doc_id,
                    fingerprint,
                    content_hash,
                    doc_payload
                )

                # Salva estado da leitura
                await asyncio.to_thread(
                    analise_processual_completa.save_document_state,
                    job_id,
                    document_id=doc_id,
                    source_fingerprint=fingerprint,
                    status="completed",
                    content_sha256=content_hash,
                    pages=1,
                    pages_without_text=0 if texto else 1,
                    truncated=False,
                    reused=False
                )

                manifest_entry = {
                    "schema_version": "pje.document-manifest/v2",
                    "document_id": doc_id,
                    "parent_document_id": None,
                    "canonical_order": order,
                    "type": tipo,
                    "type_code": None,
                    "title": titulo,
                    "date": None,
                    "author": None,
                    "mime_type": None,
                    "size_bytes": len(texto.encode("utf-8")),
                    "visibility": None,
                    "active": None,
                    "signed": None,
                    "source_origin": "extensao_chrome",
                    "source_fingerprint": fingerprint,
                    "source_fields": {}
                }
                manifest.append(manifest_entry)
                processed_documents.append(doc_payload)

            manifest_sha = hashlib.sha256(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()

            await asyncio.to_thread(
                analise_processual_completa.update_job,
                job_id,
                phase="facts",
                total_documents=len(manifest),
                manifest_sha256=manifest_sha
            )

            states = await asyncio.to_thread(analise_processual_completa.document_states, job_id)
            states_by_id = {str(item["document_id"]): item for item in states}

            manifest_with_coverage = [
                {
                    **item,
                    "status": states_by_id.get(str(item["document_id"]), {}).get("status", "completed"),
                    "content_sha256": states_by_id.get(str(item["document_id"]), {}).get("content_sha256"),
                    "pages": 1,
                    "pages_without_text": states_by_id.get(str(item["document_id"]), {}).get("pages_without_text", 0),
                    "duplicate_of": None
                }
                for item in manifest
            ]

            dossier = await asyncio.to_thread(
                analise_processual_completa.build_dossier,
                process_number=numero_cnj,
                base=base_info,
                manifest=manifest_with_coverage,
                documents=processed_documents,
                document_states_value=states,
                tree_complete=True,
                expedients=expedientes,
                collected_at=datetime.now().astimezone().isoformat(),
                expedients_complete=True,
                expedients_source="extensao_chrome",
                manifest_reconciled=True,
                metadata_complete=True
            )

            await asyncio.to_thread(analise_processual_completa.save_result, job_id, dossier)
            await asyncio.to_thread(
                analise_processual_completa.update_job,
                job_id,
                status="completed",
                phase="completed",
                processed_documents=len(documents)
            )

            return JSONResponse({"status": "sucesso", "job_id": job_id, "dossier": dossier}, headers=headers)

        except Exception as e:
            import traceback
            traceback.print_exc()
            return JSONResponse({"erro": str(e)}, status_code=500, headers=headers)

    app.add_route("/analise_extensao", analise_extensao, methods=["POST", "OPTIONS"])

    @asynccontextmanager
    async def lifespan_completo(app_instance):
        # Mantém o session_manager oficial do SDK e envolve-o com o runtime
        # único do navegador/watchdog.
        async with lifespan_sessoes_mcp(app_instance):
            async with _runtime_pje():
                yield

    app.router.lifespan_context = lifespan_completo
    return app



TRIBUNAL = "TJPA"
GRAUS_LEGIVEIS = {"1g": "1º grau", "2g": "2º grau"}


def _parametros_confirmacao_chamada(
    valores: dict[str, Any],
) -> dict[str, Any]:
    """Copia somente os parâmetros públicos que definem a consulta."""
    return {
        chave: valor
        for chave, valor in valores.items()
        if chave not in {"confirmar_consulta", "confirmation_token"}
    }


def _exigir_confirmacao_consulta(
    ferramenta: str,
    acao: str,
    parametros: dict[str, Any],
    confirmar_consulta: str,
    token_confirmacao: str,
) -> dict[str, Any] | None:
    """Compatibilidade: consultas válidas executam sem handshake duplicado."""
    del ferramenta, acao, parametros, confirmar_consulta, token_confirmacao
    return None


def _normaliza_persona(persona: str) -> str:
    """Normaliza a persona sem reclassificar usuário interno como advogado."""
    return perfil_contexto.normalizar_persona(persona)


async def _injetar_evidencia_se_solicitado(
    retorno: Any,
    capturar_screenshot: bool,
    persona: str,
    grau: str,
    full_page: bool = False,
) -> Any:
    if not capturar_screenshot or not isinstance(retorno, dict):
        return retorno
    try:
        _normaliza_persona(persona)
        _normaliza_grau(grau)
        pje = cliente_singleton._cliente
        if pje and pje._page and not pje._page.is_closed():
            from evidencia import tirar_evidencia_efemera
            retorno["evidencia_screenshot"] = await tirar_evidencia_efemera(
                pje._page, full_page=full_page
            )
    except Exception as exc:
        retorno["evidencia_screenshot_erro"] = str(exc)
    return retorno


def _log_audit(data: dict) -> None:
    """Registra uma entrada de auditoria estruturada em JSON para o stderr."""
    if data.get("nivel") == "ALERT":
        sys.stderr.write(f"[ALERT] {json.dumps(data)}\n")
    else:
        sys.stderr.write(f"[AUDIT] {json.dumps(data)}\n")
    sys.stderr.flush()


class OperationResultDict(dict):
    def __init__(self, inner_data: dict, envelope_data: dict):
        super().__init__(envelope_data)
        self._inner_data = inner_data
        self._orig_has_status = "status" in inner_data

    def __eq__(self, other: Any) -> bool:
        if super().__eq__(other):
            return True
        if isinstance(other, dict) and (other == self._inner_data or dict(other) == self._inner_data):
            return True
        return False

    def __contains__(self, key: Any) -> bool:
        if key == "status" and not self._orig_has_status:
            return False
        return super().__contains__(key)


def _padronizar_resultado_mcp(
    resultado: Any, tracker: LatencyTracker, erro_excecao: Exception | None = None
) -> Dict[str, Any]:
    lat = tracker.finalize()
    if erro_excecao is not None:
        err_detail = ErrorDetail(
            code=ErrorCode.UNEXPECTED_INTERNAL_ERROR,
            category=ErrorCategory.INTERNAL,
            message=str(erro_excecao),
            is_recoverable=False,
            origin="INTERNAL_MCP",
            details={"exception_type": type(erro_excecao).__name__},
        )
        op_res = OperationResult(
            status="error",
            data={"erro": str(erro_excecao)},
            latency=lat,
            error=err_detail,
        )
        return op_res.to_dict()

    if not isinstance(resultado, dict):
        op_res = OperationResult(
            status="success",
            data={"resultado": resultado},
            latency=lat,
        )
        return op_res.to_dict()

    if (
        "completeness" in resultado
        and "latency" in resultado
        and "status" in resultado
        and "data" in resultado
        and "gap_details" in resultado
    ):
        if not resultado.get("latency") or resultado["latency"].get("total_duration_ms", 0.0) == 0.0:
            resultado["latency"] = lat.to_dict()
        return resultado

    orig_status = resultado.get("status")

    is_error = False
    err_code = ErrorCode.UNEXPECTED_INTERNAL_ERROR
    err_cat = ErrorCategory.INTERNAL
    err_msg = ""
    is_rec = False
    origin = "INTERNAL_MCP"

    status_str = str(orig_status or "").lower()
    is_explicit_success = status_str in ("success", "sucesso", "ok", "completed")

    if status_str in ("error", "erro", "bloqueado", "failed", "falha"):
        is_error = True
    elif is_explicit_success:
        err_val = resultado.get("erro") or resultado.get("error") if isinstance(resultado, dict) else None
        if err_val and not isinstance(err_val, bool):
            if isinstance(err_val, (dict, str)) and len(err_val) > 0:
                is_error = True
            else:
                is_error = False
        else:
            is_error = False
    else:
        # Nenhum status explicito de sucesso; inspeciona resultado
        if isinstance(resultado, dict):
            err_val = resultado.get("erro") or resultado.get("error")
            raw_code = str(resultado.get("codigo") or resultado.get("code") or "")
            if err_val is not None and err_val != "" and err_val is not False:
                is_error = True
            elif raw_code and raw_code not in ("SESSAO_CONTEXTO_FIXADO", "OK", "SUCESSO", "SUCCESS", "COMPLETED"):
                is_error = True
            else:
                is_error = False
        else:
            is_error = False

    err_detail = None
    if is_error:
        err_msg = str(
            resultado.get("erro")
            or resultado.get("message")
            or resultado.get("motivo")
            or "Erro na operacao MCP"
        )
        raw_code = str(resultado.get("codigo") or resultado.get("code") or "")

        if raw_code in (
            "CONTEXTO_DIVERGENTE",
            "PERFIL_OBRIGATORIO",
            "PERFIL_DIVERGENTE",
            "INVALID_PERFIL",
        ):
            err_code = ErrorCode.INVALID_PERFIL
            err_cat = ErrorCategory.VALIDATION
            is_rec = False
        elif "CNJ" in err_msg or "cnj" in err_msg or raw_code == "CNJ_INVALIDO":
            err_code = ErrorCode.INVALID_CNJ_FORMAT
            err_cat = ErrorCategory.VALIDATION
            is_rec = False
        elif "AUTH" in raw_code or "SESSAO" in raw_code or "EXPIRADA" in raw_code:
            err_code = ErrorCode.AUTH_EXPIRED
            err_cat = ErrorCategory.AUTHENTICATION
            is_rec = True
            origin = "TJPA_EXTERNAL"
        elif "503" in raw_code or "UNAVAILABLE" in raw_code or "503" in err_msg:
            err_code = ErrorCode.TJPA_503_UNAVAILABLE
            err_cat = ErrorCategory.EXTERNAL_TJPA
            is_rec = True
            origin = "TJPA_EXTERNAL"
        elif "SINGLE_FLIGHT" in raw_code:
            err_code = ErrorCode.SINGLE_FLIGHT_TIMEOUT
            err_cat = ErrorCategory.CONCURRENCY
            is_rec = True
        elif "NAO_ENCONTRADO" in raw_code or "NOT_FOUND" in raw_code:
            err_code = ErrorCode.DOCUMENT_NOT_FOUND
            err_cat = ErrorCategory.PARSING
            is_rec = False
        elif raw_code:
            try:
                err_code = ErrorCode(raw_code)
            except ValueError:
                err_code = ErrorCode.UNEXPECTED_INTERNAL_ERROR

        err_detail = ErrorDetail(
            code=err_code,
            category=err_cat,
            message=err_msg,
            is_recoverable=is_rec,
            origin=origin,
            details=resultado,
        )

    determined_status = orig_status or ("error" if is_error else "success")
    op_res = OperationResult(
        status=str(determined_status),
        data=resultado,
        latency=lat,
        error=err_detail,
    )
    res_dict = op_res.to_dict()
    if orig_status is not None:
        res_dict["status"] = orig_status
    return OperationResultDict(resultado, res_dict)


def auditar_ferramenta(func):
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        request_id = uuid.uuid4().hex
        inicio = time.monotonic()
        tracker = LatencyTracker()
        token = set_current_latency_tracker(tracker)
        
        try:
            # Extrai os parâmetros
            persona = kwargs.get("persona", "servidor")
            grau = kwargs.get("grau", "1")
            perfil = kwargs.get("perfil", "")
            
            # Mascara o CPF do usuário para o hash de auditoria
            try:
                cpf, _, _ = cliente_singleton._get_creds()
                cpf_hash = hashlib.sha256(cpf.encode()).hexdigest()[:16]
            except Exception:
                cpf_hash = "offline"
                
            perfil_solicitado_hash = (
                hashlib.sha256(
                    perfil_contexto.normalizar_texto(perfil).encode("utf-8")
                ).hexdigest()[:16]
                if perfil
                else None
            )
            
            log_data = {
                "evento": "mcp_tool_call_start",
                "request_id": request_id,
                "ferramenta": func.__name__,
                "hash_usuario": cpf_hash,
                "persona": persona,
                "grau": grau,
                "perfil_solicitado_hash": perfil_solicitado_hash,
            }
            _log_audit(log_data)
            
            ctx = OperationContext(
                operation_name=func.__name__,
                correlation_id=request_id,
                persona=_normaliza_persona(persona),
                grau=_normaliza_grau(grau),
            )
            try:
                async with ctx:
                    async with tracker.measure("internal_mcp"):
                        resultado = await func(*args, **kwargs)
                duracao = round((time.monotonic() - inicio) * 1000, 2)
                
                # Obtém perfil confirmado e página atual se o cliente estiver ativo
                perfil_confirmado_hash = None
                pagina_encontrada = None
                session_id = None
                if cliente_singleton._cliente:
                    p_ctx = cliente_singleton.perfil_contexto.contexto_atual()
                    if p_ctx:
                        confirmado = (
                            p_ctx.get("localizador_nao_confiavel")
                            or p_ctx.get("rotulo")
                            or ""
                        )
                        if confirmado:
                            perfil_confirmado_hash = hashlib.sha256(
                                perfil_contexto.normalizar_texto(confirmado).encode(
                                    "utf-8"
                                )
                            ).hexdigest()[:16]
                    if cliente_singleton._cliente._page:
                        try:
                            pagina_encontrada = re.sub(
                                r"[?#].*$",
                                "",
                                cliente_singleton._cliente._page.url,
                            )
                        except Exception:
                            pass
                    if cliente_singleton._chave_ativa:
                        session_id = hashlib.sha256(str(cliente_singleton._chave_ativa).encode()).hexdigest()[:16]
                
                try:
                    destino_cache = str(
                        pje_downloader.pasta_base_grau(_normaliza_grau(grau))
                    )
                except perfil_contexto.PerfilObrigatorioError:
                    destino_cache = None
                
                log_data_end = {
                    "evento": "mcp_tool_call_success",
                    "request_id": request_id,
                    "session_id": session_id,
                    "ferramenta": func.__name__,
                    "hash_usuario": cpf_hash,
                    "persona": persona,
                    "grau": grau,
                    "perfil_solicitado_hash": perfil_solicitado_hash,
                    "perfil_confirmado_hash": perfil_confirmado_hash,
                    "pagina_encontrada": pagina_encontrada,
                    "destino_cache": destino_cache,
                    "duracao_ms": duracao,
                    "status": "sucesso"
                }
                
                # Se for dicionário de erro/bloqueio
                if isinstance(resultado, dict) and (
                    (resultado.get("status") and str(resultado.get("status")).lower() in ("error", "erro", "failed", "falha"))
                    or (resultado.get("erro") and resultado.get("erro") is not None)
                ):
                    log_data_end.update(
                        status="erro",
                        codigo_erro=resultado.get("codigo"),
                        motivo_bloqueio=resultado.get("erro")
                    )
                    if resultado.get("codigo") in (
                        "CONTEXTO_DIVERGENTE",
                        "PERFIL_OBRIGATORIO",
                        "PERFIL_DIVERGENTE",
                    ):
                        log_data_end["nivel"] = "ALERT"
                        log_data_end["alerta_tipo"] = resultado.get("codigo")
                # Se a ferramenta pediu screenshot, captura e injeta no resultado.
                capturar_screenshot = kwargs.get("capturar_screenshot", False)
                if capturar_screenshot:
                    full_page = kwargs.get("full_page", False)
                    resultado = await _injetar_evidencia_se_solicitado(
                        resultado, True, persona, grau, full_page=full_page
                    )
                
                wrapped_result = _padronizar_resultado_mcp(resultado, tracker)
                _log_audit(log_data_end)
                return wrapped_result
            except Exception as e:
                duracao = round((time.monotonic() - inicio) * 1000, 2)
                log_data_err = {
                    "evento": "mcp_tool_call_failure",
                    "request_id": request_id,
                    "ferramenta": func.__name__,
                    "hash_usuario": cpf_hash,
                    "persona": persona,
                    "grau": grau,
                    "perfil_solicitado_hash": perfil_solicitado_hash,
                    "duracao_ms": duracao,
                    "status": "excecao",
                    "erro": str(e),
                    "tipo_erro": type(e).__name__
                }
                if "CONTEXTO_DIVERGENTE" in str(e) or type(e).__name__ == "RuntimeError":
                    log_data_err["nivel"] = "ALERT"
                    log_data_err["alerta_tipo"] = "CONTEXTO_DIVERGENTE"
                _log_audit(log_data_err)
                return _padronizar_resultado_mcp(None, tracker, erro_excecao=e)
        finally:
            _current_latency_tracker.reset(token)
    return wrapper


def _ativar_perfil_ferramenta(
    persona: str, grau: str, perfil: str
) -> tuple[str, str, dict[str, Any] | None]:
    """Configura o contexto isolado da chamada e devolve eventual bloqueio."""
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    perfil_contexto.definir_contexto(p, g, perfil)
    erro_id = perfil_contexto.erro_identificador_estavel(p)
    if erro_id:
        # Mesma passagem que painel_e_prazos_pje já usava: sem pje_id vindo do
        # PJe, a sessão fixada por rótulo é a única via. Sem tentar promovê-la
        # aqui, toda ferramenta que depende deste helper ficava inalcançável
        # para usuário interno, mesmo com a sessão já fixada e revalidada.
        if cliente_singleton.promover_contexto_fixado(p, g, perfil):
            return p, g, None
        erro_id["dica"] = (
            "Sem identificador do PJe, valide primeiro com "
            "painel_e_prazos_pje(acao='sessao_contexto_fixado') e repita "
            "com o mesmo perfil."
        )
        return p, g, erro_id
    return p, g, perfil_contexto.erro_perfil_obrigatorio(p)


def _normaliza_grau(grau: str) -> str:
    """Normaliza variacoes de texto para '1g' ou '2g'."""
    if not grau:
        return "1g"
    g = str(grau).strip().lower()
    if any(k in g for k in ["2", "segundo", "apela", "camara", "câmara", "tribunal"]):
        return "2g"
    return "1g"


def _normaliza_cnj(numero_cnj: str) -> str:
    """Normaliza e formata numero CNJ para a mascara NNNNNNN-DD.YYYY.J.TR.OOOO."""
    if not numero_cnj:
        return ""
    digits = re.sub(r"\D", "", numero_cnj)
    if len(digits) in (19, 20):
        d = digits.zfill(20)
        return f"{d[:7]}-{d[7:9]}.{d[9:13]}.{d[13:14]}.{d[14:16]}.{d[16:20]}"
    return numero_cnj.strip()


def _mascarar_cnj(numero_cnj: str) -> str:
    """Redige um CNJ de entrada preservando apenas sequência e origem."""
    digitos = re.sub(r"\D", "", str(numero_cnj or ""))
    if len(digitos) != 20:
        return "[CNJ redigido]"
    return f"{digitos[:7]}-**.****.*.**.{digitos[-4:]}"


def _marcar_grau(r: dict, persona: str, grau: str = "1g") -> dict:
    """Adiciona metadados de tribunal/grau/persona ao retorno."""
    r["tribunal"] = TRIBUNAL
    r["grau"] = GRAUS_LEGIVEIS.get(grau, grau)
    r["persona_utilizada"] = persona
    return r


# =========================================================================
# DESPACHO DE ACOES DAS SUPER FERRAMENTAS
# =========================================================================


def _slug_acao(acao: str) -> str:
    """Reduz uma acao a forma canonica: sem acento, minuscula, separador '_'."""
    if not acao:
        return ""
    s = unicodedata.normalize("NFKD", str(acao))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.strip().lower()
    s = re.sub(r"[\s\-\.]+", "_", s)
    return re.sub(r"[^a-z0-9_]", "", s)


def _resolver_acao(acao: str, acoes: dict, aliases: dict = None) -> tuple:
    """Resolve o nome da acao pedida contra o mapa canonico da ferramenta.

    Tolera maiuscula/acento/hifen/espaco e apelidos comuns. Retorna
    (acao_canonica, None) em caso de sucesso ou (None, dict_de_erro) com a
    lista de acoes validas e a sugestao mais proxima.
    """
    slug = _slug_acao(acao)
    mapa_slug = {_slug_acao(k): k for k in acoes}
    if slug in mapa_slug:
        return mapa_slug[slug], None
    if aliases:
        alvo = {_slug_acao(k): v for k, v in aliases.items()}.get(slug)
        if alvo in acoes:
            return alvo, None
    candidatos = list(mapa_slug) + [_slug_acao(k) for k in (aliases or {})]
    sugestoes = difflib.get_close_matches(slug, candidatos, n=1, cutoff=0.6)
    erro = {
        "erro": "Ação inválida",
        "acao_recebida": acao,
        "acoes_validas": sorted(acoes),
    }
    if sugestoes:
        erro["voce_quis_dizer"] = mapa_slug.get(sugestoes[0], sugestoes[0])
    return None, erro


def _exigir(campo: str, valor: str, acao: str, dica: str = "") -> dict:
    """Retorna um erro estruturado quando um parametro obrigatorio veio vazio."""
    if valor and str(valor).strip():
        return None
    return {
        "erro": f"Parâmetro obrigatório ausente: '{campo}'",
        "acao": acao,
        "dica": dica or f"Informe '{campo}' ao chamar a ação '{acao}'.",
    }


# =========================================================================
# NORMALIZACAO DE CRITERIOS DE BUSCA
# =========================================================================

UFS_BRASIL = {
    "AC",
    "AL",
    "AM",
    "AP",
    "BA",
    "CE",
    "DF",
    "ES",
    "GO",
    "MA",
    "MG",
    "MS",
    "MT",
    "PA",
    "PB",
    "PE",
    "PI",
    "PR",
    "RJ",
    "RN",
    "RO",
    "RR",
    "RS",
    "SC",
    "SE",
    "SP",
    "TO",
}

UF_OAB_PADRAO = "PA"


def _digitos(valor: str) -> str:
    """So os digitos de uma string (None-safe)."""
    return re.sub(r"\D", "", str(valor or ""))


def _cpf_valido(cpf: str) -> bool:
    """Valida CPF pelos digitos verificadores (modulo 11)."""
    d = _digitos(cpf)
    if len(d) != 11 or d == d[0] * 11:
        return False
    for corte in (9, 10):
        soma = sum(int(d[i]) * (corte + 1 - i) for i in range(corte))
        dv = (soma * 10) % 11 % 10
        if dv != int(d[corte]):
            return False
    return True


def _cnpj_valido(cnpj: str) -> bool:
    """Valida CNPJ pelos digitos verificadores (modulo 11)."""
    d = _digitos(cnpj)
    if len(d) != 14 or d == d[0] * 14:
        return False
    for pesos in (
        [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2],
        [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2],
    ):
        corte = len(pesos)
        soma = sum(int(d[i]) * pesos[i] for i in range(corte))
        resto = soma % 11
        dv = 0 if resto < 2 else 11 - resto
        if dv != int(d[corte]):
            return False
    return True


def _parse_oab(valor: str) -> tuple:
    """Separa numero e UF de uma inscricao OAB.

    Aceita '12345/PA', 'PA 12345', 'OAB/PA 12345' ou so o numero.
    Retorna (numero, uf_ou_None).
    """
    numero, _letra, uf = _parse_oab_detalhada(valor)
    return numero, uf


def _parse_oab_detalhada(valor: str) -> tuple[str, str, str | None]:
    """Separa número, letra complementar e UF de uma inscrição OAB."""
    texto = str(valor or "").upper().strip()
    uf = None
    for candidata in re.findall(r"[A-Z]{2}", texto):
        if candidata in UFS_BRASIL:
            uf = candidata
            break
    sem_prefixo = re.sub(r"\bOAB\b", "", texto).strip(" /-")
    if uf:
        sem_prefixo = re.sub(
            rf"(?:/|\s){re.escape(uf)}\s*$", "", sem_prefixo
        ).strip(" /-")
        sem_prefixo = re.sub(
            rf"^{re.escape(uf)}(?:/|\s)+", "", sem_prefixo
        ).strip(" /-")
    match = re.fullmatch(r"(\d{1,10})\s*[-./]?\s*([A-Z]?)", sem_prefixo)
    if not match:
        return _digitos(texto), "", uf
    return match.group(1), match.group(2), uf


def _dv_cnj(seq: str, ano: str, j: str, tr: str, origem: str) -> str:
    """Digito verificador do CNJ (Resolucao 65/2008): 98 - ((N || '00') mod 97)."""
    resto = int(f"{seq}{ano}{j}{tr}{origem}00") % 97
    return f"{98 - resto:02d}"


def _analisar_cnj(numero_cnj: str) -> dict:
    """Analise PURA de um numero CNJ - nao abre browser, nao toca no PJe.

    Extraido de validar_numero_cnj pra poder barrar numero torto ANTES de
    gastar uma abertura de Chromium, que e' a operacao mais cara do MCP.
    CPF e CNPJ ja eram checados assim; o CNJ, que e' a entrada mais comum,
    passava direto.
    """
    digits = re.sub(r"\D", "", numero_cnj or "")
    if len(digits) not in (19, 20):
        return {
            "valido": False,
            "motivo": f"CNJ deve ter 20 dígitos; encontrados {len(digits)}",
            "digitos_encontrados": len(digits),
        }
    d = digits.zfill(20)
    seq, dd, ano, j, tr, origem = d[:7], d[7:9], d[9:13], d[13:14], d[14:16], d[16:20]
    esperado = _dv_cnj(seq, ano, j, tr, origem)
    return {
        "valido": dd == esperado,
        "motivo": "dígito verificador confere"
        if dd == esperado
        else f"dígito verificador é '{dd}', deveria ser '{esperado}'",
        "numero_formatado": f"{seq}-{dd}.{ano}.{j}.{tr}.{origem}",
        "numero_corrigido": f"{seq}-{esperado}.{ano}.{j}.{tr}.{origem}",
        "digito_informado": dd,
        "digito_esperado": esperado,
        "ano_distribuicao": ano,
        "codigo_tribunal": tr,
        "pertence_ao_tjpa": tr == "14",
    }


async def corrigir_numero_cnj(valor: str) -> dict:
    """[TJPA 1g|2g] Recalcula o digito verificador de um CNJ - SEM tocar no PJe.

    Erro de digitacao em CNJ e' rotina (20 digitos). Em vez de so recusar,
    devolve o numero com o DV correto, pronto pra reusar na busca.
    """
    lista = [x.strip() for x in re.split(r"[,;\n]+", valor or "") if x.strip()]
    if not lista:
        return _exigir(
            "valor",
            "",
            "corrigir_cnj",
            "Informe um ou mais números CNJ separados por vírgula.",
        )

    itens = []
    for bruto in lista[:25]:
        a = _analisar_cnj(bruto)
        item = {"numero_informado": bruto, "valido": a["valido"], "motivo": a["motivo"]}
        if "numero_corrigido" in a:
            item.update(
                numero_formatado=a["numero_formatado"],
                numero_corrigido=a["numero_corrigido"],
                digito_esperado=a["digito_esperado"],
                pertence_ao_tjpa=a["pertence_ao_tjpa"],
                ano_distribuicao=a["ano_distribuicao"],
            )
            if not a["pertence_ao_tjpa"]:
                item["aviso"] = (
                    f"Tribunal {a['codigo_tribunal']} — este MCP atende só o TJPA (14)."
                )
        itens.append(item)

    invalidos = [i for i in itens if not i["valido"]]
    return {
        "total": len(itens),
        "validos": len(itens) - len(invalidos),
        "invalidos": len(invalidos),
        "itens": itens,
        "itens_ignorados": max(0, len(lista) - 25) or None,
        "observacao": (
            "Só o dígito verificador é recalculado — um número com DV correto "
            "pode ainda assim não existir no PJe."
        ),
    }


def _detectar_tipo_busca(valor: str) -> dict:
    """Infere o criterio de busca a partir do formato do valor.

    Retorna {'acao': ..., 'motivo': ..., 'valor_normalizado': ...}. Usado
    pela acao 'auto' e pela busca em lote, pra que o chamador nao precise
    classificar cada dado na mao.
    """
    bruto = str(valor or "").strip()
    d = _digitos(bruto)

    if len(d) in (19, 20):
        return {
            "acao": "consultar_numero",
            "motivo": f"{len(d)} dígitos no formato de número CNJ",
            "valor_normalizado": _normaliza_cnj(bruto),
        }

    if len(d) == 11 and _cpf_valido(d):
        return {
            "acao": "cpf",
            "motivo": "11 dígitos com dígito verificador de CPF válido",
            "valor_normalizado": d,
        }

    if len(d) == 14 and _cnpj_valido(d):
        return {
            "acao": "cnpj",
            "motivo": "14 dígitos com dígito verificador de CNPJ válido",
            "valor_normalizado": d,
        }

    if len(d) in (11, 14) and not re.search(r"[A-Za-z]", bruto):
        return {
            "acao": "busca_geral",
            "motivo": (
                f"{len(d)} dígitos são ambíguos; a busca geral preserva o "
                "identificador e não presume CPF/CNPJ"
            ),
            "valor_normalizado": bruto,
        }

    numero_oab, uf_oab = _parse_oab(bruto)
    if numero_oab and 3 <= len(numero_oab) <= 7 and (uf_oab or "oab" in bruto.lower()):
        return {
            "acao": "oab",
            "motivo": f"número curto com UF ({uf_oab or 'UF não informada'}) — padrão de inscrição OAB",
            "valor_normalizado": bruto,
        }
    if d and not re.search(r"[A-Za-z]", bruto) and 3 <= len(d) <= 7:
        return {
            "acao": "oab",
            "motivo": "somente dígitos, comprimento de inscrição OAB",
            "valor_normalizado": bruto,
        }

    return {
        "acao": "nome_parte",
        "motivo": "texto sem formato de documento — tratado como nome de parte",
        "valor_normalizado": bruto,
    }


# =========================================================================
# EXPEDIENTES E PRAZOS
# =========================================================================


_PROCESSO_INICIADO_EM = datetime.now()


def _git(repo_dir: str, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", repo_dir, *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode != 0:
            return None
        return out.stdout.strip()
    except Exception:
        return None


def _identidade_build() -> dict:
    """Identidade git do código que está DE FATO rodando neste processo.

    Não existe pipeline de build/artefato: o systemd executa este arquivo
    direto da working tree. 'commit exposto' aqui é só o HEAD da árvore no
    disco no momento da chamada — se alguém editou sem reiniciar o serviço,
    isto não reflete o processo em memória, só o que um restart pegaria.
    """
    repo_dir = str(Path(__file__).resolve().parent)
    commit = _git(repo_dir, "rev-parse", "HEAD")
    branch = _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD")
    dirty_raw = _git(repo_dir, "status", "--porcelain")
    upstream = _git(repo_dir, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    ahead_behind = None
    if upstream:
        counts = _git(repo_dir, "rev-list", "--left-right", "--count", f"HEAD...{upstream}")
        if counts and " " in counts:
            ahead_s, behind_s = counts.split()
            ahead_behind = {"ahead": int(ahead_s), "behind": int(behind_s)}
    return {
        "git_commit": commit,
        "git_branch": branch,
        "git_upstream": upstream,
        "working_tree_dirty": bool(dirty_raw),
        "arquivos_nao_commitados": len(dirty_raw.splitlines()) if dirty_raw else 0,
        "ahead_behind_upstream": ahead_behind,
        "processo_iniciado_em": _PROCESSO_INICIADO_EM.isoformat(),
    }


async def status_servidor() -> dict:
    """[TJPA 1g|2g] Diagnóstico de saúde do servidor MCP, credenciais do Keychain e sessão viva do PJe."""
    status_s = cliente_singleton.status_sessao()
    return {
        "servidor": "pje-tjpa",
        "tribunal": "TJPA",
        "instancias": {
            "1g": "https://pje.tjpa.jus.br/pje",
            "2g": "https://pje.tjpa.jus.br/pje-2g",
        },
        "sessao": status_s,
        "build": await asyncio.to_thread(_identidade_build),
        "data_hora": datetime.now().isoformat(),
    }


async def auditoria_mcp_pje() -> dict:
    """[TJPA 1g|2g] Auditoria completa do ambiente: pacotes, credenciais Keychain, espaço em disco e conectividade HTTP."""
    import urllib.request

    # ESSENCIAIS derrubam o servidor se faltarem; os demais degradam uma
    # funcionalidade especifica. A auditoria listava tudo junto e nao usava
    # nada disso no veredito.
    ESSENCIAIS = {"playwright", "pyotp"}
    pacotes = {}
    faltando = []
    for mod in ["playwright", "scrapling", "pdfplumber", "pyotp", "docx", "keyring"]:
        try:
            m = __import__(mod)
            pacotes[mod] = getattr(m, "__version__", "instalado")
        except ImportError:
            pacotes[mod] = "não instalado"
            faltando.append(mod)

    sessao_info = cliente_singleton.status_sessao()
    # Checa os DOIS graus e cria a pasta se faltar: a versao antiga so olhava
    # o 1o grau e, com a pasta ainda inexistente, cravava permissao_escrita
    # False num servidor saudavel.
    armazenamento = pje_downloader.checar_escrita_storage()

    urls_teste = {
        "pje_tjpa_1g": "https://pje.tjpa.jus.br/pje",
        "pje_tjpa_2g": "https://pje.tjpa.jus.br/pje-2g",
        "sso_pdpj": "https://sso.cloud.pje.jus.br",
    }

    def _sondar(url: str) -> dict:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                return {"status_code": resp.status, "acessivel": True}
        except Exception as e:
            return {"status_code": None, "acessivel": False, "erro": str(e)[:100]}

    # urlopen e' bloqueante: em serie, no event loop, sao ate 15s com TODO o
    # servidor travado - inclusive os downloads em background, que rodam neste
    # mesmo loop. to_thread + gather derruba isso pra ~1 RTT, fora do loop.
    resultados = await asyncio.gather(
        *(asyncio.to_thread(_sondar, url) for url in urls_teste.values())
    )
    conectividade = dict(zip(urls_teste, resultados))

    jobs = pje_downloader.listar_jobs()
    alertas = []

    # Sem credencial o MCP nao autentica no PJe e NADA funciona. O veredito
    # 'saudavel' ignorava justamente isso: um servidor sem credencial passava
    # como saudavel porque disco e rede estavam bem.
    if not sessao_info.get("credenciais_keychain"):
        alertas.append(
            "credenciais ausentes (nem systemd-credentials nem keyring) — "
            "nenhuma consulta ao PJe vai funcionar"
        )
    essenciais_faltando = sorted(ESSENCIAIS.intersection(faltando))
    if essenciais_faltando:
        alertas.append(f"pacote essencial ausente: {', '.join(essenciais_faltando)}")
    opcionais_faltando = sorted(set(faltando) - ESSENCIAIS)
    if opcionais_faltando:
        alertas.append(
            f"pacote opcional ausente (funcionalidade degradada): "
            f"{', '.join(opcionais_faltando)}"
        )

    for g, info in armazenamento.items():
        if not info.get("permissao_escrita"):
            alertas.append(f"sem permissão de escrita em {info['pasta']}")
        elif info.get("espaco_livre_gb", 0) < 1:
            alertas.append(f"menos de 1 GB livre em {info['pasta']}")
    for nome, c in conectividade.items():
        if not c["acessivel"]:
            alertas.append(f"{nome} inacessível")
    if jobs["com_erro"]:
        alertas.append(f"{jobs['com_erro']} download(s) com erro")
    recursos = sessao_info.get("recursos_processo", {})
    uso_fds = recursos.get("uso_descritores_percentual")
    if uso_fds is not None and uso_fds >= 80:
        alertas.append(
            f"descritores de arquivo em nível crítico: {uso_fds}% "
            f"({recursos.get('descritores_abertos')}/"
            f"{recursos.get('limite_descritores_soft')})"
        )
    filhos = recursos.get("subprocessos_filhos")
    if filhos is not None and filhos > 8:
        alertas.append(
            f"{filhos} subprocessos diretos no MCP — possível vazamento "
            "de drivers Playwright"
        )

    return {
        "servidor": "pje-tjpa",
        "timestamp": datetime.now().isoformat(),
        "saudavel": not alertas,
        "alertas": alertas or None,
        "ambiente_python": {
            "versao": sys.version.split()[0],
            "executavel": sys.executable,
            "pacotes": pacotes,
        },
        "sessao_e_keychain": sessao_info,
        "armazenamento": armazenamento,
        "conectividade_rede": conectividade,
        "downloads": {
            "em_andamento": jobs["em_andamento"],
            "concluidos": jobs["concluidos"],
            "com_erro": jobs["com_erro"],
        },
    }


_ACOES_AUTOS_DIGITAIS = {
    "listar": "Timeline completa e lista de documentos dos autos.",
    "ler_documento": "Teor textual de uma peça identificada por id_documento.",
    "ler_lote": "Lê várias peças em uma única abertura dos autos.",
    "resumo": "Cabeçalho, últimos movimentos e amostra rápida de documentos.",
}
_ALIAS_AUTOS_DIGITAIS = {
    "cabecalho": "resumo",
    "conteudo": "ler_documento",
    "documentos": "listar",
    "ler": "ler_documento",
    "ler_documentos": "ler_lote",
    "ler_pecas": "ler_lote",
    "ler_varios": "ler_lote",
    "lote": "ler_lote",
    "movimentos": "listar",
    "resumo_rapido": "resumo",
    "teor": "ler_documento",
    "timeline": "listar",
}
_MODOS_RASTREIO_AR = {
    "codigo_ar": "Rastreia diretamente um código AR sem abrir o PJe.",
    "documento": "Extrai e rastreia o AR de um id_documento do processo.",
    "processo": "Varre os expedientes do processo e rastreia todos os códigos AR.",
}
_ACOES_EVIDENCIA = {
    "evidencia": "Captura um screenshot efêmero da sessão ativa do PJe.",
}
_ACOES_CONSULTAR_PROCESSO = {
    "consultar": "Consulta dados básicos de um processo pelo número CNJ.",
}
_ACOES_ANALISAR_AUTOS_LOTE = {
    "lote": "Abre os autos do processo e realiza leitura de múltiplas peças em lote.",
}


async def inventario_capacidades(filtro: str = "") -> dict:
    """[TJPA 1g|2g] Inventario de TODAS as capacidades das ferramentas.

    A consolidacao trocou ~50 tools por 9, o que economiza contexto mas esconde
    o repertorio: pra descobrir uma acao e' preciso ler a descricao de cada
    ferramenta. Aqui vem tudo de uma vez, com os apelidos aceitos.

    - filtro: opcional, restringe por texto no nome da acao ou na descricao
      (ex: 'prazo', 'download', 'minuta').
    """
    # (nome_da_tool, dict_de_capacidades, dict_de_aliases, seletor_publico)
    registro = [
        ("status_e_auditoria_pje", _ACOES_STATUS, _ALIAS_STATUS, "acao"),
        ("painel_e_prazos_pje", _ACOES_PAINEL, _ALIAS_PAINEL, "acao"),
        ("buscar_processos_pje", _ACOES_BUSCAR, _ALIASES_BUSCAR, "acao"),
        ("analisar_processo_pje", _ACOES_ANALISAR, _ALIAS_ANALISAR, "acao"),
        ("gerir_documentos_pje", _ACOES_DOCUMENTOS, _ALIAS_DOCUMENTOS, "acao"),
        ("download_e_cache_pje", _ACOES_DOWNLOAD, _ALIAS_DOWNLOAD, "acao"),
        (
            "producao_minutas_e_relatorios",
            _ACOES_PRODUCAO,
            _ALIAS_PRODUCAO,
            "acao",
        ),
        (
            "auditar_fluxo_processual_pje",
            _ACOES_AUDITORIA_PROCESSUAL,
            _ALIAS_AUDITORIA_PROCESSUAL,
            "acao",
        ),
        (
            "analisar_processo_completo_pje",
            _ACOES_ANALISE_COMPLETA,
            _ALIAS_ANALISE_COMPLETA,
            "acao",
        ),
        (
            "pje_ler_autos_digitais",
            _ACOES_AUTOS_DIGITAIS,
            _ALIAS_AUTOS_DIGITAIS,
            "acao",
        ),
        (
            "pje_rastrear_ar_correios",
            _MODOS_RASTREIO_AR,
            {},
            "numero_cnj | numero_cnj + id_documento | codigo_ar",
        ),
        (
            "atuar_fluxo_tarefas_pje",
            _ACOES_ATUACAO,
            _ALIAS_ATUACAO,
            "acao",
        ),
        (
            "pje_capturar_evidencia",
            _ACOES_EVIDENCIA,
            {},
            "persona",
        ),
        (
            "pje_consultar_processo",
            _ACOES_CONSULTAR_PROCESSO,
            {},
            "numero_processo",
        ),
        (
            "pje_analisar_autos_lote",
            _ACOES_ANALISAR_AUTOS_LOTE,
            {},
            "numero_processo",
        ),
        (
            "automacao_navegador_pje",
            _ACOES_AUTOMACAO_NAVEGADOR,
            {},
            "acao",
        ),
        (
            "gerenciar_etiqueta_processo_pje",
            _ACOES_ETIQUETA_PROCESSO,
            {},
            "acao",
        ),
        (
            "retificar_autuacao_pje",
            _ACOES_RETIFICACAO_AUTUACAO,
            {},
            "acao",
        ),
    ]

    alvo = _slug_acao(filtro) if filtro else ""
    termo = (filtro or "").strip().lower()
    ferramentas = []
    total_acoes = 0

    for nome_tool, acoes, aliases, seletor in registro:
        ferramenta_corresponde = bool(
            termo
            and (
                termo in nome_tool.lower()
                or alvo in _slug_acao(nome_tool)
            )
        )
        itens = []
        for acao, descricao in sorted(acoes.items()):
            # _ACOES_ANALISAR guarda callables como valor; as demais, texto.
            desc = (
                descricao
                if isinstance(descricao, str)
                else (
                    (getattr(descricao, "__doc__", "") or "").strip().split("\n")[0]
                    or "(sem descrição)"
                )
            )
            if (
                termo
                and not ferramenta_corresponde
                and termo not in acao.lower()
                and termo not in desc.lower()
                and alvo not in _slug_acao(acao)
            ):
                continue
            apelidos = sorted(k for k, v in aliases.items() if v == acao)
            item = {"acao": acao, "descricao": desc}
            if apelidos:
                item["apelidos"] = apelidos
            itens.append(item)
        if itens:
            total_acoes += len(itens)
            ferramentas.append(
                {
                    "ferramenta": nome_tool,
                    "total_acoes": len(itens),
                    "seletor": seletor,
                    "acoes": itens,
                }
            )

    res = {
        "total_ferramentas": len(registro),
        "ferramentas_com_resultado": len(ferramentas),
        "total_acoes": total_acoes,
        "filtro": filtro or None,
        "ferramentas": ferramentas,
        "observacao": (
            "Nas ferramentas consolidadas, cada ação é chamada pelo parâmetro "
            "'acao' e aceita os apelidos listados. Ferramentas autônomas "
            "informam em 'seletor' quais parâmetros escolhem o modo de uso."
        ),
    }
    if filtro and not ferramentas:
        res["aviso"] = f"Nenhuma ação corresponde a '{filtro}'."
    return res


async def jobs_em_andamento(incluir_concluidos: bool = True) -> dict:
    """[TJPA 1g|2g] Lista os downloads em background registrados nesta sessao.

    O 'status_download' exige saber o CNJ de antemao. Esta acao responde a
    pergunta operacional oposta - o que o servidor esta fazendo agora - sem
    tocar no browser nem no PJe.
    """
    return pje_downloader.listar_jobs(incluir_concluidos=incluir_concluidos)


async def validar_numero_cnj(numero_cnj: str) -> dict:
    """[TJPA 1g|2g] Valida e analisa a estrutura de um número CNJ (Resolução CNJ 65/2008).

    Verifica dígito verificador (módulo 97), ano de distribuição, ramo da justiça,
    tribunal (TR 14 = TJPA) e comarca de origem.
    """
    digits = re.sub(r"\D", "", numero_cnj)
    if len(digits) not in (19, 20):
        return {
            "valido": False,
            "numero_original": numero_cnj,
            "erro": f"Número CNJ deve conter 20 dígitos (foram encontrados {len(digits)} dígitos).",
        }

    d = digits.zfill(20)
    seq, dd, ano, j, tr, origem = d[:7], d[7:9], d[9:13], d[13:14], d[14:16], d[16:20]
    # Mesma fonte de verdade do guard em buscar_processos_pje: duas
    # implementacoes do DV acabariam divergindo.
    dv_esperado = _dv_cnj(seq, ano, j, tr, origem)
    calc_dd = int(dv_esperado)
    valido = dd == dv_esperado
    formatted = f"{seq}-{dd}.{ano}.{j}.{tr}.{origem}"

    tribunais = {
        "14": "TJPA (Tribunal de Justiça do Pará)",
        "10": "TJMA (Tribunal de Justiça do Maranhão)",
        "18": "TJPI (Tribunal de Justiça do Piauí)",
        "01": "TRF-1 (Tribunal Regional Federal 1ª Região)",
    }

    return {
        "valido": valido,
        "numero_original": numero_cnj,
        "numero_formatado": formatted,
        "digito_verificador_correto": f"{calc_dd:02d}",
        "estrutura": {
            "sequencial": seq,
            "digito_verificador": dd,
            "ano_distribuicao": ano,
            "ramo_justica": "8 - Justiça Estadual"
            if j == "8"
            else f"{j} - Ramo da Justiça",
            "codigo_tribunal": tr,
            "tribunal_nome": tribunais.get(tr, "Outro tribunal/ramo"),
            "comarca_origem": origem,
        },
        "pertence_ao_tjpa": tr == "14",
    }


async def expedientes_pendentes(persona: str = "advogado", grau: str = "1") -> dict:
    """[TJPA 1g|2g] Lista expedientes pendentes de ciencia/resposta."""
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    return _marcar_grau(await pje.expedientes_pendentes(), p, g)


async def listar_processos_tarefa(
    pagina: int = 1,
    itens_por_pagina: int = 50,
    nome_tarefa: str = "",
    termo_busca: str = "",
    filtro_classe: str = "",
    filtro_orgao: str = "",
    filtro_assunto: str = "",
    filtro_parte: str = "",
    filtros_customizados: dict | None = None,
    apenas_com_prazo: bool = False,
    apenas_urgente: bool = False,
    ordenacao: str = "score_prioridade",
    ordenacao_secundaria: str | None = "dias_parado",
    modo_compacto: bool = False,
    apenas_resumo: bool = False,
    exportar_caminho: str | None = None,
    dias_parado_min: int | None = None,
    score_min: float | None = None,
    agrupar_por: str | None = None,
    incluir_metricas_avancadas: bool = True,
    gerar_dashboard_html: str | None = None,
    comparar_com_snapshot: list[dict] | None = None,
    distribuir_por_operadores: int | None = None,
    preset_triagem: str | None = None,
    detectar_anomalias: bool = True,
    calcular_saude: bool = True,
    gerar_relatorio_sintese_final: bool = False,
    persona: str = "advogado",
    grau: str = "1",
) -> dict:
    """[TJPA 1g|2g] Lista, filtra, agrupa, gera síntese e pagina processos da caixa/tarefas no PJe.

    Otimizado para caixas com MILHARES de processos, oferecendo:
    - Paginamento de alto desempenho (1..N páginas)
    - Relatório de Síntese Final Consolidado (gerar_relatorio_sintese_final com diagnóstico executivo de 1 clique)
    - Filtros Customizados Arbitrários (filtros_customizados: dict para correspondência flexível por chave-valor)
    - Exportação Multiformato (CSV, JSON, Markdown .md) para análise off-line em caixas volumosas
    - Índice de Saúde da Caixa (indice_saude_caixa: score 0..100 e classificação EXCELENTE/BOM/ATENCAO/CRITICO)
    - Ordenação Multi-Nível Primária e Secundária (ordenacao_secundaria: 'dias_parado'|'cnj'|'score_prioridade')
    - Filtros Diretos por Assunto e Partes (filtro_assunto, filtro_parte)
    - Detector Automático de Anomalias & Outliers (super parados >180d, atrasos graves >30d)
    - Presets Operacionais de Triagem Rápida (preset_triagem: 'urgencias_vencidas', 'gargalos_antigos', 'resumo_executivo')
    - Plano de Distribuição Equitativa de Carga (distribuir_por_operadores para N pessoas/equipes)
    - Assinatura de Estado & Cálculo de Delta Diferencial (hash_caixa e comparar_com_snapshot para ver novos/resolvidos)
    - Matriz de Priorização Dinâmica (score_prioridade 0..100: Crítico, Alto, Médio, Baixo)
    - Agrupamento Dinâmico por atribuição (agrupar_por: 'tarefa', 'classe', 'orgao', 'nivel_urgencia')
    - Métricas Avançadas de Retenção (média, percentil P90 e identificação de gargalos)
    - Motor de Recomendações e Ações Sugeridas (sugestoes_acao para triagem rápida)
    - Gerador de Dashboard HTML Interativo (gerar_dashboard_html para visualização executiva)
    - Filtros por tempo parado na tarefa (dias_parado_min) e score mínimo (score_min)
    - Modo compacto (economiza tokens) e Resumo estatístico consolidado (apenas_resumo=True)
    - Exportação direta para CSV/JSON no disco via exportar_caminho
    """
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    res = await pje.listar_processos_tarefa(
        pagina=pagina,
        itens_por_pagina=itens_por_pagina,
        nome_tarefa=nome_tarefa,
        termo_busca=termo_busca,
        filtro_classe=filtro_classe,
        filtro_orgao=filtro_orgao,
        filtro_assunto=filtro_assunto,
        filtro_parte=filtro_parte,
        filtros_customizados=filtros_customizados,
        apenas_com_prazo=apenas_com_prazo,
        apenas_urgente=apenas_urgente,
        ordenacao=ordenacao,
        ordenacao_secundaria=ordenacao_secundaria,
        modo_compacto=modo_compacto,
        apenas_resumo=apenas_resumo,
        exportar_caminho=exportar_caminho,
        dias_parado_min=dias_parado_min,
        score_min=score_min,
        agrupar_por=agrupar_por,
        incluir_metricas_avancadas=incluir_metricas_avancadas,
        gerar_dashboard_html=gerar_dashboard_html,
        comparar_com_snapshot=comparar_com_snapshot,
        distribuir_por_operadores=distribuir_por_operadores,
        preset_triagem=preset_triagem,
        detectar_anomalias=detectar_anomalias,
        calcular_saude=calcular_saude,
        gerar_relatorio_sintese_final=gerar_relatorio_sintese_final,
    )
    return _marcar_grau(res, p, g)


async def diagnosticar_caixas_tarefas(
    persona: str = "servidor",
    grau: str = "1",
    diagnostico_profundo: bool = False,
) -> dict:
    """[TJPA 1g|2g] Descobre o painel de caixas/tarefas do perfil ativo."""
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g, permitir_sem_perfil=True)
    bruto = await pje.diagnosticar_painel_tarefas(
        profundo=diagnostico_profundo
    )
    perfis = []
    perfis_catalogo = []
    for item in bruto.get("perfis_disponiveis", []):
        rotulo = str(item.get("texto", "")).strip()
        # Nome isolado do usuário e ação "Sair" não são perfis funcionais.
        # Perfis internos válidos sempre identificam ao menos unidade/papel.
        if (
            not rotulo
            or rotulo.casefold() == "sair"
            or "/" not in rotulo
        ):
            continue
        decomposicao = perfil_contexto.decompor_rotulo(rotulo)
        pje_id = str(item.get("pje_id") or "").strip()
        perfil_real = perfil_contexto.PerfilFuncional(
            pje_id=pje_id,
            rotulo=rotulo,
            unidade_id=str(item.get("unidade_id") or ""),
            localizacao_id=str(item.get("localizacao_id") or ""),
            papel_id=str(item.get("papel_id") or ""),
            unidade=decomposicao["unidade"],
            localizacao=decomposicao["localizacao"],
            papel=decomposicao["papel"],
            persona=p,
            grau=g,
            fonte_id=str(item.get("fonte_id") or ""),
        )
        if pje_id:
            perfis_catalogo.append(perfil_real)
        perfis.append(
            {
                "pje_id": pje_id or None,
                "rotulo": rotulo,
                "unidade_id": perfil_real.unidade_id or None,
                "localizacao_id": perfil_real.localizacao_id or None,
                "papel_id": perfil_real.papel_id or None,
                "unidade": perfil_real.unidade,
                "localizacao": perfil_real.localizacao,
                "papel": perfil_real.papel,
                "fonte_id": perfil_real.fonte_id or None,
                "parametro_perfil": f"pje_id:{pje_id}" if pje_id else None,
            }
        )
    perfil_contexto.registrar_perfis_funcionais(p, g, perfis_catalogo)
    # Alguns temas do PJe colocam no primeiro ``a`` o nome do usuário junto
    # do primeiro perfil. Como o perfil verdadeiro também aparece em item
    # próprio, remove o ancestral concatenado por comparação de sufixo.
    rotulos_normalizados = [
        perfil_contexto.normalizar_texto(item["rotulo"]) for item in perfis
    ]
    perfis = [
        item
        for indice, item in enumerate(perfis)
        if not any(
            indice != outro_indice
            and rotulos_normalizados[indice].endswith(outro_rotulo)
            and rotulos_normalizados[indice] != outro_rotulo
            for outro_indice, outro_rotulo in enumerate(rotulos_normalizados)
        )
    ]
    resultado = {
        "status": "ok" if len(perfis_catalogo) == len(perfis) else "bloqueado",
        "codigo": (
            None
            if len(perfis_catalogo) == len(perfis)
            else "PERFIL_SEM_IDENTIFICADOR_ESTAVEL"
        ),
        "modo": "profundo" if diagnostico_profundo else "leve",
        "perfis_funcionais": perfis,
        "quantidade_perfis": len(perfis),
        "selecao_obrigatoria": (
            p in perfil_contexto.PERSONAS_INTERNAS and len(perfis) > 0
        ),
        "instrucao": (
            "Antes de consultar dados internos, pergunte ao usuário qual "
            "perfil funcional deve ser usado e repita a chamada com "
            "parametro perfil."
        ),
        "somente_leitura": True,
    }
    if resultado["codigo"] == "PERFIL_SEM_IDENTIFICADOR_ESTAVEL":
        resultado["evidencias_identificador"] = [
            item.get("evidencia_id")
            for item in bruto.get("perfis_disponiveis", [])
            if item.get("evidencia_id")
        ]
    if diagnostico_profundo:
        quadros = bruto.get("quadros") or []
        resultado["diagnostico_estrutural"] = {
            "corpo_tem_tarefas": bool(bruto.get("corpo_tem_tarefas")),
            "corpo_tem_caixas": bool(bruto.get("corpo_tem_caixas")),
            "total_controles": len(bruto.get("controles") or []),
            "total_iframes": len(bruto.get("iframes") or []),
            "total_quadros": len(quadros),
            "quadros": [
                {
                    "total_links": int(item.get("total_links") or 0),
                    "total_botoes": int(item.get("total_botoes") or 0),
                    "total_testes_api_processos": len(
                        item.get("testes_api_processos") or []
                    ),
                    "status_testes_api_processos": [
                        teste.get("status")
                        for teste in item.get("testes_api_processos") or []
                    ],
                }
                for item in quadros
            ],
        }
    return _marcar_grau(resultado, p, g)


async def sincronizar_inventario_caixas(
    persona: str = "advogado",
    grau: str = "1",
    concorrencia: int = 4,
    max_retentativas: int = 3,
    lotacao: str = "",
    modo: str = "incremental",
) -> dict:
    """Coleta todas as caixas/processos e produz snapshot com prova de cobertura."""
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    res = await pje.sincronizar_caixas_tarefas(
        concorrencia=concorrencia,
        max_retentativas=max_retentativas,
        lotacao=lotacao,
        modo=modo,
    )
    return _marcar_grau(res, p, g)


async def listar_inventario_caixas(
    persona: str = "advogado",
    grau: str = "1",
    snapshot_id: str = "",
) -> dict:
    """Lê o último snapshot do disco sem abrir navegador ou PJe."""
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    res = await asyncio.to_thread(
        caixas_tarefas.obter_snapshot,
        snapshot_id or None,
        g,
        p,
        True,
    )
    return _marcar_grau(res, p, g)


async def consultar_inventario_processos(
    persona: str = "advogado",
    grau: str = "1",
    snapshot_id: str = "",
    nome_tarefa: str = "",
    termo_busca: str = "",
    pagina: int = 1,
    itens_por_pagina: int = 50,
    incluir_metadados_origem: bool = False,
) -> dict:
    """Pesquisa paginada no catálogo; o JSON integral é opcional."""
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    res = await asyncio.to_thread(
        caixas_tarefas.consultar_ocorrencias,
        snapshot_id or None,
        g,
        p,
        nome_tarefa,
        termo_busca,
        pagina,
        itens_por_pagina,
        incluir_metadados_origem,
    )
    return _marcar_grau(res, p, g)


async def consultar_acervo_tarefas_estruturado(
    persona: str = "advogado",
    grau: str = "1",
    snapshot_id: str = "",
    nome_tarefa: str = "",
    termo_busca: str = "",
    filtro_classe: str = "",
    filtro_assunto: str = "",
    filtro_parte: str = "",
    filtro_orgao: str = "",
    filtro_etiqueta: str = "",
    sigiloso: bool | None = None,
    prioridade: bool | None = None,
    conferido: bool | None = None,
    morador_de_rua: bool | None = None,
    data_chegada_de: str = "",
    data_chegada_ate: str = "",
    dias_na_tarefa_min: int | None = None,
    dias_na_tarefa_max: int | None = None,
    pagina: int = 1,
    itens_por_pagina: int = 50,
    ordenar_por: str = "data_chegada",
    direcao: str = "asc",
    incluir_facetas: bool = True,
    incluir_campos_extras: bool = True,
    incluir_metadados_origem: bool = False,
    formato: str = "completo",
    campos: str = "",
    envelope: str = "completo",
    if_revision: str = "",
    cursor: str = "",
) -> dict:
    """Consulta semântica versionada, pronta para filtros e interfaces."""
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    res = await asyncio.to_thread(
        caixas_tarefas.consultar_acervo_estruturado,
        snapshot_id or None,
        g,
        p,
        nome_tarefa,
        termo_busca,
        filtro_classe,
        filtro_assunto,
        filtro_parte,
        filtro_orgao,
        filtro_etiqueta,
        sigiloso,
        prioridade,
        conferido,
        morador_de_rua,
        data_chegada_de,
        data_chegada_ate,
        dias_na_tarefa_min,
        dias_na_tarefa_max,
        pagina,
        itens_por_pagina,
        ordenar_por,
        direcao,
        incluir_facetas,
        incluir_campos_extras,
        incluir_metadados_origem,
        formato,
        campos,
        envelope,
        if_revision,
        cursor,
    )
    return _marcar_grau(res, p, g)


async def exportar_acervo_tarefas(
    persona: str = "advogado",
    grau: str = "1",
    snapshot_id: str = "",
    formato_arquivo: str = "ndjson",
    modo: str = "compacto",
    autorizacao_ref: str = "",
    **filtros: Any,
) -> dict:
    """Gera export confidencial comprimido e retorna seu manifesto."""
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    filtros.update({"persona": p, "grau": g})
    res = await asyncio.to_thread(
        caixas_tarefas.exportar_acervo,
        formato_arquivo,
        modo,
        snapshot_id,
        autorizacao_ref,
        **filtros,
    )
    return _marcar_grau(res, p, g)


async def estatisticas_acervo_tarefas(
    persona: str = "advogado",
    grau: str = "1",
    snapshot_id: str = "",
    dimensao: str = "tarefa",
    metricas: str = (
        "quantidade,mediana_dias,p90_dias,max_dias,prioritarios,"
        "sigilosos,maiores_180,maiores_365"
    ),
    **filtros: Any,
) -> dict:
    """Agrega o acervo no servidor e retorna uma linha por dimensão."""
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    filtros.update({"persona": p, "grau": g})
    res = await asyncio.to_thread(
        caixas_tarefas.estatisticas_acervo,
        dimensao,
        metricas,
        snapshot_id,
        **filtros,
    )
    return _marcar_grau(res, p, g)


async def _executar_analise_processual_completa(job_id: str) -> None:
    """Executa um job persistente sem bloquear as ações status/resultado."""
    try:
        job = await asyncio.to_thread(
            analise_processual_completa.get_job,
            job_id,
        )
        identity = await asyncio.to_thread(
            analise_processual_completa.identity_for_job,
            job_id,
        )
        numero_cnj = str(identity["process_number"])
        mode = str(job.get("mode") or "integral")
        previous_snapshot = await asyncio.to_thread(
            analise_processual_completa.find_latest_available_result,
            numero_cnj,
            str(job["grau"]),
            exclude_job_id=job_id,
        )
        previous_manifest = list(
            ((previous_snapshot or {}).get("dossier") or {}).get("manifest") or []
        )
        await asyncio.to_thread(
            analise_processual_completa.reset_document_states,
            job_id,
        )
        await asyncio.to_thread(
            analise_processual_completa.update_job,
            job_id,
            status="running",
            phase="manifest",
            cancel_requested=0,
            processed_documents=0,
            failed_documents=0,
            reused_documents=0,
            pages_discovered=0,
            pages_without_text=0,
            current_document_id=None,
            error=None,
        )
        pje = await cliente_singleton.get_cliente(
            str(job["persona"]),
            str(job["grau"]),
        )
        manifest: list[dict[str, Any]] = []
        processed_documents: list[dict[str, Any]] = []
        cache_plan: dict[str, dict[str, Any]] = {}
        priority_plan: list[dict[str, Any]] = []
        incremental_delta: dict[str, Any] = {}
        hash_owner: dict[str, str] = {}
        counters = {
            "processed": 0,
            "failed": 0,
            "reused": 0,
            "pages": 0,
            "pages_without_text": 0,
        }

        async def on_manifest(base, documents, tree_complete):
            del base, tree_complete
            manifest.clear()
            cache_plan.clear()
            priority_plan.clear()
            for order, document in enumerate(documents, start=1):
                manifest_entry = (
                    analise_processual_completa.build_manifest_entry(
                        document,
                        canonical_order=order,
                    )
                )
                fingerprint = str(manifest_entry["source_fingerprint"])
                manifest.append(manifest_entry)
            incremental_delta.clear()
            incremental_delta.update(
                analise_processual_completa.compare_manifests(
                    previous_manifest,
                    manifest,
                )
            )
            if previous_snapshot:
                incremental_delta["baseline_job_id"] = previous_snapshot["job"][
                    "job_id"
                ]
            if mode == "rapida":
                priority_plan.extend(
                    analise_processual_completa.prioritise_documents(documents)
                )
            elif mode == "integral":
                priority_plan.extend(dict(item) for item in documents)
            selected_ids = {
                str(document.get("id") or "") for document in priority_plan
            }
            for document, manifest_entry in zip(documents, manifest):
                if str(document.get("id") or "") not in selected_ids:
                    continue
                fingerprint = str(manifest_entry["source_fingerprint"])
                if job["force_reread"]:
                    continue
                cached = await asyncio.to_thread(
                    auditoria_processual.get_cached_document,
                    numero_cnj,
                    str(document.get("id") or ""),
                    fingerprint,
                )
                if cached is not None:
                    cache_plan[str(document.get("id") or "")] = cached
            manifest_sha = hashlib.sha256(
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            await asyncio.to_thread(
                analise_processual_completa.update_job,
                job_id,
                phase="acquisition",
                total_documents=len(priority_plan),
                manifest_sha256=manifest_sha,
            )
            return cache_plan

        async def on_document(document, cached, response, source):
            document_id = str(document.get("id") or "")
            fingerprint = auditoria_processual.build_document_fingerprint(document)
            await asyncio.to_thread(
                analise_processual_completa.update_job,
                job_id,
                current_document_id=document_id,
                phase="extraction",
            )
            if source == "cache":
                payload = dict(cached or {})
                counters["reused"] += 1
                reused = True
            elif response and not response.get("erro"):
                payload = analise_processual_completa.normalise_document_payload(
                    document,
                    response,
                )
                await asyncio.to_thread(
                    auditoria_processual.save_cached_document,
                    numero_cnj,
                    document_id,
                    fingerprint,
                    payload["content_sha256"],
                    payload,
                )
                reused = False
            else:
                counters["failed"] += 1
                counters["processed"] += 1
                await asyncio.to_thread(
                    analise_processual_completa.save_document_state,
                    job_id,
                    document_id=document_id,
                    source_fingerprint=fingerprint,
                    status="failed",
                    safe_error="peça não pôde ser lida",
                )
                await asyncio.to_thread(
                    analise_processual_completa.update_job,
                    job_id,
                    processed_documents=counters["processed"],
                    failed_documents=counters["failed"],
                    reused_documents=counters["reused"],
                )
                return

            content_hash = str(payload.get("content_sha256") or "")
            pages = list(payload.get("pages") or [])
            pages_without_text = int(payload.get("pages_without_text") or 0)
            counters["pages"] += len(pages)
            counters["pages_without_text"] += pages_without_text
            duplicate_of = hash_owner.get(content_hash, "")
            if not duplicate_of:
                hash_owner[content_hash] = document_id
                processed_documents.append(payload)
            counters["processed"] += 1
            await asyncio.to_thread(
                analise_processual_completa.save_document_state,
                job_id,
                document_id=document_id,
                source_fingerprint=fingerprint,
                status=(
                    "duplicate"
                    if duplicate_of
                    else ("reused" if reused else "completed")
                ),
                content_sha256=content_hash,
                duplicate_of=duplicate_of,
                pages=len(pages),
                pages_without_text=pages_without_text,
                truncated=bool(payload.get("truncated")),
                reused=reused,
            )
            await asyncio.to_thread(
                analise_processual_completa.update_job,
                job_id,
                processed_documents=counters["processed"],
                failed_documents=counters["failed"],
                reused_documents=counters["reused"],
                pages_discovered=counters["pages"],
                pages_without_text=counters["pages_without_text"],
            )

        async def should_cancel():
            return await asyncio.to_thread(
                analise_processual_completa.cancellation_requested,
                job_id,
            )

        selector = None
        max_pages = None
        concurrency = 4
        collect_expedients = True
        if mode == "inventario":
            def select_no_documents(_documents):
                return []

            selector = select_no_documents
            concurrency = 1
            collect_expedients = False
        elif mode == "rapida":
            selector = analise_processual_completa.prioritise_documents
            max_pages = analise_processual_completa.RAPID_MAX_PAGES_PER_DOCUMENT
            concurrency = analise_processual_completa.RAPID_CONCURRENCY

        collected = await pje.coletar_processo_integral(
            numero_cnj,
            on_manifest,
            on_document,
            should_cancel,
            max_paginas=max_pages,
            document_selector=selector,
            concurrency=concurrency,
            collect_expedients=collect_expedients,
            parallel_process=await asyncio.to_thread(
                batch_engine.job_belongs_to_parallel_batch,
                job_id,
            ),
        )
        await asyncio.to_thread(
            analise_processual_completa.update_job,
            job_id,
            phase="facts",
            current_document_id=None,
        )
        states = await asyncio.to_thread(
            analise_processual_completa.document_states,
            job_id,
        )
        states_by_id = {str(item["document_id"]): item for item in states}
        manifest_with_coverage = [
            {
                **item,
                "status": states_by_id.get(str(item["document_id"]), {}).get(
                    "status", "not_attempted"
                ),
                "content_sha256": states_by_id.get(str(item["document_id"]), {}).get(
                    "content_sha256"
                ),
                "pages": int(
                    states_by_id.get(str(item["document_id"]), {}).get("pages") or 0
                ),
                "pages_without_text": int(
                    states_by_id.get(str(item["document_id"]), {}).get(
                        "pages_without_text"
                    )
                    or 0
                ),
                "duplicate_of": states_by_id.get(str(item["document_id"]), {}).get(
                    "duplicate_of"
                ),
            }
            for item in manifest
        ]
        if mode == "inventario":
            dossier = await asyncio.to_thread(
                analise_processual_completa.build_inventory_dossier,
                process_number=numero_cnj,
                base=collected["base"],
                manifest_value=manifest_with_coverage,
                tree_complete=bool(collected["tree_complete"]),
                incremental=incremental_delta,
                collected_at=datetime.now().astimezone().isoformat(),
            )
            await asyncio.to_thread(
                analise_processual_completa.update_job,
                job_id,
                phase="synthesis",
            )
            await asyncio.to_thread(
                analise_processual_completa.save_result,
                job_id,
                dossier,
            )
            await asyncio.to_thread(
                analise_processual_completa.update_job,
                job_id,
                status="completed",
                phase="completed",
                total_documents=len(manifest),
                current_document_id=None,
            )
            return
        dossier = await asyncio.to_thread(
            analise_processual_completa.build_dossier,
            process_number=numero_cnj,
            base=collected["base"],
            manifest=manifest_with_coverage,
            documents=processed_documents,
            document_states_value=states,
            tree_complete=bool(collected["tree_complete"]),
            expedients=list(collected.get("expedients") or []),
            collected_at=datetime.now().astimezone().isoformat(),
            expedients_complete=bool(collected.get("expedients_complete")),
            expedients_source=str(
                collected.get("expedients_source") or "aba_expedientes"
            ),
            expedients_safe_error=collected.get("expedients_safe_error"),
            manifest_reconciled=bool(collected.get("manifest_reconciled")),
            manifest_delta=collected.get("manifest_delta"),
            # O manifesto atual deriva apenas da árvore DOM. Até o cruzamento
            # estruturado v2, vínculos e metadados permanecem lacuna explícita.
            metadata_complete=False,
        )
        dossier["analysis_level"] = "rapid" if mode == "rapida" else "integral"
        dossier["incremental"] = {
            **incremental_delta,
            "documents_reused": counters["reused"],
        }
        dossier["selection"] = {
            "strategy": "legal_priority_then_recency" if mode == "rapida" else "all",
            "documents_selected": len(priority_plan),
            "documents_skipped": int(collected.get("documents_skipped") or 0),
            "max_pages_per_document": max_pages,
            "concurrency": concurrency,
            "priorities": [
                {
                    "document_id": str(item.get("id") or ""),
                    "type": item.get("type") or item.get("tipo"),
                    "title": item.get("title") or item.get("titulo"),
                    "priority": item.get("analysis_priority"),
                }
                for item in priority_plan
            ],
        }
        dossier["next_recommended_action"] = (
            "aprofundar" if mode == "rapida" else "consultar_resultado"
        )
        if collected.get("cancelled"):
            dossier.setdefault("gaps", []).append(
                "job cancelado antes de processar todas as peças"
            )
            dossier.setdefault("gap_details", []).append(
                {
                    "code": "JOB_CANCELLED",
                    "dimension": "documents",
                    "severity": "high",
                    "message": (
                        "job cancelado antes de processar todas as peças"
                    ),
                }
            )
            dossier["coverage"]["complete"] = False
            dossier["coverage"]["status"] = "partial_with_gaps"
            dossier["completeness"]["documents"]["status"] = "partial"
        await asyncio.to_thread(
            analise_processual_completa.update_job,
            job_id,
            phase="synthesis",
        )
        await asyncio.to_thread(
            analise_processual_completa.save_result,
            job_id,
            dossier,
        )
        if collected.get("cancelled"):
            final_status = "cancelled"
        elif mode == "rapida":
            # Cobertura parcial é deliberada nessa modalidade e não representa
            # falha operacional do job.
            final_status = "completed"
        elif dossier["coverage"]["complete"]:
            final_status = "completed"
        else:
            final_status = "partial_with_gaps"
        await asyncio.to_thread(
            analise_processual_completa.update_job,
            job_id,
            status=final_status,
            phase="completed",
            current_document_id=None,
        )
    except asyncio.CancelledError:
        await asyncio.to_thread(
            analise_processual_completa.update_job,
            job_id,
            status="queued",
            error="serviço interrompeu a execução; retomada automática pendente",
        )
        raise
    except Exception:
        await asyncio.to_thread(
            analise_processual_completa.update_job,
            job_id,
            status="failed",
            error="falha segura no job; consulte os logs protegidos",
        )
    finally:
        _complete_analysis_tasks.pop(job_id, None)


def _agendar_analise_completa(job_id: str) -> bool:
    current = _complete_analysis_tasks.get(job_id)
    if current and not current.done():
        return False
    task = asyncio.create_task(_executar_analise_processual_completa(job_id))
    _complete_analysis_tasks[job_id] = task
    return True


async def _executar_agente_vertex(run_id: str) -> None:
    try:
        await asyncio.to_thread(vertex_process_agent.execute_run, run_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        # O módulo já persiste uma mensagem pública segura; detalhes ficam
        # apenas nos logs protegidos do serviço.
        pass
    finally:
        _vertex_agent_tasks.pop(run_id, None)


def _agendar_agente_vertex(run_id: str) -> bool:
    current = _vertex_agent_tasks.get(run_id)
    if current and not current.done():
        return False
    task = asyncio.create_task(_executar_agente_vertex(run_id))
    _vertex_agent_tasks[run_id] = task
    return True


async def _retomar_agentes_vertex() -> None:
    await asyncio.sleep(0)
    try:
        runs = await asyncio.to_thread(vertex_process_agent.list_resumable_runs)
    except Exception:
        return
    for run in runs:
        _agendar_agente_vertex(str(run["run_id"]))


async def _retomar_analises_completas() -> None:
    """Retoma jobs persistidos após reinício do serviço."""
    await asyncio.sleep(0)
    try:
        jobs = await asyncio.to_thread(analise_processual_completa.list_resumable_jobs)
    except Exception:
        return
    for job in jobs:
        if job["status"] in {"queued", "running", "cancel_requested"}:
            _agendar_analise_completa(str(job["job_id"]))


async def _executar_pdf_integral_efemero(
    job_id: str,
    *,
    capsule: analysis_capsule.Capsule,
    numero_cnj: str,
    persona: str,
    grau: str,
) -> None:
    """Baixa e processa sem persistir número CNJ no controle do job."""
    job = _ephemeral_pdf_jobs[job_id]
    job["status"] = "running"
    try:
        client = await cliente_singleton.get_cliente(persona, grau)
        listing = await client.listar_documentos(numero_cnj)
        documents = list(listing.get("documentos") or [])
        if listing.get("arvore_completa") is not True or not documents:
            raise consolidated_process.ConsolidatedProcessError(
                "árvore documental incompleta"
            )
        destination = capsule.file("source/process-original.pdf")
        await client.baixar_processo_nativo(
            numero_cnj=numero_cnj,
            caminho_destino=destination,
            cronologia="crescente",
            incluir_expediente=False,
            incluir_movimentos=False,
        )
        manifest = await asyncio.to_thread(
            consolidated_process.process_consolidated_pdf,
            destination,
            capsule,
            known_documents=documents,
        )
        shards = await asyncio.to_thread(
            consolidated_process.build_text_shards,
            manifest,
        )
        shard_plan = capsule.file("shards.json")
        shard_plan.write_text(
            json.dumps(shards, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.chmod(shard_plan, 0o600)
        job.update(
            status="completed",
            page_count=manifest["page_count"],
            document_count=len(manifest["documents"]),
            shard_count=len(shards),
            complete=True,
        )
    except asyncio.CancelledError:
        job.update(status="failed", safe_error="serviço interrompeu a preparação")
        raise
    except Exception:
        job.update(
            status="failed",
            safe_error="preparação integral falhou em modo fechado",
            complete=False,
        )
    finally:
        _ephemeral_pdf_tasks.pop(job_id, None)


def _public_ephemeral_pdf_job(job_id: str) -> dict[str, Any]:
    job = _ephemeral_pdf_jobs.get(str(job_id or ""))
    if not job:
        raise analysis_capsule.CapsuleError("job efêmero não encontrado")
    return {**job, "read_only": True, "process_data_persisted": False}


def _public_ephemeral_agent_run(run_id: str) -> dict[str, Any]:
    run = _ephemeral_agent_runs.get(str(run_id or ""))
    if not run:
        raise analysis_capsule.CapsuleError("execução efêmera do agente não encontrada")
    return {**run, "read_only": True, "process_data_persisted": False}


def _emitir_recibo_entrega(job_id: str) -> dict[str, Any]:
    """Recibo opaco de entrega: em produção, o descarte exige este token.

    O token prova que o solicitante recebeu a entrega — a confirmação humana
    permanece inequívoca. Nada no recibo identifica o processo.
    """
    job = _ephemeral_pdf_jobs[str(job_id)]
    token = job.setdefault("discard_token", uuid.uuid4().hex)
    return {
        "job_id": str(job_id),
        "capsule_id": str(job["capsule_id"]),
        "mode": str(job["mode"]),
        "discard_token": token,
        "instrucao": (
            "após conferir a entrega, confirme o descarte chamando "
            "confirmar_descarte com este job_id e confirmation_token="
            "discard_token; em produção o descarte não ocorre sem o token"
        ),
    }


def _descartar_agente_efemero_do_job(job_id: str) -> None:
    """Remove do registro os runs cuja cápsula acabou de ser apagada."""
    for run_id, run in list(_ephemeral_agent_runs.items()):
        if str(run.get("source_job_id")) == str(job_id):
            task = _ephemeral_agent_tasks.pop(run_id, None)
            if task and not task.done():
                task.cancel()
            _ephemeral_agent_runs.pop(run_id, None)


async def _executar_agente_efemero(run_id: str) -> None:
    """Roda o orquestrador sobre a cápsula; o resultado só existe dentro dela."""
    run = _ephemeral_agent_runs[run_id]
    run["status"] = "running"
    try:
        capsule = await asyncio.to_thread(
            analysis_capsule.open_capsule,
            str(run["capsule_id"]),
            mode=str(run["mode"]),
        )
        manifest = json.loads(
            capsule.file("manifest.json").read_text(encoding="utf-8")
        )
        shards = json.loads(capsule.file("shards.json").read_text(encoding="utf-8"))
        result = await asyncio.to_thread(
            vertex_process_agent.run_ephemeral_manifest,
            manifest,
            shards,
            model=str(run["model"]),
        )
        raw = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        result_path = capsule.file("agent/result.json")
        result_path.write_text(raw, encoding="utf-8")
        os.chmod(result_path, 0o600)
        verification = result.get("verification") or {}
        run.update(
            status="completed",
            result_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            verified_findings=verification.get("verified_findings"),
            rejected_findings=verification.get("rejected_findings"),
            shards_used=(result.get("input_coverage") or {}).get("shards_used"),
        )
    except asyncio.CancelledError:
        run.update(status="failed", safe_error="serviço interrompeu a análise efêmera")
        raise
    except Exception:
        run.update(
            status="failed",
            safe_error="falha segura no agente efêmero; consulte logs protegidos",
        )
    finally:
        _ephemeral_agent_tasks.pop(run_id, None)


def _resultado_agente_efemero(run_id: str) -> dict[str, Any]:
    run = _public_ephemeral_agent_run(run_id)
    if run.get("status") != "completed":
        return {**run, "available": False}
    capsule = analysis_capsule.open_capsule(
        str(run["capsule_id"]),
        mode=str(run["mode"]),
    )
    result = json.loads(capsule.file("agent/result.json").read_text(encoding="utf-8"))
    relatorio = report_format.build_standard_report(result)
    payload = {
        **run,
        "available": True,
        "result": result,
        "relatorio": relatorio,
        "relatorio_markdown": report_format.render_markdown(relatorio),
    }
    source_job_id = str(run.get("source_job_id") or "")
    if source_job_id in _ephemeral_pdf_jobs:
        payload["delivery_receipt"] = _emitir_recibo_entrega(source_job_id)
    return payload


_ACOES_ANALISE_COMPLETA = {
    "planejar": "Estima cobertura e cache sem abrir os autos.",
    "inventariar": "Mapeia capa, movimentos e árvore sem ler o teor das peças.",
    "iniciar_rapida": "Lê seletivamente até 12 peças jurídicas prioritárias.",
    "aprofundar": "Completa a leitura integral reaproveitando o cache anterior.",
    "iniciar": "Cria ou retoma job integral somente leitura.",
    "status": "Consulta progresso persistido sem abrir o navegador.",
    "resultado": "Entrega dossiê completo ou parcial com cobertura.",
    "explicar": "Detalha conclusão e citações por peça, página e hash.",
    "cancelar": "Interrompe localmente o job, que permanece retomável.",
    "reanalisar": "Reaplica extratores ao cache sem baixar novamente.",
    "analisar_varios": "Cria e inicia lote paralelo em uma única chamada.",
    "criar_lote": "Cria fila persistente com modo e concorrência definidos.",
    "adicionar_ao_lote": "Adiciona processos a uma fila existente.",
    "iniciar_lote": "Inicia o preenchimento contínuo das vagas do lote.",
    "status_lote": "Mostra processos ativos, fila e vagas disponíveis.",
    "resultado_lote": "Agrega os dossiês já concluídos.",
    "cancelar_lote": "Cancela itens ativos e ainda enfileirados.",
    "retomar_lote": "Recoloca falhas e cancelamentos na fila.",
    "iniciar_agente": "Analisa um dossiê pronto com Gemini 3.5 no Vertex AI.",
    "status_agente": "Consulta o estado da execução sem expor o resultado.",
    "resultado_agente": "Entrega somente conclusões com provas verificadas localmente.",
    "explicar_agente": "Entrega uma conclusão e seus trechos probatórios.",
    "preparar_pdf_integral": "Baixa PDF nativo numa cápsula efêmera e processa OCR local.",
    "status_pdf_integral": "Consulta o preparo efêmero sem expor dados do processo.",
    "resultado_pdf_integral": "Entrega manifesto integral após o portão de cobertura.",
    "confirmar_descarte": "Apaga e verifica a ausência de todos os dados da cápsula.",
    "registrar_resultado_teste": "Apaga imediatamente a cápsula quando o teste passa.",
}
_ALIAS_ANALISE_COMPLETA = {
    "planejar_analise": "planejar",
    "iniciar_analise": "iniciar",
    "status_analise": "status",
    "obter_resultado": "resultado",
    "explicar_conclusao": "explicar",
    "parar": "cancelar",
    "reprocessar": "reanalisar",
    "inventario": "inventariar",
    "mapear_pecas": "inventariar",
    "rapida": "iniciar_rapida",
    "analisar_rapido": "iniciar_rapida",
    "triagem_rapida": "iniciar_rapida",
    "aprofundar_analise": "aprofundar",
    "integral": "aprofundar",
    "analisar_em_paralelo": "analisar_varios",
    "analisar_processos": "analisar_varios",
    "agente": "iniciar_agente",
    "analisar_com_gemini": "iniciar_agente",
    "resultado_gemini": "resultado_agente",
}


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
    structured_output=True,
)
@auditar_ferramenta
async def analisar_processo_completo_pje(
    acao: str,
    numero_cnj: str = "",
    job_id: str = "",
    autorizacao_leitura: bool = False,
    autorizacao_ref: str = "",
    modo: str = "integral",
    forcar_releitura: bool = False,
    analise_semantica: bool = False,
    retomar: bool = True,
    topico: str = "",
    finding_id: str = "",
    persona: str = "servidor",
    grau: str = "1",
    perfil: str = "",
    confirmar_consulta: str = "",
    confirmation_token: str = "",
    capturar_screenshot: bool = False,
    concorrencia_processos: int = 2,
    agent_run_id: str = "",
    modelo_agente: str = "gemini-3.5-flash",
    modo_dados: str = "production",
    teste_aprovado: bool = False,
) -> Dict[str, Any]:
    """Análise integral, persistente, citável e somente leitura de um processo.

    acao: 'planejar' | 'inventariar' | 'iniciar_rapida' | 'aprofundar'
        | 'iniciar' | 'status' | 'resultado' | 'explicar' | 'cancelar'
        | 'reanalisar'

    ``iniciar`` retorna imediatamente um ``job_id``. ``status`` nunca abre o
    navegador. ``resultado`` e ``explicar`` exigem autorização explícita.
    OCR permanece sujeito à disponibilidade local. A análise Gemini só ocorre
    pela ação explícita ``iniciar_agente`` e usa um dossiê já concluído.
    Nenhuma ação movimenta, marca, minuta, assina ou altera o PJe.
    """
    parametros_confirmacao = _parametros_confirmacao_chamada(locals())
    _p, _g, bloqueio_perfil = _ativar_perfil_ferramenta(
        persona, grau, perfil
    )
    if bloqueio_perfil:
        return bloqueio_perfil
    action = _sem_acento(str(acao or "")).strip().casefold().replace(" ", "_")
    action = _ALIAS_ANALISE_COMPLETA.get(action, action)
    allowed = {
        "planejar",
        "inventariar",
        "iniciar_rapida",
        "aprofundar",
        "iniciar",
        "status",
        "resultado",
        "explicar",
        "cancelar",
        "reanalisar",
        "analisar_varios",
        "criar_lote",
        "adicionar_ao_lote",
        "iniciar_lote",
        "status_lote",
        "resultado_lote",
        "cancelar_lote",
        "retomar_lote",
        "iniciar_agente",
        "status_agente",
        "resultado_agente",
        "explicar_agente",
        "preparar_pdf_integral",
        "status_pdf_integral",
        "resultado_pdf_integral",
        "confirmar_descarte",
        "registrar_resultado_teste",
    }
    if action not in allowed:
        return {"erro": "ação desconhecida", "acoes": sorted(allowed)}

    _legacy_write_actions = {
        "inventariar",
        "iniciar_rapida",
        "aprofundar",
        "iniciar",
        "reanalisar",
        "analisar_varios",
        "criar_lote",
        "adicionar_ao_lote",
        "iniciar_lote",
        "retomar_lote",
    }
    if retention_policy.zero_retention_enabled() and (
        action in _legacy_write_actions
        or (action == "iniciar_agente" and job_id not in _ephemeral_pdf_jobs)
    ):
        return {
            "erro": (
                "retenção zero ativa: esta ação legada persistiria dados "
                "processuais por dias"
            ),
            "alternativa": (
                "use preparar_pdf_integral e, sobre o job efêmero, "
                "iniciar_agente; o descarte ocorre com confirmar_descarte"
            ),
            "read_only": True,
        }

    if action in {
        "status_pdf_integral",
        "resultado_pdf_integral",
        "confirmar_descarte",
        "registrar_resultado_teste",
    }:
        if not job_id:
            return {"erro": "job_id efêmero é obrigatório", "read_only": True}
        try:
            job = _public_ephemeral_pdf_job(job_id)
            if action == "status_pdf_integral":
                return job
            if action == "resultado_pdf_integral":
                if not autorizacao_leitura or not autorizacao_ref.strip():
                    return {
                        "erro": "autorização explícita é obrigatória para expor o manifesto",
                        "read_only": True,
                    }
                if job.get("status") != "completed":
                    return job
                capsule = await asyncio.to_thread(
                    analysis_capsule.open_capsule,
                    str(job["capsule_id"]),
                    mode=str(job["mode"]),
                )
                manifest = json.loads(
                    capsule.file("manifest.json").read_text(encoding="utf-8")
                )
                shards = json.loads(
                    capsule.file("shards.json").read_text(encoding="utf-8")
                )
                return {
                    **job,
                    "manifest": manifest,
                    "shards": shards,
                    "discard_required_after_confirmation": True,
                    "delivery_receipt": _emitir_recibo_entrega(job_id),
                }
            if action == "confirmar_descarte":
                expected_token = str(job.get("discard_token") or "")
                if str(job.get("mode")) == "production" and expected_token:
                    provided = str(confirmation_token or "").strip()
                    if provided != expected_token:
                        return {
                            "erro": (
                                "descarte em produção exige o discard_token do "
                                "recibo de entrega em confirmation_token"
                            ),
                            "read_only": True,
                        }
                receipt = await asyncio.to_thread(
                    analysis_capsule.purge_capsule,
                    str(job["capsule_id"]),
                    reason="delivery_confirmed",
                    mode=str(job["mode"]),
                )
                _descartar_agente_efemero_do_job(job_id)
                _ephemeral_pdf_jobs.pop(job_id, None)
                return receipt
            if str(job.get("mode")) != "training":
                return {
                    "erro": "resultado de teste exige modo training",
                    "read_only": True,
                }
            result = await asyncio.to_thread(
                analysis_capsule.record_test_result,
                str(job["capsule_id"]),
                passed=bool(teste_aprovado),
                mode="training",
            )
            if teste_aprovado:
                _descartar_agente_efemero_do_job(job_id)
                _ephemeral_pdf_jobs.pop(job_id, None)
            return result
        except analysis_capsule.CapsuleError as exc:
            return {"erro": str(exc), "read_only": True}

    if action == "preparar_pdf_integral":
        cnj = _normaliza_cnj(numero_cnj)
        validation = _analisar_cnj(cnj)
        if not validation.get("valido"):
            return {
                "erro": validation.get("erro") or "número CNJ inválido",
                "read_only": True,
            }
        if not autorizacao_leitura or not autorizacao_ref.strip():
            return {
                "erro": "autorizacao_leitura=True e autorizacao_ref são obrigatórios",
                "read_only": True,
            }
        data_mode = str(modo_dados or "production").strip().casefold()
        if data_mode not in {"production", "training"}:
            return {
                "erro": "modo_dados deve ser production ou training",
                "read_only": True,
            }
        confirmacao = _exigir_confirmacao_consulta(
            "analisar_processo_completo_pje",
            action,
            parametros_confirmacao,
            confirmar_consulta,
            confirmation_token,
        )
        if confirmacao:
            return confirmacao
        capsule = await asyncio.to_thread(
            analysis_capsule.create_capsule,
            mode=data_mode,
        )
        ephemeral_job_id = uuid.uuid4().hex
        _ephemeral_pdf_jobs[ephemeral_job_id] = {
            "job_id": ephemeral_job_id,
            "capsule_id": capsule.capsule_id,
            "status": "queued",
            "mode": data_mode,
            "complete": False,
        }
        task = asyncio.create_task(
            _executar_pdf_integral_efemero(
                ephemeral_job_id,
                capsule=capsule,
                numero_cnj=cnj,
                persona=_p,
                grau=_g,
            )
        )
        _ephemeral_pdf_tasks[ephemeral_job_id] = task
        return {
            **_public_ephemeral_pdf_job(ephemeral_job_id),
            "message": "PDF integral nativo e OCR local agendados em cápsula efêmera",
        }

    # Ações de Lote (Batch)
    if action in {
        "analisar_varios",
        "criar_lote",
        "adicionar_ao_lote",
        "iniciar_lote",
        "status_lote",
        "resultado_lote",
        "cancelar_lote",
        "retomar_lote",
    }:
        batch_mode = str(modo or "rapida").strip().casefold()
        if batch_mode not in analise_processual_completa.SUPPORTED_MODES:
            return {
                "erro": "modo do lote deve ser inventario, rapida ou integral",
                "read_only": True,
            }
        try:
            batch_concurrency = max(1, min(int(concorrencia_processos), 3))
        except (TypeError, ValueError):
            return {
                "erro": "concorrencia_processos deve ser um inteiro entre 1 e 3",
                "read_only": True,
            }
        if action == "analisar_varios":
            cnjs = list(
                dict.fromkeys(
                    item.strip()
                    for item in re.split(r"[,;\n\s]+", numero_cnj or "")
                    if item.strip()
                )
            )
            invalidos = [
                item for item in cnjs if not _analisar_cnj(_normaliza_cnj(item)).get("valido")
            ]
            if len(cnjs) < 2 or len(cnjs) > 100 or invalidos:
                return {
                    "erro": "informe de 2 a 100 números CNJ válidos e distintos",
                    "invalidos": [_mascarar_cnj(item) for item in invalidos],
                    "read_only": True,
                }
            if not autorizacao_leitura or not autorizacao_ref.strip():
                return {
                    "erro": "autorizacao_leitura=True e autorizacao_ref são obrigatórios",
                    "read_only": True,
                }
            confirmacao = _exigir_confirmacao_consulta(
                "analisar_processo_completo_pje",
                action,
                parametros_confirmacao,
                confirmar_consulta,
                confirmation_token,
            )
            if confirmacao:
                return confirmacao
            batch_id = await asyncio.to_thread(
                batch_engine.criar_lote,
                mode=batch_mode,
                max_concurrency=batch_concurrency,
            )
            added = await asyncio.to_thread(
                batch_engine.adicionar_ao_lote,
                batch_id,
                [_normaliza_cnj(item) for item in cnjs],
                grau=_normaliza_grau(grau),
                persona=_normaliza_persona(persona),
            )
            started = await asyncio.to_thread(
                batch_engine.iniciar_lote,
                batch_id,
                authorisation_ref=autorizacao_ref,
                force_reread=forcar_releitura,
                mode=batch_mode,
                max_concurrency=batch_concurrency,
            )
            return {
                **started,
                **added,
                "message": "lote paralelo iniciado; cada saída libera imediatamente a próxima entrada",
                "read_only": True,
            }

        if action == "criar_lote":
            res = await asyncio.to_thread(
                batch_engine.criar_lote,
                batch_id=job_id,
                mode=batch_mode,
                max_concurrency=batch_concurrency,
            )
            return {
                "batch_id": res,
                "status": "queued",
                "mode": batch_mode,
                "max_concurrency": batch_concurrency,
            }

        if not job_id:
            return {"erro": "job_id (batch_id) é obrigatório", "read_only": True}

        if action == "adicionar_ao_lote":
            if not numero_cnj:
                return {"erro": "numero_cnj é obrigatório", "read_only": True}
            cnjs = [c.strip() for c in numero_cnj.replace(",", " ").split() if c.strip()]
            res = await asyncio.to_thread(
                batch_engine.adicionar_ao_lote,
                batch_id=job_id,
                cnj_list=cnjs,
                grau=grau,
                persona=persona,
            )
            return res

        if action == "iniciar_lote":
            if not autorizacao_leitura or not autorizacao_ref.strip():
                return {
                    "erro": "autorizacao_leitura=True e autorizacao_ref são obrigatórios para abrir os autos",
                    "read_only": True,
                }
            confirmacao = _exigir_confirmacao_consulta(
                "analisar_processo_completo_pje",
                action,
                parametros_confirmacao,
                confirmar_consulta,
                confirmation_token,
            )
            if confirmacao:
                return confirmacao
            res = await asyncio.to_thread(
                batch_engine.iniciar_lote,
                batch_id=job_id,
                authorisation_ref=autorizacao_ref,
                force_reread=forcar_releitura,
                mode=batch_mode,
                max_concurrency=batch_concurrency,
            )
            return res

        if action == "status_lote":
            res = await asyncio.to_thread(batch_engine.status_lote, batch_id=job_id)
            return res

        if action == "resultado_lote":
            if not autorizacao_leitura or not autorizacao_ref.strip():
                return {
                    "erro": "autorizacao_leitura=True e autorizacao_ref são obrigatórios para ler os resultados",
                    "read_only": True,
                }
            res = await asyncio.to_thread(batch_engine.resultado_lote, batch_id=job_id)
            return res

        if action == "cancelar_lote":
            res = await asyncio.to_thread(batch_engine.cancelar_lote, batch_id=job_id)
            return res

        if action == "retomar_lote":
            res = await asyncio.to_thread(batch_engine.retomar_lote, batch_id=job_id)
            return res

    if action in {
        "iniciar_agente",
        "status_agente",
        "resultado_agente",
        "explicar_agente",
    }:
        if action == "status_agente":
            if not agent_run_id:
                return {"erro": "agent_run_id é obrigatório", "read_only": True}
            if agent_run_id in _ephemeral_agent_runs:
                return _public_ephemeral_agent_run(agent_run_id)
            return await asyncio.to_thread(vertex_process_agent.get_run, agent_run_id)
        if action in {"resultado_agente", "explicar_agente"}:
            if not agent_run_id:
                return {"erro": "agent_run_id é obrigatório", "read_only": True}
            if not autorizacao_leitura or not autorizacao_ref.strip():
                return {
                    "erro": "autorização explícita é obrigatória para expor a análise",
                    "read_only": True,
                }
            if agent_run_id in _ephemeral_agent_runs:
                try:
                    payload = await asyncio.to_thread(
                        _resultado_agente_efemero, agent_run_id
                    )
                except analysis_capsule.CapsuleError as exc:
                    return {"erro": str(exc), "read_only": True}
                if action == "resultado_agente":
                    return payload
                if not finding_id:
                    return {"erro": "finding_id é obrigatório", "read_only": True}
                if not payload.get("available"):
                    return payload
                wanted = str(finding_id).strip()
                for finding in payload["result"].get("findings") or []:
                    if str(finding.get("finding_id") or "") == wanted:
                        return {
                            "agent_run_id": agent_run_id,
                            "finding": finding,
                            "verified": True,
                            "read_only": True,
                            "process_data_persisted": False,
                        }
                return {
                    "erro": "conclusão verificada não encontrada",
                    "read_only": True,
                }
            if action == "resultado_agente":
                payload = await asyncio.to_thread(
                    vertex_process_agent.get_result,
                    agent_run_id,
                )
                if payload.get("available"):
                    relatorio = report_format.build_standard_report(
                        payload["result"]
                    )
                    payload = {
                        **payload,
                        "relatorio": relatorio,
                        "relatorio_markdown": report_format.render_markdown(
                            relatorio
                        ),
                    }
                return payload
            if not finding_id:
                return {"erro": "finding_id é obrigatório", "read_only": True}
            return await asyncio.to_thread(
                vertex_process_agent.explain,
                agent_run_id,
                finding_id,
            )
        if not job_id:
            return {
                "erro": "job_id do dossiê fonte é obrigatório",
                "read_only": True,
            }
        if not autorizacao_leitura or not autorizacao_ref.strip():
            return {
                "erro": "autorizacao_leitura=True e autorizacao_ref são obrigatórios",
                "read_only": True,
            }
        confirmacao = _exigir_confirmacao_consulta(
            "analisar_processo_completo_pje",
            action,
            parametros_confirmacao,
            confirmar_consulta,
            confirmation_token,
        )
        if confirmacao:
            return confirmacao
        if job_id in _ephemeral_pdf_jobs:
            source_job = _public_ephemeral_pdf_job(job_id)
            if source_job.get("status") != "completed":
                return {
                    "erro": "o preparo efêmero ainda não está completo",
                    **source_job,
                }
            selected_model = str(
                modelo_agente or vertex_process_agent.DEFAULT_MODEL
            ).strip()
            if selected_model != vertex_process_agent.DEFAULT_MODEL:
                return {
                    "erro": "modelo permitido nesta versão: gemini-3.5-flash",
                    "read_only": True,
                }
            ephemeral_run_id = uuid.uuid4().hex
            _ephemeral_agent_runs[ephemeral_run_id] = {
                "agent_run_id": ephemeral_run_id,
                "source_job_id": job_id,
                "capsule_id": str(source_job["capsule_id"]),
                "mode": str(source_job["mode"]),
                "status": "queued",
                "source": "ephemeral_capsule",
                "provider": "vertex_ai",
                "model": selected_model,
            }
            agent_task = asyncio.create_task(
                _executar_agente_efemero(ephemeral_run_id)
            )
            _ephemeral_agent_tasks[ephemeral_run_id] = agent_task
            return {
                **_public_ephemeral_agent_run(ephemeral_run_id),
                "message": (
                    "agente Vertex agendado sobre a cápsula efêmera; "
                    "o resultado permanece apenas dentro dela"
                ),
                "evidence_policy": "document_id+page+literal_excerpt+sha256",
            }
        run = await asyncio.to_thread(
            vertex_process_agent.create_run,
            job_id,
            model=modelo_agente,
        )
        _agendar_agente_vertex(str(run["run_id"]))
        return {
            **run,
            "message": "agente Gemini 3.5 Flash no Vertex AI agendado",
            "evidence_policy": "document_id+page+literal_excerpt+sha256",
        }

    if action in {"status", "cancelar"}:
        if not job_id:
            return {"erro": "job_id é obrigatório", "read_only": True}
        if action == "cancelar":
            return await asyncio.to_thread(
                analise_processual_completa.request_cancel,
                job_id,
            )
        return await asyncio.to_thread(
            analise_processual_completa.get_job,
            job_id,
        )

    if action in {"resultado", "explicar"}:
        if not job_id:
            return {"erro": "job_id é obrigatório", "read_only": True}
        if not autorizacao_leitura or not autorizacao_ref.strip():
            return {
                "erro": "autorização explícita é obrigatória para expor o dossiê",
                "read_only": True,
            }
        if action == "resultado":
            return await asyncio.to_thread(
                analise_processual_completa.get_result,
                job_id,
            )
        return await asyncio.to_thread(
            analise_processual_completa.explain,
            job_id,
            topico,
            finding_id,
        )

    cnj = _normaliza_cnj(numero_cnj)
    validation = _analisar_cnj(cnj)
    if not validation.get("valido"):
        return {
            "erro": validation.get("erro") or "número CNJ inválido",
            "read_only": True,
        }
    g = _normaliza_grau(grau)
    p = _normaliza_persona(persona)

    if action == "planejar":
        latest = await asyncio.to_thread(
            analise_processual_completa.find_latest_job,
            cnj,
            g,
        )
        return {
            "numero_cnj": cnj,
            "modos": ["inventario", "rapida", "integral"],
            "job_anterior": latest,
            "manifesto_disponivel": bool(latest and latest.get("total_documents")),
            "documentos_conhecidos": (
                int(latest.get("total_documents") or 0) if latest else 0
            ),
            "estimativa": (
                "reuso rápido do cache disponível"
                if latest and latest.get("processed_documents")
                else "manifesto será descoberto na primeira abertura dos autos"
            ),
            "ocr": "disabled_until_institutional_approval",
            "semantic_analysis": "disabled_until_institutional_approval",
            "read_only": True,
        }

    if not autorizacao_leitura or not autorizacao_ref.strip():
        return {
            "erro": (
                "autorizacao_leitura=True e autorizacao_ref são obrigatórios "
                "para abrir os autos"
            ),
            "read_only": True,
        }
    requested_mode = {
        "inventariar": "inventario",
        "iniciar_rapida": "rapida",
        "aprofundar": "integral",
        "reanalisar": "integral",
    }.get(action, modo.strip().casefold())
    if requested_mode not in analise_processual_completa.SUPPORTED_MODES:
        return {
            "erro": "modo deve ser inventario, rapida ou integral",
            "read_only": True,
        }
    confirmacao = _exigir_confirmacao_consulta(
        "analisar_processo_completo_pje",
        action,
        parametros_confirmacao,
        confirmar_consulta,
        confirmation_token,
    )
    if confirmacao:
        return confirmacao

    cache_source = (
        await asyncio.to_thread(
            analise_processual_completa.find_latest_job,
            cnj,
            g,
        )
        if action == "reanalisar" and not forcar_releitura
        else None
    )
    previous = (
        await asyncio.to_thread(
            analise_processual_completa.find_latest_job,
            cnj,
            g,
            resumable_only=True,
        )
        if retomar
        and action in {"iniciar", "inventariar", "iniciar_rapida", "aprofundar"}
        else None
    )
    if previous and previous.get("mode") != requested_mode:
        previous = None
    if previous and previous["status"] in {"queued", "running"}:
        _agendar_analise_completa(str(previous["job_id"]))
        return {**previous, "message": "job já estava em andamento"}
    if previous:
        selected_job = previous
        await asyncio.to_thread(
            analise_processual_completa.update_job,
            str(selected_job["job_id"]),
            status="queued",
            phase="queued",
            cancel_requested=0,
            error=None,
        )
    else:
        selected_job = await asyncio.to_thread(
            analise_processual_completa.create_job,
            process_number=cnj,
            grau=g,
            persona=p,
            mode=requested_mode,
            semantic_enabled=False,
            force_reread=forcar_releitura,
            authorisation_ref=autorizacao_ref,
        )
    if (
        action == "reanalisar"
        and cache_source
        and cache_source.get("status") in {"completed", "partial_with_gaps"}
        and not forcar_releitura
    ):
        try:
            completed_job = await asyncio.to_thread(
                analise_processual_completa.reanalyse_from_cache,
                str(selected_job["job_id"]),
                str(cache_source["job_id"]),
            )
            return {
                **completed_job,
                "message": (
                    "reanálise concluída com cache criptografado; "
                    "nenhum navegador foi aberto"
                ),
                "ocr": "disabled_until_institutional_approval",
                "semantic_analysis": "disabled_until_institutional_approval",
            }
        except analise_processual_completa.CompleteAnalysisError:
            # Cache expirado/incompleto: cai para a execução fria retomável.
            pass
    _agendar_analise_completa(str(selected_job["job_id"]))
    return {
        **selected_job,
        "status": "queued",
        "message": (
            "reanálise agendada com reaproveitamento do cache"
            if action == "reanalisar"
            else {
                "inventario": "inventário somente leitura iniciado",
                "rapida": "análise rápida seletiva iniciada",
                "integral": "análise integral somente leitura iniciada",
            }[requested_mode]
        ),
        "modo": requested_mode,
        "ocr": "disabled_until_institutional_approval",
        "semantic_analysis": "disabled_until_institutional_approval",
    }


async def verificar_prazos_urgentes(
    dias_limite: int = 3, persona: str = "advogado", grau: str = "1"
) -> dict:
    """[TJPA 1g|2g] Retorna expedientes com data limite em ate N dias (padrao 3 dias)."""
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    r = await pje.expedientes_pendentes()

    hoje = datetime.now()
    limite = hoje + timedelta(days=dias_limite)
    urgentes = []
    for exp in r.get("expedientes", []):
        try:
            dl = datetime.strptime(exp["data_limite"], "%d/%m/%Y %H:%M")
            dias = (dl - hoje).days
            if dl <= limite:
                # vencido=True deixa explicito que o prazo JA passou
                # (dias_restantes negativo era facil de passar batido)
                urgentes.append({**exp, "dias_restantes": dias, "vencido": dl < hoje})
        except (ValueError, KeyError):
            continue
    urgentes.sort(key=lambda x: x.get("dias_restantes", 999))
    return _marcar_grau(
        {
            "dias_limite": dias_limite,
            "total_urgentes": len(urgentes),
            "total_expedientes": (
                r.get("total_declarado")
                if r.get("dados_incompletos")
                else len(r.get("expedientes", []))
            ),
            "total_expedientes_extraidos": len(r.get("expedientes", [])),
            "dados_incompletos": r.get("dados_incompletos", False),
            "aviso_consistencia": r.get("aviso_consistencia"),
            "urgentes": urgentes,
        },
        p,
        g,
    )


async def analisar_risco_prazos(persona: str = "advogado", grau: str = "1") -> dict:
    """[TJPA 1g|2g] Matriz de análise de risco de todos os prazos pendentes (Crítico, Alto, Médio, Baixo)."""
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    r = await pje.expedientes_pendentes()

    hoje = datetime.now()
    matriz = {
        "critico_vencido": [],  # <= 0 dias
        "alto_risco": [],  # 1 a 3 dias
        "medio_risco": [],  # 4 a 7 dias
        "baixo_risco": [],  # > 7 dias
    }

    for exp in r.get("expedientes", []):
        try:
            dl = datetime.strptime(exp["data_limite"], "%d/%m/%Y %H:%M")
            dias = (dl - hoje).days
            item = {**exp, "dias_restantes": dias, "vencido": dl < hoje}
            if dias <= 0:
                matriz["critico_vencido"].append(item)
            elif 1 <= dias <= 3:
                matriz["alto_risco"].append(item)
            elif 4 <= dias <= 7:
                matriz["medio_risco"].append(item)
            else:
                matriz["baixo_risco"].append(item)
        except (ValueError, KeyError):
            matriz["baixo_risco"].append(exp)

    res = {
        "total_expedientes": (
            r.get("total_declarado")
            if r.get("dados_incompletos")
            else len(r.get("expedientes", []))
        ),
        "total_expedientes_extraidos": len(r.get("expedientes", [])),
        "dados_incompletos": r.get("dados_incompletos", False),
        "aviso_consistencia": r.get("aviso_consistencia"),
        "resumo_risco": {
            "critico_vencido": len(matriz["critico_vencido"]),
            "alto_risco": len(matriz["alto_risco"]),
            "medio_risco": len(matriz["medio_risco"]),
            "baixo_risco": len(matriz["baixo_risco"]),
        },
        "matriz_prazos": matriz,
    }
    return _marcar_grau(res, p, g)


async def calcular_prazos_processuais(
    data_inicio: str,
    dias_uteis: int = 15,
    feriados_extras=None,
) -> dict:
    """[TJPA 1g|2g] Calcula a data final de um prazo processual em DIAS ÚTEIS (CPC/2015).

    Considera fins de semana, feriados nacionais fixos, os feriados móveis
    (Sexta-feira Santa e Corpus Christi) e a suspensão do recesso forense
    (art. 220 do CPC: 20 de dezembro a 6 de janeiro).
    Data de início no formato YYYY-MM-DD ou DD/MM/YYYY.
    'feriados_extras' aceita feriados estaduais/municipais e suspensões do TJPA.
    """
    # Calendário único: _parse_data_br + _motivo_nao_util + _contar_prazo.
    # Havia aqui uma segunda implementação dos feriados, sem os móveis — duas
    # tabelas de calendário no mesmo arquivo é exatamente como elas divergem.
    dt_start = _parse_data_br(data_inicio)
    if dt_start is None:
        return {
            "erro": f"Data de início inválida: {data_inicio}. Use YYYY-MM-DD ou DD/MM/YYYY."
        }

    extras, invalidos = _parse_feriados_extras(feriados_extras)
    if invalidos:
        return {
            "erro": "Data inválida em 'feriados_extras'.",
            "invalidos": invalidos,
            "dica": "Use YYYY-MM-DD ou DD/MM/YYYY, separadas por vírgula.",
        }

    calculo = _contar_prazo(dt_start, dias_uteis, "uteis", extras)
    return {
        "data_intimacao_ou_publicacao": dt_start.strftime("%d/%m/%Y"),
        "dias_uteis_solicitados": dias_uteis,
        **calculo,
    }


# =========================================================================
# PRAZOS RECURSAIS (CPC/2015)
# =========================================================================

# Prazo legal por tipo de ato. 'contagem' distingue dias UTEIS (regra do
# art. 219 do CPC) de dias CORRIDOS (microssistema dos Juizados, Lei 9.099).
PRAZOS_RECURSAIS = {
    "apelacao": (15, "uteis", "art. 1.003, §5º, CPC"),
    "contrarrazoes_apelacao": (15, "uteis", "art. 1.010, §1º, CPC"),
    "agravo_de_instrumento": (15, "uteis", "art. 1.003, §5º, CPC"),
    "agravo_interno": (15, "uteis", "art. 1.021, §2º, CPC"),
    "embargos_de_declaracao": (5, "uteis", "art. 1.023, CPC"),
    "recurso_especial": (15, "uteis", "art. 1.003, §5º, CPC"),
    "recurso_extraordinario": (15, "uteis", "art. 1.003, §5º, CPC"),
    "agravo_em_recurso_especial": (15, "uteis", "art. 1.042, CPC"),
    "contestacao": (15, "uteis", "art. 335, CPC"),
    "replica": (15, "uteis", "art. 350/351, CPC"),
    "embargos_a_execucao": (15, "uteis", "art. 915, CPC"),
    "impugnacao_cumprimento": (15, "uteis", "art. 525, CPC"),
    "cumprimento_voluntario": (15, "uteis", "art. 523, CPC"),
    "manifestacao_simples": (5, "uteis", "art. 218, §3º, CPC"),
    "recurso_inominado": (10, "corridos", "art. 42, Lei 9.099/95 (Juizados)"),
    "embargos_declaracao_jec": (5, "corridos", "art. 49, Lei 9.099/95 (Juizados)"),
}

_ALIAS_PRAZOS = {
    "apelacao_civel": "apelacao",
    "recurso_de_apelacao": "apelacao",
    "contrarrazoes": "contrarrazoes_apelacao",
    "agravo": "agravo_de_instrumento",
    "ai": "agravo_de_instrumento",
    "agravo_regimental": "agravo_interno",
    "embargos": "embargos_de_declaracao",
    "ed": "embargos_de_declaracao",
    "edcl": "embargos_de_declaracao",
    "resp": "recurso_especial",
    "re": "recurso_extraordinario",
    "aresp": "agravo_em_recurso_especial",
    "defesa": "contestacao",
    "impugnacao_a_contestacao": "replica",
    "impugnacao": "impugnacao_cumprimento",
    "pagamento_voluntario": "cumprimento_voluntario",
    "manifestacao": "manifestacao_simples",
    "intimacao_simples": "manifestacao_simples",
    "inominado": "recurso_inominado",
    "jec": "recurso_inominado",
}

FERIADOS_NACIONAIS_FIXOS = {
    (1, 1),
    (4, 21),
    (5, 1),
    (9, 7),
    (10, 12),
    (11, 2),
    (11, 15),
    (11, 20),
    (12, 25),
}


def _pascoa(ano: int) -> datetime:
    """Domingo de Pascoa pelo algoritmo gregoriano anonimo (Meeus/Butcher)."""
    a = ano % 19
    b, c = divmod(ano, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ajuste_dia = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ajuste_dia) // 451
    mes, dia = divmod(h + ajuste_dia - 7 * m + 114, 31)
    return datetime(ano, mes, dia + 1)


def _feriados_moveis(ano: int) -> dict:
    """Feriados moveis do ano, derivados da Pascoa: {date: (rotulo, conta)}.

    'conta' diz se o dia entra na contagem de prazo. Sexta-feira Santa e
    Corpus Christi sao feriados forenses consolidados nacionalmente e contam.
    Carnaval e quarta-feira de cinzas dependem de portaria do tribunal —
    ficam listados mas NAO contam por padrao, porque tratar um dia util como
    suspenso empurraria o vencimento para depois do prazo real, que e o erro
    perigoso. Quem tiver a portaria do TJPA passa em 'feriados_extras'.
    """
    pascoa = _pascoa(ano)
    return {
        (pascoa - timedelta(days=48)).date(): (
            "Carnaval (segunda) — confirmar portaria",
            False,
        ),
        (pascoa - timedelta(days=47)).date(): (
            "Carnaval (terça) — confirmar portaria",
            False,
        ),
        (pascoa - timedelta(days=46)).date(): (
            "Quarta-feira de Cinzas — confirmar portaria",
            False,
        ),
        (pascoa - timedelta(days=2)).date(): (
            "Sexta-feira Santa (Paixão de Cristo)",
            True,
        ),
        (pascoa + timedelta(days=60)).date(): ("Corpus Christi", True),
    }


# Feriados locais (estaduais/municipais e suspensoes do TJPA) configurados de
# uma vez, em vez de repetidos em 'feriados_extras' a cada chamada. Fonte:
# PJE_FERIADOS_LOCAIS — lista de datas separadas por virgula OU caminho de um
# arquivo com uma data por linha (aceita '# comentario' e 'DATA  # rotulo').
#
# NAO ha feriado do Para embutido no codigo de proposito: a data varia por
# comarca e uma data errada aqui empurra o vencimento para DEPOIS do prazo
# real, que e o erro que faz perder prazo. Quem tem a portaria configura.
_CACHE_FERIADOS_LOCAIS = {}


def _feriados_locais_configurados() -> dict:
    """Le PJE_FERIADOS_LOCAIS. Retorna {date: rotulo}, com cache por valor."""
    bruto = (os.environ.get("PJE_FERIADOS_LOCAIS") or "").strip()
    if not bruto:
        return {}
    if bruto in _CACHE_FERIADOS_LOCAIS:
        return _CACHE_FERIADOS_LOCAIS[bruto]

    linhas = []
    caminho = Path(bruto)
    try:
        if caminho.is_file():
            linhas = caminho.read_text(encoding="utf-8").splitlines()
        else:
            linhas = re.split(r"[,;\n]+", bruto)
    except OSError:
        linhas = re.split(r"[,;\n]+", bruto)

    achados = {}
    for linha in linhas:
        texto = linha.split("#")[0].strip()
        if not texto:
            continue
        rotulo = linha.split("#", 1)[1].strip() if "#" in linha else ""
        d = _parse_data_br(texto)
        if d:
            achados[d.date()] = rotulo or "feriado local configurado"
    _CACHE_FERIADOS_LOCAIS[bruto] = achados
    return achados


def carregar_comarca(comarca: str) -> dict:
    comarca_clean = re.sub(r"[^\w-]", "", comarca.lower()).strip()
    if not comarca_clean:
        return {"erro": "Nome de comarca inválido."}
        
    pasta_dados = Path(__file__).resolve().parents[1] / "resources" / "calendars"
    if not pasta_dados.exists():
        pasta_dados.mkdir(parents=True, exist_ok=True)
        
    caminho = pasta_dados / f"feriados_{comarca_clean}.txt"
    
    if not caminho.exists():
        conteudo_padrao = ""
        if comarca_clean in ("belem", "belém"):
            conteudo_padrao = (
                "# Feriados e suspensões locais de Belém-PA\n"
                "12/01/2026 # Aniversário de Belém\n"
                "15/08/2026 # Adesão do Pará (estadual)\n"
                "12/10/2026 # Recírio (ponto facultativo)\n"
                "08/12/2026 # Nossa Senhora da Conceição\n"
            )
        elif comarca_clean in ("maraba", "marabá"):
            conteudo_padrao = (
                "# Feriados e suspensões locais de Marabá-PA\n"
                "05/04/2026 # Aniversário de Marabá\n"
                "15/08/2026 # Adesão do Pará (estadual)\n"
                "20/10/2026 # Padroeiro de Marabá\n"
            )
        else:
            conteudo_padrao = (
                f"# Feriados e suspensões locais da Comarca de {comarca.capitalize()}\n"
                "15/08/2026 # Adesão do Pará (estadual)\n"
            )
        caminho.write_text(conteudo_padrao, encoding="utf-8")
        
    os.environ["PJE_FERIADOS_LOCAIS"] = str(caminho)
    _CACHE_FERIADOS_LOCAIS.clear()
    
    carregados = _feriados_locais_configurados()
    
    return {
        "status": "sucesso",
        "mensagem": f"Calendário local da comarca '{comarca_clean}' carregado com sucesso.",
        "caminho_arquivo": str(caminho),
        "total_datas": len(carregados),
        "datas_carregadas": {str(k): v for k, v in carregados.items()}
    }


def _motivo_nao_util(d: datetime, feriados_extras=None) -> str:
    """Diz por que a data nao e dia util forense, ou '' se for util.

    feriados_extras: iteravel de date/datetime com feriados estaduais,
    municipais ou suspensoes do TJPA que o calendario nacional nao cobre.
    Alem deles, valem os configurados em PJE_FERIADOS_LOCAIS.
    """
    if d.weekday() in (5, 6):
        return "Final de Semana"
    if (d.month == 12 and d.day >= 20) or (d.month == 1 and d.day <= 6):
        return "Recesso Forense (CPC art. 220)"
    if (d.month, d.day) in FERIADOS_NACIONAIS_FIXOS:
        return "Feriado Nacional"
    movel = _feriados_moveis(d.year).get(d.date())
    if movel and movel[1]:
        return f"Feriado Nacional Móvel — {movel[0]}"
    if feriados_extras:
        for extra in feriados_extras:
            alvo = extra.date() if isinstance(extra, datetime) else extra
            if alvo == d.date():
                return "Feriado/suspensão informado pelo usuário"
    local = _feriados_locais_configurados().get(d.date())
    if local:
        return f"Feriado local configurado — {local}"
    return ""


def _parse_data_br(data: str):
    """Aceita YYYY-MM-DD ou DD/MM/YYYY. Retorna datetime ou None."""
    bruto = str(data or "").strip()
    if not bruto:
        return None
    if "/" in bruto:
        partes = bruto.split("/")
        if len(partes) != 3:
            return None
        bruto = (
            f"{partes[0]}-{partes[1]}-{partes[2]}"
            if len(partes[0]) == 4
            else f"{partes[2]}-{partes[1]}-{partes[0]}"
        )
    try:
        return datetime.strptime(bruto, "%Y-%m-%d")
    except ValueError:
        return None


def _parse_feriados_extras(valor) -> tuple:
    """Le a lista de feriados extras. Retorna (datas, invalidos)."""
    if not valor:
        return [], []
    itens = (
        valor
        if isinstance(valor, (list, tuple))
        else [v for v in re.split(r"[,;\n]+", str(valor)) if v.strip()]
    )
    datas, invalidos = [], []
    for item in itens:
        d = _parse_data_br(str(item).strip())
        (datas if d else invalidos).append(d or str(item).strip())
    return datas, invalidos


def _calendario_forense(ano: int, feriados_extras=None) -> dict:
    """Lista os dias sem expediente forense do ano (fora fins de semana)."""
    fixos = [
        {
            "data": datetime(ano, m, d).strftime("%d/%m/%Y"),
            "motivo": "Feriado Nacional",
            "conta_no_prazo": True,
        }
        for (m, d) in sorted(FERIADOS_NACIONAIS_FIXOS)
    ]
    moveis = [
        {"data": dia.strftime("%d/%m/%Y"), "motivo": rotulo, "conta_no_prazo": conta}
        for dia, (rotulo, conta) in sorted(_feriados_moveis(ano).items())
    ]
    extras, _ = _parse_feriados_extras(feriados_extras)
    informados = [
        {
            "data": d.strftime("%d/%m/%Y"),
            "motivo": "Informado pelo usuário",
            "conta_no_prazo": True,
        }
        for d in sorted(extras)
    ]
    locais_todos = _feriados_locais_configurados()
    locais = [
        {
            "data": d.strftime("%d/%m/%Y"),
            "motivo": f"Feriado local configurado — {rotulo}",
            "conta_no_prazo": True,
        }
        for d, rotulo in sorted(locais_todos.items())
        if d.year == ano
    ]
    origem_local = os.environ.get("PJE_FERIADOS_LOCAIS") or ""

    return {
        "ano": ano,
        "feriados_nacionais_fixos": fixos,
        "feriados_moveis": moveis,
        "feriados_locais_configurados": locais,
        "feriados_informados": informados,
        "recesso_forense": {
            "periodo": f"20/12/{ano} a 06/01/{ano + 1}",
            "fundamento": "art. 220 do CPC",
        },
        "observacao": (
            "Carnaval e quarta-feira de cinzas aparecem com conta_no_prazo=false: "
            "dependem de portaria do tribunal. Tratar dia útil como suspenso "
            "empurraria o vencimento para DEPOIS do prazo real — por segurança "
            "eles não são contados. Tendo a portaria do TJPA, configure em "
            "PJE_FERIADOS_LOCAIS ou passe em 'feriados_extras'."
        ),
        "configuracao_local": {
            "variavel": "PJE_FERIADOS_LOCAIS",
            "definida": bool(origem_local),
            "origem": origem_local if origem_local else None,
            "total_datas_carregadas": len(locais_todos),
            "formato": (
                "Datas separadas por vírgula OU caminho de arquivo com uma data "
                "por linha (YYYY-MM-DD ou DD/MM/YYYY). Aceita '# comentário' "
                "para rotular, ex.: '15/08/2026  # Adesão do Pará'."
            ),
        },
        "nao_coberto": (
            "Nenhum feriado estadual (PA) ou municipal vem embutido no código: a "
            "data varia por comarca e cravar uma errada empurraria o vencimento "
            "para depois do prazo real. Configure em PJE_FERIADOS_LOCAIS "
            "(persistente) ou passe em 'feriados_extras' (por chamada)."
            if not locais_todos
            else f"{len(locais_todos)} data(s) local(is) carregada(s) de "
            f"PJE_FERIADOS_LOCAIS. Feriado de comarca fora dessa lista continua "
            f"por conta de 'feriados_extras'."
        ),
    }


async def consultar_calendario_forense(
    ano=None, data_inicial: str = "", feriados_extras=None
) -> dict:
    """[TJPA 1g|2g] Calendario forense do ano: o que o cálculo de prazo considera.

    Sem 'ano', usa o ano de 'data_inicial' ou o ano corrente. Se 'data_inicial'
    vier preenchida, responde tambem se aquela data especifica e dia util.
    Nao abre o PJe.
    """
    extras, invalidos = _parse_feriados_extras(feriados_extras)
    if invalidos:
        return {
            "erro": "Data inválida em 'feriados_extras'.",
            "invalidos": invalidos,
            "dica": "Use YYYY-MM-DD ou DD/MM/YYYY, separadas por vírgula.",
        }

    referencia = _parse_data_br(data_inicial) if data_inicial else None
    if ano in (None, "", 0):
        alvo = referencia.year if referencia else datetime.now().year
    else:
        try:
            alvo = int(ano)
        except (TypeError, ValueError):
            return {"erro": f"'ano' deve ser um número inteiro, veio: {ano!r}"}
    if alvo < 1900 or alvo > 2200:
        return {"erro": f"'ano' fora da faixa aceita (1900..2200): {alvo}"}

    resultado = _calendario_forense(alvo, extras)
    if data_inicial:
        if referencia is None:
            return {
                "erro": f"Data inválida: '{data_inicial}'.",
                "dica": "Use YYYY-MM-DD ou DD/MM/YYYY.",
            }
        motivo = _motivo_nao_util(referencia, extras)
        resultado["consulta_data"] = {
            "data": referencia.strftime("%d/%m/%Y"),
            "eh_dia_util_forense": not motivo,
            "motivo": motivo or "Dia útil",
        }
    return resultado


async def criar_sessao_contexto_fixado(
    *,
    persona: str,
    grau: str,
    perfil: str,
    confirmar_localizacao_nao_aplicavel: str = "",
) -> dict:
    """Cria e valida uma sessão interna isolada sem executar consulta."""
    cliente = await cliente_singleton.criar_sessao_contexto_fixado(
        localizador_nao_confiavel=perfil,
        persona=_normaliza_persona(persona),
        grau=_normaliza_grau(grau),
        confirmar_localizacao_nao_aplicavel=(
            confirmar_localizacao_nao_aplicavel
        ),
    )
    contexto = dict(cliente.contexto_fixado or {})
    return {
        "status": "ok",
        "codigo": "SESSAO_CONTEXTO_FIXADO",
        "contexto_validado_id": contexto.get("contexto_validado_id"),
        "usuario_confirmado": bool(contexto.get("usuario_id")),
        "persona": contexto.get("persona"),
        "grau": contexto.get("grau"),
        "unidade": contexto.get("unidade"),
        "localizacao": contexto.get("localizacao"),
        "localizacao_status": contexto.get("localizacao_status"),
        "papel": contexto.get("papel"),
        "titulo_confirmado": bool(contexto.get("titulo")),
        "rota_confirmada": bool(contexto.get("rota")),
        "somente_leitura": True,
        "sessao_imutavel": True,
        "cache_liberado": True,
    }


def _contar_prazo(
    inicio: datetime, dias: int, contagem: str = "uteis", feriados_extras=None
) -> dict:
    """Conta o prazo a partir do dia seguinte a intimacao (CPC art. 224).

    Em 'uteis' pula fim de semana, feriado nacional fixo e recesso forense.
    Em 'corridos' conta dia a dia, mas prorroga o vencimento que cair em dia
    nao util (CPC art. 224, §1º) — regra que vale para os dois modos.
    """
    suspensos = []
    curr = inicio
    if contagem == "corridos":
        curr = inicio + timedelta(days=int(dias))
        while True:
            motivo = _motivo_nao_util(curr, feriados_extras)
            if not motivo:
                break
            suspensos.append(
                f"{curr.strftime('%d/%m/%Y')} ({motivo}) — vencimento prorrogado"
            )
            curr += timedelta(days=1)
    else:
        contados = 0
        while contados < int(dias):
            curr += timedelta(days=1)
            motivo = _motivo_nao_util(curr, feriados_extras)
            if motivo:
                suspensos.append(f"{curr.strftime('%d/%m/%Y')} ({motivo})")
            else:
                contados += 1
    dias_semana = [
        "Segunda-feira",
        "Terça-feira",
        "Quarta-feira",
        "Quinta-feira",
        "Sexta-feira",
        "Sábado",
        "Domingo",
    ]
    return {
        "data_vencimento_calculada": curr.strftime("%d/%m/%Y"),
        "dia_da_semana": dias_semana[curr.weekday()],
        "total_dias_corridos": (curr - inicio).days,
        "total_dias_suspensos": len(suspensos),
        "dias_suspensos_detalhados": suspensos,
    }


async def calcular_prazo_recursal(
    data_inicial: str,
    tipo_prazo: str,
    dobro: bool = False,
    feriados_extras=None,
) -> dict:
    """[TJPA 1g|2g] Prazo legal de um ato processual e sua data de vencimento.

    Resolve o prazo pelo tipo (apelação, embargos, contestação...) segundo o
    CPC/2015 ou a Lei 9.099/95, conta a partir do dia seguinte à intimação e
    devolve o vencimento. dobro=True aplica o prazo em dobro da Fazenda
    Pública, Defensoria e MP (arts. 180, 183 e 186 do CPC).

    Não abre o PJe — é cálculo puro sobre a data informada.
    """
    inicio = _parse_data_br(data_inicial)
    if inicio is None:
        return {
            "erro": f"Data inicial inválida: '{data_inicial}'.",
            "dica": "Use YYYY-MM-DD ou DD/MM/YYYY (data da intimação/publicação).",
        }

    canonica, erro = _resolver_acao(tipo_prazo, PRAZOS_RECURSAIS, _ALIAS_PRAZOS)
    if erro:
        return {
            "erro": "Tipo de prazo desconhecido",
            "tipo_recebido": tipo_prazo,
            "tipos_validos": {
                k: f"{v[0]} dias {v[1]} ({v[2]})"
                for k, v in sorted(PRAZOS_RECURSAIS.items())
            },
            **(
                {"voce_quis_dizer": erro["voce_quis_dizer"]}
                if "voce_quis_dizer" in erro
                else {}
            ),
        }

    dias_base, contagem, fundamento = PRAZOS_RECURSAIS[canonica]
    dias = dias_base * 2 if dobro else dias_base
    extras, invalidos = _parse_feriados_extras(feriados_extras)
    if invalidos:
        return {
            "erro": "Data inválida em 'feriados_extras'.",
            "invalidos": invalidos,
            "dica": "Use YYYY-MM-DD ou DD/MM/YYYY, separadas por vírgula.",
        }
    calculo = _contar_prazo(inicio, dias, contagem, extras)

    resultado = {
        "tipo_prazo": canonica,
        "fundamento_legal": fundamento,
        "prazo_legal_dias": dias_base,
        "contagem": contagem,
        "prazo_em_dobro_aplicado": bool(dobro),
        "dias_aplicados": dias,
        "data_intimacao_ou_publicacao": inicio.strftime("%d/%m/%Y"),
        **calculo,
    }
    if dobro:
        resultado["fundamento_dobro"] = (
            "arts. 180, 183 e 186 do CPC (Fazenda, MP e Defensoria)"
        )
    if contagem == "corridos":
        resultado["observacao"] = (
            "Prazo do microssistema dos Juizados: conta-se em dias CORRIDOS, "
            "não se aplicando o art. 219 do CPC."
        )
    resultado["aviso_feriados"] = (
        "Considera fins de semana, feriados nacionais fixos e móveis "
        "(Sexta-feira Santa e Corpus Christi) e o recesso do art. 220 do CPC. "
        "NÃO considera Carnaval (depende de portaria), feriados "
        "estaduais/municipais nem suspensões locais do TJPA — informe em "
        "'feriados_extras' ou veja acao='feriados'."
    )
    return resultado


async def analisar_estatisticas_painel_expedientes(
    persona: str = "advogado", grau: str = "1"
) -> dict:
    """[TJPA 1g|2g] Dashboard analítico (BI) de todos os expedientes pendentes da caixa de entrada."""
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    r = await pje.expedientes_pendentes()

    expedientes = r.get("expedientes", [])
    if not expedientes:
        total_declarado = r.get("total_declarado") or 0
        return _marcar_grau(
            {
                "total_expedientes": total_declarado,
                "total_expedientes_extraidos": 0,
                "dados_incompletos": r.get("dados_incompletos", False),
                "mensagem": (
                    r.get("aviso_consistencia")
                    or "Nenhum expediente pendente encontrado."
                ),
            },
            p,
            g,
        )

    por_orgao = {}
    por_tipo = {}
    dias_restantes_list = []
    hoje = datetime.now()

    for exp in expedientes:
        orgao = exp.get("orgao", exp.get("vara", "Não informado"))
        tipo = exp.get("ato", exp.get("tipo", "Comunicação"))

        por_orgao[orgao] = por_orgao.get(orgao, 0) + 1
        por_tipo[tipo] = por_tipo.get(tipo, 0) + 1

        try:
            dl = datetime.strptime(exp["data_limite"], "%d/%m/%Y %H:%M")
            dias = (dl - hoje).days
            dias_restantes_list.append(dias)
        except Exception:
            pass

    media_dias = (
        round(sum(dias_restantes_list) / len(dias_restantes_list), 1)
        if dias_restantes_list
        else 0.0
    )

    return _marcar_grau(
        {
            "total_expedientes": (
                r.get("total_declarado")
                if r.get("dados_incompletos")
                else len(expedientes)
            ),
            "total_expedientes_extraidos": len(expedientes),
            "dados_incompletos": r.get("dados_incompletos", False),
            "aviso_consistencia": r.get("aviso_consistencia"),
            "media_dias_restantes": media_dias,
            "distribuicao_por_orgao": dict(
                sorted(por_orgao.items(), key=lambda x: x[1], reverse=True)
            ),
            "distribuicao_por_tipo_ato": dict(
                sorted(por_tipo.items(), key=lambda x: x[1], reverse=True)
            ),
        },
        p,
        g,
    )


_MESES_PT = {
    "jan": 1,
    "janeiro": 1,
    "fev": 2,
    "feb": 2,
    "fevereiro": 2,
    "february": 2,
    "mar": 3,
    "marco": 3,
    "abr": 4,
    "apr": 4,
    "abril": 4,
    "april": 4,
    "mai": 5,
    "may": 5,
    "maio": 5,
    "jun": 6,
    "junho": 6,
    "jul": 7,
    "julho": 7,
    "ago": 8,
    "aug": 8,
    "agosto": 8,
    "august": 8,
    "set": 9,
    "sep": 9,
    "sept": 9,
    "setembro": 9,
    "september": 9,
    "out": 10,
    "oct": 10,
    "outubro": 10,
    "october": 10,
    "nov": 11,
    "novembro": 11,
    "dez": 12,
    "dec": 12,
    "dezembro": 12,
    "december": 12,
}


def _parse_data_movimentacao(valor: str):
    """Aceita as datas numéricas e textuais emitidas pela timeline do PJe."""
    bruto = re.sub(r"\s+", " ", str(valor or "")).strip()
    if not bruto:
        return None

    numerica = re.search(
        r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"
        r"|\b(\d{4})-(\d{1,2})-(\d{1,2})\b",
        bruto,
    )
    if numerica:
        try:
            if numerica.group(1):
                dia, mes, ano = map(int, numerica.group(1, 2, 3))
            else:
                ano, mes, dia = map(int, numerica.group(4, 5, 6))
            return datetime(ano, mes, dia)
        except ValueError:
            return None

    normalizada = _sem_acento(bruto)
    textual = re.search(r"\b(\d{1,2})\s+([a-z]+)\s+(\d{4})\b", normalizada)
    if not textual:
        return None
    mes = _MESES_PT.get(textual.group(2))
    if not mes:
        return None
    try:
        return datetime(int(textual.group(3)), mes, int(textual.group(1)))
    except ValueError:
        return None


async def analisar_linha_do_tempo_processo(
    numero_cnj: str,
    persona: str = "advogado",
    grau: str = "1",
    *,
    _relatorio: dict | None = None,
) -> dict:
    """[TJPA 1g|2g] Análise da linha do tempo e períodos de inatividade/paralisação do processo."""
    cnj = _normaliza_cnj(numero_cnj)
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    if _relatorio is None:
        pje = await cliente_singleton.get_cliente(p, g)
        rel = await pje.relatorio_processo(cnj)
    else:
        rel = _relatorio

    dados = rel.get("dados_basicos", {})
    movs = rel.get("movimentacoes", [])

    datas_movs = []
    datas_invalidas = []
    for m in movs:
        d_str = m.get("data", "")
        dt = _parse_data_movimentacao(d_str)
        if dt:
            titulo = m.get("titulo") or m.get("tipo") or m.get("descricao") or ""
            datas_movs.append((dt, titulo))
        elif d_str:
            datas_invalidas.append(str(d_str))

    datas_movs.sort(key=lambda x: x[0])

    agora = datetime.now()
    dt_dados_basicos = _parse_data_movimentacao(dados.get("data_distribuicao", ""))
    if dt_dados_basicos and dt_dados_basicos <= agora:
        dt_distribuicao = dt_dados_basicos
        origem_distribuicao = "dados_basicos"
    elif datas_movs:
        dt_distribuicao = datas_movs[0][0]
        origem_distribuicao = "primeira_movimentacao"
    else:
        dt_distribuicao = None
        origem_distribuicao = None
    dias_duracao_total = (
        max(0, (agora - dt_distribuicao).days) if dt_distribuicao else None
    )

    gaps = []
    for i in range(len(datas_movs) - 1):
        dt_atual, tit_atual = datas_movs[i]
        dt_prox, tit_prox = datas_movs[i + 1]
        gap_dias = (dt_prox - dt_atual).days
        if gap_dias >= 30:
            gaps.append(
                {
                    "data_inicio": dt_atual.strftime("%d/%m/%Y"),
                    "ato_inicio": tit_atual,
                    "data_fim": dt_prox.strftime("%d/%m/%Y"),
                    "ato_fim": tit_prox,
                    "dias_parado": gap_dias,
                }
            )

    gaps.sort(key=lambda x: x["dias_parado"], reverse=True)
    maior_gap = gaps[0] if gaps else None

    dias_sem_movimentacao = (
        max(0, (agora - datas_movs[-1][0]).days) if datas_movs else None
    )

    cobertura_datas = round(100 * len(datas_movs) / len(movs), 1) if movs else 0.0
    resultado = {
        "numero_cnj": cnj,
        "data_distribuicao": (
            dt_distribuicao.strftime("%d/%m/%Y") if dt_distribuicao else None
        ),
        "origem_data_distribuicao": origem_distribuicao,
        "duracao_total_dias": dias_duracao_total,
        "duracao_total_anos": (
            round(dias_duracao_total / 365.25, 1)
            if dias_duracao_total is not None
            else None
        ),
        "total_movimentacoes": len(movs),
        "movimentacoes_com_data_valida": len(datas_movs),
        "cobertura_datas_percentual": cobertura_datas,
        "dias_sem_movimentacao_atual": dias_sem_movimentacao,
        "processo_parado_atualmente": (
            dias_sem_movimentacao >= 30 if dias_sem_movimentacao is not None else None
        ),
        "maior_periodo_inatividade": maior_gap,
        "periodos_inatividade_superiores_30_dias": len(gaps),
        "detalhes_inatividade": gaps[:5],
        "calculo_datas_confiavel": bool(datas_movs) and not datas_invalidas,
    }
    if datas_invalidas:
        resultado["datas_movimentacoes_nao_reconhecidas"] = sorted(
            set(datas_invalidas)
        )[:10]
    if not datas_movs:
        resultado["aviso_datas"] = (
            "Nenhuma data de movimentação foi reconhecida; duração e "
            "inatividade não foram inventadas com a data atual."
        )
    return _marcar_grau(resultado, p, g)


async def recomendar_estrategia_processual(
    numero_cnj: str, persona: str = "advogado", grau: str = "1"
) -> dict:
    """[TJPA 1g|2g] Analisa a situação do processo e recomenda ações estratégicas (impulso, embargos, execução...)."""
    cnj = _normaliza_cnj(numero_cnj)
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)

    pje = await cliente_singleton.get_cliente(p, g)
    relatorio = await pje.relatorio_processo(cnj)
    resumo = await resumo_executivo_processo(
        cnj,
        persona=p,
        grau=g,
        _cliente=pje,
        _relatorio=relatorio,
    )
    timeline = await analisar_linha_do_tempo_processo(
        cnj,
        persona=p,
        grau=g,
        _relatorio=relatorio,
    )

    decisao = resumo.get("ultima_decisao", {})
    pendencias = resumo.get("pendencias", {})
    dias_parado = timeline.get("dias_sem_movimentacao_atual") or 0

    recomendacoes = []

    prazos_urgentes = [
        exp
        for exp in pendencias.get("expedientes", [])
        if exp.get("dias_restantes", 99) <= 3
    ]
    if prazos_urgentes:
        recomendacoes.append(
            {
                "prioridade": "🔴 ALTA",
                "tipo_acao": "Cumprimento de Prazo Urgente",
                "motivo": f"Existem {len(prazos_urgentes)} expedientes com vencimento em até 3 dias.",
                "sugestao": "Elaborar e protocolar petição de resposta/cumprimento imediatamente.",
            }
        )

    if dias_parado >= 90:
        recomendacoes.append(
            {
                "prioridade": "🟠 MÉDIA-ALTA",
                "tipo_acao": "Petição de Impulso Processual",
                "motivo": f"O processo está sem movimentação há {dias_parado} dias.",
                "sugestao": "Protocolar pedido de andamento/conclusão para despacho fundamentado no Art. 226, II do CPC.",
            }
        )

    teor_dec = str(decisao.get("teor_resumido", "")).lower()
    if "procedente" in teor_dec or "procedência" in teor_dec:
        recomendacoes.append(
            {
                "prioridade": "🟢 ESTRATÉGICA",
                "tipo_acao": "Cumprimento de Sentença",
                "motivo": "A última decisão/sentença indica provimento/procedência.",
                "sugestao": "Verificar trânsito em julgado e iniciar execução de título judicial/honorários.",
            }
        )
    elif (
        "omissão" in teor_dec or "contradição" in teor_dec or "obscuridade" in teor_dec
    ):
        recomendacoes.append(
            {
                "prioridade": "🟡 ATENÇÃO",
                "tipo_acao": "Embargos de Declaração",
                "motivo": "Possível vício de integração na última decisão.",
                "sugestao": "Avaliar protocolo de Embargos de Declaração no prazo de 5 dias úteis (Art. 1.023 CPC).",
            }
        )

    if not recomendacoes:
        recomendacoes.append(
            {
                "prioridade": "🟢 REGULAR",
                "tipo_acao": "Acompanhamento Ordinário",
                "motivo": "O processo encontra-se em trâmite regular sem alertas críticos.",
                "sugestao": "Aguardar próxima intimação ou publicação oficial no PJe.",
            }
        )

    return _marcar_grau(
        {
            "numero_cnj": cnj,
            "dias_parado_atualmente": dias_parado,
            "ultima_decisao_titulo": decisao.get("titulo", "N/I"),
            "total_recomendacoes": len(recomendacoes),
            "recomendacoes_estrategicas": recomendacoes,
            "completude": {
                "resumo": resumo.get("cobertura", {}),
                "linha_tempo_confiavel": timeline.get(
                    "calculo_datas_confiavel",
                    False,
                ),
                "cobertura_datas_percentual": timeline.get(
                    "cobertura_datas_percentual",
                    0,
                ),
            },
        },
        p,
        g,
    )


async def diagnosticar_saude_processual(
    numero_cnj: str, persona: str = "advogado", grau: str = "1"
) -> dict:
    """[TJPA 1g|2g] Índice de Saúde Processual (0-100) para triagem rápida da carteira.

    Consolida prazos, inatividade, duração e teor da última decisão num único
    score com os fatores que o penalizaram, cada um com peso e evidência.
    Serve para priorizar: quanto MENOR o score, mais urgente o processo.
    """
    cnj = _normaliza_cnj(numero_cnj)
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)

    pje = await cliente_singleton.get_cliente(p, g)
    relatorio = await pje.relatorio_processo(cnj)
    resumo = await resumo_executivo_processo(
        cnj,
        persona=p,
        grau=g,
        _cliente=pje,
        _relatorio=relatorio,
    )
    timeline = await analisar_linha_do_tempo_processo(
        cnj,
        persona=p,
        grau=g,
        _relatorio=relatorio,
    )

    pendencias = resumo.get("pendencias", {})
    expedientes = [e for e in pendencias.get("expedientes", []) if isinstance(e, dict)]
    vencidos = [e for e in expedientes if e.get("vencido")]
    urgentes = [
        e
        for e in expedientes
        if not e.get("vencido") and e.get("dias_restantes", 99) <= 3
    ]

    dias_parado = timeline.get("dias_sem_movimentacao_atual") or 0
    anos_duracao = timeline.get("duracao_total_anos") or 0
    maior_gap = (timeline.get("maior_periodo_inatividade") or {}).get("dias_parado", 0)
    teor = str(resumo.get("ultima_decisao", {}).get("teor_resumido", "")).lower()

    score = 100
    fatores = []

    def _penalizar(pontos: int, titulo: str, evidencia: str, severidade: str):
        nonlocal score
        score -= pontos
        fatores.append(
            {
                "fator": titulo,
                "severidade": severidade,
                "peso": -pontos,
                "evidencia": evidencia,
            }
        )

    if vencidos:
        _penalizar(
            min(30 * len(vencidos), 60),
            "Prazo vencido",
            f"{len(vencidos)} expediente(s) com data limite já ultrapassada.",
            "🔴 CRÍTICA",
        )
    if urgentes:
        _penalizar(
            min(15 * len(urgentes), 30),
            "Prazo em vencimento",
            f"{len(urgentes)} expediente(s) vencem em até 3 dias.",
            "🟠 ALTA",
        )

    if dias_parado >= 180:
        _penalizar(
            20,
            "Inércia prolongada",
            f"Sem movimentação há {dias_parado} dias.",
            "🟠 ALTA",
        )
    elif dias_parado >= 90:
        _penalizar(
            12,
            "Processo parado",
            f"Sem movimentação há {dias_parado} dias.",
            "🟡 MÉDIA",
        )
    elif dias_parado >= 30:
        _penalizar(
            5,
            "Movimentação lenta",
            f"Sem movimentação há {dias_parado} dias.",
            "🟢 BAIXA",
        )

    if maior_gap >= 365:
        _penalizar(
            10,
            "Histórico de paralisação",
            f"Já houve um intervalo de {maior_gap} dias sem qualquer ato.",
            "🟡 MÉDIA",
        )

    if anos_duracao > 5:
        _penalizar(
            10,
            "Duração excessiva",
            f"Processo tramita há {anos_duracao} anos.",
            "🟡 MÉDIA",
        )
    elif anos_duracao > 3:
        _penalizar(
            5,
            "Duração acima da média",
            f"Processo tramita há {anos_duracao} anos.",
            "🟢 BAIXA",
        )

    if any(
        k in teor
        for k in ["omissão", "omissao", "contradição", "contradicao", "obscuridade"]
    ):
        _penalizar(
            8,
            "Vício de integração na última decisão",
            "Teor da decisão sugere cabimento de Embargos de Declaração (Art. 1.023 CPC).",
            "🟡 MÉDIA",
        )

    score = max(0, min(100, score))
    if score >= 80:
        classificacao, acao_prioritaria = "🟢 SAUDÁVEL", "Acompanhamento ordinário."
    elif score >= 60:
        classificacao, acao_prioritaria = (
            "🟡 ATENÇÃO",
            "Revisar na próxima triagem semanal.",
        )
    elif score >= 40:
        classificacao, acao_prioritaria = (
            "🟠 ALERTA",
            "Agendar providência nos próximos dias.",
        )
    else:
        classificacao, acao_prioritaria = "🔴 CRÍTICO", "Providência imediata."

    if vencidos:
        acao_prioritaria = (
            "Regularizar prazo VENCIDO imediatamente (avaliar preclusão)."
        )
    elif urgentes:
        acao_prioritaria = "Protocolar manifestação antes do vencimento (≤3 dias)."
    elif dias_parado >= 90:
        acao_prioritaria = (
            "Protocolar petição de impulso processual (Art. 226, II, CPC)."
        )

    fatores.sort(key=lambda f: f["peso"])

    return _marcar_grau(
        {
            "numero_cnj": cnj,
            "score_saude": score,
            "classificacao": classificacao,
            "acao_prioritaria": acao_prioritaria,
            "sintese": (
                f"Score {score}/100 ({classificacao}) — "
                f"{len(vencidos)} prazo(s) vencido(s), {len(urgentes)} urgente(s), "
                f"{dias_parado} dia(s) sem movimentação."
            ),
            "indicadores": {
                "prazos_vencidos": len(vencidos),
                "prazos_urgentes_ate_3_dias": len(urgentes),
                "total_pendencias": pendencias.get("total", 0),
                "dias_sem_movimentacao": dias_parado,
                "duracao_anos": anos_duracao,
                "maior_paralisacao_dias": maior_gap,
                "classe_judicial": resumo.get("dados_basicos", {}).get("classe", "N/I"),
            },
            "total_fatores_penalizantes": len(fatores),
            "fatores_penalizantes": fatores,
            "completude": {
                "resumo": resumo.get("cobertura", {}),
                "linha_tempo_confiavel": timeline.get(
                    "calculo_datas_confiavel",
                    False,
                ),
                "cobertura_datas_percentual": timeline.get(
                    "cobertura_datas_percentual",
                    0,
                ),
            },
        },
        p,
        g,
    )


# =========================================================================
# CONSULTA DE PROCESSO POR NUMERO CNJ
# =========================================================================


async def comparar_processos(
    numeros_cnj: list[str], persona: str = "advogado", grau: str = "1"
) -> dict:
    """[TJPA 1g|2g] Compara dados básicos, classe, vara, partes e movimentações de múltiplos processos."""
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)

    comparacao = []
    for raw_cnj in numeros_cnj:
        cnj = _normaliza_cnj(raw_cnj)
        try:
            rel = await pje.relatorio_processo(cnj)
            dados = rel.get("dados_basicos", {})
            comparacao.append(
                {
                    "numero_cnj": cnj,
                    "classe": dados.get("classe", "N/I"),
                    "assunto": dados.get("assunto", "N/I"),
                    "vara": dados.get("vara", "N/I"),
                    "valor_causa": dados.get("valor_causa", "N/I"),
                    "total_partes": len(rel.get("partes", [])),
                    "total_movimentacoes": len(rel.get("movimentacoes", [])),
                    "total_documentos": len(rel.get("documentos", [])),
                }
            )
        except Exception as e:
            comparacao.append(
                {
                    "numero_cnj": cnj,
                    "erro": str(e),
                }
            )

    res = {
        "total_processos_analisados": len(numeros_cnj),
        "comparativo": comparacao,
    }
    return _marcar_grau(res, p, g)


CAMPOS_COMPARAVEIS = ("classe", "assunto", "vara", "valor_causa")


def _analisar_divergencias(comparativo: list) -> dict:
    """Diz o que os processos comparados tem em comum e onde divergem.

    Funcao pura. Ignora as linhas que vieram com erro — comparar um processo
    que falhou contra os demais so produziria divergencia falsa.
    """
    validos = [c for c in comparativo if isinstance(c, dict) and not c.get("erro")]
    campos = {}
    for campo in CAMPOS_COMPARAVEIS:
        valores = {}
        for c in validos:
            valores.setdefault(str(c.get(campo, "N/I")), []).append(c.get("numero_cnj"))
        campos[campo] = {
            "iguais_em_todos": len(valores) == 1 and len(validos) > 1,
            "valores": valores,
        }
    divergentes = [
        c
        for c, info in campos.items()
        if not info["iguais_em_todos"] and len(info["valores"]) > 1
    ]
    comuns = [c for c, info in campos.items() if info["iguais_em_todos"]]

    volumes = {
        c.get("numero_cnj"): {
            "movimentacoes": c.get("total_movimentacoes", 0),
            "documentos": c.get("total_documentos", 0),
            "partes": c.get("total_partes", 0),
        }
        for c in validos
    }
    mais_movimentado = max(
        volumes, key=lambda k: volumes[k]["movimentacoes"], default=None
    )

    return {
        "processos_comparaveis": len(validos),
        "campos_iguais_em_todos": comuns,
        "campos_divergentes": divergentes,
        "detalhe_por_campo": campos,
        "volume_por_processo": volumes,
        "mais_movimentado": mais_movimentado,
        "provavel_conexao": bool(comuns) and "vara" in comuns and len(validos) > 1,
    }


async def comparar_processos_detalhado(
    numeros_cnj, persona: str = "advogado", grau: str = "1"
) -> dict:
    """[TJPA 1g|2g] Compara varios processos e aponta onde eles divergem.

    Aceita lista ou string separada por virgula/ponto-e-virgula/quebra de
    linha. Alem do comparativo cru, devolve quais campos sao iguais em todos,
    quais divergem e qual processo esta mais movimentado.
    """
    if isinstance(numeros_cnj, str):
        itens = [v.strip() for v in re.split(r"[,;\n]+", numeros_cnj) if v.strip()]
    else:
        itens = [str(v).strip() for v in (numeros_cnj or []) if str(v).strip()]

    erro_validacao = _validar_lista_comparacao(itens)
    if erro_validacao:
        return erro_validacao

    bruto = await comparar_processos(itens, persona=persona, grau=grau)
    comparativo = bruto.get("comparativo", [])
    bruto["analise"] = _analisar_divergencias(comparativo)
    falhas = [
        c.get("numero_cnj")
        for c in comparativo
        if isinstance(c, dict) and c.get("erro")
    ]
    if falhas:
        bruto["nao_comparados"] = falhas
        bruto["aviso"] = (
            f"{len(falhas)} processo(s) não puderam ser lidos e ficaram fora da análise."
        )
    return bruto


def _validar_lista_comparacao(itens: list[str]) -> dict[str, Any] | None:
    """Valida a lista sem navegador, antes de pedir confirmação."""
    if len(itens) < 2:
        return {
            "erro": "Comparar exige pelo menos 2 números CNJ.",
            "recebido": itens,
            "dica": "Passe em numero_cnj os processos separados por vírgula.",
        }
    if len(itens) > 10:
        return {
            "erro": f"Comparação limitada a 10 processos por chamada (recebidos {len(itens)}).",
            "dica": "Cada processo é uma abertura de autos no PJe.",
        }

    normalizados = [_normaliza_cnj(item) for item in itens]
    duplicados = sorted(
        {
            item
            for item in normalizados
            if normalizados.count(item) > 1
        }
    )
    if duplicados:
        return {
            "erro": "Comparação contém números CNJ duplicados.",
            "duplicados_mascarados": [
                _mascarar_cnj(item)
                for item in duplicados
            ],
            "dica": "Informe entre 2 e 10 processos distintos.",
        }

    invalidos = [
        item
        for item in itens
        if not _analisar_cnj(item).get("valido", False)
    ]
    if invalidos:
        return {
            "erro": "Número CNJ inválido na lista (formato ou DV).",
            "invalidos_mascarados": [
                _mascarar_cnj(item)
                for item in invalidos
            ],
            "dica": (
                "Confira os 20 dígitos e o DV antes de comparar. "
                "Use buscar_processos_pje(acao='corrigir_cnj')."
            ),
        }

    return None


async def consultar_processo(
    numero_cnj: str, persona: str = "advogado", grau: str = "1"
) -> dict:
    """[TJPA 1g|2g] Consulta dados basicos de um processo pelo numero CNJ."""
    cnj = _normaliza_cnj(numero_cnj)
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    return _marcar_grau(await pje.buscar_processo(cnj), p, g)


async def ultimas_movimentacoes(
    numero_cnj: str, limite: int = 5, persona: str = "advogado", grau: str = "1"
) -> dict:
    """[TJPA 1g|2g] Lista as ultimas N movimentacoes (padrao 5)."""
    cnj = _normaliza_cnj(numero_cnj)
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    res = await pje.ultimas_movimentacoes(cnj, limite)
    movs = res.get("movimentacoes", [])

    # Destacar eventos juridicos criticos em Vara de Familia
    eventos_familia = []
    for m in movs:
        txt = " ".join(
            str(m.get(campo, ""))
            for campo in ("tipo", "titulo", "descricao", "detalhes")
        ).lower()
        alerta = None
        if "audiência" in txt or "audiencia" in txt:
            alerta = "AUDIÊNCIA (Conciliação/Mediação/Instrução)"
        elif (
            "ministério público" in txt
            or "ministerio publico" in txt
            or "promotor" in txt
            or re.search(r"\bmp\b", txt)
        ):
            alerta = "MANIFESTAÇÃO/PARECER DO MP"
        elif any(
            termo in txt
            for termo in (
                "estudo social",
                "estudo de caso",
                "laudo psicológico",
                "laudo psicologico",
                "psicossocial",
                "setor técnico",
                "setor tecnico",
                "setor social",
            )
        ):
            alerta = "LAUDO/ESTUDO PSICOSSOCIAL"
        elif "prisão" in txt or "prisao" in txt or "execução de alimentos" in txt:
            alerta = "EXECUÇÃO DE ALIMENTOS / DECRETO DE PRISÃO"

        if alerta:
            m["alerta_vara_familia"] = alerta
            eventos_familia.append(alerta)

    res["destaques_familia"] = sorted(set(eventos_familia))
    return _marcar_grau(res, p, g)


def _sem_acento(texto: str) -> str:
    """Minuscula e sem diacritico, pra comparar texto juridico em portugues.

    'Sentença' e 'sentenca' tem de casar: ninguem digita cedilha em busca.
    """
    s = unicodedata.normalize("NFKD", str(texto or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


# ---- Audiencias -------------------------------------------------------
_RE_DATA_HORA = re.compile(
    r"(\d{2})/(\d{2})/(\d{4})"
    r"(?:[^\d\n]{0,15}?(\d{1,2})\s*(?:[:h]\s*(\d{2})?)?)?",
    re.IGNORECASE,
)

TIPOS_AUDIENCIA = [
    ("conciliacao", "Conciliação"),
    ("mediacao", "Mediação"),
    ("instrucao e julgamento", "Instrução e Julgamento"),
    ("instrucao", "Instrução"),
    ("una", "Una"),
    ("justificacao", "Justificação"),
    ("admonitoria", "Admonitória"),
    ("custodia", "Custódia"),
    ("saneamento", "Saneamento"),
]

STATUS_AUDIENCIA = [
    ("redesignad", "redesignada"),
    ("cancelad", "cancelada"),
    ("adiad", "adiada"),
    ("realizad", "realizada"),
    ("designad", "designada"),
    ("remarcad", "redesignada"),
]


def _extrair_audiencias(movimentacoes, hoje=None) -> dict:
    """Garimpa audiencias nas movimentacoes do processo.

    Funcao pura. A data da audiencia sai do TEXTO do movimento (o campo
    'data' do movimento e quando ele foi lancado, nao quando a audiencia
    ocorre) — confundir os dois daria uma agenda inteiramente errada.
    """
    hoje = hoje or datetime.now()
    achadas = []
    for m in movimentacoes or []:
        if not isinstance(m, dict):
            continue
        bruto = f"{m.get('titulo', '')} {m.get('detalhes', '')}"
        norm = _sem_acento(bruto)
        if "audiencia" not in norm:
            continue

        tipo = next(
            (rotulo for chave, rotulo in TIPOS_AUDIENCIA if chave in norm),
            "Não especificado",
        )
        status = next(
            (rotulo for chave, rotulo in STATUS_AUDIENCIA if chave in norm),
            "mencionada",
        )

        quando, hora = None, None
        # Procura a data DEPOIS de 'para'/'dia', que é onde o PJe põe a data
        # da sessão; sem isso pegaríamos a data de protocolo citada antes.
        trecho = bruto
        marcador = re.search(r"\b(?:para|dia|em)\b", norm)
        if marcador:
            trecho = bruto[marcador.start() :]
        m_data = _RE_DATA_HORA.search(trecho) or _RE_DATA_HORA.search(bruto)
        if m_data:
            try:
                quando = datetime(
                    int(m_data.group(3)), int(m_data.group(2)), int(m_data.group(1))
                )
                if m_data.group(4):
                    hora = f"{int(m_data.group(4)):02d}:{m_data.group(5) or '00'}"
            except ValueError:
                quando = None

        achadas.append(
            {
                "tipo": tipo,
                "status": status,
                "data_audiencia": quando.strftime("%d/%m/%Y") if quando else None,
                "hora": hora,
                "data_movimento": m.get("data"),
                "descricao": re.sub(r"\s+", " ", bruto).strip()[:300],
                "_ordem": quando,
            }
        )

    # Cada menção cai em EXATAMENTE um balde. A primeira versão perdia a
    # audiência cancelada com data futura: ela não era 'ativa' (fora das
    # futuras), não era passada e tinha data (fora das sem_data) — sumia da
    # resposta. Some silenciosamente justo o item que explica por que não há
    # nada na pauta.
    canceladas, futuras, passadas, sem_data = [], [], [], []
    for a in achadas:
        if a["status"] == "cancelada":
            canceladas.append(a)
        elif not a["_ordem"]:
            sem_data.append(a)
        elif a["_ordem"].date() >= hoje.date():
            futuras.append(a)
        else:
            passadas.append(a)

    futuras.sort(key=lambda a: a["_ordem"])
    passadas.sort(key=lambda a: a["_ordem"], reverse=True)
    canceladas.sort(key=lambda a: a["_ordem"] or datetime.min, reverse=True)
    for a in achadas:
        a.pop("_ordem", None)

    proxima = futuras[0] if futuras else None
    resultado = {
        "total_mencoes_audiencia": len(achadas),
        "proxima_audiencia": proxima,
        "futuras": futuras,
        "passadas": passadas,
        "canceladas": canceladas,
        "sem_data_identificada": sem_data,
        "contadores": {
            "futuras": len(futuras),
            "passadas": len(passadas),
            "canceladas": len(canceladas),
            "sem_data": len(sem_data),
        },
    }
    resultado["contadores_fecham"] = (
        sum(resultado["contadores"].values()) == len(achadas)
    )
    if proxima and proxima.get("data_audiencia"):
        dias = (
            datetime.strptime(proxima["data_audiencia"], "%d/%m/%Y").date()
            - hoje.date()
        ).days
        resultado["dias_ate_proxima"] = dias
    if sem_data:
        resultado["aviso"] = (
            f"{len(sem_data)} menção(ões) a audiência sem data reconhecível no "
            "texto do movimento — confira em 'sem_data_identificada'."
        )
    return resultado


async def audiencias_processo(
    numero_cnj: str, persona: str = "advogado", grau: str = "1"
) -> dict:
    """[TJPA 1g|2g] Agenda de audiencias do processo, extraida das movimentacoes."""
    cnj = _normaliza_cnj(numero_cnj)
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    rel = await pje.relatorio_processo(cnj)
    movs = rel.get("movimentacoes", [])

    res = {"numero_cnj": cnj, "movimentacoes_analisadas": len(movs)}
    res.update(_extrair_audiencias(movs))
    res["completude"] = {
        "contadores_fecham": res["contadores_fecham"],
        "fonte_movimentacoes_completa": rel.get(
            "movimentacoes_completas",
            rel.get("arvore_completa", "unknown"),
        ),
    }
    if not res["total_mencoes_audiencia"]:
        res["mensagem"] = "Nenhuma menção a audiência nas movimentações do processo."
    res["observacao"] = (
        "Datas extraídas do texto das movimentações. Audiência redesignada "
        "aparece com a data de cada lançamento — confira a mais recente antes "
        "de agendar."
    )
    return _marcar_grau(res, p, g)


async def buscar_movimentacoes_por_termo(
    numero_cnj: str, termo: str, persona: str = "advogado", grau: str = "1"
) -> dict:
    """[TJPA 1g|2g] Filtra as movimentações do processo que contêm um termo específico.

    Exemplos de termo: 'audiência', 'citação', 'penhora', 'bancenjud', 'sentença', 'perícia'.
    """
    cnj = _normaliza_cnj(numero_cnj)
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    rel = await pje.relatorio_processo(cnj)

    movs = rel.get("movimentacoes", [])
    termo_norm = termo.lower().strip()

    # Expansão automatica de sinonimos praticos de Vara de Familia
    sinonimos_familia = {
        "alimentos": [
            "pensão",
            "pensao",
            "trinômio",
            "trinomio",
            "alimentante",
            "alimentando",
            "exequendo",
        ],
        "guarda": [
            "visitas",
            "convivência",
            "convivencia",
            "busca e apreensão",
            "guarda compartilhada",
        ],
        "divórcio": ["divorcio", "partilha", "bens", "união estável", "uniao estavel"],
        "estudo": [
            "psicossocial",
            "laudo",
            "assistente social",
            "psicólogo",
            "psicologo",
            "equipe técnica",
        ],
        "mp": ["ministério público", "ministerio publico", "promotor", "parecer"],
    }

    termos_busca = [termo_norm]
    for chave, lista_sinonimos in sinonimos_familia.items():
        if termo_norm == chave or termo_norm in lista_sinonimos:
            termos_busca.append(chave)
            termos_busca.extend(lista_sinonimos)

    termos_busca = list(set(termos_busca))
    # Comparação SEM acento dos dois lados: 'sentenca' tem de achar 'Sentença'.
    # Antes era só .lower(), então quem digitasse sem acento — o normal em
    # busca — recebia zero resultado sem nenhum aviso. A tabela de sinônimos
    # acima duplica acentos à mão justamente por causa disso, mas só cobria
    # as 5 chaves de família.
    termos_norm = list({_sem_acento(t) for t in termos_busca if t})
    filtradas = []

    for m in movs:
        texto = _sem_acento(
            f"{m.get('data', '')} {m.get('titulo', '')} {m.get('detalhes', '')}"
        )
        if any(t in texto for t in termos_norm):
            filtradas.append(m)

    res = {
        "numero_cnj": cnj,
        "termo_busca": termo,
        "termos_expandidos_familia": termos_busca if len(termos_busca) > 1 else [],
        "busca_ignora_acento": True,
        "total_encontrados": len(filtradas),
        "total_movimentacoes_processo": len(movs),
        "movimentacoes": filtradas,
    }
    if not filtradas and movs:
        res["dica"] = (
            f"Nenhuma movimentação com '{termo}'. O processo tem "
            f"{len(movs)} movimentações — veja todas com acao='movimentacoes' "
            f"(limite alto) ou procure no teor das peças com "
            f"gerir_documentos_pje(acao='pesquisar_texto')."
        )
    return _marcar_grau(res, p, g)


async def quadro_partes_advogados(
    numero_cnj: str, persona: str = "advogado", grau: str = "1"
) -> dict:
    """[TJPA 1g|2g] Devolve a estrutura qualificada e categorizada das partes e advogados do processo."""
    cnj = _normaliza_cnj(numero_cnj)
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    proc_info = await pje.buscar_processo(cnj)

    dados = proc_info.get("dados_basicos", {})
    partes = proc_info.get("partes_estruturadas")
    if not isinstance(partes, list):
        partes_legadas = proc_info.get("partes", [])
        partes = partes_legadas if isinstance(partes_legadas, list) else []
    if not partes:
        if proc_info.get("polo_ativo"):
            partes.append(
                {
                    "polo": "AUTOR",
                    "nome": proc_info["polo_ativo"],
                    "advogados": [],
                }
            )
        if proc_info.get("polo_passivo"):
            partes.append(
                {
                    "polo": "REU",
                    "nome": proc_info["polo_passivo"],
                    "advogados": [],
                }
            )

    polos = {"polo_ativo": [], "polo_passivo": [], "outros": []}
    for parte in partes:
        if not isinstance(parte, dict):
            continue
        polo = str(parte.get("polo", "")).lower()
        if (
            "ativo" in polo
            or "autor" in polo
            or "exequente" in polo
            or "requerente" in polo
        ):
            polos["polo_ativo"].append(parte)
        elif (
            "passivo" in polo
            or "réu" in polo
            or "reu" in polo
            or "executado" in polo
            or "requerido" in polo
        ):
            polos["polo_passivo"].append(parte)
        else:
            polos["outros"].append(parte)

    res = {
        "numero_cnj": cnj,
        "classe": dados.get("classe", ""),
        "assunto": dados.get("assunto", ""),
        "vara": dados.get("vara", ""),
        "quadro_polos": polos,
        "total_partes": len(partes),
    }
    return _marcar_grau(res, p, g)


async def relatorio_processo(
    numero_cnj: str, persona: str = "advogado", grau: str = "1"
) -> dict:
    """[TJPA 1g|2g] Relatorio completo: dados, partes, movimentacoes, documentos."""
    cnj = _normaliza_cnj(numero_cnj)
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    return _marcar_grau(await pje.relatorio_processo(cnj), p, g)


async def resumo_executivo_processo(
    numero_cnj: str,
    persona: str = "advogado",
    grau: str = "1",
    *,
    _cliente: Any = None,
    _relatorio: dict | None = None,
) -> dict:
    """[TJPA 1g|2g] Resumo executivo consolidado de um processo num único retorno.

    Combina em uma única chamada: dados básicos, partes, últimas 5 movimentações,
    teor da última decisão/sentença, pendências de intimação/prazo e arquivos em cache.
    """
    cnj = _normaliza_cnj(numero_cnj)
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = _cliente or await cliente_singleton.get_cliente(p, g)

    rel = (
        _relatorio
        if _relatorio is not None
        else await pje.relatorio_processo(cnj)
    )
    decisao = await pje.ler_documento_filtrado(
        cnj,
        r"(decis[ãa]o|senten[çc]a|despacho|ato\s+ordinat[óo]rio)",
    )

    r_exp = await pje.expedientes_pendentes()
    cnj_norm = re.sub(r"\D", "", cnj)
    hoje = datetime.now()
    prazos = []
    for exp in r_exp.get("expedientes", []):
        n = re.sub(r"\D", "", exp.get("numero_processo", ""))
        if n == cnj_norm:
            try:
                dl = datetime.strptime(exp["data_limite"], "%d/%m/%Y %H:%M")
                prazos.append(
                    {**exp, "dias_restantes": (dl - hoje).days, "vencido": dl < hoje}
                )
            except Exception:
                prazos.append(exp)

    cache_info = pje_downloader.estatisticas_e_limpeza_storage(numero_cnj=cnj, grau=g)
    teor_decisao = _teor_texto(decisao)
    alertas_fidelidade = []
    if decisao.get("erro"):
        alertas_fidelidade.append(f"última decisão não lida: {decisao['erro']}")
    if not rel.get("dados_basicos"):
        alertas_fidelidade.append(
            "dados básicos não foram reconhecidos no DOM atual do PJe"
        )
    if not rel.get("partes"):
        alertas_fidelidade.append("partes não foram reconhecidas no DOM atual do PJe")
    if not decisao.get("encontrado"):
        alertas_fidelidade.append(
            "nenhuma decisão/sentença/despacho foi localizada na árvore"
        )
    if not rel.get("movimentacoes") and rel.get("total_movimentacoes_encontradas", 0):
        alertas_fidelidade.append(
            "há movimentações, mas a coleção não foi propagada ao resumo"
        )

    # Analise de severidade dos prazos pendentes
    prazos_urgentes_qtd = sum(
        1
        for p in prazos
        if isinstance(p, dict)
        and p.get("vencido", False)
        or (isinstance(p, dict) and p.get("dias_restantes", 99) <= 3)
    )

    # Completa os dados básicos com o inventário interno quando o DOM dos
    # autos não expõe classe/assunto/órgão (comum em processos sigilosos).
    contexto_snapshot = caixas_tarefas.consultar_ocorrencias(
        grau=g,
        persona=p,
        termo=cnj,
        pagina=1,
        itens_por_pagina=1,
    )
    ocorrencias_snapshot = contexto_snapshot.get("ocorrencias", [])
    contexto = ocorrencias_snapshot[0] if ocorrencias_snapshot else {}
    basicos = rel.get("dados_basicos", {})
    classe_proc = str(
        basicos.get("classe") or contexto.get("classe_judicial") or ""
    ).lower()
    assunto_proc = str(
        basicos.get("assunto") or contexto.get("assunto_principal") or ""
    ).lower()
    orgao_proc = str(
        basicos.get("vara")
        or basicos.get("orgao_julgador")
        or contexto.get("orgao_julgador")
        or ""
    ).lower()
    conteudo_decisao = teor_decisao.lower()

    termos_familia = [
        "alimentos",
        "guarda",
        "visita",
        "divórcio",
        "divorcio",
        "paternidade",
        "interdição",
        "interdicao",
        "curatela",
        "adoção",
        "adocao",
        "família",
        "familia",
        "alienação parental",
        "alienacao parental",
        "reconhecimento / dissolução",
    ]
    eh_familia = (
        "vara de família" in orgao_proc
        or "vara de familia" in orgao_proc
        or any(k in classe_proc or k in assunto_proc for k in termos_familia)
    )

    detalhes_familia = {}
    if eh_familia or "alimentos" in conteudo_decisao or "guarda" in conteudo_decisao:
        detalhes_familia = {
            "tem_alimentos": "alimentos" in classe_proc
            or "alimentos" in assunto_proc
            or "alimentos" in conteudo_decisao
            or "pensão" in conteudo_decisao,
            "tem_guarda_visitas": "guarda" in classe_proc
            or "guarda" in assunto_proc
            or "visita" in assunto_proc
            or "guarda" in conteudo_decisao,
            "segredo_justica_presumido": True,  # Processos de familia correm em segredo de justiça (art. 189, II, CPC)
            "prioridade_interessado": "Criança/Adolescente ou Incapaz"
            if any(
                k in classe_proc or k in assunto_proc
                for k in [
                    "alimentos",
                    "guarda",
                    "visita",
                    "paternidade",
                    "interdição",
                    "interdicao",
                    "curatela",
                    "adoção",
                    "adocao",
                ]
            )
            else "Geral",
            "fonte_contexto": (
                "dados_basicos_pje" if basicos else "snapshot_painel_interno"
            ),
        }

    resumo = {
        "numero_cnj": cnj,
        "dados_basicos": rel.get("dados_basicos", {}),
        "partes": rel.get("partes", []),
        "ultimas_movimentacoes": rel.get("movimentacoes", [])[:5],
        "ultima_decisao": {
            "encontrado": decisao.get("encontrado", False),
            "titulo": (
                decisao.get("documento_titulo") or decisao.get("tipo_documento") or ""
            ),
            "id": decisao.get("documento_id", ""),
            "teor_resumido": teor_decisao[:1000],
            "erro": decisao.get("erro"),
            "formato": decisao.get("formato"),
            "paginas": decisao.get("num_paginas"),
            "paginas_sem_texto": decisao.get("paginas_sem_texto", []),
        },
        "pendencias": {
            "total": len(prazos),
            "urgentes_criticos": prazos_urgentes_qtd,
            "expedientes": prazos,
        },
        "sintese_inteligente": {
            "polo_ativo_principal": rel.get("partes", [{}])[0].get(
                "nome", "Não identificado"
            )
            if rel.get("partes")
            else "Não identificado",
            "classe_judicial": rel.get("dados_basicos", {}).get("classe", "N/A"),
            "status_prazos": "CRÍTICO (Existem prazos vencendo/vencidos)"
            if prazos_urgentes_qtd > 0
            else "OK",
            "total_movimentacoes_historicas": len(rel.get("movimentacoes", [])),
            "materia_familia": eh_familia,
            "diagnostico_vara_familia": detalhes_familia,
        },
        "cache_local": cache_info,
        "cobertura": {
            "escopo": (
                "resumo executivo; não equivale à leitura integral de todas "
                "as peças dos autos"
            ),
            "dados_basicos_extraidos": bool(rel.get("dados_basicos")),
            "partes_extraidas": bool(rel.get("partes")),
            "movimentacoes_extraidas": len(rel.get("movimentacoes", [])),
            "teor_ultima_decisao_extraido": bool(teor_decisao),
            "completo_para_analise": not alertas_fidelidade,
            "completo_para_analise_integral": False,
            "alertas": alertas_fidelidade or None,
        },
    }
    return _marcar_grau(resumo, p, g)


# =========================================================================
# BUSCAS POR DIFERENTES CRITERIOS
# =========================================================================


async def buscar_por_nome_parte(
    nome: str, limite: int = 20, persona: str = "advogado", grau: str = "1"
) -> dict:
    """[TJPA 1g|2g] Busca processos pelo NOME de uma parte (autor ou reu)."""
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    return _marcar_grau(await pje.buscar_por_nome_parte(nome, limite), p, g)


async def buscar_por_nome_requerente(
    nome: str, limite: int = 20, persona: str = "advogado", grau: str = "1"
) -> dict:
    """Busca pelo nome e confirma a coincidência no polo ativo."""
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    return _marcar_grau(await pje.buscar_por_nome_requerente(nome, limite), p, g)


async def buscar_por_nome_requerido(
    nome: str, limite: int = 20, persona: str = "advogado", grau: str = "1"
) -> dict:
    """Busca pelo nome e confirma a coincidência no polo passivo."""
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    return _marcar_grau(await pje.buscar_por_nome_requerido(nome, limite), p, g)


async def buscar_por_nome_advogado(
    nome: str, limite: int = 20, persona: str = "advogado", grau: str = "1"
) -> dict:
    """[TJPA 1g|2g] Busca processos pelo NOME do advogado/representante."""
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    return _marcar_grau(await pje.buscar_por_nome_advogado(nome, limite), p, g)


async def buscar_por_outros_nomes(
    nome: str, limite: int = 20, persona: str = "advogado", grau: str = "1"
) -> dict:
    """Busca por outros nomes, nome social ou alcunha cadastrada."""
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    return _marcar_grau(await pje.buscar_por_outros_nomes(nome, limite), p, g)


async def buscar_por_numero_documento(
    numero: str, limite: int = 20, persona: str = "advogado", grau: str = "1"
) -> dict:
    """Busca pelo campo genérico Número do documento da consulta nativa."""
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    return _marcar_grau(
        await pje.buscar_por_numero_documento(numero, limite), p, g
    )


async def buscar_por_criterio_avancado(
    criterio: str,
    valor: Any,
    limite: int = 20,
    persona: str = "advogado",
    grau: str = "1",
) -> dict:
    """Busca nativa por um campo avançado previamente normalizado."""
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    return _marcar_grau(
        await pje.buscar_por_criterio_avancado(criterio, valor, limite), p, g
    )


async def buscar_por_cpf(
    cpf_busca: str, limite: int = 20, persona: str = "advogado", grau: str = "1"
) -> dict:
    """[TJPA 1g|2g] Busca processos pelo CPF de uma das partes."""
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    return _marcar_grau(await pje.buscar_por_cpf(cpf_busca, limite), p, g)


async def buscar_por_cnpj(
    cnpj: str, limite: int = 20, persona: str = "advogado", grau: str = "1"
) -> dict:
    """[TJPA 1g|2g] Busca processos pelo CNPJ de uma das partes."""
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    return _marcar_grau(await pje.buscar_por_cnpj(cnpj, limite), p, g)


async def buscar_por_oab(
    numero_oab: str,
    uf: str = "PI",
    limite: int = 20,
    persona: str = "advogado",
    grau: str = "1",
) -> dict:
    """[TJPA 1g|2g] Busca processos pelo numero OAB do advogado."""
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    return _marcar_grau(await pje.buscar_por_oab(numero_oab, uf.upper(), limite), p, g)


async def buscar_processo_geral(
    identificador: str,
    limite: int = 20,
    persona: str = "advogado",
    grau: str = "1",
) -> dict:
    """Usa o menu lateral Consulta processual sem classificar o valor."""
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    if g != "1g":
        return {
            "erro": (
                "Busca processual geral ainda mapeada positivamente somente no 1º grau"
            ),
            "somente_leitura": True,
        }
    pje = await cliente_singleton.get_cliente(p, g)
    resultado = await pje.buscar_processo_geral(identificador, limite)
    return _marcar_grau(resultado, p, g)


def _extrair_cnjs(resultado: dict) -> list:
    """Colhe os numeros CNJ de qualquer retorno de busca/consulta."""
    if not isinstance(resultado, dict):
        return []
    achados = []
    if resultado.get("numero_cnj"):
        achados.append(_normaliza_cnj(resultado["numero_cnj"]))
    for item in resultado.get("resultados") or []:
        if isinstance(item, dict) and item.get("numero_cnj"):
            achados.append(_normaliza_cnj(item["numero_cnj"]))
    basicos = resultado.get("dados_basicos")
    if isinstance(basicos, dict) and basicos.get("numero_cnj"):
        achados.append(_normaliza_cnj(basicos["numero_cnj"]))
    return [c for c in achados if c]


def _redigir_eco_busca(valor: Any, original: str, mascarado: str) -> Any:
    """Remove o eco do critério bruto sem alterar os CNJs encontrados."""
    if isinstance(valor, dict):
        return {
            chave: _redigir_eco_busca(item, original, mascarado)
            for chave, item in valor.items()
        }
    if isinstance(valor, list):
        return [
            _redigir_eco_busca(item, original, mascarado)
            for item in valor
        ]
    if isinstance(valor, tuple):
        return tuple(
            _redigir_eco_busca(item, original, mascarado)
            for item in valor
        )
    if isinstance(valor, str) and original:
        return valor.replace(original, mascarado)
    return valor


def _mascarar_item_busca(valor: str, criterio: str = "auto") -> str:
    """Infere somente o necessário para redigir um item de busca."""
    criterio_normalizado = _slug_acao(criterio)
    if criterio_normalizado in {"", "auto"}:
        criterio_normalizado = _detectar_tipo_busca(valor)["acao"]
    if criterio_normalizado == "consultar_numero":
        criterio_normalizado = "numero_cnj"
    if criterio_normalizado == "numero_cnj":
        return _mascarar_cnj(valor)
    return mask_process_search_value(criterio_normalizado, valor)


async def buscar_em_lote(
    valores: str,
    acao: str = "auto",
    limite: int = 20,
    uf_oab: str = UF_OAB_PADRAO,
    max_itens: int = 25,
    persona: str = "advogado",
    grau: str = "1",
) -> dict:
    """[TJPA 1g|2g] Roda varias buscas numa tacada e consolida o resultado.

    'valores' aceita uma lista separada por virgula, ponto-e-virgula ou
    quebra de linha (CNJs, CPFs, CNPJs, OABs ou nomes, misturados). Com
    acao='auto' cada item e classificado pelo formato; com uma acao fixa,
    todos os itens usam o mesmo criterio.

    As buscas rodam em serie de proposito: o cliente PJe e um browser
    singleton, entao paralelizar so embaralharia a mesma aba.
    """
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)

    itens = [v.strip() for v in re.split(r"[,;\n]+", str(valores or "")) if v.strip()]
    if not itens:
        return {
            "erro": "Nenhum valor informado para a busca em lote.",
            "dica": "Separe os itens por vírgula, ponto-e-vírgula ou quebra de linha.",
        }

    limite = max(1, min(int(limite or 20), 100))
    max_itens = max(1, min(int(max_itens or 25), 50))
    ignorados = itens[max_itens:]
    itens = itens[:max_itens]

    por_item = []
    indice_cnj = {}
    for valor in itens:
        detec = (
            _detectar_tipo_busca(valor)
            if _slug_acao(acao) in ("auto", "")
            else {
                "acao": _slug_acao(acao),
                "motivo": "critério fixado pelo chamador",
                "valor_normalizado": valor,
            }
        )
        valor_mascarado = _mascarar_item_busca(valor, detec["acao"])
        registro = {
            # Alias de transição: preserva consumidores do contrato anterior
            # sem reintroduzir o critério bruto no payload.
            "valor": valor_mascarado,
            "valor_mascarado": valor_mascarado,
            "criterio": detec["acao"],
            "motivo_deteccao": detec["motivo"],
        }
        try:
            resultado = await _executar_busca(
                detec["acao"],
                detec.get("valor_normalizado") or valor,
                p,
                g,
                limite,
                uf_oab,
            )
            if isinstance(resultado, dict) and resultado.get("erro"):
                registro["status"] = "erro"
                registro["erro"] = _redigir_eco_busca(
                    resultado["erro"],
                    valor,
                    valor_mascarado,
                )
            else:
                cnjs = _extrair_cnjs(resultado)
                zero_confirmado = bool(
                    isinstance(resultado, dict)
                    and resultado.get("sem_resultado_confirmado") is True
                )
                inconclusivo = not cnjs and not zero_confirmado
                registro["status"] = (
                    "ok"
                    if cnjs
                    else "sem_resultado"
                    if zero_confirmado
                    else "inconclusivo"
                )
                registro["inconclusive"] = inconclusivo
                registro["sem_resultado_confirmado"] = zero_confirmado
                registro["total_encontrados"] = resultado.get(
                    "total_encontrados", len(cnjs)
                )
                registro["numeros_cnj"] = cnjs
                for cnj in cnjs:
                    indice_cnj.setdefault(cnj, []).append(valor_mascarado)
            registro["resultado"] = _redigir_eco_busca(
                resultado,
                valor,
                valor_mascarado,
            )
        except Exception as e:
            registro["status"] = "erro"
            registro["erro"] = _redigir_eco_busca(
                f"{type(e).__name__}: {e}"[:300],
                valor,
                valor_mascarado,
            )
        por_item.append(registro)

    coincidencias = {c: v for c, v in indice_cnj.items() if len(set(v)) > 1}
    resumo = {
        "itens_consultados": len(itens),
        "com_resultado": sum(1 for r in por_item if r["status"] == "ok"),
        "sem_resultado": sum(1 for r in por_item if r["status"] == "sem_resultado"),
        "inconclusivos": sum(1 for r in por_item if r["status"] == "inconclusivo"),
        "com_erro": sum(1 for r in por_item if r["status"] == "erro"),
        "processos_distintos": len(indice_cnj),
    }
    saida = {
        "resumo": resumo,
        "processos_distintos": sorted(indice_cnj),
        "coincidencias": coincidencias,
        "por_item": por_item,
    }
    if ignorados:
        saida["itens_ignorados_mascarados"] = [
            _mascarar_item_busca(item, acao) for item in ignorados
        ]
        saida["aviso"] = (
            f"{len(ignorados)} item(ns) além do teto de {max_itens} não foram consultados."
        )
    return _marcar_grau(saida, p, g)


# =========================================================================
# DOCUMENTOS
# =========================================================================


def _propagar_arvore(res: dict, docs_resp: dict) -> dict:
    """Repassa o sinal de árvore incompleta para quem consome a listagem.

    Sem isso, uma acao derivada (filtrar, indice, historico) devolvia numeros
    que pareciam definitivos em cima de uma arvore truncada.
    """
    completa = docs_resp.get("arvore_completa") is True
    res["arvore_completa"] = completa
    if not completa:
        res.setdefault("status", "partial")
        res["aviso_arvore"] = docs_resp.get(
            "aviso",
            "A árvore de documentos pode estar incompleta — trate os totais como piso.",
        )
    return res


async def pecas_recentes_processo(
    numero_cnj: str,
    limite: int = 10,
    tipo_filtro: str = "",
    persona: str = "advogado",
    grau: str = "1",
) -> dict:
    """[TJPA 1g|2g] Ultimas pecas juntadas aos autos, da mais nova para a mais antiga.

    Responde "o que entrou no processo desde a ultima vez que olhei" sem ler o
    teor de nada. A ordem vem do id do documento, que no PJe e' sequencial -
    confiavel mesmo quando a data nao aparece no rotulo da arvore.

    - limite: quantas pecas retornar (0 = todas).
    - tipo_filtro: opcional, restringe por tipo/titulo (ex: 'peticao', 'decisao').
    """
    cnj = _normaliza_cnj(numero_cnj)
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)

    docs_resp = await pje.listar_documentos(cnj)
    docs = docs_resp.get("documentos", [])

    if tipo_filtro:
        alvo = tipo_filtro.lower().strip()
        docs = [
            d
            for d in docs
            if alvo in str(d.get("tipo", "")).lower()
            or alvo in str(d.get("titulo", "")).lower()
        ]

    docs = sorted(
        docs,
        key=lambda d: int(d["id"]) if str(d.get("id", "")).isdigit() else 0,
        reverse=True,
    )
    total_filtrado = len(docs)
    if limite and limite > 0:
        docs = docs[:limite]

    res = {
        "numero_cnj": cnj,
        "total_documentos_processo": docs_resp.get("total", 0),
        "total_apos_filtro": total_filtrado,
        "retornados": len(docs),
        "tipo_filtro": tipo_filtro or None,
        "ordem": "mais recente primeiro (por id sequencial do PJe)",
        "pecas": [
            {
                "documento_id": str(d.get("id", "")),
                "tipo": d.get("tipo", ""),
                "titulo": d.get("titulo", "") or d.get("tipo", ""),
                "data": d.get("data", "") or "N/I",
            }
            for d in docs
        ],
    }
    if total_filtrado > len(docs):
        res["aviso"] = (
            f"{total_filtrado} peças atendem ao critério, {len(docs)} retornadas "
            f"por limite={limite}."
        )
    return _propagar_arvore(_marcar_grau(res, p, g), docs_resp)


async def listar_documentos(
    numero_cnj: str, persona: str = "advogado", grau: str = "1"
) -> dict:
    """[TJPA 1g|2g] Lista todos os documentos do processo (id + tipo)."""
    cnj = _normaliza_cnj(numero_cnj)
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    return _marcar_grau(await pje.listar_documentos(cnj), p, g)


async def filtrar_documentos_processo(
    numero_cnj: str, termo: str, persona: str = "advogado", grau: str = "1"
) -> dict:
    """[TJPA 1g|2g] Filtra a lista de documentos do processo por palavra-chave ou tipo.

    Exemplos de termo: 'contestação', 'inicial', 'laudo', 'decisão', 'procuração'.
    """
    cnj = _normaliza_cnj(numero_cnj)
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    docs_resp = await pje.listar_documentos(cnj)

    docs = docs_resp.get("documentos", [])
    termo_norm = termo.lower().strip()
    filtrados = []

    for d in docs:
        titulo = str(d.get("titulo", "")).lower()
        tipo = str(d.get("tipo", "")).lower()
        id_doc = str(d.get("id", "")).lower()
        if termo_norm in titulo or termo_norm in tipo or termo_norm == id_doc:
            filtrados.append(d)

    res = {
        "numero_cnj": cnj,
        "termo_busca": termo,
        "total_encontrados": len(filtrados),
        "total_documentos_processo": len(docs),
        "documentos": filtrados,
    }
    return _propagar_arvore(_marcar_grau(res, p, g), docs_resp)


def _teor_texto(resposta: dict) -> str:
    """Extrai o teor textual de uma resposta de leitura de documento.

    O pje_client devolve a chave 'texto'; varios pontos do server liam
    'conteudo', que nunca existiu — a busca textual e a classificacao de
    decisao viviam recebendo string vazia e falhando em silencio. Aceita as
    duas chaves pra nao depender de qual lado for ajustado depois.
    """
    if not isinstance(resposta, dict):
        return ""
    for chave in ("texto", "conteudo", "teor"):
        valor = resposta.get(chave)
        if valor:
            return str(valor)
    return ""


async def pesquisar_autos_texto(
    numero_cnj: str,
    termo: str,
    max_documentos: int = 10,
    max_paginas: int = 30,
    tempo_maximo_segundos: float = 45,
    persona: str = "advogado",
    grau: str = "1",
) -> dict:
    """[TJPA 1g|2g] Realiza busca textual profunda no TEOR dos documentos do processo.

    Busca o termo dentro do conteúdo dos primeiros N documentos e retorna os trechos
    exatos (snippets) onde a palavra-chave aparece.
    """
    cnj = _normaliza_cnj(numero_cnj)
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)

    lote = await pje.ler_documentos_em_lote(
        cnj,
        max_documentos=max_documentos,
        max_paginas=max_paginas,
        tempo_maximo_segundos=tempo_maximo_segundos,
    )

    termo_norm = termo.lower().strip()
    resultados = []
    falhas = list(lote.get("falhas", []))

    for item in lote.get("leituras", []):
        d = item.get("documento", {})
        teor_resp = item.get("teor", {})
        doc_id = str(d.get("id", teor_resp.get("id_documento", "")))
        titulo = d.get("titulo", "")
        tipo = d.get("tipo", "")
        texto = _teor_texto(teor_resp)
        if termo_norm in texto.lower():
            snippets = []
            for match in re.finditer(re.escape(termo_norm), texto, re.IGNORECASE):
                start = max(0, match.start() - 100)
                end = min(len(texto), match.end() + 100)
                snippet = texto[start:end].replace("\n", " ").strip()
                snippets.append(f"... {snippet} ...")
                if len(snippets) >= 3:
                    break

            resultados.append(
                {
                    "documento_id": doc_id,
                    "titulo": titulo,
                    "tipo": tipo,
                    "total_ocorrencias": texto.lower().count(termo_norm),
                    "trechos_encontrados": snippets,
                }
            )

    planejados = int(lote.get("documentos_planejados", 0))
    tentados = int(
        lote.get("documentos_tentados", len(lote.get("leituras", [])) + len(falhas))
    )
    lidos = len(lote.get("leituras", []))
    return _propagar_arvore(
        _marcar_grau(
            {
                "numero_cnj": cnj,
                "termo_busca": termo,
                "documentos_planejados": planejados,
                "documentos_analisados": tentados,
                "documentos_lidos_com_sucesso": lidos,
                "documentos_com_falha": len(falhas),
                "documentos_nao_tentados": lote.get("documentos_nao_tentados", 0),
                "falhas": falhas,
                "cobertura_percentual": (
                    round(100 * lidos / planejados, 2) if planejados else 100.0
                ),
                "busca_concluida": lote.get("busca_concluida", False),
                "motivo_interrupcao": lote.get("motivo_interrupcao"),
                "tempo_maximo_segundos": lote.get("tempo_maximo_segundos"),
                "tempo_decorrido_segundos": lote.get("tempo_decorrido_segundos"),
                "total_documentos_com_ocorrencia": len(resultados),
                "resultados": resultados,
            },
            p,
            g,
        ),
        lote,
    )


async def gerar_indice_remissivo_autos(
    numero_cnj: str, persona: str = "advogado", grau: str = "1"
) -> dict:
    """[TJPA 1g|2g] Gera um índice remissivo e tabela de conteúdos dos documentos dos autos."""
    cnj = _normaliza_cnj(numero_cnj)
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)

    pje = await cliente_singleton.get_cliente(p, g)
    docs_resp = await pje.listar_documentos(cnj)
    docs = docs_resp.get("documentos", [])

    categorias = {
        "Petição Inicial & Emendas": [],
        "Contestações & Defesas": [],
        "Decisões & Sentenças": [],
        "Provas & Laudos": [],
        "Certidões & Termos": [],
        "Outros Documentos": [],
    }

    for d in docs:
        t = str(d.get("titulo", "")).lower()
        tp = str(d.get("tipo", "")).lower()
        item = {
            "id": d.get("id"),
            "titulo": d.get("titulo"),
            "tipo": d.get("tipo"),
            "data": d.get("data", "N/I"),
        }

        if any(k in t or k in tp for k in ["inicial", "emenda"]):
            categorias["Petição Inicial & Emendas"].append(item)
        elif any(
            k in t or k in tp
            for k in ["contestação", "defesa", "resposta", "reconvenção"]
        ):
            categorias["Contestações & Defesas"].append(item)
        elif any(
            k in t or k in tp for k in ["decisão", "sentença", "despacho", "acórdão"]
        ):
            categorias["Decisões & Sentenças"].append(item)
        elif any(
            k in t or k in tp
            for k in ["laudo", "perícia", "documento", "comprovante", "anexo"]
        ):
            categorias["Provas & Laudos"].append(item)
        elif any(k in t or k in tp for k in ["certidão", "termo", "intimação"]):
            categorias["Certidões & Termos"].append(item)
        else:
            categorias["Outros Documentos"].append(item)

    resumo_cats = {k: len(v) for k, v in categorias.items() if v}

    return _propagar_arvore(
        _marcar_grau(
            {
                "numero_cnj": cnj,
                "total_documentos": len(docs),
                "resumo_por_categoria": resumo_cats,
                "indice_remissivo": categorias,
            },
            p,
            g,
        ),
        docs_resp,
    )


async def ler_documento(
    numero_cnj: str,
    id_documento: str,
    max_paginas: int = 30,
    persona: str = "advogado",
    grau: str = "1",
) -> dict:
    """[TJPA 1g|2g] Le o teor completo de um documento (HTML ou PDF).

    PDFs com mais de max_paginas paginas voltam truncados, com truncado=True
    e aviso explicito no retorno.
    """
    cnj = _normaliza_cnj(numero_cnj)
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    return _marcar_grau(
        await pje.ler_documento(cnj, str(id_documento), max_paginas), p, g
    )


async def historico_decisorio(
    numero_cnj: str,
    limite: int = 20,
    incluir_teor: bool = False,
    max_paginas: int = 30,
    persona: str = "advogado",
    grau: str = "1",
) -> dict:
    """[TJPA 1g|2g] Trilha decisoria completa do processo, do mais recente ao mais antigo.

    Enquanto 'ultima_decisao' devolve so o ultimo ato, aqui vem TODA a sequencia
    de decisoes/sentencas/despachos/acordaos com id, titulo, tipo e data - dando
    a evolucao do processo numa unica chamada.

    - limite: teto de atos decisorios retornados (0 = todos).
    - incluir_teor: True le o teor de cada ato e classifica juridicamente
      (Procedencia, Improcedencia, Tutela Deferida...). Custa 1 requisicao por
      documento - use limite baixo. False (default) so lista, sem ler.
    """
    cnj = _normaliza_cnj(numero_cnj)
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)

    docs_resp = await pje.listar_documentos(cnj)
    docs = docs_resp.get("documentos", [])
    rx = re.compile(_PADRAO_DECISORIO, re.IGNORECASE)
    decisorios = [
        d for d in docs if rx.search(f"{d.get('titulo', '')} {d.get('tipo', '')}")
    ]
    # IDs do PJe sao sequenciais: maior id = ato mais recente.
    decisorios.sort(
        key=lambda d: int(d["id"]) if str(d.get("id", "")).isdigit() else 0,
        reverse=True,
    )
    total_encontrados = len(decisorios)
    if limite and limite > 0:
        decisorios = decisorios[:limite]

    trilha = []
    for d in decisorios:
        item = {
            "documento_id": str(d.get("id", "")),
            "titulo": d.get("titulo", ""),
            "tipo": d.get("tipo", ""),
            "data": d.get("data", "N/I"),
        }
        if incluir_teor:
            try:
                # _teor_texto: o pje_client devolve 'texto', nao 'conteudo' —
                # ler a chave errada classificava todo ato como "OUTRO".
                teor = _teor_texto(
                    await pje.ler_documento(cnj, str(d.get("id", "")), max_paginas)
                )
                categoria, frases = _classificar_texto_decisao(teor)
                item.update(
                    categoria_juridica=categoria,
                    frases_chave_detectadas=frases,
                    tamanho_teor_caracteres=len(teor),
                    teor_resumido=teor[:800],
                )
            except Exception as e:
                item["erro_leitura"] = f"{type(e).__name__}: {e}"
        trilha.append(item)

    res = {
        "numero_cnj": cnj,
        "total_documentos_processo": len(docs),
        "total_atos_decisorios": total_encontrados,
        "retornados": len(trilha),
        "teor_lido": incluir_teor,
        "trilha_decisoria": trilha,
    }
    if total_encontrados > len(trilha):
        res["aviso"] = (
            f"{total_encontrados} atos decisórios no processo, {len(trilha)} "
            f"retornados por limite={limite}. Aumente 'limite' para ver o resto."
        )
    if not incluir_teor and trilha:
        res["dica"] = (
            "Use incluir_teor=True (com limite baixo) para ler e classificar "
            "juridicamente cada ato."
        )
    return _propagar_arvore(_marcar_grau(res, p, g), docs_resp)


# =========================================================================
# CITACOES LEGAIS
# =========================================================================

DIPLOMAS_LEGAIS = {
    "CPC": "Código de Processo Civil",
    "CC": "Código Civil",
    "CF": "Constituição Federal",
    "CRFB": "Constituição Federal",
    "CLT": "Consolidação das Leis do Trabalho",
    "CDC": "Código de Defesa do Consumidor",
    "CP": "Código Penal",
    "CPP": "Código de Processo Penal",
    "CTN": "Código Tributário Nacional",
    "CTB": "Código de Trânsito Brasileiro",
    "ECA": "Estatuto da Criança e do Adolescente",
    "LINDB": "Lei de Introdução às Normas do Direito Brasileiro",
}

_RE_ARTIGO = re.compile(
    r"\bart(?:igo)?s?\s*\.?\s*(\d{1,4}(?:[-\.]?[A-Z])?)\s*[ºo°]?", re.IGNORECASE
)
_RE_DIPLOMA = re.compile(
    r"\b(" + "|".join(sorted(DIPLOMAS_LEGAIS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
_RE_SUMULA = re.compile(
    r"\bs[úu]mula\s*(vinculante)?\s*(?:n?[ºo°\.]*\s*)?(\d{1,4})"
    r"(?:[^\n]{0,20}?\b(STF|STJ|TST|TJPA|TNU))?",
    re.IGNORECASE,
)
_RE_TEMA = re.compile(
    r"\btema\s*(?:n?[ºo°\.]*\s*)?(\d{1,4}(?:\.\d{3})?)"
    r"(?:[^\n]{0,25}?\b(STF|STJ))?",
    re.IGNORECASE,
)
_RE_PRECEDENTE = re.compile(
    r"\b(REsp|AREsp|AgRg|AgInt|EDcl|RE|ARE|HC|RHC|RMS|MS|ADI|ADPF|ADC|IRDR)"
    r"\s*(?:n?[ºo°\.]*\s*)?(\d[\d\.]{2,})(?:\s*/\s*([A-Z]{2}))?",
    re.IGNORECASE,
)
_RE_LEI = re.compile(
    r"\b((?:Decreto[-\s]?)?Lei(?:\s+Complementar)?)\s*(?:n?[ºo°\.]*\s*)?"
    r"(\d[\d\.]*)\s*/\s*(\d{2,4})",
    re.IGNORECASE,
)


def _trecho(texto: str, inicio: int, fim: int, margem: int = 90) -> str:
    """Recorta o texto ao redor de um match, em uma linha."""
    ini = max(0, inicio - margem)
    f = min(len(texto), fim + margem)
    return re.sub(r"\s+", " ", texto[ini:f]).strip()


def _extrair_citacoes(texto: str) -> dict:
    """Extrai referencias juridicas de um teor: artigos, sumulas, temas,
    precedentes e leis.

    Funcao pura — nao toca o PJe. Cada citacao volta normalizada, com a
    contagem de ocorrencias e um trecho de contexto da primeira aparicao.
    """
    texto = str(texto or "")
    achados = {
        "artigos": {},
        "sumulas": {},
        "temas_repetitivos": {},
        "precedentes": {},
        "leis": {},
    }

    def registrar(categoria, rotulo, m, extra=None):
        item = achados[categoria].setdefault(
            rotulo,
            {
                "citacao": rotulo,
                "ocorrencias": 0,
                "trecho": _trecho(texto, m.start(), m.end()),
            },
        )
        item["ocorrencias"] += 1
        if extra:
            item.update(extra)

    for m in _RE_ARTIGO.finditer(texto):
        numero = m.group(1).upper()
        # O diploma costuma vir logo depois ("art. 300 do CPC"); olha adiante
        # uma janela curta pra nao colar o artigo num codigo de outra frase.
        janela = texto[m.end() : m.end() + 60]
        corte = re.search(r"[.;]\s", janela)
        if corte:
            janela = janela[: corte.start()]
        m_dip = _RE_DIPLOMA.search(janela)
        diploma = m_dip.group(1).upper() if m_dip else None
        rotulo = f"art. {numero}" + (f" do {diploma}" if diploma else "")
        registrar(
            "artigos",
            rotulo,
            m,
            {"diploma": diploma, "diploma_extenso": DIPLOMAS_LEGAIS.get(diploma)},
        )

    for m in _RE_SUMULA.finditer(texto):
        vinculante = bool(m.group(1))
        numero, corte = m.group(2), (m.group(3) or "").upper()
        rotulo = ("Súmula Vinculante " if vinculante else "Súmula ") + numero
        if corte and not vinculante:
            rotulo += f" do {corte}"
        registrar(
            "sumulas", rotulo, m, {"vinculante": vinculante, "tribunal": corte or None}
        )

    for m in _RE_TEMA.finditer(texto):
        tribunal = (m.group(2) or "").upper()
        rotulo = f"Tema {m.group(1)}" + (f" do {tribunal}" if tribunal else "")
        registrar("temas_repetitivos", rotulo, m, {"tribunal": tribunal or None})

    for m in _RE_PRECEDENTE.finditer(texto):
        classe = m.group(1).upper().replace("RESP", "REsp").replace("ARESP", "AREsp")
        classe = {"AGRG": "AgRg", "AGINT": "AgInt", "EDCL": "EDcl"}.get(
            m.group(1).upper(), classe
        )
        uf = (m.group(3) or "").upper()
        rotulo = f"{classe} {m.group(2)}" + (f"/{uf}" if uf else "")
        registrar("precedentes", rotulo, m, {"classe": classe, "uf": uf or None})

    for m in _RE_LEI.finditer(texto):
        especie = (
            re.sub(r"\s+", " ", m.group(1))
            .title()
            .replace("Lei Complementar", "Lei Complementar")
        )
        rotulo = f"{especie} {m.group(2)}/{m.group(3)}"
        registrar("leis", rotulo, m)

    por_categoria = {
        cat: sorted(itens.values(), key=lambda i: -i["ocorrencias"])
        for cat, itens in achados.items()
    }
    total = sum(len(v) for v in por_categoria.values())
    diplomas = sorted(
        {i["diploma"] for i in por_categoria["artigos"] if i.get("diploma")}
    )
    return {
        "total_citacoes_distintas": total,
        "diplomas_citados": diplomas,
        "por_categoria": por_categoria,
    }


async def extrair_citacoes_legais(
    numero_cnj: str,
    id_documento: str = "",
    max_paginas: int = 30,
    persona: str = "advogado",
    grau: str = "1",
) -> dict:
    """[TJPA 1g|2g] Mapeia a base legal invocada num documento dos autos.

    Sem id_documento, le a ultima decisao/sentenca do processo. Devolve
    artigos, sumulas, temas repetitivos, precedentes e leis citados, com
    contagem e trecho de contexto.
    """
    cnj = _normaliza_cnj(numero_cnj)
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)

    if id_documento:
        doc = await pje.ler_documento(cnj, str(id_documento), max_paginas)
        origem = f"documento {id_documento}"
    else:
        doc = await pje.ler_documento_filtrado(
            cnj, r"(decis[ãa]o|senten[çc]a|ac[óo]rd[ãa]o|despacho)", max_paginas
        )
        origem = "última decisão/sentença dos autos"
        if not doc.get("encontrado"):
            return _marcar_grau(
                {
                    "numero_cnj": cnj,
                    "extraido": False,
                    "mensagem": "Nenhuma decisão/sentença encontrada para extrair citações.",
                    "dica": "Passe id_doc para analisar uma peça específica.",
                },
                p,
                g,
            )

    texto = _teor_texto(doc)
    if not texto.strip():
        return _marcar_grau(
            {
                "numero_cnj": cnj,
                "extraido": False,
                "mensagem": f"O teor de {origem} veio vazio (documento pode ser imagem sem OCR).",
                "documento_id": doc.get("documento_id") or id_documento,
            },
            p,
            g,
        )

    citacoes = _extrair_citacoes(texto)
    resultado = {
        "numero_cnj": cnj,
        "extraido": True,
        "origem": origem,
        "documento_id": doc.get("documento_id") or id_documento,
        "tipo_documento": doc.get("tipo_documento") or doc.get("documento_titulo"),
        "caracteres_analisados": len(texto),
        **citacoes,
    }
    if doc.get("truncado"):
        resultado["aviso"] = (
            "Teor truncado na leitura: as citações refletem apenas as "
            f"{doc.get('paginas_extraidas', max_paginas)} primeiras páginas. "
            "Aumente 'limite' para cobrir o documento inteiro."
        )
    return _marcar_grau(resultado, p, g)


# =========================================================================
# CONSULTA PONTUAL (sem download)
# =========================================================================


async def ultima_decisao(
    numero_cnj: str, persona: str = "advogado", grau: str = "1"
) -> dict:
    """
    [TJPA 1g|2g] Le o teor da ultima decisao/sentenca/despacho/ato ordinatorio.

    Abre os autos UMA vez, filtra os decisorios, escolhe o de maior ID
    (mais recente) e devolve o teor completo. NAO baixa o processo todo.
    """
    cnj = _normaliza_cnj(numero_cnj)
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    r = await pje.ler_documento_filtrado(
        cnj,
        r"(decis[ãa]o|senten[çc]a|despacho|ato\s+ordinat[óo]rio)",
    )
    if not r.get("encontrado"):
        r.setdefault(
            "aviso",
            "Nenhuma decisao/sentenca/despacho encontrado nos documentos listados.",
        )
        r["numero_cnj"] = cnj
    return _marcar_grau(r, p, g)


async def ultimo_despacho(
    numero_cnj: str, persona: str = "advogado", grau: str = "1"
) -> dict:
    """[TJPA 1g|2g] Le o teor do ultimo despacho (so despacho, nao decisao)."""
    cnj = _normaliza_cnj(numero_cnj)
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    r = await pje.ler_documento_filtrado(cnj, r"despacho")
    if not r.get("encontrado"):
        r.setdefault("aviso", "Nenhum despacho encontrado.")
        r["numero_cnj"] = cnj
    return _marcar_grau(r, p, g)


_REGRAS_CLASSIFICACAO = [
    (
        "SUSPENSÃO DO PROCESSO E DO PRAZO PRESCRICIONAL (ART. 366 CPP)",
        "decreto a suspensão do processo e do prazo prescricional",
        [
            "decreto a suspensão do processo",
            "decreto a suspensao do processo",
            "suspensão do processo e do lapso prescricional",
            "suspensao do processo e do lapso prescricional",
            "suspender o processo e o curso do prazo prescricional",
            "art. 366 do cpp",
            "artigo 366 do cpp",
        ],
    ),
    (
        "PROCEDÊNCIA TOTAL",
        "julgo procedente / acolho o pedido",
        ["julgo procedente", "concedo a ordem", "acolho o pedido"],
    ),
    (
        "PROCEDÊNCIA PARCIAL",
        "julgo parcialmente procedente",
        ["julgo parcialmente procedente", "parcial procedência"],
    ),
    (
        "IMPROCEDÊNCIA",
        "julgo improcedente / rejeito o pedido",
        ["julgo improcedente", "denego a ordem", "rejeito o pedido"],
    ),
    (
        "EXTINÇÃO SEM RESOLUÇÃO DO MÉRITO (ART. 485 CPC)",
        "extingo o processo sem resolução do mérito",
        ["extingo o processo sem resolução", "art. 485", "extinto sem julgamento"],
    ),
    (
        "TUTELA DE URGÊNCIA DEFERIDA",
        "defiro a tutela / concedo a liminar",
        [
            "defiro a tutela",
            "concedo a liminar",
            "defiro o pedido de liminar",
            "antecipação dos efeitos",
        ],
    ),
    (
        "TUTELA DE URGÊNCIA INDEFERIDA",
        "indefiro a tutela / liminar",
        ["indeferido a tutela", "indefiro a liminar", "indefiro o pedido liminar"],
    ),
    (
        "INTIMAÇÃO PARA DILIGÊNCIA / PROVAS",
        "intime-se para manifestação/provas",
        ["intime-se", "manifeste-se", "especificarem provas"],
    ),
]

# Padrao dos documentos decisorios (usado por ultima_decisao e historico_decisorio)
_PADRAO_DECISORIO = (
    r"(decis[ãa]o|senten[çc]a|despacho|ato\s+ordinat[óo]rio|ac[óo]rd[ãa]o)"
)


def _classificar_texto_decisao(teor: str) -> tuple:
    """Classifica o resultado juridico pelo teor. Retorna (categoria, frases_chave).

    Extraido de classificar_teor_decisao pra ser reusado pelo historico_decisorio.
    A ordem das regras reproduz exatamente o if/elif original - 'parcialmente
    procedente' vem depois de 'procedente' mas nao e' mascarado por ele, ja que
    a string "julgo parcialmente procedente" nao contem "julgo procedente".
    """
    t = (teor or "").lower()
    for categoria, frase, gatilhos in _REGRAS_CLASSIFICACAO:
        if any(k in t for k in gatilhos):
            return categoria, [frase]
    return "OUTRO / DESPACHO ORDINÁRIO", []


async def classificar_teor_decisao(
    numero_cnj: str, persona: str = "advogado", grau: str = "1"
) -> dict:
    """[TJPA 1g|2g] Classifica o resultado jurídico da última decisão/sentença (Procedente, Improcedente, Tutela Deferida...)."""
    cnj = _normaliza_cnj(numero_cnj)
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)

    pje = await cliente_singleton.get_cliente(p, g)
    dec = await pje.ler_documento_filtrado(cnj, r"(decis[ãa]o|senten[çc]a|despacho)")

    if not dec.get("encontrado"):
        return _marcar_grau(
            {
                "numero_cnj": cnj,
                "classificado": False,
                "mensagem": "Nenhuma decisão encontrada.",
            },
            p,
            g,
        )

    teor = _teor_texto(dec).lower()
    titulo = str(dec.get("documento_titulo", ""))
    categoria, frases_chave = _classificar_texto_decisao(teor)

    return _marcar_grau(
        {
            "numero_cnj": cnj,
            "documento_id": dec.get("documento_id"),
            "titulo_documento": titulo,
            "categoria_juridica": categoria,
            "frases_chave_detectadas": frases_chave,
            "tamanho_teor_caracteres": len(teor),
            "teor_resumido": teor[:800],
        },
        p,
        g,
    )


async def pendencias_processo(
    numero_cnj: str, persona: str = "advogado", grau: str = "1"
) -> dict:
    """
    [TJPA 1g|2g] Retorna pendencias (expedientes + prazos) de UM processo.

    Filtra os expedientes pendentes pelo numero do processo.
    """
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    r = await pje.expedientes_pendentes()

    cnj_normalizado = re.sub(r"\D", "", numero_cnj)
    pendencias = []
    hoje = datetime.now()
    for exp in r.get("expedientes", []):
        n = re.sub(r"\D", "", exp.get("numero_processo", ""))
        if n == cnj_normalizado:
            try:
                dl = datetime.strptime(exp["data_limite"], "%d/%m/%Y %H:%M")
                exp = {**exp, "dias_restantes": (dl - hoje).days}
            except (ValueError, KeyError):
                pass
            pendencias.append(exp)

    return _marcar_grau(
        {
            "numero_cnj": numero_cnj,
            "total_pendencias": len(pendencias),
            "pendencias": pendencias,
        },
        p,
        g,
    )


# =========================================================================
# DOWNLOAD
# =========================================================================


async def expedientes_do_processo(
    numero_cnj: str, persona: str = "advogado", grau: str = "1"
) -> dict:
    """
    [TJPA 1g|2g] Historico COMPLETO de expedientes de UM processo (aba
    Expedientes dos autos): ato, destinatario, via, data de expedicao,
    data da ciencia, prazo e data limite - INCLUSIVE expedientes ja
    fechados/vencidos, que nao aparecem em pendencias_processo.
    """
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    return _marcar_grau(await pje.expedientes_do_processo(numero_cnj), p, g)


async def baixar_documento(
    numero_cnj: str,
    id_documento: str,
    tipo_descritivo: str = "",
    persona: str = "advogado",
    grau: str = "1",
) -> dict:
    """
    [TJPA 1g|2g] Baixa UM documento especifico e salva no iCloud.

    Pasta: ~/Library/Mobile Documents/.../Processos TJPA 1 Grau/{cnj}/documentos/

    - tipo_descritivo: opcional, vai pro nome do arquivo (ex: 'Despacho')
    """
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    # garantir_processo_id (chamado dentro do downloader) abre os autos so
    # se necessario e FECHA a aba - antes a aba ficava vazando no singleton
    r = await pje_downloader.salvar_documento(
        pje, numero_cnj, str(id_documento), tipo_descritivo
    )
    return _marcar_grau(r, p, g)


async def baixar_processo(
    numero_cnj: str,
    metodo: str = "nativo",
    limite: int = 0,
    cronologia: str = "decrescente",
    forcar: bool = False,
    background: bool = True,
    persona: str = "advogado",
    grau: str = "1",
) -> dict:
    """
    [TJPA 1g|2g] Baixa os autos COMPLETOS do processo.

    Salva PDF consolidado em: Processos TJPA 1 Grau/{cnj}/{cnj}.pdf
    (so no metodo nativo; doc_a_doc consolida com sufixo proprio e tambem
    salva individuais em .../documentos/)

    - metodo: 'nativo' (default, completo - servidor PJe consolida com
              capa/indice + expediente + movimentos)
            | 'doc_a_doc' (alternativo - itera arvore de docs e concatena;
              util pra ter arquivos individuais separados; pode nao pegar
              todos os docs em processos muito grandes - lazy-load da arvore)
    - limite: 0=todos | N=baixa so os primeiros N documentos (por cronologia)
              [aplicavel apenas em metodo='doc_a_doc']
    - cronologia: 'decrescente' (default, mais recente primeiro) | 'crescente'
    - forcar: True pra re-baixar mesmo se ja existir cache
    - background: True (default, so vale pro metodo 'nativo') dispara o
              download num task que sobrevive ao timeout do protocolo MCP.
              Processo pequeno resolve na 1a chamada (status='concluido');
              processo grande retorna status='em_andamento' - acompanhe com
              a tool status_download. Use background=False pra forcar a
              chamada sincrona antiga (bloqueia; estoura em autos grandes).

    Retorna dict com 'status': 'concluido' | 'em_andamento' | 'erro'.
    """
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)

    if metodo == "nativo" and background:
        r = await pje_downloader.baixar_processo_background(
            pje, numero_cnj=numero_cnj, cronologia=cronologia, forcar=forcar
        )
        return _marcar_grau(r, p, g)

    r = await pje_downloader.baixar_processo_completo(
        pje,
        numero_cnj=numero_cnj,
        cronologia=cronologia,
        forcar=forcar,
        metodo=metodo,
        limite=limite,
    )
    r.setdefault("status", "concluido")
    return _marcar_grau(r, p, g)


async def status_download(
    numero_cnj: str, persona: str = "advogado", grau: str = "1"
) -> dict:
    """
    [TJPA 1g|2g] Consulta o andamento de um download disparado em
    background por baixar_processo.

    status: 'em_andamento' (servidor ainda gerando/baixando - reconsulte em
    ~30-60s) | 'concluido' (traz caminho + tamanho_mb) | 'erro' | 'inexistente'.

    Nao reabre o browser nem trava - so le o registro de jobs e o disco.
    """
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    return _marcar_grau(pje_downloader.status_download(numero_cnj, g), p, g)


async def preparar_processo(
    numero_cnj: str, forcar: bool = False, persona: str = "advogado", grau: str = "1"
) -> dict:
    """
    [TJPA 1g|2g] Baixa o processo + decide estrategia de analise.

    Retorna:
    - estrategia='pdf_direto' (≤18 MB): Claude le o PDF direto
    - estrategia='ingestao_local_incremental' (>18 MB): manifesto e cache local
    """
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    pje = await cliente_singleton.get_cliente(p, g)
    r = await pje_downloader.preparar_processo_orquestrador(
        pje, numero_cnj, forcar=forcar
    )
    return _marcar_grau(r, p, g)


async def limpar_cache_processos(
    numero_cnj: str = "", apagar_pdfs: bool = False, grau: str = "1"
) -> dict:
    """
    [TJPA 1g|2g] Consulta uso de disco e limpa PDFs/HTMLs baixados.

    - numero_cnj: opcional. Se informado, calcula/limpa apenas este processo.
    - apagar_pdfs: True remove PDFs/HTMLs em cache mantendo as peças geradas (.docx).
    """
    g = _normaliza_grau(grau)
    res = pje_downloader.estatisticas_e_limpeza_storage(
        numero_cnj=numero_cnj.strip() if numero_cnj else None,
        grau=g,
        apagar_pdfs=apagar_pdfs,
    )
    res["grau"] = GRAUS_LEGIVEIS.get(g, g)
    return res


METODOS_DOWNLOAD = {
    "nativo": "nativo",
    "completo": "nativo",
    "consolidado": "nativo",
    "servidor": "nativo",
    "doc_a_doc": "doc_a_doc",
    "documento_a_documento": "doc_a_doc",
    "individual": "doc_a_doc",
    "pecas": "doc_a_doc",
}

CRONOLOGIAS = {
    "decrescente": "decrescente",
    "desc": "decrescente",
    "mais_recente": "decrescente",
    "recente_primeiro": "decrescente",
    "crescente": "crescente",
    "asc": "crescente",
    "mais_antigo": "crescente",
    "antigo_primeiro": "crescente",
}

ORDENACOES_CACHE = {
    "tamanho": "tamanho",
    "size": "tamanho",
    "peso": "tamanho",
    "data": "data",
    "date": "data",
    "recente": "data",
    "cnj": "cnj",
    "numero": "cnj",
    "processo": "cnj",
}


def _resolver_opcao(valor, mapa: dict, nome: str, padrao: str):
    """Normaliza um valor de enum tolerando caixa/acento/hifen.

    Retorna (valor_canonico, None) ou (None, erro). Diferente de deixar o
    valor cru passar: um 'crescente' escrito errado virava silenciosamente a
    ordem padrao, e o chamador recebia a lista invertida sem aviso.
    """
    if valor in (None, ""):
        return padrao, None
    slug = _slug_acao(str(valor))
    if slug in mapa:
        return mapa[slug], None
    sugestao = difflib.get_close_matches(slug, list(mapa), n=1, cutoff=0.6)
    erro = {
        "erro": f"Valor inválido para '{nome}': {valor!r}",
        "valores_validos": sorted(set(mapa.values())),
        "aceita_tambem": sorted(mapa),
    }
    if sugestao:
        erro["voce_quis_dizer"] = mapa[sugestao[0]]
    return None, erro


# Assinaturas de arquivo: um PDF valido comeca por '%PDF-' e termina com
# '%%EOF'. Download interrompido deixa arquivo que EXISTE mas esta cortado —
# e como o cache so checa existencia, ele seria reservido para sempre.
_ASSINATURA_PDF = b"%PDF-"
_FIM_PDF = b"%%EOF"


def _conferir_arquivo(caminho) -> dict:
    """Confere se um arquivo PDF ou HTML em cache esta integro. So le disco."""
    p = Path(caminho)
    info = {"arquivo": p.name, "caminho": str(p)}
    try:
        tamanho = p.stat().st_size
    except OSError as e:
        info.update(integro=False, problema=f"não foi possível ler: {e}")
        return info
    info["tamanho_mb"] = round(tamanho / (1024 * 1024), 2)

    try:
        with p.open("rb") as fh:
            body = fh.read()
    except OSError as e:
        info.update(integro=False, problema=f"falha ao ler: {e}")
        return info

    ext = p.suffix.lower()
    content_type = "application/pdf" if ext == ".pdf" else "text/html"

    integro, motivo = pje_downloader.conferir_conteudo(body, content_type)
    info.update(integro=integro, problema=motivo)
    return info


async def verificar_integridade_cache(numero_cnj: str = "", grau: str = "1") -> dict:
    """[TJPA 1g|2g] Confere se os arquivos (PDF/HTML) em cache estao integros (so disco).

    O cache decide reuso por existencia do arquivo: um download interrompido
    deixa um arquivo cortado que sera reservido indefinidamente, ate alguem pedir
    forcar=True. Esta acao encontra esses arquivos e diz quais re-baixar.
    """
    g = _normaliza_grau(grau)
    cnj = _normaliza_cnj(numero_cnj) if numero_cnj else ""
    if cnj and len(_digitos(cnj)) not in (19, 20):
        return {
            "erro": f"Número CNJ inválido: '{numero_cnj}'",
            "dica": "Omita o CNJ para verificar todo o cache do grau.",
        }

    inventario = pje_downloader.inventario_cache(
        grau=g, numero_cnj=cnj or None, ordenar_por="cnj"
    )
    processos = inventario.get("processos", []) or []

    conferidos, corrompidos = [], []
    for proc in processos:
        num = proc.get("numero_cnj") or proc.get("cnj") or ""
        try:
            pasta = pje_downloader.pasta_processo(num, grau=g, criar=False)
        except Exception as e:
            conferidos.append({"numero_cnj": num, "erro": f"pasta inacessível: {e}"})
            continue
            
        arquivos = []
        if Path(pasta).exists():
            for ext in ("*.pdf", "*.html"):
                arquivos.extend(Path(pasta).rglob(ext))
        arquivos = sorted(arquivos, key=lambda f: f.name)
        
        itens = [_conferir_arquivo(a) for a in arquivos]
        ruins = [i for i in itens if not i.get("integro")]
        if not itens:
            ruins = [
                {
                    "arquivo": None,
                    "integro": False,
                    "problema": "nenhum arquivo PDF ou HTML encontrado",
                }
            ]
        registro = {
            "numero_cnj": num,
            "arquivos_encontrados": len(itens),
            "integros": len(itens) - len(ruins),
            "com_problema": ruins,
        }
        conferidos.append(registro)
        if ruins:
            corrompidos.append(num)

    return {
        "grau": GRAUS_LEGIVEIS.get(g, g),
        "escopo": cnj or "todo o cache do grau",
        "processos_verificados": len(conferidos),
        "processos_com_problema": (
            corrompidos or ([cnj] if cnj and not conferidos else [])
        ),
        "tudo_integro": bool(conferidos) and not corrompidos,
        "nenhum_arquivo_verificado": not conferidos,
        "detalhe": conferidos,
        "como_corrigir": (
            "Re-baixe os processos listados com "
            "download_e_cache_pje(acao='baixar_processo', forcar=True) — sem "
            "forcar=True o arquivo cortado continua sendo reaproveitado."
        )
        if corrompidos or not conferidos
        else None,
    }


async def inventariar_cache_processos(
    numero_cnj: str = "", ordenar_por: str = "tamanho", grau: str = "1"
) -> dict:
    """
    [TJPA 1g|2g] Inventario detalhado do cache local de processos.

    Nao abre o browser nem consulta o PJe - le apenas o disco. Para cada
    processo em cache informa: se os autos consolidados existem, tamanho,
    quantas pecas individuais, quantas minutas/relatorios ja gerados, quando
    foi baixado e se o PDF cabe na leitura direta (<= 18 MB).

    - numero_cnj: opcional, restringe a um processo.
    - ordenar_por: 'tamanho' (default) | 'data' | 'cnj'
    """
    g = _normaliza_grau(grau)
    res = pje_downloader.inventario_cache(
        grau=g,
        numero_cnj=_normaliza_cnj(numero_cnj) if numero_cnj else None,
        ordenar_por=ordenar_por,
    )
    res["grau"] = GRAUS_LEGIVEIS.get(g, g)
    return res


# =========================================================================
# MODELOS E MINUTAS
# =========================================================================


async def listar_modelos_peticao() -> dict:
    """
    [TJPA 1g|2g] Lista modelos de peticao/relatorio em iCloud.

    Pasta: ~/Library/Mobile Documents/com~apple~CloudDocs/Modelos TJPA/
    """
    return modelos.listar_modelos()


async def ler_modelo_peticao(arquivo: str) -> dict:
    """[TJPA 1g|2g] Le o conteudo textual de um modelo (.docx/.md/.txt)."""
    return modelos.ler_modelo(arquivo)


async def pesquisar_modelos_peticao(termo: str) -> dict:
    """[TJPA 1g|2g] Pesquisa uma palavra-chave no nome ou conteúdo dos modelos de petição salvos."""
    return modelos.pesquisar_modelos(termo)


_RX_VARIAVEL_MODELO = re.compile(r"\{[A-Z0-9_ÁÉÍÓÚÂÊÔÃÕÇ]{2,40}\}")


def _mapa_substituicoes(
    cnj: str, dados: dict, autores: list, reus: list, extras: dict | None = None
) -> dict:
    """Monta o mapa {VARIAVEL} -> valor a partir dos metadados do processo."""
    mapa = {
        "{NUMERO_CNJ}": cnj,
        "{CLASSE}": dados.get("classe", "N/I"),
        "{ASSUNTO}": dados.get("assunto", "N/I"),
        "{VARA}": dados.get("vara", "N/I"),
        "{VALOR_CAUSA}": dados.get("valor_causa", "N/I"),
        "{AUTOR}": ", ".join(autores) or "N/I",
        "{REU}": ", ".join(reus) or "N/I",
        "{DATA_ATUAL}": datetime.now().strftime("%d de %B de %Y"),
    }
    for k, v in (extras or {}).items():
        chave = k if k.startswith("{") else f"{{{k}}}"
        mapa[chave] = str(v)
    return mapa


def _aplicar_substituicoes(texto: str, mapa: dict) -> str:
    """Aplica o mapa de variaveis sobre o texto do modelo."""
    for var, val in mapa.items():
        texto = texto.replace(var, val)
    return texto


def _variaveis_nao_resolvidas(texto: str) -> list:
    """Variaveis {ASSIM} que sobraram no texto depois da substituicao.

    Peca protocolada com '{AUTOR}' literal no corpo e' erro grave e silencioso -
    o preview existe justamente pra pegar isso antes de gravar o arquivo.
    """
    return sorted(set(_RX_VARIAVEL_MODELO.findall(texto or "")))


async def duplicar_e_preencher_modelo(
    modelo: str,
    numero_cnj: str,
    substituicoes_extras: dict | None = None,
    persona: str = "advogado",
    grau: str = "1",
) -> dict:
    """[TJPA 1g|2g] Preenche um modelo de petição com metadados do processo e salva na pasta do processo.

    Substitui automaticamente variáveis como {NUMERO_CNJ}, {CLASSE}, {VARA}, {AUTOR}, {REU}, {VALOR_CAUSA}, {DATA_ATUAL}.
    """
    cnj = _normaliza_cnj(numero_cnj)
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)

    m_info = modelos.ler_modelo(modelo)
    texto = m_info.get("conteudo", "")

    pje = await cliente_singleton.get_cliente(p, g)
    rel = await pje.relatorio_processo(cnj)
    dados = rel.get("dados_basicos", {})
    partes = rel.get("partes", [])

    autores = [pt.get("nome", "") for pt in partes if pt.get("polo") == "AUTOR"]
    reus = [pt.get("nome", "") for pt in partes if pt.get("polo") == "REU"]

    mapa = _mapa_substituicoes(cnj, dados, autores, reus, substituicoes_extras)
    texto_preenchido = _aplicar_substituicoes(texto, mapa)

    salvo = minutas.salvar_peca(
        cnj,
        texto_preenchido,
        tipo=f"Minuta_{Path(modelo).stem}",
        formato="docx" if m_info.get("extensao") == ".docx" else "md",
        grau=g,
    )
    salvo["substituicoes_aplicadas"] = mapa
    salvo["variaveis_nao_resolvidas"] = _variaveis_nao_resolvidas(texto_preenchido)
    return _marcar_grau(salvo, p, g)


async def previsualizar_modelo_preenchido(
    modelo: str,
    numero_cnj: str,
    substituicoes_extras: dict | None = None,
    max_caracteres: int = 4000,
    persona: str = "advogado",
    grau: str = "1",
) -> dict:
    """[TJPA 1g|2g] Preenche o modelo com os dados do processo SEM gravar arquivo.

    Mesma substituicao do 'duplicar_preencher', porem em memoria: devolve o
    texto pronto pra conferencia e, principalmente, a lista de variaveis que
    NAO foram resolvidas. Rode isto antes de gravar - peca com '{AUTOR}'
    literal no corpo passa despercebida ate o protocolo.
    """
    cnj = _normaliza_cnj(numero_cnj)
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)

    m_info = modelos.ler_modelo(modelo)
    texto = m_info.get("conteudo", "")

    pje = await cliente_singleton.get_cliente(p, g)
    rel = await pje.relatorio_processo(cnj)
    dados = rel.get("dados_basicos", {})
    partes = rel.get("partes", [])
    autores = [pt.get("nome", "") for pt in partes if pt.get("polo") == "AUTOR"]
    reus = [pt.get("nome", "") for pt in partes if pt.get("polo") == "REU"]

    mapa = _mapa_substituicoes(cnj, dados, autores, reus, substituicoes_extras)
    preenchido = _aplicar_substituicoes(texto, mapa)
    pendentes = _variaveis_nao_resolvidas(preenchido)

    res = {
        "numero_cnj": cnj,
        "modelo": modelo,
        "gravado": False,
        "substituicoes_aplicadas": mapa,
        "variaveis_nao_resolvidas": pendentes,
        "pronto_para_gravar": not pendentes,
        "tamanho_caracteres": len(preenchido),
        "texto_preenchido": preenchido[:max_caracteres],
        "texto_truncado": len(preenchido) > max_caracteres,
    }
    if pendentes:
        res["aviso"] = (
            f"{len(pendentes)} variável(is) sem valor: {', '.join(pendentes)}. "
            f"Passe-as em 'parametros' (JSON) antes de gravar com "
            f"acao='duplicar_preencher'."
        )
    return _marcar_grau(res, p, g)


async def salvar_peticao_processo(
    numero_cnj: str,
    conteudo: str,
    tipo: str = "Petição",
    formato: str = "docx",
    grau: str = "1",
) -> dict:
    """
    [TJPA 1g|2g] Salva uma peticao na pasta do processo.

    - tipo: 'Petição', 'Manifestação', 'Embargos', 'Recurso', 'Contestação'
    - formato: 'docx' (default) | 'md' | 'txt'
    """
    return minutas.salvar_peca(
        numero_cnj, conteudo, tipo=tipo, formato=formato, grau=_normaliza_grau(grau)
    )


async def salvar_relatorio_processo(
    numero_cnj: str,
    conteudo: str,
    formato: str = "docx",
    grau: str = "1",
) -> dict:
    """[TJPA 1g|2g] Salva um relatorio de analise na pasta do processo."""
    cnj = _normaliza_cnj(numero_cnj)
    return minutas.salvar_peca(
        cnj, conteudo, tipo="Relatório", formato=formato, grau=_normaliza_grau(grau)
    )


async def ler_minuta_processo(
    numero_cnj: str, arquivo: str, max_caracteres: int = 20000, grau: str = "1"
) -> dict:
    """[TJPA 1g|2g] Le de volta o texto de uma peca ja gravada na pasta do processo."""
    cnj = _normaliza_cnj(numero_cnj)
    g = _normaliza_grau(grau)
    res = minutas.ler_minuta(cnj, arquivo, g, max_caracteres=max_caracteres)
    res["grau"] = GRAUS_LEGIVEIS.get(g, g)
    return res


async def listar_minutas_processo(numero_cnj: str, grau: str = "1") -> dict:
    """[TJPA 1g|2g] Lista todas as peças e relatórios (.docx/.md/.txt) criados na pasta do processo."""
    cnj = _normaliza_cnj(numero_cnj)
    g = _normaliza_grau(grau)
    res = minutas.listar_minutas_processo(cnj, g)
    res["grau"] = GRAUS_LEGIVEIS.get(g, g)
    return res


async def gerar_relatorio_markdown_processo(
    numero_cnj: str, persona: str = "advogado", grau: str = "1"
) -> dict:
    """[TJPA 1g|2g] Gera e salva um relatório estruturado em Markdown (.md) na pasta do processo."""
    cnj = _normaliza_cnj(numero_cnj)
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)

    resumo = await resumo_executivo_processo(cnj, persona=p, grau=g)
    dados = resumo.get("dados_basicos", {})
    partes = resumo.get("partes", [])
    movs = resumo.get("ultimas_movimentacoes", [])
    decisao = resumo.get("ultima_decisao", {})
    pendencias = resumo.get("pendencias", {})

    linhas = []
    linhas.append(f"# ⚖️ Relatório Processual — PJe {GRAUS_LEGIVEIS.get(g, g)}")
    linhas.append(f"**Processo Nº**: `{cnj}`  ")
    linhas.append(
        f"**Tribunal**: TJPA | **Gerado em**: `{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}`\n"
    )

    linhas.append("## 📌 Dados Básicos")
    linhas.append(f"- **Classe**: {dados.get('classe', 'N/I')}")
    linhas.append(f"- **Assunto**: {dados.get('assunto', 'N/I')}")
    linhas.append(f"- **Vara / Órgão Julgador**: {dados.get('vara', 'N/I')}")
    linhas.append(f"- **Valor da Causa**: {dados.get('valor_causa', 'N/I')}")
    linhas.append(
        f"- **Data de Distribuição**: {dados.get('data_distribuicao', 'N/I')}\n"
    )

    linhas.append("## 👥 Partes e Qualificação")
    if partes:
        for parte in partes:
            polo = parte.get("polo", "Parte")
            nome = parte.get("nome", "N/I")
            advs = ", ".join(parte.get("advogados", [])) or "Sem advogado cadastrado"
            linhas.append(f"- **{polo}**: {nome} *(Advogados: {advs})*")
    else:
        linhas.append("- Nenhuma parte qualificada encontrada.")
    linhas.append("")

    linhas.append("## 🚨 Pendências e Prazos")
    prazos_list = pendencias.get("expedientes", [])
    if prazos_list:
        for exp in prazos_list:
            venc = (
                "⚠️ VENCIDO"
                if exp.get("vencido")
                else f"⏰ {exp.get('dias_restantes', 0)} dias restantes"
            )
            linhas.append(
                f"- **Ato**: {exp.get('ato', 'N/A')} | **Limite**: `{exp.get('data_limite', 'N/A')}` ({venc})"
            )
    else:
        linhas.append("✅ Nenhuma pendência de expediente encontrada.")
    linhas.append("")

    linhas.append("## 📄 Última Decisão / Despacho")
    if decisao.get("encontrado"):
        linhas.append(f"### {decisao.get('titulo', 'Decisão')}")
        linhas.append(f"```text\n{decisao.get('teor_resumido', '')}\n```")
    else:
        linhas.append("Nenhuma decisão ou despacho encontrado.")
    linhas.append("")

    linhas.append("## 📜 Últimas Movimentações")
    if movs:
        for m in movs:
            linhas.append(
                f"- **{m.get('data', '')}**: {m.get('titulo', '')} {m.get('detalhes', '')}"
            )
    else:
        linhas.append("Nenhuma movimentação registrada.")

    conteudo_md = "\n".join(linhas)

    salvo = minutas.salvar_peca(
        cnj,
        conteudo_md,
        tipo="Relatório Sintético",
        formato="md",
        grau=g,
    )
    salvo["relatorio"] = resumo
    return _marcar_grau(salvo, p, g)


async def gerar_relatorio_html_processo(
    numero_cnj: str, persona: str = "advogado", grau: str = "1"
) -> dict:
    """[TJPA 1g|2g] Gera e salva um relatório HTML renderizável e imprimível na pasta do processo."""
    cnj = _normaliza_cnj(numero_cnj)
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)

    resumo = await resumo_executivo_processo(cnj, persona=p, grau=g)
    dados = resumo.get("dados_basicos", {})
    partes = resumo.get("partes", [])
    movs = resumo.get("ultimas_movimentacoes", [])
    decisao = resumo.get("ultima_decisao", {})
    pendencias = resumo.get("pendencias", {})

    partes_html = ""
    for parte in partes:
        polo = parte.get("polo", "Parte")
        nome = parte.get("nome", "N/I")
        advs = ", ".join(parte.get("advogados", [])) or "Sem advogado cadastrado"
        partes_html += (
            f"<tr><td><strong>{polo}</strong></td><td>{nome}</td><td>{advs}</td></tr>"
        )

    movs_html = ""
    for m in movs:
        movs_html += f"<tr><td>{m.get('data', '')}</td><td><strong>{m.get('titulo', '')}</strong></td><td>{m.get('detalhes', '')}</td></tr>"

    prazos_html = ""
    for exp in pendencias.get("expedientes", []):
        venc_class = "vencido" if exp.get("vencido") else "urgente"
        venc_label = (
            "VENCIDO" if exp.get("vencido") else f"{exp.get('dias_restantes', 0)} dias"
        )
        prazos_html += f"<tr><td>{exp.get('ato', 'N/A')}</td><td>{exp.get('data_limite', 'N/A')}</td><td><span class='badge {venc_class}'>{venc_label}</span></td></tr>"

    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<title>Relatório Processual - {cnj}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 30px; }}
.container {{ max-width: 900px; margin: 0 auto; background: #1e293b; padding: 40px; border-radius: 12px; border: 1px solid #334155; }}
h1 {{ color: #38bdf8; font-size: 24px; margin-bottom: 5px; }}
h2 {{ color: #94a3b8; font-size: 18px; border-bottom: 1px solid #334155; padding-bottom: 8px; margin-top: 30px; }}
.meta {{ color: #64748b; font-size: 14px; margin-bottom: 25px; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
th, td {{ padding: 10px; text-align: left; border-bottom: 1px solid #334155; font-size: 14px; }}
th {{ background: #0f172a; color: #38bdf8; }}
.badge {{ padding: 4px 8px; border-radius: 4px; font-weight: bold; font-size: 12px; }}
.vencido {{ background: #ef4444; color: #fff; }}
.urgente {{ background: #f59e0b; color: #000; }}
.box {{ background: #0f172a; padding: 15px; border-radius: 8px; border-left: 4px solid #38bdf8; font-family: monospace; font-size: 13px; white-space: pre-wrap; }}
</style>
</head>
<body>
<div class="container">
<h1>⚖️ Relatório Processual — PJe {GRAUS_LEGIVEIS.get(g, g)}</h1>
<div class="meta">Processo nº <strong>{cnj}</strong> | Tribunal: TJPA | Gerado em: {datetime.now().strftime("%d/%m/%Y %H:%M:%S")}</div>

<h2>📌 Dados Básicos</h2>
<table>
<tr><th>Classe</th><td>{dados.get("classe", "N/I")}</td><th>Assunto</th><td>{dados.get("assunto", "N/I")}</td></tr>
<tr><th>Vara / Órgão</th><td>{dados.get("vara", "N/I")}</td><th>Valor Causa</th><td>{dados.get("valor_causa", "N/I")}</td></tr>
</table>

<h2>👥 Partes e Qualificação</h2>
<table>
<tr><th>Pólo</th><th>Nome</th><th>Advogados</th></tr>
{partes_html or '<tr><td colspan="3">Nenhuma parte cadastrada</td></tr>'}
</table>

<h2>🚨 Pendências e Prazos</h2>
<table>
<tr><th>Ato</th><th>Data Limite</th><th>Status</th></tr>
{prazos_html or '<tr><td colspan="3">Nenhuma pendência encontrada</td></tr>'}
</table>

<h2>📄 Última Decisão / Despacho</h2>
<div class="box">{decisao.get("teor_resumido", "Nenhuma decisão encontrada")}</div>

<h2>📜 Últimas Movimentações</h2>
<table>
<tr><th>Data</th><th>Movimento</th><th>Detalhes</th></tr>
{movs_html or '<tr><td colspan="3">Nenhuma movimentação</td></tr>'}
</table>
</div>
</body>
</html>"""

    salvo = minutas.salvar_peca(
        cnj,
        html,
        tipo="Relatório Visual",
        formato="txt",
        grau=g,
    )
    path_html = Path(salvo["caminho"]).with_suffix(".html")
    Path(salvo["caminho"]).rename(path_html)
    salvo["caminho"] = str(path_html)
    salvo["formato"] = "html"
    return _marcar_grau(salvo, p, g)


async def exportar_todos_relatorios_processos(
    numeros_cnj: list[str],
    formato: str = "md",
    persona: str = "advogado",
    grau: str = "1",
) -> dict:
    """[TJPA 1g|2g] Gera relatórios sintéticos em lote (.md ou .html) para múltiplos processos."""
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    relatorios = []

    for raw_cnj in numeros_cnj:
        cnj = _normaliza_cnj(raw_cnj)
        try:
            if formato.lower() == "html":
                r = await gerar_relatorio_html_processo(cnj, persona=p, grau=g)
            else:
                r = await gerar_relatorio_markdown_processo(cnj, persona=p, grau=g)
            relatorios.append(
                {
                    "numero_cnj": cnj,
                    "status": "sucesso",
                    "caminho": r.get("caminho"),
                    "tamanho_kb": r.get("tamanho_kb"),
                }
            )
        except Exception as e:
            relatorios.append(
                {
                    "numero_cnj": cnj,
                    "status": "erro",
                    "erro": str(e),
                }
            )

    return _marcar_grau(
        {
            "total_processos": len(numeros_cnj),
            "formato": formato,
            "relatorios_gerados": relatorios,
        },
        p,
        g,
    )


async def exportar_relatorio_pdf_processo(
    numero_cnj: str, persona: str = "advogado", grau: str = "1"
) -> dict:
    """[TJPA 1g|2g] Gera e exporta um relatório em PDF de alta qualidade via Playwright Chromium."""
    cnj = _normaliza_cnj(numero_cnj)
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)

    # 1. Gera o relatório HTML primeiro
    r_html = await gerar_relatorio_html_processo(cnj, persona=p, grau=g)
    caminho_html = Path(r_html["caminho"])

    if not caminho_html.exists():
        return {"erro": f"Arquivo HTML base não encontrado em {caminho_html}"}

    caminho_pdf = caminho_html.with_suffix(".pdf")

    # 2. Renderiza para PDF reaproveitando o contexto do cliente_singleton
    pje = await cliente_singleton.get_cliente(p, g)
    # O contexto e compartilhado com as consultas PJe. Sem este lock, uma
    # consulta concorrente pode limpar abas secundarias no meio do page.pdf().
    async with pje._op_lock:
        page = await pje._context.new_page()
        try:
            await page.goto(caminho_html.as_uri(), wait_until="domcontentloaded")
            await page.pdf(
                path=str(caminho_pdf),
                format="A4",
                print_background=True,
                margin={"top": "1cm", "bottom": "1cm", "left": "1cm", "right": "1cm"},
            )
        finally:
            await page.close()

    size_kb = round(caminho_pdf.stat().st_size / 1024, 1)
    return _marcar_grau(
        {
            "numero_cnj": cnj,
            "status": "sucesso",
            "caminho": str(caminho_pdf),
            "formato": "pdf",
            "tamanho_kb": size_kb,
            "mensagem": f"Relatório PDF impresso com sucesso ({size_kb} KB).",
        },
        p,
        g,
    )


async def gerar_dossie_executivo_zip(
    numero_cnj: str,
    incluir_autos: bool = False,
    incluir_pecas: bool = True,
    persona: str = "advogado",
    grau: str = "1",
) -> dict:
    """[TJPA 1g|2g] Empacota relatórios, minutas e peças do processo num .zip.

    - incluir_autos: False (padrão) deixa de fora o PDF dos autos completos.
    - incluir_pecas: True (padrão) inclui as peças individuais de
      .../{cnj}/documentos/, preservando a subpasta dentro do .zip.
    """
    import zipfile

    cnj = _normaliza_cnj(numero_cnj)
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)

    pasta = minutas.pasta_processo(cnj, g)
    if not pasta.exists():
        return {"erro": f"Pasta do processo {cnj} não existe."}

    zip_path = pasta / f"Dossie_Processual_{minutas.cnj_safe(cnj)}.zip"
    arquivos_incluidos = []
    arquivos_excluidos = []
    pecas_individuais = 0

    # O PDF dos autos e' salvo como '{cnj_safe}.pdf' (e variantes com sufixo:
    # ' (doc_a_doc)', ' (recorte)', ' (parcial N docs)'). O filtro antigo
    # procurava 'autos_completos' no nome — string que NUNCA existiu —, entao
    # incluir_autos=False nao excluia nada e o dossie "leve" saia com os autos
    # inteiros dentro, as vezes dezenas de MB.
    base_autos = minutas.cnj_safe(cnj)

    def _eh_autos(nome: str) -> bool:
        return nome.lower().endswith(".pdf") and Path(nome).stem.startswith(base_autos)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for item in sorted(pasta.iterdir()):
            if (
                not item.is_file()
                or item.name.startswith(".")
                or item.suffix.lower() == ".zip"
            ):
                continue
            if not incluir_autos and _eh_autos(item.name):
                arquivos_excluidos.append(
                    {
                        "arquivo": item.name,
                        "tamanho_mb": round(item.stat().st_size / (1024 * 1024), 2),
                    }
                )
                continue
            zipf.write(item, arcname=item.name)
            arquivos_incluidos.append(item.name)

        # As pecas individuais ficam em .../{cnj}/documentos/ (gravadas por
        # baixar_documento e pelo metodo doc_a_doc). O iterdir so varre o topo,
        # entao elas nunca entravam - apesar de o docstring prometer "peças".
        pasta_docs = pasta / "documentos"
        if incluir_pecas and pasta_docs.is_dir():
            for item in sorted(pasta_docs.iterdir()):
                if not item.is_file() or item.name.startswith("."):
                    continue
                arc = f"documentos/{item.name}"
                zipf.write(item, arcname=arc)
                arquivos_incluidos.append(arc)
                pecas_individuais += 1

    size_kb = round(zip_path.stat().st_size / 1024, 1)
    res = {
        "numero_cnj": cnj,
        "status": "sucesso",
        "caminho_zip": str(zip_path),
        "tamanho_kb": size_kb,
        "incluir_autos": incluir_autos,
        "incluir_pecas": incluir_pecas,
        "pecas_individuais": pecas_individuais,
        "total_arquivos_compactados": len(arquivos_incluidos),
        "arquivos_incluidos": arquivos_incluidos,
    }
    if arquivos_excluidos:
        poupado = round(sum(a["tamanho_mb"] for a in arquivos_excluidos), 2)
        res["arquivos_excluidos"] = arquivos_excluidos
        res["mb_poupados"] = poupado
        res["observacao"] = (
            f"Autos deixados de fora ({poupado} MB). Use incluir_autos=True "
            f"para empacotar o processo completo."
        )
    if not arquivos_incluidos:
        res["aviso"] = (
            "Nenhum arquivo entrou no dossiê — gere relatórios ou minutas antes "
            "(acao='gerar_markdown', 'salvar_peticao'…)."
        )
    return _marcar_grau(res, p, g)


async def gerar_folha_de_rosto_processual(
    numero_cnj: str, persona: str = "advogado", grau: str = "1"
) -> dict:
    """[TJPA 1g|2g] Gera uma folha de rosto oficial (.html e .pdf) com sumário e metadados para a capa do processo."""
    cnj = _normaliza_cnj(numero_cnj)
    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)

    resumo = await resumo_executivo_processo(cnj, persona=p, grau=g)
    dados = resumo.get("dados_basicos", {})
    partes = resumo.get("partes", [])
    pendencias = resumo.get("pendencias", {})
    decisao = resumo.get("ultima_decisao", {})

    autores = (
        ", ".join([pt.get("nome", "") for pt in partes if pt.get("polo") == "AUTOR"])
        or "N/I"
    )
    reus = (
        ", ".join([pt.get("nome", "") for pt in partes if pt.get("polo") == "REU"])
        or "N/I"
    )

    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<title>Folha de Rosto - {cnj}</title>
<style>
@page {{ size: A4; margin: 1.5cm; }}
body {{ font-family: 'Helvetica Neue', Arial, sans-serif; color: #1e293b; line-height: 1.5; }}
.header {{ text-align: center; border-bottom: 3px double #0f172a; padding-bottom: 15px; margin-bottom: 25px; }}
.header h1 {{ margin: 0; font-size: 22px; color: #0f172a; text-transform: uppercase; letter-spacing: 1px; }}
.header p {{ margin: 5px 0 0 0; color: #475569; font-size: 14px; font-weight: bold; }}
.cnj-box {{ background: #f8fafc; border: 2px solid #0f172a; border-radius: 8px; padding: 15px; text-align: center; margin-bottom: 25px; }}
.cnj-number {{ font-size: 24px; font-weight: bold; font-family: monospace; color: #0284c7; letter-spacing: 1px; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 15px; margin-bottom: 25px; }}
.card {{ background: #f1f5f9; padding: 12px 15px; border-radius: 6px; border-left: 4px solid #0284c7; }}
.card-title {{ font-size: 11px; text-transform: uppercase; color: #64748b; font-weight: bold; margin-bottom: 4px; }}
.card-val {{ font-size: 14px; font-weight: bold; color: #0f172a; }}
.section-title {{ font-size: 14px; text-transform: uppercase; color: #0f172a; border-bottom: 1px solid #cbd5e1; padding-bottom: 4px; margin-top: 20px; font-weight: bold; }}
.footer {{ margin-top: 40px; text-align: center; font-size: 11px; color: #94a3b8; border-top: 1px solid #e2e8f0; padding-top: 10px; }}
</style>
</head>
<body>
<div class="header">
<h1>Tribunal de Justiça do Estado do Pará</h1>
<p>PJe — Processo Judicial Eletrônico ({GRAUS_LEGIVEIS.get(g, g)})</p>
</div>

<div class="cnj-box">
<div style="font-size: 12px; color: #64748b; font-weight: bold;">NÚMERO ÚNICO CNJ</div>
<div class="cnj-number">{cnj}</div>
</div>

<div class="grid">
<div class="card"><div class="card-title">Classe Judicial</div><div class="card-val">{dados.get("classe", "N/I")}</div></div>
<div class="card"><div class="card-title">Assunto Principal</div><div class="card-val">{dados.get("assunto", "N/I")}</div></div>
<div class="card"><div class="card-title">Órgão Julgador / Vara</div><div class="card-val">{dados.get("vara", "N/I")}</div></div>
<div class="card"><div class="card-title">Valor da Causa</div><div class="card-val">{dados.get("valor_causa", "N/I")}</div></div>
</div>

<div class="section-title">Pólo Ativo (Requerente / Autor)</div>
<p><strong>{autores}</strong></p>

<div class="section-title">Pólo Passivo (Requerido / Réu)</div>
<p><strong>{reus}</strong></p>

<div class="section-title">Status da Útilma Decisão</div>
<p><strong>{decisao.get("titulo", "Sem decisão cadastrada")}</strong>: {decisao.get("teor_resumido", "")[:300]}...</p>

<div class="section-title">Pendências de Intimação / Prazos</div>
<p>Total de expedientes em aberto: <strong>{pendencias.get("total", 0)}</strong></p>

<div class="footer">
Documento gerado automaticamente pelo Servidor MCP PJe-TJPA em {datetime.now().strftime("%d/%m/%Y às %H:%M:%S")}
</div>
</body>
</html>"""

    salvo = minutas.salvar_peca(cnj, html, tipo="Folha_de_Rosto", formato="txt", grau=g)
    caminho_html = Path(salvo["caminho"]).with_suffix(".html")
    Path(salvo["caminho"]).rename(caminho_html)

    caminho_pdf = caminho_html.with_suffix(".pdf")
    pje = await cliente_singleton.get_cliente(p, g)
    async with pje._op_lock:
        page = await pje._context.new_page()
        try:
            await page.goto(caminho_html.as_uri(), wait_until="domcontentloaded")
            await page.pdf(path=str(caminho_pdf), format="A4", print_background=True)
        finally:
            await page.close()

    return _marcar_grau(
        {
            "numero_cnj": cnj,
            "status": "sucesso",
            "caminho_html": str(caminho_html),
            "caminho_pdf": str(caminho_pdf),
            "tamanho_pdf_kb": round(caminho_pdf.stat().st_size / 1024, 1),
        },
        p,
        g,
    )


# ==============================================================================
# SUPER FERRAMENTAS CONSOLIDADAS (PJe MCP)
# Superferramentas consolidadas para otimização de contexto do LLM.
# ==============================================================================

_ACOES_STATUS = {
    "status": "Saúde do servidor e da sessão do PJe (rápido, sem rede externa).",
    "auditoria": "Diagnóstico completo: pacotes, storage por grau, conectividade e alertas.",
    "jobs": "Downloads em background desta sessão — o que está rodando agora.",
    "capacidades": "Inventário de todas as ações das superferramentas, com apelidos.",
}


_ACOES_AUTOMACAO_NAVEGADOR = {
    "estado": (
        "Confirma a aba oficial, a sessão autenticada e o processo visível "
        "na automação Playwright local."
    ),
    "buscar": (
        "Pesquisa na página real do PJe pela automação Playwright conectada "
        "ao Chrome, em modo somente leitura."
    ),
    "abrir_processo": (
        "Pesquisa o número CNJ e abre os autos digitais na sessão real do PJe."
    ),
    "listar_documentos": (
        "Lista os documentos encontrados nos autos digitais atualmente abertos."
    ),
    "abrir_expedientes": (
        "Abre a aba Expedientes, identificada pelo ícone de envelope, nos autos atuais."
    ),
    "listar_expedientes": (
        "Lista atos, prazos, documentos e ações disponíveis na aba Expedientes."
    ),
    "mapear_juntada_documentos": (
        "Abre Juntar documentos sem salvar nem protocolar e inventaria tipos, "
        "certidões, modelos da Vara de Família, PDF externo, editor e movimento."
    ),
    "mapear_comunicacoes": (
        "Lê a tarefa Preparar comunicação, seus instrumentos e catálogo contextual de modelos, "
        "sem salvar, expedir, assinar ou enviar qualquer ato."
    ),
    "analisar_plano_comunicacoes": (
        "Aplica travas operacionais de destinatário, prazo, MP, audiência e Central de Mandados "
        "a um plano, sem tocar na página e sem disponibilizar ação final."
    ),
    "mapear_expedicao_documento": (
        "Lê a tarefa Expedir documento, tipos, modelos, editor, movimento 60 e etapa de comunicação "
        "separada, sem assinar nem expedir."
    ),
    "analisar_plano_expedicao_documento": (
        "Valida a minuta interna, o tipo e o complemento do movimento 60 sem alterar o PJe."
    ),
    "inspecionar_metadados_processo": (
        "Lê etiquetas, situações processuais e lembretes do documento atual nos autos."
    ),
    "inspecionar_retificacao": (
        "Abre a Retificação da Autuação sem gravar e informa campos e capacidades disponíveis."
    ),
    "listar_processos_retificaveis": (
        "Lista números CNJ elegíveis para retificação no perfil funcional atualmente ativo."
    ),
    "navegar_documento": (
        "Move o visualizador para first, previous, next ou last."
    ),
    "baixar_documento_aberto": (
        "Baixa o PDF atualmente exibido para o storage local protegido."
    ),
    "analisar_documento_aberto": (
        "Baixa e extrai o PDF exibido, aplicando OCR seletivo quando necessário."
    ),
    "extrair_documento_aberto": (
        "Extrai o texto selecionado ou o documento atualmente aberto na aba "
        "oficial do PJe."
    ),
}


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
    structured_output=True,
)
@auditar_ferramenta
async def automacao_navegador_pje(
    acao: str,
    criterio: str = "numero_cnj",
    valor: str = "",
    valor_final: str = "",
    limite: int = 20,
    uf_oab: str = "PA",
    letra_oab: str = "",
    max_caracteres: int = 20_000,
    direcao: str = "next",
    tipo_documento: str = "Certidão",
    plano_comunicacoes: Dict[str, Any] | None = None,
    plano_expedicao_documento: Dict[str, Any] | None = None,
    aplicar_ocr: bool = True,
    max_paginas_ocr: int = 10,
    timeout_ms: int = 15_000,
) -> Dict[str, Any]:
    """Integra o MCP à automação Playwright local que controla o Chrome.

    acao: 'estado' | 'buscar' | 'abrir_processo' | 'listar_documentos' |
        'abrir_expedientes' | 'listar_expedientes' | 'mapear_juntada_documentos' |
        'mapear_comunicacoes' | 'analisar_plano_comunicacoes' |
        'mapear_expedicao_documento' | 'analisar_plano_expedicao_documento' |
        'inspecionar_metadados_processo' |
        'inspecionar_retificacao' |
        'navegar_documento' | 'baixar_documento_aberto' |
        'analisar_documento_aberto' | 'extrair_documento_aberto'
    criterio: em 'buscar', use numero_cnj, cpf, cnpj, nome_parte,
        nome_requerente, nome_requerido, nome_advogado, outros_nomes,
        numero_documento, oab, assunto, classe_judicial, jurisdicao,
        orgao_julgador, prioridade_processual, data_autuacao, valor_causa,
        movimento_processual, orgao_origem_criminal, procedimento_criminal,
        ano_procedimento_criminal ou protocolo_policia. Também aceita autor,
        réu, requerente, requerido, representante, alcunha, classe, prioridade,
        movimento e movimentacao_processual como aliases.
    valor: dado pesquisado. OAB também usa uf_oab e, quando existir,
        letra_oab.
    valor_final: fim opcional de data_autuacao/valor_causa ou ano separado do
        procedimento_criminal.

    Esta ferramenta não inicia outro navegador e não usa o Playwright Python.
    Ela chama exclusivamente o bridge Node local por socket UNIX protegido.
    """
    canonica, erro = _resolver_acao(acao, _ACOES_AUTOMACAO_NAVEGADOR, {})
    if erro:
        erro["acoes"] = _ACOES_AUTOMACAO_NAVEGADOR
        return erro
    try:
        if canonica == "estado":
            data = await browser_bridge_client.call_browser_bridge(
                "get_state", timeout_ms=timeout_ms
            )
        elif canonica == "extrair_documento_aberto":
            max_chars = max(1, min(int(max_caracteres or 20_000), 20_000))
            data = await browser_bridge_client.call_browser_bridge(
                "extract_document",
                {"max_chars": max_chars},
                timeout_ms=timeout_ms,
            )
        elif canonica == "abrir_processo":
            data = await browser_bridge_client.call_browser_bridge(
                "open_process",
                {"process_number": valor},
                timeout_ms=timeout_ms,
            )
        elif canonica == "listar_documentos":
            data = await browser_bridge_client.call_browser_bridge(
                "list_documents",
                {"limit": max(1, min(int(limite or 100), 500))},
                timeout_ms=timeout_ms,
            )
        elif canonica == "abrir_expedientes":
            data = await browser_bridge_client.call_browser_bridge(
                "open_expedients", timeout_ms=timeout_ms
            )
        elif canonica == "listar_expedientes":
            data = await browser_bridge_client.call_browser_bridge(
                "list_expedients",
                {"limit": max(1, min(int(limite or 100), 500))},
                timeout_ms=timeout_ms,
            )
        elif canonica == "mapear_juntada_documentos":
            data = await browser_bridge_client.call_browser_bridge(
                "inspect_document_join",
                {
                    "process_number": valor,
                    "document_type": str(tipo_documento or "").strip(),
                    "task_box": "Verificar providência a adotar",
                },
                timeout_ms=timeout_ms,
            )
        elif canonica == "mapear_comunicacoes":
            data = await browser_bridge_client.call_browser_bridge(
                "inspect_communications",
                {"process_number": valor},
                timeout_ms=timeout_ms,
            )
        elif canonica == "analisar_plano_comunicacoes":
            if not isinstance(plano_comunicacoes, dict):
                return {
                    "status": "erro",
                    "codigo": "INVALID_REQUEST",
                    "erro": "plano_comunicacoes deve ser um objeto.",
                }
            data = await browser_bridge_client.call_browser_bridge(
                "analyze_communication_plan",
                plano_comunicacoes,
                timeout_ms=timeout_ms,
            )
        elif canonica == "mapear_expedicao_documento":
            data = await browser_bridge_client.call_browser_bridge(
                "inspect_document_issue",
                {"process_number": valor},
                timeout_ms=timeout_ms,
            )
        elif canonica == "analisar_plano_expedicao_documento":
            if not isinstance(plano_expedicao_documento, dict):
                return {
                    "status": "erro",
                    "codigo": "INVALID_REQUEST",
                    "erro": "plano_expedicao_documento deve ser um objeto.",
                }
            data = await browser_bridge_client.call_browser_bridge(
                "analyze_document_issue_plan",
                plano_expedicao_documento,
                timeout_ms=timeout_ms,
            )
        elif canonica == "inspecionar_metadados_processo":
            data = await browser_bridge_client.call_browser_bridge(
                "inspect_process_metadata",
                {"process_number": valor},
                timeout_ms=timeout_ms,
            )
        elif canonica == "inspecionar_retificacao":
            data = await browser_bridge_client.call_browser_bridge(
                "inspect_retification",
                {"process_number": valor},
                timeout_ms=timeout_ms,
            )
        elif canonica == "listar_processos_retificaveis":
            data = await browser_bridge_client.call_browser_bridge(
                "list_retification_candidates",
                {"limit": max(1, min(int(limite or 20), 100))},
                timeout_ms=timeout_ms,
            )
        elif canonica == "navegar_documento":
            data = await browser_bridge_client.call_browser_bridge(
                "navigate_document",
                {"direction": str(direcao or "next").strip().lower()},
                timeout_ms=timeout_ms,
            )
        elif canonica == "baixar_documento_aberto":
            data = await browser_bridge_client.call_browser_bridge(
                "download_current_document", timeout_ms=timeout_ms
            )
        elif canonica == "analisar_documento_aberto":
            artifact = await browser_bridge_client.call_browser_bridge(
                "download_current_document", timeout_ms=timeout_ms
            )
            max_chars = max(1, min(int(max_caracteres or 20_000), 100_000))
            analysis = await asyncio.to_thread(
                pdf_scan.inspect_pdf,
                artifact.get("path", ""),
                apply_ocr=bool(aplicar_ocr),
                max_ocr_pages=max(0, min(int(max_paginas_ocr or 0), 100)),
                max_chars=max_chars,
            )
            if analysis["sha256"] != artifact.get("sha256"):
                return {
                    "status": "erro",
                    "codigo": "ARTIFACT_INTEGRITY_FAILED",
                    "erro": "O PDF mudou entre o download e a análise.",
                }
            data = {"download": artifact, "analysis": analysis}
        else:
            criterio_normalizado = _slug_acao(criterio)
            criterio_normalizado = {
                "autor": "nome_requerente",
                "requerente": "nome_requerente",
                "polo_ativo": "nome_requerente",
                "reu": "nome_requerido",
                "requerido": "nome_requerido",
                "polo_passivo": "nome_requerido",
                "advogado": "nome_advogado",
                "representante": "nome_advogado",
                "alcunha": "outros_nomes",
                "documento": "numero_documento",
                "classe": "classe_judicial",
                "prioridade": "prioridade_processual",
                "movimento": "movimento_processual",
                "movimentacao": "movimento_processual",
                "movimentacao_processual": "movimento_processual",
                "numero_procedimento_criminal": "procedimento_criminal",
                "protocolo_policial": "protocolo_policia",
            }.get(criterio_normalizado, criterio_normalizado)
            payload = {
                "criterion": criterio_normalizado,
                "value": valor,
                "limit": max(1, min(int(limite or 20), 100)),
            }
            if str(valor_final or "").strip():
                payload["value_end"] = str(valor_final).strip()
            if criterio_normalizado == "oab":
                payload["uf_oab"] = str(uf_oab or "PA").strip().upper()
                payload["letra_oab"] = str(letra_oab or "").strip().upper()
            data = await browser_bridge_client.call_browser_bridge(
                "search_process", payload, timeout_ms=timeout_ms
            )
    except (TypeError, ValueError):
        return {
            "status": "erro",
            "codigo": "INVALID_REQUEST",
            "erro": "Parâmetro numérico inválido para a automação.",
        }
    except browser_bridge_client.BrowserBridgeError as exc:
        return {
            "status": "erro",
            "codigo": exc.code,
            "erro": str(exc),
            "repetivel": exc.retryable,
            "origem": "playwright_bridge",
        }
    except pdf_scan.PdfScanError as exc:
        return {
            "status": "erro",
            "codigo": "PDF_SCAN_FAILED",
            "erro": str(exc),
            "repetivel": False,
            "origem": "pdf_scan_local",
        }
    return {
        "status": "sucesso",
        "origem": "playwright_bridge",
        "acao": canonica,
        "resultado": data,
    }


_ACOES_ETIQUETA_PROCESSO = {
    "previsualizar": "Confere a etiqueta e gera uma confirmação vinculada ao estado atual.",
    "aplicar": "Vincula ou remove exatamente a etiqueta anteriormente confirmada.",
}


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
    structured_output=True,
)
@auditar_ferramenta
async def gerenciar_etiqueta_processo_pje(
    acao: str,
    numero_cnj: str,
    operacao: str,
    etiqueta: str,
    caixa_tarefa: str = "Verificar providência a adotar",
    token_confirmacao: str = "",
    timeout_ms: int = 30_000,
) -> Dict[str, Any]:
    """Adiciona ou remove uma etiqueta de um processo com confirmação em duas fases.

    `operacao` deve ser `adicionar` ou `remover`. A adição vincula uma etiqueta já
    existente no catálogo do perfil; esta ferramenta não cria etiquetas globais.
    `previsualizar` não grava e gera token válido por cinco minutos. `aplicar`
    exige esse token e falha se etiquetas, situações ou lembretes mudarem.
    """
    canonica, erro = _resolver_acao(acao, _ACOES_ETIQUETA_PROCESSO, {})
    if erro:
        erro["acoes"] = _ACOES_ETIQUETA_PROCESSO
        return erro
    change = {
        "acao": str(operacao or "").strip().lower(),
        "etiqueta": str(etiqueta or "").strip(),
        "caixa_tarefa": str(caixa_tarefa or "").strip(),
    }
    try:
        if canonica == "previsualizar":
            data = await browser_bridge_client.call_browser_bridge(
                "preview_process_label_change",
                {"process_number": numero_cnj, "change": change},
                timeout_ms=timeout_ms,
            )
        else:
            if not str(token_confirmacao or "").strip():
                return {
                    "status": "erro",
                    "codigo": "CONFIRMATION_REQUIRED",
                    "erro": "A aplicação exige o token da previsualização correspondente.",
                }
            data = await browser_bridge_client.call_browser_bridge(
                "apply_process_label_change",
                {
                    "process_number": numero_cnj,
                    "change": change,
                    "confirmation_token": str(token_confirmacao).strip(),
                },
                timeout_ms=timeout_ms,
            )
    except browser_bridge_client.BrowserBridgeError as exc:
        return {
            "status": "erro",
            "codigo": exc.code,
            "erro": str(exc),
            "repetivel": exc.retryable,
            "origem": "playwright_bridge",
        }
    return {
        "status": "sucesso",
        "origem": "playwright_bridge",
        "acao": canonica,
        "resultado": data,
    }


_ACOES_RETIFICACAO_AUTUACAO = {
    "previsualizar": (
        "Valida uma única seção, confere o formulário atual e gera token de confirmação."
    ),
    "simular": (
        "Preenche endereço/contato/parte e cancela; reabre o processo para confirmar estado igual."
    ),
    "aplicar": (
        "Aplica exatamente a alteração previsualizada, após reconfirmar que o formulário não mudou."
    ),
}


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
    structured_output=True,
)
@auditar_ferramenta
async def retificar_autuacao_pje(
    acao: str,
    numero_cnj: str,
    alteracoes: Dict[str, Any] | None = None,
    token_confirmacao: str = "",
    timeout_ms: int = 30_000,
) -> Dict[str, Any]:
    """Previsualiza ou aplica alterações cadastrais na autuação do PJe.

    A ferramenta aceita uma seção por confirmação:
    - dados_iniciais: {classe_judicial}
    - assuntos: {adicionar: [{codigo, descricao}]}
    - partes: [{polo, indice, dados}] ou uma operação `endereco`/`contato`
    - ministerio_publico: {acao, fundamento}; remoção exige o CNPJ institucional exato
    - caracteristicas: {tutela_liminar, valor_causa, justica_gratuita, prioridade}

    `previsualizar` e `simular` nunca gravam. `aplicar` exige o token retornado pela prévia,
    válido por cinco minutos e vinculado ao processo, alterações e estado do
    formulário. A remoção genérica de parte continua bloqueada; somente o MPPA
    identificado pelo CNPJ institucional exato pode ser removido. Inclusão de
    nova parte e resposta a expediente não são executadas nesta tela.
    """
    canonica, erro = _resolver_acao(acao, _ACOES_RETIFICACAO_AUTUACAO, {})
    if erro:
        erro["acoes"] = _ACOES_RETIFICACAO_AUTUACAO
        return erro
    if not isinstance(alteracoes, dict):
        return {
            "status": "erro",
            "codigo": "INVALID_REQUEST",
            "erro": "alteracoes deve ser um objeto com exatamente uma seção.",
        }
    try:
        if canonica == "previsualizar":
            data = await browser_bridge_client.call_browser_bridge(
                "preview_retification",
                {"process_number": numero_cnj, "changes": alteracoes},
                timeout_ms=timeout_ms,
            )
        elif canonica == "simular":
            data = await browser_bridge_client.call_browser_bridge(
                "simulate_retification",
                {"process_number": numero_cnj, "changes": alteracoes},
                timeout_ms=timeout_ms,
            )
        else:
            if not str(token_confirmacao or "").strip():
                return {
                    "status": "erro",
                    "codigo": "CONFIRMATION_REQUIRED",
                    "erro": "A aplicação exige o token da previsualização correspondente.",
                }
            data = await browser_bridge_client.call_browser_bridge(
                "apply_retification",
                {
                    "process_number": numero_cnj,
                    "changes": alteracoes,
                    "confirmation_token": str(token_confirmacao).strip(),
                },
                timeout_ms=timeout_ms,
            )
    except (TypeError, ValueError):
        return {
            "status": "erro",
            "codigo": "INVALID_REQUEST",
            "erro": "Parâmetro inválido para a retificação.",
        }
    except browser_bridge_client.BrowserBridgeError as exc:
        return {
            "status": "erro",
            "codigo": exc.code,
            "erro": str(exc),
            "repetivel": exc.retryable,
            "origem": "playwright_bridge",
        }
    return {
        "status": "sucesso",
        "origem": "playwright_bridge",
        "acao": canonica,
        "resultado": data,
    }

_ALIAS_STATUS = {
    "saude": "status",
    "health": "status",
    "healthcheck": "status",
    "diagnostico": "auditoria",
    "auditar": "auditoria",
    "completo": "auditoria",
    "downloads": "jobs",
    "downloads_ativos": "jobs",
    "tarefas": "jobs",
    "fila": "jobs",
    "acoes": "capacidades",
    "inventario": "capacidades",
    "ajuda": "capacidades",
    "help": "capacidades",
    "o_que_voce_faz": "capacidades",
}


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
    structured_output=True,
)
@auditar_ferramenta
async def status_e_auditoria_pje(
    acao: str = "status",
    incluir_concluidos: bool = True,
    filtro: str = "",
    confirmar_consulta: str = "",
    confirmation_token: str = "",
    capturar_screenshot: bool = False,
) -> Dict[str, Any]:
    """
    Diagnóstico, saúde e observabilidade do servidor MCP PJe.

    acao: 'status' (rápido) | 'auditoria' (completo, sonda a rede) | 'jobs'
        | 'capacidades'

    - auditoria: devolve 'saudavel' (bool) e 'alertas' já consolidados —
      storage sem escrita, menos de 1 GB livre, host inacessível ou download
      com erro. As sondagens de rede rodam em paralelo, fora do event loop.
    - jobs: incluir_concluidos=False mostra só o que ainda está em andamento.
    - capacidades: lista TODAS as ações das ferramentas com seus apelidos —
      use quando não souber qual ação existe. 'filtro' restringe por texto
      (ex: filtro='prazo', 'download', 'minuta').

    O veredito 'saudavel' da auditoria considera credenciais ausentes e pacotes
    essenciais faltando, além de disco e rede.
    """
    # Os parâmetros fazem parte do contrato das ferramentas consolidadas, mas estas
    # ações inspecionam somente o próprio servidor e não consultam o PJe.
    canonica, erro = _resolver_acao(acao, _ACOES_STATUS, _ALIAS_STATUS)
    if erro:
        erro["acoes"] = _ACOES_STATUS
        return erro

    if canonica == "status":
        return await status_servidor()
    if canonica == "auditoria":
        return await auditoria_mcp_pje()
    if canonica == "capacidades":
        return await inventario_capacidades(filtro=filtro)
    return await jobs_em_andamento(incluir_concluidos=incluir_concluidos)


_ACOES_PROTOCOLO = {
    "mapear": (
        "Abre a tela de cadastro e lista campos e opções visíveis, sem "
        "preencher, salvar, assinar ou protocolar."
    ),
    "avancar_dados_iniciais": (
        "Seleciona matéria, jurisdição e classe e avança para a próxima "
        "etapa, sem protocolar. Exige confirmar_preparacao=True."
    ),
    "mapear_assuntos": (
        "Avança os dados iniciais e pesquisa o catálogo de assuntos sem "
        "selecionar resultado. Exige confirmar_preparacao=True."
    ),
    "adicionar_assunto": (
        "Pesquisa e associa um assunto pelo código TPU, sem protocolar. "
        "Exige confirmar_preparacao=True."
    ),
    "mapear_partes": (
        "Prepara dados iniciais, associa o assunto e abre a etapa Partes. "
        "Exige confirmar_preparacao=True."
    ),
    "mapear_form_parte": (
        "Abre o modal de inclusão no polo ativo ou passivo, sem preencher. "
        "Exige confirmar_preparacao=True."
    ),
    "pesquisar_parte_cpf": (
        "Consulta uma pessoa física pelo CPF no polo informado, com retorno "
        "sanitizado. Exige confirmar_preparacao=True."
    ),
    "adicionar_parte_cpf": (
        "Consulta e confirma a pessoa física pelo CPF no polo informado, sem "
        "protocolar. Exige confirmar_preparacao=True."
    ),
    "mapear_documentos": (
        "Após adicionar a parte, abre Petições e documentos para mapear "
        "campos e uploads. Exige confirmar_preparacao=True."
    ),
    "criar_pdfs_teste": (
        "Cria de 1 a 3 PDFs vazios em diretório temporário isolado. "
        "Exige confirmar_preparacao=True."
    ),
    "anexar_pdf_teste": (
        "Prepara o cadastro, vincula a parte e carrega um PDF sintético "
        "como documento principal, sem protocolar. Exige confirmação."
    ),
}

_ALIAS_PROTOCOLO = {
    "mapa": "mapear",
    "inspecionar": "mapear",
    "campos": "mapear",
    "preparar": "avancar_dados_iniciais",
    "dados_iniciais": "avancar_dados_iniciais",
    "assuntos": "mapear_assuntos",
    "pesquisar_assunto": "mapear_assuntos",
    "selecionar_assunto": "adicionar_assunto",
    "partes": "mapear_partes",
    "form_parte": "mapear_form_parte",
    "cpf_parte": "pesquisar_parte_cpf",
    "confirmar_parte": "adicionar_parte_cpf",
    "documentos_protocolo": "mapear_documentos",
    "pdfs_vazios": "criar_pdfs_teste",
    "simular_documento": "anexar_pdf_teste",
}


_ACOES_ATUACAO = {
    "consultar_destino_tarefa": (
        "Consulta o destino recomendado para os processos de uma tarefa/caixa, "
        "cruzando as transições mapeadas no banco local com o playbook normativo. "
        "Ação de somente leitura; a movimentação efetiva exige commit humano."
    ),
    "mapear_transicoes_tarefa": (
        "Abre a caixa de tarefas, seleciona um processo e mapeia as "
        "transições/destinos oferecidos pelo PJe. A classificação de "
        "reversibilidade é heurística (selects saem INDETERMINADA, botões "
        "COMMIT_NA_SELECAO) e NÃO libera preparação."
    ),
    "confirmar_reversibilidade_transicao": (
        "Promove uma transição mapeada a REVERSIVEL_ANTES_COMMIT após "
        "verificação humana. Exige nome_tarefa, destino_id, evidência textual "
        "e confirmation_token='CONFIRMO_REVERSIVEL_ANTES_COMMIT'. Só destinos "
        "assim promovidos passam no gate de preparação."
    ),
    "preparar_movimentacao_lote": (
        "Prepara a movimentação de um lote de processos para uma tarefa destino. "
        "Se modo='simular' (default), executa a triagem offline e gera o lote_hash. "
        "Se modo='preparar' e confirmar_preparacao=True, seleciona os processos na UI e "
        "escolhe o destino, congelando a sessão na tela. Exige transição "
        "previamente confirmada como reversível; o clique final é sempre humano."
    ),
    "conferir_movimentacao_lote": (
        "Compara o estado atual das tarefas com o snapshot anterior para verificar a "
        "efetivação das movimentações do lote e encerra a sessão assistida."
    ),
    "elaborar_minutas_lote": (
        "Gera e salva minutas em lote na pasta local dos processos elegíveis "
        "sem alterar o fluxo no PJe."
    ),
}

_ALIAS_ATUACAO = {
    "consultar_destino": "consultar_destino_tarefa",
    "qual_destino": "consultar_destino_tarefa",
    "destino_tarefa": "consultar_destino_tarefa",
    "mapear_transicoes": "mapear_transicoes_tarefa",
    "confirmar_reversibilidade": "confirmar_reversibilidade_transicao",
    "promover_transicao": "confirmar_reversibilidade_transicao",
    "simular_lote": "preparar_movimentacao_lote",
    "preparar_lote": "preparar_movimentacao_lote",
    "conferir_lote": "conferir_movimentacao_lote",
    "reconciliar_lote": "conferir_movimentacao_lote",
    "minutar_lote": "elaborar_minutas_lote",
}

_TOKEN_CONFIRMA_REVERSIVEL = "CONFIRMO_REVERSIVEL_ANTES_COMMIT"

@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
    structured_output=True,
)
@auditar_ferramenta
async def atuar_fluxo_tarefas_pje(
    acao: str,
    persona: str = "servidor",
    grau: str = "1",
    perfil: str = "",
    nome_tarefa: str = "",
    modo: str = "simular",
    processos_ids: str = "",
    destino_id: str = "",
    lote_hash: str = "",
    minuta_texto: str = "",
    tipo: str = "Decisão",
    formato: str = "docx",
    confirmar_preparacao: bool = False,
    confirmation_token: str = "",
    evidencia: str = "",
    capturar_screenshot: bool = False,
) -> Dict[str, Any]:
    """
    Atuação assistida no fluxo de tarefas e caixas do PJe.

    acao: 'mapear_transicoes_tarefa' | 'confirmar_reversibilidade_transicao'
        | 'preparar_movimentacao_lote' | 'conferir_movimentacao_lote'
        | 'elaborar_minutas_lote'

    Parâmetros:
    - nome_tarefa: nome exato da tarefa/caixa do painel.
    - processos_ids: lista separada por vírgulas de CNJs ou IDs de processos.
    - destino_id: ID do destino selecionado mapeado na fase de descoberta.
    - modo: 'simular' (triagem e hash offline) ou 'preparar' (interage na UI).
    - confirmar_preparacao: True para efetivamente selecionar e interagir na UI
      (exige também modo='preparar').
    - confirmation_token / evidencia: exigidos por
      confirmar_reversibilidade_transicao.

    O commit final (clicar o botão que movimenta) é sempre humano; nenhuma
    ação desta ferramenta o executa.
    """
    canonica, erro = _resolver_acao(acao, _ACOES_ATUACAO, _ALIAS_ATUACAO)
    if erro:
        erro["acoes"] = _ACOES_ATUACAO
        return erro

    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    perfil_contexto.definir_contexto(p, g, perfil)

    exige_perfil = perfil_contexto.erro_perfil_obrigatorio(p)
    if exige_perfil:
        return exige_perfil

    if canonica == "consultar_destino_tarefa":
        # Consulta puramente local (playbook + banco de transições);
        # não exige sessão viva no PJe.
        if not nome_tarefa:
            return {"erro": "Parâmetro 'nome_tarefa' é obrigatório para consultar o destino."}

        playbook_path = os.environ.get("PJE_TASK_POLICY_PATH", "")
        if not playbook_path:
            policies_dir = Path(__file__).resolve().parents[1] / "policies"
            draft1 = policies_dir / "familia_maraba_draft_1.json"
            draft0 = policies_dir / "familia_maraba_draft_0.json"
            if draft1.exists():
                playbook_path = str(draft1)
            elif draft0.exists():
                playbook_path = str(draft0)

        policy = None
        playbook_erro = None
        if playbook_path:
            try:
                policy = auditoria_processual.load_policy(playbook_path)
            except Exception as exc:
                playbook_erro = str(exc)
        else:
            playbook_erro = "playbook não configurado; defina PJE_TASK_POLICY_PATH"

        task_policy = None
        if policy:
            task_policy = auditoria_processual.policy_task_for_name(policy, nome_tarefa)

        transicoes_mapeadas = await asyncio.to_thread(
            caixas_tarefas.obter_transicoes_para_perfil,
            rotulo=perfil,
            persona=p,
            grau=g,
            tarefa=nome_tarefa,
        )

        destino_recomendado = None
        regra_id = None
        if task_policy and policy:
            allowed_ids = task_policy.get("allowed_destination_ids") or []
            for r in policy.get("rules", []):
                if r.get("current_task_id") == task_policy.get("id"):
                    if r.get("outcome") == "move" and r.get("allowed_destination_ids"):
                        dest_id = r["allowed_destination_ids"][0]
                        regra_id = r.get("id")
                        for t in policy.get("tasks", []):
                            if t.get("id") == dest_id:
                                destino_recomendado = t.get("canonical_name")
                                break
                        break
            if not destino_recomendado and allowed_ids:
                dest_id = allowed_ids[0]
                for t in policy.get("tasks", []):
                    if t.get("id") == dest_id:
                        destino_recomendado = t.get("canonical_name")
                        break

        # O mapeamento heurístico salva centenas de opções de <select> como
        # INDETERMINADA; sem este corte a resposta estoura o limite do MCP.
        _dest_norm = (destino_recomendado or "").casefold()
        relevantes = [
            t
            for t in transicoes_mapeadas
            if t.get("reversivel") != "INDETERMINADA"
            or (_dest_norm and _dest_norm in str(t.get("destino_nome") or "").casefold())
        ]
        fundamento = {
            "playbook": policy.get("version") if policy else None,
            "playbook_path": playbook_path if policy else None,
            "regra_id": regra_id,
            "transicoes_mapeadas": (relevantes or transicoes_mapeadas)[:20],
            "total_transicoes_mapeadas": len(transicoes_mapeadas),
        }
        if playbook_erro:
            fundamento["playbook_erro"] = playbook_erro

        res = {
            "tarefa": nome_tarefa,
            "tarefa_canonica": task_policy.get("canonical_name") if task_policy else nome_tarefa,
            "destino_recomendado": destino_recomendado,
            "fundamento": fundamento,
            "commit_humano_obrigatorio": True,
        }
        return _marcar_grau(res, p, g)

    pje = await cliente_singleton.get_cliente(p, g, permitir_sem_perfil=False)

    if canonica == "mapear_transicoes_tarefa":
        if not nome_tarefa:
            return {"erro": "Parâmetro 'nome_tarefa' é obrigatório para mapear transições."}
        resultado = await pje.mapear_transicoes_caixa(nome_tarefa)
        return _marcar_grau(resultado, p, g)

    if canonica == "confirmar_reversibilidade_transicao":
        if not nome_tarefa or not destino_id:
            return {
                "erro": (
                    "Parâmetros 'nome_tarefa' e 'destino_id' são obrigatórios "
                    "para confirmar reversibilidade."
                )
            }
        if confirmation_token != _TOKEN_CONFIRMA_REVERSIVEL:
            return {
                "erro": (
                    "Confirmação humana ausente: repita com "
                    f"confirmation_token='{_TOKEN_CONFIRMA_REVERSIVEL}' após "
                    "verificar, na tela, que escolher este destino NÃO "
                    "movimenta o processo sem um botão de confirmação."
                ),
                "promovido": False,
            }
        if not (evidencia or "").strip():
            return {
                "erro": (
                    "Parâmetro 'evidencia' é obrigatório: descreva como a "
                    "reversibilidade foi verificada (tela, item usado, "
                    "resultado observado)."
                ),
                "promovido": False,
            }
        try:
            resultado = await pje.confirmar_reversibilidade_transicao(
                nome_tarefa=nome_tarefa,
                destino_id=destino_id,
                evidencia=evidencia,
            )
        except ValueError as exc:
            return {"erro": str(exc), "promovido": False}
        resultado["promovido"] = True
        return _marcar_grau(resultado, p, g)

    if canonica == "preparar_movimentacao_lote":
        if not nome_tarefa:
            return {"erro": "Parâmetro 'nome_tarefa' é obrigatório para preparar movimentação."}
        if not processos_ids:
            return {"erro": "Parâmetro 'processos_ids' é obrigatório."}
        interagir_na_ui = confirmar_preparacao and modo == "preparar"
        if confirmar_preparacao and modo != "preparar":
            return {
                "erro": (
                    "Para interagir na UI exija-se a dupla intenção: "
                    "modo='preparar' E confirmar_preparacao=True. Com "
                    f"modo='{modo}' a chamada seria ambígua; nada foi feito."
                )
            }
        if interagir_na_ui and not destino_id:
            return {"erro": "Parâmetro 'destino_id' é obrigatório em modo='preparar'."}

        lista_ids = [pid.strip() for pid in processos_ids.split(",") if pid.strip()]
        resultado = await pje.preparar_movimentacao_lote(
            snapshot_id="",
            processos_ids=lista_ids,
            destino_id=destino_id,
            nome_tarefa=nome_tarefa,
            confirmar_preparacao=interagir_na_ui
        )
        return _marcar_grau(resultado, p, g)
        
    if canonica == "conferir_movimentacao_lote":
        if not lote_hash:
            return {"erro": "Parâmetro 'lote_hash' é obrigatório para conferência."}
        resultado = await pje.conferir_movimentacao_lote(lote_hash)
        return _marcar_grau(resultado, p, g)

    if canonica == "elaborar_minutas_lote":
        if not processos_ids:
            return {"erro": "Parâmetro 'processos_ids' é obrigatório."}
        if not minuta_texto:
            return {"erro": "Parâmetro 'minuta_texto' é obrigatório."}
            
        lista_ids = [pid.strip() for pid in processos_ids.split(",") if pid.strip()]
        
        snap_recente = await asyncio.to_thread(
            caixas_tarefas.obter_snapshot,
            snapshot_id=None,
            grau=g,
            persona=p,
            incluir_caixas=False
        )
        snapshot_id = snap_recente.get("snapshot_id")
        if not snapshot_id:
            return {"erro": "Nenhum snapshot de acervo encontrado. Sincronize as caixas primeiro."}
            
        simulacao = await asyncio.to_thread(
            caixas_tarefas.simular_lote,
            snapshot_id=snapshot_id,
            processos_ids=lista_ids
        )
        
        if not simulacao.get("valido"):
            return simulacao
            
        import minutas
        resultados = []
        for item in simulacao["itens"]:
            if item["status"] == "INCLUIDO":
                r = await asyncio.to_thread(
                    minutas.salvar_peca,
                    numero_cnj=item["numero_processo"],
                    conteudo=minuta_texto,
                    tipo=tipo or "Decisão",
                    formato=formato or "docx",
                    grau=g + "g"
                )
                resultados.append(r)
                
        return _marcar_grau({
            "status": "concluido",
            "total_processados": len(lista_ids),
            "total_minutados": len(resultados),
            "resultados": resultados
        }, p, g)


async def protocolar_processo_pje(
    acao: str = "mapear",
    materia: str = "",
    jurisdicao: str = "",
    classe_judicial: str = "",
    assunto: str = "",
    codigo_assunto: str = "",
    polo: str = "ativo",
    cpf: str = "",
    caminho_pdf: str = "",
    descricao_documento: str = "Documento sintético para teste",
    tipo_documento: str = "Petição Inicial",
    quantidade_pdfs: int = 2,
    confirmar_preparacao: bool = False,
    persona: str = "advogado",
    grau: str = "1",
) -> dict:
    """Prepara o cadastro de processo com protocolo final sempre humano.

    acao: 'mapear' | 'avancar_dados_iniciais' | 'mapear_assuntos'
        | 'adicionar_assunto'
        | 'mapear_partes'
        | 'mapear_form_parte'
        | 'pesquisar_parte_cpf'
        | 'adicionar_parte_cpf'
        | 'mapear_documentos'
        | 'criar_pdfs_teste'
        | 'anexar_pdf_teste'

    'mapear' pode preencher as opções informadas apenas para carregar os
    selects dependentes, sem clicar em Incluir. 'avancar_dados_iniciais'
    exige confirmação explícita e clica somente em Incluir.
    Esta ferramenta nunca clica em Protocolar, Assinar ou Enviar processo.
    """
    return {
        "status": "desabilitado_por_politica",
        "erro": ("preparação e protocolo não pertencem ao MCP observador/read-only"),
        "read_only": True,
        "protocolo_executado": False,
    }


_ACOES_PAINEL = {
    "expedientes_pendentes": "Caixa de entrada: expedientes pendentes de ciência/resposta.",
    "prazos_urgentes": "Expedientes com data limite dentro de 'dias_urgentes'.",
    "risco_prazos": "Matriz de risco dos prazos pendentes (Crítico → Baixo).",
    "estatisticas": "Dashboard analítico da caixa de expedientes.",
    "calcular_prazos": "Vencimento de um prazo em dias úteis. Exige 'data_inicial'.",
    "prazo_recursal": "Prazo legal por tipo de ato (CPC/2015) + vencimento. Exige 'data_inicial' e 'tipo_prazo'.",
    "feriados": "Calendário forense do ano — o que o cálculo de prazo considera e o que não.",
    "diagnosticar_caixas": (
        "LISTA OS PERFIS FUNCIONAIS disponíveis no PJe (não exige 'perfil') "
        "e descobre, em modo somente leitura, o painel e os controles de "
        "caixas/tarefas visíveis ao perfil ativo. É o ponto de partida quando "
        "não se conhece o rótulo do perfil."
    ),
    "sessao_contexto_fixado": (
        "Cria um navegador interno isolado, usa o perfil apenas como "
        "localizador e fixa a sessão após validar todo o contexto."
    ),
    "sincronizar_caixas": (
        "Sincroniza TODAS as caixas pela API do painel; modo incremental "
        "reaproveita conteúdo apenas após confirmação por hash e modo "
        "integral força recoleta. Só conclui quando as contagens fecham."
    ),
    "listar_caixas": (
        "Lista todas as caixas do último snapshot, com contagem declarada, "
        "coletada, hash e eventual divergência; não abre o navegador."
    ),
    "listar_processos_caixa": (
        "Consulta paginada e pesquisável dos metadados do inventário, sem "
        "transportar milhares de registros numa única resposta."
    ),
    "consultar_acervo_estruturado": (
        "Retorna contrato pje.acervo-tarefas/v2 pronto para UI: processo, "
        "classe TPU resolvida, tarefas normalizadas, polos preservados, fluxo, "
        "datas, flags, capacidades, qualidade e facetas completas. Para "
        "acervos grandes, use formato='compacto'."
    ),
    "exportar_acervo": (
        "Gera NDJSON/CSV/JSON/SQLite gzip criptografado e retorna manifesto."
    ),
    "ler_export_acervo": (
        "Revalida autorização, TTL e integridade antes de descriptografar."
    ),
    "estatisticas_acervo": (
        "Calcula quantidade, mediana, P90, máximo e indicadores agrupados "
        "por tarefa, classe, assunto, órgão ou mês de chegada."
    ),
    "schema_acervo_tarefas": (
        "Descreve campos, filtros, ordenações e limites da fonte sem retornar "
        "processos."
    ),
    "metricas_desempenho_acervo": (
        "Retorna p50/p95/p99 locais das consultas do acervo, sem dados "
        "processuais e sem abrir navegador."
    ),
    "carregar_calendario_comarca": (
        "Carrega os feriados locais de uma comarca específica. Exige 'termo_busca'."
    ),
}

_ALIAS_PAINEL = {
    "painel": "expedientes_pendentes",
    "expedientes": "expedientes_pendentes",
    "caixa_de_entrada": "expedientes_pendentes",
    "pendentes": "expedientes_pendentes",
    "urgentes": "prazos_urgentes",
    "prazos": "prazos_urgentes",
    "vencendo": "prazos_urgentes",
    "risco": "risco_prazos",
    "matriz_risco": "risco_prazos",
    "dashboard": "estatisticas",
    "bi": "estatisticas",
    "calcular": "calcular_prazos",
    "calculo_prazo": "calcular_prazos",
    "vencimento": "calcular_prazos",
    "recurso": "prazo_recursal",
    "prazo_recurso": "prazo_recursal",
    "prazo_legal": "prazo_recursal",
    "prazo_por_tipo": "prazo_recursal",
    "calendario": "feriados",
    "calendario_forense": "feriados",
    "dias_uteis": "feriados",
    "suspensoes": "feriados",
    "eh_dia_util": "feriados",
    "caixas": "listar_caixas",
    "tarefas": "listar_caixas",
    "diagnostico_caixas": "diagnosticar_caixas",
    "listar_perfis": "diagnosticar_caixas",
    "perfis": "diagnosticar_caixas",
    "perfis_disponiveis": "diagnosticar_caixas",
    "listar_lotacoes": "diagnosticar_caixas",
    "lotacoes": "diagnosticar_caixas",
    "meus_perfis": "diagnosticar_caixas",
    "inventariar_caixas": "sincronizar_caixas",
    "atualizar_caixas": "sincronizar_caixas",
    "catalogar_caixas": "sincronizar_caixas",
    "processos_da_caixa": "listar_processos_caixa",
    "consultar_caixa": "listar_processos_caixa",
    "acervo_estruturado": "consultar_acervo_estruturado",
    "consultar_acervo": "consultar_acervo_estruturado",
    "dados_para_dashboard": "consultar_acervo_estruturado",
    "exportar_acervo_tarefas": "exportar_acervo",
    "baixar_acervo": "exportar_acervo",
    "ler_export": "ler_export_acervo",
    "metricas_acervo": "estatisticas_acervo",
    "agregar_acervo": "estatisticas_acervo",
    "schema_acervo": "schema_acervo_tarefas",
    "campos_acervo": "schema_acervo_tarefas",
    "performance_acervo": "metricas_desempenho_acervo",
    "latencia_acervo": "metricas_desempenho_acervo",
    "percentis_acervo": "metricas_desempenho_acervo",
    "cobertura_caixas": "listar_caixas",
    "comarca": "carregar_calendario_comarca",
    "carregar_comarca": "carregar_calendario_comarca",
}


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
    structured_output=True,
)
@auditar_ferramenta
async def painel_e_prazos_pje(
    acao: str,
    persona: str = "servidor",
    grau: str = "1",
    perfil: str = "",
    dias_urgentes: int = 3,
    data_inicial: str = None,
    dias_uteis: int = 15,
    tipo_prazo: str = "",
    dobro: bool = False,
    feriados_extras: str = "",
    ano: int = 0,
    snapshot_id: str = "",
    nome_tarefa: str = "",
    termo_busca: str = "",
    filtro_classe: str = "",
    filtro_assunto: str = "",
    filtro_parte: str = "",
    filtro_orgao: str = "",
    filtro_etiqueta: str = "",
    sigiloso: bool | None = None,
    prioridade: bool | None = None,
    conferido: bool | None = None,
    morador_de_rua: bool | None = None,
    data_chegada_de: str = "",
    data_chegada_ate: str = "",
    dias_na_tarefa_min: int | None = None,
    dias_na_tarefa_max: int | None = None,
    ordenar_por: str = "data_chegada",
    direcao: str = "asc",
    incluir_facetas: bool = True,
    incluir_campos_extras: bool = True,
    pagina: int = 1,
    itens_por_pagina: int = 50,
    incluir_metadados_origem: bool = False,
    formato: str = "completo",
    campos: str = "",
    envelope: str = "completo",
    if_revision: str = "",
    cursor: str = "",
    formato_arquivo: str = "ndjson",
    modo: str = "",
    dimensao: str = "tarefa",
    metricas: str = (
        "quantidade,mediana_dias,p90_dias,max_dias,prioritarios,"
        "sigilosos,maiores_180,maiores_365"
    ),
    concorrencia: int = 4,
    max_retentativas: int = 3,
    lotacao: str = "",
    export_id: str = "",
    autorizacao_leitura: bool = False,
    autorizacao_ref: str = "",
    diagnostico_profundo: bool = False,
    confirmar_consulta: str = "",
    confirmation_token: str = "",
    confirmar_localizacao_nao_aplicavel: str = "",
    capturar_screenshot: bool = False,
) -> Dict[str, Any]:
    """
    Painel do usuário, prazos, estatísticas e cálculo de prazos processuais.

    acao: 'expedientes_pendentes' | 'prazos_urgentes' | 'risco_prazos'
        | 'calcular_prazos' | 'estatisticas' | 'prazo_recursal' | 'feriados'
        | 'diagnosticar_caixas' | 'sincronizar_caixas' | 'listar_caixas'
        | 'sessao_contexto_fixado'
        | 'listar_processos_caixa' | 'consultar_acervo_estruturado'
        | 'exportar_acervo' | 'estatisticas_acervo'
        | 'ler_export_acervo'
        | 'schema_acervo_tarefas'
        | 'metricas_desempenho_acervo'

    Parâmetros por ação:
    - diagnosticar_caixas (aliases: listar_perfis, perfis, lotacoes): NÃO
      exige 'perfil' — lista os perfis funcionais disponíveis no PJe
      ('perfis_disponiveis', com rótulo completo pronto para usar nas demais
      ações) e os controles do painel. Use primeiro quando não souber o
      rótulo do perfil.
    - prazos_urgentes: dias_urgentes = janela de urgência (default 3).
    - calcular_prazos: exige data_inicial; dias_uteis = tamanho do prazo
      (default 15, o prazo comum do CPC).
    - prazo_recursal: exige data_inicial e tipo_prazo ('apelacao', 'embargos',
      'contestacao', 'recurso_inominado'...). O prazo legal vem da tabela do
      CPC/2015 — não precisa informar dias. dobro=True aplica o prazo em
      dobro da Fazenda/MP/Defensoria.
    - feriados: calendário forense de 'ano' (ou do ano de data_inicial, ou o
      corrente). Com data_inicial preenchida, diz se aquele dia é útil.
    - feriados_extras (nas 3 ações de cálculo e em 'feriados'): datas de
      feriado estadual/municipal ou suspensão do TJPA, separadas por vírgula.
    - sincronizar_caixas: modo='incremental' (default) reaproveita caixas
      somente após confirmação por hash; modo='integral' força recoleta.
      concorrencia=1..8 e
      max_retentativas=1..5. 'lotacao' seleciona explicitamente a unidade/
      papel interno (ex.: 'Vara de Família'). Persiste snapshot auditável.
    - listar_caixas: lê o último snapshot ou o snapshot_id informado.
    - listar_processos_caixa: nome_tarefa e termo_busca são filtros opcionais;
      pagina/itens_por_pagina controlam a resposta (máximo 500).
      incluir_metadados_origem=True inclui o JSON integral recebido do PJe.
    - consultar_acervo_estruturado: contrato versionado pronto para interface,
      com polos separados, classe TPU resolvida sem esconder ambiguidades,
      tarefa original + normalizada, idades da fila, capacidades em lote,
      qualidade e facetas globais completas para tarefa/classe/assunto/órgão.
      Cada opção traz parâmetro e valor exatos para o filtro. Aceita filtros
      por sigla, código ou descrição da classe, assunto, parte, órgão, etiqueta,
      flags, datas e dias na tarefa.
      Para acervos grandes, prefira formato='compacto' (até 5.000 itens por
      página) e use campos='cnj,tarefa,classe,dias,flags' para projeção.
      envelope='minimo' limita metadados e dicionários à página atual;
      if_revision evita retransmitir snapshot local inalterado.
      cursor percorre páginas do mesmo snapshot e falha se filtros mudarem.
      O formato='completo' permanece o default para compatibilidade.
    - exportar_acervo: formato_arquivo='ndjson'|'csv'|'json'|'sqlite',
      modo='compacto'|'completo'. Retorna manifesto pequeno com arquivo,
      sha256 e identificador opaco; o conteúdo cifrado expira em 7 dias.
      A leitura exige ler_export_acervo e a mesma autorizacao_ref.
    - estatisticas_acervo: dimensao='tarefa'|'classe'|'assunto'|'orgao'|
      'mes_chegada'; metricas é uma lista CSV.
    - schema_acervo_tarefas: catálogo dos campos e limites da fonte; não lê
      registros nem abre o navegador.
    - metricas_desempenho_acervo: p50/p95/p99 em memória, sem CNJ, partes
      ou termos de busca; não abre o navegador.
    """
    parametros_confirmacao = _parametros_confirmacao_chamada(locals())
    canonica, erro = _resolver_acao(acao, _ACOES_PAINEL, _ALIAS_PAINEL)
    if erro:
        erro["acoes"] = _ACOES_PAINEL
        return erro
    if canonica in {
        "expedientes_pendentes",
        "prazos_urgentes",
        "risco_prazos",
        "estatisticas",
        "diagnosticar_caixas",
        "sessao_contexto_fixado",
        "sincronizar_caixas",
    }:
        confirmacao = _exigir_confirmacao_consulta(
            "painel_e_prazos_pje",
            canonica,
            parametros_confirmacao,
            confirmar_consulta,
            confirmation_token,
        )
        if confirmacao:
            return confirmacao

    p = _normaliza_persona(persona)
    g = _normaliza_grau(grau)
    perfil_contexto.definir_contexto(p, g, perfil)

    if canonica == "feriados":
        return await consultar_calendario_forense(
            ano=ano, data_inicial=data_inicial or "", feriados_extras=feriados_extras
        )
    if canonica == "diagnosticar_caixas":
        return await diagnosticar_caixas_tarefas(
            persona=persona,
            grau=grau,
            diagnostico_profundo=diagnostico_profundo,
        )
    if canonica == "sessao_contexto_fixado":
        return await criar_sessao_contexto_fixado(
            persona=persona,
            grau=grau,
            perfil=perfil,
            confirmar_localizacao_nao_aplicavel=(
                confirmar_localizacao_nao_aplicavel
            ),
        )
    if canonica == "schema_acervo_tarefas":
        return _marcar_grau(
            caixas_tarefas.obter_schema_acervo_tarefas(),
            _normaliza_persona(persona),
            _normaliza_grau(grau),
        )
    if canonica == "metricas_desempenho_acervo":
        return _marcar_grau(
            observabilidade_acervo.resumo(),
            _normaliza_persona(persona),
            _normaliza_grau(grau),
        )
    exige_perfil = perfil_contexto.erro_perfil_obrigatorio(p)
    if exige_perfil:
        return exige_perfil
    exige_id_estavel = perfil_contexto.erro_identificador_estavel(p)
    if exige_id_estavel and not cliente_singleton.promover_contexto_fixado(
        p, g, perfil
    ):
        # Sem pje_id do PJe, a única passagem é a sessão fixada por rótulo
        # (sessao_contexto_fixado), que get_cliente revalida contra o PJe.
        exige_id_estavel["dica"] = (
            "Sem identificador do PJe, valide primeiro com "
            "acao='sessao_contexto_fixado' e repita com o mesmo perfil."
        )
        return exige_id_estavel
    if canonica == "sincronizar_caixas":
        if lotacao and perfil_contexto.normalizar_texto(lotacao) != (
            perfil_contexto.normalizar_texto(perfil)
        ):
            return {
                "erro": "lotacao e perfil identificam escopos diferentes",
                "codigo": "PERFIL_DIVERGENTE",
                "perfil": perfil,
                "lotacao": lotacao,
            }
        return await sincronizar_inventario_caixas(
            persona=persona,
            grau=grau,
            concorrencia=concorrencia,
            max_retentativas=max_retentativas,
            lotacao=perfil,
            modo=modo or "incremental",
        )
    if canonica == "listar_caixas":
        return await listar_inventario_caixas(
            persona=persona,
            grau=grau,
            snapshot_id=snapshot_id,
        )
    if canonica == "listar_processos_caixa":
        return await consultar_inventario_processos(
            persona=persona,
            grau=grau,
            snapshot_id=snapshot_id,
            nome_tarefa=nome_tarefa,
            termo_busca=termo_busca,
            pagina=pagina,
            itens_por_pagina=itens_por_pagina,
            incluir_metadados_origem=incluir_metadados_origem,
        )
    if canonica == "consultar_acervo_estruturado":
        return await consultar_acervo_tarefas_estruturado(
            persona=persona,
            grau=grau,
            snapshot_id=snapshot_id,
            nome_tarefa=nome_tarefa,
            termo_busca=termo_busca,
            filtro_classe=filtro_classe,
            filtro_assunto=filtro_assunto,
            filtro_parte=filtro_parte,
            filtro_orgao=filtro_orgao,
            filtro_etiqueta=filtro_etiqueta,
            sigiloso=sigiloso,
            prioridade=prioridade,
            conferido=conferido,
            morador_de_rua=morador_de_rua,
            data_chegada_de=data_chegada_de,
            data_chegada_ate=data_chegada_ate,
            dias_na_tarefa_min=dias_na_tarefa_min,
            dias_na_tarefa_max=dias_na_tarefa_max,
            pagina=pagina,
            itens_por_pagina=itens_por_pagina,
            ordenar_por=ordenar_por,
            direcao=direcao,
            incluir_facetas=incluir_facetas,
            incluir_campos_extras=incluir_campos_extras,
            incluir_metadados_origem=incluir_metadados_origem,
            formato=formato,
            campos=campos,
            envelope=envelope,
            if_revision=if_revision,
            cursor=cursor,
        )
    filtros_acervo = {
        "tarefa": nome_tarefa,
        "termo": termo_busca,
        "filtro_classe": filtro_classe,
        "filtro_assunto": filtro_assunto,
        "filtro_parte": filtro_parte,
        "filtro_orgao": filtro_orgao,
        "filtro_etiqueta": filtro_etiqueta,
        "sigiloso": sigiloso,
        "prioridade": prioridade,
        "conferido": conferido,
        "morador_de_rua": morador_de_rua,
        "data_chegada_de": data_chegada_de,
        "data_chegada_ate": data_chegada_ate,
        "dias_na_tarefa_min": dias_na_tarefa_min,
        "dias_na_tarefa_max": dias_na_tarefa_max,
        "ordenar_por": ordenar_por,
        "direcao": direcao,
    }
    if canonica == "exportar_acervo":
        if not autorizacao_leitura or not autorizacao_ref.strip():
            return {"erro": "autorização explícita é obrigatória para exportar"}
        return await exportar_acervo_tarefas(
            persona=persona,
            grau=grau,
            snapshot_id=snapshot_id,
            formato_arquivo=formato_arquivo,
            modo=modo or "compacto",
            autorizacao_ref=autorizacao_ref,
            **filtros_acervo,
        )
    if canonica == "ler_export_acervo":
        if not export_id or not autorizacao_leitura or not autorizacao_ref.strip():
            return {
                "erro": ("export_id e autorização explícita são obrigatórios para ler")
            }
        conteudo = await asyncio.to_thread(
            caixas_tarefas.ler_exportacao,
            export_id,
            autorizacao_ref,
        )
        limite = int(os.environ.get("PJE_MCP_EXPORT_MAX_BYTES", 10 * 1024 * 1024))
        if len(conteudo) > limite:
            return {
                "erro": "export excede o limite seguro de transporte MCP",
                "bytes": len(conteudo),
                "limite_bytes": limite,
                "export_id": export_id,
            }
        return {
            "export_id": export_id,
            "encoding": "base64",
            "mime_type": "application/gzip",
            "bytes": len(conteudo),
            "conteudo_base64": base64.b64encode(conteudo).decode("ascii"),
        }
    if canonica == "estatisticas_acervo":
        return await estatisticas_acervo_tarefas(
            persona=persona,
            grau=grau,
            snapshot_id=snapshot_id,
            dimensao=dimensao,
            metricas=metricas,
            **filtros_acervo,
        )

    # Ações de cálculo não abrem o PJe: valida a data antes de qualquer coisa.
    if canonica in ("calcular_prazos", "prazo_recursal"):
        falta = _exigir(
            "data_inicial",
            data_inicial,
            canonica,
            "Informe a data da intimação/publicação (YYYY-MM-DD ou DD/MM/YYYY).",
        )
        if falta:
            return falta
        if _parse_data_br(data_inicial) is None:
            return {
                "erro": f"Data inicial inválida: '{data_inicial}'.",
                "acao": canonica,
                "dica": "Use YYYY-MM-DD ou DD/MM/YYYY.",
            }

    if canonica == "prazo_recursal":
        falta = _exigir(
            "tipo_prazo",
            tipo_prazo,
            canonica,
            "Ex.: 'apelacao', 'embargos', 'contestacao', 'agravo'.",
        )
        if falta:
            falta["tipos_validos"] = sorted(PRAZOS_RECURSAIS)
            return falta
        return await calcular_prazo_recursal(
            data_inicial, tipo_prazo, dobro=dobro, feriados_extras=feriados_extras
        )

    if canonica == "calcular_prazos":
        try:
            dias = int(dias_uteis)
        except (TypeError, ValueError):
            return {
                "erro": f"'dias_uteis' deve ser um número inteiro, veio: {dias_uteis!r}"
            }
        if dias < 1 or dias > 365:
            return {"erro": f"'dias_uteis' fora da faixa aceita (1..365): {dias}"}
        # Por keyword: a assinatura é (data_inicio, dias_uteis) — passar
        # posicional aqui já entregou prazo de 3 dias no lugar de 15.
        return await calcular_prazos_processuais(
            data_inicio=data_inicial, dias_uteis=dias, feriados_extras=feriados_extras
        )

    if canonica == "prazos_urgentes":
        try:
            janela = int(dias_urgentes)
        except (TypeError, ValueError):
            return {
                "erro": f"'dias_urgentes' deve ser um número inteiro, veio: {dias_urgentes!r}"
            }
        if janela < 0 or janela > 180:
            return {"erro": f"'dias_urgentes' fora da faixa aceita (0..180): {janela}"}
        # dias_limite é o PRIMEIRO parâmetro da função interna: posicional
        # colocava 'persona' aí e o grau era engolido.
        return await verificar_prazos_urgentes(
            dias_limite=janela, persona=persona, grau=grau
        )

    if canonica == "expedientes_pendentes":
        return await expedientes_pendentes(persona=persona, grau=grau)
    if canonica == "risco_prazos":
        return await analisar_risco_prazos(persona=persona, grau=grau)
    if canonica == "estatisticas":
        return await analisar_estatisticas_painel_expedientes(
            persona=persona, grau=grau
        )
    if canonica == "carregar_calendario_comarca":
        falta = _exigir(
            "termo_busca",
            termo_busca,
            canonica,
            "Informe o nome da comarca em 'termo_busca' (ex: 'belem', 'maraba').",
        )
        if falta:
            return falta
        return carregar_comarca(termo_busca)
    return {"erro": f"Ação não roteada: {canonica}"}


_ACOES_BUSCAR = {
    "busca_geral": (
        "Pesquisa pelo menu lateral 'Consulta processual', preservando o "
        "identificador sem inferir CPF, CNPJ ou CNJ."
    ),
    "consultar_numero": "Consulta um processo pelo número CNJ.",
    "cpf": "Processos em que o CPF informado é parte.",
    "cnpj": "Processos em que o CNPJ informado é parte.",
    "oab": "Processos do advogado pela inscrição OAB (aceita '12345/PA').",
    "nome_parte": "Processos pelo nome do autor/réu.",
    "nome_requerente": "Processos pelo nome no polo ativo (requerente/autor).",
    "nome_requerido": "Processos pelo nome no polo passivo (requerido/réu).",
    "nome_advogado": "Processos pelo nome do advogado/representante.",
    "outros_nomes": "Processos por alcunha ou outro nome cadastrado da parte.",
    "numero_documento": "Processos pelo número de documento cadastrado.",
    "assunto": "Processos pelo assunto informado no formulário nativo.",
    "classe_judicial": "Processos pela classe judicial.",
    "jurisdicao": "Processos por jurisdição (rótulo ou código da opção).",
    "orgao_julgador": "Processos por órgão julgador (rótulo ou código).",
    "prioridade_processual": "Processos por prioridade processual.",
    "data_autuacao": "Processos por data ou intervalo de autuação.",
    "valor_causa": "Processos por valor ou intervalo de valor da causa.",
    "movimento_processual": "Processos por movimento selecionado no autocompletar.",
    "orgao_origem_criminal": "Processos pelo órgão de origem do procedimento criminal.",
    "procedimento_criminal": "Processos pelo número e ano opcional do procedimento criminal.",
    "ano_procedimento_criminal": "Processos somente pelo ano do procedimento criminal.",
    "protocolo_policia": "Processos pelo protocolo de polícia.",
    "validar_cnj": "Só valida o dígito verificador do número, sem acessar o PJe.",
    "auto": "Detecta o critério pelo formato do valor e executa a busca.",
    "lote": "Vários valores de uma vez (separados por vírgula/quebra de linha), consolidados.",
    "corrigir_cnj": "Recalcula o dígito verificador de um ou mais CNJ — puro, não toca no PJe.",
}

_ALIASES_BUSCAR = {
    "geral": "busca_geral",
    "consulta_geral": "busca_geral",
    "consulta_processual": "busca_geral",
    "menu_geral": "busca_geral",
    "menu_lateral": "busca_geral",
    "cnj": "consultar_numero",
    "numero": "consultar_numero",
    "numero_cnj": "consultar_numero",
    "processo": "consultar_numero",
    "consultar": "consultar_numero",
    "parte": "nome_parte",
    "autor": "nome_requerente",
    "requerente": "nome_requerente",
    "polo_ativo": "nome_requerente",
    "reu": "nome_requerido",
    "requerido": "nome_requerido",
    "polo_passivo": "nome_requerido",
    "nome": "nome_parte",
    "advogado": "nome_advogado",
    "representante": "nome_advogado",
    "alcunha": "outros_nomes",
    "outro_nome": "outros_nomes",
    "nome_social": "outros_nomes",
    "numero_documento": "numero_documento",
    "documento": "numero_documento",
    "classe": "classe_judicial",
    "prioridade": "prioridade_processual",
    "movimento": "movimento_processual",
    "movimentacao": "movimento_processual",
    "movimentacao_processual": "movimento_processual",
    "numero_procedimento_criminal": "procedimento_criminal",
    "protocolo_policial": "protocolo_policia",
    "detectar": "auto",
    "automatico": "auto",
    "validar": "validar_cnj",
    "em_lote": "lote",
    "varios": "lote",
    "corrigir": "corrigir_cnj",
    "dv": "corrigir_cnj",
    "digito_verificador": "corrigir_cnj",
    "conferir_cnj": "corrigir_cnj",
}


def _normalizar_faixa_busca(
    acao: str, valor: str, valor_final: str = ""
) -> Any:
    """Normaliza intervalos avançados sem enviar entradas ambíguas ao PJe."""
    raw = str(valor or "").strip()
    raw_final = str(valor_final or "").strip()
    if (
        not raw or len(raw) > 200 or len(raw_final) > 200
        or any(ord(char) < 32 for char in f"{raw}{raw_final}")
    ):
        raise ValueError("valor de busca avançada inválido")
    pieces = re.split(r"\s*(?:\.\.|\sa\s)\s*", raw, maxsplit=1)
    inicio = pieces[0].strip()
    fim = str(raw_final or (pieces[1] if len(pieces) > 1 else inicio)).strip()
    if acao == "data_autuacao":
        try:
            data_inicio = datetime.strptime(inicio, "%d/%m/%Y")
            data_fim = datetime.strptime(fim, "%d/%m/%Y")
        except ValueError as exc:
            raise ValueError(
                "data_autuacao exige DD/MM/AAAA ou início..fim"
            ) from exc
        if data_inicio > data_fim:
            raise ValueError("intervalo de data de autuação está invertido")
        return {"inicio": inicio, "fim": fim}
    if acao == "valor_causa":
        def decimal_br(value: str) -> Decimal:
            normalized = re.sub(r"^R\$\s*", "", value, flags=re.I).replace(" ", "")
            if not re.fullmatch(
                r"(?:\d{1,3}(?:\.\d{3})*|\d+)(?:,\d{1,2})?|\d+(?:\.\d{1,2})?",
                normalized,
            ):
                raise ValueError("valor monetário inválido")
            if "," in normalized:
                normalized = normalized.replace(".", "").replace(",", ".")
            try:
                amount = Decimal(normalized)
            except InvalidOperation as exc:
                raise ValueError("valor monetário inválido") from exc
            if amount < 0:
                raise ValueError("valor monetário não pode ser negativo")
            return amount
        if decimal_br(inicio) > decimal_br(fim):
            raise ValueError("intervalo de valor da causa está invertido")
        return {"inicio": inicio, "fim": fim}
    if acao == "procedimento_criminal":
        match = re.fullmatch(r"(\d{1,30})(?:\s*[-/]\s*(\d{4}))?", raw)
        if not match:
            raise ValueError(
                "procedimento_criminal exige número e ano opcional (12345/2024)"
            )
        ano = str(valor_final or match.group(2) or "").strip()
        if ano and not re.fullmatch(r"\d{4}", ano):
            raise ValueError("ano do procedimento deve ter quatro dígitos")
        return {"numero": match.group(1), "ano": ano}
    if acao == "ano_procedimento_criminal" and not re.fullmatch(r"\d{4}", raw):
        raise ValueError("ano do procedimento deve ter quatro dígitos")
    return raw


async def _executar_busca(
    acao: str, valor: str, p: str, g: str, limite: int, uf_oab: str,
    valor_final: str = "",
) -> dict:
    """Roteia uma acao ja canonica para a busca correspondente."""
    if acao == "busca_geral":
        return await buscar_processo_geral(valor, limite, p, g)
    if acao == "consultar_numero":
        return await consultar_processo(valor, p, g)
    if acao == "cpf":
        return await buscar_por_cpf(valor, limite, p, g)
    if acao == "cnpj":
        return await buscar_por_cnpj(valor, limite, p, g)
    if acao == "oab":
        numero, letra, uf_detectada = _parse_oab_detalhada(valor)
        return await buscar_por_oab(
            f"{numero}{letra}", uf_detectada or uf_oab, limite, p, g
        )
    if acao == "nome_parte":
        return await buscar_por_nome_parte(valor, limite, p, g)
    if acao == "nome_requerente":
        return await buscar_por_nome_requerente(valor, limite, p, g)
    if acao == "nome_requerido":
        return await buscar_por_nome_requerido(valor, limite, p, g)
    if acao == "nome_advogado":
        return await buscar_por_nome_advogado(valor, limite, p, g)
    if acao == "outros_nomes":
        return await buscar_por_outros_nomes(valor, limite, p, g)
    if acao == "numero_documento":
        return await buscar_por_numero_documento(valor, limite, p, g)
    criterios_avancados = {
        "assunto", "classe_judicial", "jurisdicao", "orgao_julgador",
        "prioridade_processual", "data_autuacao", "valor_causa",
        "movimento_processual", "orgao_origem_criminal",
        "procedimento_criminal", "ano_procedimento_criminal",
        "protocolo_policia",
    }
    if acao in criterios_avancados:
        normalizado = _normalizar_faixa_busca(acao, valor, valor_final)
        return await buscar_por_criterio_avancado(
            acao, normalizado, limite, p, g
        )
    if acao == "validar_cnj":
        return await validar_numero_cnj(valor)
    return {"erro": f"Ação de busca não roteada: {acao}"}


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
    structured_output=True,
)
@auditar_ferramenta
async def buscar_processos_pje(
    acao: str,
    valor: str,
    valor_final: str = "",
    persona: str = "servidor",
    grau: str = "1",
    perfil: str = "",
    limite: int = 20,
    uf_oab: str = UF_OAB_PADRAO,
    confirmar_consulta: str = "",
    confirmation_token: str = "",
    capturar_screenshot: bool = False,
) -> Dict[str, Any]:
    """
    Ferramenta universal de busca de processos no PJe-TJPA.

    acao: 'busca_geral', 'auto', 'consultar_numero', 'cpf', 'cnpj', 'oab',
          'nome_parte', 'nome_requerente', 'nome_requerido', 'nome_advogado',
          'outros_nomes', 'numero_documento', 'assunto', 'classe_judicial',
          'jurisdicao', 'orgao_julgador', 'prioridade_processual',
          'data_autuacao', 'valor_causa', 'movimento_processual',
          'orgao_origem_criminal', 'procedimento_criminal',
          'ano_procedimento_criminal', 'protocolo_policia',
          'validar_cnj', 'corrigir_cnj', 'lote'
    valor: o dado da busca (CNJ, CPF, CNPJ, OAB ou nome). Em 'lote' e em
           'corrigir_cnj', vários valores separados por vírgula, ponto-e-vírgula
           ou quebra de linha.
    limite: teto de processos retornados por busca (1..100, padrão 20)
    valor_final: fim opcional para data_autuacao/valor_causa ou ano separado
                 de procedimento_criminal.
    uf_oab: UF da inscrição OAB quando não vier junto do valor (padrão PA)

    'busca_geral' tenta "Consulta processual" pelo menu lateral Angular. Se a
    rota não estiver publicada para o perfil, usa o item real
    Menu geral > Processo > Processo e informa o critério aplicado pelo
    formulário legado. Em 'auto', valores de 11 ou 14 dígitos com DV válido
    (CPF/CNPJ) caem em pesquisa dedicada; sem DV válido, preserva busca geral
    para não forçar interpretação.

    Nas ações específicas, CNJ, CPF e CNPJ são validados por dígito
    verificador antes de abrir o navegador. 'corrigir_cnj' faz só essa
    conferência, em lote e sem tocar no PJe.
    """
    parametros_confirmacao = _parametros_confirmacao_chamada(locals())
    canonica, erro = _resolver_acao(acao, _ACOES_BUSCAR, _ALIASES_BUSCAR)
    if erro:
        erro["acoes"] = _ACOES_BUSCAR
        return erro
    p, g, bloqueio_perfil = _ativar_perfil_ferramenta(
        persona, grau, perfil
    )
    if canonica not in {"validar_cnj", "corrigir_cnj"} and bloqueio_perfil:
        return bloqueio_perfil

    faltando = _exigir(
        "valor",
        valor,
        canonica,
        dica=(
            "Informe o identificador bruto, CNJ, CPF, CNPJ, OAB ou nome a pesquisar."
        ),
    )
    if faltando:
        return faltando

    limite = max(1, min(int(limite or 20), 100))
    uf_oab = (uf_oab or UF_OAB_PADRAO).strip().upper()
    if uf_oab not in UFS_BRASIL:
        return {
            "erro": f"UF de OAB inválida: '{uf_oab}'",
            "ufs_validas": sorted(UFS_BRASIL),
        }

    # Acao pura: resolve antes de qualquer normalizacao de persona/grau.
    if canonica == "corrigir_cnj":
        return await corrigir_numero_cnj(valor)

    if canonica == "lote":
        confirmacao = _exigir_confirmacao_consulta(
            "buscar_processos_pje",
            canonica,
            parametros_confirmacao,
            confirmar_consulta,
            confirmation_token,
        )
        if confirmacao:
            return confirmacao
        return await buscar_em_lote(valor, "auto", limite, uf_oab, 25, p, g)

    deteccao = None
    if canonica == "auto":
        deteccao = _detectar_tipo_busca(valor)
        canonica = deteccao["acao"]
        valor = deteccao.get("valor_normalizado") or valor

    # Barra documento malformado antes de gastar uma ida ao PJe (cada busca
    # abre o Chromium e navega o listView.seam).
    if canonica == "cpf" and not _cpf_valido(valor):
        return {
            "erro": "CPF inválido (dígitos verificadores não conferem).",
            "valor_recebido_mascarado": mask_process_search_value("cpf", valor),
            "dica": "Confira o número; para buscar por nome use acao='nome_parte'.",
        }
    if canonica == "cnpj" and not _cnpj_valido(valor):
        return {
            "erro": "CNPJ inválido (dígitos verificadores não conferem).",
            "valor_recebido_mascarado": mask_process_search_value("cnpj", valor),
            "dica": "Confira o número; para buscar por nome use acao='nome_parte'.",
        }
    # CNJ tambem: era o unico documento que passava direto pro Chromium.
    if canonica == "consultar_numero":
        a = _analisar_cnj(valor)
        if not a["valido"]:
            erro = {
                "erro": f"Número CNJ inválido — {a['motivo']}.",
                "valor_recebido_mascarado": mask_process_search_value(
                    "numero_cnj", valor
                ),
            }
            if a.get("numero_corrigido"):
                erro["numero_corrigido"] = a["numero_corrigido"]
                erro["dica"] = (
                    f"Provável erro de digitação. Tente {a['numero_corrigido']} "
                    f"(ou use acao='corrigir_cnj' para conferir vários de uma vez)."
                )
            else:
                erro["dica"] = (
                    "Informe os 20 dígitos do CNJ. Para achar o processo por "
                    "outro critério use acao='nome_parte', 'cpf' ou 'oab'."
                )
            return erro

    if canonica != "validar_cnj":
        confirmacao = _exigir_confirmacao_consulta(
            "buscar_processos_pje",
            canonica,
            parametros_confirmacao,
            confirmar_consulta,
            confirmation_token,
        )
        if confirmacao:
            return confirmacao

    try:
        resultado = await _executar_busca(
            canonica, valor, p, g, limite, uf_oab, valor_final
        )
    except ValueError as exc:
        return {
            "status": "erro",
            "codigo": "INVALID_REQUEST",
            "erro": str(exc),
            "criterio": canonica,
        }
    if isinstance(resultado, dict) and deteccao:
        resultado["criterio_detectado"] = canonica
        resultado["motivo_deteccao"] = deteccao["motivo"]
    return resultado


# Todas por KEYWORD. A lambda escondia o bug posicional da checagem AST: em
# 'movimentacoes' o `p` caía no slot `limite` (2º parâmetro), então o PJe
# recebia limite="advogado" e estourava TypeError no fatiamento; e o `grau`
# ia parar em `persona`, fazendo 2º grau consultar sempre o 1º.
_ACOES_ANALISAR = {
    "relatorio_completo": lambda cnj, p, g, t, lim: relatorio_processo(
        cnj, persona=p, grau=g
    ),
    "resumo_executivo": lambda cnj, p, g, t, lim: resumo_executivo_processo(
        cnj, persona=p, grau=g
    ),
    "diagnostico_saude": lambda cnj, p, g, t, lim: diagnosticar_saude_processual(
        cnj, persona=p, grau=g
    ),
    "linha_tempo": lambda cnj, p, g, t, lim: analisar_linha_do_tempo_processo(
        cnj, persona=p, grau=g
    ),
    "estrategia": lambda cnj, p, g, t, lim: recomendar_estrategia_processual(
        cnj, persona=p, grau=g
    ),
    "partes": lambda cnj, p, g, t, lim: quadro_partes_advogados(cnj, persona=p, grau=g),
    "pendencias": lambda cnj, p, g, t, lim: pendencias_processo(cnj, persona=p, grau=g),
    "historico_expedientes": lambda cnj, p, g, t, lim: expedientes_do_processo(
        cnj, persona=p, grau=g
    ),
    "movimentacoes": lambda cnj, p, g, t, lim: ultimas_movimentacoes(
        cnj, limite=lim, persona=p, grau=g
    ),
    "buscar_movimento": lambda cnj, p, g, t, lim: buscar_movimentacoes_por_termo(
        cnj, t, persona=p, grau=g
    ),
    "comparar": lambda cnj, p, g, t, lim: comparar_processos_detalhado(
        cnj, persona=p, grau=g
    ),
    "audiencias": lambda cnj, p, g, t, lim: audiencias_processo(cnj, persona=p, grau=g),
}

_ALIAS_ANALISAR = {
    "relatorio": "relatorio_completo",
    "completo": "relatorio_completo",
    "resumo": "resumo_executivo",
    "saude": "diagnostico_saude",
    "diagnostico": "diagnostico_saude",
    "score": "diagnostico_saude",
    "triagem": "diagnostico_saude",
    "timeline": "linha_tempo",
    "linha_do_tempo": "linha_tempo",
    "inatividade": "linha_tempo",
    "recomendacoes": "estrategia",
    "advogados": "partes",
    "prazos": "pendencias",
    "expedientes": "historico_expedientes",
    "movimentos": "movimentacoes",
    "andamentos": "movimentacoes",
    "buscar_movimentacao": "buscar_movimento",
    "comparacao": "comparar",
    "comparar_processos": "comparar",
    "confrontar": "comparar",
    "diferencas": "comparar",
    "conexao": "comparar",
    "audiencia": "audiencias",
    "agenda": "audiencias",
    "proxima_audiencia": "audiencias",
    "sessoes": "audiencias",
    "pauta": "audiencias",
}


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
    structured_output=True,
)
@auditar_ferramenta
async def analisar_processo_pje(
    acao: str,
    numero_cnj: str,
    persona: str = "servidor",
    grau: str = "1",
    perfil: str = "",
    termo_busca: str = "",
    limite: int = 5,
    confirmar_consulta: str = "",
    confirmation_token: str = "",
    capturar_screenshot: bool = False,
) -> Dict[str, Any]:
    """
    Obtém relatórios, partes, movimentos, pendências e inteligência sobre um processo.

    acao: 'relatorio_completo' | 'resumo_executivo' | 'diagnostico_saude'
        | 'linha_tempo' | 'estrategia' | 'partes' | 'pendencias'
        | 'historico_expedientes' | 'movimentacoes' | 'buscar_movimento'
        | 'comparar' | 'audiencias'

    'diagnostico_saude' devolve um score 0-100 para triagem (quanto menor, mais urgente).
    'buscar_movimento' exige 'termo_busca' e ignora acento ('sentenca' acha 'Sentença').
    'audiencias' monta a agenda de audiências do processo a partir das
    movimentações, com a próxima e quantos dias faltam.
    'comparar' recebe 2..10 números CNJ em 'numero_cnj', separados por vírgula,
    e aponta em que campos os processos divergem.
    'movimentacoes' usa 'limite' (default 5) para o número de movimentos.
    """
    parametros_confirmacao = _parametros_confirmacao_chamada(locals())
    canonica, erro = _resolver_acao(acao, _ACOES_ANALISAR, _ALIAS_ANALISAR)
    if erro:
        erro["acoes"] = sorted(_ACOES_ANALISAR)
        return erro
    _p, _g, bloqueio_perfil = _ativar_perfil_ferramenta(
        persona, grau, perfil
    )
    if bloqueio_perfil:
        return bloqueio_perfil

    faltando = _exigir(
        "numero_cnj",
        numero_cnj,
        canonica,
        "Informe o número CNJ do processo (com ou sem máscara).",
    )
    if faltando:
        return faltando

    if canonica == "buscar_movimento":
        faltando = _exigir(
            "termo_busca",
            termo_busca,
            canonica,
            "Informe o termo a procurar nas movimentações, ex.: 'sentença'.",
        )
        if faltando:
            return faltando

    try:
        lim = int(limite)
    except (TypeError, ValueError):
        return {"erro": f"'limite' deve ser um número inteiro, veio: {limite!r}"}
    if lim < 1 or lim > 200:
        return {"erro": f"'limite' fora da faixa aceita (1..200): {lim}"}

    # CNJ malformado ou com DV inválido não deve custar uma abertura de autos
    # no Chromium.
    # 'comparar' faz a própria validação da lista, então fica de fora.
    if canonica != "comparar":
        analise_cnj = _analisar_cnj(numero_cnj)
        if not analise_cnj.get("valido", False):
            erro_cnj = {
                "erro": f"Número CNJ inválido — {analise_cnj['motivo']}.",
                "valor_recebido_mascarado": _mascarar_cnj(numero_cnj),
                "acao": canonica,
                "dica": (
                    "Confira formato e DV. Para localizar ou corrigir o número, "
                    "use buscar_processos_pje."
                ),
            }
            if analise_cnj.get("numero_corrigido"):
                erro_cnj["numero_corrigido"] = analise_cnj["numero_corrigido"]
            return erro_cnj
    else:
        itens_comparacao = [
            v.strip()
            for v in re.split(r"[,;\n]+", numero_cnj)
            if v.strip()
        ]
        erro_comparacao = _validar_lista_comparacao(itens_comparacao)
        if erro_comparacao:
            return erro_comparacao

    confirmacao = _exigir_confirmacao_consulta(
        "analisar_processo_pje",
        canonica,
        parametros_confirmacao,
        confirmar_consulta,
        confirmation_token,
    )
    if confirmacao:
        return confirmacao

    return await _ACOES_ANALISAR[canonica](numero_cnj, persona, grau, termo_busca, lim)


_ACOES_DOCUMENTOS = {
    "listar": "Lista todas as peças dos autos (id + tipo + título).",
    "filtrar": "Filtra a lista de peças por palavra-chave/tipo (exige termo_busca).",
    "pesquisar_texto": "Busca profunda no TEOR das peças e devolve os trechos (exige termo_busca).",
    "indice_remissivo": "Índice das peças agrupadas por categoria processual.",
    "ler": "Lê o teor completo de UMA peça (exige id_doc).",
    "ultima_decisao": "Teor da última decisão/sentença/despacho.",
    "ultimo_despacho": "Teor do último despacho (só despacho).",
    "classificar_decisao": "Classifica juridicamente a última decisão.",
    "historico_decisorio": "Trilha completa dos atos decisórios, do mais recente ao mais antigo.",
    "citacoes_legais": "Base legal citada numa peça: artigos, súmulas, temas repetitivos, precedentes e leis.",
    "pecas_recentes": "Últimas peças juntadas aos autos — o que entrou desde a última olhada.",
}

_ALIAS_DOCUMENTOS = {
    "listar_documentos": "listar",
    "documentos": "listar",
    "buscar_texto": "pesquisar_texto",
    "pesquisar": "pesquisar_texto",
    "busca_textual": "pesquisar_texto",
    "indice": "indice_remissivo",
    "ler_documento": "ler",
    "ler_peca": "ler",
    "sentenca": "ultima_decisao",
    "decisao": "ultima_decisao",
    "despacho": "ultimo_despacho",
    "classificar": "classificar_decisao",
    "historico_decisoes": "historico_decisorio",
    "linha_decisoria": "historico_decisorio",
    "decisoes": "historico_decisorio",
    "citacoes": "citacoes_legais",
    "base_legal": "citacoes_legais",
    "fundamentos": "citacoes_legais",
    "jurisprudencia": "citacoes_legais",
    "dispositivos_citados": "citacoes_legais",
    "recentes": "pecas_recentes",
    "novidades": "pecas_recentes",
    "ultimas_pecas": "pecas_recentes",
    "juntadas": "pecas_recentes",
}


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
    structured_output=True,
)
@auditar_ferramenta
async def gerir_documentos_pje(
    acao: str,
    numero_cnj: str,
    persona: str = "servidor",
    grau: str = "1",
    perfil: str = "",
    id_doc: str = "",
    termo_busca: str = "",
    max_paginas: int = 30,
    max_documentos: int = 10,
    tempo_maximo_segundos: float = 45,
    limite: int = 20,
    incluir_teor: bool = False,
    confirmar_consulta: str = "",
    confirmation_token: str = "",
    capturar_screenshot: bool = False,
) -> Dict[str, Any]:
    """
    Leitura de peças e extração de teor (sentenças, decisões, documentos).

    acao: 'listar' | 'filtrar' | 'pesquisar_texto' | 'indice_remissivo' | 'ler'
        | 'ultima_decisao' | 'ultimo_despacho' | 'classificar_decisao'
        | 'historico_decisorio' | 'citacoes_legais' | 'pecas_recentes'

    Parâmetros por ação:
    - filtrar / pesquisar_texto: exigem termo_busca. Em 'pesquisar_texto',
      max_documentos limita quantas peças têm o teor lido (default 10).
      tempo_maximo_segundos (10..50, default 45) limita a operação inteira;
      se acabar, a resposta parcial informa cobertura e peças não tentadas.
    - ler: exige id_doc (descubra com acao='listar'). max_paginas limita a
      extração de PDFs longos (default 30); acima disso volta truncado.
    - historico_decisorio: limite = teto de atos retornados (0 = todos);
      incluir_teor=True lê e classifica cada ato (1 requisição por peça —
      use com limite baixo).
    - citacoes_legais: mapeia a base legal invocada (artigos, súmulas, temas
      repetitivos, precedentes, leis) com contagem e trecho de contexto. Sem
      id_doc, analisa a última decisão/sentença; max_paginas vale aqui também.
    - pecas_recentes: últimas peças juntadas, da mais nova para a mais antiga
      (ordem pelo id sequencial do PJe). 'limite' controla quantas (0 = todas)
      e 'termo_busca' restringe por tipo/título. Não lê teor — é barato.

    Quando a árvore de documentos não termina de carregar, o retorno traz
    'arvore_completa': false e 'aviso_arvore' — nesse caso os totais são piso,
    não o número real de peças.
    """
    parametros_confirmacao = _parametros_confirmacao_chamada(locals())
    canonica, erro = _resolver_acao(acao, _ACOES_DOCUMENTOS, _ALIAS_DOCUMENTOS)
    if erro:
        erro["acoes"] = _ACOES_DOCUMENTOS
        return erro
    _p, _g, bloqueio_perfil = _ativar_perfil_ferramenta(
        persona, grau, perfil
    )
    if bloqueio_perfil:
        return bloqueio_perfil

    cnj = _normaliza_cnj(numero_cnj)
    falta = _exigir("numero_cnj", cnj, canonica)
    if falta:
        return falta

    analise_cnj = _analisar_cnj(cnj)
    if not analise_cnj.get("valido", False):
        erro_cnj = {
            "erro": f"Número CNJ inválido — {analise_cnj['motivo']}.",
            "valor_recebido_mascarado": _mascarar_cnj(cnj),
            "acao": canonica,
            "dica": (
                "Confira formato e DV antes de abrir os autos. "
                "Use buscar_processos_pje(acao='corrigir_cnj')."
            ),
        }
        if analise_cnj.get("numero_corrigido"):
            erro_cnj["numero_corrigido"] = analise_cnj["numero_corrigido"]
        return erro_cnj

    try:
        paginas = int(max_paginas)
        documentos = int(max_documentos)
        segundos = float(tempo_maximo_segundos)
        limite_validado = int(limite)
    except (TypeError, ValueError):
        return {
            "erro": (
                "max_paginas, max_documentos, tempo_maximo_segundos e limite "
                "devem ser numéricos"
            )
        }
    if not 1 <= paginas <= 500:
        return {"erro": f"max_paginas fora da faixa aceita (1..500): {paginas}"}
    if not 1 <= documentos <= 100:
        return {
            "erro": f"max_documentos fora da faixa aceita (1..100): {documentos}"
        }
    if not 10 <= segundos <= 50:
        return {
            "erro": (
                "tempo_maximo_segundos fora da faixa aceita "
                f"(10..50): {segundos}"
            )
        }
    if not 0 <= limite_validado <= 500:
        return {"erro": f"limite fora da faixa aceita (0..500): {limite_validado}"}

    if canonica in ("filtrar", "pesquisar_texto"):
        falta = _exigir(
            "termo_busca",
            termo_busca,
            canonica,
            f"A ação '{canonica}' precisa do termo a procurar.",
        )
        if falta:
            return falta
        if len(termo_busca) > 200:
            return {
                "erro": "termo_busca excede o limite de 200 caracteres",
                "acao": canonica,
            }

    if canonica == "ler":
        falta = _exigir(
            "id_doc", id_doc, canonica, "Use acao='listar' pra descobrir o id da peça."
        )
        if falta:
            return falta
        if not str(id_doc).isdigit():
            return {
                "erro": "id_doc inválido; use somente o identificador numérico da peça",
                "acao": canonica,
            }

    confirmacao = _exigir_confirmacao_consulta(
        "gerir_documentos_pje",
        canonica,
        parametros_confirmacao,
        confirmar_consulta,
        confirmation_token,
    )
    if confirmacao:
        return confirmacao

    if canonica == "listar":
        return await listar_documentos(numero_cnj=cnj, persona=persona, grau=grau)

    if canonica == "filtrar":
        return await filtrar_documentos_processo(
            numero_cnj=cnj, termo=termo_busca, persona=persona, grau=grau
        )

    if canonica == "pesquisar_texto":
        return await pesquisar_autos_texto(
            numero_cnj=cnj,
            termo=termo_busca,
            max_documentos=documentos,
            max_paginas=paginas,
            tempo_maximo_segundos=segundos,
            persona=persona,
            grau=grau,
        )

    if canonica == "indice_remissivo":
        return await gerar_indice_remissivo_autos(
            numero_cnj=cnj, persona=persona, grau=grau
        )

    if canonica == "ler":
        return await ler_documento(
            numero_cnj=cnj,
            id_documento=id_doc,
            max_paginas=paginas,
            persona=persona,
            grau=grau,
        )

    if canonica == "ultima_decisao":
        return await ultima_decisao(numero_cnj=cnj, persona=persona, grau=grau)

    if canonica == "ultimo_despacho":
        return await ultimo_despacho(numero_cnj=cnj, persona=persona, grau=grau)

    if canonica == "classificar_decisao":
        return await classificar_teor_decisao(
            numero_cnj=cnj, persona=persona, grau=grau
        )

    if canonica == "citacoes_legais":
        return await extrair_citacoes_legais(
            numero_cnj=cnj,
            id_documento=id_doc,
            max_paginas=paginas,
            persona=persona,
            grau=grau,
        )

    if canonica == "pecas_recentes":
        return await pecas_recentes_processo(
            numero_cnj=cnj,
            limite=limite_validado,
            tipo_filtro=termo_busca,
            persona=persona,
            grau=grau,
        )

    return await historico_decisorio(
        numero_cnj=cnj,
        limite=limite_validado,
        incluir_teor=incluir_teor,
        max_paginas=paginas,
        persona=persona,
        grau=grau,
    )


_ACOES_DOWNLOAD = {
    "baixar_documento": "Baixa UMA peça pelo id_doc e salva em .../{cnj}/documentos/",
    "baixar_processo": "Baixa os autos COMPLETOS (PDF consolidado).",
    "status_download": "Acompanha um download disparado em background.",
    "preparar_processo": (
        "Baixa + decide estratégia (leitura direta ou ingestão local criptografada)."
    ),
    "listar_cache": "Inventário detalhado do cache local (só disco, não abre o PJe; exige perfil).",
    "limpar_cache": "Uso de disco e, sob confirmação, remoção dos PDFs em cache.",
    "verificar_integridade": "Confere se os PDFs em cache estão íntegros (só disco; exige perfil). Acha download interrompido.",
}

_ALIAS_DOWNLOAD = {
    "baixar_peca": "baixar_documento",
    "baixar_autos": "baixar_processo",
    "baixar": "baixar_processo",
    "status": "status_download",
    "preparar": "preparar_processo",
    "inventario": "listar_cache",
    "inventario_cache": "listar_cache",
    "listar_processos_baixados": "listar_cache",
    "cache": "listar_cache",
    "estatisticas_cache": "limpar_cache",
    "uso_disco": "limpar_cache",
    "integridade": "verificar_integridade",
    "verificar": "verificar_integridade",
    "conferir_cache": "verificar_integridade",
    "validar_cache": "verificar_integridade",
    "pdfs_corrompidos": "verificar_integridade",
}


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
    structured_output=True,
)
@auditar_ferramenta
async def download_e_cache_pje(
    acao: str,
    numero_cnj: str = "",
    persona: str = "servidor",
    grau: str = "1",
    perfil: str = "",
    id_doc: str = "",
    tipo_descritivo: str = "",
    metodo: str = "nativo",
    limite: int = 0,
    cronologia: str = "decrescente",
    forcar: bool = False,
    background: bool = True,
    apagar_pdfs: bool = False,
    confirmar: str = "",
    ordenar_por: str = "tamanho",
    confirmar_consulta: str = "",
    confirmation_token: str = "",
    capturar_screenshot: bool = False,
) -> Dict[str, Any]:
    """
    Gerencia downloads (síncronos/background), inventário e limpeza do cache de processos.

    acao: 'baixar_documento' | 'baixar_processo' | 'status_download'
        | 'preparar_processo' | 'listar_cache' | 'limpar_cache'
        | 'verificar_integridade'

    Parâmetros por ação:
    - baixar_documento: numero_cnj + id_doc (tipo_descritivo entra no nome do arquivo)
    - baixar_processo: numero_cnj; metodo='nativo' (completo, default) ou 'doc_a_doc'
      (salva peças separadas); background=True devolve 'em_andamento' em autos
      grandes — acompanhe com 'status_download'; forcar=True re-baixa ignorando cache;
      limite/cronologia só valem no 'doc_a_doc'
    - listar_cache: ordenar_por='tamanho'|'data'|'cnj'
    - limpar_cache: sem numero_cnj devolve o agregado do grau. Para APAGAR PDFs use
      apagar_pdfs=True + numero_cnj + confirmar=<o mesmo numero_cnj> (a remoção é
      irreversível e por isso exige a confirmação explícita).
    - verificar_integridade: sem numero_cnj varre o cache inteiro do grau. Só lê
      disco; aponta PDFs vazios, truncados ou sem '%%EOF' (download interrompido)
      que o cache continuaria reaproveitando.

    TODAS as ações — inclusive as que só leem disco — exigem perfil funcional
    selecionado quando a persona é interna (servidor/magistrado). O inventário
    e a verificação de integridade devolvem nomes de arquivo que são números
    CNJ, ou seja, revelam quais processos existem no cache; por isso a lotação
    precisa estar declarada antes. Sem perfil, o retorno é
    codigo='PERFIL_OBRIGATORIO' — use painel_e_prazos_pje(acao='listar_caixas')
    ou informe o rótulo completo em 'perfil'.

    metodo, cronologia e ordenar_por são normalizados (caixa/acento/apelido);
    valor desconhecido vira erro com a lista de válidos, em vez de cair no default.
    """
    parametros_confirmacao = _parametros_confirmacao_chamada(locals())
    canonica, erro = _resolver_acao(acao, _ACOES_DOWNLOAD, _ALIAS_DOWNLOAD)
    if erro:
        erro["acoes"] = _ACOES_DOWNLOAD
        return erro
    _p, _g, bloqueio_perfil = _ativar_perfil_ferramenta(
        persona, grau, perfil
    )
    if bloqueio_perfil:
        return bloqueio_perfil

    cnj = _normaliza_cnj(numero_cnj)

    if canonica == "verificar_integridade":
        return await verificar_integridade_cache(numero_cnj=cnj, grau=grau)

    if canonica in (
        "baixar_documento",
        "baixar_processo",
        "status_download",
        "preparar_processo",
    ):
        falta = _exigir("numero_cnj", cnj, canonica)
        if falta:
            return falta
        # Baixar autos é a operação mais cara do MCP e a validação do método
        # só acontecia lá dentro, DEPOIS de get_cliente() abrir e logar o
        # Chromium. CNJ torto ou opção escrita errada agora morre aqui.
        if len(_digitos(cnj)) not in (19, 20):
            return {
                "erro": f"Número CNJ inválido: '{numero_cnj}'",
                "acao": canonica,
                "dica": "O CNJ tem 20 dígitos. Confira com "
                "buscar_processos_pje(acao='corrigir_cnj').",
            }

    # Enums normalizados ANTES de qualquer acesso ao PJe. Antes, 'crescente'
    # escrito errado virava silenciosamente a ordem padrão.
    metodo, erro_opcao = _resolver_opcao(metodo, METODOS_DOWNLOAD, "metodo", "nativo")
    if erro_opcao:
        return erro_opcao
    cronologia, erro_opcao = _resolver_opcao(
        cronologia, CRONOLOGIAS, "cronologia", "decrescente"
    )
    if erro_opcao:
        return erro_opcao
    ordenar_por, erro_opcao = _resolver_opcao(
        ordenar_por, ORDENACOES_CACHE, "ordenar_por", "tamanho"
    )
    if erro_opcao:
        return erro_opcao

    try:
        limite = int(limite or 0)
    except (TypeError, ValueError):
        return {"erro": f"'limite' deve ser um número inteiro, veio: {limite!r}"}
    if limite < 0 or limite > 500:
        return {"erro": f"'limite' fora da faixa aceita (0..500, 0=todos): {limite}"}
    if limite and metodo != "doc_a_doc":
        return {
            "erro": "'limite' só se aplica ao metodo='doc_a_doc'.",
            "metodo_recebido": metodo,
            "dica": "No método nativo o PJe consolida os autos inteiros; "
            "para baixar só as N peças mais recentes use metodo='doc_a_doc'.",
        }

    if canonica in {"baixar_documento", "baixar_processo", "preparar_processo"}:
        confirmacao_consulta = _exigir_confirmacao_consulta(
            "download_e_cache_pje",
            canonica,
            parametros_confirmacao,
            confirmar_consulta,
            confirmation_token,
        )
        if confirmacao_consulta:
            return confirmacao_consulta

    if canonica == "baixar_documento":
        falta = _exigir(
            "id_doc",
            id_doc,
            canonica,
            "Use gerir_documentos_pje(acao='listar') pra descobrir o id da peça.",
        )
        if falta:
            return falta
        return await baixar_documento(
            numero_cnj=cnj,
            id_documento=id_doc,
            tipo_descritivo=tipo_descritivo,
            persona=persona,
            grau=grau,
        )

    if canonica == "baixar_processo":
        return await baixar_processo(
            numero_cnj=cnj,
            metodo=metodo,
            limite=limite,
            cronologia=cronologia,
            forcar=forcar,
            background=background,
            persona=persona,
            grau=grau,
        )

    if canonica == "status_download":
        return await status_download(numero_cnj=cnj, persona=persona, grau=grau)

    if canonica == "preparar_processo":
        return await preparar_processo(
            numero_cnj=cnj, forcar=forcar, persona=persona, grau=grau
        )

    if canonica == "listar_cache":
        return await inventariar_cache_processos(
            numero_cnj=cnj, ordenar_por=ordenar_por, grau=grau
        )

    # limpar_cache — única ação destrutiva do conjunto.
    if apagar_pdfs:
        if not cnj:
            return {
                "erro": "Remoção em massa não é permitida",
                "acao": "limpar_cache",
                "dica": "apagar_pdfs=True exige numero_cnj. Limpe um processo por vez.",
            }
        if _normaliza_cnj(confirmar) != cnj:
            return {
                "erro": "Confirmação ausente ou divergente",
                "acao": "limpar_cache",
                "numero_cnj": cnj,
                "dica": f"Para apagar os PDFs deste processo repita o número em "
                f"'confirmar' (confirmar='{cnj}'). A remoção é irreversível.",
            }

    return await limpar_cache_processos(
        numero_cnj=cnj, apagar_pdfs=apagar_pdfs, grau=grau
    )


_ACOES_PRODUCAO = {
    "listar_modelos": "Lista os modelos de petição disponíveis.",
    "ler_modelo": "Lê o texto cru de um modelo (exige arquivo).",
    "pesquisar_modelos": "Procura termo no nome/conteúdo dos modelos (exige termo em 'parametros').",
    "preview_modelo": "Preenche o modelo e devolve o texto SEM gravar, apontando variáveis pendentes.",
    "duplicar_preencher": "Preenche o modelo com os dados do processo e GRAVA a minuta.",
    "salvar_peticao": "Grava uma peça na pasta do processo (exige conteudo).",
    "salvar_relatorio": "Grava um relatório de análise na pasta do processo (exige conteudo).",
    "listar_minutas": "Lista peças e relatórios já gerados do processo.",
    "ler_minuta": "Lê de volta o texto de uma peça já gravada (exige arquivo).",
    "gerar_markdown": "Relatório processual em Markdown.",
    "gerar_html": "Relatório processual em HTML.",
    "exportar_pdf": "Relatório processual em PDF.",
    "exportar_todos": "Relatórios em lote para vários CNJ (use 'lista_cnj').",
    "folha_rosto": "Folha de rosto oficial (.html + .pdf) com sumário e metadados.",
    "dossie_zip": "Empacota tudo da pasta do processo num .zip.",
}

_ALIAS_PRODUCAO = {
    "modelos": "listar_modelos",
    "buscar_modelos": "pesquisar_modelos",
    "previa": "preview_modelo",
    "preview": "preview_modelo",
    "simular": "preview_modelo",
    "preencher": "duplicar_preencher",
    "gerar_minuta": "duplicar_preencher",
    "salvar_peca": "salvar_peticao",
    "minutas": "listar_minutas",
    "abrir_minuta": "ler_minuta",
    "reler": "ler_minuta",
    "ler_peca_salva": "ler_minuta",
    "markdown": "gerar_markdown",
    "md": "gerar_markdown",
    "html": "gerar_html",
    "pdf": "exportar_pdf",
    "lote": "exportar_todos",
    "capa": "folha_rosto",
    "folha_de_rosto": "folha_rosto",
    "zip": "dossie_zip",
    "dossie": "dossie_zip",
}


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
    structured_output=True,
)
@auditar_ferramenta
async def producao_minutas_e_relatorios(
    acao: str,
    arquivo: str = "",
    numero_cnj: str = "",
    persona: str = "servidor",
    grau: str = "1",
    perfil: str = "",
    parametros: str = "",
    conteudo: str = "",
    tipo: str = "Petição",
    formato: str = "docx",
    lista_cnj: str = "",
    incluir_autos: bool = False,
    incluir_pecas: bool = True,
    max_caracteres: int = 4000,
    confirmar_consulta: str = "",
    confirmation_token: str = "",
    capturar_screenshot: bool = False,
) -> Dict[str, Any]:
    """
    Exporta relatórios (Markdown/HTML/PDF) e gerencia peças/modelos/minutas.

    acao: 'listar_modelos' | 'ler_modelo' | 'pesquisar_modelos' | 'preview_modelo'
        | 'duplicar_preencher' | 'salvar_peticao' | 'salvar_relatorio'
        | 'listar_minutas' | 'ler_minuta' | 'gerar_markdown' | 'gerar_html'
        | 'exportar_pdf' | 'exportar_todos' | 'folha_rosto' | 'dossie_zip'

    Parâmetros por ação:
    - ler_modelo / preview_modelo / duplicar_preencher: 'arquivo' é o modelo.
      'parametros' é um JSON de variáveis extras, ex: {"CIDADE": "Belém"}.
    - salvar_peticao / salvar_relatorio: o TEXTO da peça vai em 'conteudo'
      (não em 'arquivo'). 'tipo' entra no nome do arquivo e 'formato' é
      'docx' (padrão) | 'md' | 'txt'.
    - pesquisar_modelos: o termo procurado vai em 'parametros'.
    - exportar_todos: 'lista_cnj' com os números separados por vírgula ou
      quebra de linha; 'formato' aceita 'md' ou 'html'.
    - ler_minuta: 'arquivo' é o nome exibido por 'listar_minutas'
      (.docx/.md/.txt); 'max_caracteres' trunca a leitura (padrão 20000).
    - dossie_zip: por padrão os autos ficam DE FORA e o retorno diz quais
      arquivos foram excluídos e quantos MB isso poupou; incluir_autos=True
      empacota o processo completo. As peças individuais de .../documentos/
      entram por padrão (incluir_pecas=False as deixa fora).

    Fluxo recomendado para minutas: 'preview_modelo' primeiro (confere as
    variáveis resolvidas sem gravar nada), depois 'duplicar_preencher'.
    """
    parametros_confirmacao = _parametros_confirmacao_chamada(locals())
    canonica, erro = _resolver_acao(acao, _ACOES_PRODUCAO, _ALIAS_PRODUCAO)
    if erro:
        erro["acoes"] = _ACOES_PRODUCAO
        return erro
    perfil_contexto.definir_contexto(
        _normaliza_persona(persona), _normaliza_grau(grau), perfil
    )

    cnj = _normaliza_cnj(numero_cnj)

    exige_cnj = {
        "preview_modelo",
        "duplicar_preencher",
        "salvar_peticao",
        "salvar_relatorio",
        "listar_minutas",
        "ler_minuta",
        "gerar_markdown",
        "gerar_html",
        "exportar_pdf",
        "folha_rosto",
        "dossie_zip",
    }
    if canonica in exige_cnj:
        falta = _exigir("numero_cnj", cnj, canonica)
        if falta:
            return falta

    if canonica in ("ler_modelo", "preview_modelo", "duplicar_preencher"):
        falta = _exigir(
            "arquivo",
            arquivo,
            canonica,
            "Use acao='listar_modelos' pra ver os modelos disponíveis.",
        )
        if falta:
            return falta

    # 'parametros' vira dict de substituicoes nas acoes de modelo
    extras = {}
    if canonica in ("preview_modelo", "duplicar_preencher") and parametros:
        try:
            extras = json.loads(parametros)
        except json.JSONDecodeError as e:
            return {
                "erro": "Parâmetro 'parametros' não é JSON válido",
                "detalhe": str(e),
                "exemplo": '{"CIDADE": "Belém", "PRAZO": "15 dias"}',
            }
        if not isinstance(extras, dict):
            return {
                "erro": "'parametros' precisa ser um objeto JSON",
                "recebido": type(extras).__name__,
                "exemplo": '{"CIDADE": "Belém"}',
            }

    if canonica in {
        "preview_modelo",
        "duplicar_preencher",
        "gerar_markdown",
        "gerar_html",
        "exportar_pdf",
        "exportar_todos",
        "folha_rosto",
    }:
        confirmacao = _exigir_confirmacao_consulta(
            "producao_minutas_e_relatorios",
            canonica,
            parametros_confirmacao,
            confirmar_consulta,
            confirmation_token,
        )
        if confirmacao:
            return confirmacao

    if canonica == "listar_modelos":
        return await listar_modelos_peticao()

    if canonica == "ler_modelo":
        return await ler_modelo_peticao(arquivo)

    if canonica == "pesquisar_modelos":
        termo = parametros or arquivo
        falta = _exigir(
            "parametros",
            termo,
            canonica,
            "Informe o termo a procurar nos modelos em 'parametros'.",
        )
        if falta:
            return falta
        return await pesquisar_modelos_peticao(termo)

    if canonica == "preview_modelo":
        return await previsualizar_modelo_preenchido(
            modelo=arquivo,
            numero_cnj=cnj,
            substituicoes_extras=extras,
            max_caracteres=max_caracteres,
            persona=persona,
            grau=grau,
        )

    if canonica == "duplicar_preencher":
        return await duplicar_e_preencher_modelo(
            modelo=arquivo,
            numero_cnj=cnj,
            substituicoes_extras=extras,
            persona=persona,
            grau=grau,
        )

    if canonica in ("salvar_peticao", "salvar_relatorio"):
        falta = _exigir(
            "conteudo",
            conteudo,
            canonica,
            "O texto da peça vai em 'conteudo'. 'arquivo' não é o corpo do documento.",
        )
        if falta:
            return falta
        if formato not in ("docx", "md", "txt"):
            return {
                "erro": f"Formato inválido: '{formato}'",
                "formatos_validos": ["docx", "md", "txt"],
            }
        if canonica == "salvar_peticao":
            return await salvar_peticao_processo(
                numero_cnj=cnj,
                conteudo=conteudo,
                tipo=tipo,
                formato=formato,
                grau=grau,
            )
        return await salvar_relatorio_processo(
            numero_cnj=cnj, conteudo=conteudo, formato=formato, grau=grau
        )

    if canonica == "listar_minutas":
        return await listar_minutas_processo(numero_cnj=cnj, grau=grau)

    if canonica == "ler_minuta":
        falta = _exigir(
            "arquivo",
            arquivo,
            canonica,
            "Use acao='listar_minutas' pra ver os arquivos gravados.",
        )
        if falta:
            return falta
        return await ler_minuta_processo(
            numero_cnj=cnj,
            arquivo=arquivo,
            max_caracteres=max_caracteres,
            grau=grau,
        )

    if canonica == "gerar_markdown":
        return await gerar_relatorio_markdown_processo(
            numero_cnj=cnj, persona=persona, grau=grau
        )

    if canonica == "gerar_html":
        return await gerar_relatorio_html_processo(
            numero_cnj=cnj, persona=persona, grau=grau
        )

    if canonica == "exportar_pdf":
        return await exportar_relatorio_pdf_processo(
            numero_cnj=cnj, persona=persona, grau=grau
        )

    if canonica == "exportar_todos":
        numeros = [
            _normaliza_cnj(x)
            for x in re.split(r"[,;\n]+", lista_cnj or "")
            if x.strip()
        ]
        falta = _exigir(
            "lista_cnj",
            ",".join(numeros),
            canonica,
            "Informe os CNJ separados por vírgula ou quebra de linha.",
        )
        if falta:
            return falta
        if formato not in ("md", "html"):
            return {
                "erro": f"Formato inválido para relatório em lote: '{formato}'",
                "formatos_validos": ["md", "html"],
            }
        return await exportar_todos_relatorios_processos(
            numeros_cnj=numeros, formato=formato, persona=persona, grau=grau
        )

    if canonica == "folha_rosto":
        return await gerar_folha_de_rosto_processual(
            numero_cnj=cnj, persona=persona, grau=grau
        )

    return await gerar_dossie_executivo_zip(
        numero_cnj=cnj,
        incluir_autos=incluir_autos,
        incluir_pecas=incluir_pecas,
        persona=persona,
        grau=grau,
    )


# ==============================================================================
# AUDITORIA PROCESSUAL CONSULTIVA — SOMENTE LEITURA
# ==============================================================================

_ACOES_AUDITORIA_PROCESSUAL = {
    "planejar_auditoria": (
        "Valida snapshot, lotação, playbook e quantidade sem abrir autos."
    ),
    "iniciar_auditoria_caixa": (
        "Inicia job somente leitura para montar dossiês e aplicar regras "
        "aprovadas; nunca movimenta processos."
    ),
    "status_auditoria": "Mostra progresso, cobertura, falhas e retomada do job.",
    "listar_resultados": (
        "Lista resultados sem expor CNJ, com filtros por status, destino, "
        "classe, urgência e confiança."
    ),
    "explicar_resultado": (
        "Abre um resultado autorizado com regras, evidências, cobertura e "
        "motivo de eventual abstenção."
    ),
    "exportar_relatorio": ("Gera JSON gzip criptografado, modo 0600, com hash e TTL."),
    "ler_export_relatorio": (
        "Revalida autorização, TTL e integridade antes de descriptografar."
    ),
    "registrar_revisao_humana": (
        "Registra aceite, rejeição ou correção local; não altera o playbook "
        "nem escreve no PJe."
    ),
}

_ALIAS_AUDITORIA_PROCESSUAL = {
    "planejar": "planejar_auditoria",
    "iniciar": "iniciar_auditoria_caixa",
    "auditar_caixa": "iniciar_auditoria_caixa",
    "status": "status_auditoria",
    "resultados": "listar_resultados",
    "explicar": "explicar_resultado",
    "relatorio": "exportar_relatorio",
    "ler_export": "ler_export_relatorio",
    "revisar": "registrar_revisao_humana",
}


def _todas_ocorrencias_snapshot(snapshot_id: str) -> list[dict[str, Any]]:
    pagina = 1
    todas: list[dict[str, Any]] = []
    while True:
        resposta = caixas_tarefas.consultar_ocorrencias(
            snapshot_id=snapshot_id,
            pagina=pagina,
            itens_por_pagina=500,
            incluir_metadados_origem=False,
        )
        if resposta.get("snapshot_status") != "completo":
            raise auditoria_processual.AuditError(
                "snapshot deixou de estar completo durante a consulta"
            )
        todas.extend(resposta.get("ocorrencias") or [])
        if not (resposta.get("paginacao") or {}).get("tem_proxima"):
            return todas
        pagina += 1


def _selecionar_ocorrencias_da_tarefa(
    ocorrencias: list[dict[str, Any]],
    policy: dict[str, Any],
    nome_tarefa: str,
    limite: int = 0,
) -> list[dict[str, Any]]:
    alvo = auditoria_processual.policy_task_for_name(policy, nome_tarefa)
    if alvo is None:
        return []
    selecionadas = []
    for ocorrencia in ocorrencias:
        atual = auditoria_processual.policy_task_for_name(
            policy,
            str(ocorrencia.get("tarefa") or ""),
        )
        if atual and atual.get("id") == alvo.get("id"):
            selecionadas.append(ocorrencia)
    if limite > 0:
        return selecionadas[:limite]
    return selecionadas


def _tarefas_paralelas_por_processo(
    ocorrencias: list[dict[str, Any]],
) -> dict[str, list[str]]:
    resultado: dict[str, list[str]] = {}
    for ocorrencia in ocorrencias:
        numero = str(ocorrencia.get("numero_processo") or "")
        tarefa = str(ocorrencia.get("tarefa") or "")
        if numero and tarefa:
            resultado.setdefault(numero, []).append(tarefa)
    return {numero: sorted(set(tarefas)) for numero, tarefas in resultado.items()}


def _documentos_prioritarios(
    documentos: list[dict[str, Any]],
    limite: int,
) -> list[dict[str, Any]]:
    limite = max(1, min(100, int(limite)))
    padrao = re.compile(
        r"(decis[ãa]o|senten[çc]a|despacho|ato\s+ordinat[óo]rio|"
        r"certid[ãa]o|recurso|audi[êe]ncia|suspens)",
        re.IGNORECASE,
    )
    ordenados = sorted(
        documentos,
        key=lambda item: (
            int(item.get("id")) if str(item.get("id") or "").isdigit() else 0
        ),
        reverse=True,
    )
    prioritarios = [
        item
        for item in ordenados
        if padrao.search(f"{item.get('tipo', '')} {item.get('titulo', '')}")
    ]
    selecionados: list[dict[str, Any]] = []
    vistos = set()
    for item in [*prioritarios, *ordenados]:
        document_id = str(item.get("id") or "")
        if document_id and document_id not in vistos:
            vistos.add(document_id)
            selecionados.append(item)
        if len(selecionados) >= limite:
            break
    return selecionados


async def _montar_dossie_auditoria(
    pje: Any,
    ocorrencia: dict[str, Any],
    snapshot: dict[str, Any],
    tarefas_paralelas: list[str],
    max_documentos: int,
) -> dict[str, Any]:
    numero = str(ocorrencia.get("numero_processo") or "")
    if not numero:
        return {
            "schema_version": auditoria_processual.DOSSIER_SCHEMA,
            "process_number": "indisponível",
            "occurrence_key": str(ocorrencia.get("chave_ocorrencia") or ""),
            "snapshot_id": snapshot["snapshot_id"],
            "parallel_tasks": tarefas_paralelas,
            "facts": {},
            "documents": [],
            "gaps": ["ocorrência sem número CNJ"],
            "coverage": {
                "complete": False,
                "documents_discovered": 0,
                "documents_read": 0,
                "documents_failed": 0,
            },
            "extractor_versions": {
                "facts": "deterministic-text/v1",
                "documents": "pje-client/v1",
            },
        }

    relatorio = await pje.relatorio_processo(numero)
    resposta_documentos = await pje.listar_documentos(numero)
    documentos = list(resposta_documentos.get("documentos") or [])
    selecionados = _documentos_prioritarios(documentos, max_documentos)
    leituras = []
    falhas = []
    paginas_sem_texto = 0
    truncados = 0
    reaproveitados = 0

    for documento in selecionados:
        document_id = str(documento.get("id") or "")
        fingerprint = auditoria_processual.build_document_fingerprint(documento)
        cache = await asyncio.to_thread(
            auditoria_processual.get_cached_document,
            numero,
            document_id,
            fingerprint,
        )
        if cache is not None:
            leituras.append(cache)
            reaproveitados += 1
            paginas_sem_texto += int(cache.get("pages_without_text") or 0)
            truncados += int(bool(cache.get("truncated")))
            continue
        try:
            resposta = await pje.ler_documento(
                numero,
                document_id,
                max_paginas=60,
            )
            if resposta.get("erro"):
                falhas.append(
                    {
                        "document_id": document_id,
                        "reason": "leitura não concluída",
                    }
                )
                continue
            texto_bruto = str(
                resposta.get("texto")
                or resposta.get("conteudo")
                or resposta.get("teor")
                or ""
            )
            security = document_security.segregate_untrusted_text(
                texto_bruto,
                document_id=document_id,
            )
            texto = str(security["canonical_text"])
            if not texto.strip():
                falhas.append(
                    {
                        "document_id": document_id,
                        "reason": "documento sem texto extraível",
                    }
                )
                continue
            payload = {
                "document_id": document_id,
                "title": documento.get("titulo") or documento.get("tipo"),
                "type": documento.get("tipo"),
                "date": documento.get("data"),
                "pages": auditoria_processual.split_document_pages(texto),
                "content_sha256": hashlib.sha256(
                    texto_bruto.encode("utf-8")
                ).hexdigest(),
                "pages_without_text": len(resposta.get("paginas_sem_texto") or []),
                "truncated": bool(resposta.get("truncado")),
                "source_fingerprint": fingerprint,
                "security": {
                    "schema_version": security["schema_version"],
                    "anomalies": security["anomalies"],
                    "quarantined_sha256": security["quarantined_sha256"],
                    "safe_for_automated_analysis": security[
                        "safe_for_automated_analysis"
                    ],
                },
            }
            paginas_sem_texto += payload["pages_without_text"]
            truncados += int(payload["truncated"])
            await asyncio.to_thread(
                auditoria_processual.save_cached_document,
                numero,
                document_id,
                fingerprint,
                payload["content_sha256"],
                payload,
            )
            leituras.append(payload)
        except Exception:
            # Detalhes de Playwright podem conter material de sessão. O
            # finding registra apenas a peça e a categoria segura da falha.
            falhas.append(
                {
                    "document_id": document_id,
                    "reason": "falha segura de leitura",
                }
            )

    movimentos = list(relatorio.get("movimentacoes") or [])
    facts = auditoria_processual.extract_structured_facts(
        ocorrencia,
        movimentos,
        leituras,
    )
    gaps = []
    security_alerts = [
        anomaly
        for leitura in leituras
        for anomaly in (leitura.get("security") or {}).get("anomalies") or []
    ]
    if not resposta_documentos.get("arvore_completa", True):
        gaps.append("árvore de documentos incompleta")
    if len(selecionados) < len(documentos):
        gaps.append("segunda passagem limitada antes de ler todas as peças")
    if falhas:
        gaps.append("uma ou mais peças não puderam ser lidas")
    if paginas_sem_texto:
        gaps.append("há páginas que exigem OCR aprovado")
    if truncados:
        gaps.append("há documentos truncados pelo limite de páginas")
    if security_alerts:
        gaps.append("conteúdo documental anômalo foi segregado para revisão humana")

    descricao_snapshot = str(ocorrencia.get("descricao_ultimo_movimento") or "").strip()
    if descricao_snapshot and movimentos:
        descricoes_atuais = " ".join(
            f"{item.get('titulo', '')} {item.get('detalhes', '')}"
            for item in movimentos
            if isinstance(item, dict)
        )
        if _sem_acento(descricao_snapshot) not in _sem_acento(descricoes_atuais):
            gaps.append("estado dos autos pode ter mudado após o snapshot")

    complete = (
        resposta_documentos.get("arvore_completa", True)
        and len(selecionados) == len(documentos)
        and not falhas
        and not paginas_sem_texto
        and not truncados
        and not gaps
    )
    return {
        "schema_version": auditoria_processual.DOSSIER_SCHEMA,
        "process_number": numero,
        "occurrence_key": str(ocorrencia.get("chave_ocorrencia") or ""),
        "snapshot_id": snapshot["snapshot_id"],
        "current_task": ocorrencia.get("tarefa"),
        "parallel_tasks": tarefas_paralelas,
        "state": {
            "case_class": ocorrencia.get("classe_judicial"),
            "subject": ocorrencia.get("assunto_principal"),
            "last_snapshot_movement": descricao_snapshot or None,
            "movements_observed": len(movimentos),
        },
        "facts": facts,
        "document_security": {
            "schema_version": document_security.SECURITY_SCHEMA,
            "status": "review_required" if security_alerts else "clear",
            "anomalies": security_alerts,
        },
        "documents": [
            {
                "document_id": item["document_id"],
                "title": item.get("title"),
                "type": item.get("type"),
                "date": item.get("date"),
                "content_sha256": item.get("content_sha256"),
                "pages": len(item.get("pages") or []),
            }
            for item in leituras
        ],
        "gaps": gaps,
        "coverage": {
            "complete": complete,
            "documents_discovered": len(documentos),
            "documents_selected": len(selecionados),
            "documents_read": len(leituras),
            "documents_reused": reaproveitados,
            "documents_failed": len(falhas),
            "pages_without_text": paginas_sem_texto,
            "truncated_documents": truncados,
            "tree_complete": resposta_documentos.get("arvore_completa", True),
        },
        "extractor_versions": {
            "facts": "deterministic-text/v1",
            "documents": "pje-client/v1",
            "ocr": "disabled-until-approved",
            "semantic": "disabled-until-approved",
        },
    }


async def _executar_job_auditoria(
    job_id: str,
    policy: dict[str, Any],
    snapshot: dict[str, Any],
    ocorrencias: list[dict[str, Any]],
    todas_ocorrencias: list[dict[str, Any]],
    persona: str,
    grau: str,
    max_documentos: int,
) -> None:
    try:
        job_atual = await asyncio.to_thread(
            auditoria_processual.get_job,
            job_id,
        )
        concluidas = await asyncio.to_thread(
            auditoria_processual.completed_occurrence_refs,
            job_id,
        )
        paralelas = _tarefas_paralelas_por_processo(todas_ocorrencias)
        processed = len(concluidas)
        mismatches = int(job_atual.get("finding_items") or 0)
        errors = int(job_atual.get("error_items") or 0)
        await asyncio.to_thread(
            auditoria_processual.update_job,
            job_id,
            status="running",
            processed_items=processed,
        )
        pje = await cliente_singleton.get_cliente(persona, grau)

        for ocorrencia in ocorrencias:
            occurrence_key = str(ocorrencia.get("chave_ocorrencia") or "")
            occurrence_ref = await asyncio.to_thread(
                auditoria_processual.occurrence_reference,
                occurrence_key,
            )
            if occurrence_ref in concluidas:
                continue
            try:
                numero = str(ocorrencia.get("numero_processo") or "")
                dossier = await _montar_dossie_auditoria(
                    pje,
                    ocorrencia,
                    snapshot,
                    paralelas.get(numero, []),
                    max_documentos,
                )
                dossier_meta = await asyncio.to_thread(
                    auditoria_processual.save_dossier,
                    dossier,
                )
                finding = await asyncio.to_thread(
                    auditoria_processual.evaluate_dossier,
                    ocorrencia,
                    dossier,
                    policy,
                    snapshot,
                )
                finding.update(dossier_meta)
                await asyncio.to_thread(
                    auditoria_processual.save_finding,
                    job_id,
                    finding,
                )
                if finding["status"] in {
                    "confirmed_mismatch",
                    "probable_mismatch",
                }:
                    mismatches += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                errors += 1
                # Toda ocorrência precisa de um resultado revisável. Este
                # dossiê não inventa fatos: registra apenas a falha segura e
                # força a abstenção do motor.
                numero = str(
                    ocorrencia.get("numero_processo")
                    or f"indisponível:{occurrence_key}"
                )
                dossier = {
                    "schema_version": auditoria_processual.DOSSIER_SCHEMA,
                    "process_number": numero,
                    "occurrence_key": occurrence_key,
                    "snapshot_id": snapshot["snapshot_id"],
                    "current_task": ocorrencia.get("tarefa"),
                    "parallel_tasks": paralelas.get(
                        str(ocorrencia.get("numero_processo") or ""),
                        [],
                    ),
                    "facts": {},
                    "documents": [],
                    "gaps": [
                        "falha segura impediu montar o dossiê; revisão humana obrigatória"
                    ],
                    "coverage": {
                        "complete": False,
                        "documents_discovered": 0,
                        "documents_read": 0,
                        "documents_failed": 1,
                    },
                    "extractor_versions": {
                        "facts": "deterministic-text/v1",
                        "documents": "pje-client/v1",
                        "ocr": "disabled-until-approved",
                        "semantic": "disabled-until-approved",
                    },
                }
                try:
                    dossier_meta = await asyncio.to_thread(
                        auditoria_processual.save_dossier,
                        dossier,
                    )
                    finding = await asyncio.to_thread(
                        auditoria_processual.evaluate_dossier,
                        ocorrencia,
                        dossier,
                        policy,
                        snapshot,
                    )
                    finding.update(dossier_meta)
                    await asyncio.to_thread(
                        auditoria_processual.save_finding,
                        job_id,
                        finding,
                    )
                except Exception:
                    # Persistência criptografada indisponível também deve
                    # falhar fechada, sem vazar detalhes do processo.
                    pass
            processed += 1
            await asyncio.to_thread(
                auditoria_processual.update_job,
                job_id,
                processed_items=processed,
                finding_items=mismatches,
                error_items=errors,
            )

        await asyncio.to_thread(
            auditoria_processual.update_job,
            job_id,
            status="completed_with_errors" if errors else "completed",
            processed_items=processed,
            finding_items=mismatches,
            error_items=errors,
        )
    except asyncio.CancelledError:
        await asyncio.to_thread(
            auditoria_processual.update_job,
            job_id,
            status="cancelled",
            error="job interrompido; pode ser retomado",
        )
        raise
    except Exception:
        await asyncio.to_thread(
            auditoria_processual.update_job,
            job_id,
            status="failed",
            error="falha segura no orquestrador; consulte logs protegidos",
        )


async def _planejar_auditoria_processual(
    snapshot_id: str,
    nome_tarefa: str,
    playbook_version: str,
    lotacao: str,
    limite_processos: int,
) -> dict[str, Any]:
    policy = await asyncio.to_thread(auditoria_processual.load_policy)
    snapshot = await asyncio.to_thread(
        caixas_tarefas.obter_snapshot,
        snapshot_id,
        None,
        None,
        True,
    )
    validation = auditoria_processual.validate_snapshot_and_policy(
        snapshot,
        policy,
        task_name=nome_tarefa,
        policy_version=playbook_version,
        requested_unit=lotacao,
    )
    total = 0
    if validation["ok"]:
        all_occurrences = await asyncio.to_thread(
            _todas_ocorrencias_snapshot,
            snapshot_id,
        )
        total = len(
            _selecionar_ocorrencias_da_tarefa(
                all_occurrences,
                policy,
                nome_tarefa,
                limite_processos,
            )
        )
        if total == 0:
            validation["ok"] = False
            validation["blockers"].append(
                "snapshot não contém ocorrências na tarefa solicitada"
            )
    return {
        "schema_version": auditoria_processual.AUDIT_SCHEMA,
        "ready": validation["ok"],
        "blockers": validation["blockers"],
        "snapshot": {
            "id": validation.get("snapshot_id"),
            "status": validation.get("snapshot_status"),
            "coverage_percent": validation.get("coverage_percent"),
            "unit": validation.get("unit"),
        },
        "playbook": {
            "version": validation.get("policy_version"),
            "sha256": validation.get("policy_sha256"),
            "approved_rules": validation.get("approved_rules"),
        },
        "scope": {
            "task": nome_tarefa,
            "process_occurrences": total,
            "pilot": "Providências a adotar / 1º grau",
        },
        "estimated": {
            "browser_concurrency": 1,
            "maximum_documents_per_process": 25,
            "semantic_analysis": "disabled_until_governance_approval",
        },
        "read_only": True,
    }


async def _validar_escopo_job_auditoria(job_id: str) -> dict[str, Any] | None:
    """Confirma que o job e seu snapshot pertencem ao perfil da chamada."""
    if not job_id:
        return None
    try:
        job = await asyncio.to_thread(auditoria_processual.get_job, job_id)
    except Exception:
        return None
    snapshot_id = str(job.get("snapshot_id") or "")
    if not snapshot_id:
        return {
            "erro": "job sem snapshot de origem verificável",
            "codigo": "ESCOPO_JOB_INVALIDO",
            "read_only": True,
        }
    snapshot = await asyncio.to_thread(
        caixas_tarefas.obter_snapshot,
        snapshot_id,
        None,
        None,
        False,
    )
    if snapshot.get("codigo") in {
        "PERFIL_DIVERGENTE",
        "PERFIL_OBRIGATORIO",
    }:
        print(
            f"[AUDITORIA] tentativa de acesso fora do perfil: job={job_id}",
            file=sys.stderr,
            flush=True,
        )
        return {
            "erro": "job de auditoria não pertence ao perfil selecionado",
            "codigo": "PERFIL_DIVERGENTE",
            "read_only": True,
        }
    return None


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
    structured_output=True,
)
@auditar_ferramenta
async def auditar_fluxo_processual_pje(
    acao: str,
    snapshot_id: str = "",
    nome_tarefa: str = "Providências a adotar",
    playbook_version: str = "",
    lotacao: str = "",
    autorizacao_leitura: bool = False,
    autorizacao_ref: str = "",
    job_id: str = "",
    export_id: str = "",
    finding_id: str = "",
    numero_cnj: str = "",
    status: str = "",
    destino: str = "",
    classe: str = "",
    somente_urgentes: bool = False,
    confianca_minima: float = 0,
    pagina: int = 1,
    itens_por_pagina: int = 50,
    limite_processos: int = 0,
    max_documentos_por_processo: int = 25,
    retomar: bool = True,
    decisao_revisao: str = "",
    revisor: str = "",
    justificativa: str = "",
    destino_corrigido: str = "",
    proximos_atos_json: str = "",
    ttl_horas: int = 24,
    persona: str = "servidor",
    grau: str = "1",
    perfil: str = "",
    confirmar_consulta: str = "",
    confirmation_token: str = "",
    capturar_screenshot: bool = False,
) -> Dict[str, Any]:
    """Auditoria processual consultiva e estritamente somente leitura.

    acao: 'planejar_auditoria' | 'iniciar_auditoria_caixa'
        | 'status_auditoria' | 'listar_resultados' | 'explicar_resultado'
        | 'exportar_relatorio' | 'ler_export_relatorio'
        | 'registrar_revisao_humana'

    A análise real exige snapshot completo, playbook aprovado, lotação
    compatível, ``autorizacao_leitura=True`` e ``autorizacao_ref``. O piloto
    aceita apenas 1º grau e a tarefa "Providências a adotar". Nenhuma ação
    desta ferramenta movimenta, marca, minuta, assina ou altera o PJe.

    ``listar_resultados`` não expõe CNJ. Use ``explicar_resultado`` com
    ``finding_id`` ou ``job_id + numero_cnj`` quando houver autorização.
    """
    parametros_confirmacao = _parametros_confirmacao_chamada(locals())
    canonica, erro = _resolver_acao(
        acao,
        _ACOES_AUDITORIA_PROCESSUAL,
        _ALIAS_AUDITORIA_PROCESSUAL,
    )
    if erro:
        erro["acoes"] = _ACOES_AUDITORIA_PROCESSUAL
        return erro
    # O contrato anterior já trazia ``lotacao``. Ela continua aceita como
    # rótulo completo do perfil enquanto os clientes migram para ``perfil``.
    perfil_efetivo = perfil or lotacao
    _p, _g, bloqueio_perfil = _ativar_perfil_ferramenta(
        persona, grau, perfil_efetivo
    )

    if canonica == "planejar_auditoria":
        if bloqueio_perfil:
            return bloqueio_perfil
        if not snapshot_id or not playbook_version:
            return {
                "erro": "snapshot_id e playbook_version são obrigatórios",
                "read_only": True,
            }
        return await _planejar_auditoria_processual(
            snapshot_id,
            nome_tarefa,
            playbook_version,
            lotacao,
            max(0, min(500, int(limite_processos or 0))),
        )

    if canonica == "iniciar_auditoria_caixa":
        if _normaliza_grau(grau) != "1g":
            return {
                "erro": "piloto de auditoria habilitado somente para o 1º grau",
                "read_only": True,
            }
        if not autorizacao_leitura or not autorizacao_ref.strip():
            return {
                "erro": (
                    "autorizacao_leitura=True e autorizacao_ref são "
                    "obrigatórios para abrir autos"
                ),
                "read_only": True,
            }
        if bloqueio_perfil:
            return bloqueio_perfil
        if not snapshot_id or not playbook_version:
            return {
                "erro": "snapshot_id e playbook_version são obrigatórios",
                "read_only": True,
            }
        confirmacao = _exigir_confirmacao_consulta(
            "auditar_fluxo_processual_pje",
            canonica,
            parametros_confirmacao,
            confirmar_consulta,
            confirmation_token,
        )
        if confirmacao:
            return confirmacao
        plan = await _planejar_auditoria_processual(
            snapshot_id,
            nome_tarefa,
            playbook_version,
            lotacao,
            max(0, min(500, int(limite_processos or 0))),
        )
        if not plan["ready"]:
            return {
                "erro": "auditoria bloqueada por pré-condições",
                "blockers": plan["blockers"],
                "read_only": True,
            }
        policy = await asyncio.to_thread(auditoria_processual.load_policy)
        allowed_tasks = {
            item.strip()
            for item in os.environ.get(
                "PJE_AUDIT_ALLOWED_TASKS",
                "Providências a adotar",
            ).split("|")
            if item.strip()
        }
        if not auditoria_processual.task_matches_allowlist(
            policy,
            nome_tarefa,
            allowed_tasks,
        ):
            return {
                "erro": "tarefa fora da allowlist do piloto",
                "allowed_tasks": sorted(allowed_tasks),
                "read_only": True,
            }
        resolved_task = auditoria_processual.policy_task_for_name(
            policy,
            nome_tarefa,
        )
        if resolved_task is None:
            return {
                "erro": "tarefa não existe no playbook",
                "read_only": True,
            }
        canonical_task_name = str(resolved_task["canonical_name"])
        snapshot = await asyncio.to_thread(
            caixas_tarefas.obter_snapshot,
            snapshot_id,
            None,
            None,
            True,
        )
        all_occurrences = await asyncio.to_thread(
            _todas_ocorrencias_snapshot,
            snapshot_id,
        )
        selected = _selecionar_ocorrencias_da_tarefa(
            all_occurrences,
            policy,
            nome_tarefa,
            max(0, min(500, int(limite_processos or 0))),
        )
        resumable = (
            await asyncio.to_thread(
                auditoria_processual.find_resumable_job,
                snapshot_id,
                canonical_task_name,
                playbook_version,
            )
            if retomar
            else None
        )
        if resumable and resumable["status"] in {"queued", "running"}:
            return {
                **resumable,
                "message": "job já estava em andamento",
            }
        if resumable:
            audit_job = resumable
        else:
            audit_job = await asyncio.to_thread(
                auditoria_processual.create_job,
                snapshot_id=snapshot_id,
                task_name=canonical_task_name,
                policy_version=playbook_version,
                policy_sha256=policy["_file_sha256"],
                authorisation_ref=hashlib.sha256(
                    autorizacao_ref.encode("utf-8")
                ).hexdigest(),
                unit_name=plan["snapshot"]["unit"] or "",
                total_items=len(selected),
            )
        task = asyncio.create_task(
            _executar_job_auditoria(
                audit_job["job_id"],
                policy,
                snapshot,
                selected,
                all_occurrences,
                _normaliza_persona(persona),
                _normaliza_grau(grau),
                max(1, min(100, int(max_documentos_por_processo or 25))),
            )
        )
        _audit_background_tasks.add(task)
        task.add_done_callback(_audit_background_tasks.discard)
        return {
            **audit_job,
            "status": "queued",
            "message": "auditoria somente leitura iniciada",
            "semantic_analysis": "disabled_until_governance_approval",
        }

    if bloqueio_perfil:
        return bloqueio_perfil
    erro_escopo_job = await _validar_escopo_job_auditoria(job_id)
    if erro_escopo_job:
        return erro_escopo_job

    if canonica == "status_auditoria":
        if not job_id:
            return {"erro": "job_id é obrigatório"}
        return await asyncio.to_thread(auditoria_processual.get_job, job_id)

    if canonica == "listar_resultados":
        if not job_id:
            return {"erro": "job_id é obrigatório"}
        return await asyncio.to_thread(
            auditoria_processual.list_findings,
            job_id=job_id,
            status=status,
            destination=destino,
            case_class=classe,
            minimum_confidence=confianca_minima,
            urgent_only=somente_urgentes,
            page=pagina,
            items_per_page=itens_por_pagina,
        )

    if canonica == "explicar_resultado":
        if not autorizacao_leitura or not autorizacao_ref.strip():
            return {"erro": "autorização explícita é obrigatória para expor o dossiê"}
        return await asyncio.to_thread(
            auditoria_processual.explain_finding,
            finding_id=finding_id,
            job_id=job_id,
            process_number=_normaliza_cnj(numero_cnj) if numero_cnj else "",
        )

    if canonica == "exportar_relatorio":
        if not job_id or not autorizacao_leitura or not autorizacao_ref.strip():
            return {
                "erro": (
                    "job_id e autorização explícita são obrigatórios para exportar"
                )
            }
        return await asyncio.to_thread(
            auditoria_processual.export_report,
            job_id,
            ttl_horas,
            autorizacao_ref,
        )

    if canonica == "ler_export_relatorio":
        if not export_id or not autorizacao_leitura or not autorizacao_ref.strip():
            return {
                "erro": ("export_id e autorização explícita são obrigatórios para ler")
            }
        compressed = await asyncio.to_thread(
            auditoria_processual.read_export,
            export_id,
            autorizacao_ref,
        )
        return json.loads(gzip.decompress(compressed).decode("utf-8"))

    if not finding_id:
        return {"erro": "finding_id é obrigatório para registrar revisão"}
    try:
        next_steps = (
            json.loads(proximos_atos_json) if proximos_atos_json.strip() else []
        )
    except json.JSONDecodeError as exc:
        return {
            "erro": "proximos_atos_json não é JSON válido",
            "detail": str(exc),
        }
    if not isinstance(next_steps, list):
        return {"erro": "proximos_atos_json deve conter uma lista"}
    return await asyncio.to_thread(
        auditoria_processual.register_review,
        finding_id=finding_id,
        decision=decisao_revisao,
        reviewer=revisor,
        justification=justificativa,
        corrected_destination=destino_corrigido,
        corrected_next_steps=next_steps,
    )




# Mesmo alerta de ultimas_movimentacoes: o fallback regex sobre o texto
# renderizado atribui a data do grupo SEGUINTE ao movimento, então prazo
# calculado em cima dele pode estar deslocado.
_AVISO_TIMELINE_REGEX = (
    "Movimentos extraídos pelo fallback regex (o parse DOM da timeline voltou "
    "vazio). As DATAS podem estar deslocadas — confirme antes de usar em prazo."
)


def _sinais_leitura_documento(teor: Any) -> dict[str, Any]:
    """Campos de integridade de uma leitura de peça, para mesclar na resposta.

    O pje_client sinaliza falha em teor['erro'] e incompletude em 'truncado' /
    'paginas_sem_texto'. Enterrados dentro de 'teor', esses sinais passavam
    batido: a resposta tinha forma de sucesso. Usado pela leitura individual e
    por cada item do lote, para os dois caminhos prometerem a mesma coisa.
    """
    if not isinstance(teor, dict):
        return {}
    if teor.get("erro"):
        return {"erro": teor["erro"], "status": "erro_leitura"}

    # Chave canônica do teor textual (o pje_client usa 'texto'; ler 'conteudo'
    # direto já causou falha muda neste projeto).
    sinais: dict[str, Any] = {"texto": _teor_texto(teor)}
    avisos = []
    if teor.get("truncado"):
        avisos.append(
            teor.get("aviso") or "documento truncado pelo limite de páginas extraídas"
        )
    paginas_sem_texto = teor.get("paginas_sem_texto") or []
    if paginas_sem_texto:
        avisos.append(
            f"{len(paginas_sem_texto)} página(s) sem texto extraível "
            f"(ocr_status={teor.get('ocr_status', 'desconhecido')})"
        )
    if avisos:
        sinais["leitura_completa"] = False
        sinais["status"] = "partial"
        sinais["avisos"] = avisos
    else:
        sinais["leitura_completa"] = True
    return sinais


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
    structured_output=True,
)
@auditar_ferramenta
async def pje_ler_autos_digitais(
    numero_cnj: str,
    acao: str = "listar",
    id_documento: str = "",
    limite_movimentos: int = 50,
    max_documentos: int = 10,
    max_paginas: int = 30,
    tempo_maximo_segundos: int = 45,
    persona: str = "servidor",
    grau: str = "1",
    perfil: str = "",
    confirmar_consulta: str = "",
    confirmation_token: str = "",
    capturar_screenshot: bool = False,
) -> Dict[str, Any]:
    """Abre os autos digitais de um processo e retorna a timeline e/ou o teor de um documento.

    Esta ferramenta resolve a lacuna histórica do MCP PJe: até agora era possível
    LISTAR processos da caixa, mas não ENTRAR nos autos e LER o conteúdo da
    timeline e das peças processuais. Ela navega pelo fluxo completo:
    ConsultaProcesso → resultado → listAutosDigitais.seam — lidando
    automaticamente com a tela de token (PROSSEGUIR SEM O TOKEN) quando aparece.

    acao:
      - 'listar'         — retorna a timeline (movimentos) + lista de documentos (id, tipo, data)
      - 'ler_documento'  — retorna o teor textual do documento especificado por id_documento
      - 'ler_lote'       — lê várias peças numa única abertura dos autos (ver max_documentos)
      - 'resumo'         — cabeçalho + últimos 10 movimentos. Resposta RÁPIDA: não
                           espera o lazy-load da árvore, então 'total_documentos'
                           vem como PISO ('arvore_completa': false). Para total
                           exato use 'listar'.

    Cada movimento é um objeto {tipo, data, hora} e, quando a timeline aponta
    para uma peça, também {id, descricao} — o 'id' serve direto em
    acao='ler_documento'. 'origem_movimentos' diz de onde vieram: 'dom' (parse
    estruturado da timeline), 'regex' (fallback — vem com 'aviso' de data
    possivelmente deslocada) ou 'nenhuma' (aí 'texto_pagina_bruto' traz o texto
    da página para leitura manual).

    Em acao='ler_documento' o teor textual vem em 'texto' (além do bruto em
    'teor'). Falha de leitura vem como 'erro' no TOPO da resposta, com
    'status': 'erro_leitura' — nunca só aninhada. Documento truncado pelo
    limite de páginas ou com páginas sem texto extraível vem com
    'leitura_completa': false, 'status': 'partial' e 'avisos'.

    numero_cnj:    número no formato CNJ (use apenas um identificador autorizado)
    id_documento:  ID interno do documento (obrigatório para acao='ler_documento')
    limite_movimentos: máximo de movimentos a retornar (padrão 50, teto 200).
      0 = todos, até o teto.
    max_documentos: peças a ler em acao='ler_lote' (padrão 10, máx 50)
    max_paginas: páginas por peça a extrair (padrão 30). **0 = todas.** Com o
      limite ativo e documento maior, a resposta vem com 'leitura_completa':
      false e o aviso dizendo quantas páginas o documento tem de verdade.
    tempo_maximo_segundos: orçamento do lote (padrão 45). Ao esgotar, devolve o
      que já leu com 'busca_concluida': false e 'motivo_interrupcao', em vez de
      estourar o timeout do transporte e perder tudo.

    Quando a árvore de documentos não termina de carregar, o retorno traz
    'arvore_completa': false, 'status': 'partial' e 'aviso_arvore' — nesse caso
    'total_documentos' é piso, não o número real de peças.
    """
    # Snapshot dos parâmetros públicos ANTES de criar locais derivados — é o que
    # as outras superferramentas fazem para alimentar o gate de confirmação.
    parametros_confirmacao = _parametros_confirmacao_chamada(locals())

    _p = _normaliza_persona(persona)
    _g = _normaliza_grau(grau)
    # Registra contexto sem exigir pje_id confirmado: esta tool acessa
    # ConsultaProcesso diretamente, sem depender do painel de caixas/perfis.
    perfil_contexto.definir_contexto(_p, _g, perfil)

    cnj = _normaliza_cnj(numero_cnj)
    validation = _analisar_cnj(cnj)
    if not validation.get("valido"):
        return {
            "erro": f"Número CNJ inválido: {numero_cnj}",
            "detalhe": validation.get("erro", ""),
        }

    acao_norm = _sem_acento(str(acao or "listar")).strip().casefold()
    alias_acoes = {
        "timeline": "listar",
        "movimentos": "listar",
        "documentos": "listar",
        "ler": "ler_documento",
        "teor": "ler_documento",
        "conteudo": "ler_documento",
        "lote": "ler_lote",
        "ler_varios": "ler_lote",
        "ler_documentos": "ler_lote",
        "ler_pecas": "ler_lote",
        "resumo_rapido": "resumo",
        "cabecalho": "resumo",
    }
    acao_norm = alias_acoes.get(acao_norm, acao_norm)
    acoes_validas = {"listar", "ler_documento", "ler_lote", "resumo"}
    if acao_norm not in acoes_validas:
        return {"erro": f"ação inválida: {acao}", "acoes": sorted(acoes_validas)}

    if acao_norm == "ler_documento" and not id_documento.strip():
        return {
            "erro": "id_documento é obrigatório para acao='ler_documento'",
            "dica": "Use acao='listar' primeiro para obter os IDs dos documentos disponíveis.",
        }

    # 0 (ou negativo) = sem limite de páginas. É a mesma convenção do
    # pje_client, que trata <= 0 e None como "extrai todas". Ficava travado em
    # 30 aqui, então petição inicial de 120 páginas voltava com 30 — a truncagem
    # era sinalizada, mas não havia como pedir o documento inteiro.
    try:
        paginas_por_peca = max(0, int(max_paginas))
    except (TypeError, ValueError):
        return {
            "erro": "max_paginas deve ser um número inteiro",
            "valor_recebido": str(max_paginas),
            "dica": "Use 0 para extrair todas as páginas.",
        }

    # Mesma convenção: 0 (ou negativo) = todos os movimentos, até o teto de 200.
    # Sem piso, `min(limite_movimentos, 200)` com 0 zerava a lista e a resposta
    # caía no ramo "a timeline não foi extraída via seletores", despejando 8000
    # chars de texto bruto com um diagnóstico que não tinha nada a ver.
    try:
        limite_movs = int(limite_movimentos)
    except (TypeError, ValueError):
        return {
            "erro": "limite_movimentos deve ser um número inteiro",
            "valor_recebido": str(limite_movimentos),
            "dica": "Use 0 para trazer todos os movimentos (teto de 200).",
        }
    limite_movs = 200 if limite_movs <= 0 else min(limite_movs, 200)

    # 'confirmar_consulta' e 'confirmation_token' são campos LEGADOS: hoje o
    # gate os ignora de propósito (ver _exigir_confirmacao_consulta e
    # test_campos_legados_sao_ignorados). Ainda assim toda ação daqui alcança o
    # PJe externo, então ela passa pelo mesmo ponto de controle das outras
    # superferramentas — declarar os parâmetros e nunca usá-los é que era o
    # defeito: se o gate voltar a exigir handshake, as demais ferramentas
    # ficariam protegidas e justo esta furaria em silêncio.
    confirmacao = _exigir_confirmacao_consulta(
        "pje_ler_autos_digitais",
        acao_norm,
        parametros_confirmacao,
        confirmar_consulta,
        confirmation_token,
    )
    if confirmacao:
        return confirmacao

    try:
        pje = await cliente_singleton.get_cliente(_p, _g, permitir_sem_perfil=True)

        if acao_norm in ("listar", "resumo"):
            aba = await pje._abrir_autos_processo(cnj)
            try:
                # Timeline pelo parser DOM do cliente — o mesmo de
                # ultimas_movimentacoes, validado contra o DataJud em
                # 12/07/2026. Devolve {tipo, data, hora, id, descricao} por
                # movimento. O JS de texto que vivia aqui devolvia strings de
                # até 400 chars, misturava o cabeçalho de data com o movimento
                # e deduplicava por prefixo de 60 caracteres — o que descartava
                # em silêncio movimentos reais de mesmo tipo em datas
                # diferentes ("Juntada de Petição", "Decorrido prazo...").
                html_autos = await aba.content()
                movimentos_raw = pje._extrair_movimentacoes_dom(html_autos)
                origem_movimentos = "dom"
                if not movimentos_raw:
                    # Rede de segurança para mudança de estrutura da timeline.
                    # As DATAS podem vir deslocadas — por isso o aviso.
                    origem_movimentos = "regex"
                    movimentos_raw = pje._extrair_movimentacoes(
                        await aba.inner_text("body")
                    )
                cabecalho = await aba.evaluate(
                    "() => { const h = document.querySelector('h1, h2, .processo-titulo, title'); "
                    "return h ? (h.innerText || h.textContent || '').trim() : document.title; }"
                )
                url_autos = aba.url
                movimentos = movimentos_raw[:limite_movs] if movimentos_raw else []

                if acao_norm == "resumo":
                    # 'resumo' é a resposta RÁPIDA e por isso não paga o
                    # lazy-load da árvore (wait_for_function de até 30s), que
                    # ela pagava só para devolver 5 documentos e um total. Os
                    # ids saem do HTML já em mãos — o mesmo que a timeline usou,
                    # zero ida extra ao navegador — e o total vai rotulado como
                    # PISO, porque só a primeira leva da árvore está presente.
                    # Use acao='listar' quando o total precisar ser exato.
                    docs_resumo = pje._extrair_documentos_do_html(html_autos)
                    resumo = {
                        "numero_cnj": cnj,
                        "cabecalho": cabecalho,
                        "url_autos": url_autos,
                        "origem_movimentos": origem_movimentos if movimentos else "nenhuma",
                        "ultimos_movimentos": movimentos[:10],
                        "total_documentos": len(docs_resumo),
                        "primeiros_documentos": docs_resumo[:5],
                    }
                    if movimentos and origem_movimentos == "regex":
                        resumo["aviso"] = _AVISO_TIMELINE_REGEX
                    # Sempre piso: nenhum lazy-load rodou nesta ação. Não pode
                    # ler _ultima_arvore_completa aqui — o valor seria o de uma
                    # chamada anterior e poderia afirmar 'completa' sem base.
                    return _propagar_arvore(resumo, {"arvore_completa": False})

                docs = await pje._extrair_documentos_da_aba(aba)
                # A árvore lateral do PJe é lazy: quando o carregamento estoura
                # o orçamento, _extrair_documentos_da_aba marca a árvore como
                # incompleta. Sem repassar esse sinal, 'total_documentos' se
                # apresenta como definitivo em cima de uma lista truncada.
                sinal_arvore = {
                    "arvore_completa": getattr(pje, "_ultima_arvore_completa", True)
                }

                resultado = {
                    "numero_cnj": cnj,
                    "cabecalho": cabecalho,
                    "url_autos": url_autos,
                    "origem_movimentos": origem_movimentos if movimentos else "nenhuma",
                    "total_movimentos_extraidos": len(movimentos),
                    "movimentos": movimentos,
                    "total_documentos": len(docs),
                    "documentos": docs,
                }
                if not movimentos:
                    # Terceira rede: nem DOM nem regex acharam movimento algum.
                    resultado["texto_pagina_bruto"] = await aba.evaluate(
                        "() => document.body.innerText.slice(0, 8000)"
                    )
                    resultado["aviso"] = (
                        "A timeline não foi extraída via seletores; "
                        "o campo 'texto_pagina_bruto' contém o texto completo da página."
                    )
                elif origem_movimentos == "regex":
                    resultado["aviso"] = _AVISO_TIMELINE_REGEX
                return _propagar_arvore(resultado, sinal_arvore)
            finally:
                await pje._fechar_aba_autos(aba)

        if acao_norm == "ler_lote":
            # Uma única aba dos autos para N peças, com orçamento de tempo que
            # devolve o parcial em vez de deixar o transporte MCP descartar a
            # resposta inteira. Ler N peças por acao='ler_documento' custava
            # N aberturas completas dos autos (lazy-load a cada uma).
            lote = await pje.ler_documentos_em_lote(
                numero_cnj=cnj,
                max_documentos=max(1, min(int(max_documentos), 50)),
                max_paginas=paginas_por_peca,
                tempo_maximo_segundos=int(tempo_maximo_segundos),
            )
            if not isinstance(lote, dict):
                return {"erro": "resposta inesperada do cliente", "numero_cnj": cnj}

            leituras = lote.get("leituras") or []
            for leitura in leituras:
                if isinstance(leitura, dict):
                    # Mesmo contrato de integridade da leitura individual: sem
                    # isso o lote reintroduz o erro/truncagem enterrados no teor.
                    leitura.update(_sinais_leitura_documento(leitura.get("teor")))
            lote["leituras_com_erro"] = sum(
                1 for leitura in leituras if isinstance(leitura, dict) and leitura.get("erro")
            )
            lote["leituras_incompletas"] = sum(
                1
                for leitura in leituras
                if isinstance(leitura, dict) and leitura.get("leitura_completa") is False
            )
            return lote

        # acao == "ler_documento"
        doc_id = id_documento.strip()
        teor = await pje.ler_documento(cnj, doc_id, max_paginas=paginas_por_peca)
        resposta: dict[str, Any] = {
            "numero_cnj": cnj,
            "id_documento": doc_id,
            "teor": teor,
        }
        resposta.update(_sinais_leitura_documento(teor))
        return resposta

    except Exception as exc:
        return {"erro": str(exc), "numero_cnj": cnj, "acao": acao}



@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
    structured_output=True,
)
@auditar_ferramenta
async def pje_rastrear_ar_correios(
    numero_cnj: str = "",
    codigo_ar: str = "",
    id_documento: str = "",
    persona: str = "servidor",
    grau: str = "1",
    confirmar_consulta: str = "",
    confirmation_token: str = "",
    capturar_screenshot: bool = False,
) -> Dict[str, Any]:
    """Extrai e rastreia símbolos/códigos AR dos Correios em expedientes processuais.

    No PJe, citações e intimações enviadas pelos Correios recebem um código de
    rastreamento (AR - Aviso de Recebimento) no formato AA000000000BR. Esta
    ferramenta faz o ciclo completo:

      1. Abre os autos do processo (via ConsultaProcesso)
      2. Lê a aba 'Expedientes' para listar atos de comunicação por carta/AR
      3. Lê os documentos de carta/citação/intimação e extrai código(s) AR
         pelo padrão [A-Z]{2}\\d{9}[A-Z]{2}
      4. Consulta a API oficial dos Correios (SRO) e retorna o status de entrega

    Modos de uso:
      - numero_cnj        → abre autos, varre expedientes e rastreia todos os AR
      - numero_cnj + id_documento → lê apenas esse documento e rastreia o AR
      - codigo_ar         → rastreia diretamente o código sem abrir o PJe

    numero_cnj:    CNJ do processo (use apenas um identificador autorizado)
    codigo_ar:     código AR avulso (ex: 'AA123456789BR')
    id_documento:  ID interno de um documento PJe para extrair o código AR
    """
    parametros_confirmacao = _parametros_confirmacao_chamada(locals())
    _p = _normaliza_persona(persona)
    _g = _normaliza_grau(grau)
    perfil_contexto.definir_contexto(_p, _g, "")

    RE_AR = re.compile(r"\b([A-Z]{2}\d{9}[A-Z]{2})\b")

    async def _rastrear_codigo(session, codigo: str) -> dict[str, Any]:
        """Consulta a API SRO dos Correios para um código AR."""
        try:
            url = (
                "https://rastreamento.correios.com.br/sro-rastro-server/api/v1/"
                f"objetos?coPosto=0&nuObjeto={codigo}&tipoConsulta=L&tipoResultado=T"
            )
            resp = await session.get(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}, timeout=15)
            if resp.status_code == 200:
                dados = resp.json()
                obj = (dados.get("objetos") or [{}])[0] or {}
                eventos = [
                    evento
                    for evento in (obj.get("eventos") or [])
                    if isinstance(evento, dict)
                ]
                ultimo = eventos[0] if eventos else {}
                unidade_ultimo = ultimo.get("unidade") or {}
                endereco_ultimo = unidade_ultimo.get("endereco") or {}
                eventos_normalizados = []
                for evento in eventos[:15]:
                    unidade = evento.get("unidade") or {}
                    endereco = unidade.get("endereco") or {}
                    eventos_normalizados.append(
                        {
                            "data": evento.get("dtHrCriado", ""),
                            "status": evento.get("descricao", ""),
                            "local": (
                                unidade.get("nome", "")
                                + " - "
                                + endereco.get("cidade", "")
                            ).strip(" -"),
                            "detalhe": evento.get("detalhe", ""),
                        }
                    )
                return {
                    "codigo": codigo,
                    "descricao_objeto": obj.get("descricao", ""),
                    "ultimo_status": ultimo.get("descricao", "sem eventos"),
                    "ultima_data": (
                        f"{ultimo.get('dtHrCriado', '')} "
                        f"{endereco_ultimo.get('cidade', '')}"
                    ).strip(),
                    "total_eventos": len(eventos),
                    "entregue": any(
                        "ENTREGUE" in evento.get("descricao", "").upper()
                        for evento in eventos
                    ),
                    "eventos": eventos_normalizados,
                }
            return {"codigo": codigo, "erro": f"Correios HTTP {resp.status_code}", "entregue": None}
        except Exception as exc:
            return {"codigo": codigo, "erro": str(exc), "entregue": None}

    import httpx

    codigos_avulsos: list[str] = []
    cnj = ""
    if codigo_ar.strip():
        modo = "codigo_ar"
        codigos_avulsos = RE_AR.findall(codigo_ar.upper().replace(" ", ""))
        if not codigos_avulsos:
            return {
                "erro": "Código AR inválido. Formato: AA000000000BR (2 letras + 9 dígitos + 2 letras)",
                "codigo_recebido": codigo_ar,
            }
    elif not numero_cnj.strip():
        return {
            "erro": "Informe numero_cnj ou codigo_ar.",
            "exemplos": [
                "pje_rastrear_ar_correios(numero_cnj='0000000-00.0000.8.14.0000')",
                "pje_rastrear_ar_correios(codigo_ar='AA123456789BR')",
            ],
        }
    else:
        cnj = _normaliza_cnj(numero_cnj)
        validation = _analisar_cnj(cnj)
        if not validation.get("valido"):
            return {"erro": f"CNJ inválido: {numero_cnj}"}
        modo = "documento" if id_documento.strip() else "processo"

    confirmacao = _exigir_confirmacao_consulta(
        "pje_rastrear_ar_correios",
        modo,
        parametros_confirmacao,
        confirmar_consulta,
        confirmation_token,
    )
    if confirmacao:
        return confirmacao

    # Modo avulso: rastreia direto sem abrir PJe.
    if codigos_avulsos:
        async with httpx.AsyncClient() as sess:
            resultados = [
                await _rastrear_codigo(sess, codigo)
                for codigo in codigos_avulsos
            ]
        return {
            "consulta_avulsa": True,
            "total": len(resultados),
            "rastreamentos": resultados,
        }

    try:
        pje = await cliente_singleton.get_cliente(_p, _g, permitir_sem_perfil=True)

        aba = await pje._abrir_autos_processo(cnj)
        try:
            # Tenta abrir aba Expedientes
            try:
                await aba.locator("a[id='navbar:linkAbaExpedientes1']").click(timeout=8000)
                await aba.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass

            html_exp = await aba.content()
            ids_exp = list(dict.fromkeys(re.findall(r"abrirLinkDocumento\(['\"]?(\d{6,9})['\"]?\)", html_exp)))

            if id_documento.strip():
                ids_para_ler = [id_documento.strip()]
            elif ids_exp:
                ids_para_ler = ids_exp[:10]
            else:
                docs = await pje._extrair_documentos_da_aba(aba)
                tipos_carta = {"carta", "mandado", "citaç", "citac", "intimaç", "intimac", "ar ", "expediente", "oficio", "ofício"}
                ids_para_ler = [
                    d["id"] for d in docs
                    if any(t in (d.get("tipo", "") + d.get("titulo", "")).lower() for t in tipos_carta)
                ][:10]

            codigos_encontrados: dict[str, str] = {}
            for doc_id in ids_para_ler:
                try:
                    teor = await pje.ler_documento(cnj, doc_id)
                    teor_txt = teor.get("texto", "") if isinstance(teor, dict) else str(teor or "")
                    for cod in RE_AR.findall(teor_txt.upper()):
                        if cod not in codigos_encontrados:
                            codigos_encontrados[cod] = doc_id
                except Exception:
                    continue
        finally:
            await pje._fechar_aba_autos(aba)

        if not codigos_encontrados:
            return {
                "numero_cnj": cnj,
                "resultado": "Nenhum código AR dos Correios encontrado nos expedientes analisados.",
                "docs_analisados": len(ids_para_ler),
                "dica": (
                    "Informe id_documento de uma carta/citação específica, "
                    "ou verifique se o processo possui expedientes pelos Correios."
                ),
            }

        async with httpx.AsyncClient() as sess:
            rastreamentos = []
            for cod, doc_id in codigos_encontrados.items():
                r = await _rastrear_codigo(sess, cod)
                r["encontrado_no_doc_id"] = doc_id
                rastreamentos.append(r)

        entregues = sum(
            1 for rastreamento in rastreamentos
            if rastreamento.get("entregue") is True
        )
        pendentes = sum(
            1 for rastreamento in rastreamentos
            if rastreamento.get("entregue") is False
        )
        com_erro = sum(
            1 for rastreamento in rastreamentos
            if rastreamento.get("entregue") is None
        )
        return {
            "numero_cnj": cnj,
            "total_codigos_ar": len(rastreamentos),
            "entregues": entregues,
            "pendentes_ou_em_transito": pendentes,
            "com_erro": com_erro,
            "docs_analisados": len(ids_para_ler),
            "rastreamentos": rastreamentos,
        }

    except Exception as exc:
        return {"erro": str(exc), "numero_cnj": numero_cnj}


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
    structured_output=True,
)
@auditar_ferramenta
async def pje_capturar_evidencia(
    numero_processo: str,
    descricao_acao: str,
    full_page: bool = False,
    persona: str = "servidor",
    grau: str = "1",
    perfil: str = "",
    confirmar_consulta: str = "",
    confirmation_token: str = "",
) -> Dict[str, Any]:
    """Captura um screenshot carimbado da tela do PJe.

    Navega até o processo se informado em numero_processo.
    """
    parametros_confirmacao = _parametros_confirmacao_chamada(locals())
    _p, _g, bloqueio_perfil = _ativar_perfil_ferramenta(
        persona, grau, perfil
    )
    if bloqueio_perfil:
        return bloqueio_perfil

    confirmacao = _exigir_confirmacao_consulta(
        "pje_capturar_evidencia",
        "evidencia",
        parametros_confirmacao,
        confirmar_consulta,
        confirmation_token,
    )
    if confirmacao:
        return confirmacao

    try:
        pje = await cliente_singleton.get_cliente(_p, _g, permitir_sem_perfil=True)
        if not pje or not pje._page:
            return {"erro": "Sessão do navegador não inicializada."}

        aba = None
        fechar_aba = False
        cnj = None
        if numero_processo.strip():
            cnj = _normaliza_cnj(numero_processo)
            validation = _analisar_cnj(cnj)
            if not validation.get("valido"):
                return {
                    "erro": f"Número CNJ inválido: {numero_processo}",
                    "detalhe": validation.get("erro", ""),
                }
            aba = await pje._abrir_autos_processo(cnj)
            fechar_aba = True
        else:
            aba = pje._page

        try:
            from evidencia import tirar_evidencia_efemera
            evidencia = await tirar_evidencia_efemera(
                aba,
                full_page=full_page,
                processo=cnj,
                descricao=descricao_acao,
            )

            return {
                "status": "concluido",
                "evidencia_screenshot": evidencia,
            }
        finally:
            if fechar_aba and aba:
                await pje._fechar_aba_autos(aba)
    except Exception as exc:
        return {"erro": str(exc)}


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
    structured_output=True,
)
@auditar_ferramenta
async def pje_consultar_processo(
    numero_processo: str,
    incluir_movimentacoes: bool = True,
    limite_movimentacoes: int = 10,
    persona: str = "servidor",
    grau: str = "1",
    perfil: str = "",
    confirmar_consulta: str = "",
    confirmation_token: str = "",
) -> Dict[str, Any]:
    """Consulta dados básicos de um processo pelo número CNJ."""
    parametros_confirmacao = _parametros_confirmacao_chamada(locals())
    _p, _g, bloqueio_perfil = _ativar_perfil_ferramenta(
        persona, grau, perfil
    )
    if bloqueio_perfil:
        return bloqueio_perfil

    confirmacao = _exigir_confirmacao_consulta(
        "pje_consultar_processo",
        "consultar",
        parametros_confirmacao,
        confirmar_consulta,
        confirmation_token,
    )
    if confirmacao:
        return confirmacao

    cnj = _normaliza_cnj(numero_processo)
    validation = _analisar_cnj(cnj)
    if not validation.get("valido"):
        return {
            "erro": f"Número CNJ inválido: {numero_processo}",
            "detalhe": validation.get("erro", ""),
        }

    try:
        dados = await consultar_processo(cnj, _p, _g)
        if incluir_movimentacoes:
            movs = await ultimas_movimentacoes(cnj, limite_movimentacoes, _p, _g)
            dados["ultimas_movimentacoes"] = movs.get("movimentacoes", [])
            dados["destaques_familia"] = movs.get("destaques_familia", [])
        return dados
    except Exception as exc:
        return {"erro": str(exc), "numero_cnj": cnj}


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
    structured_output=True,
)
@auditar_ferramenta
async def pje_analisar_autos_lote(
    numero_processo: str,
    incluir_scans: bool = False,
    max_paginas: int = 100,
    max_documentos: int = 20,
    tempo_maximo_segundos: int = 120,
    persona: str = "servidor",
    grau: str = "1",
    perfil: str = "",
    confirmar_consulta: str = "",
    confirmation_token: str = "",
) -> Dict[str, Any]:
    """Abre os autos do processo e realiza leitura de múltiplas peças em lote com classificação e timeline."""
    if incluir_scans:
        return {
            "erro": "A opção incluir_scans está pendente de aprovação institucional no momento e não pode ser ativada.",
            "status": "bloqueado",
        }

    parametros_confirmacao = _parametros_confirmacao_chamada(locals())
    _p, _g, bloqueio_perfil = _ativar_perfil_ferramenta(
        persona, grau, perfil
    )
    if bloqueio_perfil:
        return bloqueio_perfil

    confirmacao = _exigir_confirmacao_consulta(
        "pje_analisar_autos_lote",
        "lote",
        parametros_confirmacao,
        confirmar_consulta,
        confirmation_token,
    )
    if confirmacao:
        return confirmacao

    cnj = _normaliza_cnj(numero_processo)
    validation = _analisar_cnj(cnj)
    if not validation.get("valido"):
        return {
            "erro": f"Número CNJ inválido: {numero_processo}",
            "detalhe": validation.get("erro", ""),
        }

    try:
        res = await pje_ler_autos_digitais(
            numero_cnj=cnj,
            acao="ler_lote",
            max_documentos=max_documentos,
            max_paginas=max_paginas,
            tempo_maximo_segundos=tempo_maximo_segundos,
            persona=persona,
            grau=grau,
            perfil=perfil,
            confirmar_consulta=confirmar_consulta,
            confirmation_token=confirmation_token,
        )

        if not isinstance(res, dict) or "erro" in res:
            return res

        # 1. Classificação dos documentos por tipo/classe com base no título
        leituras = res.get("leituras") or []
        for leitura in leituras:
            if isinstance(leitura, dict) and "documento" in leitura:
                doc = leitura["documento"]
                t = str(doc.get("titulo", "")).lower()
                if "petição inicial" in t or "peticao inicial" in t or "inicial" in t:
                    classe = "Petição Inicial"
                elif "contestação" in t or "contestacao" in t or "defesa" in t:
                    classe = "Contestação"
                elif "sentença" in t or "sentenca" in t or "acórdão" in t or "acordao" in t:
                    classe = "Sentença/Acórdão"
                elif "despacho" in t:
                    classe = "Despacho"
                elif "decisão" in t or "decisao" in t:
                    classe = "Decisão Interlocutória"
                elif "laudo" in t or "perícia" in t or "pericial" in t or "laudo pericial" in t:
                    classe = "Laudo Pericial"
                elif "procuração" in t or "procuracao" in t or "substabelecimento" in t:
                    classe = "Procuração/Substabelecimento"
                elif "certidão" in t or "certidao" in t:
                    classe = "Certidão"
                else:
                    classe = "Outros"
                doc["classe_documento"] = classe

        # 2. Geração da estatística/resumo de cobertura
        total_docs = res.get("total_documentos") or 0
        total_lidos = len(leituras)
        total_paginas = 0
        classificacao_por_tipo = {}

        for leitura in leituras:
            if isinstance(leitura, dict):
                teor = leitura.get("teor") or {}
                paginas = teor.get("paginas") or []
                total_paginas += len(paginas)

                classe = leitura.get("documento", {}).get("classe_documento", "Outros")
                classificacao_por_tipo[classe] = classificacao_por_tipo.get(classe, 0) + 1

        cobertura_pct = round((total_lidos / total_docs) * 100, 2) if total_docs > 0 else 0.0

        res["resumo_cobertura"] = {
            "total_paginas_lidas": total_paginas,
            "cobertura_percentual": cobertura_pct,
            "leituras_com_erro": res.get("leituras_com_erro", 0),
            "leituras_incompletas": res.get("leituras_incompletas", 0),
        }
        res["classificacao_por_tipo"] = classificacao_por_tipo

        # 3. Integração de timeline de movimentações recentes
        movimentacoes = []
        try:
            movs_res = await ultimas_movimentacoes(cnj, 15, _p, _g)
            movimentacoes = movs_res.get("movimentacoes") or []
            res["destaques_familia"] = movs_res.get("destaques_familia") or []
        except Exception:
            # Não falha a tool se a timeline falhar
            pass
        res["movimentacoes"] = movimentacoes

        return res

    except Exception as exc:
        return {"erro": str(exc), "numero_cnj": cnj}


async def extrair_dados_inventory(
    numero_cnj: str = "",
    documentos: list[dict[str, Any]] | None = None,
    base: dict[str, Any] | None = None,
    persona: str = "servidor",
    grau: str = "1",
    perfil: str = "",
    confirmar_consulta: str = "",
    confirmation_token: str = "",
) -> Dict[str, Any]:
    """Extrai dados estruturados de Inventário e Partilha (inventory/v1)."""
    from domain_analyzers.inventory_v1 import extract_inventory_v1

    docs = documentos or []
    if not docs and numero_cnj:
        cnj = _normaliza_cnj(numero_cnj)
        validation = _analisar_cnj(cnj)
        if validation.get("valido"):
            try:
                res = await pje_ler_autos_digitais(
                    numero_cnj=cnj,
                    acao="ler_lote",
                    max_documentos=20,
                    max_paginas=50,
                    persona=persona,
                    grau=grau,
                    perfil=perfil,
                    confirmar_consulta=confirmar_consulta,
                    confirmation_token=confirmation_token,
                )
                if isinstance(res, dict) and "leituras" in res:
                    docs = [
                        item.get("teor", {})
                        for item in res.get("leituras", [])
                        if isinstance(item, dict)
                    ]
            except Exception:
                pass

    return extract_inventory_v1(docs, base)


async def extrair_dados_usucapiao(
    numero_cnj: str = "",
    documentos: list[dict[str, Any]] | None = None,
    base: dict[str, Any] | None = None,
    persona: str = "servidor",
    grau: str = "1",
    perfil: str = "",
    confirmar_consulta: str = "",
    confirmation_token: str = "",
) -> Dict[str, Any]:
    """Extrai dados estruturados de Usucapião (adverse-possession/v1)."""
    from domain_analyzers.adverse_possession_v1 import extract_adverse_possession_v1

    docs = documentos or []
    if not docs and numero_cnj:
        cnj = _normaliza_cnj(numero_cnj)
        validation = _analisar_cnj(cnj)
        if validation.get("valido"):
            try:
                res = await pje_ler_autos_digitais(
                    numero_cnj=cnj,
                    acao="ler_lote",
                    max_documentos=20,
                    max_paginas=50,
                    persona=persona,
                    grau=grau,
                    perfil=perfil,
                    confirmar_consulta=confirmar_consulta,
                    confirmation_token=confirmation_token,
                )
                if isinstance(res, dict) and "leituras" in res:
                    docs = [
                        item.get("teor", {})
                        for item in res.get("leituras", [])
                        if isinstance(item, dict)
                    ]
            except Exception:
                pass

    return extract_adverse_possession_v1(docs, base)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        _criar_app_http(),
        host=mcp.settings.host,
        port=mcp.settings.port,
        log_level=mcp.settings.log_level.lower(),
    )
