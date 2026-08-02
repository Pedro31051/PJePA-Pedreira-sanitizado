"""Structured boundary shared by the ADK agents and the local verifier."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class InventoryAnalysisInput(BaseModel):
    """Arguments passed by the orchestrator to the inventory specialist."""

    process_dossier_json: str = Field(
        description=(
            "JSON integral do dossiê textual, com process_number, documents, "
            "pages e sha256; sem imagens ou arquivos binários."
        )
    )
    objective: str = Field(
        default="Produzir relatório cronológico completo, com pendências e provas."
    )


class ProcessEvidence(BaseModel):
    document_id: str
    page: int = Field(ge=1)
    excerpt: str = Field(min_length=8, max_length=700)
    sha256: str = Field(min_length=64, max_length=64)


class NormativeSource(BaseModel):
    authority: Literal["CPC", "CNJ", "TJPA", "SEFA_PA", "PARA_LEGISLACAO"]
    title: str
    provision: str
    url: str
    retrieved_excerpt: str = Field(min_length=8, max_length=700)
    relevance: str


class ProcessAct(BaseModel):
    sequence: int = Field(ge=1)
    date: str | None = None
    act_type: Literal[
        "peticao",
        "documento_prova",
        "decisao",
        "sentenca",
        "ato_secretaria",
        "citacao_intimacao",
        "manifestacao_mp",
        "tributario",
        "outro",
    ]
    actor: str | None = None
    summary: str
    procedural_effect: str
    issues: list[str] = Field(default_factory=list)
    evidence: list[ProcessEvidence] = Field(min_length=1)
    normative_sources: list[NormativeSource] = Field(default_factory=list)


class RecommendedReview(BaseModel):
    priority: Literal["alta", "media", "baixa"]
    item: str
    rationale: str
    supporting_act_sequences: list[int] = Field(default_factory=list)
    normative_sources: list[NormativeSource] = Field(default_factory=list)


class ProcessContradiction(BaseModel):
    subject: str
    description: str
    evidence: list[ProcessEvidence] = Field(min_length=2)
    requires_human_review: bool = True


class FinalConclusion(BaseModel):
    conclusion: str
    supporting_act_sequences: list[int] = Field(min_length=1)
    evidence: list[ProcessEvidence] = Field(min_length=1)


class InventoryReport(BaseModel):
    schema_version: Literal["pje.inventory-report/v1"] = "pje.inventory-report/v1"
    process_number: str
    specialty: Literal["inventario_sucessoes"] = "inventario_sucessoes"
    read_only: bool = True
    executive_summary: str
    acts: list[ProcessAct]
    documents_reviewed: list[str]
    unknowns: list[str] = Field(default_factory=list)
    contradictions: list[ProcessContradiction] = Field(default_factory=list)
    conclusions: list[FinalConclusion] = Field(default_factory=list)
    recommended_human_reviews: list[RecommendedReview] = Field(default_factory=list)
    coverage_notes: list[str] = Field(default_factory=list)
