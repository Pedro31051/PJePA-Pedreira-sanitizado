"""Authoritative post-model verification of report evidence and sources."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from .contracts import InventoryReport, ProcessEvidence
from .grounding import build_page_index, ground_evidence_list
from .guardrails import validate_and_normalize_dossier

OFFICIAL_HOSTS = {
    "atos.cnj.jus.br",
    "legis.senado.leg.br",
    "www.planalto.gov.br",
    "sefa.pa.gov.br",
    "site-sefa-wordpress.sefa.pa.gov.br",
    "www.sistemas.pa.gov.br",
    "www.tjpa.jus.br",
}


class ReportRejected(ValueError):
    """Raised when model output is not literally supported by local evidence."""


def _normalize(value: str) -> str:
    return " ".join(value.split())


def _ground_report(report: InventoryReport, documents: list[dict[str, Any]]) -> None:
    """Recupera trecho literal, página e hash pelo índice local antes de validar.

    Toda substituição sai literalmente das páginas do dossiê. Prova
    irrecuperável permanece intacta e cai na rejeição fail-closed adiante.
    """
    index = build_page_index(documents)
    holders: list[Any] = [*report.acts, *report.contradictions, *report.conclusions]
    for holder in holders:
        grounded_items, _stats = ground_evidence_list(
            [evidence.model_dump() for evidence in holder.evidence],
            index,
        )
        holder.evidence = [
            ProcessEvidence.model_validate(item) for item in grounded_items
        ]


def validate_inventory_report(
    report_payload: dict[str, Any] | InventoryReport,
    dossier_payload: dict[str, Any],
) -> InventoryReport:
    dossier = validate_and_normalize_dossier(dossier_payload)
    report = (
        report_payload
        if isinstance(report_payload, InventoryReport)
        else InventoryReport.model_validate(report_payload)
    )
    expected_process = str(dossier.get("process_number") or "")
    if expected_process and report.process_number != expected_process:
        raise ReportRejected("número do processo divergente")
    if report.read_only is not True:
        raise ReportRejected("relatório não está marcado como somente leitura")

    documents: dict[str, dict[str, Any]] = {}
    for document in dossier["documents"]:
        document_id = str(document.get("document_id") or "")
        if not document_id or document_id in documents:
            raise ReportRejected("document_id ausente ou duplicado")
        documents[document_id] = document

    if set(report.documents_reviewed) != set(documents):
        raise ReportRejected("cobertura documental incompleta ou desconhecida")

    _ground_report(report, dossier["documents"])

    sequences = [act.sequence for act in report.acts]
    if sequences != list(range(1, len(sequences) + 1)):
        raise ReportRejected("sequência de atos inválida")

    for act in report.acts:
        for evidence in act.evidence:
            _check_evidence(evidence, documents)
        for source in act.normative_sources:
            _validate_source(source.url)

    valid_sequences = set(sequences)
    for contradiction in report.contradictions:
        for evidence in contradiction.evidence:
            _check_evidence(evidence, documents)
    for conclusion in report.conclusions:
        if not set(conclusion.supporting_act_sequences).issubset(valid_sequences):
            raise ReportRejected("conclusão aponta ato inexistente")
        for evidence in conclusion.evidence:
            _check_evidence(evidence, documents)
    for review in report.recommended_human_reviews:
        if not set(review.supporting_act_sequences).issubset(valid_sequences):
            raise ReportRejected("revisão aponta ato inexistente")
        for source in review.normative_sources:
            _validate_source(source.url)
    return report


def _check_evidence(
    evidence: ProcessEvidence, documents: dict[str, dict[str, Any]]
) -> None:
    document = documents.get(evidence.document_id)
    if document is None:
        raise ReportRejected("prova aponta documento desconhecido")
    if evidence.sha256.casefold() != str(document["sha256"]).casefold():
        raise ReportRejected("hash da prova diverge do documento")
    page = next(
        (item for item in document["pages"] if int(item["page"]) == evidence.page),
        None,
    )
    if page is None:
        raise ReportRejected("prova aponta página inexistente")
    if _normalize(evidence.excerpt) not in _normalize(page["text"]):
        raise ReportRejected("trecho da prova não é literal")


def _validate_source(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in OFFICIAL_HOSTS:
        raise ReportRejected("fonte normativa não oficial ou não autorizada")
