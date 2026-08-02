"""Geracao de agenda .ics (RFC 5545) a partir dos expedientes/prazos do PJe.

Modulo puro: nao fala com o PJe nem com o browser. Recebe a lista de
expedientes ja extraida (formato de pje_client.expedientes_pendentes) e
devolve o texto do calendario, para o chamador salvar ou entregar inline.

Por que existe: o painel do PJe mostra prazo, mas nao exporta nada. O
advogado precisa dos prazos no calendario dele (Apple/Google/Outlook), com
alarme. Um .ics resolve isso sem integracao com API de terceiros.

Decisoes de formato:
- Hora "flutuante" (sem TZ e sem VTIMEZONE): o prazo processual e sempre
  lido no horario local do foro. Todo cliente de calendario interpreta
  DTSTART sem sufixo como hora local, que e exatamente o desejado.
- UID estavel derivado de CNJ + data limite: reimportar o mesmo .ics
  ATUALIZA o evento em vez de duplicar.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

PRODID = "-//MCP PJe-TJPA//Agenda de Prazos//PT-BR"

_FORMATOS_DATA = (
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
)


def parse_data_limite(valor: Any) -> tuple[datetime | None, bool]:
    """Converte a data limite do expediente em (datetime, tem_hora).

    O PJe devolve tanto 'dd/mm/aaaa' quanto 'dd/mm/aaaa HH:MM', e as vezes
    com texto em volta. Devolve (None, False) quando nao ha data utilizavel.
    """
    if not isinstance(valor, str):
        return None, False
    texto = valor.strip()
    if not texto:
        return None, False

    for formato in _FORMATOS_DATA:
        try:
            return datetime.strptime(texto, formato), "%H" in formato
        except ValueError:
            continue

    achado = re.search(r"(\d{2}/\d{2}/\d{4})(?:\s+(\d{2}:\d{2}))?", texto)
    if not achado:
        return None, False
    dia, hora = achado.group(1), achado.group(2)
    try:
        if hora:
            return datetime.strptime(f"{dia} {hora}", "%d/%m/%Y %H:%M"), True
        return datetime.strptime(dia, "%d/%m/%Y"), False
    except ValueError:
        return None, False


def escapar(texto: Any) -> str:
    """Escapa um valor de texto conforme RFC 5545 secao 3.3.11."""
    if texto is None:
        return ""
    s = str(texto)
    s = s.replace("\\", "\\\\")
    s = s.replace(";", "\\;")
    s = s.replace(",", "\\,")
    s = s.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    return s


def dobrar(linha: str) -> list[str]:
    """Dobra uma linha em pedacos de ate 75 octetos (RFC 5545 secao 3.1).

    A dobra e por OCTETO, nao por caractere: nomes de partes acentuados
    estouram o limite antes do esperado, e cortar no meio de um caractere
    UTF-8 quebra o parser do cliente de calendario.
    """
    bruto = linha.encode("utf-8")
    if len(bruto) <= 75:
        return [linha]

    pedacos: list[str] = []
    atual = bytearray()
    limite = 75
    for caractere in linha:
        octetos = caractere.encode("utf-8")
        if len(atual) + len(octetos) > limite:
            pedacos.append(atual.decode("utf-8"))
            atual = bytearray()
            limite = 74  # continuacao gasta 1 octeto com o espaco inicial
        atual.extend(octetos)
    if atual:
        pedacos.append(atual.decode("utf-8"))
    return [pedacos[0]] + [" " + p for p in pedacos[1:]]


def _uid(numero_processo: str, inicio: datetime, indice: int) -> str:
    semente = f"{numero_processo}|{inicio.isoformat()}|{indice}"
    digest = hashlib.sha1(semente.encode("utf-8")).hexdigest()[:16]
    return f"{digest}@pje-tjpa.mcp"


def _descricao(exp: dict) -> str:
    campos = (
        ("Processo", exp.get("numero_processo")),
        ("Classe", exp.get("classe")),
        ("Finalidade", exp.get("finalidade")),
        ("Prazo", exp.get("prazo")),
        ("Tipo", exp.get("tipo")),
        ("Orgao", exp.get("orgao")),
        ("Partes", exp.get("partes")),
        ("Assunto", exp.get("assunto")),
        ("Ciencia em", exp.get("ciencia_em")),
        ("Ultimo movimento", exp.get("ultimo_movimento")),
        ("Acao no PJe", exp.get("acao_disponivel")),
    )
    linhas = [f"{rotulo}: {valor}" for rotulo, valor in campos if valor]
    linhas.append("Gerado pelo MCP PJe-TJPA.")
    return "\n".join(linhas)


def gerar_ics(
    expedientes: Iterable[dict],
    *,
    titulo_calendario: str = "Prazos PJe TJPA",
    dias_limite: int | None = None,
    incluir_vencidos: bool = True,
    agora: datetime | None = None,
    max_eventos: int = 500,
) -> dict:
    """Monta o texto do calendario a partir dos expedientes.

    dias_limite: se informado, so entram prazos que vencem em ate N dias.
    incluir_vencidos: mantem no calendario os prazos ja vencidos (default
    True, porque prazo vencido e justamente o que precisa aparecer).
    Devolve dict com 'conteudo_ics', contagens e a lista de ignorados.
    """
    referencia = agora or datetime.now()
    carimbo = referencia.strftime("%Y%m%dT%H%M%SZ")

    eventos: list[str] = []
    incluidos: list[dict] = []
    ignorados: list[dict] = []

    for indice, exp in enumerate(expedientes):
        if not isinstance(exp, dict):
            continue
        inicio, tem_hora = parse_data_limite(exp.get("data_limite"))
        if inicio is None:
            ignorados.append({
                "numero_processo": exp.get("numero_processo"),
                "motivo": "sem data limite reconhecivel",
                "valor_bruto": exp.get("data_limite")
                or exp.get("data_limite_observacao"),
            })
            continue

        dias_restantes = (inicio - referencia).days
        vencido = inicio < referencia
        if vencido and not incluir_vencidos:
            continue
        if dias_limite is not None and dias_restantes > dias_limite:
            continue
        if len(eventos) >= max_eventos:
            ignorados.append({
                "numero_processo": exp.get("numero_processo"),
                "motivo": f"limite de {max_eventos} eventos atingido",
            })
            continue

        numero = exp.get("numero_processo") or "sem-numero"
        classe = exp.get("classe") or ""
        finalidade = exp.get("finalidade") or "manifestacao"
        marcador = "VENCIDO " if vencido else ""
        resumo = f"{marcador}Prazo {finalidade}: {classe} {numero}".strip()

        linhas = ["BEGIN:VEVENT"]
        linhas.append(f"UID:{_uid(numero, inicio, indice)}")
        linhas.append(f"DTSTAMP:{carimbo}")
        if tem_hora:
            linhas.append(f"DTSTART:{inicio.strftime('%Y%m%dT%H%M%S')}")
            linhas.append("DURATION:PT1H")
        else:
            linhas.append(f"DTSTART;VALUE=DATE:{inicio.strftime('%Y%m%d')}")
            fim = inicio + timedelta(days=1)
            linhas.append(f"DTEND;VALUE=DATE:{fim.strftime('%Y%m%d')}")
        linhas.append(f"SUMMARY:{escapar(resumo)}")
        linhas.append(f"DESCRIPTION:{escapar(_descricao(exp))}")
        if exp.get("orgao"):
            linhas.append(f"LOCATION:{escapar(exp['orgao'])}")
        linhas.append("CATEGORIES:PRAZO,PJE,TJPA")
        linhas.append("PRIORITY:1" if dias_restantes <= 3 else "PRIORITY:5")
        linhas.append("STATUS:CONFIRMED")
        linhas.append("TRANSP:OPAQUE")
        for gatilho, rotulo in (("-P1D", "1 dia"), ("-PT2H", "2 horas")):
            linhas.append("BEGIN:VALARM")
            linhas.append("ACTION:DISPLAY")
            linhas.append(f"TRIGGER:{gatilho}")
            linhas.append(f"DESCRIPTION:{escapar(f'{resumo} (faltam {rotulo})')}")
            linhas.append("END:VALARM")
        linhas.append("END:VEVENT")
        eventos.extend(linhas)

        incluidos.append({
            "numero_processo": numero,
            "data_limite": inicio.strftime("%d/%m/%Y %H:%M" if tem_hora else "%d/%m/%Y"),
            "dias_restantes": dias_restantes,
            "vencido": vencido,
            "finalidade": finalidade,
        })

    cabecalho = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escapar(titulo_calendario)}",
        "X-WR-TIMEZONE:America/Belem",
    ]
    todas = cabecalho + eventos + ["END:VCALENDAR"]

    dobradas: list[str] = []
    for linha in todas:
        dobradas.extend(dobrar(linha))
    conteudo = "\r\n".join(dobradas) + "\r\n"

    incluidos.sort(key=lambda e: e["dias_restantes"])
    return {
        "conteudo_ics": conteudo,
        "total_eventos": len(incluidos),
        "total_ignorados": len(ignorados),
        "eventos": incluidos,
        "ignorados": ignorados,
        "tamanho_bytes": len(conteudo.encode("utf-8")),
    }
