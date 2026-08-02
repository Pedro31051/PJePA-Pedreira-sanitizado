"""Domain Profile inventory/v1: Schema-first entities, extraction and rite validation for Inventory processes.

Provides data models, extraction cascade parsing, and procedural rite validation for
inventários judiciais, extrajudiciais e arrolamentos (sumário e comum).
"""

import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class DeathCertificateInfo(BaseModel):
    number: Optional[str] = Field(None, description="Número do termo de óbito")
    book: Optional[str] = Field(None, description="Livro do registro de óbito")
    page: Optional[str] = Field(None, description="Folha do registro de óbito")
    registry_office: Optional[str] = Field(None, description="Cartório de registro civil")
    death_date: Optional[str] = Field(None, description="Data do falecimento")
    place_of_death: Optional[str] = Field(None, description="Local do falecimento")


class DeceasedInfo(BaseModel):
    name: Optional[str] = Field(None, description="Nome do inventariado (de cujus / autor da herança)")
    cpf: Optional[str] = Field(None, description="CPF do de cujus")
    rg: Optional[str] = Field(None, description="RG do de cujus")
    marital_status: Optional[str] = Field(None, description="Estado civil ao tempo do óbito")
    matrimonial_regime: Optional[str] = Field(None, description="Regime de bens do casamento, se aplicável")
    last_residence: Optional[str] = Field(None, description="Último domicílio do falecido")
    death_certificate: Optional[DeathCertificateInfo] = None


class AuthorPartyInfo(BaseModel):
    name: Optional[str] = Field(None, description="Nome do autor / inventariante")
    cpf_cnpj: Optional[str] = Field(None, description="CPF ou CNPJ do autor/inventariante")
    qualification: Optional[str] = Field(None, description="Qualificação civil")
    relationship: Optional[str] = Field(None, description="Relação com o falecido (ex: viúva, filho, credor)")
    is_inventariante: bool = Field(True, description="Se foi nomeado ou atua como inventariante")


class PartiesInfo(BaseModel):
    author: Optional[AuthorPartyInfo] = None
    deceased: Optional[DeceasedInfo] = None


class HeirItem(BaseModel):
    name: str = Field(..., description="Nome completo do herdeiro ou meeiro")
    cpf: Optional[str] = Field(None, description="CPF do herdeiro")
    role: str = Field(..., description="Papel: 'meeiro', 'herdeiro', 'meeiro_e_herdeiro', 'cessionario', 'legatario'")
    kinship: Optional[str] = Field(None, description="Grau de parentesco (ex: filho, cônjuge, neto, irmão)")
    marital_status: Optional[str] = Field(None, description="Estado civil do herdeiro")
    spouse_name: Optional[str] = Field(None, description="Nome do cônjuge/companheiro do herdeiro, se houver")
    is_incapable: bool = Field(False, description="Se o herdeiro é menor ou incapaz")
    renunciation: bool = Field(False, description="Se renunciou à herança")
    qualification: Dict[str, Any] = Field(default_factory=dict, description="Dados adicionais de qualificação")


class HeirsInfo(BaseModel):
    meeiro: Optional[HeirItem] = Field(None, description="Cônjuge supérstite com direito à meação")
    heirs: List[HeirItem] = Field(default_factory=list, description="Lista de herdeiros necessários/testamentários")
    total_heirs_count: int = 0
    has_incapable_heirs: bool = Field(False, description="Se há herdeiros menores ou incapazes")


class ShareItem(BaseModel):
    heir_name: str = Field(..., description="Nome do herdeiro ou meeiro")
    share_percentage: Optional[float] = Field(None, description="Porcentagem do quinhão (0.0 a 100.0)")
    share_monetary_value: Optional[float] = Field(None, description="Valor monetário estimado do quinhão")
    assigned_rights: bool = Field(False, description="Se realizou cessão de direitos hereditários")
    assignee_name: Optional[str] = Field(None, description="Nome do cessionário, se houver cessão")
    description: Optional[str] = Field(None, description="Descrição detalhada do quinhão/partilha")


class SharesInfo(BaseModel):
    shares: List[ShareItem] = Field(default_factory=list)
    proposed_partition_present: bool = Field(False, description="Se há plano de partilha apresentado nos autos")
    partition_status: str = Field("pendente", description="Status da partilha: 'pendente', 'homologada', 'impugnada', 'amigavel'")


class PropertyItem(BaseModel):
    description: str = Field(..., description="Descrição do bem")
    property_type: str = Field(..., description="Tipo: 'imovel_urbano', 'imovel_rural', 'veiculo', 'saldo_bancario', 'acoes', 'outro'")
    address: Optional[str] = Field(None, description="Endereço ou localização do imóvel")
    area: Optional[str] = Field(None, description="Área do imóvel")
    matricula_number: Optional[str] = Field(None, description="Número da matrícula imobiliária")
    registry_office: Optional[str] = Field(None, description="Cartório de Registro de Imóveis (CRI)")
    declared_value: Optional[float] = Field(None, description="Valor declared pelas partes")
    evaluated_value_sefaz: Optional[float] = Field(None, description="Valor avaliado pela SEFAZ/Fazenda Estadual")


class RegistrationItem(BaseModel):
    matricula_number: str = Field(..., description="Número da matrícula imobiliária")
    registry_office: Optional[str] = Field(None, description="Nome/Comarca do Cartório")
    averbations: List[str] = Field(default_factory=list, description="Averbações relevantes")
    real_encumbrances: List[str] = Field(default_factory=list, description="Ônus reais (ex: usufruto, penhora, hipoteca, alienação fiduciária)")


class PropertiesInfo(BaseModel):
    properties: List[PropertyItem] = Field(default_factory=list)
    total_declared_value: float = 0.0
    total_evaluated_value: float = 0.0


class LiabilityItem(BaseModel):
    description: str = Field(..., description="Descrição da dívida ou encargo do espólio")
    creditor_name: Optional[str] = Field(None, description="Nome do credor / habilitante")
    amount: Optional[float] = Field(None, description="Valor monetário da dívida")
    status: str = Field("pendente", description="Status: 'paga', 'pendente', 'impugnada', 'habilitada'")


class LiabilitiesInfo(BaseModel):
    liabilities: List[LiabilityItem] = Field(default_factory=list)
    total_liabilities_value: float = 0.0


class ConflictItem(BaseModel):
    conflict_type: str = Field(..., description="Tipo: 'partition_disagreement', 'heir_impugnation', 'unrecognized_heir', 'testament_contest', 'itcmd_tax_dispute', 'creditor_opposition', 'other'")
    parties_involved: List[str] = Field(default_factory=list)
    description: str = Field(..., description="Descrição detalhada do conflito")
    severity: str = Field("medium", description="Gravidade: 'critical', 'high', 'medium', 'low'")


class ConflictsInfo(BaseModel):
    conflicts: List[ConflictItem] = Field(default_factory=list)
    has_active_conflicts: bool = False


class ITCDInfo(BaseModel):
    paid: bool = Field(False, description="Se o ITCD/ITCMD foi recolhido/quitado")
    exempt: bool = Field(False, description="Se há declaração de isenção de ITCD")
    evaluation_completed: bool = Field(False, description="Se a avaliação fiscal da SEFAZ foi concluída")
    proof_document_id: Optional[str] = Field(None, description="ID do documento comprobatório")
    total_tax_amount: Optional[float] = Field(None, description="Valor total do imposto calculado")
    payment_date: Optional[str] = Field(None, description="Data do pagamento/quitação")


class InventoryV1Result(BaseModel):
    schema_version: str = "inventory/v1"
    rite_type: str = Field("inventario_comum", description="'inventario_judicial', 'extrajudicial', 'arrolamento_sumario', 'arrolamento_comum', 'inventario_comum'")
    parties: PartiesInfo = Field(default_factory=PartiesInfo)
    heirs: HeirsInfo = Field(default_factory=HeirsInfo)
    shares: SharesInfo = Field(default_factory=SharesInfo)
    properties: PropertiesInfo = Field(default_factory=PropertiesInfo)
    liabilities: LiabilitiesInfo = Field(default_factory=LiabilitiesInfo)
    registrations: List[RegistrationItem] = Field(default_factory=list)
    conflicts: ConflictsInfo = Field(default_factory=ConflictsInfo)
    testament_exists: bool = False
    itcd: ITCDInfo = Field(default_factory=ITCDInfo)
    negative_certificates_present: bool = False
    extractor_metadata: Dict[str, Any] = Field(default_factory=dict)


InventoryProfileData = InventoryV1Result


class DomainValidationResult(BaseModel):
    profile_name: str = "inventory/v1"
    completeness_score: float = Field(..., description="Pontuação de completude de 0.0 a 1.0")
    status: str = Field(..., description="Status do perfil: 'COMPLETE', 'INCOMPLETE', 'NON_COMPLIANT'")
    missing_mandatory_docs: List[str] = Field(default_factory=list)
    missing_citations: List[str] = Field(default_factory=list)
    detected_conflicts: List[Dict[str, Any]] = Field(default_factory=list)
    procedural_risks: List[str] = Field(default_factory=list)
    validation_details: Dict[str, Any] = Field(default_factory=dict)


# Regex Patterns for inventory/v1
RE_CPF = re.compile(r"\b\d{3}\.\d{3}\.\d{3}\-\d{2}\b")
RE_CNPJ = re.compile(r"\b\d{2}\.\d{3}\.\d{3}\/\d{4}\-\d{2}\b")
RE_DECEASED = re.compile(
    r"(?i)\b(?:inventariad[oa]|de cujus|falecid[oa]|falecimento\s+d[eoa]|autor(?:a)? da heran[cç]a|óbit[oo]\s+d[eoa])\b[\s\:\,\-\–]+"
    r"([A-ZÀ-Ý][a-zà-ýA-ZÀ-Ý\s]{2,60}?)(?=\,|\.|\;|\:|\bcpf|\brg|\bóbit|\bfalec|\bnasc|$)",
)
RE_DEATH_CERT = re.compile(
    r"(?i)\b(?:certid[aã]o de [oó]bito|termo|livro|folha|cart[oó]rio)\b[^.\n]{1,150}"
)
RE_MEEIRO = re.compile(
    r"(?i)\b(?:meeir[oa]|c[ôo]njuge sup[eé]rstite|vi[uú]v[oa])\b[\s\:\,\-\–]+([A-ZÀ-Ý][a-zà-ýA-ZÀ-Ý\s]{2,60}?)(?=\,|\.|\;|\bcpf|$)"
)
RE_HEIRS = re.compile(
    r"(?i)\b(?:herdeir[oa]s?|filh[oa]s?|sucessor(?:es)?|legat[aá]ri[oa]s?)\b[\s\:\,\-\–]+([A-ZÀ-Ý][a-zà-ýA-ZÀ-Ý\s]{2,80}?)(?=\,|\.|\;|\bcpf|$)"
)
RE_SPOUSE_QUALIFICATION = re.compile(
    r"(?i)\bcasad[oa]\s+(?:sob\s+o\s+regime\s+de\s+[a-zà-ý\s]{3,50}?\s+)?com\s+([A-ZÀ-Ý][a-zà-ýA-ZÀ-Ý\s]{2,60}?)(?=\,|\.|\;|\bcpf|\brg|\bc[ôo]njuge|$)"
)
RE_HEIR_SECTION = re.compile(
    r"(?i)\b(?:herdeir[oa]s?|filh[oa]s?|sucessor(?:es)?|legat[aá]ri[oa]s?|sã[o] herdeiros)\b[\s\:\,\-\–]+([^.\n;]{2,200})"
)
RE_PROPERTIES = re.compile(
    r"(?i)\b(?:im[oó]vel|terreno|apartamento|lote|matr[ií]cula|ve[ií]culo|autom[oó]vel|saldo banc[aá]rio|poupan[çc]a|conta corrente|a[çc][oõ]es)\b[^;\n]{1,150}"
)
RE_ENCUMBRANCE = re.compile(
    r"(?i)\b(?:usufruto|penhora|hipoteca|aliena[cç][aã]o fiduci[aá]ria|tombamento|inviolabilidade)\b"
)
RE_LIABILITY = re.compile(
    r"(?i)\b(?:d[ií]vida|d[eé]bito|passivo|credor|habilita[cç][aã]o de cr[eé]dito)\b[^;\n]{1,120}"
)


def _get_local_sentence_block(full_text: str, start: int, end: int) -> str:
    delims = ['.', '\n', ';']
    left_positions = [full_text.rfind(d, 0, start) for d in delims if full_text.rfind(d, 0, start) != -1]
    left = (max(left_positions) + 1) if left_positions else 0

    right_positions = [full_text.find(d, end) for d in delims if full_text.find(d, end) != -1]
    right = min(right_positions) if right_positions else len(full_text)
    return full_text[left:right]


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


RE_RENUNCIATION_VERBS = r"\b(?:renunciou|renunciaram|renunciam|renunciarem|renunciando|renunciar|ren[uú]ncia|renúncias|renunciante|renunciantes)\b"
RE_NOT_RENNOUNCED_VERBS = r"\bn[aã]o\s+(?:renunciou|renunciaram|renunciam|renunciarem|renunciar|ren[uú]ncia)\b"
RE_ACCEPTED_VERBS = r"\b(?:aceitou|aceitaram|aceita|aceitam|aceitação|aceitante|aceitou a heran[cç]a)\b"
RE_ROLE_TERMS = re.compile(
    r"(?i)^\s*(?:meeir[oa]|vi[uú]v[oa]|herdeir[oa]|c[ôo]njuge|sup[eé]rstite|inventariante|requerente|autor[a]? da herança|de cujus|falecid[oa])(?:\s+e\s+(?:meeir[oa]|herdeir[oa]|vi[uú]v[oa]|c[ôo]njuge|sup[eé]rstite))?\s*$"
)


def _clean_person_name(raw_name: str, deceased_name: Optional[str] = None) -> Optional[str]:
    if not raw_name:
        return None
    name = raw_name.strip()

    # 1. Reject action verbs, gerunds, lead pronouns, relative clauses, and non-name phrases
    if re.search(
        r"(?i)^\s*(?:fez|realizou|pretende|requer|requerem|requerendo|solicita|solicitam|solicitando|solicitou|solicitaram|apresentou|apresentaram|apresentando|declarou|declararam|declarando|manifestou|manifestaram|manifestando|concorda|concordam|concordando|junta|juntou|juntaram|juntando|pretendendo|afirmando|informando|esclarecendo|fazendo|realizando|ressaltam|informam|afirmam|esclarecem|constado)\b",
        name,
    ):
        return None
    if re.search(
        r"(?i)^\s*(?:ambos|ambas|todos|todas|juntos|juntas|o qual|a qual|os quais|as quais|do qual|da qual|dos quais|das quais|que|enquanto|ao passo que|conforme|entretanto|no entanto|por sua vez|sendo que)\b",
        name,
    ):
        return None
    if re.search(
        r"(?i)^\s*(?:o\s+|a\s+)?(?:herdeiro|herdeira|filho|filha|meeiro|meeira)\s+(?:fez|realizou|pretende|requer|requerem|apresentou|apresentaram|declarou|declararam|ressaltam|junta|solicita|concorda)\b",
        name,
    ):
        return None

    # 2. Strip deceased prefix if present (handling do/da/dos/das/de)
    if deceased_name:
        name = re.sub(rf"(?i)^(?:d[oae]s?\s+)?(?:falecid[oa]\s+)?{re.escape(deceased_name)}\s*[\:\,\-\–\s]*", "", name).strip()

    name = re.sub(
        r"(?i)^(?:d[oae]s?\s+)?(?:de cujus|falecid[oa]|inventariad[oa]|autor[a]? da herança|viúva de|viuvo de|cônjuge de|filh[oa] de|herdeir[oa][s]?|filh[oa][s]?|sucessor(?:es)?)\s*[\:]?\s*",
        "",
        name,
    ).strip()

    # 3. Truncate trailing predicate phrases, gerunds, relative clauses, and conjunctions
    name = re.sub(
        r"(?i)\s+\b(?:é|foi|será|sobre|qualificad[oa]|representad[oa]|residente|aceitou|renunciou|renunciando|renunciarem|requerendo|solicitando|declarando|juntando|apresentando|manifestando|manifestou|manifestaram|afirmou|afirmaram|informou|informaram|declararam|solicitaram|fez|realizou|pretende|requer|requerem|apresentou|apresentaram|declarou|solicita|concorda|ren[uú]ncia|cess[aã]o|quinh[aã]o|quinh[oõ]es|não|nao|e herdeira|e herdeiro|a qual|o qual|os quais|as quais|que)\b.*$",
        "",
        name,
    ).strip()
    name = re.sub(r"\s+", " ", name).strip()

    if len(name) <= 3:
        return None
    if deceased_name and name.lower() == deceased_name.lower():
        return None

    norm_lower = name.lower()
    role_phrases = (
        "meeira", "meeiro", "viúva", "viuvo", "herdeira", "herdeiro",
        "meeira e herdeira", "meeiro e herdeiro", "viúva meeira e herdeira",
        "viúva meeira", "viuvo meeiro", "cônjuge supérstite",
        "não renunciou", "nao renunciou", "dos bens", "da herança",
        "renúncia abdicativa", "renúncia translativa", "renuncia abdicativa",
        "ambos renunciando", "ressaltam que", "capaz", "incapaz", "maior", "menor",
        "maior e capaz", "junta comprovante", "requer a intimação",
        "apresentaram termo de", "enquanto tiago souza"
    )
    if norm_lower in role_phrases:
        return None

    if re.search(r"(?i)^\s*(?:capaz|incapaz|maior|menor|solicita|concorda|junta|requer|declarou|requerendo|solicitando|declarando|juntando|apresentando|manifestando|pretendendo)$", norm_lower):
        return None

    if re.search(r"(?i)\b(?:ren[uú]ncia|renunciando|renunciarem|cess[aã]o|partilha|heran[cç]a|quinh[aã]o|quinh[oõ]es|autos|processo)\b", name):
        return None

    if RE_ROLE_TERMS.match(name):
        return None

    return name


def extract_inventory_v1(
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
    
    rite_type = "inventario_comum"
    if re.search(r"(?i)\barrolamento sum[aá]rio\b", full_text) or (cb and "sumário" in str(cb.get("case_class", "")).lower()):
        rite_type = "arrolamento_sumario"
    elif re.search(r"(?i)\barrolamento comum\b", full_text) or (cb and "arrolamento" in str(cb.get("case_class", "")).lower()):
        rite_type = "arrolamento_comum"
    elif re.search(r"(?i)\binvent[aá]rio extrajudicial|escritura p[uú]blica de invent[aá]rio\b", full_text):
        rite_type = "extrajudicial"
    elif re.search(r"(?i)\binvent[aá]rio judicial\b", full_text):
        rite_type = "inventario_judicial"

    # 1. Parties: Deceased & Author
    deceased_name = None
    deceased_cpf = None

    for m_dec in RE_DECEASED.finditer(full_text):
        raw_dec = m_dec.group(1).strip()
        pre_match = full_text[:m_dec.start()].rstrip()
        match_str = m_dec.group(0)

        # Defect 1 Fix: Skip match if preceded by 'herdeiro(s)/sucessor(es) do/da/dos/das' AND contains ':' after deceased term
        if re.search(
            r"(?i)\b(?:herdeiro[s]?|herdeira[s]?|sucessor(?:es)?|sã[o]\s+(?:herdeiros|sucessores))\s+(?:do|da|dos|das)\s*$",
            pre_match,
        ) and re.search(
            r"(?i)\b(?:falecid[oa]|de cujus|autor[a]? da herança)\s*:",
            match_str,
        ):
            continue  # Skip header label match ('São herdeiros/sucessores do falecido: ...')

        # Defect 2 Fix: Truncate conjunctions swallowing second deceased name / spouse qualification
        raw_dec = re.sub(
            r"(?i)\s+\b(?:e\s+(?:sua\s+esposa|seu\s+esposo|de\s+sua\s+esposa|de|sua\s+companheira|seu\s+companheiro)?|bem\s+como)\b.*$",
            "",
            raw_dec,
        ).strip()

        candidate = _clean_person_name(raw_dec)
        if candidate:
            deceased_name = candidate
            break

    cpfs = RE_CPF.findall(full_text)
    if cpfs and deceased_name:
        deceased_cpf = cpfs[0]

    marital_status = None
    if re.search(r"(?i)\bcasad[oa]\b", full_text):
        marital_status = "casado(a)"
    elif re.search(r"(?i)\bvi[uú]v[oa]\b", full_text):
        marital_status = "viúvo(a)"
    elif re.search(r"(?i)\bdivorciad[oa]\b", full_text):
        marital_status = "divorciado(a)"
    elif re.search(r"(?i)\bsolteir[oa]\b", full_text):
        marital_status = "solteiro(a)"
    elif re.search(r"(?i)\buni[aã]o est[aá]vel\b", full_text):
        marital_status = "união estável"

    matrimonial_regime = None
    if re.search(r"(?i)\bsepara[cç][aã]o (?:total|convencional)\b", full_text):
        matrimonial_regime = "separação_total"
    elif re.search(r"(?i)\bcomunh[aã]o parcial\b", full_text):
        matrimonial_regime = "comunhão_parcial"
    elif re.search(r"(?i)\bcomunh[aã]o universal\b", full_text):
        matrimonial_regime = "comunhão_universal"
    elif re.search(r"(?i)\bsepara[cç][aã]o (?:obrigat[oó]ria|legal)\b", full_text):
        matrimonial_regime = "separação_obrigatória"

    # Death Certificate
    death_cert = None
    m_cert = RE_DEATH_CERT.search(full_text)
    if m_cert:
        cert_snippet = m_cert.group(0)
        num_m = re.search(r"(?:termo|nº|n°|número|sob o nº)\s*([0-9\.\-]+)", cert_snippet, re.IGNORECASE)
        book_m = re.search(r"(?:livro)\s*([A-Z0-9\-]+)", cert_snippet, re.IGNORECASE)
        page_m = re.search(r"(?:folha|fls?\.)\s*([0-9\-]+)", cert_snippet, re.IGNORECASE)
        registry_m = re.search(r"(?:cart[oó]rio|of[ií]cio)\s*([A-Za-z0-9\s]+?)(?=\,|\.|$)", cert_snippet, re.IGNORECASE)
        date_m = re.search(r"(\d{2}/\d{2}/\d{4}|\d{4}-\d{2}-\d{2})", cert_snippet)
        death_cert = DeathCertificateInfo(
            number=num_m.group(1) if num_m else None,
            book=book_m.group(1) if book_m else None,
            page=page_m.group(1) if page_m else None,
            registry_office=registry_m.group(1).strip() if registry_m else None,
            death_date=date_m.group(1) if date_m else None,
        )

    deceased_info = DeceasedInfo(
        name=deceased_name,
        cpf=deceased_cpf,
        marital_status=marital_status,
        matrimonial_regime=matrimonial_regime,
        death_certificate=death_cert,
    )

    author_name = None
    author_cpf = None
    author_relationship = None
    if cb and cb.get("polo_ativo"):
        pa = cb.get("polo_ativo")
        if isinstance(pa, str):
            author_name = pa
        elif isinstance(pa, list) and pa:
            author_name = str(pa[0])
    if not author_name:
        m_aut = re.search(r"(?i)\b(?:inventariante|requerente|autor[a]?)\b[\s\:\,\-\–]+([A-ZÀ-Ý][a-zà-ýA-ZÀ-Ý\s]{2,60}?)(?=\,|\.|\;|\bcpf|$)", full_text)
        if m_aut:
            author_name = _clean_person_name(m_aut.group(1), deceased_name)
    if len(cpfs) > 1 and author_name:
        author_cpf = cpfs[1]
    
    if re.search(r"(?i)\bvi[uú]v[oa]|c[ôo]njuge\b", full_text):
        author_relationship = "cônjuge"
    elif re.search(r"(?i)\bfilh[oa]\b", full_text):
        author_relationship = "filho(a)"

    author_info = AuthorPartyInfo(
        name=author_name,
        cpf_cnpj=author_cpf,
        relationship=author_relationship,
        is_inventariante=True,
    )

    parties_info = PartiesInfo(author=author_info, deceased=deceased_info)

    # 2. Spouse & Heirs Classification
    meeiro_item = None
    heirs_list = []
    seen_heirs = set()
    has_incapable = bool(re.search(r"(?i)\b(?:menor|incapaz|interdito|curatela|relativamente incapaz|absolutamente incapaz)\b", full_text))

    spouse_name = None
    m_mee = RE_MEEIRO.search(full_text)
    if m_mee:
        raw_mee = m_mee.group(1).strip()
        candidate = _clean_person_name(raw_mee, deceased_name)
        if candidate:
            spouse_name = candidate
    if not spouse_name:
        m_sq = RE_SPOUSE_QUALIFICATION.search(full_text)
        if m_sq:
            spouse_name = _clean_person_name(m_sq.group(1), deceased_name)
    if not spouse_name and author_relationship == "cônjuge" and author_name:
        spouse_name = author_name

    if spouse_name:
        if matrimonial_regime == "separação_total":
            if spouse_name.lower() not in seen_heirs:
                heirs_list.append(
                    HeirItem(
                        name=spouse_name,
                        role="herdeiro",
                        kinship="cônjuge",
                    )
                )
                seen_heirs.add(spouse_name.lower())
        elif matrimonial_regime == "comunhão_parcial":
            is_herdeira_too = bool(re.search(r"(?i)\bherdeir[oa]\b", full_text))
            role_val = "meeiro_e_herdeiro" if is_herdeira_too else "meeiro"
            meeiro_item = HeirItem(
                name=spouse_name,
                role=role_val,
                kinship="cônjuge",
            )
            seen_heirs.add(spouse_name.lower())
        else:
            meeiro_item = HeirItem(
                name=spouse_name,
                role="meeiro",
                kinship="cônjuge",
            )
            seen_heirs.add(spouse_name.lower())

    for m_sec in RE_HEIR_SECTION.finditer(full_text):
        section_text = m_sec.group(1).strip()
        tokens = re.split(r"(?i)\s*(?:,|\be\b|\be/ou\b|;)\s*", section_text)
        for tok in tokens:
            tok_clean = re.sub(
                r"(?i)\b(?:filho[s]?|filha[s]?|herdeiro[s]?|sucessor(?:es)?|maior|capaz|menor|incapaz|que renunciou.*$)\b",
                "",
                tok,
            ).strip()
            h_name = _clean_person_name(tok_clean, deceased_name)
            if h_name and h_name.lower() not in seen_heirs:
                seen_heirs.add(h_name.lower())

                local_text = _get_local_sentence_block(full_text, m_sec.start(), m_sec.end())
                accepted_local = bool(re.search(RE_ACCEPTED_VERBS, local_text, re.IGNORECASE))
                renounced_local = bool(re.search(RE_RENUNCIATION_VERBS, local_text, re.IGNORECASE)) and not bool(re.search(RE_NOT_RENNOUNCED_VERBS, local_text, re.IGNORECASE)) and not accepted_local

                accepted_global = bool(
                    re.search(rf"(?i){re.escape(h_name)}(?:(?!,?\s*\b(?:enquanto|ao\s+passo\s+que|mas|por[eê]m|todavia|contudo|j[aá]|entretanto|no\s+entanto|por\s+sua\s+vez|sendo\s+que)\b)[^.\n;]){{0,60}}?{RE_ACCEPTED_VERBS}", full_text)
                    or re.search(rf"(?i){RE_ACCEPTED_VERBS}(?:(?!,?\s*\b(?:enquanto|ao\s+passo\s+que|mas|por[eê]m|todavia|contudo|j[aá]|entretanto|no\s+entanto|por\s+sua\s+vez|sendo\s+que)\b)[^.\n;]){{0,60}}?{re.escape(h_name)}", full_text)
                )

                renounced_global = bool(
                    re.search(rf"(?i){re.escape(h_name)}(?:(?!,?\s*\b(?:enquanto|ao\s+passo\s+que|mas|por[eê]m|todavia|contudo|j[aá]|entretanto|no\s+entanto|por\s+sua\s+vez|sendo\s+que)\b)[^.\n;]){{0,120}}?{RE_RENUNCIATION_VERBS}", full_text)
                    or re.search(rf"(?i){RE_RENUNCIATION_VERBS}(?:(?!,?\s*\b(?:enquanto|ao\s+passo\s+que|mas|por[eê]m|todavia|contudo|j[aá]|entretanto|no\s+entanto|por\s+sua\s+vez|sendo\s+que)\b)[^.\n;]){{0,120}}?{re.escape(h_name)}", full_text)
                ) and not bool(
                    re.search(rf"(?i){re.escape(h_name)}[\s\S]{{0,60}}?{RE_NOT_RENNOUNCED_VERBS}", full_text)
                    or re.search(rf"(?i){RE_NOT_RENNOUNCED_VERBS}[\s\S]{{0,60}}?{re.escape(h_name)}", full_text)
                ) and not accepted_global

                generic_renunciation = (len(seen_heirs) <= 1) and bool(
                    re.search(r"(?i)\bren[uú]ncia\s+(?:abdicativa|translativa)\b", full_text)
                    or re.search(r"(?i)\b(?:o|a|os|as)?\s*herdeir[oa]s?\s+(?:fez|fizeram)?\s*ren[uú]ncia\b", full_text)
                ) and not bool(re.search(RE_NOT_RENNOUNCED_VERBS, full_text, re.IGNORECASE)) and not accepted_global

                renounced = (renounced_local or renounced_global or generic_renunciation) and not accepted_global
                incapable = bool(re.search(r"(?i)\b(?:menor|incapaz)\b", local_text)) or bool(re.search(rf"(?i){re.escape(h_name)}[^.\n]{{0,60}}?\b(?:menor|incapaz)\b", full_text))

                heirs_list.append(
                    HeirItem(
                        name=h_name,
                        role="herdeiro",
                        kinship="filho(a)",
                        is_incapable=incapable,
                        renunciation=renounced,
                    )
                )

    for m in RE_HEIRS.finditer(full_text):
        raw_heir = m.group(1).strip()
        sub_names = [s.strip() for s in re.split(r"(?i)\s+\be\s+(?=[A-ZÀ-Ý])", raw_heir) if s.strip()]
        
        for sub_n in sub_names:
            h_name = _clean_person_name(sub_n, deceased_name)
            if h_name and h_name.lower() not in seen_heirs:
                seen_heirs.add(h_name.lower())
                
                local_text = _get_local_sentence_block(full_text, m.start(), m.end())
                accepted_local = bool(re.search(RE_ACCEPTED_VERBS, local_text, re.IGNORECASE))
                renounced_local = bool(re.search(RE_RENUNCIATION_VERBS, local_text, re.IGNORECASE)) and not bool(re.search(RE_NOT_RENNOUNCED_VERBS, local_text, re.IGNORECASE)) and not accepted_local

                accepted_global = bool(
                    re.search(rf"(?i){re.escape(h_name)}(?:(?!,?\s*\b(?:enquanto|ao\s+passo\s+que|mas|por[eê]m|todavia|contudo|j[aá]|entretanto|no\s+entanto|por\s+sua\s+vez|sendo\s+que)\b)[^.\n;]){{0,60}}?{RE_ACCEPTED_VERBS}", full_text)
                    or re.search(rf"(?i){RE_ACCEPTED_VERBS}(?:(?!,?\s*\b(?:enquanto|ao\s+passo\s+que|mas|por[eê]m|todavia|contudo|j[aá]|entretanto|no\s+entanto|por\s+sua\s+vez|sendo\s+que)\b)[^.\n;]){{0,60}}?{re.escape(h_name)}", full_text)
                )

                renounced_global = bool(
                    re.search(rf"(?i){re.escape(h_name)}(?:(?!,?\s*\b(?:enquanto|ao\s+passo\s+que|mas|por[eê]m|todavia|contudo|j[aá]|entretanto|no\s+entanto|por\s+sua\s+vez|sendo\s+que)\b)[^.\n;]){{0,120}}?{RE_RENUNCIATION_VERBS}", full_text)
                    or re.search(rf"(?i){RE_RENUNCIATION_VERBS}(?:(?!,?\s*\b(?:enquanto|ao\s+passo\s+que|mas|por[eê]m|todavia|contudo|j[aá]|entretanto|no\s+entanto|por\s+sua\s+vez|sendo\s+que)\b)[^.\n;]){{0,120}}?{re.escape(h_name)}", full_text)
                ) and not bool(
                    re.search(rf"(?i){re.escape(h_name)}[\s\S]{{0,60}}?{RE_NOT_RENNOUNCED_VERBS}", full_text)
                    or re.search(rf"(?i){RE_NOT_RENNOUNCED_VERBS}[\s\S]{{0,60}}?{re.escape(h_name)}", full_text)
                ) and not accepted_global

                generic_renunciation = (len(seen_heirs) <= 1) and bool(
                    re.search(r"(?i)\bren[uú]ncia\s+(?:abdicativa|translativa)\b", full_text)
                    or re.search(r"(?i)\b(?:o|a|os|as)?\s*herdeir[oa]s?\s+(?:fez|fizeram)?\s*ren[uú]ncia\b", full_text)
                ) and not bool(re.search(RE_NOT_RENNOUNCED_VERBS, full_text, re.IGNORECASE)) and not accepted_global

                renounced = (renounced_local or renounced_global or generic_renunciation) and not accepted_global
                incapable = bool(re.search(r"(?i)\b(?:menor|incapaz)\b", local_text))

                heirs_list.append(
                    HeirItem(
                        name=h_name,
                        role="herdeiro",
                        kinship="filho(a)",
                        is_incapable=incapable,
                        renunciation=renounced,
                    )
                )

    if len(heirs_list) == 1:
        h0_name = heirs_list[0].name
        accepted_g0 = bool(
            re.search(rf"(?i){re.escape(h0_name)}(?:(?!,?\s*\b(?:enquanto|ao\s+passo\s+que|mas|por[eê]m|todavia|contudo|j[aá]|entretanto|no\s+entanto|por\s+sua\s+vez|sendo\s+que)\b)[^.\n;]){{0,60}}?{RE_ACCEPTED_VERBS}", full_text)
            or re.search(rf"(?i){RE_ACCEPTED_VERBS}(?:(?!,?\s*\b(?:enquanto|ao\s+passo\s+que|mas|por[eê]m|todavia|contudo|j[aá]|entretanto|no\s+entanto|por\s+sua\s+vez|sendo\s+que)\b)[^.\n;]){{0,60}}?{re.escape(h0_name)}", full_text)
        )
        generic_ren = bool(
            re.search(r"(?i)\bren[uú]ncia\s+(?:abdicativa|translativa)\b", full_text)
            or re.search(r"(?i)\b(?:o|a|os|as)?\s*herdeir[oa]s?\s+(?:fez|fizeram)?\s*ren[uú]ncia\b", full_text)
        ) and not bool(re.search(RE_NOT_RENNOUNCED_VERBS, full_text, re.IGNORECASE)) and not accepted_g0
        if generic_ren:
            heirs_list[0].renunciation = True

    heirs_info = HeirsInfo(
        meeiro=meeiro_item,
        heirs=heirs_list,
        total_heirs_count=len(heirs_list) + (1 if meeiro_item else 0),
        has_incapable_heirs=has_incapable or any(h.is_incapable for h in heirs_list),
    )

    # 3. Shares (Quinhões & Partilha)
    shares_list = []
    has_proposed_partition = bool(re.search(r"(?i)\b(?:plano de partilha|esbo[çc]o de partilha|proposta de partilha)\b", full_text))
    partition_status = "pendente"
    if re.search(r"(?i)\bpartilha homologada|homologo a partilha\b", full_text):
        partition_status = "homologada"
    elif re.search(r"(?i)\bpartilha impugnada|impugna[cç][aã]o [aà] partilha\b", full_text):
        partition_status = "impugnada"
    elif re.search(r"(?i)\bpartilha amig[aá]vel\b", full_text):
        partition_status = "amigavel"

    for m in re.finditer(r"(?i)([A-ZÀ-Ý][a-zà-ýA-ZÀ-Ý\s]{2,40}?)\s*(?:cabe|caber[aá]|quinh[aã]o|porcentagem)?\s*(\d+(?:[\.\,]\d+)?)\s*\%", full_text):
        h_n = m.group(1).strip()
        try:
            pct = float(m.group(2).replace(",", "."))
        except ValueError:
            pct = None
        has_cessao = bool(re.search(r"(?i)\bcess[aã]o de direitos\b", full_text))
        shares_list.append(
            ShareItem(
                heir_name=h_n,
                share_percentage=pct,
                assigned_rights=has_cessao,
                description=m.group(0),
            )
        )

    shares_info = SharesInfo(
        shares=shares_list,
        proposed_partition_present=has_proposed_partition,
        partition_status=partition_status,
    )

    # 4. Properties (Bens) & Registrations
    prop_list = []
    reg_list = []
    tot_dec = 0.0
    tot_ev = 0.0

    for m in RE_PROPERTIES.finditer(full_text):
        snippet = m.group(0)
        p_type = "outro"
        if re.search(r"(?i)im[oó]vel|terreno|lote|apartamento", snippet):
            p_type = "imovel_urbano" if "urbano" in snippet.lower() or "rua" in snippet.lower() else "imovel_rural"
        elif re.search(r"(?i)ve[ií]culo|autom[oó]vel|carro", snippet):
            p_type = "veiculo"
        elif re.search(r"(?i)saldo|banco|poupan[çc]a|conta", snippet):
            p_type = "saldo_bancario"
        elif re.search(r"(?i)a[çc][oõ]es", snippet):
            p_type = "acoes"

        val_m = re.search(r"R\$\s*(\d+(?:\.\d{3})*(?:\,\d{2})?)", snippet)
        dec_val = None
        if val_m:
            try:
                dec_val = float(val_m.group(1).replace(".", "").replace(",", "."))
                tot_dec += dec_val
            except ValueError:
                pass

        mat_m = re.search(r"(?:matr[ií]cula|sob o nº)\s*([0-9\.\-]+)", snippet, re.IGNORECASE)
        mat_num = mat_m.group(1) if mat_m else None

        prop_list.append(
            PropertyItem(
                description=snippet.strip(),
                property_type=p_type,
                matricula_number=mat_num,
                declared_value=dec_val,
            )
        )

        if mat_num:
            encs = [m.group(0) for m in RE_ENCUMBRANCE.finditer(full_text)]
            reg_list.append(
                RegistrationItem(
                    matricula_number=mat_num,
                    averbations=[],
                    real_encumbrances=list(set(encs)),
                )
            )

    properties_info = PropertiesInfo(
        properties=prop_list,
        total_declared_value=tot_dec,
        total_evaluated_value=tot_ev,
    )

    # 5. Liabilities (Dívidas)
    liabilities_list = []
    tot_liab = 0.0
    for m in RE_LIABILITY.finditer(full_text):
        snip = m.group(0)
        val_m = re.search(r"R\$\s*(\d+(?:\.\d{3})*(?:\,\d{2})?)", snip)
        l_val = None
        if val_m:
            try:
                l_val = float(val_m.group(1).replace(".", "").replace(",", "."))
                tot_liab += l_val
            except ValueError:
                pass
        cred_m = re.search(r"(?:credor|favor de)\s+([A-ZÀ-Ý][a-zà-ýA-ZÀ-Ý\s]{2,40})", snip, re.IGNORECASE)
        cred_n = cred_m.group(1).strip() if cred_m else None
        liabilities_list.append(
            LiabilityItem(
                description=snip.strip(),
                creditor_name=cred_n,
                amount=l_val,
                status="habilitada" if "habilitação" in snip.lower() else "pendente",
            )
        )

    liabilities_info = LiabilitiesInfo(
        liabilities=liabilities_list,
        total_liabilities_value=tot_liab,
    )

    # 6. Conflicts
    conflicts_list = []
    if re.search(r"(?i)\b(?:diverg[êe]ncia|divergencia|impugna[cç][aã]o)\s+(?:de|à|a)\s+partilha\b", full_text):
        conflicts_list.append(
            ConflictItem(
                conflict_type="partition_disagreement",
                description="Divergência entre herdeiros sobre o plano de partilha",
                severity="high",
            )
        )
    if re.search(r"(?i)\b(?:herdeiro|habilita[cç][aã]o)\s+impugnad[oa]\b", full_text):
        conflicts_list.append(
            ConflictItem(
                conflict_type="heir_impugnation",
                description="Impugnação quanto à habilitação ou qualidade de herdeiro",
                severity="high",
            )
        )
    if re.search(r"(?i)\b(?:disputa|impugna[cç][aã]o|contestac[aã]o|discord[âa]ncia)\b[^.\n]{0,80}?\b(?:itcd|itcmd|imposto)\b", full_text):
        conflicts_list.append(
            ConflictItem(
                conflict_type="itcmd_tax_dispute",
                description="Disputa ou impugnação do valor lançado para o ITCD/ITCMD",
                severity="medium",
            )
        )
    if liabilities_list and any(
        liability.status == "impugnada" for liability in liabilities_list
    ):
        conflicts_list.append(
            ConflictItem(
                conflict_type="creditor_opposition",
                description="Impugnação de habilitação de crédito do espólio",
                severity="high",
            )
        )

    conflicts_info = ConflictsInfo(
        conflicts=conflicts_list,
        has_active_conflicts=len(conflicts_list) > 0,
    )

    # 7. ITCD/ITCMD (Check non-negated payment)
    itcd_paid = False
    if (
        re.search(r"(?i)\b(?:itcd|itcmd)\b[^.\n]{0,60}?\b(?:pago|quitado|recolhido|recolhimento|pagamento|quita[cç][aã]o)\b", full_text)
        or re.search(r"(?i)\b(?:recolhimento|pagamento|quita[cç][aã]o|pago|quitado|recolhido)\b[^.\n]{0,60}?\b(?:do|da|dos|das)?\s*(?:itcd|itcmd)\b", full_text)
        or re.search(r"(?i)\b(?:guia|dae)\b[^.\n]{0,50}?\b(?:quitad[oa]|paga|recolhida)\b", full_text)
    ):
        negated = (
            bool(re.search(r"(?i)\b(?:n[aã]o|sem|ainda n[aã]o|pendente|falta)\s+(?:foi|ser|ter|houve|se encontra|encontra-se)?\s*(?:foi|ser|ter|houve|o|do|da|dos|das)?\s*(?:pago|quitado|recolhido|recolhimento|pagamento|quita[cç][aã]o|de\s+recolhimento|de\s+pagamento)\b", full_text))
            or bool(re.search(r"(?i)\b(?:itcd|itcmd|imposto)\b[^.\n]{0,60}?\b(?:n[aã]o|pendente|ainda n[aã]o|falta)\b", full_text))
            or bool(re.search(r"(?i)\b(?:n[aã]o|sem|pendente)\b[^.\n]{0,60}?\b(?:itcd|itcmd)\b", full_text))
        )
        if not negated:
            itcd_paid = True

    itcd_exempt = bool(
        re.search(
            r"(?i)\b(?:isen[cç][aã]o|isento)\s*(?:de|do)?\s*(?:itcd|itcmd)\b",
            full_text,
        )
        or re.search(
            r"(?i)\b(?:itcd|itcmd)\b[^.\n]{0,50}?\b(?:isento|isen[cç][aã]o)\b",
            full_text,
        )
    )
    sefaz_done = bool(re.search(r"(?i)\b(?:avalia[cç][aã]o fiscal|sefaz)\s*(?:conclu[ií]da|homologada)\b", full_text))

    itcd_info = ITCDInfo(
        paid=itcd_paid,
        exempt=itcd_exempt,
        evaluation_completed=sefaz_done,
    )

    # 8. Testament & Negative Certificates
    testament_exists = False
    if re.search(r"(?i)\btestamento\b", full_text):
        if not re.search(
            r"(?i)\b(?:inexist[êe]ncia|n[aã]o deixou|n[aã]o deixo|sem|negativa de)\s+testamento\b",
            full_text,
        ):
            testament_exists = True

    neg_certs = bool(re.search(r"(?i)\b(?:certid[aã]o negativa|certid[oõ]es negativas|quita[cç][aã]o fiscal)\b", full_text))

    res = InventoryV1Result(
        schema_version="inventory/v1",
        rite_type=rite_type,
        parties=parties_info,
        heirs=heirs_info,
        shares=shares_info,
        properties=properties_info,
        liabilities=liabilities_info,
        registrations=reg_list,
        conflicts=conflicts_info,
        testament_exists=testament_exists,
        itcd=itcd_info,
        negative_certificates_present=neg_certs,
        extractor_metadata={
            "documents_analyzed": len(documents or []),
            "has_case_base": cb is not None,
        },
    )
    return res.model_dump()


def validate_inventory_v1(profile_data: Dict[str, Any]) -> DomainValidationResult:
    missing_docs: List[str] = []
    missing_citations: List[str] = []
    detected_conflicts: List[Dict[str, Any]] = []
    procedural_risks: List[str] = []
    details: Dict[str, Any] = {}

    p_data = profile_data if isinstance(profile_data, dict) else {}
    parties = p_data.get("parties") if isinstance(p_data.get("parties"), dict) else {}
    deceased = parties.get("deceased") if isinstance(parties.get("deceased"), dict) else {}
    heirs = p_data.get("heirs") if isinstance(p_data.get("heirs"), dict) else {}
    shares = p_data.get("shares") if isinstance(p_data.get("shares"), dict) else {}
    properties = p_data.get("properties") if isinstance(p_data.get("properties"), dict) else {}
    itcd = p_data.get("itcd") if isinstance(p_data.get("itcd"), dict) else {}
    conflicts = p_data.get("conflicts") if isinstance(p_data.get("conflicts"), dict) else {}
    registrations = p_data.get("registrations") if isinstance(p_data.get("registrations"), list) else []
    rite_type = p_data.get("rite_type") or "inventario_comum"
    if not isinstance(rite_type, str):
        rite_type = "inventario_comum"

    testament_exists = bool(p_data.get("testament_exists", False))
    neg_certs = bool(p_data.get("negative_certificates_present", False))

    # 1. Required Documents Check
    death_cert = deceased.get("death_certificate") if isinstance(deceased, dict) else None
    if not isinstance(death_cert, dict) or (not death_cert.get("number") and not death_cert.get("death_date")):
        missing_docs.append("Certidão de Óbito do Falecido")

    prop_list = properties.get("properties") if isinstance(properties, dict) and isinstance(properties.get("properties"), list) else []
    if prop_list:
        has_matricula = any(isinstance(p, dict) and p.get("matricula_number") for p in prop_list)
        if not has_matricula:
            missing_docs.append("Titulação / Comprovação de Propriedade dos Bens Imóveis")
    else:
        missing_docs.append("Declaração de Bens e Direitos")

    itcd_paid = itcd.get("paid", False) if isinstance(itcd, dict) else False
    itcd_exempt = itcd.get("exempt", False) if isinstance(itcd, dict) else False
    if not itcd_paid and not itcd_exempt:
        missing_docs.append("Guia / Comprovante de Quitação ou Isenção do ITCMD")
        procedural_risks.append("PENDENCIA_QUITACAO_ITCMD: ITCMD pendente de apuração/quitação perante a Fazenda Estadual")

    if not neg_certs:
        missing_docs.append("Certidões Negativas de Débitos Fiscais (União, Estado, Município)")

    # 2. Rite Compliance Validation
    has_incapable = heirs.get("has_incapable_heirs", False) if isinstance(heirs, dict) else False
    has_active_conflicts = conflicts.get("has_active_conflicts", False) if isinstance(conflicts, dict) else False
    partition_status = shares.get("partition_status", "pendente") if isinstance(shares, dict) else "pendente"
    tot_declared_val = properties.get("total_declared_value", 0.0) if isinstance(properties, dict) else 0.0
    if not isinstance(tot_declared_val, (int, float)):
        try:
            tot_declared_val = float(tot_declared_val) if tot_declared_val is not None else 0.0
        except (ValueError, TypeError):
            tot_declared_val = 0.0

    if rite_type == "arrolamento_sumario":
        if has_incapable:
            procedural_risks.append("INCOMPATIBILIDADE_RITO_ARROLAMENTO_SUMARIO: Existência de herdeiros incapazes/menores veda o arrolamento sumário (CPC Art. 659)")
        if partition_status == "impugnada" or has_active_conflicts:
            procedural_risks.append("INCOMPATIBILIDADE_RITO_ARROLAMENTO_SUMARIO: Divergência quanto à partilha impede o arrolamento sumário")

    elif rite_type == "arrolamento_comum":
        if tot_declared_val > 1500000.0:
            procedural_risks.append(f"INCOMPATIBILIDADE_RITO_ARROLAMENTO_COMUM: Valor dos bens (R$ {tot_declared_val:,.2f}) excede o teto legal do Arrolamento Comum (1.000 salários mínimos - CPC Art. 664)")

    elif rite_type == "extrajudicial":
        if testament_exists:
            procedural_risks.append("INCOMPATIBILIDADE_RITO_EXTRAJUDICIAL: Existência de testamento veda via extrajudicial em cartório")
        if has_incapable:
            procedural_risks.append("INCOMPATIBILIDADE_RITO_EXTRAJUDICIAL: Presença de herdeiros incapazes exige via judicial")
        if partition_status == "impugnada" or has_active_conflicts:
            procedural_risks.append("INCOMPATIBILIDADE_RITO_EXTRAJUDICIAL: Litígio entre herdeiros desautoriza a via extrajudicial")

    # 3. Conflicts & Encumbrances
    conflict_items = conflicts.get("conflicts") if isinstance(conflicts, dict) and isinstance(conflicts.get("conflicts"), list) else []
    for c in conflict_items:
        if isinstance(c, dict):
            detected_conflicts.append(c)
        elif hasattr(c, "model_dump"):
            detected_conflicts.append(c.model_dump())
        elif isinstance(c, str):
            detected_conflicts.append({"conflict_type": "other", "description": c})

    for reg in registrations:
        if isinstance(reg, dict):
            encs = reg.get("real_encumbrances") if isinstance(reg.get("real_encumbrances"), list) else []
            if encs:
                procedural_risks.append(f"ONUS_REAL_NO_IMVEL: Matrícula {reg.get('matricula_number')} possui ônus ({', '.join(encs)}) sem comprovação de ciência/habilitação de credores")

    # 4. Completeness Score and Final Status Calculation
    mandatory_total = 4
    present_count = mandatory_total - len(missing_docs)
    doc_score = max(0.0, present_count / mandatory_total)

    non_compliant_keywords = ("INCOMPATIBILIDADE_RITO", "EXTRAJUDICIAL_RITE_VIOLATION")
    is_non_compliant = any(any(kw in r for kw in non_compliant_keywords) for r in procedural_risks)

    if is_non_compliant:
        status = "NON_COMPLIANT"
        completeness_score = round(doc_score * 0.5, 2)
    elif len(missing_docs) == 0 and len(procedural_risks) == 0:
        status = "COMPLETE"
        completeness_score = 1.0
    else:
        status = "INCOMPLETE"
        completeness_score = round(doc_score * 0.85, 2)

    details["rite_evaluated"] = rite_type
    details["total_heirs"] = heirs.get("total_heirs_count", 0)
    details["total_assets_value"] = tot_declared_val

    return DomainValidationResult(
        profile_name="inventory/v1",
        completeness_score=completeness_score,
        status=status,
        missing_mandatory_docs=missing_docs,
        missing_citations=missing_citations,
        detected_conflicts=detected_conflicts,
        procedural_risks=procedural_risks,
        validation_details=details,
    )
