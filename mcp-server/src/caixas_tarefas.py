"""Catálogo persistente e auditável das caixas/tarefas do PJe.

O PJe pode devolver milhares de processos por tarefa. Este módulo evita
transportar tudo em uma resposta MCP: guarda snapshots em SQLite, preserva o
JSON integral de origem e oferece consultas paginadas e prova de cobertura.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import tempfile
import threading
import time
import unicodedata
import uuid
from collections import Counter
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import auditoria_processual
import observabilidade_acervo
import perfil_contexto
import retention_policy
import tpu_catalogo
from storage import cursors

SCHEMA_ACERVO_TAREFAS = "pje.acervo-tarefas/v2"
SCHEMA_ACERVO_COMPACTO = "pje.acervo-tarefas/v2-compacto"
SCHEMA_ACERVO_ANTERIOR = "pje.acervo-tarefas/v1"
FLAGS_COMPACTAS = {
    "sigiloso": 1,
    "prioridade": 2,
    "morador_de_rua": 4,
    "conferido": 8,
}
ORDEM_CAMPOS_COMPACTOS = (
    "cnj",
    "tarefa",
    "classe",
    "assunto",
    "partes",
    "chegada",
    "dias",
    "dias_mov",
    "flags",
    "etiquetas",
)
CAMPOS_COMPACTOS_OPCIONAIS = (
    "ocorrencia_chave",
    "orgao",
)
ENVELOPES_ACERVO = {"minimo", "padrao", "completo"}
_BANCOS_INICIALIZADOS: set[Path] = set()
_LOCK_INICIALIZACAO_BANCO = threading.Lock()

# Correções editoriais restritas a nomes efetivamente observados no painel.
# O valor bruto continua preservado para auditoria e para o filtro da API.
ALIASES_TAREFAS = {
    "Aavaliar ato proferido de julgamento": ("Avaliar ato proferido de julgamento"),
    "Remeter ao 2o Grau": "Remeter ao 2º Grau",
}

# Campos conhecidos do endpoint da lista de tarefas. Tudo o que o PJe passar
# fora desta relação continua sendo devolvido em ``campos_extras_origem``.
CAMPOS_ORIGEM_CONHECIDOS = {
    "assuntoPrincipal",
    "cargoJudicial",
    "classeJudicial",
    "classeJudicialCodigo",
    "classeJudicialDescricao",
    "codigoClasseJudicial",
    "codigoClasseProcessual",
    "conferido",
    "dataChegada",
    "descricaoClasseJudicial",
    "descricaoUltimoMovimento",
    "idOrgaoJulgador",
    "idProcesso",
    "idTaskInstance",
    "idTaskInstanceProximo",
    "lembretes",
    "loginResponsavelTarefa",
    "nomeClasseJudicial",
    "nomeResponsavelTarefa",
    "nomeTarefa",
    "numeroProcesso",
    "orgaoJulgador",
    "podeDesignarAudienciaEmLote",
    "podeDesignarPericiaEmLote",
    "podeIntimarEmLote",
    "podeMinutarEmLote",
    "podeMovimentarEmLote",
    "podeRenajudEmLote",
    "poloAtivo",
    "poloPassivo",
    "prioridade",
    "sigiloso",
    "tagsProcessoList",
    "temParteMoradorDeRua",
    "ultimoMovimento",
}


def _agora_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _revisao_snapshot(snapshot: dict[str, Any]) -> str:
    """Identificador estável do conteúdo lógico de um snapshot imutável."""
    campos = {
        "snapshot_id": snapshot.get("snapshot_id"),
        "status": snapshot.get("status"),
        "finalizado_em": snapshot.get("finalizado_em"),
        "total_caixas": snapshot.get("total_caixas"),
        "ocorrencias_coletadas": snapshot.get("ocorrencias_coletadas"),
        "processos_unicos": snapshot.get("processos_unicos"),
        "cobertura_percentual": snapshot.get("cobertura_percentual"),
    }
    serializado = json.dumps(
        campos,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serializado).hexdigest()


def caminho_banco() -> Path:
    base = Path(os.environ.get("PJE_STORAGE_DIR", "/var/lib/pjepa-mcp/storage"))
    pasta = base / "inventario"
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta / "caixas_tarefas.sqlite3"


def _chave_cursor() -> bytes:
    """Lê ou cria uma chave local exclusiva para autenticar cursores."""
    return cursors.load_or_create_key(caminho_banco().parent / ".cursor_hmac_key")


def _codificar_cursor(payload: dict[str, Any]) -> str:
    return cursors.encode(payload, _chave_cursor())


def _decodificar_cursor(token: str) -> dict[str, Any]:
    return cursors.decode(token, _chave_cursor())


def _fingerprint_consulta_cursor(parametros: dict[str, Any]) -> str:
    corpo = json.dumps(
        parametros,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(corpo).hexdigest()


def _conectar() -> sqlite3.Connection:
    caminho = caminho_banco().resolve()
    con = sqlite3.connect(caminho, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        with _LOCK_INICIALIZACAO_BANCO:
            if caminho not in _BANCOS_INICIALIZADOS:
                con.execute("PRAGMA journal_mode=WAL")
                _criar_schema(con)
                os.chmod(caminho, 0o600)
                _BANCOS_INICIALIZADOS.add(caminho)
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA foreign_keys=ON")
        return con
    except Exception:
        con.close()
        raise


@contextmanager
def _banco():
    """Transação que sempre confirma/verte e fecha a conexão/FD."""
    con = _conectar()
    try:
        with con:
            yield con
    finally:
        con.close()


def _criar_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            snapshot_id TEXT PRIMARY KEY,
            tribunal TEXT NOT NULL,
            grau TEXT NOT NULL,
            persona TEXT NOT NULL,
            perfil_id TEXT,
            perfil_rotulo TEXT,
            escopo_status TEXT NOT NULL DEFAULT 'legado_ambiguo',
            iniciado_em TEXT NOT NULL,
            finalizado_em TEXT,
            status TEXT NOT NULL,
            total_caixas INTEGER NOT NULL DEFAULT 0,
            ocorrencias_declaradas INTEGER NOT NULL DEFAULT 0,
            ocorrencias_coletadas INTEGER NOT NULL DEFAULT 0,
            processos_unicos INTEGER NOT NULL DEFAULT 0,
            caixas_divergentes INTEGER NOT NULL DEFAULT 0,
            erros INTEGER NOT NULL DEFAULT 0,
            cobertura_percentual REAL NOT NULL DEFAULT 0,
            metadados_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS caixas (
            snapshot_id TEXT NOT NULL,
            grupo TEXT NOT NULL,
            nome TEXT NOT NULL,
            quantidade_declarada INTEGER NOT NULL,
            quantidade_api INTEGER NOT NULL,
            quantidade_coletada INTEGER NOT NULL,
            status TEXT NOT NULL,
            tentativas INTEGER NOT NULL DEFAULT 1,
            hash_sha256 TEXT,
            erro TEXT,
            PRIMARY KEY (snapshot_id, grupo, nome),
            FOREIGN KEY (snapshot_id) REFERENCES snapshots(snapshot_id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS ocorrencias (
            snapshot_id TEXT NOT NULL,
            chave_ocorrencia TEXT NOT NULL,
            grupo TEXT NOT NULL,
            tarefa TEXT NOT NULL,
            id_task_instance TEXT,
            id_task_instance_proximo TEXT,
            id_processo TEXT,
            numero_processo TEXT,
            classe_judicial TEXT,
            id_orgao_julgador TEXT,
            orgao_julgador TEXT,
            assunto_principal TEXT,
            polo_ativo TEXT,
            polo_passivo TEXT,
            cargo_judicial TEXT,
            data_chegada_epoch INTEGER,
            data_chegada_iso TEXT,
            ultimo_movimento_epoch INTEGER,
            ultimo_movimento_iso TEXT,
            descricao_ultimo_movimento TEXT,
            sigiloso INTEGER,
            prioridade INTEGER,
            conferido INTEGER,
            morador_de_rua INTEGER,
            podeMovimentarEmLote INTEGER,
            podeMinutarEmLote INTEGER,
            podeIntimarEmLote INTEGER,
            podeDesignarAudienciaEmLote INTEGER,
            podeDesignarPericiaEmLote INTEGER,
            podeRenajudEmLote INTEGER,
            etiquetas_json TEXT NOT NULL DEFAULT '[]',
            metadados_origem_json TEXT NOT NULL,
            reaproveitado_de_snapshot TEXT,
            PRIMARY KEY (snapshot_id, chave_ocorrencia),
            FOREIGN KEY (snapshot_id) REFERENCES snapshots(snapshot_id)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_ocorrencias_snapshot_tarefa
            ON ocorrencias(snapshot_id, tarefa);
        CREATE INDEX IF NOT EXISTS idx_ocorrencias_snapshot_cnj
            ON ocorrencias(snapshot_id, numero_processo);
        CREATE INDEX IF NOT EXISTS idx_ocorrencias_snapshot_processo
            ON ocorrencias(snapshot_id, id_processo);
        CREATE INDEX IF NOT EXISTS ix_oc_snap_tarefa_dias
            ON ocorrencias(snapshot_id, tarefa, data_chegada_iso);
        CREATE INDEX IF NOT EXISTS ix_oc_snap_chegada_chave
            ON ocorrencias(snapshot_id, data_chegada_epoch, chave_ocorrencia);
        CREATE INDEX IF NOT EXISTS ix_oc_snap_movimento_chave
            ON ocorrencias(snapshot_id, ultimo_movimento_epoch, chave_ocorrencia);
        CREATE INDEX IF NOT EXISTS ix_oc_snap_classe_chave
            ON ocorrencias(snapshot_id, classe_judicial, chave_ocorrencia);
        CREATE INDEX IF NOT EXISTS ix_oc_snap_assunto_chave
            ON ocorrencias(snapshot_id, assunto_principal, chave_ocorrencia);
        CREATE INDEX IF NOT EXISTS idx_caixas_snapshot_status
            ON caixas(snapshot_id, status);
        CREATE TABLE IF NOT EXISTS perfis (
            perfil_id TEXT PRIMARY KEY,
            persona TEXT NOT NULL,
            grau TEXT NOT NULL,
            rotulo TEXT NOT NULL,
            unidade TEXT NOT NULL DEFAULT '',
            localizacao TEXT NOT NULL DEFAULT '',
            papel TEXT NOT NULL DEFAULT '',
            orgao_julgador_id TEXT,
            orgao_julgador_nome TEXT,
            vinculo_status TEXT NOT NULL DEFAULT 'provisorio',
            atualizado_em TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS transicoes (
            perfil_id TEXT NOT NULL,
            tarefa TEXT NOT NULL,
            destino_id TEXT NOT NULL,
            destino_nome TEXT NOT NULL,
            reversivel TEXT NOT NULL,
            fonte TEXT NOT NULL DEFAULT 'heuristica_tipo_elemento',
            evidencia TEXT NOT NULL DEFAULT '',
            atualizado_em TEXT NOT NULL,
            PRIMARY KEY (perfil_id, tarefa, destino_id)
        );
        """
    )
    colunas_transicoes = {
        linha["name"] for linha in con.execute("PRAGMA table_info(transicoes)")
    }
    migracoes_transicoes = {
        "fonte": (
            "ALTER TABLE transicoes ADD COLUMN fonte TEXT "
            "NOT NULL DEFAULT 'heuristica_tipo_elemento'"
        ),
        "evidencia": (
            "ALTER TABLE transicoes ADD COLUMN evidencia TEXT "
            "NOT NULL DEFAULT ''"
        ),
    }
    for coluna, comando in migracoes_transicoes.items():
        if coluna not in colunas_transicoes:
            con.execute(comando)
    colunas_snapshots = {
        linha["name"] for linha in con.execute("PRAGMA table_info(snapshots)")
    }
    migracoes_snapshot = {
        "perfil_id": "ALTER TABLE snapshots ADD COLUMN perfil_id TEXT",
        "perfil_rotulo": "ALTER TABLE snapshots ADD COLUMN perfil_rotulo TEXT",
        "escopo_status": (
            "ALTER TABLE snapshots ADD COLUMN escopo_status TEXT "
            "NOT NULL DEFAULT 'legado_ambiguo'"
        ),
    }
    for coluna, comando in migracoes_snapshot.items():
        if coluna not in colunas_snapshots:
            con.execute(comando)
    con.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_snapshots_escopo
        ON snapshots(grau, persona, perfil_id, iniciado_em)
        """
    )
    colunas_ocorrencias = {
        linha["name"] for linha in con.execute("PRAGMA table_info(ocorrencias)")
    }
    for col in (
        "podeMovimentarEmLote",
        "podeMinutarEmLote",
        "podeIntimarEmLote",
        "podeDesignarAudienciaEmLote",
        "podeDesignarPericiaEmLote",
        "podeRenajudEmLote",
    ):
        if col not in colunas_ocorrencias:
            try:
                con.execute(f"ALTER TABLE ocorrencias ADD COLUMN {col} INTEGER DEFAULT 0")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).casefold():
                    raise
    if "reaproveitado_de_snapshot" not in colunas_ocorrencias:
        try:
            con.execute(
                "ALTER TABLE ocorrencias ADD COLUMN reaproveitado_de_snapshot TEXT"
            )
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).casefold():
                raise
    con.commit()


def novo_snapshot(
    grau: str,
    persona: str,
    caixas: list[dict[str, Any]],
    metadados: dict[str, Any] | None = None,
) -> str:
    snapshot_id = uuid.uuid4().hex
    contexto = perfil_contexto.contexto_atual()
    perfil_id = (contexto or {}).get("perfil_id")
    perfil_rotulo = (contexto or {}).get("rotulo")
    escopo_status = "provisorio" if perfil_id else "legado_ambiguo"
    declaradas = sum(max(0, int(c.get("quantidade", 0))) for c in caixas)
    with _banco() as con:
        if contexto:
            con.execute(
                """
                INSERT INTO perfis (
                    perfil_id, persona, grau, rotulo, unidade, localizacao,
                    papel, vinculo_status, atualizado_em
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'provisorio', ?)
                ON CONFLICT(perfil_id) DO UPDATE SET
                    rotulo=excluded.rotulo,
                    unidade=excluded.unidade,
                    localizacao=excluded.localizacao,
                    papel=excluded.papel,
                    atualizado_em=excluded.atualizado_em
                """,
                (
                    perfil_id,
                    persona,
                    grau,
                    perfil_rotulo,
                    contexto.get("unidade", ""),
                    contexto.get("localizacao", ""),
                    contexto.get("papel", ""),
                    _agora_iso(),
                ),
            )
        con.execute(
            """
            INSERT INTO snapshots (
                snapshot_id, tribunal, grau, persona, perfil_id,
                perfil_rotulo, escopo_status, iniciado_em, status,
                total_caixas, ocorrencias_declaradas, metadados_json
            ) VALUES (?, 'TJPA', ?, ?, ?, ?, ?, ?, 'em_andamento', ?, ?, ?)
            """,
            (
                snapshot_id,
                grau,
                persona,
                perfil_id,
                perfil_rotulo,
                escopo_status,
                _agora_iso(),
                len(caixas),
                declaradas,
                json.dumps(metadados or {}, ensure_ascii=False, sort_keys=True),
            ),
        )
        for caixa in caixas:
            con.execute(
                """
                INSERT INTO caixas (
                    snapshot_id, grupo, nome, quantidade_declarada,
                    quantidade_api, quantidade_coletada, status
                ) VALUES (?, ?, ?, ?, 0, 0, 'pendente')
                """,
                (
                    snapshot_id,
                    caixa.get("grupo", "tarefas"),
                    caixa["nome"],
                    max(0, int(caixa.get("quantidade", 0))),
                ),
            )
    return snapshot_id


def atualizar_metadados_snapshot(
    snapshot_id: str,
    atualizacoes: dict[str, Any],
) -> None:
    with _banco() as con:
        row = con.execute(
            "SELECT metadados_json FROM snapshots WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Snapshot não encontrado: {snapshot_id}")
        metadados = json.loads(row["metadados_json"] or "{}")
        metadados.update(atualizacoes)
        con.execute(
            "UPDATE snapshots SET metadados_json=? WHERE snapshot_id=?",
            (
                json.dumps(metadados, ensure_ascii=False, sort_keys=True, default=str),
                snapshot_id,
            ),
        )


def _epoch_iso(valor: Any) -> str | None:
    if valor in (None, ""):
        return None
    try:
        numero = float(valor)
        if numero > 10_000_000_000:
            numero /= 1000
        return datetime.fromtimestamp(numero, timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _bool_int(valor: Any) -> int | None:
    if valor is None:
        return None
    return int(bool(valor))


def _chave_ocorrencia(tarefa: str, item: dict[str, Any]) -> str:
    identificador = item.get("idTaskInstance")
    if identificador not in (None, ""):
        return f"{tarefa}:{identificador}"
    bruto = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
    return f"{tarefa}:sha256:{hashlib.sha256(bruto.encode()).hexdigest()}"


def hash_entidades(entidades: Iterable[dict[str, Any]]) -> str:
    """Hash estável do conteúdo completo de uma caixa, não só das chaves."""
    linhas = [
        json.dumps(
            item,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        for item in entidades
    ]
    linhas.sort()
    return hashlib.sha256("\n".join(linhas).encode("utf-8")).hexdigest()


def _valor_texto(valor: Any) -> str | None:
    if valor is None:
        return None
    if isinstance(valor, (dict, list)):
        return json.dumps(valor, ensure_ascii=False, sort_keys=True)
    return str(valor)


def _primeiro_valor(dados: dict[str, Any], *chaves: str) -> Any:
    for chave in chaves:
        valor = dados.get(chave)
        if valor not in (None, ""):
            return valor
    return None


def _normalizar_texto_exibicao(valor: Any) -> str | None:
    if valor in (None, ""):
        return None
    texto = unicodedata.normalize("NFKC", str(valor))
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto or None


def _texto_busca(valor: Any) -> str:
    if valor is None:
        return ""
    normalizado = unicodedata.normalize("NFKD", str(valor))
    return "".join(
        caractere for caractere in normalizado if not unicodedata.combining(caractere)
    ).casefold()


def estruturar_tarefa(nome: Any) -> dict[str, Any]:
    """Preserva o valor PJe e entrega um nome limpo, estável e filtrável."""
    original = None if nome in (None, "") else str(nome)
    limpo = _normalizar_texto_exibicao(original)
    normalizado = ALIASES_TAREFAS.get(limpo, limpo)
    alteracoes = []
    if original is not None and limpo != original:
        alteracoes.append("unicode_e_espacos_normalizados")
    if normalizado != limpo:
        alteracoes.append("alias_editorial_catalogado")
    base_id = (normalizado or limpo or original or "").casefold()
    identificador = hashlib.sha256(base_id.encode("utf-8")).hexdigest()[:16]
    return {
        "id_estavel": f"tarefa:{identificador}",
        "nome": normalizado,
        "nome_original_pje": original,
        "valor_filtro": original,
        "foi_normalizada": bool(alteracoes),
        "alteracoes_aplicadas": alteracoes,
    }


def _data_iso(valor: Any) -> datetime | None:
    if not valor:
        return None
    try:
        return datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _dias_entre(inicio: Any, fim: Any) -> int | None:
    dt_inicio = _data_iso(inicio)
    dt_fim = _data_iso(fim)
    if dt_inicio is None or dt_fim is None:
        return None
    return max(0, int((dt_fim - dt_inicio).total_seconds() // 86400))


def _parte_principal(nome: Any, polo: str) -> list[dict[str, Any]]:
    if nome in (None, ""):
        return []
    polo = polo.upper()
    return [
        {
            "nome": str(nome),
            "polo": polo,
            "polo_descricao": "Polo ativo" if polo == "ATIVO" else "Polo passivo",
            "principal": True,
            "tipo_parte": None,
            "documento": None,
            "advogados": [],
            "fonte": "lista de tarefas do PJe",
        }
    ]


def estruturar_ocorrencia(
    item: dict[str, Any],
    referencia_temporal: str | None = None,
    capacidades_tarefa: dict[str, Any] | None = None,
    incluir_campos_extras: bool = True,
    incluir_metadados_origem: bool = False,
) -> dict[str, Any]:
    """Projeta uma ocorrência legada no contrato semântico versionado.

    A projeção não inventa completude: a lista de tarefas fornece somente uma
    parte principal por polo. Siglas de classe são resolvidas pelo catálogo
    oficial TPU embarcado, mas códigos ambíguos permanecem sem confirmação.
    """
    origem = dict(item.get("metadados_origem") or {})
    sigla_classe = item.get("classe_judicial") or origem.get("classeJudicial")
    descricao_classe = _primeiro_valor(
        origem,
        "classeJudicialDescricao",
        "descricaoClasseJudicial",
        "nomeClasseJudicial",
    )
    codigo_classe = _primeiro_valor(
        origem,
        "codigoClasseProcessual",
        "codigoClasseJudicial",
        "classeJudicialCodigo",
    )
    classe = tpu_catalogo.resolver_classe(
        sigla_classe,
        descricao_origem=descricao_classe,
        codigo_origem=codigo_classe,
    )
    tarefa = estruturar_tarefa(item.get("tarefa"))

    ativo = _parte_principal(item.get("polo_ativo") or origem.get("poloAtivo"), "ATIVO")
    passivo = _parte_principal(
        item.get("polo_passivo") or origem.get("poloPassivo"), "PASSIVO"
    )
    todas_partes = ativo + passivo
    partes_resumo = " x ".join(parte["nome"] for parte in todas_partes)

    chegada_iso = item.get("data_chegada_iso")
    movimento_iso = item.get("ultimo_movimento_iso")
    referencia = referencia_temporal or _agora_iso()

    campos_capacidade = {
        "movimentar": "podeMovimentarEmLote",
        "minutar": "podeMinutarEmLote",
        "intimar": "podeIntimarEmLote",
        "designar_audiencia": "podeDesignarAudienciaEmLote",
        "designar_pericia": "podeDesignarPericiaEmLote",
        "consultar_renajud": "podeRenajudEmLote",
    }
    capacidades_tarefa = capacidades_tarefa or {}
    capacidades = {
        nome: (origem.get(chave) if chave in origem else capacidades_tarefa.get(chave))
        for nome, chave in campos_capacidade.items()
    }
    informada_no_registro = any(chave in origem for chave in campos_capacidade.values())
    informada_na_tarefa = any(
        chave in capacidades_tarefa for chave in campos_capacidade.values()
    )
    capacidades["informadas_pela_fonte"] = informada_no_registro or informada_na_tarefa
    capacidades["escopo"] = "tarefa"
    capacidades["resolucao"] = (
        "presente_no_registro"
        if informada_no_registro
        else "propagada_do_bloco_de_capacidades_da_tarefa"
        if informada_na_tarefa
        else "nao_informada"
    )

    etiquetas = item.get("etiquetas") or origem.get("tagsProcessoList") or []
    etiquetas_estruturadas = []
    for etiqueta in etiquetas:
        if isinstance(etiqueta, dict):
            etiquetas_estruturadas.append(
                {
                    "id": etiqueta.get("id"),
                    "id_processo": etiqueta.get("idProcesso"),
                    "nome": etiqueta.get("nomeTag"),
                    "nome_completo": etiqueta.get("nomeTagCompleto"),
                }
            )
        else:
            etiquetas_estruturadas.append(
                {
                    "id": None,
                    "id_processo": item.get("id_processo"),
                    "nome": str(etiqueta),
                    "nome_completo": str(etiqueta),
                }
            )

    resultado = {
        "schema_version": SCHEMA_ACERVO_TAREFAS,
        "ocorrencia": {
            "chave": item.get("chave_ocorrencia"),
            "grupo": item.get("grupo"),
            "tarefa": tarefa,
            "id_instancia_tarefa": item.get("id_task_instance"),
            "id_proxima_instancia_tarefa": item.get("id_task_instance_proximo"),
        },
        "processo": {
            "id": item.get("id_processo"),
            "numero_cnj": item.get("numero_processo"),
            "classe": classe,
            "assunto_principal": {
                "codigo_tpu": None,
                "descricao": (
                    item.get("assunto_principal") or origem.get("assuntoPrincipal")
                ),
            },
        },
        "partes": {
            "polos": {
                "ativo": ativo,
                "passivo": passivo,
                "outros_interessados": [],
            },
            "lista": todas_partes,
            "resumo_textual": partes_resumo,
            "completude": {
                "nivel": "somente_principal_por_polo",
                "partes_completas": False,
                "advogados_incluidos": False,
                "documentos_incluidos": False,
                "observacao": (
                    "A lista de tarefas expõe uma string principal por polo; "
                    "demais partes, qualificações e advogados exigem abrir os autos."
                ),
            },
        },
        "unidade_judicial": {
            "id_orgao_julgador": item.get("id_orgao_julgador"),
            "nome_orgao_julgador": item.get("orgao_julgador"),
            "cargo_judicial": item.get("cargo_judicial"),
        },
        "fluxo": {
            "tarefa": tarefa,
            "responsavel": {
                "nome": origem.get("nomeResponsavelTarefa"),
                "login": origem.get("loginResponsavelTarefa"),
                "informado": bool(
                    origem.get("nomeResponsavelTarefa")
                    or origem.get("loginResponsavelTarefa")
                ),
            },
            "lembretes": origem.get("lembretes") or [],
        },
        "datas": {
            "chegada_na_tarefa": chegada_iso,
            "chegada_na_tarefa_epoch": item.get("data_chegada_epoch"),
            "dias_na_tarefa": _dias_entre(chegada_iso, referencia),
            "ultimo_movimento": movimento_iso,
            "ultimo_movimento_epoch": item.get("ultimo_movimento_epoch"),
            "dias_desde_ultimo_movimento": _dias_entre(movimento_iso, referencia),
            "referencia_calculo": referencia,
        },
        "ultimo_movimento": {
            "data": movimento_iso,
            "descricao": item.get("descricao_ultimo_movimento"),
        },
        "indicadores": {
            "sigiloso": (
                bool(item["sigiloso"]) if item.get("sigiloso") is not None else None
            ),
            "prioridade_pje": (
                bool(item["prioridade"]) if item.get("prioridade") is not None else None
            ),
            "conferido": (
                bool(item["conferido"]) if item.get("conferido") is not None else None
            ),
            "tem_parte_morador_de_rua": (
                bool(item["morador_de_rua"])
                if item.get("morador_de_rua") is not None
                else None
            ),
        },
        "etiquetas": etiquetas_estruturadas,
        "capacidades_operacao_em_lote": capacidades,
        "proveniencia": {
            "fonte": (
                "pje-legacy/painelUsuario/recuperarProcessosTarefaPendenteComCriterios"
            ),
            "campos_presentes_na_origem": sorted(origem),
            "enriquecimentos": {
                "classe": {
                    "fonte": classe["fonte_resolucao"],
                    "catalogo_ref": (
                        "catalogos.classes_tpu"
                        if classe["fonte_resolucao"] == "catalogo_tpu_cnj"
                        else None
                    ),
                },
                "tarefa": {
                    "fonte": "normalizacao_local_auditavel",
                    "valor_original_preservado": True,
                },
            },
        },
    }
    if incluir_campos_extras:
        resultado["campos_extras_origem"] = {
            chave: valor
            for chave, valor in origem.items()
            if chave not in CAMPOS_ORIGEM_CONHECIDOS
        }
    if incluir_metadados_origem:
        resultado["metadados_origem"] = origem
    if item.get("reaproveitado_de_snapshot"):
        resultado["proveniencia"]["reaproveitado_de"] = item[
            "reaproveitado_de_snapshot"
        ]
    return resultado


def salvar_caixa(
    snapshot_id: str,
    caixa: dict[str, Any],
    quantidade_api: int,
    entidades: Iterable[dict[str, Any]],
    tentativas: int = 1,
    erro: str | None = None,
) -> dict[str, Any]:
    """Substitui atomicamente uma caixa e calcula sua cobertura."""
    grupo = caixa.get("grupo", "tarefas")
    tarefa = caixa["nome"]
    declarada = max(0, int(caixa.get("quantidade", 0)))
    itens = list(entidades)
    chaves = [_chave_ocorrencia(tarefa, item) for item in itens]
    duplicadas = len(chaves) - len(set(chaves))
    hash_caixa = hash_entidades(itens)

    if erro:
        status = "erro"
    elif duplicadas:
        status = "divergente"
        erro = f"{duplicadas} chave(s) de ocorrência duplicada(s) na resposta"
    elif quantidade_api != len(itens):
        status = "divergente"
        erro = f"API declarou {quantidade_api}, mas devolveu {len(itens)} entidade(s)"
    elif declarada != len(itens):
        status = "divergente"
        erro = f"painel declarou {declarada}, mas a API devolveu {len(itens)}"
    else:
        status = "completa"

    with _banco() as con:
        con.execute(
            "DELETE FROM ocorrencias WHERE snapshot_id=? AND grupo=? AND tarefa=?",
            (snapshot_id, grupo, tarefa),
        )
        for item, chave in zip(itens, chaves):
            tags = item.get("tagsProcessoList") or []
            con.execute(
                """
                INSERT OR REPLACE INTO ocorrencias (
                    snapshot_id, chave_ocorrencia, grupo, tarefa,
                    id_task_instance, id_task_instance_proximo, id_processo,
                    numero_processo, classe_judicial, id_orgao_julgador,
                    orgao_julgador, assunto_principal, polo_ativo, polo_passivo,
                    cargo_judicial, data_chegada_epoch, data_chegada_iso,
                    ultimo_movimento_epoch, ultimo_movimento_iso,
                    descricao_ultimo_movimento, sigiloso, prioridade, conferido,
                    morador_de_rua, podeMovimentarEmLote, podeMinutarEmLote,
                    podeIntimarEmLote, podeDesignarAudienciaEmLote,
                    podeDesignarPericiaEmLote, podeRenajudEmLote,
                    etiquetas_json, metadados_origem_json
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    snapshot_id,
                    chave,
                    grupo,
                    tarefa,
                    _valor_texto(item.get("idTaskInstance")),
                    _valor_texto(item.get("idTaskInstanceProximo")),
                    _valor_texto(item.get("idProcesso")),
                    _valor_texto(item.get("numeroProcesso")),
                    _valor_texto(item.get("classeJudicial")),
                    _valor_texto(item.get("idOrgaoJulgador")),
                    _valor_texto(item.get("orgaoJulgador")),
                    _valor_texto(item.get("assuntoPrincipal")),
                    _valor_texto(item.get("poloAtivo")),
                    _valor_texto(item.get("poloPassivo")),
                    _valor_texto(item.get("cargoJudicial")),
                    item.get("dataChegada"),
                    _epoch_iso(item.get("dataChegada")),
                    item.get("ultimoMovimento"),
                    _epoch_iso(item.get("ultimoMovimento")),
                    _valor_texto(item.get("descricaoUltimoMovimento")),
                    _bool_int(item.get("sigiloso")),
                    _bool_int(item.get("prioridade")),
                    _bool_int(item.get("conferido")),
                    _bool_int(item.get("temParteMoradorDeRua")),
                    _bool_int(item.get("podeMovimentarEmLote")),
                    _bool_int(item.get("podeMinutarEmLote")),
                    _bool_int(item.get("podeIntimarEmLote")),
                    _bool_int(item.get("podeDesignarAudienciaEmLote")),
                    _bool_int(item.get("podeDesignarPericiaEmLote")),
                    _bool_int(item.get("podeRenajudEmLote")),
                    json.dumps(tags, ensure_ascii=False, sort_keys=True),
                    json.dumps(item, ensure_ascii=False, sort_keys=True, default=str),
                ),
            )
        con.execute(
            """
            UPDATE caixas
               SET quantidade_api=?, quantidade_coletada=?, status=?,
                   tentativas=?, hash_sha256=?, erro=?
             WHERE snapshot_id=? AND grupo=? AND nome=?
            """,
            (
                int(quantidade_api),
                len(itens),
                status,
                tentativas,
                hash_caixa,
                erro,
                snapshot_id,
                grupo,
                tarefa,
            ),
        )
    return {
        "nome": tarefa,
        "grupo": grupo,
        "declarada": declarada,
        "api": int(quantidade_api),
        "coletada": len(itens),
        "status": status,
        "tentativas": tentativas,
        "erro": erro,
        "hash_sha256": hash_caixa,
    }


def snapshot_anterior_completo(
    grau: str,
    persona: str,
    excluir_snapshot_id: str = "",
) -> str | None:
    contexto = perfil_contexto.contexto_atual()
    perfil_id = (contexto or {}).get("perfil_id")
    if persona in perfil_contexto.PERSONAS_INTERNAS and not perfil_id:
        perfil_contexto.exigir_contexto(persona)
    with _banco() as con:
        filtros = [
            "grau=?",
            "persona=?",
            "status='completo'",
            "snapshot_id<>?",
        ]
        valores: list[Any] = [grau, persona, excluir_snapshot_id]
        if perfil_id:
            filtros.extend(["perfil_id=?", "escopo_status='validado'"])
            valores.append(perfil_id)
        row = con.execute(
            f"""
            SELECT snapshot_id
              FROM snapshots
             WHERE {' AND '.join(filtros)}
             ORDER BY iniciado_em DESC
             LIMIT 1
            """,
            valores,
        ).fetchone()
    return row["snapshot_id"] if row else None


def obter_hash_caixa(
    snapshot_id: str,
    grupo: str,
    nome: str,
) -> dict[str, Any] | None:
    with _banco() as con:
        row = con.execute(
            """
            SELECT quantidade_declarada, quantidade_api,
                   quantidade_coletada, status, hash_sha256
              FROM caixas
             WHERE snapshot_id=? AND grupo=? AND nome=?
            """,
            (snapshot_id, grupo, nome),
        ).fetchone()
    return dict(row) if row else None


def reaproveitar_caixa(
    snapshot_id: str,
    snapshot_origem: str,
    caixa: dict[str, Any],
) -> dict[str, Any]:
    """Copia atomicamente uma caixa validada de um snapshot completo."""
    grupo = caixa.get("grupo", "tarefas")
    tarefa = caixa["nome"]
    declarada = max(0, int(caixa.get("quantidade", 0)))
    origem = obter_hash_caixa(snapshot_origem, grupo, tarefa)
    if (
        not origem
        or origem["status"] != "completa"
        or int(origem["quantidade_coletada"]) != declarada
    ):
        raise ValueError("Caixa de origem ausente, divergente ou incompatível")

    colunas = (
        "chave_ocorrencia, grupo, tarefa, id_task_instance, "
        "id_task_instance_proximo, id_processo, numero_processo, "
        "classe_judicial, id_orgao_julgador, orgao_julgador, "
        "assunto_principal, polo_ativo, polo_passivo, cargo_judicial, "
        "data_chegada_epoch, data_chegada_iso, ultimo_movimento_epoch, "
        "ultimo_movimento_iso, descricao_ultimo_movimento, sigiloso, "
        "prioridade, conferido, morador_de_rua, etiquetas_json, "
        "metadados_origem_json"
    )
    with _banco() as con:
        con.execute(
            f"""
            INSERT INTO ocorrencias (
                snapshot_id, {colunas}, reaproveitado_de_snapshot
            )
            SELECT ?, {colunas}, ?
              FROM ocorrencias
             WHERE snapshot_id=? AND grupo=? AND tarefa=?
            """,
            (
                snapshot_id,
                snapshot_origem,
                snapshot_origem,
                grupo,
                tarefa,
            ),
        )
        copiados = con.execute(
            """
            SELECT COUNT(*) AS total
              FROM ocorrencias
             WHERE snapshot_id=? AND grupo=? AND tarefa=?
            """,
            (snapshot_id, grupo, tarefa),
        ).fetchone()["total"]
        if copiados != declarada:
            raise ValueError(f"Reuso copiou {copiados}, esperado {declarada}")
        con.execute(
            """
            UPDATE caixas
               SET quantidade_api=?, quantidade_coletada=?, status='completa',
                   tentativas=0, hash_sha256=?, erro=NULL
             WHERE snapshot_id=? AND grupo=? AND nome=?
            """,
            (
                origem["quantidade_api"],
                copiados,
                origem["hash_sha256"],
                snapshot_id,
                grupo,
                tarefa,
            ),
        )
    return {
        "nome": tarefa,
        "grupo": grupo,
        "declarada": declarada,
        "api": int(origem["quantidade_api"]),
        "coletada": copiados,
        "status": "completa",
        "tentativas": 0,
        "erro": None,
        "hash_sha256": origem["hash_sha256"],
        "reaproveitado": True,
        "reaproveitado_de": snapshot_origem,
    }


def finalizar_snapshot(snapshot_id: str) -> dict[str, Any]:
    with _banco() as con:
        snapshot = con.execute(
            """
            SELECT persona, grau, perfil_id, perfil_rotulo, escopo_status
              FROM snapshots WHERE snapshot_id=?
            """,
            (snapshot_id,),
        ).fetchone()
        if snapshot is None:
            raise ValueError(f"Snapshot não encontrado: {snapshot_id}")
        linha = con.execute(
            """
            SELECT COUNT(*) AS total_caixas,
                   COALESCE(SUM(quantidade_declarada), 0) AS declaradas,
                   COALESCE(SUM(quantidade_coletada), 0) AS coletadas,
                   SUM(CASE WHEN status <> 'completa' THEN 1 ELSE 0 END)
                       AS divergentes,
                   SUM(CASE WHEN status = 'erro' THEN 1 ELSE 0 END) AS erros
              FROM caixas WHERE snapshot_id=?
            """,
            (snapshot_id,),
        ).fetchone()
        unicos = con.execute(
            """
            SELECT COUNT(DISTINCT COALESCE(
                NULLIF(id_processo, ''), NULLIF(numero_processo, ''),
                chave_ocorrencia
            )) FROM ocorrencias WHERE snapshot_id=?
            """,
            (snapshot_id,),
        ).fetchone()[0]
        declaradas = int(linha["declaradas"] or 0)
        coletadas = int(linha["coletadas"] or 0)
        divergentes = int(linha["divergentes"] or 0)
        erros = int(linha["erros"] or 0)
        cobertura = (
            100.0
            if declaradas == 0 and divergentes == 0
            else round(min(100.0, coletadas * 100.0 / declaradas), 4)
            if declaradas
            else 0.0
        )
        status = (
            "completo" if divergentes == 0 and coletadas == declaradas else "incompleto"
        )
        escopo_status = snapshot["escopo_status"]
        perfil_id = snapshot["perfil_id"]
        if perfil_id:
            perfil = con.execute(
                """
                SELECT unidade, papel FROM perfis WHERE perfil_id=?
                """,
                (perfil_id,),
            ).fetchone()
            orgaos = con.execute(
                """
                SELECT DISTINCT id_orgao_julgador, orgao_julgador
                  FROM ocorrencias
                 WHERE snapshot_id=?
                   AND COALESCE(id_orgao_julgador, '') <> ''
                """,
                (snapshot_id,),
            ).fetchall()
            ids_orgaos = {str(item["id_orgao_julgador"]) for item in orgaos}
            nomes_orgaos = {
                str(item["orgao_julgador"] or "").strip()
                for item in orgaos
                if str(item["orgao_julgador"] or "").strip()
            }
            cargos = {
                str(item[0]).strip()
                for item in con.execute(
                    """
                    SELECT DISTINCT cargo_judicial
                      FROM ocorrencias
                     WHERE snapshot_id=?
                       AND COALESCE(cargo_judicial, '') <> ''
                    """,
                    (snapshot_id,),
                ).fetchall()
                if str(item[0]).strip()
            }
            unidade_esperada = perfil_contexto.normalizar_texto(
                perfil["unidade"] if perfil else ""
            )
            papel_esperado = perfil_contexto.normalizar_texto(
                perfil["papel"] if perfil else ""
            )
            nomes_normalizados = {
                perfil_contexto.normalizar_texto(nome) for nome in nomes_orgaos
            }
            cargos_normalizados = {
                perfil_contexto.normalizar_texto(cargo) for cargo in cargos
            }
            # cargo_judicial das ocorrências descreve o MAGISTRADO do
            # processo; só serve de prova para papéis de magistratura.
            # Para papéis de serventia (diretor, servidor...), a prova de
            # escopo é o órgão único coincidindo com a unidade do perfil.
            papel_de_magistratura = any(
                termo in papel_esperado
                for termo in ("juiz", "juiza", "desembargador")
            )
            vinculo_unico = (
                len(ids_orgaos) == 1
                and len(nomes_orgaos) == 1
                and unidade_esperada in nomes_normalizados
                and (
                    not papel_de_magistratura
                    or papel_esperado in cargos_normalizados
                )
            )
            if vinculo_unico:
                orgao_id = next(iter(ids_orgaos))
                orgao_nome = next(iter(nomes_orgaos))
                con.execute(
                    """
                    UPDATE perfis
                       SET orgao_julgador_id=?, orgao_julgador_nome=?,
                           vinculo_status='validado', atualizado_em=?
                     WHERE perfil_id=?
                    """,
                    (orgao_id, orgao_nome, _agora_iso(), perfil_id),
                )
                escopo_status = "validado"
            else:
                # Perfil interno sem órgão único nunca vira fonte de cache,
                # auditoria ou reaproveitamento incremental.
                status = "incompleto"
                escopo_status = "perfil_nao_validado"
        con.execute(
            """
            UPDATE snapshots
               SET finalizado_em=?, status=?, total_caixas=?,
                   ocorrencias_declaradas=?, ocorrencias_coletadas=?,
                   processos_unicos=?, caixas_divergentes=?, erros=?,
                   cobertura_percentual=?, escopo_status=?
             WHERE snapshot_id=?
            """,
            (
                _agora_iso(),
                status,
                int(linha["total_caixas"]),
                declaradas,
                coletadas,
                int(unicos),
                divergentes,
                erros,
                cobertura,
                escopo_status,
                snapshot_id,
            ),
        )
    return obter_snapshot(snapshot_id, incluir_caixas=True)


def obter_snapshot(
    snapshot_id: str | None = None,
    grau: str | None = None,
    persona: str | None = None,
    incluir_caixas: bool = True,
    incluir_orgaos: bool = True,
) -> dict[str, Any]:
    contexto = perfil_contexto.contexto_atual()
    perfil_id_esperado = (contexto or {}).get("perfil_id")
    if (
        persona in perfil_contexto.PERSONAS_INTERNAS
        and not perfil_id_esperado
    ):
        return {
            "status": "perfil_obrigatorio",
            "erro": "perfil funcional obrigatório para consultar snapshots",
            "codigo": "PERFIL_OBRIGATORIO",
        }
    with _banco() as con:
        if snapshot_id:
            row = con.execute(
                "SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
            if (
                row is not None
                and perfil_id_esperado
                and row["perfil_id"] != perfil_id_esperado
            ):
                return {
                    "status": "perfil_divergente",
                    "erro": "snapshot não pertence ao perfil funcional selecionado",
                    "codigo": "PERFIL_DIVERGENTE",
                }
        else:
            filtros = []
            valores: list[Any] = []
            if grau:
                filtros.append("grau=?")
                valores.append(grau)
            if persona:
                filtros.append("persona=?")
                valores.append(persona)
            if perfil_id_esperado:
                filtros.append("perfil_id=?")
                filtros.append("escopo_status='validado'")
                valores.append(perfil_id_esperado)
            where = f"WHERE {' AND '.join(filtros)}" if filtros else ""
            row = con.execute(
                f"""
                SELECT * FROM snapshots {where}
                 ORDER BY iniciado_em DESC LIMIT 1
                """,
                valores,
            ).fetchone()
        if row is None:
            return {
                "status": "sem_snapshot",
                "mensagem": "Ainda não há inventário persistido para este perfil/grau.",
            }
        resultado = dict(row)
        resultado["metadados"] = json.loads(resultado.pop("metadados_json") or "{}")
        resultado["banco"] = str(caminho_banco())
        if incluir_orgaos:
            resultado["orgaos_julgadores"] = [
                {
                    "id_orgao_julgador": item["id_orgao_julgador"],
                    "orgao_julgador": item["orgao_julgador"],
                    "ocorrencias": item["ocorrencias"],
                }
                for item in con.execute(
                    """
                    SELECT id_orgao_julgador, orgao_julgador,
                           COUNT(*) AS ocorrencias
                      FROM ocorrencias
                     WHERE snapshot_id=?
                     GROUP BY id_orgao_julgador, orgao_julgador
                     ORDER BY ocorrencias DESC, orgao_julgador
                    """,
                    (resultado["snapshot_id"],),
                )
            ]
        if incluir_caixas:
            resultado["caixas"] = [
                dict(item)
                for item in con.execute(
                    """
                    SELECT grupo, nome, quantidade_declarada, quantidade_api,
                           quantidade_coletada, status, tentativas,
                           hash_sha256, erro
                      FROM caixas WHERE snapshot_id=?
                     ORDER BY grupo, nome
                    """,
                    (resultado["snapshot_id"],),
                )
            ]
        return resultado


def consultar_ocorrencias(
    snapshot_id: str | None = None,
    grau: str | None = None,
    persona: str | None = None,
    tarefa: str = "",
    termo: str = "",
    pagina: int = 1,
    itens_por_pagina: int = 50,
    incluir_metadados_origem: bool = False,
) -> dict[str, Any]:
    pagina = max(1, int(pagina))
    itens_por_pagina = max(1, min(500, int(itens_por_pagina)))
    snapshot = obter_snapshot(
        snapshot_id=snapshot_id,
        grau=grau,
        persona=persona,
        incluir_caixas=False,
        incluir_orgaos=False,
    )
    if "snapshot_id" not in snapshot:
        return snapshot
    sid = snapshot["snapshot_id"]
    filtros = ["snapshot_id=?"]
    valores: list[Any] = [sid]
    if tarefa:
        filtros.append("LOWER(tarefa) LIKE ?")
        valores.append(f"%{tarefa.lower()}%")
    if termo:
        filtros.append(
            """
            (LOWER(COALESCE(numero_processo,'')) LIKE ?
             OR LOWER(COALESCE(classe_judicial,'')) LIKE ?
             OR LOWER(COALESCE(orgao_julgador,'')) LIKE ?
             OR LOWER(COALESCE(assunto_principal,'')) LIKE ?
             OR LOWER(COALESCE(polo_ativo,'')) LIKE ?
             OR LOWER(COALESCE(polo_passivo,'')) LIKE ?)
            """
        )
        busca = f"%{termo.lower()}%"
        valores.extend([busca] * 6)
    where = " AND ".join(filtros)
    with _banco() as con:
        total = con.execute(
            f"SELECT COUNT(*) FROM ocorrencias WHERE {where}", valores
        ).fetchone()[0]
        colunas = """
            chave_ocorrencia, grupo, tarefa, id_task_instance,
            id_task_instance_proximo, id_processo, numero_processo,
            classe_judicial, id_orgao_julgador, orgao_julgador,
            assunto_principal, polo_ativo, polo_passivo, cargo_judicial,
            data_chegada_epoch, data_chegada_iso, ultimo_movimento_epoch,
            ultimo_movimento_iso, descricao_ultimo_movimento, sigiloso,
            prioridade, conferido, morador_de_rua, etiquetas_json
        """
        if incluir_metadados_origem:
            colunas += ", metadados_origem_json"
        rows = con.execute(
            f"""
            SELECT {colunas} FROM ocorrencias WHERE {where}
             ORDER BY data_chegada_epoch ASC, tarefa, numero_processo
             LIMIT ? OFFSET ?
            """,
            valores + [itens_por_pagina, (pagina - 1) * itens_por_pagina],
        ).fetchall()
    itens = []
    for row in rows:
        item = dict(row)
        item["etiquetas"] = json.loads(item.pop("etiquetas_json") or "[]")
        if "metadados_origem_json" in item:
            item["metadados_origem"] = json.loads(item.pop("metadados_origem_json"))
        itens.append(item)
    total_paginas = max(1, (total + itens_por_pagina - 1) // itens_por_pagina)
    return {
        "snapshot_id": sid,
        "snapshot_status": snapshot["status"],
        "cobertura_percentual": snapshot["cobertura_percentual"],
        "filtros": {"tarefa": tarefa, "termo": termo},
        "paginacao": {
            "pagina": pagina,
            "itens_por_pagina": itens_por_pagina,
            "total_itens": total,
            "total_paginas": total_paginas,
            "tem_proxima": pagina < total_paginas,
        },
        "ocorrencias": itens,
    }


def obter_schema_acervo_tarefas() -> dict[str, Any]:
    """Catálogo autocontido para agentes montarem filtros e visualizações."""
    return {
        "schema_version": SCHEMA_ACERVO_TAREFAS,
        "compatibilidade": {
            "substitui": SCHEMA_ACERVO_ANTERIOR,
            "mudancas_relevantes": [
                (
                    "ocorrencia.tarefa e fluxo.tarefa agora são objetos com "
                    "nome limpo, valor original e valor_filtro."
                ),
                (
                    "processo.classe é resolvida pelo catálogo oficial TPU "
                    "do CNJ, sem escolher arbitrariamente códigos ambíguos."
                ),
                (
                    "facetas de tarefa e classe devolvem opções completas e "
                    "já estruturadas para controles de interface."
                ),
            ],
        },
        "entidade_raiz": "ocorrencia_em_tarefa",
        "formatos": {
            "completo": {
                "schema_version": SCHEMA_ACERVO_TAREFAS,
                "default": True,
                "itens_por_pagina_max": 500,
            },
            "compacto": {
                "schema_version": SCHEMA_ACERVO_COMPACTO,
                "itens_por_pagina_max": 5000,
                "campos_default": list(ORDEM_CAMPOS_COMPACTOS),
                "campos_opcionais": list(CAMPOS_COMPACTOS_OPCIONAIS),
                "flags_bitmask": FLAGS_COMPACTAS,
            },
        },
        "envelopes": {
            "completo": (
                "Compatibilidade v2: inclui catálogos, qualidade, avisos e "
                "dicionários globais."
            ),
            "padrao": (
                "Mantém qualidade e referências resumidas dos catálogos."
            ),
            "minimo": (
                "Retorna somente snapshot, ordenação, paginação, registros e "
                "dicionários da página; habilita fast path SQL quando não há "
                "filtros nem facetas."
            ),
        },
        "paginacao_consistente": {
            "snapshot_id": "Fixa o inventário imutável consultado.",
            "cursor": (
                "Token opaco autenticado para a próxima página; falha se "
                "snapshot ou parâmetros da consulta forem alterados."
            ),
            "complexidade_cursor_atual": (
                "Cursor garante consistência; consultas sem filtros usam SQL "
                "indexado e consultas semânticas permanecem O(n) em Python."
            ),
        },
        "cache_logico": {
            "revision": "SHA-256 estável dos metadados materiais do snapshot.",
            "if_revision": (
                "Quando igual à revisão atual, retorna status=not_modified e "
                "zero registros."
            ),
        },
        "acoes_relacionadas": {
            "exportar_acervo": {
                "formatos": ["ndjson", "csv", "json", "sqlite"],
                "modo_default": "compacto",
                "ttl_dias": 7,
            },
            "estatisticas_acervo": {
                "dimensoes": ["tarefa", "classe", "assunto", "orgao", "mes_chegada"],
            },
            "metricas_desempenho_acervo": {
                "percentis": ["p50", "p95", "p99"],
                "dados_processuais_incluidos": False,
                "armazenamento": "memoria_do_processo",
            },
        },
        "identidade": {
            "ocorrencia": "ocorrencia.chave",
            "processo": "processo.numero_cnj",
            "observacao": (
                "Um mesmo processo pode aparecer em mais de uma tarefa e "
                "deve permanecer como ocorrência distinta."
            ),
        },
        "grupos": {
            "ocorrencia": "Estado operacional do processo naquela tarefa.",
            "processo": "Identificação, classe e assunto principal.",
            "partes": "Polos preservados; a fonte contém só a principal de cada polo.",
            "unidade_judicial": "Órgão julgador e cargo.",
            "fluxo": (
                "Tarefa estruturada (nome limpo + origem), responsável e lembretes."
            ),
            "datas": "Datas ISO/epoch e idades calculadas no instante do snapshot.",
            "ultimo_movimento": "Data e descrição do movimento mais recente.",
            "indicadores": "Flags nativas do PJe.",
            "etiquetas": "Tags do processo.",
            "capacidades_operacao_em_lote": "Ações em lote, quando informadas.",
            "campos_extras_origem": "Campos futuros ainda não normalizados.",
        },
        "filtros": {
            "nome_tarefa": {"tipo": "texto", "operador": "contem"},
            "termo_busca": {
                "tipo": "texto",
                "campos": [
                    "CNJ",
                    "classe",
                    "assunto",
                    "partes",
                    "órgão",
                    "último movimento",
                    "responsável",
                ],
            },
            "filtro_classe": {"tipo": "texto", "operador": "contem"},
            "filtro_assunto": {"tipo": "texto", "operador": "contem"},
            "filtro_parte": {"tipo": "texto", "operador": "contem"},
            "filtro_orgao": {"tipo": "texto", "operador": "contem"},
            "filtro_etiqueta": {"tipo": "texto", "operador": "contem"},
            "sigiloso": {"tipo": "booleano_nullable"},
            "prioridade": {"tipo": "booleano_nullable"},
            "conferido": {"tipo": "booleano_nullable"},
            "morador_de_rua": {"tipo": "booleano_nullable"},
            "data_chegada_de": {"tipo": "data_iso"},
            "data_chegada_ate": {"tipo": "data_iso"},
            "dias_na_tarefa_min": {"tipo": "inteiro"},
            "dias_na_tarefa_max": {"tipo": "inteiro"},
        },
        "ordenacoes": [
            "data_chegada",
            "ultimo_movimento",
            "numero_processo",
            "classe",
            "assunto",
            "tarefa",
            "dias_na_tarefa",
        ],
        "contrato_de_filtros": {
            "fonte_das_opcoes": "facetas.<dimensao>.valores",
            "regra": (
                "Envie filtro.valor no parâmetro indicado por "
                "filtro.parametro; rotulo é somente para exibição."
            ),
            "facetas_completas": [
                "tarefas",
                "classes",
                "assuntos",
                "orgaos_julgadores",
            ],
            "faceta_limitada": {
                "etiquetas": (
                    "Pode ser truncada por volume; a resposta informa "
                    "retornados, total_valores_distintos e truncada."
                )
            },
        },
        "catalogo_classes": tpu_catalogo.metadados_catalogo(),
        "limites_da_fonte": {
            "classe_por_extenso": (
                "Enriquecida pelo catálogo oficial TPU do CNJ quando a fonte "
                "PJe entrega somente a sigla."
            ),
            "codigo_classe_tpu": (
                "Só é afirmado quando fornecido pelo PJe ou quando a sigla "
                "possui um único código no catálogo. Em caso de histórico "
                "ambíguo, codigo_tpu fica nulo e os candidatos são expostos."
            ),
            "partes": "Somente uma string principal por polo.",
            "advogados": False,
            "documentos_partes": False,
            "valor_causa": False,
            "data_distribuicao": False,
            "documentos_autos": False,
            "prazo_data_limite": False,
        },
    }


def _contador_faceta(
    valores: Iterable[Any],
    limite: int | None = 200,
    parametro_filtro: str | None = None,
) -> dict[str, Any]:
    contador = Counter(
        str(valor).strip()
        for valor in valores
        if valor not in (None, "") and str(valor).strip()
    )
    ordenados = sorted(contador.items(), key=lambda par: (-par[1], par[0]))
    selecionados = ordenados if limite is None else ordenados[:limite]
    total_itens = sum(contador.values())
    valores_estruturados = []
    for valor, quantidade in selecionados:
        opcao = {
            "valor": valor,
            "rotulo": valor,
            "quantidade": quantidade,
            "percentual": round(quantidade * 100 / total_itens, 4)
            if total_itens
            else 0.0,
        }
        if parametro_filtro:
            opcao["filtro"] = {
                "parametro": parametro_filtro,
                "valor": valor,
            }
        valores_estruturados.append(opcao)
    truncada = limite is not None and len(ordenados) > limite
    return {
        "valores": valores_estruturados,
        "total_valores_distintos": len(ordenados),
        "retornados": len(selecionados),
        "truncada": truncada,
        "completa": not truncada,
    }


def _faceta_tarefas(valores: Iterable[Any]) -> dict[str, Any]:
    faceta = _contador_faceta(
        valores,
        limite=None,
        parametro_filtro="nome_tarefa",
    )
    for opcao in faceta["valores"]:
        tarefa = estruturar_tarefa(opcao["valor"])
        opcao["rotulo"] = tarefa["nome"]
        opcao["tarefa"] = tarefa
    faceta["semantica"] = (
        "Catálogo completo das tarefas não vazias do resultado, antes da "
        "paginação. valor/filtro.valor preservam o nome original do PJe."
    )
    return faceta


def _faceta_classes(valores: Iterable[Any]) -> dict[str, Any]:
    faceta = _contador_faceta(
        valores,
        limite=None,
        parametro_filtro="filtro_classe",
    )
    for opcao in faceta["valores"]:
        classe = tpu_catalogo.resolver_classe(opcao["valor"])
        opcao["rotulo"] = classe["exibicao"] or opcao["valor"]
        opcao["classe"] = classe
    faceta["semantica"] = (
        "Catálogo completo das classes do resultado. O valor do filtro é a "
        "sigla PJe; o rótulo usa a descrição oficial TPU."
    )
    return faceta


def _resumo_qualidade(
    itens: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    itens = list(itens)
    classes = [
        tpu_catalogo.resolver_classe(
            item.get("classe_judicial"),
            descricao_origem=_primeiro_valor(
                item.get("metadados_origem") or {},
                "classeJudicialDescricao",
                "descricaoClasseJudicial",
                "nomeClasseJudicial",
            ),
            codigo_origem=_primeiro_valor(
                item.get("metadados_origem") or {},
                "codigoClasseProcessual",
                "codigoClasseJudicial",
                "classeJudicialCodigo",
            ),
        )
        for item in itens
    ]
    tarefas = [estruturar_tarefa(item.get("tarefa")) for item in itens]
    return {
        "registros_avaliados": len(itens),
        "classes": {
            "descricao_resolvida": sum(
                bool(classe["descricao_completa"]) for classe in classes
            ),
            "codigo_tpu_confirmado": sum(
                bool(classe["codigo_tpu_confirmado"]) for classe in classes
            ),
            "codigo_tpu_ambiguo": sum(
                bool(classe["codigo_tpu_ambiguo"]) for classe in classes
            ),
            "sigla_nao_mapeada": sum(
                not classe["mapeada_no_catalogo"] for classe in classes
            ),
            "siglas_distintas": len(
                {classe["sigla_pje"] for classe in classes if classe["sigla_pje"]}
            ),
        },
        "tarefas": {
            "nomes_distintos_originais": len(
                {
                    tarefa["nome_original_pje"]
                    for tarefa in tarefas
                    if tarefa["nome_original_pje"]
                }
            ),
            "nomes_distintos_normalizados": len(
                {tarefa["nome"] for tarefa in tarefas if tarefa["nome"]}
            ),
            "registros_com_normalizacao": sum(
                bool(tarefa["foi_normalizada"]) for tarefa in tarefas
            ),
        },
        "partes_principais": {
            "com_polo_ativo": sum(bool(item.get("polo_ativo")) for item in itens),
            "com_polo_passivo": sum(bool(item.get("polo_passivo")) for item in itens),
            "sem_polo_ativo": sum(not bool(item.get("polo_ativo")) for item in itens),
            "sem_polo_passivo": sum(
                not bool(item.get("polo_passivo")) for item in itens
            ),
            "escopo": "somente_parte_principal_de_cada_polo",
        },
    }


def _nome_etiqueta_compacta(etiqueta: Any) -> str:
    if isinstance(etiqueta, dict):
        return str(etiqueta.get("nomeTagCompleto") or etiqueta.get("nomeTag") or "")
    return str(etiqueta or "")


def _dicionarios_compactos(
    itens: Iterable[dict[str, Any]],
) -> tuple[dict[str, list[str]], dict[str, dict[str, int]]]:
    tarefas = set()
    classes = set()
    assuntos = set()
    for item in itens:
        tarefas.add(str(item.get("tarefa") or "—"))
        classes.add(str(item.get("classe_judicial") or "—"))
        assuntos.add(str(item.get("assunto_principal") or "—"))
    dicionarios = {
        "tarefas": sorted(tarefas),
        "classes": sorted(classes),
        "assuntos": sorted(assuntos),
    }
    indices = {
        nome: {valor: indice for indice, valor in enumerate(valores)}
        for nome, valores in dicionarios.items()
    }
    return dicionarios, indices


def _campos_compactos_solicitados(campos: str) -> list[str]:
    validos = set(ORDEM_CAMPOS_COMPACTOS) | set(CAMPOS_COMPACTOS_OPCIONAIS)
    solicitados = (
        [campo.strip() for campo in campos.split(",") if campo.strip()]
        if campos.strip()
        else list(ORDEM_CAMPOS_COMPACTOS)
    )
    invalidos = sorted(set(solicitados) - validos)
    if invalidos:
        raise ValueError(f"Campos inválidos: {invalidos}. Válidos: {sorted(validos)}")
    return list(dict.fromkeys(solicitados))


def _projetar_compacto(
    item: dict[str, Any],
    campos: Iterable[str],
    indices: dict[str, dict[str, int]],
    referencia: str,
) -> dict[str, Any]:
    tarefa = str(item.get("tarefa") or "—")
    classe = str(item.get("classe_judicial") or "—")
    assunto = str(item.get("assunto_principal") or "—")
    partes = " x ".join(
        filter(
            None,
            [
                str(item.get("polo_ativo") or ""),
                str(item.get("polo_passivo") or ""),
            ],
        )
    )
    flags = (
        FLAGS_COMPACTAS["sigiloso"] * bool(item.get("sigiloso"))
        | FLAGS_COMPACTAS["prioridade"] * bool(item.get("prioridade"))
        | FLAGS_COMPACTAS["morador_de_rua"] * bool(item.get("morador_de_rua"))
        | FLAGS_COMPACTAS["conferido"] * bool(item.get("conferido"))
    )
    chegada = item.get("data_chegada_iso")
    valores = {
        "cnj": item.get("numero_processo"),
        "tarefa": indices["tarefas"][tarefa],
        "classe": indices["classes"][classe],
        "assunto": indices["assuntos"][assunto],
        "partes": partes,
        "chegada": chegada[:10] if chegada else None,
        "dias": _dias_entre(chegada, referencia),
        "dias_mov": _dias_entre(item.get("ultimo_movimento_iso"), referencia),
        "flags": flags,
        "etiquetas": "; ".join(
            filter(
                None,
                (
                    _nome_etiqueta_compacta(etiqueta)
                    for etiqueta in item.get("etiquetas") or []
                ),
            )
        ),
        "ocorrencia_chave": item.get("chave_ocorrencia"),
        "orgao": item.get("orgao_julgador"),
    }
    return {campo: valores[campo] for campo in campos}


def _pagina_compacta_sem_filtros(
    snapshot_id: str,
    pagina: int,
    itens_por_pagina: int,
    ordenar_por: str,
    direcao: str,
    total_snapshot: int,
) -> tuple[int, list[dict[str, Any]]]:
    """Fast path SQL para projeção compacta sem filtros nem facetas."""
    ordenacoes = {
        # Em SQLite, NULL fica antes dos epochs positivos em ASC e depois em
        # DESC, equivalendo ao fallback numérico zero usado no caminho Python.
        "data_chegada": ("data_chegada_epoch", False),
        "ultimo_movimento": ("ultimo_movimento_epoch", False),
        "numero_processo": ("COALESCE(numero_processo, '')", False),
        "classe": ("COALESCE(classe_judicial, '')", False),
        "assunto": ("COALESCE(assunto_principal, '')", False),
        "tarefa": ("COALESCE(tarefa, '')", False),
        # Menos dias significa chegada mais recente: a direção é invertida.
        "dias_na_tarefa": ("data_chegada_epoch", True),
    }
    expressao, inverter = ordenacoes.get(
        ordenar_por,
        ordenacoes["data_chegada"],
    )
    direcao_sql = "DESC" if str(direcao).casefold() == "desc" else "ASC"
    if inverter:
        direcao_sql = "ASC" if direcao_sql == "DESC" else "DESC"
    offset = (pagina - 1) * itens_por_pagina
    with _banco() as con:
        rows = con.execute(
            f"""
            SELECT chave_ocorrencia, tarefa, numero_processo,
                   classe_judicial, assunto_principal,
                   polo_ativo, polo_passivo, orgao_julgador,
                   data_chegada_iso, ultimo_movimento_iso,
                   sigiloso, prioridade, conferido, morador_de_rua,
                   etiquetas_json
              FROM ocorrencias
             WHERE snapshot_id=?
             ORDER BY {expressao} {direcao_sql},
                      chave_ocorrencia {direcao_sql}
             LIMIT ? OFFSET ?
            """,
            (snapshot_id, itens_por_pagina, offset),
        ).fetchall()
    itens: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["etiquetas"] = json.loads(item.pop("etiquetas_json") or "[]")
        itens.append(item)
    return max(0, int(total_snapshot)), itens


def consultar_acervo_estruturado(
    snapshot_id: str | None = None,
    grau: str | None = None,
    persona: str | None = None,
    tarefa: str = "",
    termo: str = "",
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
) -> dict[str, Any]:
    """Consulta pronta para UI, mantendo a semântica de ocorrência.

    Para acervos grandes, prefira ``formato='compacto'`` ou a ação
    ``exportar_acervo``. O modo completo é indicado para auditoria de
    registros individuais e permanece como comportamento padrão.
    """
    inicio_total = time.perf_counter()
    formato = str(formato or "completo").strip().casefold()
    if formato not in {"completo", "compacto"}:
        raise ValueError("formato deve ser 'completo' ou 'compacto'")
    envelope = str(envelope or "completo").strip().casefold()
    if envelope not in ENVELOPES_ACERVO:
        raise ValueError("envelope deve ser 'minimo', 'padrao' ou 'completo'")
    teto_pagina = 5000 if formato == "compacto" else 500
    itens_por_pagina = max(1, min(teto_pagina, int(itens_por_pagina)))
    parametros_cursor = {
        "tarefa": tarefa,
        "termo": termo,
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
        "itens_por_pagina": itens_por_pagina,
        "ordenar_por": ordenar_por,
        "direcao": direcao,
        "incluir_facetas": bool(incluir_facetas),
        "incluir_campos_extras": bool(incluir_campos_extras),
        "incluir_metadados_origem": bool(incluir_metadados_origem),
        "formato": formato,
        "campos": campos,
        "envelope": envelope,
    }
    fingerprint_cursor = _fingerprint_consulta_cursor(parametros_cursor)
    pagina = max(1, int(pagina))
    if cursor:
        cursor_decodificado = _decodificar_cursor(cursor)
        if cursor_decodificado.get("fingerprint") != fingerprint_cursor:
            raise ValueError("cursor não corresponde aos filtros da consulta")
        cursor_snapshot = str(cursor_decodificado.get("snapshot_id") or "")
        if snapshot_id and str(snapshot_id) != cursor_snapshot:
            raise ValueError("cursor pertence a outro snapshot")
        snapshot_id = cursor_snapshot
        pagina = max(1, int(cursor_decodificado.get("pagina") or 1))
    snapshot = obter_snapshot(
        snapshot_id=snapshot_id,
        grau=grau,
        persona=persona,
        incluir_caixas=False,
        incluir_orgaos=False,
    )
    fim_snapshot = time.perf_counter()
    if "snapshot_id" not in snapshot:
        return snapshot

    sid = snapshot["snapshot_id"]
    revisao = _revisao_snapshot(snapshot)
    if if_revision and hmac.compare_digest(str(if_revision).strip(), revisao):
        resposta_nao_modificada = {
            "schema_version": (
                SCHEMA_ACERVO_COMPACTO
                if formato == "compacto"
                else SCHEMA_ACERVO_TAREFAS
            ),
            "status": "not_modified",
            "snapshot": {
                "id": sid,
                "revision": revisao,
                "status": snapshot.get("status"),
                "finalizado_em": snapshot.get("finalizado_em"),
                "cobertura_percentual": snapshot.get("cobertura_percentual"),
            },
            "registros": [],
        }
        fim = time.perf_counter()
        observabilidade_acervo.registrar(
            {
                "total_ms": (fim - inicio_total) * 1000,
                "snapshot_ms": (fim_snapshot - inicio_total) * 1000,
                "sqlite_ms": 0,
                "filtros_ms": 0,
                "projecao_ms": 0,
                "itens_retornados": 0,
                "payload_json_bytes": len(
                    json.dumps(
                        resposta_nao_modificada,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ),
                "not_modified": True,
            }
        )
        return resposta_nao_modificada
    referencia = (
        snapshot.get("finalizado_em") or snapshot.get("iniciado_em") or _agora_iso()
    )
    filtros_ativos_fast_path = any(
        (
            tarefa.strip(),
            termo.strip(),
            filtro_classe.strip(),
            filtro_assunto.strip(),
            filtro_parte.strip(),
            filtro_orgao.strip(),
            filtro_etiqueta.strip(),
            data_chegada_de.strip(),
            data_chegada_ate.strip(),
            sigiloso is not None,
            prioridade is not None,
            conferido is not None,
            morador_de_rua is not None,
            dias_na_tarefa_min is not None,
            dias_na_tarefa_max is not None,
        )
    )
    usar_fast_path = (
        formato == "compacto"
        and envelope == "minimo"
        and not incluir_facetas
        and not filtros_ativos_fast_path
    )
    if usar_fast_path:
        inicio_sqlite = time.perf_counter()
        total_filtrados, pagina_legada = _pagina_compacta_sem_filtros(
            sid,
            pagina,
            itens_por_pagina,
            ordenar_por,
            direcao,
            int(snapshot.get("ocorrencias_coletadas") or 0),
        )
        fim_sqlite = time.perf_counter()
        campos_selecionados = _campos_compactos_solicitados(campos)
        dicionarios, indices = _dicionarios_compactos(pagina_legada)
        registros = [
            _projetar_compacto(item, campos_selecionados, indices, referencia)
            for item in pagina_legada
        ]
        fim_projecao = time.perf_counter()
        total_paginas = max(
            1,
            (total_filtrados + itens_por_pagina - 1) // itens_por_pagina,
        )
        direcao_normalizada = (
            "desc" if str(direcao).casefold() == "desc" else "asc"
        )
        resposta_rapida: dict[str, Any] = {
            "schema_version": SCHEMA_ACERVO_COMPACTO,
            "status": "ok",
            "snapshot": {
                "id": sid,
                "revision": revisao,
                "status": snapshot.get("status"),
                "finalizado_em": snapshot.get("finalizado_em"),
                "cobertura_percentual": snapshot.get("cobertura_percentual"),
            },
            "consulta": {
                "ordenacao": {
                    "campo": ordenar_por,
                    "direcao": direcao_normalizada,
                },
                "caminho_execucao": "sql_compacto_sem_filtros",
            },
            "paginacao": {
                "pagina": pagina,
                "itens_por_pagina": itens_por_pagina,
                "total_itens": total_filtrados,
                "total_paginas": total_paginas,
                "tem_proxima": pagina < total_paginas,
                "next_cursor": (
                    _codificar_cursor(
                        {
                            "v": 1,
                            "snapshot_id": sid,
                            "pagina": pagina + 1,
                            "fingerprint": fingerprint_cursor,
                        }
                    )
                    if pagina < total_paginas
                    else None
                ),
            },
            "registros": registros,
            "avisos_criticos": [],
            "contrato": {
                "entidade_raiz": "ocorrencia_em_tarefa_compacta",
                "complementa": SCHEMA_ACERVO_TAREFAS,
                "substitui": None,
                "indices": {
                    "tarefa": "dicionarios.tarefas",
                    "classe": "dicionarios.classes",
                    "assunto": "dicionarios.assuntos",
                },
                "flags_bitmask": FLAGS_COMPACTAS,
                "campos_retornados": campos_selecionados,
            },
            "dicionarios": dicionarios,
        }
        fim_total = time.perf_counter()
        payload_bytes = len(
            json.dumps(
                resposta_rapida,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        observabilidade_acervo.registrar(
            {
                "total_ms": (fim_total - inicio_total) * 1000,
                "snapshot_ms": (fim_snapshot - inicio_total) * 1000,
                "sqlite_ms": (fim_sqlite - inicio_sqlite) * 1000,
                "filtros_ms": 0,
                "projecao_ms": (fim_projecao - fim_sqlite) * 1000,
                "montagem_resposta_ms": (fim_total - fim_projecao) * 1000,
                "itens_filtrados": total_filtrados,
                "itens_retornados": len(registros),
                "payload_json_bytes": payload_bytes,
                "not_modified": False,
                "fast_path": True,
            }
        )
        return resposta_rapida
    inicio_sqlite = time.perf_counter()
    with _banco() as con:
        rows = con.execute(
            """
            SELECT chave_ocorrencia, grupo, tarefa, id_task_instance,
                   id_task_instance_proximo, id_processo, numero_processo,
                   classe_judicial, id_orgao_julgador, orgao_julgador,
                   assunto_principal, polo_ativo, polo_passivo, cargo_judicial,
                   data_chegada_epoch, data_chegada_iso,
                   ultimo_movimento_epoch, ultimo_movimento_iso,
                   descricao_ultimo_movimento, sigiloso, prioridade,
                   conferido, morador_de_rua, etiquetas_json,
                   metadados_origem_json, reaproveitado_de_snapshot
              FROM ocorrencias
             WHERE snapshot_id=?
            """,
            (sid,),
        ).fetchall()
    fim_sqlite = time.perf_counter()

    itens_legados: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["etiquetas"] = json.loads(item.pop("etiquetas_json") or "[]")
        item["metadados_origem"] = json.loads(item.pop("metadados_origem_json") or "{}")
        itens_legados.append(item)

    termos = {
        "tarefa": _texto_busca(tarefa.strip()),
        "geral": _texto_busca(termo.strip()),
        "classe": _texto_busca(filtro_classe.strip()),
        "assunto": _texto_busca(filtro_assunto.strip()),
        "parte": _texto_busca(filtro_parte.strip()),
        "orgao": _texto_busca(filtro_orgao.strip()),
        "etiqueta": _texto_busca(filtro_etiqueta.strip()),
    }
    limite_chegada_de = data_chegada_de.strip()
    limite_chegada_ate = data_chegada_ate.strip()
    if len(limite_chegada_de) == 10:
        limite_chegada_de += "T00:00:00"
    if len(limite_chegada_ate) == 10:
        limite_chegada_ate += "T23:59:59.999999"

    def texto(valor: Any) -> str:
        return _texto_busca(valor)

    def classe_resolvida(item: dict[str, Any]) -> dict[str, Any]:
        origem = item["metadados_origem"]
        return tpu_catalogo.resolver_classe(
            item.get("classe_judicial"),
            descricao_origem=_primeiro_valor(
                origem,
                "classeJudicialDescricao",
                "descricaoClasseJudicial",
                "nomeClasseJudicial",
            ),
            codigo_origem=_primeiro_valor(
                origem,
                "codigoClasseProcessual",
                "codigoClasseJudicial",
                "classeJudicialCodigo",
            ),
        )

    def corresponde(item: dict[str, Any]) -> bool:
        origem = item["metadados_origem"]
        tarefa_estruturada = estruturar_tarefa(item.get("tarefa"))
        campo_tarefa = " ".join(
            [
                texto(tarefa_estruturada["nome_original_pje"]),
                texto(tarefa_estruturada["nome"]),
            ]
        )
        if termos["tarefa"] not in campo_tarefa:
            return False
        classe = classe_resolvida(item)
        campo_classe = " ".join(
            [
                texto(classe.get("sigla_pje")),
                texto(classe.get("codigo_tpu")),
                texto(classe.get("descricao_completa")),
                " ".join(map(texto, classe.get("codigos_tpu_candidatos", []))),
                " ".join(map(texto, classe.get("descricoes_candidatas", []))),
            ]
        )
        if termos["classe"] not in campo_classe:
            return False
        if termos["assunto"] not in texto(item.get("assunto_principal")):
            return False
        if termos["parte"] and termos["parte"] not in " ".join(
            [texto(item.get("polo_ativo")), texto(item.get("polo_passivo"))]
        ):
            return False
        if termos["orgao"] not in texto(item.get("orgao_julgador")):
            return False
        nomes_etiquetas = " ".join(
            texto(etiqueta.get("nomeTag") or etiqueta.get("nomeTagCompleto"))
            if isinstance(etiqueta, dict)
            else texto(etiqueta)
            for etiqueta in item.get("etiquetas", [])
        )
        if termos["etiqueta"] and termos["etiqueta"] not in nomes_etiquetas:
            return False

        filtros_flags = (
            ("sigiloso", sigiloso),
            ("prioridade", prioridade),
            ("conferido", conferido),
            ("morador_de_rua", morador_de_rua),
        )
        for campo, esperado in filtros_flags:
            if esperado is not None and bool(item.get(campo)) != bool(esperado):
                return False

        chegada = item.get("data_chegada_iso") or ""
        if limite_chegada_de and chegada < limite_chegada_de:
            return False
        if limite_chegada_ate and chegada > limite_chegada_ate:
            return False
        dias = _dias_entre(chegada, referencia)
        if dias_na_tarefa_min is not None and (
            dias is None or dias < int(dias_na_tarefa_min)
        ):
            return False
        if dias_na_tarefa_max is not None and (
            dias is None or dias > int(dias_na_tarefa_max)
        ):
            return False

        if termos["geral"]:
            campo_geral = " ".join(
                [
                    texto(item.get("numero_processo")),
                    campo_classe,
                    texto(item.get("assunto_principal")),
                    texto(item.get("polo_ativo")),
                    texto(item.get("polo_passivo")),
                    texto(item.get("orgao_julgador")),
                    texto(item.get("descricao_ultimo_movimento")),
                    texto(origem.get("nomeResponsavelTarefa")),
                    texto(origem.get("loginResponsavelTarefa")),
                    nomes_etiquetas,
                    campo_tarefa,
                ]
            )
            if termos["geral"] not in campo_geral:
                return False
        return True

    filtrados = [item for item in itens_legados if corresponde(item)]
    total_filtrados = len(filtrados)
    fim_filtros = time.perf_counter()

    chaves_capacidade = (
        "podeMovimentarEmLote",
        "podeMinutarEmLote",
        "podeIntimarEmLote",
        "podeDesignarAudienciaEmLote",
        "podeDesignarPericiaEmLote",
        "podeRenajudEmLote",
    )
    capacidades_por_tarefa: dict[str, dict[str, Any]] = {}
    for item in itens_legados:
        origem = item.get("metadados_origem") or {}
        capacidades_encontradas = {
            chave: origem[chave] for chave in chaves_capacidade if chave in origem
        }
        if capacidades_encontradas:
            capacidades_por_tarefa[item.get("tarefa") or ""] = capacidades_encontradas

    ordenacoes = {
        "data_chegada": lambda i: i.get("data_chegada_epoch") or 0,
        "ultimo_movimento": lambda i: i.get("ultimo_movimento_epoch") or 0,
        "numero_processo": lambda i: texto(i.get("numero_processo")),
        "classe": lambda i: texto(
            classe_resolvida(i).get("descricao_completa") or i.get("classe_judicial")
        ),
        "assunto": lambda i: texto(i.get("assunto_principal")),
        "tarefa": lambda i: texto(estruturar_tarefa(i.get("tarefa")).get("nome")),
        "dias_na_tarefa": lambda i: (
            _dias_entre(i.get("data_chegada_iso"), referencia) or 0
        ),
    }
    if ordenar_por not in ordenacoes:
        ordenar_por = "data_chegada"
    direcao = "desc" if str(direcao).casefold() == "desc" else "asc"
    filtrados.sort(
        key=lambda item: (
            ordenacoes[ordenar_por](item),
            texto(item.get("chave_ocorrencia")),
        ),
        reverse=direcao == "desc",
    )

    inicio = (pagina - 1) * itens_por_pagina
    pagina_legada = filtrados[inicio : inicio + itens_por_pagina]
    if formato == "compacto":
        campos_selecionados = _campos_compactos_solicitados(campos)
        fonte_dicionarios = pagina_legada if envelope == "minimo" else filtrados
        dicionarios, indices = _dicionarios_compactos(fonte_dicionarios)
        registros = [
            _projetar_compacto(item, campos_selecionados, indices, referencia)
            for item in pagina_legada
        ]
    else:
        registros = [
            estruturar_ocorrencia(
                item,
                referencia_temporal=referencia,
                capacidades_tarefa=capacidades_por_tarefa.get(item.get("tarefa") or ""),
                incluir_campos_extras=incluir_campos_extras,
                incluir_metadados_origem=incluir_metadados_origem,
            )
            for item in pagina_legada
        ]
    fim_projecao = time.perf_counter()

    total_paginas = max(1, (total_filtrados + itens_por_pagina - 1) // itens_por_pagina)
    resposta: dict[str, Any] = {
        "schema_version": SCHEMA_ACERVO_TAREFAS,
        "contrato": {
            "entidade_raiz": "ocorrencia_em_tarefa",
            "identificador_ocorrencia": "ocorrencia.chave",
            "identificador_processo": "processo.numero_cnj",
            "opcoes_de_filtro": "facetas.<dimensao>.valores",
            "como_aplicar_filtro": (
                "Use filtro.parametro e filtro.valor da opção selecionada."
            ),
            "compatibilidade": {
                "substitui": SCHEMA_ACERVO_ANTERIOR,
                "tarefa_agora_estruturada": True,
                "classe_enriquecida_por_tpu": True,
            },
        },
        "snapshot": {
            "id": sid,
            "revision": revisao,
            "status": snapshot.get("status"),
            "iniciado_em": snapshot.get("iniciado_em"),
            "finalizado_em": snapshot.get("finalizado_em"),
            "referencia_temporal": referencia,
            "cobertura_percentual": snapshot.get("cobertura_percentual"),
            "ocorrencias_coletadas": snapshot.get("ocorrencias_coletadas"),
            "processos_unicos": snapshot.get("processos_unicos"),
            "metadados": snapshot.get("metadados", {}),
        },
        "consulta": {
            "filtros_aplicados": {
                "nome_tarefa": tarefa,
                "termo_busca": termo,
                "classe": filtro_classe,
                "assunto": filtro_assunto,
                "parte": filtro_parte,
                "orgao": filtro_orgao,
                "etiqueta": filtro_etiqueta,
                "sigiloso": sigiloso,
                "prioridade": prioridade,
                "conferido": conferido,
                "morador_de_rua": morador_de_rua,
                "data_chegada_de": data_chegada_de,
                "data_chegada_ate": data_chegada_ate,
                "dias_na_tarefa_min": dias_na_tarefa_min,
                "dias_na_tarefa_max": dias_na_tarefa_max,
            },
            "ordenacao": {"campo": ordenar_por, "direcao": direcao},
        },
        "paginacao": {
            "pagina": pagina,
            "itens_por_pagina": itens_por_pagina,
            "total_itens": total_filtrados,
            "total_paginas": total_paginas,
            "tem_proxima": pagina < total_paginas,
            "next_cursor": (
                _codificar_cursor(
                    {
                        "v": 1,
                        "snapshot_id": sid,
                        "pagina": pagina + 1,
                        "fingerprint": fingerprint_cursor,
                    }
                )
                if pagina < total_paginas
                else None
            ),
        },
        "registros": registros,
        "catalogos": {
            "classes_tpu": tpu_catalogo.metadados_catalogo(),
            "normalizacao_tarefas": {
                "aliases_editoriais": [
                    {"original": original, "normalizado": normalizado}
                    for original, normalizado in sorted(ALIASES_TAREFAS.items())
                ],
                "valor_original_preservado": True,
            },
        },
        "qualidade_dados": _resumo_qualidade(filtrados),
        "avisos_de_completude": [
            (
                "A descrição da classe é enriquecida pela TPU oficial quando "
                "o PJe fornece só a sigla; código ambíguo nunca é escolhido "
                "sem evidência e aparece em codigos_tpu_candidatos."
            ),
            (
                "Partes contêm somente a pessoa principal de cada polo; "
                "não equivalem ao cadastro completo dos autos."
            ),
        ],
    }

    if incluir_facetas:
        faixas = Counter()
        for item in filtrados:
            dias = _dias_entre(item.get("data_chegada_iso"), referencia)
            if dias is None:
                faixa = "sem_data"
            elif dias <= 7:
                faixa = "0_a_7_dias"
            elif dias <= 30:
                faixa = "8_a_30_dias"
            elif dias <= 90:
                faixa = "31_a_90_dias"
            elif dias <= 365:
                faixa = "91_a_365_dias"
            else:
                faixa = "mais_de_365_dias"
            faixas[faixa] += 1
        resposta["facetas"] = {
            "escopo": "resultado_filtrado_antes_da_paginacao",
            "instrucao": (
                "As listas abaixo são opções de filtro prontas. Use o bloco "
                "filtro de cada opção; não derive valores do rótulo."
            ),
            "tarefas": _faceta_tarefas(i.get("tarefa") for i in filtrados),
            "classes": _faceta_classes(i.get("classe_judicial") for i in filtrados),
            "assuntos": _contador_faceta(
                (i.get("assunto_principal") for i in filtrados),
                limite=None,
                parametro_filtro="filtro_assunto",
            ),
            "orgaos_julgadores": _contador_faceta(
                (i.get("orgao_julgador") for i in filtrados),
                limite=None,
                parametro_filtro="filtro_orgao",
            ),
            "etiquetas": _contador_faceta(
                (
                    (etiqueta.get("nomeTagCompleto") or etiqueta.get("nomeTag"))
                    for item in filtrados
                    for etiqueta in item.get("etiquetas", [])
                    if isinstance(etiqueta, dict)
                ),
                limite=200,
                parametro_filtro="filtro_etiqueta",
            ),
            "indicadores": {
                "sigilosos": sum(bool(i.get("sigiloso")) for i in filtrados),
                "prioritarios": sum(bool(i.get("prioridade")) for i in filtrados),
                "conferidos": sum(bool(i.get("conferido")) for i in filtrados),
                "com_parte_morador_de_rua": sum(
                    bool(i.get("morador_de_rua")) for i in filtrados
                ),
            },
            "faixas_permanencia": [
                {"faixa": faixa, "quantidade": quantidade}
                for faixa, quantidade in (
                    ("0_a_7_dias", faixas["0_a_7_dias"]),
                    ("8_a_30_dias", faixas["8_a_30_dias"]),
                    ("31_a_90_dias", faixas["31_a_90_dias"]),
                    ("91_a_365_dias", faixas["91_a_365_dias"]),
                    ("mais_de_365_dias", faixas["mais_de_365_dias"]),
                    ("sem_data", faixas["sem_data"]),
                )
            ],
        }
    if formato == "compacto":
        resposta["schema_version"] = SCHEMA_ACERVO_COMPACTO
        resposta["contrato"] = {
            "entidade_raiz": "ocorrencia_em_tarefa_compacta",
            "complementa": SCHEMA_ACERVO_TAREFAS,
            "substitui": None,
            "indices": {
                "tarefa": "dicionarios.tarefas",
                "classe": "dicionarios.classes",
                "assunto": "dicionarios.assuntos",
            },
            "flags_bitmask": FLAGS_COMPACTAS,
            "campos_retornados": campos_selecionados,
        }
        resposta["dicionarios"] = dicionarios
    if envelope == "padrao":
        resposta["catalogos"] = {
            "classes_tpu": tpu_catalogo.metadados_catalogo(),
            "normalizacao_tarefas": {
                "aliases_editoriais_total": len(ALIASES_TAREFAS),
                "valor_original_preservado": True,
            },
        }
    elif envelope == "minimo":
        minima: dict[str, Any] = {
            "schema_version": resposta["schema_version"],
            "status": "ok",
            "snapshot": {
                "id": sid,
                "revision": revisao,
                "status": snapshot.get("status"),
                "finalizado_em": snapshot.get("finalizado_em"),
                "cobertura_percentual": snapshot.get("cobertura_percentual"),
            },
            "consulta": {
                "ordenacao": resposta["consulta"]["ordenacao"],
            },
            "paginacao": resposta["paginacao"],
            "registros": resposta["registros"],
            "avisos_criticos": [],
        }
        if formato == "compacto":
            minima["contrato"] = resposta["contrato"]
            minima["dicionarios"] = resposta["dicionarios"]
        if incluir_facetas and "facetas" in resposta:
            minima["facetas"] = resposta["facetas"]
        resposta = minima
    fim_total = time.perf_counter()
    observabilidade_acervo.registrar(
        {
            "total_ms": (fim_total - inicio_total) * 1000,
            "snapshot_ms": (fim_snapshot - inicio_total) * 1000,
            "sqlite_ms": (fim_sqlite - inicio_sqlite) * 1000,
            "filtros_ms": (fim_filtros - fim_sqlite) * 1000,
            "projecao_ms": (fim_projecao - fim_filtros) * 1000,
            "montagem_resposta_ms": (fim_total - fim_projecao) * 1000,
            "itens_filtrados": total_filtrados,
            "itens_retornados": len(resposta.get("registros") or []),
            "payload_json_bytes": len(
                json.dumps(
                    resposta,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            ),
            "not_modified": False,
        }
    )
    return resposta


def _iterar_itens_snapshot(
    snapshot_id: str,
) -> Iterable[dict[str, Any]]:
    """Percorre ocorrências com fetchmany, mantendo memória limitada."""
    con = _conectar()
    try:
        cursor = con.execute(
            """
            SELECT chave_ocorrencia, grupo, tarefa, id_task_instance,
                   id_task_instance_proximo, id_processo, numero_processo,
                   classe_judicial, id_orgao_julgador, orgao_julgador,
                   assunto_principal, polo_ativo, polo_passivo, cargo_judicial,
                   data_chegada_epoch, data_chegada_iso,
                   ultimo_movimento_epoch, ultimo_movimento_iso,
                   descricao_ultimo_movimento, sigiloso, prioridade,
                   conferido, morador_de_rua, etiquetas_json,
                   metadados_origem_json, reaproveitado_de_snapshot
              FROM ocorrencias
             WHERE snapshot_id=?
             ORDER BY data_chegada_epoch, tarefa, chave_ocorrencia
            """,
            (snapshot_id,),
        )
        while lote := cursor.fetchmany(500):
            for row in lote:
                item = dict(row)
                item["etiquetas"] = json.loads(item.pop("etiquetas_json") or "[]")
                item["metadados_origem"] = json.loads(
                    item.pop("metadados_origem_json") or "{}"
                )
                yield item
    finally:
        con.close()


def _fonte_exportacao(
    snapshot_id: str,
    filtros: dict[str, Any],
    modo: str,
) -> tuple[dict[str, Any], Iterable[dict[str, Any]], str]:
    """Seleciona cursor direto para o caso comum e fallback exato filtrado."""
    chaves_sem_filtro = {"grau", "persona"}
    filtros_ativos = {
        chave: valor
        for chave, valor in filtros.items()
        if (
            chave not in chaves_sem_filtro
            and valor not in (None, "")
            and not (chave == "ordenar_por" and valor == "data_chegada")
            and not (chave == "direcao" and valor == "asc")
        )
    }
    if modo == "compacto" and not filtros_ativos:
        snapshot = obter_snapshot(
            snapshot_id=snapshot_id or None,
            grau=filtros.get("grau"),
            persona=filtros.get("persona"),
            incluir_caixas=False,
        )
        if "snapshot_id" not in snapshot:
            raise ValueError(snapshot.get("erro") or "Snapshot não encontrado")
        sid = snapshot["snapshot_id"]
        referencia = (
            snapshot.get("finalizado_em") or snapshot.get("iniciado_em") or _agora_iso()
        )
        dicionarios, indices = _dicionarios_compactos(_iterar_itens_snapshot(sid))
        campos = ORDEM_CAMPOS_COMPACTOS + CAMPOS_COMPACTOS_OPCIONAIS
        registros = (
            _projetar_compacto(item, campos, indices, referencia)
            for item in _iterar_itens_snapshot(sid)
        )
        return (
            {"snapshot": {"id": sid}, "dicionarios": dicionarios},
            registros,
            "cursor_sqlite",
        )

    paginas = iter(_paginas_exportacao(snapshot_id, filtros, modo))
    primeira = next(paginas)

    def iterar_paginado() -> Iterable[dict[str, Any]]:
        yield from primeira["registros"]
        for resposta in paginas:
            yield from resposta["registros"]

    return primeira, iterar_paginado(), "consulta_paginada"


def _pasta_exports() -> Path:
    base = Path(os.environ.get("PJE_STORAGE_DIR", "/var/lib/pjepa-mcp/storage"))
    pasta = base / "exports"
    pasta.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        pasta.chmod(0o700)
    except OSError:
        pass
    return pasta


def _limpar_exports(ttl_dias: int = 7, limite_arquivos: int = 50) -> None:
    pasta = _pasta_exports().resolve()
    agora = datetime.now(timezone.utc).timestamp()
    arquivos = sorted(
        (
            arquivo
            for arquivo in pasta.iterdir()
            if arquivo.is_file()
            and arquivo.name.startswith("acervo_")
            and arquivo.suffix == ".enc"
        ),
        key=lambda arquivo: arquivo.stat().st_mtime,
        reverse=True,
    )
    for indice, arquivo in enumerate(arquivos):
        expirou = agora - arquivo.stat().st_mtime > ttl_dias * 86400
        excedeu_limite = indice >= limite_arquivos
        if (expirou or excedeu_limite) and arquivo.parent.resolve() == pasta:
            arquivo.unlink(missing_ok=True)
            match = re.search(r"_([0-9a-f]{32})\.", arquivo.name)
            if match:
                (_pasta_exports() / f".acervo_{match.group(1)}.meta.json").unlink(
                    missing_ok=True
                )


def caminho_exportacao(export_id: str) -> Path:
    """Resolve um export por ID opaco, sem aceitar caminho do consumidor."""
    if not re.fullmatch(r"[0-9a-f]{32}", str(export_id)):
        raise ValueError("ID de export inválido")
    candidatos = list(_pasta_exports().glob(f"acervo_*_{export_id}.*.enc"))
    if len(candidatos) != 1:
        raise FileNotFoundError(f"Export não encontrado: {export_id}")
    caminho = candidatos[0]
    idade = datetime.now(timezone.utc).timestamp() - caminho.stat().st_mtime
    if idade > 7 * 86400:
        caminho.unlink(missing_ok=True)
        (_pasta_exports() / f".acervo_{export_id}.meta.json").unlink(missing_ok=True)
        raise FileNotFoundError(f"Export expirado: {export_id}")
    return caminho


def _metadata_exportacao(export_id: str) -> dict[str, Any]:
    metadata_path = _pasta_exports() / f".acervo_{export_id}.meta.json"
    try:
        envelope = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PermissionError("metadados seguros do export indisponíveis") from exc
    payload = envelope.get("payload")
    signature = str(envelope.get("signature") or "")
    if not isinstance(payload, dict) or not signature:
        raise PermissionError("metadados seguros do export inválidos")
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if not hmac.compare_digest(
        signature,
        auditoria_processual.CryptoBox().reference(canonical),
    ):
        raise PermissionError("integridade dos metadados do export inválida")
    return payload


def ler_exportacao(export_id: str, autorizacao_ref: str) -> bytes:
    if not str(autorizacao_ref or "").strip():
        raise PermissionError("autorização explícita é obrigatória")
    caminho = caminho_exportacao(export_id)
    metadata = _metadata_exportacao(export_id)
    crypto = auditoria_processual.CryptoBox()
    if not hmac.compare_digest(
        str(metadata.get("authorisation_ref_hmac") or ""),
        crypto.reference(str(autorizacao_ref)),
    ):
        raise PermissionError("autorização não corresponde ao export")
    expires_at = datetime.fromisoformat(str(metadata.get("expires_at") or ""))
    if expires_at <= datetime.now(timezone.utc):
        caminho.unlink(missing_ok=True)
        (_pasta_exports() / f".acervo_{export_id}.meta.json").unlink(missing_ok=True)
        raise FileNotFoundError(f"Export expirado: {export_id}")
    return crypto.decrypt_file(caminho, aad=f"acervo-export:{export_id}")


def _sha256_arquivo(caminho: Path) -> str:
    digest = hashlib.sha256()
    with caminho.open("rb") as stream:
        while bloco := stream.read(1024 * 1024):
            digest.update(bloco)
    return digest.hexdigest()


def _parametros_consulta_exportacao(
    snapshot_id: str,
    filtros: dict[str, Any],
    formato: str,
    pagina: int,
) -> dict[str, Any]:
    permitidos = {
        "grau",
        "persona",
        "tarefa",
        "termo",
        "filtro_classe",
        "filtro_assunto",
        "filtro_parte",
        "filtro_orgao",
        "filtro_etiqueta",
        "sigiloso",
        "prioridade",
        "conferido",
        "morador_de_rua",
        "data_chegada_de",
        "data_chegada_ate",
        "dias_na_tarefa_min",
        "dias_na_tarefa_max",
        "ordenar_por",
        "direcao",
    }
    parametros = {
        chave: valor for chave, valor in filtros.items() if chave in permitidos
    }
    parametros.update(
        {
            "snapshot_id": snapshot_id or None,
            "pagina": pagina,
            "itens_por_pagina": 5000 if formato == "compacto" else 500,
            "incluir_facetas": pagina == 1,
            "incluir_campos_extras": formato == "completo",
            "formato": formato,
        }
    )
    if formato == "compacto":
        parametros["campos"] = ",".join(
            ORDEM_CAMPOS_COMPACTOS + CAMPOS_COMPACTOS_OPCIONAIS
        )
    return parametros


def _paginas_exportacao(
    snapshot_id: str,
    filtros: dict[str, Any],
    formato: str,
) -> Iterable[dict[str, Any]]:
    pagina = 1
    while True:
        resposta = consultar_acervo_estruturado(
            **_parametros_consulta_exportacao(snapshot_id, filtros, formato, pagina)
        )
        if "snapshot" not in resposta:
            raise ValueError(resposta.get("erro") or "Snapshot não encontrado")
        yield resposta
        if not resposta["paginacao"]["tem_proxima"]:
            break
        pagina += 1


def _registro_csv_compacto(
    registro: dict[str, Any],
    dicionarios: dict[str, list[str]],
) -> dict[str, Any]:
    linha = dict(registro)
    for campo, nome_dicionario in (
        ("tarefa", "tarefas"),
        ("classe", "classes"),
        ("assunto", "assuntos"),
    ):
        indice = linha.get(campo)
        linha[campo] = (
            dicionarios[nome_dicionario][indice] if isinstance(indice, int) else indice
        )
    return linha


def exportar_acervo(
    formato_arquivo: str = "ndjson",
    modo: str = "compacto",
    snapshot_id: str = "",
    autorizacao_ref: str = "",
    **filtros: Any,
) -> dict[str, Any]:
    """Materializa um acervo filtrado e devolve somente o manifesto."""
    retention_policy.require_legacy_persistence(
        "a exportação de acervo com retenção em disco"
    )
    formato_arquivo = str(formato_arquivo).strip().casefold()
    modo = str(modo).strip().casefold()
    if formato_arquivo not in {"ndjson", "csv", "json", "sqlite"}:
        raise ValueError("formato_arquivo deve ser ndjson, csv, json ou sqlite")
    if modo not in {"compacto", "completo"}:
        raise ValueError("modo deve ser 'compacto' ou 'completo'")
    if not str(autorizacao_ref or "").strip():
        raise PermissionError("autorização explícita é obrigatória para exportar")
    if formato_arquivo == "csv":
        modo = "compacto"

    _limpar_exports()
    export_id = uuid.uuid4().hex
    primeira, fonte_registros, motor_leitura = _fonte_exportacao(
        snapshot_id, filtros, modo
    )
    registros = 0
    contem_sigilosos = False
    extensao = formato_arquivo
    nome = f"acervo_pendente_{modo}_{export_id}.{extensao}.gz"
    destino = _pasta_exports() / nome

    if formato_arquivo == "sqlite":
        temporario = _pasta_exports() / f".{export_id}.sqlite"
        con = sqlite3.connect(temporario)
        try:
            con.execute(
                """
                CREATE TABLE acervo (
                    ordem INTEGER PRIMARY KEY,
                    cnj TEXT, tarefa TEXT, classe TEXT, assunto TEXT,
                    partes TEXT, chegada TEXT, dias INTEGER, dias_mov INTEGER,
                    flags INTEGER, etiquetas TEXT, ocorrencia_chave TEXT,
                    orgao TEXT, registro_json TEXT NOT NULL
                )
                """
            )
            for registro in fonte_registros:
                registros += 1
                linha = (
                    _registro_csv_compacto(registro, primeira.get("dicionarios", {}))
                    if modo == "compacto"
                    else {}
                )
                con.execute(
                    """
                    INSERT INTO acervo VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        registros,
                        linha.get("cnj"),
                        linha.get("tarefa"),
                        linha.get("classe"),
                        linha.get("assunto"),
                        linha.get("partes"),
                        linha.get("chegada"),
                        linha.get("dias"),
                        linha.get("dias_mov"),
                        linha.get("flags"),
                        linha.get("etiquetas"),
                        linha.get("ocorrencia_chave"),
                        linha.get("orgao"),
                        json.dumps(registro, ensure_ascii=False),
                    ),
                )
                indicadores = registro.get("indicadores", {})
                contem_sigilosos = contem_sigilosos or bool(
                    indicadores.get("sigiloso")
                    if modo == "completo"
                    else registro.get("flags", 0) & 1
                )
            con.commit()
        finally:
            con.close()
        with temporario.open("rb") as origem, gzip.open(destino, "wb") as fh:
            while bloco := origem.read(1024 * 1024):
                fh.write(bloco)
        temporario.unlink(missing_ok=True)
    else:
        with gzip.open(destino, "wt", encoding="utf-8", newline="") as fh:
            escritor = None
            primeiro_json = True
            if formato_arquivo == "json":
                fh.write("[")
            dicionarios = primeira.get("dicionarios", {})
            for registro in fonte_registros:
                registros += 1
                indicadores = registro.get("indicadores", {})
                contem_sigilosos = contem_sigilosos or bool(
                    indicadores.get("sigiloso")
                    if modo == "completo"
                    else registro.get("flags", 0) & 1
                )
                if formato_arquivo == "csv":
                    linha = _registro_csv_compacto(registro, dicionarios)
                    if escritor is None:
                        fh.write("\ufeff")
                        escritor = csv.DictWriter(
                            fh,
                            fieldnames=list(linha),
                            delimiter=";",
                            extrasaction="ignore",
                        )
                        escritor.writeheader()
                    escritor.writerow(linha)
                elif formato_arquivo == "json":
                    if not primeiro_json:
                        fh.write(",")
                    fh.write(json.dumps(registro, ensure_ascii=False))
                    primeiro_json = False
                else:
                    fh.write(json.dumps(registro, ensure_ascii=False))
                    fh.write("\n")
            if formato_arquivo == "json":
                fh.write("]")

    sid = primeira["snapshot"]["id"]
    plaintext = _pasta_exports() / (
        f"acervo_{sid[:8]}_{modo}_{export_id}.{extensao}.gz"
    )
    destino.rename(plaintext)
    plaintext.chmod(0o600)
    final = plaintext.with_suffix(plaintext.suffix + ".enc")
    crypto = auditoria_processual.CryptoBox()
    try:
        crypto.encrypt_file(
            plaintext,
            final,
            aad=f"acervo-export:{export_id}",
        )
    finally:
        plaintext.unlink(missing_ok=True)
    sha256 = _sha256_arquivo(final)
    expira_em = datetime.now(timezone.utc) + timedelta(days=7)
    metadata_payload = {
        "schema_version": "pje.acervo-export/v2",
        "export_id": export_id,
        "file_name": final.name,
        "expires_at": expira_em.isoformat(),
        "authorisation_ref_hmac": crypto.reference(str(autorizacao_ref)),
    }
    canonical = json.dumps(
        metadata_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    metadata = {
        "payload": metadata_payload,
        "signature": crypto.reference(canonical),
    }
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".acervo_{export_id}.",
        dir=_pasta_exports(),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(metadata, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(
            temporary,
            _pasta_exports() / f".acervo_{export_id}.meta.json",
        )
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {
        "export_id": export_id,
        "arquivo": str(final),
        "read_action": "ler_export_acervo",
        "formato": formato_arquivo,
        "modo": modo,
        "compressao": "gzip",
        "registros": registros,
        "bytes": final.stat().st_size,
        "sha256": sha256,
        "snapshot_id": sid,
        "expira_em": expira_em.isoformat(),
        "dicionarios": primeira.get("dicionarios", {}),
        "contem_sigilosos": contem_sigilosos,
        "confidencial": contem_sigilosos,
        "encrypted_at_rest": True,
        "motor_leitura": motor_leitura,
    }


def _metricas_grupo(registros: list[dict[str, Any]]) -> dict[str, int]:
    dias = sorted(
        int(registro["dias"])
        for registro in registros
        if registro.get("dias") is not None
    )
    n = len(registros)
    return {
        "quantidade": n,
        "mediana_dias": dias[(len(dias) - 1) // 2] if dias else 0,
        "p90_dias": (dias[max(0, math.ceil(len(dias) * 0.9) - 1)] if dias else 0),
        "max_dias": max(dias) if dias else 0,
        "prioritarios": sum(
            bool(registro.get("flags", 0) & 2) for registro in registros
        ),
        "sigilosos": sum(bool(registro.get("flags", 0) & 1) for registro in registros),
        "maiores_180": sum(dia > 180 for dia in dias),
        "maiores_365": sum(dia > 365 for dia in dias),
    }


def estatisticas_acervo(
    dimensao: str = "tarefa",
    metricas: str = (
        "quantidade,mediana_dias,p90_dias,max_dias,prioritarios,"
        "sigilosos,maiores_180,maiores_365"
    ),
    snapshot_id: str = "",
    **filtros: Any,
) -> dict[str, Any]:
    """Calcula agregados do acervo sem transportar registros ao cliente."""
    dimensao = str(dimensao).strip().casefold()
    dimensoes = {
        "tarefa": ("tarefa", "tarefas"),
        "classe": ("classe", "classes"),
        "assunto": ("assunto", "assuntos"),
        "orgao": ("orgao", None),
        "mes_chegada": ("chegada", None),
    }
    if dimensao not in dimensoes:
        raise ValueError(
            "dimensao deve ser tarefa, classe, assunto, orgao ou mes_chegada"
        )
    metricas_validas = set(_metricas_grupo([]))
    selecionadas = [
        metrica.strip() for metrica in metricas.split(",") if metrica.strip()
    ]
    invalidas = sorted(set(selecionadas) - metricas_validas)
    if invalidas:
        raise ValueError(
            f"Métricas inválidas: {invalidas}. Válidas: {sorted(metricas_validas)}"
        )

    todos: list[dict[str, Any]] = []
    primeira, fonte_registros, motor_leitura = _fonte_exportacao(
        snapshot_id, filtros, "compacto"
    )
    campo, nome_dicionario = dimensoes[dimensao]
    for registro in fonte_registros:
        valor = registro.get(campo)
        if nome_dicionario and isinstance(valor, int):
            valor = primeira["dicionarios"][nome_dicionario][valor]
        if dimensao == "mes_chegada":
            valor = str(valor)[:7] if valor else "sem_data"
        registro = dict(registro)
        registro["_dimensao"] = valor or "—"
        todos.append(registro)

    grupos: dict[str, list[dict[str, Any]]] = {}
    for registro in todos:
        grupos.setdefault(str(registro["_dimensao"]), []).append(registro)
    linhas = []
    for valor, registros_grupo in grupos.items():
        calculadas = _metricas_grupo(registros_grupo)
        linhas.append(
            {
                "valor": valor,
                **{metrica: calculadas[metrica] for metrica in selecionadas},
            }
        )
    linhas.sort(key=lambda linha: (-linha.get("quantidade", 0), linha["valor"]))
    global_calculado = _metricas_grupo(todos)
    return {
        "schema_version": "pje.acervo-tarefas/estatisticas-v1",
        "snapshot_id": primeira["snapshot"]["id"],
        "dimensao": dimensao,
        "metricas": selecionadas,
        "global": {metrica: global_calculado[metrica] for metrica in selecionadas},
        "linhas": linhas,
        "total_linhas": len(linhas),
        "filtros_aplicados": filtros,
        "motor_leitura": motor_leitura,
    }


def carregar_para_analise(
    grau: str, persona: str, snapshot_id: str | None = None
) -> list[dict[str, Any]]:
    """Carrega o snapshot para o motor analítico legado, sem perder ocorrências."""
    snapshot = obter_snapshot(
        snapshot_id=snapshot_id,
        grau=grau,
        persona=persona,
        incluir_caixas=False,
    )
    if "snapshot_id" not in snapshot:
        return []
    with _banco() as con:
        rows = con.execute(
            """
            SELECT tarefa, numero_processo, classe_judicial, orgao_julgador,
                   assunto_principal, polo_ativo, polo_passivo,
                   data_chegada_iso, ultimo_movimento_iso,
                   descricao_ultimo_movimento, sigiloso, prioridade,
                   etiquetas_json, metadados_origem_json
              FROM ocorrencias WHERE snapshot_id=?
            """,
            (snapshot["snapshot_id"],),
        ).fetchall()
    resultado = []
    for row in rows:
        item = dict(row)
        origem = json.loads(item.pop("metadados_origem_json"))
        resultado.append(
            {
                "numero_processo": item["numero_processo"] or "",
                "classe": item["classe_judicial"] or "",
                "tarefa": item["tarefa"] or "",
                "orgao": item["orgao_julgador"] or "",
                "assunto": item["assunto_principal"] or "",
                "partes": " x ".join(
                    p for p in (item["polo_ativo"], item["polo_passivo"]) if p
                ),
                "data_entrada": item["data_chegada_iso"] or "",
                "ultimo_movimento": (
                    item["descricao_ultimo_movimento"]
                    or item["ultimo_movimento_iso"]
                    or ""
                ),
                "sigiloso": bool(item["sigiloso"]),
                "prioridade_pje": bool(item["prioridade"]),
                "etiquetas": json.loads(item["etiquetas_json"] or "[]"),
                "metadados_origem": origem,
            }
        )
    return resultado


# Fontes de classificação que provam a reversibilidade. Espelha
# politica_atuacao.FONTES_REVERSIBILIDADE_CONFIAVEIS sem criar import cíclico.
_FONTES_TRANSICAO_CONFIAVEIS = ("confirmacao_humana", "prova_empirica")


def salvar_transicao(
    perfil_id: str,
    tarefa: str,
    destino_id: str,
    destino_nome: str,
    reversivel: str,
    fonte: str = "heuristica_tipo_elemento",
) -> None:
    """Salva ou atualiza uma transição descoberta no banco local.

    Uma redescoberta heurística nunca rebaixa uma classificação cuja fonte é
    confiável (confirmação humana ou prova empírica): o registro provado é
    preservado e apenas o nome exibido é atualizado.
    """
    agora = datetime.now(timezone.utc).isoformat() + "Z"
    with _banco() as con:
        existente = con.execute(
            """
            SELECT fonte FROM transicoes
             WHERE perfil_id=? AND tarefa=? AND destino_id=?
            """,
            (perfil_id, tarefa, destino_id),
        ).fetchone()
        if (
            existente
            and existente["fonte"] in _FONTES_TRANSICAO_CONFIAVEIS
            and fonte not in _FONTES_TRANSICAO_CONFIAVEIS
        ):
            con.execute(
                """
                UPDATE transicoes SET destino_nome=?, atualizado_em=?
                 WHERE perfil_id=? AND tarefa=? AND destino_id=?
                """,
                (destino_nome, agora, perfil_id, tarefa, destino_id),
            )
        else:
            con.execute(
                """
                INSERT OR REPLACE INTO transicoes (
                    perfil_id, tarefa, destino_id, destino_nome,
                    reversivel, fonte, atualizado_em
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    perfil_id,
                    tarefa,
                    destino_id,
                    destino_nome,
                    reversivel,
                    fonte,
                    agora,
                ),
            )
        con.commit()


def confirmar_transicao_reversivel(
    perfil_id: str,
    tarefa: str,
    destino_id: str,
    evidencia: str,
    fonte: str = "confirmacao_humana",
) -> dict[str, Any]:
    """Promove uma transição a REVERSIVEL_ANTES_COMMIT com prova registrada.

    Exige que a transição já tenha sido descoberta pelo mapeamento e que uma
    evidência textual não vazia acompanhe a promoção. Transições descobertas
    como COMMIT_NA_SELECAO não são promovíveis: o botão que já comete não
    vira reversível por confirmação.
    """
    if fonte not in _FONTES_TRANSICAO_CONFIAVEIS:
        raise ValueError(f"Fonte de confirmação inválida: {fonte}")
    evidencia = (evidencia or "").strip()
    if not evidencia:
        raise ValueError(
            "Evidência obrigatória: descreva como a reversibilidade foi "
            "verificada (tela, item usado, resultado da reconciliação)."
        )
    agora = datetime.now(timezone.utc).isoformat() + "Z"
    with _banco() as con:
        row = con.execute(
            """
            SELECT reversivel, fonte FROM transicoes
             WHERE perfil_id=? AND tarefa=? AND destino_id=?
            """,
            (perfil_id, tarefa, destino_id),
        ).fetchone()
        if not row:
            raise ValueError(
                "Transição não mapeada para este perfil/tarefa; rode "
                "mapear_transicoes_tarefa antes de confirmar."
            )
        if row["reversivel"] == "COMMIT_NA_SELECAO":
            raise ValueError(
                "Transição classificada como COMMIT_NA_SELECAO não é "
                "promovível: o próprio acionamento já comete o ato."
            )
        con.execute(
            """
            UPDATE transicoes
               SET reversivel='REVERSIVEL_ANTES_COMMIT', fonte=?,
                   evidencia=?, atualizado_em=?
             WHERE perfil_id=? AND tarefa=? AND destino_id=?
            """,
            (fonte, evidencia, agora, perfil_id, tarefa, destino_id),
        )
        con.commit()
    return {
        "perfil_id": perfil_id,
        "tarefa": tarefa,
        "destino_id": destino_id,
        "reversivel": "REVERSIVEL_ANTES_COMMIT",
        "fonte": fonte,
        "evidencia": evidencia,
        "atualizado_em": agora,
    }


def obter_transicoes(perfil_id: str, tarefa: str | None = None) -> list[dict[str, Any]]:
    """Obtém as transições de tarefas salvas para um perfil."""
    with _banco() as con:
        if tarefa:
            rows = con.execute(
                """
                SELECT perfil_id, tarefa, destino_id, destino_nome,
                       reversivel, fonte, evidencia, atualizado_em
                  FROM transicoes
                 WHERE perfil_id=? AND tarefa=?
                """,
                (perfil_id, tarefa)
            ).fetchall()
            if not rows:
                import re
                import unicodedata

                def _norm(s: str) -> str:
                    t = unicodedata.normalize("NFKD", str(s or ""))
                    t = "".join(c for c in t if not unicodedata.combining(c))
                    return re.sub(r"\s+", " ", t).strip().casefold()

                def _sem_plural(s: str) -> str:
                    # Plural pode estar no meio ("providências a adotar"),
                    # então o corte do "s" é por palavra, não no fim da frase.
                    return " ".join(w.removesuffix("s") for w in s.split())

                alvo = _norm(tarefa)
                alvo_base = _sem_plural(alvo)
                all_rows = con.execute(
                    """
                    SELECT perfil_id, tarefa, destino_id, destino_nome,
                           reversivel, fonte, evidencia, atualizado_em
                      FROM transicoes
                     WHERE perfil_id=?
                    """,
                    (perfil_id,)
                ).fetchall()
                rows = [
                    r
                    for r in all_rows
                    if _norm(r["tarefa"]) == alvo
                    or _sem_plural(_norm(r["tarefa"])) == alvo_base
                ]
        else:
            rows = con.execute(
                """
                SELECT perfil_id, tarefa, destino_id, destino_nome,
                       reversivel, fonte, evidencia, atualizado_em
                  FROM transicoes
                 WHERE perfil_id=?
                """,
                (perfil_id,)
            ).fetchall()
    return [dict(row) for row in rows]


def obter_transicoes_para_perfil(
    rotulo: str,
    persona: str,
    grau: str,
    tarefa: str | None = None,
) -> list[dict[str, Any]]:
    """Transições salvas para um perfil resolvido OFFLINE pelo rótulo.

    O mesmo rótulo pode ter mais de um perfil_id registrado em ``perfis``
    (revalidações), e mapeamentos feitos sem contexto fixado gravam sob
    "default_perfil"; a consulta une todos e deduplica por destino.
    """

    def _norm(s: str) -> str:
        t = unicodedata.normalize("NFKD", str(s or ""))
        t = "".join(c for c in t if not unicodedata.combining(c))
        return re.sub(r"\s+", " ", t).strip().casefold()

    alvo_rotulo = _norm(rotulo)
    perfil_ids: list[str] = []
    with _banco() as con:
        for row in con.execute(
            "SELECT perfil_id, rotulo FROM perfis WHERE persona=? AND grau=?",
            (persona, grau),
        ):
            if not alvo_rotulo or _norm(row["rotulo"]) == alvo_rotulo:
                perfil_ids.append(row["perfil_id"])
    perfil_ids.append("default_perfil")

    vistos: dict[tuple[str, str], dict[str, Any]] = {}
    for pid in perfil_ids:
        for t in obter_transicoes(pid, tarefa):
            chave = (_norm(t.get("tarefa")), str(t.get("destino_id")))
            anterior = vistos.get(chave)
            if anterior is None or str(t.get("atualizado_em") or "") > str(
                anterior.get("atualizado_em") or ""
            ):
                vistos[chave] = t
    return list(vistos.values())


def simular_lote(
    snapshot_id: str,
    processos_ids: list[str],  # CNJs ou ids_processo
    ttl_minutos: int = 120
) -> dict[str, Any]:
    """Valida a elegibilidade de um lote offline, gerando a decisão por item e lote_hash."""
    with _banco() as con:
        row_snap = con.execute(
            "SELECT iniciado_em, status FROM snapshots WHERE snapshot_id=?",
            (snapshot_id,)
        ).fetchone()
        
    if not row_snap:
        raise ValueError(f"Snapshot não encontrado: {snapshot_id}")
        
    iniciado_em = datetime.fromisoformat(row_snap["iniciado_em"].replace("Z", "+00:00"))
    if datetime.now(timezone.utc) - iniciado_em > timedelta(minutes=ttl_minutos):
        return {
            "valido": False,
            "motivo_rejeicao": f"Snapshot obsoleto. Idade superior a {ttl_minutos} minutos (criado em {row_snap['iniciado_em']}). Exige ressincronização."
        }
        
    placeholders = ",".join("?" for _ in processos_ids)
    query = f"""
        SELECT chave_ocorrencia, numero_processo, id_processo, id_task_instance,
               podeMovimentarEmLote, podeMinutarEmLote, podeIntimarEmLote,
               podeDesignarAudienciaEmLote, podeDesignarPericiaEmLote, podeRenajudEmLote,
               sigiloso, prioridade
          FROM ocorrencias
         WHERE snapshot_id=? AND (numero_processo IN ({placeholders}) OR id_processo IN ({placeholders}))
    """
    params = [snapshot_id] + processos_ids + processos_ids
    
    with _banco() as con:
        rows = con.execute(query, params).fetchall()
        
    itens_decisao = []
    ids_incluidos = []
    
    encontrados_cnjs = set()
    encontrados_ids = set()
    
    for row in rows:
        d = dict(row)
        cnj = d["numero_processo"]
        pid = d["id_processo"]
        encontrados_cnjs.add(cnj)
        encontrados_ids.add(pid)
        
        id_task = d["id_task_instance"]
        pode_mov = d["podeMovimentarEmLote"]
        sigilo = d["sigiloso"]
        prio = d["prioridade"]
        
        status = "INCLUIDO"
        motivo = "Processo elegível para movimentação em lote."
        
        if not id_task:
            status = "EXCLUIDO"
            motivo = "excluído por id_task_instance ausente"
        elif pode_mov != 1:
            status = "EXCLUIDO"
            motivo = "excluído por podeMovimentarEmLote=false"
        elif sigilo == 1:
            status = "EXCLUIDO"
            motivo = "excluído por sigiloso"
        elif prio == 1:
            status = "EXCLUIDO"
            motivo = "excluído por prioridade que exige conferência individual"
            
        if status == "INCLUIDO":
            ids_incluidos.append(id_task)
            
        itens_decisao.append({
            "numero_processo": cnj,
            "id_processo": pid,
            "id_task_instance": id_task,
            "status": status,
            "motivo": motivo
        })
        
    for p in processos_ids:
        if p not in encontrados_cnjs and p not in encontrados_ids:
            itens_decisao.append({
                "numero_processo": p if len(p) > 10 else None,
                "id_processo": p if len(p) <= 10 else None,
                "status": "EXCLUIDO",
                "motivo": "excluído por não existir no snapshot"
            })
            
    lote_hash = ""
    if ids_incluidos:
        ids_incluidos.sort()
        lote_hash = hashlib.sha256(",".join(ids_incluidos).encode()).hexdigest()
        
    return {
        "valido": True,
        "snapshot_id": snapshot_id,
        "lote_hash": lote_hash,
        "total_itens": len(processos_ids),
        "total_incluidos": len(ids_incluidos),
        "total_excluidos": len(processos_ids) - len(ids_incluidos),
        "itens": itens_decisao
    }
