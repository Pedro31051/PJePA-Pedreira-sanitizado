"""Validador fail-closed para respostas ``pje-agent-context/v3``.

O módulo não corrige, completa ou interpreta semanticamente a resposta. Ele
somente reconhece o formato textual canônico, valida o schema e cruza a
proveniência fornecida pelo conector. O relatório nunca inclui o texto bruto,
evitando propagar dados sensíveis encontrados durante a validação.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

SCHEMA_VERSION = "pje-agent-context/v3"
MAX_EVIDENCE = 8

TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "intent",
        "request_summary",
        "verdict",
        "evidence_refs",
        "facts",
        "conflicts",
        "gaps",
        "project_contract",
        "findings",
        "actions",
        "tests",
        "performance",
        "security",
        "acceptance",
        "runtime_required",
        "freshness",
    }
)
ENVELOPE_KEYS = frozenset(
    {"answer", "citations", "references", "sources_used", "source_ids"}
)

_EVIDENCE_LINE = re.compile(
    r"^(E[1-8]) \| ([^|\r\n]+) \| ([^|\r\n]+) \| ([^|\r\n]+) \| (.+)$"
)
_CITATION = re.compile(r"(?:\[(\d+)\]|【(\d+)】)")
_REFERENCE_ID = re.compile(r"^E[1-8]$")
_NUMBER = re.compile(r"(?<![\w])[-+]?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?")

_PII_PATTERNS = (
    (
        "PII_CNJ",
        re.compile(r"(?<!\d)\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}(?!\d)"),
    ),
    (
        "PII_CPF",
        re.compile(r"(?<!\d)\d{3}\.\d{3}\.\d{3}-\d{2}(?!\d)"),
    ),
    (
        "PII_CNPJ",
        re.compile(r"(?<!\d)\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}(?!\d)"),
    ),
    (
        "PII_CPF_UNFORMATTED",
        re.compile(r"(?i)\bCPF\s*[:#-]?\s*\d{11}\b"),
    ),
    (
        "PII_CNPJ_UNFORMATTED",
        re.compile(r"(?i)\bCNPJ\s*[:#-]?\s*\d{14}\b"),
    ),
)

_SENSITIVE_ARCHITECTURE = (
    ("ARCH_MTLS_UNSUPPORTED", re.compile(r"(?i)\b(?:mTLS|mutual TLS)\b")),
    ("ARCH_KEYCLOAK_UNSUPPORTED", re.compile(r"(?i)\bKeycloak\b")),
    ("ARCH_HMAC_UNSUPPORTED", re.compile(r"(?i)\bHMAC\b")),
    (
        "ARCH_CERTIFICATE_UNSUPPORTED",
        re.compile(r"(?i)\b(?:certificado|certificate)\b"),
    ),
)


@dataclass(frozen=True)
class _Issue:
    code: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


@dataclass(frozen=True)
class _Evidence:
    evidence_id: str
    citation_numbers: tuple[int, ...]


@dataclass(frozen=True)
class _ParsedAnswer:
    evidence: tuple[_Evidence, ...]
    packet: dict[str, Any]


@dataclass(frozen=True)
class _Provenance:
    snippets: dict[int, str]
    citation_sources: dict[int, str]
    selected_source_ids: tuple[str, ...]


class _Validation:
    def __init__(self) -> None:
        self.issues: list[_Issue] = []

    def add(self, code: str, path: str, message: str) -> None:
        self.issues.append(_Issue(code, path, message))

    def exact_keys(
        self,
        value: Any,
        expected: frozenset[str],
        path: str,
    ) -> bool:
        if not isinstance(value, dict):
            self.add("TYPE_OBJECT", path, "deve ser um objeto")
            return False
        actual = set(value)
        for key in sorted(expected - actual):
            self.add("MISSING_KEY", f"{path}.{key}", "chave obrigatória ausente")
        for key in sorted(actual - expected):
            self.add("EXTRA_KEY", f"{path}.{key}", "chave não permitida")
        return actual == expected

    def string(
        self,
        value: Any,
        path: str,
        *,
        nullable: bool = False,
    ) -> bool:
        if nullable and value is None:
            return True
        if not isinstance(value, str) or not value.strip():
            self.add("TYPE_STRING", path, "deve ser string não vazia")
            return False
        return True

    def boolean(self, value: Any, path: str) -> bool:
        if not isinstance(value, bool):
            self.add("TYPE_BOOLEAN", path, "deve ser booleano")
            return False
        return True

    def list(self, value: Any, path: str) -> bool:
        if not isinstance(value, list):
            self.add("TYPE_ARRAY", path, "deve ser uma lista")
            return False
        return True


def _parse_answer(answer: Any, validation: _Validation) -> _ParsedAnswer | None:
    if not isinstance(answer, str):
        validation.add("ANSWER_TYPE", "$.answer", "answer deve ser string")
        return None
    if "```" in answer:
        validation.add("FORMAT_FENCE", "$.answer", "blocos fenced não são permitidos")
        return None
    if answer.startswith("##") or "\n## EVIDENCE" in answer or "\n## PACKET" in answer:
        validation.add(
            "FORMAT_HEADING",
            "$.answer",
            "os cabeçalhos devem ser exatamente EVIDENCE e PACKET",
        )
        return None
    if not answer.startswith("EVIDENCE\n"):
        validation.add(
            "FORMAT_EVIDENCE_HEADER",
            "$.answer",
            "a resposta deve iniciar exatamente com EVIDENCE",
        )
        return None
    if answer.count("\nPACKET\n") != 1:
        validation.add(
            "FORMAT_PACKET_HEADER",
            "$.answer",
            "deve existir exatamente um separador PACKET",
        )
        return None

    evidence_text, packet_text = answer[len("EVIDENCE\n") :].split(
        "\nPACKET\n", 1
    )
    lines = evidence_text.splitlines()
    if not lines or any(not line for line in lines):
        validation.add(
            "EVIDENCE_EMPTY",
            "$.answer.EVIDENCE",
            "deve existir ao menos uma evidência e não pode haver linhas vazias",
        )
        return None
    if len(lines) > MAX_EVIDENCE:
        validation.add(
            "EVIDENCE_LIMIT",
            "$.answer.EVIDENCE",
            f"máximo de {MAX_EVIDENCE} evidências",
        )

    evidence: list[_Evidence] = []
    seen_ids: set[str] = set()
    for index, line in enumerate(lines):
        path = f"$.answer.EVIDENCE[{index}]"
        match = _EVIDENCE_LINE.fullmatch(line)
        if match is None:
            validation.add(
                "EVIDENCE_FORMAT",
                path,
                "use E<n> | source | as_of | authority | claim [n]",
            )
            continue
        evidence_id = match.group(1)
        if evidence_id in seen_ids:
            validation.add("EVIDENCE_DUPLICATE", path, "ID de evidência duplicado")
        seen_ids.add(evidence_id)
        expected_id = f"E{index + 1}"
        if evidence_id != expected_id:
            validation.add(
                "EVIDENCE_SEQUENCE",
                path,
                f"ID esperado: {expected_id}",
            )
        claim = match.group(5)
        citation_numbers = tuple(
            int(first or second)
            for first, second in _CITATION.findall(claim)
        )
        if not citation_numbers:
            validation.add(
                "EVIDENCE_CITATION_MISSING",
                path,
                "a evidência exige ao menos uma citação nativa numérica",
            )
        evidence.append(_Evidence(evidence_id, citation_numbers))

    decoder = json.JSONDecoder()
    try:
        packet, consumed = decoder.raw_decode(packet_text)
    except json.JSONDecodeError as exc:
        validation.add(
            "PACKET_JSON",
            "$.answer.PACKET",
            f"JSON inválido na linha {exc.lineno}, coluna {exc.colno}",
        )
        return None
    if packet_text[consumed:].strip():
        validation.add(
            "FORMAT_TRAILING_PROSE",
            "$.answer",
            "não pode haver texto depois do objeto PACKET",
        )
    if not isinstance(packet, dict):
        validation.add("PACKET_TYPE", "$.answer.PACKET", "PACKET deve ser objeto")
        return None
    return _ParsedAnswer(tuple(evidence), packet)


def _validate_refs(
    value: Any,
    path: str,
    declared: set[str],
    validation: _Validation,
) -> None:
    if not validation.list(value, path):
        return
    seen: set[str] = set()
    for index, ref in enumerate(value):
        ref_path = f"{path}[{index}]"
        if not isinstance(ref, str) or not _REFERENCE_ID.fullmatch(ref):
            validation.add("EVIDENCE_REF_FORMAT", ref_path, "referência E1..E8 inválida")
            continue
        if ref in seen:
            validation.add("EVIDENCE_REF_DUPLICATE", ref_path, "referência duplicada")
        seen.add(ref)
        if ref not in declared:
            validation.add(
                "EVIDENCE_REF_UNKNOWN",
                ref_path,
                "referência não declarada em EVIDENCE",
            )


def _validate_string_list(value: Any, path: str, validation: _Validation) -> None:
    if not validation.list(value, path):
        return
    for index, item in enumerate(value):
        validation.string(item, f"{path}[{index}]")


def _validate_claim_items(
    items: Any,
    path: str,
    declared: set[str],
    validation: _Validation,
) -> None:
    if not validation.list(items, path):
        return
    keys = frozenset({"claim", "status", "evidence_refs"})
    for index, item in enumerate(items):
        base = f"{path}[{index}]"
        validation.exact_keys(item, keys, base)
        if not isinstance(item, dict):
            continue
        validation.string(item.get("claim"), f"{base}.claim")
        validation.string(item.get("status"), f"{base}.status")
        _validate_refs(item.get("evidence_refs"), f"{base}.evidence_refs", declared, validation)


def _validate_packet(
    packet: dict[str, Any],
    declared: set[str],
    validation: _Validation,
) -> None:
    validation.exact_keys(packet, TOP_LEVEL_KEYS, "$.packet")
    if packet.get("schema_version") != SCHEMA_VERSION:
        validation.add(
            "SCHEMA_VERSION",
            "$.packet.schema_version",
            f"deve ser exatamente {SCHEMA_VERSION}",
        )
    validation.string(packet.get("intent"), "$.packet.intent")
    validation.string(packet.get("request_summary"), "$.packet.request_summary")
    _validate_refs(packet.get("evidence_refs"), "$.packet.evidence_refs", declared, validation)

    verdict = packet.get("verdict")
    validation.exact_keys(
        verdict,
        frozenset({"status", "answer", "confidence"}),
        "$.packet.verdict",
    )
    if isinstance(verdict, dict):
        validation.string(verdict.get("status"), "$.packet.verdict.status")
        validation.string(verdict.get("answer"), "$.packet.verdict.answer")
        confidence = verdict.get("confidence")
        valid_confidence = (
            isinstance(confidence, str)
            and confidence in {"high", "medium", "low", "unknown"}
        ) or (
            isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and math.isfinite(float(confidence))
            and 0 <= float(confidence) <= 1
        )
        if not valid_confidence:
            validation.add(
                "CONFIDENCE_VALUE",
                "$.packet.verdict.confidence",
                "use high|medium|low|unknown ou número entre 0 e 1",
            )

    _validate_claim_items(packet.get("facts"), "$.packet.facts", declared, validation)

    conflicts = packet.get("conflicts")
    if validation.list(conflicts, "$.packet.conflicts"):
        conflict_keys = frozenset(
            {"topic", "candidates", "resolution", "reason", "verification"}
        )
        candidate_keys = frozenset({"claim", "evidence_refs"})
        for index, conflict in enumerate(conflicts):
            base = f"$.packet.conflicts[{index}]"
            validation.exact_keys(conflict, conflict_keys, base)
            if not isinstance(conflict, dict):
                continue
            validation.string(conflict.get("topic"), f"{base}.topic")
            validation.string(
                conflict.get("resolution"), f"{base}.resolution", nullable=True
            )
            validation.string(conflict.get("reason"), f"{base}.reason")
            validation.string(conflict.get("verification"), f"{base}.verification")
            candidates = conflict.get("candidates")
            if validation.list(candidates, f"{base}.candidates"):
                for c_index, candidate in enumerate(candidates):
                    c_base = f"{base}.candidates[{c_index}]"
                    validation.exact_keys(candidate, candidate_keys, c_base)
                    if isinstance(candidate, dict):
                        validation.string(candidate.get("claim"), f"{c_base}.claim")
                        _validate_refs(
                            candidate.get("evidence_refs"),
                            f"{c_base}.evidence_refs",
                            declared,
                            validation,
                        )

    gaps = packet.get("gaps")
    if validation.list(gaps, "$.packet.gaps"):
        gap_keys = frozenset({"missing", "impact", "verification"})
        for index, gap in enumerate(gaps):
            base = f"$.packet.gaps[{index}]"
            validation.exact_keys(gap, gap_keys, base)
            if isinstance(gap, dict):
                validation.string(gap.get("missing"), f"{base}.missing")
                validation.string(gap.get("impact"), f"{base}.impact")
                validation.string(gap.get("verification"), f"{base}.verification")

    contract = packet.get("project_contract")
    contract_keys = frozenset(
        {
            "tool_count",
            "action_count",
            "transport",
            "write_boundary",
            "confirmation_boundary",
            "evidence_refs",
        }
    )
    validation.exact_keys(contract, contract_keys, "$.packet.project_contract")
    if isinstance(contract, dict):
        for key in ("tool_count", "action_count"):
            value = contract.get(key)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                validation.add(
                    "TYPE_NONNEGATIVE_INTEGER",
                    f"$.packet.project_contract.{key}",
                    "deve ser inteiro não negativo ou null",
                )
        for key in (
            "transport",
            "write_boundary",
            "confirmation_boundary",
        ):
            validation.string(
                contract.get(key),
                f"$.packet.project_contract.{key}",
                nullable=True,
            )
        _validate_refs(
            contract.get("evidence_refs"),
            "$.packet.project_contract.evidence_refs",
            declared,
            validation,
        )

    findings = packet.get("findings")
    if validation.list(findings, "$.packet.findings"):
        finding_keys = frozenset(
            {"id", "severity", "claim", "status", "evidence_refs"}
        )
        for index, finding in enumerate(findings):
            base = f"$.packet.findings[{index}]"
            validation.exact_keys(finding, finding_keys, base)
            if isinstance(finding, dict):
                for key in ("id", "severity", "claim", "status"):
                    validation.string(finding.get(key), f"{base}.{key}")
                _validate_refs(
                    finding.get("evidence_refs"),
                    f"{base}.evidence_refs",
                    declared,
                    validation,
                )

    actions = packet.get("actions")
    if validation.list(actions, "$.packet.actions"):
        action_keys = frozenset(
            {
                "id",
                "priority",
                "action",
                "dependencies",
                "output",
                "expected_effect",
                "risk",
                "rollback",
                "done_when",
                "evidence_refs",
            }
        )
        for index, action in enumerate(actions):
            base = f"$.packet.actions[{index}]"
            validation.exact_keys(action, action_keys, base)
            if isinstance(action, dict):
                for key in (
                    "id",
                    "priority",
                    "action",
                    "output",
                    "expected_effect",
                    "risk",
                    "rollback",
                    "done_when",
                ):
                    validation.string(action.get(key), f"{base}.{key}")
                _validate_string_list(
                    action.get("dependencies"), f"{base}.dependencies", validation
                )
                _validate_refs(
                    action.get("evidence_refs"),
                    f"{base}.evidence_refs",
                    declared,
                    validation,
                )

    tests = packet.get("tests")
    if validation.list(tests, "$.packet.tests"):
        test_keys = frozenset(
            {
                "id",
                "level",
                "data",
                "network_access",
                "procedure",
                "expected",
                "requires_human_confirmation",
                "evidence_refs",
            }
        )
        for index, test in enumerate(tests):
            base = f"$.packet.tests[{index}]"
            validation.exact_keys(test, test_keys, base)
            if not isinstance(test, dict):
                continue
            for key in ("id", "level", "procedure", "expected"):
                validation.string(test.get(key), f"{base}.{key}")
            data = test.get("data")
            if data not in {"synthetic", "sanitized", "real_authorized"}:
                validation.add(
                    "TEST_DATA",
                    f"{base}.data",
                    "use synthetic|sanitized|real_authorized",
                )
            validation.boolean(test.get("network_access"), f"{base}.network_access")
            validation.boolean(
                test.get("requires_human_confirmation"),
                f"{base}.requires_human_confirmation",
            )
            _validate_refs(
                test.get("evidence_refs"),
                f"{base}.evidence_refs",
                declared,
                validation,
            )
            if test.get("level") == "real_canary":
                if data != "real_authorized":
                    validation.add(
                        "REAL_CANARY_NOT_AUTHORIZED",
                        f"{base}.data",
                        "real_canary exige data=real_authorized",
                    )
                if test.get("requires_human_confirmation") is not True:
                    validation.add(
                        "REAL_CANARY_CONFIRMATION",
                        f"{base}.requires_human_confirmation",
                        "real_canary exige confirmação humana",
                    )

    performance = packet.get("performance")
    if validation.list(performance, "$.packet.performance"):
        performance_keys = frozenset(
            {
                "metric",
                "kind",
                "value",
                "unit",
                "method",
                "sample_size",
                "environment",
                "timestamp",
                "p50",
                "p95",
                "evidence_refs",
            }
        )
        for index, metric in enumerate(performance):
            base = f"$.packet.performance[{index}]"
            validation.exact_keys(metric, performance_keys, base)
            if not isinstance(metric, dict):
                continue
            validation.string(metric.get("metric"), f"{base}.metric")
            kind = metric.get("kind")
            if kind not in {"observed", "target", "runtime_required"}:
                validation.add(
                    "PERFORMANCE_KIND",
                    f"{base}.kind",
                    "use observed|target|runtime_required",
                )
            validation.string(metric.get("unit"), f"{base}.unit", nullable=True)
            _validate_refs(
                metric.get("evidence_refs"),
                f"{base}.evidence_refs",
                declared,
                validation,
            )
            numeric_fields = ("value", "p50", "p95")
            for key in numeric_fields:
                value = metric.get(key)
                if value is not None and not _is_finite_number(value):
                    validation.add(
                        "PERFORMANCE_NUMBER",
                        f"{base}.{key}",
                        "deve ser número finito ou null",
                    )
            sample_size = metric.get("sample_size")
            if sample_size is not None and (
                not isinstance(sample_size, int)
                or isinstance(sample_size, bool)
                or sample_size <= 0
            ):
                validation.add(
                    "PERFORMANCE_SAMPLE_SIZE",
                    f"{base}.sample_size",
                    "deve ser inteiro positivo ou null",
                )
            if kind == "observed":
                if not _is_finite_number(metric.get("value")):
                    validation.add(
                        "PERFORMANCE_OBSERVED_VALUE",
                        f"{base}.value",
                        "métrica observada exige valor finito",
                    )
                validation.string(metric.get("method"), f"{base}.method")
                if (
                    not isinstance(sample_size, int)
                    or isinstance(sample_size, bool)
                    or sample_size <= 0
                ):
                    validation.add(
                        "PERFORMANCE_OBSERVED_SAMPLE",
                        f"{base}.sample_size",
                        "métrica observada exige amostra positiva",
                    )
                validation.string(metric.get("environment"), f"{base}.environment")
                validation.string(metric.get("timestamp"), f"{base}.timestamp")
            elif kind == "runtime_required":
                for key in ("value", "sample_size", "p50", "p95"):
                    if metric.get(key) is not None:
                        validation.add(
                            "RUNTIME_REQUIRED_NUMERIC",
                            f"{base}.{key}",
                            "runtime_required exige valor numérico null",
                        )
                validation.string(
                    metric.get("method"), f"{base}.method", nullable=True
                )
                validation.string(
                    metric.get("environment"),
                    f"{base}.environment",
                    nullable=True,
                )
                validation.string(
                    metric.get("timestamp"), f"{base}.timestamp", nullable=True
                )
            else:
                validation.string(
                    metric.get("method"), f"{base}.method", nullable=True
                )
                validation.string(
                    metric.get("environment"),
                    f"{base}.environment",
                    nullable=True,
                )
                validation.string(
                    metric.get("timestamp"), f"{base}.timestamp", nullable=True
                )

    security = packet.get("security")
    security_keys = frozenset(
        {
            "operation_boundary",
            "requires_human_confirmation",
            "pii_redacted",
            "prompt_injection_detected",
            "notes",
            "evidence_refs",
        }
    )
    validation.exact_keys(security, security_keys, "$.packet.security")
    if isinstance(security, dict):
        validation.string(
            security.get("operation_boundary"), "$.packet.security.operation_boundary"
        )
        for key in (
            "requires_human_confirmation",
            "pii_redacted",
            "prompt_injection_detected",
        ):
            validation.boolean(security.get(key), f"$.packet.security.{key}")
        _validate_string_list(
            security.get("notes"), "$.packet.security.notes", validation
        )
        _validate_refs(
            security.get("evidence_refs"),
            "$.packet.security.evidence_refs",
            declared,
            validation,
        )

    acceptance = packet.get("acceptance")
    if validation.list(acceptance, "$.packet.acceptance"):
        acceptance_keys = frozenset({"criterion", "validation", "evidence_refs"})
        for index, criterion in enumerate(acceptance):
            base = f"$.packet.acceptance[{index}]"
            validation.exact_keys(criterion, acceptance_keys, base)
            if isinstance(criterion, dict):
                validation.string(criterion.get("criterion"), f"{base}.criterion")
                validation.string(criterion.get("validation"), f"{base}.validation")
                _validate_refs(
                    criterion.get("evidence_refs"),
                    f"{base}.evidence_refs",
                    declared,
                    validation,
                )

    runtime_required = packet.get("runtime_required")
    if validation.list(runtime_required, "$.packet.runtime_required"):
        runtime_keys = frozenset(
            {
                "question",
                "safe_validation",
                "requires_human_confirmation",
                "evidence_refs",
            }
        )
        for index, item in enumerate(runtime_required):
            base = f"$.packet.runtime_required[{index}]"
            validation.exact_keys(item, runtime_keys, base)
            if isinstance(item, dict):
                validation.string(item.get("question"), f"{base}.question")
                validation.string(
                    item.get("safe_validation"), f"{base}.safe_validation"
                )
                validation.boolean(
                    item.get("requires_human_confirmation"),
                    f"{base}.requires_human_confirmation",
                )
                _validate_refs(
                    item.get("evidence_refs"),
                    f"{base}.evidence_refs",
                    declared,
                    validation,
                )

    freshness = packet.get("freshness")
    freshness_keys = frozenset(
        {"as_of", "version_scope", "stale_sources_ignored"}
    )
    validation.exact_keys(freshness, freshness_keys, "$.packet.freshness")
    if isinstance(freshness, dict):
        validation.string(
            freshness.get("as_of"), "$.packet.freshness.as_of", nullable=True
        )
        validation.string(
            freshness.get("version_scope"),
            "$.packet.freshness.version_scope",
            nullable=True,
        )
        _validate_string_list(
            freshness.get("stale_sources_ignored"),
            "$.packet.freshness.stale_sources_ignored",
            validation,
        )


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _parse_envelope(
    value: Mapping[str, Any],
    validation: _Validation,
) -> tuple[Any, _Provenance | None]:
    envelope = dict(value)
    validation.exact_keys(envelope, ENVELOPE_KEYS, "$")
    answer = envelope.get("answer")

    source_ids = envelope.get("source_ids")
    selected: list[str] = []
    if validation.list(source_ids, "$.source_ids"):
        for index, source_id in enumerate(source_ids):
            path = f"$.source_ids[{index}]"
            if validation.string(source_id, path):
                if source_id in selected:
                    validation.add("SOURCE_ID_DUPLICATE", path, "source_id duplicado")
                selected.append(source_id)

    sources_used = envelope.get("sources_used")
    used: list[str] = []
    if validation.list(sources_used, "$.sources_used"):
        for index, source_id in enumerate(sources_used):
            path = f"$.sources_used[{index}]"
            if validation.string(source_id, path):
                if source_id in used:
                    validation.add("SOURCE_USED_DUPLICATE", path, "fonte duplicada")
                used.append(source_id)

    references = envelope.get("references")
    referenced: list[str] = []
    if validation.list(references, "$.references"):
        reference_keys = frozenset({"source_id", "title"})
        for index, reference in enumerate(references):
            base = f"$.references[{index}]"
            validation.exact_keys(reference, reference_keys, base)
            if isinstance(reference, dict):
                source_id = reference.get("source_id")
                if validation.string(source_id, f"{base}.source_id"):
                    referenced.append(source_id)
                validation.string(reference.get("title"), f"{base}.title")

    citations = envelope.get("citations")
    snippets: dict[int, str] = {}
    citation_sources: dict[int, str] = {}
    if validation.list(citations, "$.citations"):
        citation_keys = frozenset({"citation_number", "source_id", "cited_text"})
        for index, citation in enumerate(citations):
            base = f"$.citations[{index}]"
            validation.exact_keys(citation, citation_keys, base)
            if not isinstance(citation, dict):
                continue
            number = citation.get("citation_number")
            if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
                validation.add(
                    "CITATION_NUMBER",
                    f"{base}.citation_number",
                    "deve ser inteiro positivo",
                )
                continue
            if number in snippets:
                validation.add(
                    "CITATION_DUPLICATE",
                    f"{base}.citation_number",
                    "citation_number duplicado",
                )
            source_id = citation.get("source_id")
            cited_text = citation.get("cited_text")
            validation.string(source_id, f"{base}.source_id")
            validation.string(cited_text, f"{base}.cited_text")
            if isinstance(source_id, str) and isinstance(cited_text, str):
                snippets[number] = cited_text
                citation_sources[number] = source_id

    selected_set = set(selected)
    used_set = set(used)
    referenced_set = set(referenced)
    cited_set = set(citation_sources.values())
    for source_id in sorted(used_set - selected_set):
        validation.add(
            "SOURCE_USED_NOT_SELECTED",
            "$.sources_used",
            f"fonte usada não selecionada: {source_id}",
        )
    for source_id in sorted(cited_set - selected_set):
        validation.add(
            "CITATION_SOURCE_NOT_SELECTED",
            "$.citations",
            f"fonte citada não selecionada: {source_id}",
        )
    for source_id in sorted(cited_set - used_set):
        validation.add(
            "CITATION_SOURCE_NOT_USED",
            "$.citations",
            f"fonte citada ausente de sources_used: {source_id}",
        )
    for source_id in sorted(used_set - referenced_set):
        validation.add(
            "SOURCE_USED_WITHOUT_REFERENCE",
            "$.references",
            f"fonte usada sem referência: {source_id}",
        )
    for source_id in sorted(referenced_set - selected_set):
        validation.add(
            "REFERENCE_NOT_SELECTED",
            "$.references",
            f"referência não selecionada: {source_id}",
        )

    provenance = _Provenance(snippets, citation_sources, tuple(selected))
    return answer, provenance


def _validate_citation_map(
    parsed: _ParsedAnswer,
    provenance: _Provenance | None,
    validation: _Validation,
) -> None:
    numbers = {
        number
        for evidence in parsed.evidence
        for number in evidence.citation_numbers
    }
    if provenance is None:
        return
    mapped = set(provenance.snippets)
    for number in sorted(numbers - mapped):
        validation.add(
            "CITATION_UNMAPPED",
            "$.answer.EVIDENCE",
            f"citação [{number}] não existe no envelope",
        )
    for number in sorted(mapped - numbers):
        validation.add(
            "CITATION_UNUSED",
            "$.citations",
            f"citação [{number}] não é usada em EVIDENCE",
        )


def _citation_snippets_for_refs(
    refs: Sequence[Any],
    evidence_by_id: dict[str, _Evidence],
    provenance: _Provenance,
) -> str:
    numbers: set[int] = set()
    for ref in refs:
        evidence = evidence_by_id.get(ref) if isinstance(ref, str) else None
        if evidence is not None:
            numbers.update(evidence.citation_numbers)
    return "\n".join(
        provenance.snippets[number]
        for number in sorted(numbers)
        if number in provenance.snippets
    )


def _decimal(value: Any) -> Decimal | None:
    if not _is_finite_number(value):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _snippet_contains_number(snippet: str, value: Any) -> bool:
    expected = _decimal(value)
    if expected is None:
        return False
    for raw in _NUMBER.findall(snippet):
        try:
            if Decimal(raw.replace(",", ".")) == expected:
                return True
        except InvalidOperation:
            continue
    return False


def _validate_performance_provenance(
    parsed: _ParsedAnswer,
    provenance: _Provenance | None,
    validation: _Validation,
) -> None:
    performance = parsed.packet.get("performance")
    if not isinstance(performance, list):
        return
    evidence_by_id = {item.evidence_id: item for item in parsed.evidence}
    for index, metric in enumerate(performance):
        if not isinstance(metric, dict) or metric.get("kind") != "observed":
            continue
        base = f"$.packet.performance[{index}]"
        if provenance is None:
            validation.add(
                "PERFORMANCE_PROVENANCE_REQUIRED",
                base,
                "métrica observada exige trechos citados no envelope",
            )
            continue
        snippet = _citation_snippets_for_refs(
            metric.get("evidence_refs", []), evidence_by_id, provenance
        )
        if not snippet:
            validation.add(
                "PERFORMANCE_CITED_TEXT_MISSING",
                base,
                "as evidências da métrica não possuem trecho citado",
            )
            continue
        for key in ("value", "sample_size", "p50", "p95"):
            value = metric.get(key)
            if value is not None and not _snippet_contains_number(snippet, value):
                validation.add(
                    "PERFORMANCE_NUMBER_UNSUPPORTED",
                    f"{base}.{key}",
                    "o número não aparece nos trechos citados",
                )


def _validate_sensitive_architecture(
    answer: str,
    provenance: _Provenance | None,
    validation: _Validation,
) -> None:
    for code, pattern in _SENSITIVE_ARCHITECTURE:
        if not pattern.search(answer):
            continue
        supported = provenance is not None and any(
            pattern.search(snippet) for snippet in provenance.snippets.values()
        )
        if not supported:
            validation.add(
                code,
                "$.answer",
                "termo arquitetural sensível ausente dos trechos citados",
            )


def _validate_pii(serialized_input: str, validation: _Validation) -> None:
    for code, pattern in _PII_PATTERNS:
        if pattern.search(serialized_input):
            validation.add(code, "$", "padrão de dado pessoal/processual detectado")


def validate_notebook_context(value: str | Mapping[str, Any]) -> dict[str, Any]:
    """Valida texto canônico ou envelope do conector.

    Retorna um relatório determinístico e seguro:

    ``{"valid": bool, "errors": [...], "summary": {...}}``.

    O valor recebido jamais é alterado e o relatório não contém PACKET,
    evidências, snippets ou identificadores de fontes.
    """

    validation = _Validation()
    provenance: _Provenance | None = None
    if isinstance(value, str):
        answer: Any = value
        serialized_input = value
    elif isinstance(value, Mapping):
        answer, provenance = _parse_envelope(value, validation)
        try:
            serialized_input = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=repr,
            )
        except (TypeError, ValueError):
            serialized_input = repr(value)
    else:
        validation.add(
            "INPUT_TYPE",
            "$",
            "entrada deve ser string ou envelope objeto",
        )
        answer = None
        serialized_input = repr(value)

    _validate_pii(serialized_input, validation)
    parsed = _parse_answer(answer, validation)
    if parsed is not None:
        declared = {item.evidence_id for item in parsed.evidence}
        _validate_packet(parsed.packet, declared, validation)
        _validate_citation_map(parsed, provenance, validation)
        _validate_performance_provenance(parsed, provenance, validation)
        _validate_sensitive_architecture(str(answer), provenance, validation)

    errors = [issue.as_dict() for issue in validation.issues]
    evidence_count = len(parsed.evidence) if parsed is not None else 0
    citation_count = (
        len(
            {
                number
                for evidence in parsed.evidence
                for number in evidence.citation_numbers
            }
        )
        if parsed is not None
        else 0
    )
    schema_version = (
        parsed.packet.get("schema_version")
        if parsed is not None and isinstance(parsed.packet, dict)
        else None
    )
    return {
        "valid": not errors,
        "errors": errors,
        "summary": {
            "schema_version": schema_version,
            "evidence_count": evidence_count,
            "citation_count": citation_count,
            "envelope_present": provenance is not None,
        },
    }


def validate_context_response(value: str | Mapping[str, Any]) -> dict[str, Any]:
    """Alias explícito para consumidores que tratam o valor como resposta."""

    return validate_notebook_context(value)


__all__ = [
    "ENVELOPE_KEYS",
    "MAX_EVIDENCE",
    "SCHEMA_VERSION",
    "TOP_LEVEL_KEYS",
    "validate_context_response",
    "validate_notebook_context",
]
