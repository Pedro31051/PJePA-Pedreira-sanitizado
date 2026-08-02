"""Conflict Engine with Explicit Abstention (`conflict_engine.py`).

Detects conflicting facts, names, dates, or opposing judicial orders across documents.
When evidence is contradictory or insufficient to resolve a fact, explicitly abstains
rather than guessing:
  - status: "abstained"
  - reason: "insufficient_evidence" or "contradictory_evidence"
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Union


@dataclass
class ConflictItem:
    conflict_id: str
    category: str  # "party_identifier", "timeline_date", "opposing_judicial_orders", "factual_claim", "procedural_status"
    topic: str
    conflicting_claims: List[Dict[str, Any]]
    status: str  # "unresolved", "resolved", "abstained"
    abstention_reason: Optional[str] = None  # "contradictory_evidence" or "insufficient_evidence"
    resolution: Optional[str] = None
    confidence: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AbstainedFact:
    fact_id: str
    topic: str
    status: str = "abstained"
    reason: str = "contradictory_evidence"  # "contradictory_evidence" or "insufficient_evidence"
    explanation: str = ""
    evidence_sources: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ConflictReport:
    has_conflicts: bool = False
    total_conflicts: int = 0
    conflicts: List[ConflictItem] = field(default_factory=list)
    abstained_facts: List[AbstainedFact] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "has_conflicts": self.has_conflicts,
            "total_conflicts": self.total_conflicts,
            "conflicts": [c.to_dict() for c in self.conflicts],
            "abstained_facts": [a.to_dict() for a in self.abstained_facts],
            "summary": self.summary,
        }


def _safe_confidence(val: Any, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def evaluate_fact_with_abstention(
    topic: str,
    evidence_items: List[Dict[str, Any]],
    threshold_confidence: float = 0.80,
) -> Dict[str, Any]:
    """Evaluate evidence items for a given factual topic with explicit abstention logic.

    Args:
        topic: Name/description of the factual topic under evaluation.
        evidence_items: List of evidence dicts e.g. [{"source": "Doc 1", "value": "2024-01-10", "confidence": 0.9}]
        threshold_confidence: Confidence threshold below which evidence is considered insufficient.

    Returns:
        Dict detailing resolution state or explicit abstention status and reason.
    """
    fact_id = f"fact_{uuid.uuid4().hex[:8]}"

    clean_items: List[Dict[str, Any]] = [i for i in (evidence_items or []) if isinstance(i, dict)]

    if not clean_items:
        return {
            "fact_id": fact_id,
            "topic": str(topic or ""),
            "status": "abstained",
            "reason": "insufficient_evidence",
            "explanation": f"Nenhuma evidência ou documento foi fornecido para '{topic}'. Abstenção explícita ativada.",
            "value": None,
            "confidence": 0.0,
        }

    # Group unique values
    value_map: Dict[Any, List[Dict[str, Any]]] = {}
    for item in clean_items:
        val = item.get("value")
        if val is not None:
            val_str = str(val).strip()
            if val_str:
                value_map.setdefault(val_str, []).append(item)

    unique_values = list(value_map.keys())

    if not unique_values:
        return {
            "fact_id": fact_id,
            "topic": str(topic or ""),
            "status": "abstained",
            "reason": "insufficient_evidence",
            "explanation": f"Nenhum valor válido/não nulo foi fornecido para '{topic}'. Abstenção explícita ativada.",
            "evidence_sources": [str(i.get("source") or "") for i in clean_items if i.get("source")],
            "value": None,
            "confidence": 0.0,
        }

    # Case 1: Contradictory Evidence
    if len(unique_values) > 1:
        sources_summary = []
        for val, items in value_map.items():
            srcs = [str(i.get("source") or "desconhecido") for i in items]
            sources_summary.append(f"Valor '{val}' indicado em [{', '.join(srcs)}]")

        explanation = (
            f"Conflito irresolvível para '{topic}': evidências contraditórias encontradas. "
            f"Fontes: {'; '.join(sources_summary)}. O sistema abstém-se de adivinhar o valor correto."
        )

        return {
            "fact_id": fact_id,
            "topic": str(topic or ""),
            "status": "abstained",
            "reason": "contradictory_evidence",
            "explanation": explanation,
            "evidence_sources": [str(i.get("source") or "") for i in clean_items if i.get("source")],
            "value": None,
            "confidence": 0.0,
        }

    # Case 2: Single value, but low confidence or insufficient details
    single_val = unique_values[0]
    matching_items = value_map[single_val]
    max_conf = max((_safe_confidence(i.get("confidence"), 0.0) for i in matching_items), default=0.0)

    if max_conf < threshold_confidence or single_val.lower() in ("desconhecido", "em apuração", "não informado", "none", "null"):
        return {
            "fact_id": fact_id,
            "topic": str(topic or ""),
            "status": "abstained",
            "reason": "insufficient_evidence",
            "explanation": f"Evidência insuficiente para '{topic}' (confiança {max_conf} < limiar {threshold_confidence}). O sistema abstém-se.",
            "evidence_sources": [str(i.get("source") or "") for i in clean_items if i.get("source")],
            "value": None,
            "confidence": max_conf,
        }

    # Case 3: Resolved fact
    return {
        "fact_id": fact_id,
        "topic": str(topic or ""),
        "status": "resolved",
        "reason": None,
        "explanation": f"Fato '{topic}' resolvido com sucesso.",
        "value": single_val,
        "confidence": max_conf,
    }


def _check_party_conflicts(parties: List[Any]) -> List[ConflictItem]:
    conflicts: List[ConflictItem] = []

    # Map parties by name or role to detect identifier mismatches
    by_name: Dict[str, List[Any]] = {}
    by_role: Dict[str, List[Any]] = {}

    for p in (parties or []):
        if p is None:
            continue
        p_dict = p.to_dict() if callable(getattr(p, "to_dict", None)) else (p if isinstance(p, dict) else {})
        name = str(p_dict.get("name") or "").strip().lower()
        role = str(p_dict.get("role") or "").strip()

        if name and not name.startswith("parte"):
            by_name.setdefault(name, []).append(p_dict)
        if role in ("polo_ativo", "polo_passivo"):
            by_role.setdefault(role, []).append(p_dict)

    # Check for same name with different CPFs/CNPJs
    for name, p_list in by_name.items():
        identifiers = {str(p.get("identifier")) for p in p_list if p.get("identifier") is not None}
        if len(identifiers) > 1:
            claims = [
                {"source": str(p.get("document_source") or "documento_processual"), "value": f"CPF/CNPJ: {p.get('identifier')}"}
                for p in p_list if p.get("identifier") is not None
            ]
            conflicts.append(ConflictItem(
                conflict_id=f"conf_party_{uuid.uuid4().hex[:6]}",
                category="party_identifier",
                topic=f"Identificador (CPF/CNPJ) da parte '{name.title()}'",
                conflicting_claims=claims,
                status="abstained",
                abstention_reason="contradictory_evidence",
                confidence=0.0,
            ))

    return conflicts


def _check_timeline_conflicts(timeline: List[Any]) -> List[ConflictItem]:
    conflicts: List[ConflictItem] = []
    events_by_type: Dict[str, List[Any]] = {}

    for ev in (timeline or []):
        if ev is None:
            continue
        ev_dict = ev.to_dict() if callable(getattr(ev, "to_dict", None)) else (ev if isinstance(ev, dict) else {})
        e_type_raw = ev_dict.get("event_type")
        e_date_raw = ev_dict.get("date")

        if e_type_raw is not None and e_date_raw is not None:
            e_type = str(e_type_raw)
            e_date = str(e_date_raw)
            if e_type and e_date:
                events_by_type.setdefault(e_type, []).append(ev_dict)

    # Check for same event type with conflicting dates
    for e_type, ev_list in events_by_type.items():
        dates = {str(ev.get("date")) for ev in ev_list if ev.get("date") is not None}
        if len(dates) > 1:
            claims = [
                {"source": str(ev.get("document_ref") or "peça_processual"), "value": f"Data: {ev.get('date')}"}
                for ev in ev_list if ev.get("date") is not None
            ]
            conflicts.append(ConflictItem(
                conflict_id=f"conf_timeline_{uuid.uuid4().hex[:6]}",
                category="timeline_date",
                topic=f"Data do evento processual '{e_type}'",
                conflicting_claims=claims,
                status="abstained",
                abstention_reason="contradictory_evidence",
                confidence=0.0,
            ))

    return conflicts


def _check_order_conflicts(orders: List[Any], decisions: List[Any]) -> List[ConflictItem]:
    conflicts: List[ConflictItem] = []

    all_commands = []
    for ord_item in (orders or []):
        if ord_item is None:
            continue
        o_dict = ord_item.to_dict() if callable(getattr(ord_item, "to_dict", None)) else (ord_item if isinstance(ord_item, dict) else {})
        cmd = str(o_dict.get("command_summary") or "").upper()
        doc_ref = str(o_dict.get("document_ref") or "decisão")
        all_commands.append((cmd, doc_ref))

    for dec_item in (decisions or []):
        if dec_item is None:
            continue
        d_dict = dec_item.to_dict() if callable(getattr(dec_item, "to_dict", None)) else (dec_item if isinstance(dec_item, dict) else {})
        summary = str(d_dict.get("dispositivo_summary") or "").upper()
        doc_ref = str(d_dict.get("document_ref") or "sentença/decisão")
        if summary:
            all_commands.append((summary, doc_ref))

    # Detect opposing mandates (e.g., DEFIRO vs INDEFIRO, SUSPENSAO vs EXECUCAO, MANTER POSSE vs RESTITUIR POSSE)
    has_defer = any("DEFIRO" in cmd[0] or "CONCEDO" in cmd[0] or "JULGO PROCEDENTE" in cmd[0] for cmd in all_commands)
    has_indefer = any("INDEFIRO" in cmd[0] or "REVOGO" in cmd[0] or "JULGO IMPROCEDENTE" in cmd[0] for cmd in all_commands)

    if has_defer and has_indefer:
        claims = [
            {"source": cmd[1], "value": cmd[0][:150]}
            for cmd in all_commands
            if any(k in cmd[0] for k in ["DEFIRO", "INDEFIRO", "CONCEDO", "REVOGO", "JULGO PROCEDENTE", "JULGO IMPROCEDENTE"])
        ]
        conflicts.append(ConflictItem(
            conflict_id=f"conf_order_{uuid.uuid4().hex[:6]}",
            category="opposing_judicial_orders",
            topic="Determinações e Ordens Judiciais Conflitantes",
            conflicting_claims=claims,
            status="abstained",
            abstention_reason="contradictory_evidence",
            confidence=0.0,
        ))

    return conflicts


def detect_conflicts(
    structured_process: Union[Any, Dict[str, Any]],
    extracted_pages: Optional[List[Any]] = None,
) -> ConflictReport:
    """Detect conflicting facts, names, dates, or opposing judicial orders across process documents.

    When evidence is contradictory or insufficient, explicitly abstains rather than guessing.

    Args:
        structured_process: StructuredProcess object or dict.
        extracted_pages: Optional list of ExtractedPage objects.

    Returns:
        ConflictReport with explicit abstention state and items.
    """
    sp_dict = (
        structured_process.to_dict()
        if (structured_process is not None and callable(getattr(structured_process, "to_dict", None)))
        else (structured_process if isinstance(structured_process, dict) else {})
    ) or {}

    parties = sp_dict.get("parties", [])
    timeline = sp_dict.get("timeline", [])
    decisions = sp_dict.get("decisions", [])
    orders = sp_dict.get("orders", [])

    conflicts: List[ConflictItem] = []
    abstained_facts: List[AbstainedFact] = []

    # 1. Check Party Identifiers Mismatch
    p_conflicts = _check_party_conflicts(parties)
    conflicts.extend(p_conflicts)

    # 2. Check Timeline Date Conflicts
    t_conflicts = _check_timeline_conflicts(timeline)
    conflicts.extend(t_conflicts)

    # 3. Check Opposing Judicial Orders
    o_conflicts = _check_order_conflicts(orders, decisions)
    conflicts.extend(o_conflicts)

    # Convert abstained conflict items into explicit AbstainedFact records
    for c in conflicts:
        if c.status == "abstained":
            abstained_facts.append(AbstainedFact(
                fact_id=f"abs_{c.conflict_id}",
                topic=c.topic,
                status="abstained",
                reason=c.abstention_reason or "contradictory_evidence",
                explanation=f"Abstenção explícita para '{c.topic}': {len(c.conflicting_claims)} declarações conflitantes encontradas nas fontes.",
                evidence_sources=[claim.get("source", "") for claim in c.conflicting_claims],
            ))

    # 4. Check for Insufficient Evidence Abstention (e.g. key process milestones missing)
    party_dicts = [
        p.to_dict() if callable(getattr(p, "to_dict", None)) else (p if isinstance(p, dict) else {})
        for p in (parties or []) if p is not None
    ]
    if not party_dicts or all((p.get("name") or "").startswith("Parte") for p in party_dicts):
        abs_p = AbstainedFact(
            fact_id="abs_insufficient_parties",
            topic="Qualificação Completa das Partes",
            status="abstained",
            reason="insufficient_evidence",
            explanation="Evidência insuficiente para qualificação completa de polo ativo/passivo e identificadores.",
            evidence_sources=["documento_processual"],
        )
        abstained_facts.append(abs_p)

    has_conflicts = len(conflicts) > 0 or len(abstained_facts) > 0

    summary_parts = []
    if conflicts:
        summary_parts.append(f"{len(conflicts)} conflitos de evidência identificados")
    if abstained_facts:
        summary_parts.append(f"{len(abstained_facts)} abstenções explícitas registradas")
    if not has_conflicts:
        summary_parts.append("Nenhum conflito material ou abstenção necessária. Processo consistente.")

    return ConflictReport(
        has_conflicts=has_conflicts,
        total_conflicts=len(conflicts),
        conflicts=conflicts,
        abstained_facts=abstained_facts,
        summary="; ".join(summary_parts),
    )

