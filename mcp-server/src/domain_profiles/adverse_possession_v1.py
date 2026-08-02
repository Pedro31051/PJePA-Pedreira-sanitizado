"""Domain Profile adverse-possession/v1: Schema-first entities, extraction and rite validation for Usucapião processes.

Provides data models, extraction cascade parsing, and procedural rite validation for
usucapião (extraordinária, ordinária, especial urbana, especial rural, familiar, extrajudicial).
"""

import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ClaimantInfo(BaseModel):
    name: Optional[str] = Field(None, description="Nome do requerente da usucapião")
    cpf_cnpj: Optional[str] = Field(None, description="CPF ou CNPJ do requerente")
    marital_status: Optional[str] = Field(None, description="Estado civil do requerente")
    spouse_name: Optional[str] = Field(None, description="Nome do cônjuge/companheiro")


class TechnicalResponsibility(BaseModel):
    art_rrt_number: Optional[str] = Field(None, description="Número da ART (CREA) ou RRT (CAU)")
    professional_name: Optional[str] = Field(None, description="Nome do engenheiro ou agrimensor responsável")
    council_registration: Optional[str] = Field(None, description="Número de registro no CREA/CAU")


class SubjectPropertyInfo(BaseModel):
    property_description: Optional[str] = Field(None, description="Descrição do imóvel usucapiendo")
    address: Optional[str] = Field(None, description="Endereço físico / localização")
    matricula_number: Optional[str] = Field(None, description="Número da matrícula imobiliária ou transcrição")
    registry_office: Optional[str] = Field(None, description="Cartório de Registro de Imóveis (CRI)")
    area_m2: Optional[float] = Field(None, description="Área total em metros quadrados (m²)")
    area_hectares: Optional[float] = Field(None, description="Área total em hectares (ha)")
    perimeter_meters: Optional[float] = Field(None, description="Perímetro em metros")
    coordinates_polygon: List[str] = Field(default_factory=list, description="Vértices/coordenadas topográficas")
    memorial_descritivo_present: bool = Field(False, description="Se há memorial descritivo juntado")
    topographic_map_present: bool = Field(False, description="Se há planta topográfica juntada")
    technical_responsibility: Optional[TechnicalResponsibility] = None


class LandAreaInfo(BaseModel):
    property_description: Optional[str] = Field(None, description="Descrição do imóvel usucapiendo")
    address: Optional[str] = Field(None, description="Endereço físico / localização")
    area_m2: Optional[float] = Field(None, description="Área total em metros quadrados (m²)")
    area_hectares: Optional[float] = Field(None, description="Área total em hectares (ha)")
    perimeter_meters: Optional[float] = Field(None, description="Perímetro em metros")
    coordinates_polygon: List[str] = Field(default_factory=list, description="Vértices/coordenadas topográficas")
    memorial_descritivo_present: bool = Field(False, description="Se há memorial descritivo juntado")
    topographic_map_present: bool = Field(False, description="Se há planta topográfica juntada")
    technical_responsibility: Optional[TechnicalResponsibility] = None


class PossessionDetailsInfo(BaseModel):
    possession_time_years_claimed: Optional[float] = Field(None, description="Tempo de posse alegado em anos")
    justo_titulo: bool = Field(False, description="Se possui justo título (ex: contrato particular, promessa de compra e venda)")
    boa_fe: bool = Field(False, description="Se há presunção ou prova de boa-fé")
    mansa_e_pacifica: bool = Field(True, description="Se a posse é mansa e pacífica sem oposição")
    ininterrupta: bool = Field(True, description="Se a posse é contínua e ininterrupta")
    modality: str = Field("extraordinaria", description="'extraordinaria', 'ordinaria', 'urbana', 'rural', 'familiar', 'extrajudicial'")


class ConfrontanteItem(BaseModel):
    name: str = Field(..., description="Nome do confrontante / vizinho")
    cpf_cnpj: Optional[str] = Field(None, description="CPF ou CNPJ do confrontante")
    cardinal_direction: str = Field("confrontante_geral", description="'norte', 'sul', 'leste', 'oeste', 'confrontante_geral'")
    property_boundary: Optional[str] = Field(None, description="Descrição do limite/divisa")
    citation_status: str = Field("pendente", description="'citado_pessoalmente', 'citado_edital', 'pendente', 'nao_localizado'")
    manifestation_status: str = Field("nao_manifestado", description="'anuencia_expressa', 'contestacao', 'inercia_silencio', 'nao_manifestado'")
    evidence_doc_id: Optional[str] = Field(None, description="ID do documento de citação ou resposta")


class ConfrontantesInfo(BaseModel):
    confrontantes: List[ConfrontanteItem] = Field(default_factory=list)
    total_confrontantes: int = 0
    all_cited: bool = False
    any_contested: bool = False


class RegisteredOwnerInfo(BaseModel):
    name: Optional[str] = Field(None, description="Nome do proprietário registral constante na matrícula")
    cpf_cnpj: Optional[str] = Field(None, description="CPF ou CNPJ do proprietário registral")
    matricula_number: Optional[str] = Field(None, description="Matrícula do imóvel usucapiendo")
    citation_status: str = Field("pendente", description="'citado_pessoalmente', 'citado_edital', 'pendente', 'nao_localizado'")
    manifestation_status: str = Field("nao_manifestado", description="'anuencia_expressa', 'contestacao', 'inercia_silencio', 'nao_manifestado'")


class PublicNoticeEntity(BaseModel):
    entity: str = Field(..., description="'União', 'Estado do Pará', 'Município'")
    notified: bool = Field(False, description="Se a Fazenda Pública foi notificada/citada")
    manifestation: str = Field("não_detectada", description="'sem_interesse', 'com_interesse_ou_oposicao', 'manifestada', 'não_detectada'")
    date: Optional[str] = Field(None, description="Data da manifestação")
    evidence_snippet: Optional[str] = Field(None, description="Trecho extraído de comprovação")


class PublicNoticesInfo(BaseModel):
    uniao: PublicNoticeEntity = Field(default_factory=lambda: PublicNoticeEntity(entity="União"))
    estado: PublicNoticeEntity = Field(default_factory=lambda: PublicNoticeEntity(entity="Estado do Pará"))
    municipio: PublicNoticeEntity = Field(default_factory=lambda: PublicNoticeEntity(entity="Município"))
    all_notified: bool = False
    any_public_domain_opposition: bool = False


class BoundaryConflictItem(BaseModel):
    conflict_type: str = Field(..., description="'boundary_overlap', 'third_party_opposition', 'area_discrepancy', 'public_domain_claim', 'other'")
    parties_involved: List[str] = Field(default_factory=list)
    description: str = Field(..., description="Descrição detalhada do conflito de limites ou área")
    severity: str = Field("high", description="'critical', 'high', 'medium'")


class ConflictsInfo(BaseModel):
    conflicts: List[BoundaryConflictItem] = Field(default_factory=list)
    has_active_conflicts: bool = False


class AdversePossessionV1Result(BaseModel):
    schema_version: str = "adverse-possession/v1"
    claimant: Optional[ClaimantInfo] = None
    subject_property: Optional[SubjectPropertyInfo] = None
    land_area: LandAreaInfo = Field(default_factory=LandAreaInfo)
    possession_details: Optional[PossessionDetailsInfo] = None
    confrontantes: ConfrontantesInfo = Field(default_factory=ConfrontantesInfo)
    registered_owner: Optional[RegisteredOwnerInfo] = None
    public_notices: PublicNoticesInfo = Field(default_factory=PublicNoticesInfo)
    conflicts: ConflictsInfo = Field(default_factory=ConflictsInfo)
    unknown_location_edital: bool = Field(False, description="Se houve citação por edital devido a réu em local incerto")
    possession_time_years_claimed: Optional[float] = Field(None, description="Tempo de posse alegado em anos")
    possession_type: Optional[str] = Field(None, description="Tipo: 'extraordinaria', 'ordinaria', 'urbana', 'rural', 'familiar', 'extrajudicial'")
    ata_notarial_present: bool = Field(False, description="Se há ata notarial juntada (para usucapião extrajudicial)")
    extractor_metadata: Dict[str, Any] = Field(default_factory=dict)


AdversePossessionProfileData = AdversePossessionV1Result


class DomainValidationResult(BaseModel):
    profile_name: str = "adverse-possession/v1"
    completeness_score: float = Field(..., description="Pontuação de completude de 0.0 a 1.0")
    status: str = Field(..., description="Status do perfil: 'COMPLETE', 'INCOMPLETE', 'NON_COMPLIANT'")
    missing_mandatory_docs: List[str] = Field(default_factory=list)
    missing_citations: List[str] = Field(default_factory=list)
    detected_conflicts: List[Dict[str, Any]] = Field(default_factory=list)
    procedural_risks: List[str] = Field(default_factory=list)
    validation_details: Dict[str, Any] = Field(default_factory=dict)


# Regex Patterns for adverse-possession/v1
RE_CPF = re.compile(r"\b\d{3}\.\d{3}\.\d{3}\-\d{2}\b")
RE_CONFRONTANTE = re.compile(
    r"(?i)\b(?:confrontante[s]?|vizinh[oa]s?|confronta|limita-se|limita|extremando|dividindo-se|divide)"
    r"(?:\s+(?:(?:ao|a)\s+)?(?:norte|sul|leste|oeste|setentri[ãa]o|setentrional|meridiano|meridional|nascente|oriente|poente|ocidente|frente|fundos|lado\s+direito|lado\s+esquerdo))?"
    r"(?:\s+com)?"
    r"(?:\s+(?:(?:ao|a)\s+)?(?:norte|sul|leste|oeste|setentri[ãa]o|setentrional|meridiano|meridional|nascente|oriente|poente|ocidente|frente|fundos|lado\s+direito|lado\s+esquerdo))?"
    r"[\s\:\,\-\–]*"
    r"([A-ZÀ-Ý][a-zà-ýA-ZÀ-Ý\s]{2,60}?)"
    r"(?=\(|\,|\.|\;|\bcpf|\brg|\bcitad|\banu[êe]n|$)",
)
RE_MEMORIAL = re.compile(
    r"(?i)\b(?:memorial descritivo|planta topogr[aá]fica|levantamento topogr[aá]fico|per[ií]metro|pol[ií]gono)\b"
)
RE_ART_RRT = re.compile(
    r"(?i)\b(?:art|rrt)\s*(?:n[ºo\.\°]*)?\s*([A-Z0-9\-\.\/]{5,20})\b"
)
RE_AREA_SQM = re.compile(
    r"(?i)\b(\d+(?:[\.\,]\d+)*)\s*(?:m²|metros quadrado[s]?|m2)\b"
)
RE_AREA_HA = re.compile(
    r"(?i)\b(\d+(?:[\.\,]\d+)*)\s*(?:ha|hectare[s]?)\b"
)
RE_EDITAL_INCERTO = re.compile(
    r"(?i)\b(?:local incerto|n[aã]o sabido|edital de cita[çc][aã]o|cita[çc][aã]o edital[ií]cia)\b"
)


def _get_local_sentence_block(full_text: str, start: int, end: int) -> str:
    left = 0
    left_matches = [m.start() for m in re.finditer(r"(?<!\d)\.(?!\d)|;|\n\s*\n", full_text[:start])]
    if left_matches:
        left = max(left_matches) + 1
    else:
        left = max(0, start - 150)

    right = len(full_text)
    right_matches = [m.start() for m in re.finditer(r"(?<!\d)\.(?!\d)|;|\n\s*\n", full_text[end:])]
    if right_matches:
        right = end + min(right_matches)
    else:
        right = min(len(full_text), end + 150)

    return full_text[left:right]


def _parse_float_number(val_str: str) -> Optional[float]:
    if not val_str:
        return None
    val = val_str.strip()
    if "." in val and "," in val:
        if val.rfind(",") > val.rfind("."):
            val = val.replace(".", "").replace(",", ".")
        else:
            val = val.replace(",", "")
    elif "," in val:
        val = val.replace(",", ".")
    try:
        return float(val)
    except ValueError:
        return None



def _clean_confrontante_name(raw_name: str) -> Optional[str]:
    name = re.sub(
        r"(?i)^(?:ao\s+)?(?:norte|sul|leste|oeste|setentri[ãa]o|setentrional|meridiano|meridional|nascente|oriente|poente|ocidente|frente|fundos|lado\s+direito|lado\s+esquerdo)\s*[\:\,\-]*\s*",
        "",
        raw_name,
    ).strip()

    noise_patterns = [
        r"(?i)\s+\b(?:foi|apresentou|apresentada|alegando|citad[oa]|com|que|para|este|esta)\b.*$",
        r"(?i)\s+\b(?:invas[aã]o|divisa|limite|terreno|im[oó]vel)\b.*$",
    ]
    for np in noise_patterns:
        name = re.sub(np, "", name).strip()

    name = re.sub(r"\s+", " ", name).strip()
    if name.lower() in ("apresentada", "alegando", "citado", "citada", "invasao", "invasão", "confrontante"):
        return None
    if len(name) <= 3:
        return None
    return name


def _parse_cardinal_direction(text: str) -> str:
    t = text.lower()
    if re.search(r"\b(?:norte|setentri[ãa]o|setentrional)\b", t):
        return "norte"
    elif re.search(r"\b(?:sul|meridiano|meridional)\b", t):
        return "sul"
    elif re.search(r"\b(?:leste|nascente|oriente)\b", t):
        return "leste"
    elif re.search(r"\b(?:oeste|poente|ocidente)\b", t):
        return "oeste"
    return "confrontante_geral"


def _collect_full_text(documents: List[Dict[str, Any]], case_base: Optional[Dict[str, Any]] = None) -> str:
    parts = []
    if isinstance(case_base, dict):
        for k in ("resumo", "classes", "assunto", "polo_ativo", "polo_passivo", "observacoes", "case_class"):
            val = case_base.get(k)
            if isinstance(val, str):
                parts.append(val)
            elif isinstance(val, list):
                parts.extend([str(item) for item in val if item])
    elif isinstance(case_base, str):
        parts.append(case_base)
    for doc in documents or []:
        if isinstance(doc, dict):
            for field in ("texto", "teor", "conteudo", "titulo", "tipo"):
                val = doc.get(field)
                if isinstance(val, str) and val.strip():
                    parts.append(val)
            pages = doc.get("pages") or doc.get("paginas_estruturadas") or []
            for p in pages:
                if isinstance(p, dict) and p.get("text"):
                    parts.append(str(p.get("text")))
    return "\n\n".join(parts)


def extract_adverse_possession_v1(
    dossier_data: Any,
    case_base: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    documents = []
    cb = case_base if isinstance(case_base, dict) else None

    if isinstance(dossier_data, dict):
        documents = dossier_data.get("documents") or dossier_data.get("documentos") or dossier_data.get("pieces") or []
        if not cb:
            cb = dossier_data.get("base") or dossier_data.get("case_base") or dossier_data
    elif isinstance(dossier_data, list):
        documents = dossier_data

    full_text = _collect_full_text(documents, cb)

    # 1. Claimant & Registered Owner
    claimant_name = None
    claimant_cpf = None
    if cb and cb.get("polo_ativo"):
        pa = cb.get("polo_ativo")
        claimant_name = pa if isinstance(pa, str) else (str(pa[0]) if pa else None)
    if not claimant_name:
        m_cl = re.search(r"(?i)\b(?:requerente|autor[a]?|usucapiente)\b[\s\:\,\-\–]+([A-ZÀ-Ý][a-zà-ýA-ZÀ-Ý\s]{2,60}?)(?=\,|\.|\;|\bcpf|$)", full_text)
        if m_cl:
            claimant_name = m_cl.group(1).strip()
    
    cpfs = RE_CPF.findall(full_text)
    if cpfs:
        claimant_cpf = cpfs[0]

    claimant_info = ClaimantInfo(
        name=claimant_name,
        cpf_cnpj=claimant_cpf,
    )

    reg_owner_name = None
    m_ro = re.search(r"(?i)\b(?:propriet[aá]rio registral|titular do dom[ií]nio|consta em nome de)\b[\s\:\,\-\–]+([A-ZÀ-Ý][a-zà-ýA-ZÀ-Ý\s]{2,60}?)(?=\,|\.|\;|\bcpf|$)", full_text)
    if m_ro:
        cand = m_ro.group(1).strip()
        if cand.lower() not in ("realizada", "efetuada", "pendente", "citado", "citada"):
            reg_owner_name = cand
    
    reg_owner_cit_status = "pendente"
    if re.search(r"(?i)\bcita[çc][aã]o do propriet[aá]rio registral realizada\b", full_text) or re.search(r"(?i)\bpropriet[aá]rio registral\b[^.\n]{0,80}?\bcitad[oa]\b", full_text):
        reg_owner_cit_status = "citado_pessoalmente"
    elif reg_owner_name and re.search(rf"(?i){re.escape(reg_owner_name)}[^.\n]{{0,80}}?\bcitad[oa]\b", full_text):
        reg_owner_cit_status = "citado_pessoalmente"
    elif re.search(r"(?i)\bpropriet[aá]rio registral\b[^.\n]{0,80}?\bcitad[oa] por edital\b", full_text):
        reg_owner_cit_status = "citado_edital"

    reg_owner_info = RegisteredOwnerInfo(
        name=reg_owner_name,
        citation_status=reg_owner_cit_status,
    )

    # 2. Confrontantes
    confrontantes_list = []
    seen_names = set()

    for m in RE_CONFRONTANTE.finditer(full_text):
        raw_match = m.group(0)
        raw_name = m.group(1).strip()
        name_clean = _clean_confrontante_name(raw_name)
        
        if name_clean and name_clean.lower() not in seen_names:
            seen_names.add(name_clean.lower())
            
            card = _parse_cardinal_direction(raw_match)
            local_text = _get_local_sentence_block(full_text, m.start(), m.end())

            cit_status = "pendente"
            if re.search(r"(?i)\bcitad[oa]\s+pessoalmente|cita[çc][aã]o\s+pessoal|citad[oa]\s+com\s+anu[êe]ncia\b", local_text):
                cit_status = "citado_pessoalmente"
            elif re.search(r"(?i)\bcitad[oa]\s+por\s+edital|cita[çc][aã]o\s+edital[ií]cia\b", local_text):
                cit_status = "citado_edital"
            elif re.search(r"(?i)\bn[aã]o\s+localizad[oa]|n[aã]o\s+encontrad[oa]\b", local_text):
                cit_status = "nao_localizado"

            man_status = "nao_manifestado"
            if re.search(r"(?i)\banu[êe]ncia\s+expressa|concord[âa]ncia|com\s+anu[êe]ncia\b", local_text):
                man_status = "anuencia_expressa"
            elif re.search(r"(?i)\bcontestou|contesta[cç][aã]o\b", local_text):
                man_status = "contestacao"
            elif re.search(r"(?i)\bin[ée]rcia|sil[êe]ncio\s+decorrido|decorrido\s+in[ée]rcia\b", local_text):
                man_status = "inercia_silencio"

            cpf_match = RE_CPF.search(local_text)
            cpf_val = cpf_match.group(0) if cpf_match else None

            confrontantes_list.append(
                ConfrontanteItem(
                    name=name_clean,
                    cpf_cnpj=cpf_val,
                    cardinal_direction=card,
                    citation_status=cit_status,
                    manifestation_status=man_status,
                )
            )

    all_cited = len(confrontantes_list) > 0 and all(
        c.citation_status in ("citado_pessoalmente", "citado_edital") for c in confrontantes_list
    )
    any_contested = any(c.manifestation_status == "contestacao" for c in confrontantes_list)

    confrontantes_info = ConfrontantesInfo(
        confrontantes=confrontantes_list,
        total_confrontantes=len(confrontantes_list),
        all_cited=all_cited,
        any_contested=any_contested,
    )

    # 3. Land Area & Technical Responsibility
    area_m2 = None
    m_sqm = RE_AREA_SQM.search(full_text)
    if m_sqm:
        area_m2 = _parse_float_number(m_sqm.group(1))

    area_ha = None
    m_ha = RE_AREA_HA.search(full_text)
    if m_ha:
        area_ha = _parse_float_number(m_ha.group(1))

    perim_m = None
    m_per = re.search(r"(?i)per[ií]metro\s*[\:\=]?\s*(?:de)?\s*(\d+(?:[\.\,]\d+)*)\s*(?:m|metro[s]?)\b", full_text)
    if m_per:
        perim_m = _parse_float_number(m_per.group(1))

    has_memorial = bool(RE_MEMORIAL.search(full_text))
    has_map = bool(re.search(r"(?i)\bplanta topogr[aá]fica\b", full_text))

    art_rrt = None
    m_art = RE_ART_RRT.search(full_text)
    if m_art:
        art_rrt = m_art.group(1)

    prof_name = None
    m_prof = re.search(r"(?i)\b(?:engenheiro|agrimensor|respons[aá]vel t[eé]cnico)\b[\s\:\,\-\–]+([A-ZÀ-Ý][a-zà-ýA-ZÀ-Ý\s]{2,60})(?=\,|\.|\;|\bcrea|\bcau|\bart|\brrt|\bcpf|$)", full_text)
    if m_prof:
        prof_candidate = m_prof.group(1).strip()
        prof_name = re.sub(r"(?i)\s+\b(?:crea|cau|art|rrt)\b.*$", "", prof_candidate).strip()
        if len(prof_name) <= 2:
            prof_name = None

    tech_resp = TechnicalResponsibility(
        art_rrt_number=art_rrt,
        professional_name=prof_name,
    )

    land_area_info = LandAreaInfo(
        area_m2=area_m2,
        area_hectares=area_ha,
        perimeter_meters=perim_m,
        memorial_descritivo_present=has_memorial,
        topographic_map_present=has_map,
        technical_responsibility=tech_resp,
    )

    subject_property_info = SubjectPropertyInfo(
        property_description="Imóvel Usucapiendo",
        area_m2=area_m2,
        area_hectares=area_ha,
        perimeter_meters=perim_m,
        memorial_descritivo_present=has_memorial,
        topographic_map_present=has_map,
        technical_responsibility=tech_resp,
    )

    # 4. Public Notices (Fazendas Públicas)
    has_combined_notice = bool(
        re.search(
            r"(?i)\bnotifica[cç][aã]o da uni[aã]o,?\s*(?:do\s+)?estado\s*(?:do par[aá])?\s*e\s*(?:do\s+)?munic[ií]pio\s*(?:foram|restaram|foram\s+devidamente)?\s*(?:realizadas?|efetuadas?|efetivadas?)\b",
            full_text,
        )
    )

    def _parse_public_entity(entity_name: str, regex_kw: str) -> PublicNoticeEntity:
        notified = False
        manifestation = "não_detectada"
        best_snippet = None

        if has_combined_notice:
            notified = True

        matches = list(re.finditer(rf"(?i){regex_kw}\b", full_text))
        if matches:
            # Hierarchy: com_interesse_ou_oposicao > sem_interesse > manifestada > não_detectada
            for m in matches:
                snip = _get_local_sentence_block(full_text, m.start(), m.end())
                is_certidao = bool(re.search(r"(?i)\b(?:certid[aã]o|termo|comprovante|of[ií]cio)\b", snip))
                is_initial_request = bool(
                    re.search(
                        r"(?i)\b(?:requer|requer-se|pedido|pedidos|pretende|solicita)\b[^.\n]{0,50}?\b(?:notifica[cç][aã]o|cita[çc][aã]o)\b",
                        snip,
                    )
                ) and not is_certidao

                is_non_opposition = bool(
                    re.search(
                        r"(?i)\b(?:n[aã]o\s+(?:\w+\s+){0,3}?oposi[cç][aã]o|sem\s+(?:\w+\s+){0,3}?oposi[cç][aã]o|aus[êe]ncia\s+de\s+(?:\w+\s+){0,3}?oposi[cç][aã]o|sem\s+(?:\w+\s+){0,3}?oposi[cç][aõ]es|n[aã]o\s+(?:\w+\s+){0,3}?op[oõ]e[m]?|n[aã]o\s+(?:possui|possuir|tem|ter)\s+(?:\w+\s+){0,3}?interesse|sem\s+(?:\w+\s+){0,3}?interesse|nada\s+a\s+opor|aus[êe]ncia\s+de\s+(?:\w+\s+){0,3}?interesse|desinteresse)\b",
                        snip,
                    )
                )

                if re.search(r"(?i)\b(?:contesta[cç][aã]o|op[ôo]s contesta[cç][aã]o|contestou|com interesse|oposi[cç][aã]o|impugna[cç][aã]o|terreno de marinha|terreno p[uú]blico)\b", snip) and not is_non_opposition:
                    manifestation = "com_interesse_ou_oposicao"
                    notified = True
                    best_snippet = snip
                    break  # Highest priority, stop iteration

                elif is_non_opposition:
                    if manifestation != "com_interesse_ou_oposicao":
                        manifestation = "sem_interesse"
                        notified = True
                        best_snippet = snip

                elif (re.search(r"(?i)\b(?:manifestou|peti[cç][aã]o|parecer|manifestac[aã]o)\b", snip) or is_certidao) and not is_initial_request:
                    if manifestation == "não_detectada":
                        manifestation = "manifestada"
                        notified = True
                        best_snippet = snip
                else:
                    if bool(
                        re.search(
                            rf"(?i)\b(?:certid[aã]o de notifica[cç][aã]o|certid[aã]o de cita[cç][aã]o|of[ií]cio expedi[d]o|notificad[oa]|citad[oa])\s*(?:da|do)?\s*{regex_kw}\b",
                            full_text,
                        )
                    ):
                        notified = True
                        if not best_snippet:
                            best_snippet = snip
                        if not is_initial_request and manifestation == "não_detectada":
                            manifestation = "manifestada"

        return PublicNoticeEntity(
            entity=entity_name,
            notified=notified,
            manifestation=manifestation,
            evidence_snippet=best_snippet,
        )

    uniao = _parse_public_entity("União", r"(?:uni[aã]o|fazenda nacional|advocacia-geral da uni[aã]o|agu)")
    estado = _parse_public_entity("Estado do Pará", r"(?:estado do par[aá]|fazenda estadual|pge|procuradoria geral do estado)")
    municipio = _parse_public_entity("Município", r"(?:munic[ií]pio|prefeitura|fazenda municipal)")

    all_notified = uniao.notified and estado.notified and municipio.notified
    any_public_opposition = any(
        e.manifestation == "com_interesse_ou_oposicao" for e in (uniao, estado, municipio)
    )

    public_notices_info = PublicNoticesInfo(
        uniao=uniao,
        estado=estado,
        municipio=municipio,
        all_notified=all_notified,
        any_public_domain_opposition=any_public_opposition,
    )

    # 5. Conflicts
    conflicts_list = []
    if any_public_opposition:
        conflicts_list.append(
            BoundaryConflictItem(
                conflict_type="public_domain_claim",
                description="Oposição ou manifestação de interesse de Ente Público (Fazenda Pública / Terreno de Marinha)",
                severity="critical",
            )
        )
    if any_contested:
        conflicts_list.append(
            BoundaryConflictItem(
                conflict_type="third_party_opposition",
                description="Contestação formalmente apresentada por confrontante ou terceiro interessado",
                severity="high",
            )
        )
    if re.search(r"(?i)\bsobreposi[cç][aã]o de [aá]rea|superposi[cç][aã]o\b", full_text):
        conflicts_list.append(
            BoundaryConflictItem(
                conflict_type="boundary_overlap",
                description="Alegação de sobreposição de limites ou áreas com terreno vizinho",
                severity="high",
            )
        )
    if re.search(r"(?i)\bdiverg[êe]ncia de [aá]rea|área divergente\b", full_text):
        conflicts_list.append(
            BoundaryConflictItem(
                conflict_type="area_discrepancy",
                description="Divergência entre área medida e a registrada em cartório",
                severity="medium",
            )
        )

    conflicts_info = ConflictsInfo(
        conflicts=conflicts_list,
        has_active_conflicts=len(conflicts_list) > 0,
    )

    # 6. Possession Details & Edital
    unknown_edital = bool(RE_EDITAL_INCERTO.search(full_text))

    possession_years = None
    m_yrs = re.search(r"(?i)\b(?:posse|possui|reside|ocupa[çc][aã]o)\b[^.\n]{0,40}?\b(\d+(?:[\.\,]\d+)?)\s*anos\b", full_text)
    if m_yrs:
        try:
            possession_years = float(m_yrs.group(1).replace(",", "."))
        except ValueError:
            possession_years = None

    possession_type = "extraordinaria"
    if re.search(r"(?i)\busucapi[aã]o extrajudicial\b", full_text):
        possession_type = "extrajudicial"
    elif re.search(r"(?i)\busucapi[aã]o urbana|especial urbana\b", full_text):
        possession_type = "urbana"
    elif re.search(r"(?i)\busucapi[aã]o rural|especial rural\b", full_text):
        possession_type = "rural"
    elif re.search(r"(?i)\busucapi[aã]o ordin[aá]ria\b", full_text):
        possession_type = "ordinaria"
    elif re.search(r"(?i)\busucapi[aã]o familiar\b", full_text):
        possession_type = "familiar"

    has_justo_titulo = bool(re.search(r"(?i)\bjusto t[ií]tulo|promessa de compra|contrato particular|compromisso de compra\b", full_text))
    has_boa_fe = bool(re.search(r"(?i)\bboa[\s\-]f[eé]\b", full_text))
    has_ata_notarial = bool(re.search(r"(?i)\bata notarial\b", full_text))

    possession_details_info = PossessionDetailsInfo(
        possession_time_years_claimed=possession_years,
        justo_titulo=has_justo_titulo,
        boa_fe=has_boa_fe or (possession_type in ("urbana", "rural", "extraordinaria")),
        mansa_e_pacifica=not any_contested,
        ininterrupta=True,
        modality=possession_type,
    )

    res = AdversePossessionV1Result(
        schema_version="adverse-possession/v1",
        claimant=claimant_info,
        subject_property=subject_property_info,
        land_area=land_area_info,
        possession_details=possession_details_info,
        confrontantes=confrontantes_info,
        registered_owner=reg_owner_info,
        public_notices=public_notices_info,
        conflicts=conflicts_info,
        unknown_location_edital=unknown_edital,
        possession_time_years_claimed=possession_years,
        possession_type=possession_type,
        ata_notarial_present=has_ata_notarial,
        extractor_metadata={
            "documents_analyzed": len(documents or []),
            "has_case_base": cb is not None,
        },
    )
    return res.model_dump()


def validate_adverse_possession_v1(profile_data: Dict[str, Any]) -> DomainValidationResult:
    missing_docs: List[str] = []
    missing_citations: List[str] = []
    detected_conflicts: List[Dict[str, Any]] = []
    procedural_risks: List[str] = []
    details: Dict[str, Any] = {}

    p_data = profile_data if isinstance(profile_data, dict) else {}
    confrontantes_info = p_data.get("confrontantes") if isinstance(p_data.get("confrontantes"), dict) else {}
    land_area = p_data.get("land_area") if isinstance(p_data.get("land_area"), dict) else {}
    public_notices = p_data.get("public_notices") if isinstance(p_data.get("public_notices"), dict) else {}
    conflicts_info = p_data.get("conflicts") if isinstance(p_data.get("conflicts"), dict) else {}
    reg_owner = p_data.get("registered_owner") if isinstance(p_data.get("registered_owner"), dict) else {}
    possession_details = p_data.get("possession_details") if isinstance(p_data.get("possession_details"), dict) else {}

    possession_years = p_data.get("possession_time_years_claimed") or possession_details.get("possession_time_years_claimed")
    if not isinstance(possession_years, (int, float)):
        try:
            possession_years = float(possession_years) if possession_years is not None else None
        except (ValueError, TypeError):
            possession_years = None

    modality = p_data.get("possession_type") or possession_details.get("modality") or "extraordinaria"
    if not isinstance(modality, str):
        modality = "extraordinaria"

    has_edital = bool(p_data.get("unknown_location_edital", False))
    has_ata = bool(p_data.get("ata_notarial_present", False))

    # 1. Mandatory Citations Check
    total_confrontantes = confrontantes_info.get("total_confrontantes", 0) if isinstance(confrontantes_info, dict) else 0
    all_confrontantes_cited = confrontantes_info.get("all_cited", False) if isinstance(confrontantes_info, dict) else False
    if total_confrontantes == 0 or not all_confrontantes_cited:
        missing_citations.append("Citação Pessoal / Editalícia dos Confrontantes (Vizinhos Lindeiros)")

    uniao = public_notices.get("uniao") if isinstance(public_notices.get("uniao"), dict) else {}
    estado = public_notices.get("estado") if isinstance(public_notices.get("estado"), dict) else {}
    municipio = public_notices.get("municipio") if isinstance(public_notices.get("municipio"), dict) else {}

    if not uniao.get("notified"):
        missing_citations.append("Notificação / Citação da União (Fazenda Nacional)")
    if not estado.get("notified"):
        missing_citations.append("Notificação / Citação do Estado do Pará (Fazenda Estadual)")
    if not municipio.get("notified"):
        missing_citations.append("Notificação / Citação do Município (Fazenda Municipal)")

    owner_cit_status = reg_owner.get("citation_status", "pendente") if isinstance(reg_owner, dict) else "pendente"
    if owner_cit_status in ("pendente", "nao_localizado") and not has_edital:
        missing_citations.append("Citação do Proprietário Registral constante da Matrícula")

    # 2. Mandatory Documents Check
    has_memorial = land_area.get("memorial_descritivo_present", False) if isinstance(land_area, dict) else False
    tech_resp = land_area.get("technical_responsibility") if isinstance(land_area.get("technical_responsibility"), dict) else {}
    art_rrt = tech_resp.get("art_rrt_number") if isinstance(tech_resp, dict) else None

    if not has_memorial or not art_rrt:
        missing_docs.append("Planta Topográfica e Memorial Descritivo com ART/RRT quitada")

    if modality == "extrajudicial" and not has_ata:
        missing_docs.append("Ata Notarial Lavrada por Tabelião de Notas (Art. 216-A LRP)")

    # 3. Temporal Threshold and Modality Rules Validation
    thresholds = {
        "extraordinaria": 15.0,
        "ordinaria": 10.0,
        "urbana": 5.0,
        "especial_urbana": 5.0,
        "rural": 5.0,
        "especial_rural": 5.0,
        "familiar": 2.0,
        "extrajudicial": 5.0,
    }
    req_years = thresholds.get(modality, 15.0)

    if possession_years is None:
        procedural_risks.append(f"TEMPO_DE_POSSE_NAO_COMPROVADO: Ausência de indicação do tempo de posse contínua para a modalidade {modality}")
    elif possession_years < req_years:
        procedural_risks.append(f"TEMPO_DE_POSSE_INSUFICIENTE: Posse alegada ({possession_years} anos) é inferior ao mínimo legal exigido para usucapião {modality} ({req_years} anos)")

    area_sqm = land_area.get("area_m2") if isinstance(land_area, dict) else None
    if not isinstance(area_sqm, (int, float)):
        try:
            area_sqm = float(area_sqm) if area_sqm is not None else None
        except (ValueError, TypeError):
            area_sqm = None

    area_ha = land_area.get("area_hectares") if isinstance(land_area, dict) else None
    if not isinstance(area_ha, (int, float)):
        try:
            area_ha = float(area_ha) if area_ha is not None else None
        except (ValueError, TypeError):
            area_ha = None

    if modality in ("urbana", "especial_urbana") and area_sqm and area_sqm > 250.0:
        procedural_risks.append(f"EXCESSO_DE_AREA_URBANA: Área imóvel ({area_sqm} m²) excede o limite máximo constitucional de 250 m² para usucapião urbana (Art. 183 CF)")

    if modality in ("rural", "especial_rural"):
        if (area_ha and area_ha > 50.0) or (area_sqm and area_sqm > 500000.0):
            procedural_risks.append("EXCESSO_DE_AREA_RURAL: Área imóvel excede o teto constitucional de 50 hectares para usucapião rural (Art. 191 CF)")

    if modality == "ordinaria" and not possession_details.get("justo_titulo"):
        procedural_risks.append("AUSENCIA_JUSTO_TITULO: Usucapião ordinária exige comprovação de justo título e boa-fé (Art. 1.242 CC)")

    # 4. Conflicts & Public Domain Opposition
    any_public_opp = public_notices.get("any_public_domain_opposition", False) if isinstance(public_notices, dict) else False
    if any_public_opp:
        procedural_risks.append("OPOSICAO_FAZENDA_PUBLICA: Manifestação de oposição ou alegação de bem público / terreno de marinha impelem o indeferimento ou remessa às vias ordinárias")
        
    if modality == "extrajudicial" and (any_public_opp or (isinstance(confrontantes_info, dict) and confrontantes_info.get("any_contested"))):
        procedural_risks.append("INCOMPATIBILIDADE_EXTRAJUDICIAL_LITIGIO: Existência de contestação ou oposição de terceiros/Fazenda veda a via extrajudicial no cartório (Art. 216-A § 10 LRP)")

    conflict_items = conflicts_info.get("conflicts") if isinstance(conflicts_info, dict) and isinstance(conflicts_info.get("conflicts"), list) else []
    for c in conflict_items:
        if isinstance(c, dict):
            detected_conflicts.append(c)
        elif hasattr(c, "model_dump"):
            detected_conflicts.append(c.model_dump())
        elif isinstance(c, str):
            detected_conflicts.append({"conflict_type": "other", "description": c})

    # 5. Completeness Score & Final Status Calculation
    tot_checks = len(missing_citations) + len(missing_docs)
    
    non_compliant_keywords = (
        "INSUFICIENTE",
        "TEMPO_DE_POSSE_INSUFICIENTE",
        "EXCESSO_DE_AREA",
        "OPOSICAO_FAZENDA_PUBLICA",
        "INCOMPATIBILIDADE_EXTRAJUDICIAL",
        "TEMPO_DE_POSSE_NAO_COMPROVADO",
        "AUSENCIA_JUSTO_TITULO",
    )
    is_non_compliant = any(any(kw in r for kw in non_compliant_keywords) for r in procedural_risks)

    if is_non_compliant:
        status = "NON_COMPLIANT"
        completeness_score = max(0.0, round(0.5 - (tot_checks * 0.1), 2))
    elif tot_checks == 0 and len(procedural_risks) == 0:
        status = "COMPLETE"
        completeness_score = 1.0
    else:
        status = "INCOMPLETE"
        completeness_score = max(0.0, round(1.0 - (tot_checks * 0.15), 2))

    details["modality_evaluated"] = modality
    details["claimed_possession_years"] = possession_years
    details["required_possession_years"] = req_years

    return DomainValidationResult(
        profile_name="adverse-possession/v1",
        completeness_score=completeness_score,
        status=status,
        missing_mandatory_docs=missing_docs,
        missing_citations=missing_citations,
        detected_conflicts=detected_conflicts,
        procedural_risks=procedural_risks,
        validation_details=details,
    )
