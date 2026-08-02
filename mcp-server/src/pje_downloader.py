"""Download de documentos e processos do PJe-TJPA (1o e 2o graus).

Primitivas:
- baixar_documento_bytes: baixa 1 doc como bytes (PDF ou HTML)
- salvar_documento: salva 1 doc na pasta do processo

Constantes:
- _PASTAS: ~/Library/Mobile Documents/.../Processos TJPA {1,2} Grau/
- LIMITE_PDF_DIRETO_MB: 18 MB (acima usa ingestão local incremental)
"""

import asyncio
import hashlib
import os
import re
import sqlite3
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import auditoria_processual
import perfil_contexto
import retention_policy
from pje_client import URL_BASES

LIMITE_PDF_DIRETO_MB = 18

# Suporte a diretório customizado via PJE_STORAGE_DIR ou fallback para iCloud no macOS
_base_env = os.environ.get("PJE_STORAGE_DIR")
if _base_env:
    _PASTAS = {
        "1g": Path(_base_env) / "Processos TJPA 1 Grau",
        "2g": Path(_base_env) / "Processos TJPA 2 Grau",
    }
else:
    _PASTAS = {
        "1g": Path.home()
        / "Library/Mobile Documents/com~apple~CloudDocs/Processos TJPA 1 Grau",
        "2g": Path.home()
        / "Library/Mobile Documents/com~apple~CloudDocs/Processos TJPA 2 Grau",
    }
PASTA_PROCESSOS = _PASTAS["1g"]  # retrocompatibilidade


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def cnj_safe(numero_cnj: str) -> str:
    """Normaliza CNJ pra usar como nome de pasta/arquivo (sem caracteres problematicos)."""
    return re.sub(r"[^\w-]", "_", numero_cnj.strip())


def pasta_base_grau(grau: str = "1g") -> Path:
    """Raiz física isolada pelo perfil funcional ativo, quando interno."""
    base = _PASTAS[grau]
    contexto = perfil_contexto.contexto_atual()
    if contexto and contexto.get("persona") in perfil_contexto.PERSONAS_INTERNAS:
        contexto = perfil_contexto.exigir_contexto_fixado(contexto["persona"])
    perfil_id = (contexto or {}).get("contexto_validado_id")
    return base / perfil_id if perfil_id else base


def pasta_processo(numero_cnj: str, grau: str = "1g", criar: bool = True) -> Path:
    """Retorna a pasta do processo no iCloud (por grau).

    criar=True (default) cria a pasta se nao existir. Use criar=False em
    caminhos de SO-LEITURA (estatisticas, inventario): criar ali polui o
    storage com pastas vazias e quebra em filesystem read-only.
    """
    p = pasta_base_grau(grau) / cnj_safe(numero_cnj)
    if criar:
        p.mkdir(parents=True, exist_ok=True)
    return p


def url_documento(documento_id: str, grau: str = "1g") -> str:
    """Monta a URL de download direto confirmada na árvore do PJe-TJPA."""
    return (
        f"{URL_BASES[grau]}/seam/resource/rest/pje-legacy/"
        f"documento/download/{documento_id}"
    )


def conferir_conteudo(body: bytes, content_type: str) -> tuple[bool, str | None]:
    """Valida a integridade do corpo baixado e retorna (integro, motivo_erro)."""
    tamanho = len(body)
    if tamanho == 0:
        return False, "arquivo vazio (0 byte)"
        
    is_pdf = "pdf" in content_type.lower() or body[:5] == b"%PDF-"
    if is_pdf:
        if body[:5] != b"%PDF-":
            return False, "não começa com '%PDF-'"
        # Procurar %%EOF nos últimos 2048 bytes
        rodape = body[-2048:]
        if b"%%EOF" not in rodape:
            return False, "sem marcador de fim '%%EOF'"
        if tamanho < 1024:
            return False, f"tamanho suspeito de PDF truncado ({tamanho} bytes)"
        return True, None
        
    html_str = body.decode("utf-8", errors="ignore")
        
    html_lower = html_str.lower()
    
    redirecionamentos = (
        "window.location", "top.location", "location.href", 
        "loginform", "loginsubmit", "j_id", "sso/login", "auth"
    )
    if "login" in html_lower and any(r in html_lower for r in redirecionamentos):
        return False, "página de login ou redirecionamento detectada no lugar do documento"
        
    erros = (
        "viewexpiredexception", "erro interno", "unexpected error", 
        "ocorreu um erro", "javax.faces", "server error", "500 Internal Server"
    )
    if any(e in html_lower for e in erros):
        return False, "página de erro do servidor detectada no lugar do documento"
        
    if tamanho < 100:
        return False, f"tamanho suspeito de HTML truncado ({tamanho} bytes)"

    if "<html" not in html_lower or not any(
        marcador in html_lower for marcador in ("<head", "<body")
    ):
        return False, "estrutura HTML ausente ou incompleta"
        
    return True, None


def conferir_arquivo_cache(caminho: Path) -> tuple[bool, str | None, str]:
    """Valida conteúdo real e calcula hash antes de permitir reuso do cache."""
    try:
        body = caminho.read_bytes()
    except OSError as exc:
        return False, f"falha ao ler arquivo em cache: {exc}", ""
    content_type = (
        "application/pdf" if caminho.suffix.casefold() == ".pdf" else "text/html"
    )
    integro, motivo = conferir_conteudo(body, content_type)
    return integro, motivo, hashlib.sha256(body).hexdigest()


def _gravar_atomico(caminho: Path, body: bytes) -> None:
    """Publica o arquivo somente depois de uma escrita completa no mesmo diretório."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    fd, temporario = tempfile.mkstemp(
        prefix=f".{caminho.name}.",
        suffix=".tmp",
        dir=caminho.parent,
    )
    try:
        with os.fdopen(fd, "wb") as arquivo:
            arquivo.write(body)
            arquivo.flush()
            os.fsync(arquivo.fileno())
        os.replace(temporario, caminho)
    except BaseException:
        try:
            Path(temporario).unlink()
        except FileNotFoundError:
            pass
        raise


async def baixar_documento_bytes(client, numero_cnj: str, documento_id: str) -> dict:
    """Baixa 1 documento como bytes usando a sessao autenticada do PJeClient.

    Garante que o processo_id em cache pertence a ESTE processo (com o
    singleton, o id pode ter sobrado de outro processo consultado antes -
    usar o id errado baixaria documento de outro processo ou daria 404).
    Retorna dict com bytes, content_type, tamanho, formato.
    """
    # Abre os autos para validar que a sessão tem acesso a este processo antes
    # de requisitar a peça por id.
    await client.garantir_processo_id(numero_cnj)

    url = url_documento(str(documento_id), client.grau)
    _log(f"[BAIXA] GET {url}")

    resp = await client._context.request.get(url)
    try:
        if not resp.ok:
            raise RuntimeError(
                f"Download falhou: HTTP {resp.status} pro doc {documento_id}"
            )

        body = await resp.body()
        content_type = resp.headers.get("content-type", "")
        
        integro, motivo = conferir_conteudo(body, content_type)
        if not integro:
            raise RuntimeError(f"DOWNLOAD_CORROMPIDO: {motivo}")

        is_pdf = "pdf" in content_type.lower() or body[:5] == b"%PDF-"
        mime_detectado = "application/pdf" if is_pdf else "text/html"

        return {
            "bytes": body,
            "content_type": mime_detectado,
            "content_type_declarado": content_type,
            "tamanho_bytes": len(body),
            "formato": "pdf" if is_pdf else "html",
            "sha256": hashlib.sha256(body).hexdigest(),
        }
    finally:
        await resp.dispose()


async def salvar_documento(
    client,
    numero_cnj: str,
    documento_id: str,
    tipo_descritivo: str = "",
    titulo: str = "",
    data: str = "",
) -> dict:
    """Baixa e salva 1 documento na pasta do processo.

    Salva em: Processos TJPA 1 Grau/{cnj}/documentos/{id}{-tipo}.{ext}
    Retorna dict com caminho, tamanho, formato.
    """
    retention_policy.require_legacy_persistence("o download persistente de documento")
    info = await baixar_documento_bytes(client, numero_cnj, documento_id)

    pasta = pasta_processo(numero_cnj, client.grau) / "documentos"
    pasta.mkdir(parents=True, exist_ok=True)

    ext = "pdf" if info["formato"] == "pdf" else "html"
    nome_seguro = re.sub(r"[^\w-]+", "_", tipo_descritivo)[:60].strip("_")
    nome = (
        f"{documento_id}-{nome_seguro}.{ext}"
        if nome_seguro
        else f"{documento_id}.{ext}"
    )
    caminho = pasta / nome

    _gravar_atomico(caminho, info["bytes"])
    _log(f"[BAIXA] Salvo {caminho.name} ({info['tamanho_bytes'] / 1024:.1f} KB)")

    return {
        "numero_cnj": numero_cnj,
        "documento_id": str(documento_id),
        "caminho": str(caminho),
        "tamanho_bytes": info["tamanho_bytes"],
        "tamanho_kb": round(info["tamanho_bytes"] / 1024, 1),
        "formato": info["formato"],
        "content_type": info["content_type"],
        "content_type_declarado": info["content_type_declarado"],
        "sha256": info["sha256"],
        "source_fingerprint": auditoria_processual.build_document_fingerprint({
            "id": str(documento_id),
            "titulo": titulo,
            "tipo": tipo_descritivo,
            "data": data,
            "tamanho_bytes": info["tamanho_bytes"],
        }),
    }


async def baixar_processo_doc_a_doc(
    client,
    numero_cnj: str,
    ordem: str = "decrescente",
    limite: int = 0,
    concurrency_limit: int = 5,
) -> dict:
    """Baixa o processo iterando documentos da arvore.

    Suporta coleta concorrente limitada por semaphore e controle explicito
    de erros de aba/documento via flags de status (ERROR/PARTIAL/COMPLETE).

    ordem: 'decrescente' (mais recente primeiro) ou 'crescente'
    limite: 0 = todos; N = so os primeiros N apos ordenar
    concurrency_limit: limite de downloads simultaneos (default=5)
    """
    retention_policy.require_legacy_persistence(
        "o download persistente doc a doc"
    )
    _log("[BAIXA-DOC] Listando documentos do processo...")
    status_flag = "COMPLETE"
    safe_error = None
    listagem = {}

    try:
        listagem = await client.listar_documentos(numero_cnj)
        if not listagem or not isinstance(listagem, dict):
            status_flag = "ERROR"
            safe_error = "Listagem de documentos retornou resposta vazia ou inválida"
        elif listagem.get("status") == "ERROR":
            status_flag = "ERROR"
            safe_error = listagem.get("safe_error") or listagem.get("erro") or "Falha na listagem de documentos"
    except Exception as exc:
        _log(f"[BAIXA-DOC] Erro ao listar documentos: {exc}")
        return {
            "numero_cnj": numero_cnj,
            "metodo": "doc_a_doc",
            "status": "ERROR",
            "safe_error": f"{type(exc).__name__}: {exc}",
            "arvore_completa": False,
            "total_documentos": 0,
            "baixados": 0,
            "erros": 1,
            "pdf_consolidado": None,
            "documentos_salvos": [],
            "erros_detalhados": [{"id": None, "tipo": "listagem", "erro": str(exc), "status": "ERROR"}],
        }

    docs = listagem.get("documentos", []) if isinstance(listagem, dict) else []

    # Ordena por ID (numerico) - PJe usa IDs sequenciais
    docs = sorted(
        docs,
        key=lambda d: int(d["id"]) if str(d.get("id", "")).isdigit() else 0,
        reverse=ordem.lower().startswith("decres"),
    )

    if limite and limite > 0:
        docs = docs[:limite]

    _log(f"[BAIXA-DOC] Iniciando download de {len(docs)} docs (ordem={ordem}, concorrencia={concurrency_limit})")

    salvos = []
    erros = []

    sem = asyncio.Semaphore(max(1, int(concurrency_limit)))

    async def _download_one(doc: dict, index: int) -> tuple[str, dict]:
        async with sem:
            doc_id = str(doc.get("id") or "")
            doc_tipo = str(doc.get("tipo") or "")
            doc_titulo = str(doc.get("titulo") or "")
            doc_data = str(doc.get("data") or "")
            try:
                r = await salvar_documento(
                    client, numero_cnj, doc_id, doc_tipo, titulo=doc_titulo, data=doc_data
                )
                _log(
                    f"[BAIXA-DOC] {index + 1}/{len(docs)} OK: {doc_id} ({r['tamanho_kb']} KB, {r['formato']})"
                )
                return "ok", r
            except Exception as e:
                _log(f"[BAIXA-DOC] {index + 1}/{len(docs)} FAIL: {doc_id} - {e}")
                return "error", {"id": doc_id, "tipo": doc_tipo, "erro": str(e), "status": "ERROR"}

    if docs:
        tasks = [_download_one(d, i) for i, d in enumerate(docs)]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        for res_type, res_val in results:
            if res_type == "ok":
                salvos.append(res_val)
            else:
                erros.append(res_val)

    if status_flag != "ERROR":
        if erros and salvos:
            status_flag = "PARTIAL"
        elif erros and not salvos and docs:
            status_flag = "ERROR"
        elif isinstance(listagem, dict) and not listagem.get("arvore_completa", True):
            status_flag = "PARTIAL"
        elif not docs:
            status_flag = "ERROR"
            safe_error = safe_error or "Nenhum documento encontrado na listagem da árvore"
        else:
            status_flag = "COMPLETE"

    pdfs = [s for s in salvos if s.get("formato") == "pdf"]
    consolidado = None
    if len(pdfs) >= 1:
        try:
            from pypdf import PdfWriter

            writer = PdfWriter()
            for p in pdfs:
                writer.append(p["caminho"])
            sufixo = f" (parcial {len(pdfs)} docs)" if limite else " (doc_a_doc)"
            destino = (
                pasta_processo(numero_cnj, client.grau)
                / f"{cnj_safe(numero_cnj)}{sufixo}.pdf"
            )
            with open(destino, "wb") as f:
                writer.write(f)
            tamanho = destino.stat().st_size
            consolidado = {
                "caminho": str(destino),
                "tamanho_bytes": tamanho,
                "tamanho_mb": round(tamanho / 1024 / 1024, 2),
                "num_pdfs_concatenados": len(pdfs),
            }
            _log(
                f"[BAIXA-DOC] Consolidado: {destino.name} "
                f"({consolidado['tamanho_mb']} MB, {len(pdfs)} PDFs)"
            )
        except Exception as e:
            _log(f"[BAIXA-DOC] Falha ao consolidar: {e}")

    erros_detalhados = erros if erros else None
    if (status_flag in ("ERROR", "PARTIAL")) and not erros_detalhados:
        erros_detalhados = [
            {
                "id": None,
                "tipo": "listagem",
                "erro": safe_error or "Falha na listagem de documentos ou árvore vazia",
                "status": status_flag,
            }
        ]

    return {
        "numero_cnj": numero_cnj,
        "metodo": "doc_a_doc",
        "status": status_flag,
        "arvore_completa": listagem.get("arvore_completa", True) if isinstance(listagem, dict) else False,
        "safe_error": safe_error,
        "total_documentos": len(docs),
        "baixados": len(salvos),
        "erros": len(erros) if erros else (1 if status_flag in ("ERROR", "PARTIAL") else 0),
        "pdf_consolidado": consolidado,
        "documentos_salvos": [
            {
                "id": s["documento_id"],
                "caminho": s["caminho"],
                "formato": s["formato"],
                "tamanho_kb": s["tamanho_kb"],
                "sha256": s.get("sha256"),
                "source_fingerprint": s.get("source_fingerprint"),
            }
            for s in salvos
        ],
        "erros_detalhados": erros_detalhados,
    }


async def baixar_processo_completo(
    client,
    numero_cnj: str,
    tipo_documento: str = "",
    id_inicial: str = "",
    id_final: str = "",
    periodo_inicio: str = "",
    periodo_fim: str = "",
    cronologia: str = "decrescente",
    incluir_expediente: bool = True,
    incluir_movimentos: bool = True,
    forcar: bool = False,
    metodo: str = "nativo",
    limite: int = 0,
) -> dict:
    """Baixa o processo completo.

    metodo='nativo' (default, recomendado): consolida no servidor PJe,
        baixa em UMA requisicao do S3 pre-assinado. Inclui capa/indice e
        expediente/movimentos. Sempre completo.
    metodo='doc_a_doc' (alternativo): itera documentos da arvore e
        concatena. Util quando voce quer os arquivos individuais separados
        em Processos TJPA 1 Grau/{cnj}/documentos/. Limitacao: depende do
        listar_documentos, que tem bug de paginacao em processos com >30 docs.

    Salva docs individuais em Processos TJPA 1 Grau/{cnj}/documentos/
        (apenas no modo doc_a_doc).
    Salva PDF consolidado em Processos TJPA 1 Grau/{cnj}/{cnj}.pdf
        (apenas no modo nativo - doc_a_doc usa sufixo proprio).
    """
    if metodo not in ("nativo", "doc_a_doc"):
        # Sem validacao, qualquer typo caia silenciosamente no doc_a_doc
        raise ValueError(f"Metodo invalido: {metodo!r}. Use 'nativo' ou 'doc_a_doc'.")
    retention_policy.require_legacy_persistence(
        "o download persistente do processo completo"
    )

    pasta = pasta_processo(numero_cnj, client.grau)
    consolidado_path = pasta / f"{cnj_safe(numero_cnj)}.pdf"

    # Cache hit - SO vale pro metodo nativo SEM filtros: o {cnj}.pdf em cache
    # e' sempre os autos completos. Pro doc_a_doc (que gera individuais) ou
    # pra pedidos com filtro/recorte, o cache nao representa o que foi pedido.
    sem_filtros = not any(
        [tipo_documento, id_inicial, id_final, periodo_inicio, periodo_fim, limite]
    )
    if metodo == "nativo" and sem_filtros and consolidado_path.exists() and not forcar:
        integro, motivo, sha256 = conferir_arquivo_cache(consolidado_path)
        if integro:
            tamanho = consolidado_path.stat().st_size
            _log(
                f"[BAIXA] Cache hit validado: {consolidado_path.name} "
                f"({tamanho / 1024 / 1024:.2f} MB)"
            )
            return {
                "numero_cnj": numero_cnj,
                "metodo": "cache",
                "caminho": str(consolidado_path),
                "tamanho_bytes": tamanho,
                "tamanho_mb": round(tamanho / 1024 / 1024, 2),
                "content_type": "application/pdf",
                "sha256": sha256,
                "integridade_validada": True,
                "cache": True,
            }
        _log(
            f"[ALERT] cache corrompido recusado para {cnj_safe(numero_cnj)}: {motivo}"
        )

    if metodo == "nativo":
        # Download com filtro/recorte NAO pode ocupar o caminho de cache
        destino = (
            consolidado_path
            if sem_filtros
            else (pasta / f"{cnj_safe(numero_cnj)} (recorte).pdf")
        )
        r = await client.baixar_processo_nativo(
            numero_cnj=numero_cnj,
            caminho_destino=destino,
            tipo_documento=tipo_documento,
            id_inicial=id_inicial,
            id_final=id_final,
            periodo_inicio=periodo_inicio,
            periodo_fim=periodo_fim,
            cronologia=cronologia,
            incluir_expediente=incluir_expediente,
            incluir_movimentos=incluir_movimentos,
        )
        r["numero_cnj"] = numero_cnj
        r["metodo"] = "nativo"
        r["cache"] = False
        return r

    # Default: doc_a_doc
    return await baixar_processo_doc_a_doc(
        client, numero_cnj, ordem=cronologia, limite=limite
    )


# =========================================================================
# DOWNLOAD EM BACKGROUND (imune ao timeout do protocolo MCP)
# =========================================================================
#
# Problema: o metodo nativo gera o PDF no servidor do PJe (~30-90s pra autos
# grandes) e so entao baixa dezenas de MB do S3. A tool call sincrona estoura
# o timeout curto do protocolo MCP (~30-40s); quando o cliente cancela a
# request, a corrotina do download e' cancelada junto e NADA e' salvo.
#
# Solucao: disparar o download como asyncio.create_task (rodando no event loop
# do server, nao amarrado a request). A task e' guardada em _JOBS (ref forte,
# nao coletada pelo GC), entao sobrevive ao fim/cancelamento da tool call.
# A 1a chamada ainda espera ~GRACA_SINCRONA_S: se o processo for pequeno,
# resolve em UMA chamada; se for grande, retorna "em_andamento" e o usuario
# consulta status_download depois.
#
# Seguranca com o singleton: baixar_processo_nativo e' @_serializa (segura o
# _op_lock durante todo o download), e o watchdog de inatividade so fecha a
# sessao APOS adquirir esse mesmo lock. Logo o Chromium nunca e' fechado no
# meio de um download em andamento.

_JOBS: dict = {}  # Dict mantido em memória para retrocompatibilidade
_ACTIVE_TASKS: dict[str, asyncio.Task] = {}
GRACA_SINCRONA_S = 20  # quanto a 1a chamada espera antes de devolver "em_andamento"
JOBS_TTL_S = 1800  # TTL de 30 minutos para jobs concluidos/com erro em RAM


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect_downloader_db() -> sqlite3.Connection:
    path = auditoria_processual.database_path()
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA foreign_keys=ON")
    _init_downloader_schema(connection)
    return connection


@contextmanager
def _downloader_db() -> Iterator[sqlite3.Connection]:
    conn = _connect_downloader_db()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _init_downloader_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS consolidated_pdf_jobs (
            job_id TEXT PRIMARY KEY,
            process_cnj TEXT NOT NULL,
            grau TEXT NOT NULL,
            cronologia TEXT NOT NULL DEFAULT 'decrescente',
            status TEXT NOT NULL,
            caminho_pdf TEXT,
            tamanho_bytes INTEGER DEFAULT 0,
            tamanho_mb REAL DEFAULT 0.0,
            sha256 TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            error_message TEXT,
            worker_id TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_consolidated_pdf_cnj_grau
            ON consolidated_pdf_jobs(process_cnj, grau, status);
        """
    )


def _limpar_jobs_expirados(ttl_s: int = JOBS_TTL_S) -> None:
    """Função legada de limpeza em memória."""
    _JOBS.clear()


async def _executar_download_sqlite(
    job_id: str, client: Any, numero_cnj: str, cronologia: str, forcar: bool
) -> None:
    """Corpo da task de background: baixa e registra o resultado em consolidated_pdf_jobs (SQLite WAL)."""
    try:
        now_iso = _now_iso()
        with _downloader_db() as conn:
            conn.execute(
                "UPDATE consolidated_pdf_jobs SET status = 'running', updated_at = ? WHERE job_id = ?",
                (now_iso, job_id),
            )
        r = await baixar_processo_completo(
            client,
            numero_cnj=numero_cnj,
            cronologia=cronologia,
            forcar=forcar,
            metodo="nativo",
        )
        now_iso = _now_iso()
        caminho = r.get("caminho") or str(pasta_processo(numero_cnj, client.grau) / f"{cnj_safe(numero_cnj)}.pdf")
        tamanho_bytes = int(r.get("tamanho_bytes") or 0)
        tamanho_mb = float(r.get("tamanho_mb") or 0.0)
        sha256_val = r.get("sha256") or ""
        with _downloader_db() as conn:
            conn.execute(
                """
                UPDATE consolidated_pdf_jobs SET
                    status = 'completed',
                    caminho_pdf = ?,
                    tamanho_bytes = ?,
                    tamanho_mb = ?,
                    sha256 = ?,
                    updated_at = ?,
                    completed_at = ?
                WHERE job_id = ?
                """,
                (caminho, tamanho_bytes, tamanho_mb, sha256_val, now_iso, now_iso, job_id),
            )
        _log(f"[BG] Download concluído job {job_id}: {numero_cnj} ({tamanho_mb} MB)")
    except Exception as e:
        now_iso = _now_iso()
        err_msg = f"{type(e).__name__}: {e}"
        with _downloader_db() as conn:
            conn.execute(
                """
                UPDATE consolidated_pdf_jobs SET
                    status = 'failed',
                    error_message = ?,
                    updated_at = ?,
                    completed_at = ?
                WHERE job_id = ?
                """,
                (err_msg, now_iso, now_iso, job_id),
            )
        _log(f"[BG] Download FALHOU job {job_id}: {numero_cnj} - {err_msg}")
    finally:
        _ACTIVE_TASKS.pop(job_id, None)


async def _executar_download(
    client, numero_cnj: str, cronologia: str, forcar: bool
) -> None:
    """Wrapper de retrocompatibilidade."""
    job_id = uuid.uuid4().hex
    await _executar_download_sqlite(job_id, client, numero_cnj, cronologia, forcar)


def status_download(numero_cnj: str, grau: str = "1g") -> dict:
    """Consulta o estado de um download no banco de dados SQLite WAL."""
    cnj_clean = cnj_safe(numero_cnj)
    with _downloader_db() as conn:
        cursor = conn.execute(
            """
            SELECT * FROM consolidated_pdf_jobs
            WHERE process_cnj = ? AND grau = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (cnj_clean, grau),
        )
        row = cursor.fetchone()

    consolidado = pasta_processo(numero_cnj, grau) / f"{cnj_clean}.pdf"

    if row is None:
        if consolidado.exists():
            integro, motivo, sha256 = conferir_arquivo_cache(consolidado)
            if integro:
                tam = consolidado.stat().st_size
                return {
                    "numero_cnj": numero_cnj,
                    "status": "concluido",
                    "caminho": str(consolidado),
                    "tamanho_mb": round(tam / 1024 / 1024, 2),
                    "content_type": "application/pdf",
                    "sha256": sha256,
                    "integridade_validada": True,
                    "observacao": "arquivo ja existia (sem job no banco)",
                }
            return {
                "numero_cnj": numero_cnj,
                "status": "erro",
                "codigo": "CACHE_CORROMPIDO",
                "erro": motivo,
                "caminho": str(consolidado),
            }
        return {
            "numero_cnj": numero_cnj,
            "status": "inexistente",
            "observacao": "nenhum download iniciado pra este processo nesta sessao",
        }

    try:
        created_dt = datetime.fromisoformat(row["created_at"])
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=timezone.utc)
        decorrido_s = round((datetime.now(timezone.utc) - created_dt).total_seconds(), 1)
    except Exception:
        decorrido_s = 0.0

    status_db = row["status"]

    if status_db in ("queued", "running", "em_andamento"):
        return {
            "numero_cnj": numero_cnj,
            "job_id": row["job_id"],
            "status": "em_andamento",
            "decorrido_s": decorrido_s,
            "observacao": (
                "Servidor do PJe ainda gerando/baixando o PDF. "
                "Chame status_download de novo em ~30-60s."
            ),
        }
    elif status_db in ("completed", "concluido"):
        caminho_pdf = row["caminho_pdf"] or str(consolidado)
        return {
            "numero_cnj": numero_cnj,
            "job_id": row["job_id"],
            "status": "concluido",
            "decorrido_s": decorrido_s,
            "caminho": caminho_pdf,
            "tamanho_bytes": row["tamanho_bytes"],
            "tamanho_mb": row["tamanho_mb"],
            "sha256": row["sha256"],
            "content_type": "application/pdf",
            "integridade_validada": True,
        }
    else:  # 'failed', 'erro'
        return {
            "numero_cnj": numero_cnj,
            "job_id": row["job_id"],
            "status": "erro",
            "decorrido_s": decorrido_s,
            "erro": row["error_message"] or "Falha desconhecida durante o download",
        }


def listar_jobs(incluir_concluidos: bool = True) -> dict:
    """Inventário dos downloads em background armazenados no SQLite WAL."""
    agora = datetime.now(timezone.utc)
    with _downloader_db() as conn:
        if incluir_concluidos:
            cursor = conn.execute("SELECT * FROM consolidated_pdf_jobs ORDER BY created_at DESC")
        else:
            cursor = conn.execute(
                "SELECT * FROM consolidated_pdf_jobs WHERE status IN ('queued', 'running', 'em_andamento') ORDER BY created_at DESC"
            )
        rows = cursor.fetchall()

    jobs = []
    em_andamento = 0
    concluidos = 0
    com_erro = 0

    for r in rows:
        try:
            created_dt = datetime.fromisoformat(r["created_at"])
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
            decorrido_s = round((agora - created_dt).total_seconds(), 1)
        except Exception:
            decorrido_s = 0.0

        st = r["status"]
        if st in ("queued", "running", "em_andamento"):
            mapped_st = "em_andamento"
            em_andamento += 1
        elif st in ("completed", "concluido"):
            mapped_st = "concluido"
            concluidos += 1
        else:
            mapped_st = "erro"
            com_erro += 1

        item = {
            "job_id": r["job_id"],
            "chave": f"{r['grau']}:{r['process_cnj']}",
            "grau": r["grau"],
            "processo": r["process_cnj"],
            "status": mapped_st,
            "decorrido_s": decorrido_s,
        }
        if r["completed_at"]:
            try:
                comp_dt = datetime.fromisoformat(r["completed_at"])
                if comp_dt.tzinfo is None:
                    comp_dt = comp_dt.replace(tzinfo=timezone.utc)
                item["concluido_ha_s"] = round((agora - comp_dt).total_seconds(), 1)
            except Exception:
                pass

        if mapped_st == "erro":
            item["erro"] = r["error_message"]
        elif mapped_st == "concluido":
            item["tamanho_mb"] = r["tamanho_mb"]
            item["caminho"] = r["caminho_pdf"]

        jobs.append(item)

    return {
        "total_jobs": len(jobs),
        "em_andamento": em_andamento,
        "concluidos": concluidos,
        "com_erro": com_erro,
        "ttl_jobs_s": JOBS_TTL_S,
        "jobs": jobs,
        "observacao": "Jobs persistidos em banco SQLite WAL (consolidated_pdf_jobs).",
    }


def checar_escrita_storage() -> dict:
    """Verifica se o servidor consegue mesmo escrever no storage, por grau.

    A auditoria antiga testava so a pasta do 1o grau e, quando ela ainda nao
    existia (servidor novo, nada baixado), reportava permissao_escrita=False
    num servidor perfeitamente saudavel - alarme falso. Aqui a pasta e' criada
    se faltar, que e' o que o downloader faria no primeiro uso.
    """
    import shutil

    resultado = {}
    for g, pasta in _PASTAS.items():
        info = {"pasta": str(pasta)}
        try:
            pasta.mkdir(parents=True, exist_ok=True)
            teste = pasta / ".perm_check.tmp"
            teste.write_text("test", encoding="utf-8")
            teste.unlink()
            uso = shutil.disk_usage(pasta)
            info.update(
                existe=True,
                permissao_escrita=True,
                espaco_livre_gb=round(uso.free / (1024**3), 2),
                processos_locais=sum(
                    1
                    for d in pasta.iterdir()
                    if d.is_dir() and not d.name.startswith(".")
                ),
            )
        except Exception as e:
            info.update(
                existe=pasta.exists(),
                permissao_escrita=False,
                espaco_livre_gb=0.0,
                processos_locais=0,
                erro=f"{type(e).__name__}: {e}",
            )
        resultado[g] = info
    return resultado


async def baixar_processo_background(
    client, numero_cnj: str, cronologia: str = "decrescente", forcar: bool = False
) -> dict:
    """Dispara o download nativo em background e espera ate GRACA_SINCRONA_S.

    - cache hit: retorna na hora.
    - processo pequeno: termina dentro da graca e retorna 'concluido'.
    - processo grande: retorna 'em_andamento' e segue baixando; consulte
      status_download pra acompanhar.
    """
    cnj_clean = cnj_safe(numero_cnj)
    grau_val = client.grau
    consolidado = (
        pasta_processo(numero_cnj, client.grau) / f"{cnj_clean}.pdf"
    )

    # Cache hit imediato (autos completos ja em disco).
    if consolidado.exists() and not forcar:
        integro, motivo, sha256 = conferir_arquivo_cache(consolidado)
        if integro:
            tam = consolidado.stat().st_size
            _log(
                f"[BG] Cache hit validado: {consolidado.name} "
                f"({tam / 1024 / 1024:.2f} MB)"
            )
            return {
                "numero_cnj": numero_cnj,
                "status": "concluido",
                "metodo": "cache",
                "cache": True,
                "caminho": str(consolidado),
                "tamanho_bytes": tam,
                "tamanho_mb": round(tam / 1024 / 1024, 2),
                "content_type": "application/pdf",
                "sha256": sha256,
                "integridade_validada": True,
            }
        _log(
            f"[ALERT] cache corrompido recusado para {cnj_clean}: {motivo}"
        )

    # Request Coalescing via SQLite WAL:
    # Checar e criar job atomicamente dentro de uma transacao exclusiva
    with _downloader_db() as conn:
        conn.execute("BEGIN IMMEDIATE;")
        try:
            cursor = conn.execute(
                """
                SELECT job_id, status, created_at FROM consolidated_pdf_jobs
                WHERE process_cnj = ? AND grau = ? AND status IN ('queued', 'running', 'em_andamento')
                ORDER BY created_at DESC LIMIT 1
                """,
                (cnj_clean, grau_val),
            )
            active_job = cursor.fetchone()

            if active_job:
                conn.execute("COMMIT;")
                _log(f"[BG] Request coalesced into active job {active_job['job_id']} for {cnj_clean} ({grau_val})")
                resp = status_download(numero_cnj, grau_val)
                resp["ja_estava_em_andamento"] = True
                return resp

            job_id = uuid.uuid4().hex
            now_iso = _now_iso()
            conn.execute(
                """
                INSERT INTO consolidated_pdf_jobs
                    (job_id, process_cnj, grau, cronologia, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'running', ?, ?)
                """,
                (job_id, cnj_clean, grau_val, cronologia, now_iso, now_iso),
            )
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise

    task = asyncio.create_task(
        _executar_download_sqlite(job_id, client, numero_cnj, cronologia, forcar)
    )
    _ACTIVE_TASKS[job_id] = task
    _log(f"[BG] Download iniciado em background: job_id={job_id} {grau_val}:{cnj_clean}")

    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=GRACA_SINCRONA_S)
    except (asyncio.TimeoutError, TimeoutError):
        pass

    return status_download(numero_cnj, client.grau)


async def preparar_processo_orquestrador(
    client, numero_cnj: str, forcar: bool = False
) -> dict:
    """Baixa o processo + decide estrategia de analise.

    Retorna:
      {
        "estrategia": "pdf_direto" | "ingestao_local_incremental",
        "caminho_pdf": ...,
        "tamanho_mb": ...,
        "limite_mb": 18,
        "instrucao_claude": "..."
      }
    """
    info = await baixar_processo_completo(
        client, numero_cnj, cronologia="decrescente", forcar=forcar
    )

    tamanho_mb = info.get("tamanho_mb")
    if tamanho_mb is None:
        caminho = info.get("caminho")
        tamanho_mb = (
            round(Path(caminho).stat().st_size / 1024 / 1024, 2) if caminho else 0
        )
        info["tamanho_mb"] = tamanho_mb

    if tamanho_mb <= LIMITE_PDF_DIRETO_MB:
        estrategia = "pdf_direto"
        instrucao = (
            f"PDF do processo cabe na janela de contexto ({tamanho_mb:.2f} MB ≤ "
            f"{LIMITE_PDF_DIRETO_MB} MB). Leia diretamente o arquivo em "
            f"{info['caminho']} pra analisar."
        )
    else:
        estrategia = "ingestao_local_incremental"
        instrucao = (
            f"PDF muito grande ({tamanho_mb:.2f} MB > {LIMITE_PDF_DIRETO_MB} MB). "
            "Não envie os autos a serviços externos. Use "
            "analisar_processo_completo_pje para criar manifesto, cache "
            "criptografado e evidências locais citáveis por peça e página."
        )

    return {
        **info,
        "estrategia": estrategia,
        "limite_mb": LIMITE_PDF_DIRETO_MB,
        "instrucao_claude": instrucao,
    }


def _iso(ts: float) -> str:
    """Epoch -> ISO-8601 local, sem microssegundos."""
    return datetime.fromtimestamp(ts).replace(microsecond=0).isoformat()


def inventario_cache(
    grau: str = "1g", numero_cnj: str = None, ordenar_por: str = "tamanho"
) -> dict:
    """Inventaria o cache local de processos - SO LEITURA, nao abre browser.

    Diferente de estatisticas_e_limpeza_storage (que so devolve agregados),
    aqui cada processo vem detalhado: se tem os autos consolidados, quantas
    pecas individuais, quantas minutas geradas, quando foi baixado e se o
    PDF cabe na janela de contexto (<= LIMITE_PDF_DIRETO_MB).

    - numero_cnj: opcional, restringe a um processo.
    - ordenar_por: 'tamanho' (default) | 'data' | 'cnj'
    """
    g = "2g" if str(grau).strip() in ["2", "2g", "segundo"] else "1g"
    pasta_base = pasta_base_grau(g)

    if not pasta_base.exists():
        return {
            "grau": g,
            "pasta_base": str(pasta_base),
            "storage_existe": False,
            "total_processos": 0,
            "processos": [],
            "observacao": "nenhum processo baixado neste grau ainda",
        }

    if numero_cnj:
        alvo = cnj_safe(numero_cnj)
        dirs = [d for d in pasta_base.iterdir() if d.is_dir() and d.name == alvo]
    else:
        dirs = [
            d for d in pasta_base.iterdir() if d.is_dir() and not d.name.startswith(".")
        ]

    processos = []
    for d in dirs:
        arquivos = [f for f in d.rglob("*") if f.is_file()]
        if not arquivos:
            continue
        total_bytes = sum(f.stat().st_size for f in arquivos)

        consolidado = d / f"{d.name}.pdf"
        tem_consolidado = consolidado.exists()
        mb_consolidado = (
            round(consolidado.stat().st_size / (1024 * 1024), 2)
            if tem_consolidado
            else None
        )

        pasta_docs = d / "documentos"
        pecas = (
            [f for f in pasta_docs.iterdir() if f.is_file()]
            if pasta_docs.is_dir()
            else []
        )
        minutas_geradas = [
            f
            for f in arquivos
            if f.suffix.lower() in (".docx", ".md", ".html")
            and pasta_docs not in f.parents
        ]
        recortes = [f.name for f in d.glob("*.pdf") if f.name != consolidado.name]

        ultima = max(f.stat().st_mtime for f in arquivos)
        processos.append(
            {
                "numero_cnj": d.name.replace("_", "."),
                "pasta": str(d),
                "tamanho_mb": round(total_bytes / (1024 * 1024), 2),
                "total_arquivos": len(arquivos),
                "autos_consolidados": tem_consolidado,
                "autos_mb": mb_consolidado,
                "pecas_individuais": len(pecas),
                "minutas_e_relatorios": len(minutas_geradas),
                "pdfs_parciais": recortes or None,
                "baixado_em": _iso(ultima),
                "pronto_para_leitura_direta": bool(
                    tem_consolidado
                    and mb_consolidado is not None
                    and mb_consolidado <= LIMITE_PDF_DIRETO_MB
                ),
            }
        )

    chaves = {
        "tamanho": lambda p: -p["tamanho_mb"],
        "data": lambda p: p["baixado_em"],
        "cnj": lambda p: p["numero_cnj"],
    }
    processos.sort(key=chaves.get(ordenar_por, chaves["tamanho"]))

    total_mb = round(sum(p["tamanho_mb"] for p in processos), 2)
    incompletos = [p["numero_cnj"] for p in processos if not p["autos_consolidados"]]

    return {
        "grau": g,
        "pasta_base": str(pasta_base),
        "storage_existe": True,
        "total_processos": len(processos),
        "tamanho_total_mb": total_mb,
        "limite_leitura_direta_mb": LIMITE_PDF_DIRETO_MB,
        "processos": processos,
        "sem_autos_consolidados": incompletos or None,
        "dica": (
            "Processos em 'sem_autos_consolidados' tem pecas soltas mas nao os "
            "autos completos - rode a acao 'baixar_processo' pra fechar."
        )
        if incompletos
        else None,
    }


def estatisticas_e_limpeza_storage(
    numero_cnj: str = None, grau: str = "1g", apagar_pdfs: bool = False
) -> dict:
    """Calcula estatísticas de armazenamento e limpa cache se solicitado."""
    g = "2g" if str(grau).strip() in ["2", "2g", "segundo"] else "1g"
    pasta_base = pasta_base_grau(g)

    if not pasta_base.exists():
        return {
            "grau": g,
            "pasta_base": str(pasta_base),
            "total_processos": 0,
            "tamanho_total_mb": 0.0,
            "arquivos_removidos": 0,
            "espaco_liberado_mb": 0.0,
        }

    if numero_cnj:
        # criar=False: consultar/limpar cache nao pode materializar pasta nova
        p = pasta_processo(numero_cnj, g, criar=False)
        if not p.exists():
            return {
                "numero_cnj": numero_cnj,
                "grau": g,
                "pasta_processo": str(p),
                "em_cache": False,
                "total_arquivos": 0,
                "tamanho_mb": 0.0,
                "apagar_pdfs": apagar_pdfs,
                "arquivos_removidos": 0,
                "espaco_liberado_mb": 0.0,
                "observacao": "processo nao esta em cache local",
            }
        arquivos = [f for f in p.rglob("*") if f.is_file()]
        tamanho_antes = sum(f.stat().st_size for f in arquivos)
        removidos = 0
        tamanho_removido = 0
        if apagar_pdfs:
            for f in arquivos:
                if f.suffix.lower() in [".pdf", ".html", ".tmp"]:
                    sz = f.stat().st_size
                    try:
                        f.unlink()
                        removidos += 1
                        tamanho_removido += sz
                    except Exception:
                        pass

        arquivos_restantes = [f for f in p.rglob("*") if f.is_file()]
        tamanho_depois = sum(f.stat().st_size for f in arquivos_restantes)
        return {
            "numero_cnj": numero_cnj,
            "grau": g,
            "pasta_processo": str(p),
            "em_cache": True,
            "total_arquivos": len(arquivos),
            "tamanho_mb": round(tamanho_antes / (1024 * 1024), 2),
            "apagar_pdfs": apagar_pdfs,
            "arquivos_removidos": removidos,
            "espaco_liberado_mb": round(tamanho_removido / (1024 * 1024), 2),
            "tamanho_restante_mb": round(tamanho_depois / (1024 * 1024), 2),
        }

    total_bytes = 0
    num_processos = 0
    for child in pasta_base.iterdir():
        if child.is_dir() and not child.name.startswith("."):
            num_processos += 1
            total_bytes += sum(
                f.stat().st_size for f in child.rglob("*") if f.is_file()
            )

    return {
        "grau": g,
        "pasta_base": str(pasta_base),
        "total_processos_baixados": num_processos,
        "tamanho_total_mb": round(total_bytes / (1024 * 1024), 2),
    }
