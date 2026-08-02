"""Identidade e contexto de perfis funcionais internos do PJe.

Um usuário interno não é identificado apenas pela persona ``servidor``:
o escopo efetivo é a união de localização/unidade e papel.  Este módulo
mantém esse escopo por chamada MCP usando ``ContextVar`` e gera um
identificador opaco estável sem expor o rótulo na chave de sessão/cache.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import os
import re
import secrets
import unicodedata
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any

PERSONAS_INTERNAS = frozenset({"servidor", "magistrado"})
PERSONAS_VALIDAS = frozenset(
    {"servidor", "magistrado", "advogado", "procurador"}
)

_CONTEXTO: contextvars.ContextVar[dict[str, str] | None] = (
    contextvars.ContextVar("pje_perfil_contexto", default=None)
)
_CATALOGO_PJE: dict[tuple[str, str, str], PerfilFuncional] = {}
_CATALOGO_ROTULOS: dict[tuple[str, str, str], str] = {}


class EstadoLocalizacao(str, Enum):
    PRESENTE = "PRESENTE"
    NAO_APLICAVEL = "NAO_APLICAVEL"
    AUSENTE_NAO_VERIFICADA = "AUSENTE_NAO_VERIFICADA"


@dataclass(frozen=True)
class PerfilFuncional:
    """Identidade funcional fornecida pelo PJe, separada do rótulo visual."""

    pje_id: str
    rotulo: str
    unidade_id: str = ""
    localizacao_id: str = ""
    papel_id: str = ""
    unidade: str = ""
    localizacao: str = ""
    papel: str = ""
    persona: str = "servidor"
    grau: str = "1g"
    fonte_id: str = ""


@dataclass(frozen=True)
class SessaoContextoFixado:
    """Identidade comprovada após a seleção por localizador não confiável."""

    contexto_validado_id: str
    usuario_id: str
    persona: str
    grau: str
    unidade: str
    localizacao: str
    localizacao_status: str
    papel: str
    titulo: str
    rota: str
    localizador_nao_confiavel: str
    vinculo_status: str = "contexto_fixado"


class PerfilObrigatorioError(RuntimeError):
    """A operação interna foi chamada sem um perfil funcional inequívoco."""


def normalizar_texto(valor: str) -> str:
    base = unicodedata.normalize("NFKD", str(valor or "").casefold())
    sem_acento = "".join(c for c in base if not unicodedata.combining(c))
    return " ".join(sem_acento.split())


def normalizar_persona(persona: str) -> str:
    """Normaliza a persona sem converter silenciosamente servidor em advogado."""
    valor = normalizar_texto(persona or "servidor")
    aliases = {
        "usuario interno": "servidor",
        "usuário interno": "servidor",
        "diretor": "servidor",
        "diretor de secretaria": "servidor",
        "servidora": "servidor",
        "juiz": "magistrado",
        "juiza": "magistrado",
        "juíza": "magistrado",
        "desembargador": "magistrado",
        "desembargadora": "magistrado",
        "advogada": "advogado",
        "procuradora": "procurador",
    }
    valor = aliases.get(valor, valor)
    if valor not in PERSONAS_VALIDAS:
        raise ValueError(
            "persona inválida; use servidor, magistrado, advogado ou procurador"
        )
    return valor


def _caminho_chave() -> Path:
    raiz = Path(os.environ.get("PJE_STORAGE_DIR", "/var/lib/pjepa-mcp/storage"))
    return raiz / "inventario" / ".perfil_hmac_key"


def _chave_hmac() -> bytes:
    """Obtém chave local; prefere a credencial cifrada já entregue pelo systemd."""
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY", "").strip()
    if cred_dir:
        caminho_credencial = Path(cred_dir) / "audit_master_key"
        try:
            valor = caminho_credencial.read_bytes().strip()
            if valor:
                return hashlib.sha256(b"pje-perfil-v1\0" + valor).digest()
        except OSError:
            pass

    caminho = _caminho_chave()
    try:
        valor = caminho.read_bytes()
        if len(valor) >= 32:
            return valor
    except OSError:
        pass

    caminho.parent.mkdir(parents=True, exist_ok=True)
    valor = secrets.token_bytes(32)
    try:
        fd = os.open(caminho, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existente = caminho.read_bytes()
        if len(existente) >= 32:
            return existente
        raise RuntimeError("chave HMAC de perfis local está inválida")
    try:
        os.write(fd, valor)
    finally:
        os.close(fd)
    return valor


def decompor_rotulo(rotulo: str) -> dict[str, str]:
    """Separa ``unidade / localização / papel`` sem adivinhar partes ausentes."""
    limpo = re.sub(r"\s+", " ", str(rotulo or "")).strip()
    partes = [parte.strip() for parte in limpo.split("/") if parte.strip()]
    if len(partes) == 1:
        unidade = "Unidade Não Identificada"
        localizacao = "Localização Não Identificada"
        papel = partes[0]
    else:
        unidade = partes[0] if partes else ""
        localizacao = " / ".join(partes[1:-1]) if len(partes) > 2 else ""
        papel = partes[-1] if len(partes) > 1 else ""
    return {
        "rotulo": limpo,
        "unidade": unidade,
        "localizacao": localizacao,
        "papel": papel,
    }


def criar_contexto(persona: str, grau: str, perfil: str) -> dict[str, str] | None:
    persona_normalizada = normalizar_persona(persona)
    if persona_normalizada not in PERSONAS_INTERNAS:
        return None
    dados = decompor_rotulo(perfil)
    if not dados["rotulo"]:
        return None
    material = "\0".join(
        [
            "pje-perfil-provisorio-v1",
            persona_normalizada,
            str(grau),
            normalizar_texto(dados["rotulo"]),
        ]
    ).encode("utf-8")
    dados.update(
        {
            "perfil_id": hmac.new(
                _chave_hmac(), material, hashlib.sha256
            ).hexdigest(),
            "persona": persona_normalizada,
            "grau": str(grau),
            "vinculo_status": "provisorio",
            "pje_id": "",
            "unidade_id": "",
            "localizacao_id": "",
            "papel_id": "",
        }
    )
    catalogado = resolver_perfil_funcional(persona_normalizada, str(grau), perfil)
    if catalogado:
        dados.update(asdict(catalogado))
        dados["vinculo_status"] = "confirmado_pje"
    return dados


def registrar_perfis_funcionais(
    persona: str,
    grau: str,
    perfis: list[PerfilFuncional],
) -> None:
    """Registra apenas identidades reais descobertas no PJe durante a sessão."""
    p = normalizar_persona(persona)
    g = str(grau)
    for perfil in perfis:
        pje_id = str(perfil.pje_id or "").strip()
        rotulo = str(perfil.rotulo or "").strip()
        if not pje_id or not rotulo:
            continue
        chave = (p, g, pje_id)
        _CATALOGO_PJE[chave] = perfil
        _CATALOGO_ROTULOS[(p, g, normalizar_texto(rotulo))] = pje_id


def resolver_perfil_funcional(
    persona: str,
    grau: str,
    referencia: str,
) -> PerfilFuncional | None:
    p = normalizar_persona(persona)
    g = str(grau)
    valor = str(referencia or "").strip()
    if not valor:
        return None
    pje_id = valor.removeprefix("pje_id:")
    direto = _CATALOGO_PJE.get((p, g, pje_id))
    if direto:
        return direto
    id_por_rotulo = _CATALOGO_ROTULOS.get((p, g, normalizar_texto(valor)))
    return _CATALOGO_PJE.get((p, g, id_por_rotulo or ""))


def limpar_catalogo_perfis() -> None:
    """Limpa identidades voláteis; usado no shutdown e em testes."""
    _CATALOGO_PJE.clear()
    _CATALOGO_ROTULOS.clear()


def definir_contexto(persona: str, grau: str, perfil: str) -> dict[str, str] | None:
    contexto = criar_contexto(persona, grau, perfil)
    _CONTEXTO.set(contexto)
    return contexto


def criar_contexto_fixado(
    *,
    usuario_id: str,
    persona: str,
    grau: str,
    unidade: str,
    localizacao: str,
    localizacao_status: str,
    papel: str,
    titulo: str,
    rota: str,
    localizador_nao_confiavel: str,
) -> SessaoContextoFixado:
    """Cria identidade opaca apenas com fatos confirmados após a seleção."""
    valores = {
        "usuario_id": str(usuario_id or "").strip(),
        "persona": normalizar_persona(persona),
        "grau": str(grau or "").strip(),
        "unidade": str(unidade or "").strip(),
        "localizacao": str(localizacao or "").strip(),
        "localizacao_status": str(localizacao_status or "").strip(),
        "papel": str(papel or "").strip(),
        "titulo": str(titulo or "").strip(),
        "rota": str(rota or "").strip(),
    }
    obrigatorios = (
        "usuario_id", "persona", "grau", "unidade", "papel", "titulo", "rota"
    )
    if (
        valores["persona"] not in PERSONAS_INTERNAS
        or not all(valores[campo] for campo in obrigatorios)
    ):
        raise PerfilObrigatorioError(
            "CONTEXTO_DIVERGENTE: identidade interna confirmada está incompleta"
        )
    estado = valores["localizacao_status"]
    if (
        estado not in {item.value for item in EstadoLocalizacao}
        or (estado == EstadoLocalizacao.PRESENTE.value and not valores["localizacao"])
        or (
            estado == EstadoLocalizacao.NAO_APLICAVEL.value
            and valores["localizacao"]
        )
        or estado == EstadoLocalizacao.AUSENTE_NAO_VERIFICADA.value
    ):
        raise PerfilObrigatorioError(
            "CONTEXTO_DIVERGENTE: estado da localização não autoriza fixação"
        )
    material = "\0".join(
        [
            "pje-contexto-fixado-v1",
            valores["usuario_id"],
            valores["persona"],
            valores["grau"],
            normalizar_texto(valores["unidade"]),
            normalizar_texto(valores["localizacao"]),
            estado,
            normalizar_texto(valores["papel"]),
        ]
    ).encode("utf-8")
    return SessaoContextoFixado(
        contexto_validado_id=hmac.new(
            _chave_hmac(), material, hashlib.sha256
        ).hexdigest(),
        localizador_nao_confiavel=str(localizador_nao_confiavel or "").strip(),
        **valores,
    )


def definir_contexto_fixado(sessao: SessaoContextoFixado) -> dict[str, str]:
    contexto = asdict(sessao)
    # Compatibilidade de leitura: perfil_id nunca é derivado do localizador.
    contexto["perfil_id"] = sessao.contexto_validado_id
    contexto["pje_id"] = ""
    # Rótulo de exibição/roteamento (get_cliente casa sessão fixada por ele);
    # não participa de nenhuma identidade — perfil_id segue vindo do HMAC.
    contexto["rotulo"] = sessao.localizador_nao_confiavel
    _CONTEXTO.set(contexto)
    return dict(contexto)


def exigir_contexto_fixado(persona: str) -> dict[str, str]:
    contexto = exigir_contexto(persona)
    if (
        normalizar_persona(persona) in PERSONAS_INTERNAS
        and (
            contexto.get("vinculo_status") != "contexto_fixado"
            or not contexto.get("contexto_validado_id")
        )
    ):
        raise PerfilObrigatorioError(
            "CONTEXTO_NAO_FIXADO: valide uma sessão interna isolada antes "
            "de consultar ou gravar cache"
        )
    return contexto


def contexto_atual() -> dict[str, str] | None:
    contexto = _CONTEXTO.get()
    return dict(contexto) if contexto else None


def exigir_contexto(persona: str) -> dict[str, str]:
    persona_normalizada = normalizar_persona(persona)
    contexto = contexto_atual()
    if persona_normalizada in PERSONAS_INTERNAS and not contexto:
        raise PerfilObrigatorioError(
            "PERFIL_OBRIGATORIO: informe o perfil funcional completo "
            "(unidade / localização / papel). Use "
            "painel_e_prazos_pje(acao='diagnosticar_caixas') para listar "
            "os perfis disponíveis."
        )
    return contexto or {}


def erro_perfil_obrigatorio(persona: str) -> dict[str, Any] | None:
    persona_normalizada = normalizar_persona(persona)
    if persona_normalizada not in PERSONAS_INTERNAS or contexto_atual():
        return None
    return {
        "erro": "perfil funcional obrigatório para usuário interno",
        "codigo": "PERFIL_OBRIGATORIO",
        "persona": persona_normalizada,
        "proxima_acao": "diagnosticar_caixas",
        "dica": (
            "Liste os perfis e repita a operação informando o rótulo completo "
            "no parâmetro 'perfil'."
        ),
    }


def erro_identificador_estavel(persona: str) -> dict[str, Any] | None:
    persona_normalizada = normalizar_persona(persona)
    contexto = contexto_atual()
    if persona_normalizada not in PERSONAS_INTERNAS or not contexto:
        return None
    if contexto.get("pje_id"):
        return None
    return {
        "erro": (
            "o perfil funcional não possui identificador interno estável "
            "confirmado pelo PJe"
        ),
        "codigo": "PERFIL_SEM_IDENTIFICADOR_ESTAVEL",
        "persona": persona_normalizada,
        "proxima_acao": "diagnosticar_caixas",
        "read_only": True,
    }
