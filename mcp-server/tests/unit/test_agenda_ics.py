"""Testes do gerador de agenda .ics (offline, sem PJe e sem browser).

Rodar: pytest tests/unit/test_agenda_ics.py
"""
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import agenda_ics as A

AGORA = datetime(2026, 7, 25, 10, 0, 0)

EXPEDIENTES = [
    {
        "numero_processo": "0801234-56.2026.8.14.0301",
        "classe": "PROCEDIMENTO COMUM CÍVEL",
        "finalidade": "manifestação",
        "prazo": "15 dias",
        "data_limite": "28/07/2026 23:59",
        "orgao": "1ª Vara Cível; Belém",
        "partes": "FULANO DE TAL X BANCO, S.A.",
        "assunto": "Indenização por Dano Moral",
        "tipo": "Intimação",
        "acao_disponivel": "responder",
        "ultimo_movimento": "Decisão proferida",
    },
    {   # vencido e sem hora -> evento de dia inteiro
        "numero_processo": "0807777-11.2026.8.14.0401",
        "classe": "EXECUÇÃO",
        "finalidade": "ciência",
        "data_limite": "20/07/2026",
        "orgao": "2ª Vara",
    },
    {   # fora da janela de dias_limite
        "numero_processo": "0809999-22.2026.8.14.0501",
        "data_limite": "12/12/2026",
    },
    {   # sem data utilizavel -> vai para 'ignorados', nunca some calado
        "numero_processo": "0800000-00.2026.8.14.0001",
        "data_limite_observacao": "Sem prazo definido",
    },
]


def _uids(texto: str) -> list[str]:
    return re.findall(r"^UID:(.+)$", texto, re.MULTILINE)


def test_parse_data_limite() -> None:
    assert A.parse_data_limite("28/07/2026 23:59") == (datetime(2026, 7, 28, 23, 59), True)
    assert A.parse_data_limite("20/07/2026") == (datetime(2026, 7, 20), False)
    assert A.parse_data_limite("Data limite: 20/07/2026 08:30 (prazo em curso)")[0] == \
        datetime(2026, 7, 20, 8, 30)
    assert A.parse_data_limite("Sem prazo") == (None, False)
    assert A.parse_data_limite(None) == (None, False)


def test_dobra_por_octeto() -> None:
    linha = "SUMMARY:" + "ç" * 100
    pedacos = A.dobrar(linha)
    assert all(len(p.encode("utf-8")) <= 75 for p in pedacos)
    assert "".join([pedacos[0]] + [p[1:] for p in pedacos[1:]]) == linha


def _geracao_completa() -> dict:
    r = A.gerar_ics(EXPEDIENTES, dias_limite=30, agora=AGORA,
                    titulo_calendario="Prazos PJe TJPA 1º grau")
    ics = r["conteudo_ics"]

    assert r["total_eventos"] == 2, r["total_eventos"]
    assert r["total_ignorados"] == 1, r["ignorados"]

    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    linhas = ics.split("\r\n")[:-1]
    longas = [linha for linha in linhas if len(linha.encode("utf-8")) > 75]
    assert not longas, longas
    assert ics.count("BEGIN:VEVENT") == ics.count("END:VEVENT") == 2
    assert ics.count("BEGIN:VALARM") == ics.count("END:VALARM") == 4

    assert "DTSTART:20260728T235900" in ics          # com hora -> hora flutuante
    assert "DURATION:PT1H" in ics
    assert "DTSTART;VALUE=DATE:20260720" in ics      # sem hora -> dia inteiro
    assert "DTEND;VALUE=DATE:20260721" in ics
    assert "SUMMARY:VENCIDO Prazo ciência: EXECUÇÃO 0807777-11.2026.8.14.0401" in ics
    assert "PRIORITY:1" in ics                       # vence em <= 3 dias
    assert r"LOCATION:1ª Vara Cível\; Belém" in ics  # ';' escapado
    assert r"BANCO\, S.A." in ics                    # ',' escapado
    assert r"\n" in ics                              # quebras de linha escapadas

    # filtro de janela e expediente sem data nao podem virar evento
    assert "0809999" not in ics and "0800000" not in ics

    # o texto sobrevive ao desdobramento
    assert "PROCEDIMENTO COMUM CÍVEL" in ics.replace("\r\n ", "")

    # ordenado pelo mais urgente (vencido primeiro)
    assert [e["dias_restantes"] for e in r["eventos"]] == [-6, 3]
    return r


def test_geracao_completa() -> None:
    _geracao_completa()


def test_uid_estavel() -> None:
    a = A.gerar_ics(EXPEDIENTES, dias_limite=30, agora=AGORA)["conteudo_ics"]
    b = A.gerar_ics(EXPEDIENTES, dias_limite=30,
                    agora=datetime(2026, 7, 25, 18, 0))["conteudo_ics"]
    assert _uids(a) == _uids(b), "reimportar precisa atualizar o evento, nao duplicar"


def test_incluir_vencidos_false() -> None:
    r = A.gerar_ics(EXPEDIENTES, dias_limite=30, agora=AGORA, incluir_vencidos=False)
    assert r["total_eventos"] == 1
    assert "0807777" not in r["conteudo_ics"]


def main() -> int:
    testes = [
        test_parse_data_limite,
        test_dobra_por_octeto,
        test_geracao_completa,
        test_uid_estavel,
        test_incluir_vencidos_false,
    ]
    for t in testes:
        t()
        print(f"  ok  {t.__name__}")
    r = _geracao_completa()
    print(f"\n{len(testes)} testes passaram | "
          f"{r['total_eventos']} eventos, {r['tamanho_bytes']} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
