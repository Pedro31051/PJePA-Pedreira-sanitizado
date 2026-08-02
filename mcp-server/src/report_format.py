"""Contrato padronizado do relatório e das sugestões da análise completa.

Todo resultado entregue ao usuário segue o schema
``pje.relatorio-processual/v1``: seções fixas, citação de prova no formato
``[doc <id> p.<página> sha256:<8>]``, separação explícita entre fato provado,
interpretação e recomendação, e sugestões que nunca são executadas
automaticamente. O renderizador só reorganiza conteúdo já verificado — não
cria fato, prova nem sugestão nova.
"""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = "pje.relatorio-processual/v1"

AVISOS_PADRAO = [
    "Relatório somente leitura: nenhuma ação foi executada no PJe.",
    "Todo achado publicado tem prova literal verificada localmente "
    "(documento, página, trecho e hash).",
    "A síntese e as sugestões são interpretação assistida, não decisão "
    "judicial nem orientação humana definitiva.",
    "Sugestões exigem conferência humana antes de qualquer providência.",
]

_SECTION_BY_CATEGORY = {
    "fato": "atos_e_fatos",
    "pedido": "atos_e_fatos",
    "defesa": "atos_e_fatos",
    "decisao": "atos_e_fatos",
    "prova": "atos_e_fatos",
    "contradicao": "contradicoes",
    "pendencia": "pendencias",
    "proximo_ato": "proximos_atos",
}


def _cite(evidence: dict[str, Any]) -> str:
    sha = str(evidence.get("sha256") or "")[:8]
    return (
        f"[doc {evidence.get('document_id')} "
        f"p.{evidence.get('page')} sha256:{sha}]"
    )


def _entry(finding: dict[str, Any]) -> dict[str, Any]:
    return {
        "finding_id": str(finding.get("finding_id") or ""),
        "categoria": str(finding.get("category") or ""),
        "titulo": str(finding.get("title") or ""),
        "conclusao": str(finding.get("conclusion") or ""),
        "ressalva": str(finding.get("caveat") or ""),
        "provas": [
            {
                "document_id": str(item.get("document_id") or ""),
                "pagina": int(item.get("page") or 0),
                "trecho": str(item.get("excerpt") or ""),
                "sha256": str(item.get("sha256") or ""),
                "citacao": _cite(item),
            }
            for item in finding.get("evidence") or []
        ],
    }


def _sugestao(
    item: str,
    *,
    fundamento: str = "",
    prioridade: str = "",
    achados_suporte: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "item": item,
        "fundamento": fundamento,
        "prioridade": prioridade or "media",
        "achados_suporte": list(achados_suporte or []),
        "execucao_automatica": False,
        "exige_conferencia_humana": True,
    }


def build_standard_report(result: dict[str, Any]) -> dict[str, Any]:
    """Reorganiza um resultado verificado no contrato padronizado."""
    sections: dict[str, list[dict[str, Any]]] = {
        "atos_e_fatos": [],
        "contradicoes": [],
        "pendencias": [],
        "proximos_atos": [],
    }
    for finding in result.get("findings") or []:
        section = _SECTION_BY_CATEGORY.get(
            str(finding.get("category") or ""), "atos_e_fatos"
        )
        sections[section].append(_entry(finding))

    synthesis = result.get("sintese") or {}
    synthesis_ok = synthesis.get("status") == "completed"
    suggestions = []
    if synthesis_ok:
        for item in synthesis.get("suggestions") or []:
            suggestions.append(
                _sugestao(
                    str(item.get("item") or ""),
                    fundamento=str(item.get("rationale") or ""),
                    prioridade=str(item.get("priority") or ""),
                    achados_suporte=[
                        str(v) for v in item.get("supporting_finding_ids") or []
                    ],
                )
            )
    for review in result.get("recommended_human_reviews") or []:
        text = str(review or "").strip()
        if text and all(text != existing["item"] for existing in suggestions):
            suggestions.append(_sugestao(text))

    return {
        "schema_version": SCHEMA_VERSION,
        "read_only": True,
        "fonte": {
            "provider": result.get("provider"),
            "model": result.get("model"),
            "capsule_id": result.get("capsule_id"),
            "source_job_id": result.get("source_job_id"),
            "source": result.get("source"),
        },
        "sintese_executiva": (
            str(synthesis.get("executive_summary") or "")
            if synthesis_ok
            else str(result.get("summary") or "")
        ),
        "estado_atual": (
            str(synthesis.get("current_state") or "") if synthesis_ok else ""
        ),
        **sections,
        "desconhecidos": [str(v) for v in result.get("unknowns") or []],
        "questoes_abertas": [
            str(v) for v in (synthesis.get("open_questions") or [])
        ]
        if synthesis_ok
        else [],
        "sugestoes": suggestions,
        "sugestoes_rejeitadas": list(synthesis.get("rejected_suggestions") or []),
        "sintese_status": str(synthesis.get("status") or "unavailable"),
        "verificacao": result.get("verification") or {},
        "avisos": list(AVISOS_PADRAO),
    }


def _md_findings(title: str, entries: list[dict[str, Any]]) -> list[str]:
    lines = [f"## {title}", ""]
    if not entries:
        lines.append("Nenhum item verificado nesta seção.")
        lines.append("")
        return lines
    for entry in entries:
        citations = " ".join(prova["citacao"] for prova in entry["provas"])
        lines.append(f"- **{entry['titulo']}** — {entry['conclusao']} {citations}")
        if entry["ressalva"]:
            lines.append(f"  - Ressalva: {entry['ressalva']}")
    lines.append("")
    return lines


def render_markdown(report: dict[str, Any]) -> str:
    """Renderiza o relatório padronizado em Markdown determinístico."""
    lines: list[str] = ["# Relatório processual padronizado", ""]
    if report.get("sintese_executiva"):
        lines += ["## Síntese executiva", "", report["sintese_executiva"], ""]
    if report.get("estado_atual"):
        lines += ["## Estado atual", "", report["estado_atual"], ""]
    lines += _md_findings("Atos e fatos verificados", report["atos_e_fatos"])
    lines += _md_findings("Contradições", report["contradicoes"])
    lines += _md_findings("Pendências", report["pendencias"])
    lines += _md_findings("Próximos atos identificados", report["proximos_atos"])
    lines += ["## Lacunas e desconhecidos", ""]
    for item in report["desconhecidos"] or ["Nenhum registrado."]:
        lines.append(f"- {item}")
    lines.append("")
    lines += ["## Sugestões (não executadas automaticamente)", ""]
    if report["sugestoes"]:
        for suggestion in report["sugestoes"]:
            supports = (
                f" (suporte: {', '.join(suggestion['achados_suporte'])})"
                if suggestion["achados_suporte"]
                else ""
            )
            lines.append(
                f"- [{suggestion['prioridade']}] {suggestion['item']}{supports}"
            )
            if suggestion["fundamento"]:
                lines.append(f"  - Fundamento: {suggestion['fundamento']}")
    else:
        lines.append("Nenhuma sugestão aprovada pelo revisor.")
    lines.append("")
    verification = report.get("verificacao") or {}
    lines += [
        "## Verificação",
        "",
        f"- Achados verificados: {verification.get('verified_findings', 0)}",
        f"- Achados rejeitados: {verification.get('rejected_findings', 0)}",
        f"- Aterramento literal: {verification.get('literal_grounding', {})}",
        "",
        "## Avisos",
        "",
    ]
    lines += [f"- {aviso}" for aviso in report["avisos"]]
    lines.append("")
    return "\n".join(lines)
