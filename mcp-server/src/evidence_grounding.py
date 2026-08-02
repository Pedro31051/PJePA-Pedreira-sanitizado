"""Aterramento literal de provas antes da verificação fail-closed.

O modelo às vezes devolve prova correta com defeito recuperável: hash
alterado, página trocada ou trecho parafraseado. Este módulo tenta corrigir
cada prova usando somente o índice textual local: o trecho publicado sempre
sai literalmente do texto extraído, nunca do modelo. Quando a recuperação é
ambígua ou insuficiente, a prova volta intacta e o verificador rejeita em
modo fechado. Nada aqui chama modelo ou rede.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

DEFAULT_MINIMUM_SIMILARITY = 0.72
MAX_EXCERPT_CHARS = 700
MIN_EXCERPT_CHARS = 8
_MAX_FUZZY_PAGES = 3


def _collapse(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _fold(value: str) -> str:
    return _collapse(value).casefold()


@dataclass(frozen=True)
class _PageEntry:
    document_id: str
    page: int
    sha256: str
    text: str
    folded: str


@dataclass(frozen=True)
class GroundedEvidence:
    document_id: str
    page: int
    excerpt: str
    sha256: str
    grounded: bool
    corrections: tuple[str, ...] = ()
    similarity: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "page": self.page,
            "excerpt": self.excerpt,
            "sha256": self.sha256,
        }


def build_page_index(documents: list[dict[str, Any]]) -> list[_PageEntry]:
    """Indexa páginas com texto colapsado; a ordem preserva a dos documentos."""
    entries: list[_PageEntry] = []
    for document in documents or []:
        document_id = str(document.get("document_id") or "")
        sha256 = str(document.get("sha256") or document.get("content_sha256") or "")
        for page in document.get("pages") or []:
            text = _collapse(str(page.get("text") or ""))
            if not text:
                continue
            entries.append(
                _PageEntry(
                    document_id=document_id,
                    page=int(page.get("page") or 0),
                    sha256=sha256,
                    text=text,
                    folded=text.casefold(),
                )
            )
    return entries


def _clamp_excerpt(value: str) -> str:
    excerpt = _collapse(value)
    if len(excerpt) > MAX_EXCERPT_CHARS:
        excerpt = excerpt[:MAX_EXCERPT_CHARS].rstrip()
    return excerpt


def _grounded(
    entry: _PageEntry,
    excerpt: str,
    original: dict[str, Any],
    *,
    similarity: float | None = None,
    excerpt_recovered: bool = False,
) -> GroundedEvidence:
    corrections = []
    if excerpt_recovered:
        corrections.append("trecho_aterrado")
    if entry.document_id != str(original.get("document_id") or ""):
        corrections.append("documento_corrigido")
    if entry.page != int(original.get("page") or 0):
        corrections.append("pagina_corrigida")
    if entry.sha256 != str(original.get("sha256") or ""):
        corrections.append("sha256_corrigido")
    return GroundedEvidence(
        document_id=entry.document_id,
        page=entry.page,
        excerpt=excerpt,
        sha256=entry.sha256,
        grounded=True,
        corrections=tuple(corrections),
        similarity=similarity,
    )


def _ungrounded(original: dict[str, Any]) -> GroundedEvidence:
    return GroundedEvidence(
        document_id=str(original.get("document_id") or ""),
        page=int(original.get("page") or 0),
        excerpt=_collapse(str(original.get("excerpt") or "")),
        sha256=str(original.get("sha256") or ""),
        grounded=False,
    )


def _literal_recovery(
    index: list[_PageEntry],
    original: dict[str, Any],
    excerpt: str,
) -> GroundedEvidence | None:
    folded = excerpt.casefold()
    hits = [entry for entry in index if folded in entry.folded]
    if not hits:
        return None
    claimed_document = str(original.get("document_id") or "")
    claimed_page = int(original.get("page") or 0)
    exact = [
        entry
        for entry in hits
        if entry.document_id == claimed_document and entry.page == claimed_page
    ]
    if exact:
        return _grounded(exact[0], excerpt, original)
    same_document = [entry for entry in hits if entry.document_id == claimed_document]
    if same_document:
        return _grounded(same_document[0], excerpt, original)
    other_documents = {entry.document_id for entry in hits}
    if len(other_documents) == 1:
        return _grounded(hits[0], excerpt, original)
    # Trecho presente em vários documentos diferentes: remapear seria chute.
    return None


def _span_from_matching_blocks(
    matcher: SequenceMatcher, entry: _PageEntry, excerpt_folded: str
) -> tuple[int, int] | None:
    blocks = [block for block in matcher.get_matching_blocks() if block.size > 0]
    if not blocks:
        return None
    start = blocks[0].a
    end = blocks[-1].a + blocks[-1].size
    if end - start > min(MAX_EXCERPT_CHARS, 2 * len(excerpt_folded) + 50):
        return None
    return start, end


def _best_window(
    entry: _PageEntry, excerpt_folded: str
) -> tuple[float, str] | None:
    """Melhor fragmento literal da página para o trecho parafraseado."""
    matcher = SequenceMatcher(None, entry.folded, excerpt_folded, autojunk=False)
    span = _span_from_matching_blocks(matcher, entry, excerpt_folded)
    best: tuple[float, str] | None = None
    if span:
        start, end = span
        candidate = _collapse(entry.text[start:end])
        if len(candidate) >= MIN_EXCERPT_CHARS:
            ratio = SequenceMatcher(
                None, candidate.casefold(), excerpt_folded, autojunk=False
            ).ratio()
            best = (ratio, candidate)
    windowed = _best_sliding_window(entry, excerpt_folded)
    if windowed and (best is None or windowed[0] > best[0]):
        best = windowed
    return best


def _best_sliding_window(
    entry: _PageEntry, excerpt_folded: str
) -> tuple[float, str] | None:
    words = [
        (match.start(), match.end())
        for match in re.finditer(r"\S+", entry.text)
    ]
    if not words:
        return None
    target_words = max(1, len(excerpt_folded.split()))
    sizes = sorted(
        {
            max(1, round(target_words * factor))
            for factor in (0.6, 0.8, 1.0, 1.2, 1.5)
        }
    )
    best: tuple[float, str] | None = None
    for size in sizes:
        if size > len(words):
            size = len(words)
        for start_index in range(0, len(words) - size + 1):
            start = words[start_index][0]
            end = words[start_index + size - 1][1]
            candidate = entry.text[start:end]
            if len(candidate) < MIN_EXCERPT_CHARS or len(candidate) > MAX_EXCERPT_CHARS:
                continue
            folded_candidate = candidate.casefold()
            probe = SequenceMatcher(
                None, folded_candidate, excerpt_folded, autojunk=False
            )
            if probe.real_quick_ratio() <= (best[0] if best else 0.0):
                continue
            if probe.quick_ratio() <= (best[0] if best else 0.0):
                continue
            ratio = probe.ratio()
            if best is None or ratio > best[0]:
                best = (ratio, candidate)
        if size == len(words):
            break
    return best


def _fuzzy_recovery(
    index: list[_PageEntry],
    original: dict[str, Any],
    excerpt: str,
    minimum_similarity: float,
) -> GroundedEvidence | None:
    claimed_document = str(original.get("document_id") or "")
    claimed_page = int(original.get("page") or 0)
    scoped = [entry for entry in index if entry.document_id == claimed_document]
    if not scoped:
        return None
    excerpt_folded = excerpt.casefold()

    def coverage(entry: _PageEntry) -> float:
        matcher = SequenceMatcher(None, entry.folded, excerpt_folded, autojunk=False)
        return sum(block.size for block in matcher.get_matching_blocks()) / max(
            1, len(excerpt_folded)
        )

    ranked = sorted(
        scoped,
        key=lambda entry: (entry.page != claimed_page, -coverage(entry)),
    )[:_MAX_FUZZY_PAGES]
    best: tuple[float, str, _PageEntry] | None = None
    for entry in ranked:
        candidate = _best_window(entry, excerpt_folded)
        if candidate and (best is None or candidate[0] > best[0]):
            best = (candidate[0], candidate[1], entry)
    if best is None or best[0] < minimum_similarity:
        return None
    ratio, fragment, entry = best
    return _grounded(
        entry,
        _clamp_excerpt(fragment),
        original,
        similarity=round(ratio, 4),
        excerpt_recovered=True,
    )


def ground_evidence(
    evidence: dict[str, Any],
    index: list[_PageEntry],
    *,
    minimum_similarity: float = DEFAULT_MINIMUM_SIMILARITY,
) -> GroundedEvidence:
    """Devolve a prova aterrada ou a original intacta quando irrecuperável."""
    excerpt = _clamp_excerpt(str(evidence.get("excerpt") or ""))
    if len(excerpt) < MIN_EXCERPT_CHARS or not index:
        return _ungrounded(evidence)
    literal = _literal_recovery(index, evidence, excerpt)
    if literal is not None:
        return literal
    fuzzy = _fuzzy_recovery(index, evidence, excerpt, minimum_similarity)
    if fuzzy is not None:
        return fuzzy
    return _ungrounded(evidence)


def ground_evidence_list(
    items: list[dict[str, Any]],
    index: list[_PageEntry],
    *,
    minimum_similarity: float = DEFAULT_MINIMUM_SIMILARITY,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Aterra uma lista de provas e devolve contadores agregados opacos."""
    stats = {
        "evidence_total": 0,
        "excerpts_recovered": 0,
        "pages_corrected": 0,
        "hashes_corrected": 0,
        "documents_corrected": 0,
    }
    grounded_items: list[dict[str, Any]] = []
    for item in items:
        stats["evidence_total"] += 1
        result = ground_evidence(item, index, minimum_similarity=minimum_similarity)
        if result.grounded and result.corrections:
            if "trecho_aterrado" in result.corrections:
                stats["excerpts_recovered"] += 1
            if "pagina_corrigida" in result.corrections:
                stats["pages_corrected"] += 1
            if "sha256_corrigido" in result.corrections:
                stats["hashes_corrected"] += 1
            if "documento_corrigido" in result.corrections:
                stats["documents_corrected"] += 1
            grounded_items.append({**item, **result.as_dict()})
        else:
            grounded_items.append(dict(item))
    return grounded_items, stats
