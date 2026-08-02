"""Structured Entity Extraction Engine (`structuring_engine.py`) for legal process text and documents.

Extracts:
1. Parties (polo ativo, polo passivo, advogados/representantes, terceiros) with roles and identifiers (CPF/CNPJ/OAB).
2. Timeline events with dates, document references, and procedural milestones.
3. Judicial decisions (sentenças, acórdãos, decisões interlocutórias, tutelas).
4. Judicial orders (determinações, intimações, notificações).
5. Procedural deadlines (prazos recursais, prazos de manifestação, datas limites, business/calendar days logic).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Union

# Regex patterns for identifiers and dates
CPF_PATTERN = re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b|\b\d{11}\b")
CNPJ_PATTERN = re.compile(r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b")
OAB_PATTERN = re.compile(r"\bOAB[/-]?[A-Z]{2}\s*n?º?\s*\d+\b|\b\d{3,6}[/-]?[A-Z]{2}\b", re.IGNORECASE)
DATE_PATTERN = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b|\b(\d{4})-(\d{2})-(\d{2})\b")
PROCESS_NUMBER_PATTERN = re.compile(r"\b\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}\b")


@dataclass
class Party:
    name: str
    role: str  # "polo_ativo", "polo_passivo", "advogado", "terceiro", "outros"
    specific_role: str  # "Autor", "Réu", "Inventariante", "Executado", "Advogado do Autor", "Perito", "MP"
    identifier: Optional[str] = None  # CPF, CNPJ, OAB
    represented_by: List[str] = field(default_factory=list)
    document_source: Optional[str] = None
    confidence: float = 0.90

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TimelineEvent:
    event_id: str
    date: Optional[str]  # YYYY-MM-DD or raw date string
    event_type: str  # "peticao_inicial", "citacao", "contestacao", "despacho", "decisao", "sentenca", "recurso", "certidao"
    description: str
    document_ref: Optional[str] = None
    source_page: Optional[int] = None
    significance: str = "procedural_step"  # "major_milestone", "procedural_step", "ordinary_notice"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class JudicialDecision:
    decision_id: str
    decision_type: str  # "sentenca", "acordao", "decisao_interlocutoria", "tutela_urgencia", "despacho_saneador"
    date: Optional[str]
    judge_or_court: Optional[str]
    dispositivo_summary: str
    granted_reliefs: List[str] = field(default_factory=list)
    denied_reliefs: List[str] = field(default_factory=list)
    document_ref: Optional[str] = None
    source_page: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class JudicialOrder:
    order_id: str
    order_type: str  # "determinacao", "intimacao", "notificacao", "penhora", "citacao", "pericia"
    target_party: Optional[str]
    command_summary: str
    deadline_days: Optional[int] = None
    deadline_type: Optional[str] = "business_days"  # "business_days" / "dias_uteis", "calendar_days" / "dias_corridos"
    due_date: Optional[str] = None
    document_ref: Optional[str] = None
    source_page: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ProceduralDeadline:
    deadline_id: str
    name: str
    trigger_event: str
    start_date: Optional[str]
    deadline_days: int
    day_type: str  # "business_days" ("dias_uteis") or "calendar_days" ("dias_corridos")
    calculated_due_date: Optional[str]
    status: str = "active"  # "active", "expired", "fulfilled", "pending"
    source_ref: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StructuredProcess:
    process_number: Optional[str] = None
    parties: List[Party] = field(default_factory=list)
    timeline: List[TimelineEvent] = field(default_factory=list)
    decisions: List[JudicialDecision] = field(default_factory=list)
    orders: List[JudicialOrder] = field(default_factory=list)
    deadlines: List[ProceduralDeadline] = field(default_factory=list)
    conflict_report: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "process_number": self.process_number,
            "parties": [p.to_dict() if callable(getattr(p, "to_dict", None)) else (p if isinstance(p, dict) else {}) for p in (self.parties or []) if p is not None],
            "timeline": [t.to_dict() if callable(getattr(t, "to_dict", None)) else (t if isinstance(t, dict) else {}) for t in (self.timeline or []) if t is not None],
            "decisions": [d.to_dict() if callable(getattr(d, "to_dict", None)) else (d if isinstance(d, dict) else {}) for d in (self.decisions or []) if d is not None],
            "orders": [o.to_dict() if callable(getattr(o, "to_dict", None)) else (o if isinstance(o, dict) else {}) for o in (self.orders or []) if o is not None],
            "deadlines": [dl.to_dict() if callable(getattr(dl, "to_dict", None)) else (dl if isinstance(dl, dict) else {}) for dl in (self.deadlines or []) if dl is not None],
            "conflict_report": self.conflict_report,
            "metadata": self.metadata,
        }


# --- Procedural Deadline Calculation Engine ---

def parse_date(date_val: Union[str, date, datetime]) -> Optional[date]:
    """Parse string or date into datetime.date object."""
    if isinstance(date_val, datetime):
        return date_val.date()
    if isinstance(date_val, date):
        return date_val

    if not date_val or not isinstance(date_val, str):
        return None

    date_str = date_val.strip()

    # Try ISO YYYY-MM-DD
    try:
        return datetime.strptime(date_str[:10], "%Y-%m-%d").date()
    except ValueError:
        pass

    # Try Brazilian DD/MM/YYYY
    try:
        return datetime.strptime(date_str[:10], "%d/%m/%Y").date()
    except ValueError:
        pass

    # Match via regex
    match = DATE_PATTERN.search(date_str)
    if match:
        g = match.groups()
        if g[0] and g[1] and g[2]:  # DD/MM/YYYY
            try:
                return date(int(g[2]), int(g[1]), int(g[0]))
            except ValueError:
                pass
        elif g[3] and g[4] and g[5]:  # YYYY-MM-DD
            try:
                return date(int(g[3]), int(g[4]), int(g[5]))
            except ValueError:
                pass

    return None


def is_business_day(d: date, holidays: Optional[List[Union[str, date]]] = None) -> bool:
    """Check if date is a business day (Monday-Friday, not a court holiday)."""
    if d.weekday() >= 5:  # Saturday (5) or Sunday (6)
        return False

    if holidays:
        holiday_dates = set()
        for h in holidays:
            parsed = parse_date(h)
            if parsed:
                holiday_dates.add(parsed)
        if d in holiday_dates:
            return False

    return True


def calculate_procedural_deadline(
    start_date: Union[str, date, datetime],
    days: int,
    day_type: str = "business_days",
    holidays: Optional[List[Union[str, date]]] = None,
) -> str:
    """Calculate procedural deadline due date following CPC 2015 rules (art. 219 & 224).

    Rules:
    - Exclusion of start date, inclusion of due date (Art. 224).
    - Starts counting from the next business day after notice/publication.
    - If day_type is 'business_days' ('dias_uteis'), count only business days (Art. 219).
    - If day_type is 'calendar_days' ('dias_corridos'), count calendar days, but if the final day
      falls on a weekend/holiday, extend to the next business day (Art. 224 § 1º).

    Returns:
        ISO formatted date string 'YYYY-MM-DD'.
    """
    st_date = parse_date(start_date)
    if not st_date:
        st_date = date.today()

    days_int = int(days) if (days is not None and str(days).isdigit()) else 0

    norm_day_type = str(day_type or "business_days").lower()
    is_dias_uteis = norm_day_type in ("business_days", "dias_uteis", "dias uteis", "uteis")

    current_date = st_date + timedelta(days=1)

    # Move start of counting to first business day if starting on weekend/holiday
    while not is_business_day(current_date, holidays):
        current_date += timedelta(days=1)

    if days_int <= 0:
        return current_date.isoformat()

    if is_dias_uteis:
        # Count N business days
        count = 0
        while count < days_int:
            if is_business_day(current_date, holidays):
                count += 1
                if count == days_int:
                    break
            current_date += timedelta(days=1)
    else:
        # Count N calendar days
        current_date = current_date + timedelta(days=days_int - 1)
        # Prorogate if final day falls on weekend/holiday
        while not is_business_day(current_date, holidays):
            current_date += timedelta(days=1)

    return current_date.isoformat()


# --- Extraction Heuristics & Pattern Processing ---

def extract_process_number(text: str) -> Optional[str]:
    text = str(text or "")
    match = PROCESS_NUMBER_PATTERN.search(text)
    if match:
        return match.group(0)
    return None


def extract_parties(text: str, source_doc: Optional[str] = None) -> List[Party]:
    """Extract parties with roles, specific roles, and identifiers."""
    text = str(text or "")
    parties: List[Party] = []
    seen_party_keys: set[tuple[str, Optional[str]]] = set()
    name_to_identifiers: Dict[str, set[Optional[str]]] = {}

    def add_party_candidate(
        name: str,
        role: str,
        specific_role: str,
        identifier: Optional[str],
        doc_source: Optional[str],
        confidence: float,
    ) -> None:
        name_lower = name.lower()
        key = (name_lower, identifier)
        if key in seen_party_keys:
            return

        if identifier is None:
            if name_lower in name_to_identifiers:
                return
        else:
            if name_lower in name_to_identifiers:
                if None in name_to_identifiers[name_lower]:
                    for p in parties:
                        if p.name.lower() == name_lower and p.identifier is None:
                            p.identifier = identifier
                            name_to_identifiers[name_lower].remove(None)
                            name_to_identifiers[name_lower].add(identifier)
                            seen_party_keys.add(key)
                            return

        parties.append(Party(
            name=name,
            role=role,
            specific_role=specific_role,
            identifier=identifier,
            document_source=doc_source,
            confidence=confidence,
        ))
        seen_party_keys.add(key)
        name_to_identifiers.setdefault(name_lower, set()).add(identifier)

    # Pattern 1: Structured role blocks (AUTOR:, REQUERENTE:, REU:, etc.)
    role_blocks = re.findall(
        r"(AUTOR(?:A)?|REQUERENTE|EXEQUENTE|INVENTARIANTE|RÉU|RÉ|REQUERIDO|EXECUTADO|EMBARGADO|INTERESSADO|TERCEIRO|ADVOGADO(?:A)?|PROCURADOR(?:A)?):\s*([^\n;]+)",
        text,
        re.IGNORECASE,
    )

    for role_raw, name_raw in role_blocks:
        role_upper = role_raw.upper()
        clean_name = re.sub(r"\b(CPF|CNPJ|OAB|MF|RG|nº|Nº)[\s\d\.\-/:]+", "", name_raw, flags=re.IGNORECASE).strip()
        clean_name = clean_name.split(",")[0].strip()

        if len(clean_name) < 3:
            continue

        # Extract identifier from full block string
        cpf_match = CPF_PATTERN.search(name_raw)
        cnpj_match = CNPJ_PATTERN.search(name_raw)
        oab_match = OAB_PATTERN.search(name_raw)

        identifier = None
        if cpf_match:
            identifier = cpf_match.group(0)
        elif cnpj_match:
            identifier = cnpj_match.group(0)
        elif oab_match:
            identifier = oab_match.group(0)

        # Determine macro role
        if any(w in role_upper for w in ["AUTOR", "REQUERENTE", "EXEQUENTE", "INVENTARIANTE"]):
            macro_role = "polo_ativo"
        elif any(w in role_upper for w in ["RÉU", "REQUERIDO", "EXECUTADO", "EMBARGADO"]):
            macro_role = "polo_passivo"
        elif any(w in role_upper for w in ["ADVOGADO", "PROCURADOR"]):
            macro_role = "advogado"
        else:
            macro_role = "terceiro"

        add_party_candidate(
            name=clean_name,
            role=macro_role,
            specific_role=role_raw.capitalize(),
            identifier=identifier,
            doc_source=source_doc,
            confidence=0.92,
        )

    # Pattern 2: Generic CPF/CNPJ regex extraction if not captured above
    if not parties:
        lines = text.splitlines()
        for line in lines:
            if any(k in line.upper() for k in ["AUTOR", "RÉU", "REQUERIDO", "REQUERENTE", "CPF", "CNPJ"]):
                cpf_match = CPF_PATTERN.search(line)
                cnpj_match = CNPJ_PATTERN.search(line)
                if cpf_match or cnpj_match:
                    ident = cpf_match.group(0) if cpf_match else cnpj_match.group(0)

                    # Extract name candidate
                    parts = re.split(r"[,;:\-–]", line)
                    cand_name = parts[0].strip()
                    cand_name = re.sub(r"^(AUTOR|RÉU|REQUERENTE|REQUERIDO)\s*", "", cand_name, flags=re.IGNORECASE).strip()

                    if len(cand_name) >= 3:
                        role = "polo_ativo" if "AUTOR" in line.upper() or "REQUERENTE" in line.upper() else "polo_passivo"
                        add_party_candidate(
                            name=cand_name,
                            role=role,
                            specific_role="Parte Processual",
                            identifier=ident,
                            doc_source=source_doc,
                            confidence=0.85,
                        )

    # Ensure at least basic party representations if text describes active/passive roles
    if not parties and ("AUTOR" in text.upper() or "RÉU" in text.upper()):
        parties.append(Party(
            name="Parte Autora (Identificação em apuração)",
            role="polo_ativo",
            specific_role="Autor",
            confidence=0.70,
        ))
        parties.append(Party(
            name="Parte Ré (Identificação em apuração)",
            role="polo_passivo",
            specific_role="Réu",
            confidence=0.70,
        ))

    return parties


def extract_timeline(text: str, source_doc: Optional[str] = None) -> List[TimelineEvent]:
    """Extract timeline events and procedural milestones."""
    text = str(text or "")
    events: List[TimelineEvent] = []
    seen = set()

    milestone_specs = [
        ("peticao_inicial", r"\b(petição inicial|inicial|ação de [^\n,]+)\b", "Petição Inicial apresentada", "major_milestone"),
        ("citacao", r"\b(citação|citado|mandado de citação)\b", "Citação da parte ré", "major_milestone"),
        ("contestacao", r"\b(contestação|resposta do réu)\b", "Contestação apresentada", "major_milestone"),
        ("replica", r"\b(réplica|impugnação à contestação)\b", "Réplica apresentada", "procedural_step"),
        ("despacho", r"\b(despacho|despacho saneador)\b", "Despacho proferido", "procedural_step"),
        ("decisao", r"\b(decisão interlocutória|tutela de urgência)\b", "Decisão Interlocutória proferida", "major_milestone"),
        ("sentenca", r"\b(sentença|julgo procedente|julgo improcedente)\b", "Sentença proferida", "major_milestone"),
        ("recurso", r"\b(apelação|recurso|agravo de instrumento)\b", "Recurso interposto", "major_milestone"),
        ("certidao", r"\b(certidão de trânsito em julgado|trânsito em julgado)\b", "Certidão de Trânsito em Julgado", "major_milestone"),
    ]

    for m_type, pattern, default_desc, significance in milestone_specs:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            # Find closest date near match
            context_snippet = text[max(0, match.start() - 100): min(len(text), match.end() + 100)]
            date_match = DATE_PATTERN.search(context_snippet)

            event_date = None
            if date_match:
                parsed = parse_date(date_match.group(0))
                if parsed:
                    event_date = parsed.isoformat()

            key = (m_type, event_date)
            if key not in seen:
                events.append(TimelineEvent(
                    event_id=f"evt_{len(events)+1}_{m_type}",
                    date=event_date,
                    event_type=m_type,
                    description=f"{default_desc}: '{match.group(0)}'",
                    document_ref=source_doc,
                    significance=significance,
                ))
                seen.add(key)

    return events


def extract_decisions(text: str, source_doc: Optional[str] = None) -> List[JudicialDecision]:
    """Extract judicial decisions (sentenças, tutelas, decisões interlocutórias)."""
    text = str(text or "")
    decisions: List[JudicialDecision] = []

    decision_keywords = [
        ("sentenca", [r"SENTENÇA", r"POSTO ISSO", r"JULGO PROCEDENTE", r"JULGO IMPROCEDENTE", r"JULGO PARCIALMENTE PROCEDENTE"]),
        ("tutela_urgencia", [r"TUTELA DE URGÊNCIA", r"TUTELA DE EVIDÊNCIA", r"DEFIRO O PEDIDO DE LIMINAR", r"CONCEDO A ANTECIPAÇÃO DE TUTELA"]),
        ("decisao_interlocutoria", [r"DECISÃO INTERLOCUTÓRIA", r"DECISÃO", r"DEFIRO", r"INDEFIRO"]),
    ]

    for dec_type, kw_list in decision_keywords:
        for kw in kw_list:
            match = re.search(kw, text, re.IGNORECASE)
            if match:
                snippet = text[match.start(): min(len(text), match.start() + 400)].replace("\n", " ").strip()
                date_match = DATE_PATTERN.search(text[max(0, match.start() - 150): match.start() + 200])
                dec_date = parse_date(date_match.group(0)).isoformat() if date_match and parse_date(date_match.group(0)) else None

                granted = []
                denied = []

                if any(w in snippet.upper() for w in ["PROCEDENTE", "DEFIRO", "CONCEDO"]):
                    granted.append(snippet[:200])
                if any(w in snippet.upper() for w in ["IMPROCEDENTE", "INDEFIRO", "NEGOU"]):
                    denied.append(snippet[:200])

                decisions.append(JudicialDecision(
                    decision_id=f"dec_{len(decisions)+1}_{dec_type}",
                    decision_type=dec_type,
                    date=dec_date,
                    judge_or_court="Juízo de Direito / Vara Cível",
                    dispositivo_summary=snippet[:300],
                    granted_reliefs=granted,
                    denied_reliefs=denied,
                    document_ref=source_doc,
                ))
                break  # Process one decision per type match

    return decisions


def extract_orders(text: str, source_doc: Optional[str] = None) -> List[JudicialOrder]:
    """Extract judicial orders, commands, determinations, and target deadlines."""
    text = str(text or "")
    orders: List[JudicialOrder] = []

    order_patterns = [
        (r"DETERMINO\s+(?:A\s+)?([^\n\.]+)", "determinacao"),
        (r"INTIME-SE\s+([^\n\.]+)", "intimacao"),
        (r"CITE-SE\s+([^\n\.]+)", "citacao"),
        (r"NOTIFIQUE-SE\s+([^\n\.]+)", "notificacao"),
        (r"PENHORE-SE\s+([^\n\.]+)", "penhora"),
    ]

    for pattern, ord_type in order_patterns:
        matches = re.finditer(pattern, text, re.IGNORECASE)
        for m in matches:
            cmd_text = m.group(0).strip()
            # Look for deadline days
            days_match = re.search(r"prazo de\s+(\d+)\s+dias", cmd_text, re.IGNORECASE)
            deadline_days = int(days_match.group(1)) if days_match else None

            # Day type heuristic
            day_type = "business_days"
            if re.search(r"dias corridos", cmd_text, re.IGNORECASE):
                day_type = "calendar_days"

            orders.append(JudicialOrder(
                order_id=f"ord_{len(orders)+1}_{ord_type}",
                order_type=ord_type,
                target_party="Parte Intimada / Requerida",
                command_summary=cmd_text[:250],
                deadline_days=deadline_days,
                deadline_type=day_type,
                document_ref=source_doc,
            ))

    return orders


def extract_deadlines(
    orders: List[Union[JudicialOrder, Dict[str, Any]]],
    timeline: List[TimelineEvent],
    start_reference_date: Optional[str] = None,
) -> List[ProceduralDeadline]:
    """Generate active procedural deadlines from extracted orders and timeline triggers."""
    deadlines: List[ProceduralDeadline] = []

    ref_date = start_reference_date or date.today().isoformat()

    for idx, ord_item in enumerate(orders or [], start=1):
        if ord_item is None:
            continue

        if isinstance(ord_item, dict):
            d_days = ord_item.get("deadline_days")
            d_type = ord_item.get("deadline_type") or "business_days"
            o_type = str(ord_item.get("order_type") or "determinacao")
            cmd_sum = str(ord_item.get("command_summary") or "")
            doc_ref = ord_item.get("document_ref")
        else:
            d_days = getattr(ord_item, "deadline_days", None)
            d_type = getattr(ord_item, "deadline_type", None) or "business_days"
            o_type = str(getattr(ord_item, "order_type", "determinacao") or "determinacao")
            cmd_sum = str(getattr(ord_item, "command_summary", "") or "")
            doc_ref = getattr(ord_item, "document_ref", None)

        if d_days:
            due_str = calculate_procedural_deadline(
                start_date=ref_date,
                days=d_days,
                day_type=d_type,
            )
            deadlines.append(ProceduralDeadline(
                deadline_id=f"dl_{idx}_{o_type}",
                name=f"Prazo para {o_type.capitalize()}",
                trigger_event=cmd_sum[:100],
                start_date=ref_date,
                deadline_days=d_days,
                day_type=d_type,
                calculated_due_date=due_str,
                status="active",
                source_ref=doc_ref,
            ))

    # Standard fallback deadlines if none found explicitly
    if not deadlines:
        default_due = calculate_procedural_deadline(ref_date, days=15, day_type="business_days")
        deadlines.append(ProceduralDeadline(
            deadline_id="dl_default_15d",
            name="Prazo Geral de Manifestação (CPC Art. 219)",
            trigger_event="Intimação ordinária",
            start_date=ref_date,
            deadline_days=15,
            day_type="business_days",
            calculated_due_date=default_due,
            status="active",
        ))

    return deadlines


# --- Main Structuring Pipeline Function ---

def structure_process(
    extracted_input: Union[List[Any], Dict[str, Any], str],
    process_number: Optional[str] = None,
) -> StructuredProcess:
    """Structure extracted page/document texts into comprehensive legal process entities.

    Args:
        extracted_input: ExtractedPage list, TextCascadeResult dict, text string, or doc list.
        process_number: Optional explicit process reference string.

    Returns:
        StructuredProcess instance containing parties, timeline, decisions, orders, and deadlines.
    """
    all_text_parts: List[str] = []
    if isinstance(extracted_input, str):
        all_text_parts.append(extracted_input or "")
    elif isinstance(extracted_input, dict):
        if "pages" in extracted_input:
            for p in extracted_input["pages"]:
                if isinstance(p, dict):
                    all_text_parts.append(p.get("extracted_text") or "")
                elif hasattr(p, "extracted_text"):
                    all_text_parts.append(getattr(p, "extracted_text", "") or "")
        elif "extracted_text" in extracted_input:
            all_text_parts.append(extracted_input.get("extracted_text") or "")
    elif isinstance(extracted_input, list):
        for item in extracted_input:
            if hasattr(item, "extracted_text"):
                all_text_parts.append(getattr(item, "extracted_text", "") or "")
            elif isinstance(item, dict):
                all_text_parts.append(item.get("extracted_text") or item.get("text") or "")
            elif isinstance(item, str):
                all_text_parts.append(item or "")

    safe_text_parts = [str(t) if t is not None else "" for t in all_text_parts]
    combined_text = "\n\n".join(safe_text_parts)

    proc_num = process_number or extract_process_number(combined_text)

    # Entity extractions
    parties = extract_parties(combined_text)
    timeline = extract_timeline(combined_text)
    decisions = extract_decisions(combined_text)
    orders = extract_orders(combined_text)
    deadlines = extract_deadlines(orders, timeline)

    # Internal conflict check integration
    conflict_report_dict = None
    try:
        import conflict_engine
        sp_temp = StructuredProcess(
            process_number=proc_num,
            parties=parties,
            timeline=timeline,
            decisions=decisions,
            orders=orders,
            deadlines=deadlines,
        )
        conf_report = conflict_engine.detect_conflicts(sp_temp)
        conflict_report_dict = conf_report.to_dict()
    except ImportError:
        pass

    return StructuredProcess(
        process_number=proc_num,
        parties=parties,
        timeline=timeline,
        decisions=decisions,
        orders=orders,
        deadlines=deadlines,
        conflict_report=conflict_report_dict,
        metadata={
            "total_extracted_characters": len(combined_text),
            "parties_count": len(parties),
            "timeline_events_count": len(timeline),
            "decisions_count": len(decisions),
            "orders_count": len(orders),
            "deadlines_count": len(deadlines),
        },
    )
