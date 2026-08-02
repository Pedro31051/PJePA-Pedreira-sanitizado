"""Aterramento literal no verificador do agente, com dados sintéticos."""

import hashlib

import pytest

from app.grounding import build_page_index, ground_evidence
from app.verifier import ReportRejected, validate_inventory_report

PAGE_1 = (
    "Em 10 de janeiro de 2025 foi juntada a certidão de óbito do autor "
    "da herança, lavrada pelo cartório sintético."
)
PAGE_2 = (
    "As primeiras declarações foram apresentadas em 13 de maio de 2025 "
    "pela inventariante nomeada."
)
DIGEST = hashlib.sha256((PAGE_1 + PAGE_2).encode()).hexdigest()


def _dossier():
    return {
        "process_number": "0000000-00.2025.8.14.0000",
        "documents": [
            {
                "document_id": "doc-1",
                "sha256": DIGEST,
                "pages": [
                    {"page": 1, "text": PAGE_1},
                    {"page": 2, "text": PAGE_2},
                ],
            }
        ],
    }


def _report(excerpt: str, sha256: str = DIGEST, page: int = 1):
    return {
        "process_number": "0000000-00.2025.8.14.0000",
        "executive_summary": "Há uma certidão de óbito juntada.",
        "documents_reviewed": ["doc-1"],
        "acts": [
            {
                "sequence": 1,
                "date": "2025-01-10",
                "act_type": "documento_prova",
                "actor": None,
                "summary": "Juntada da certidão de óbito.",
                "procedural_effect": "Documenta o falecimento alegado.",
                "evidence": [
                    {
                        "document_id": "doc-1",
                        "page": page,
                        "excerpt": excerpt,
                        "sha256": sha256,
                    }
                ],
            }
        ],
    }


def test_grounds_paraphrased_excerpt_to_literal_fragment() -> None:
    report = validate_inventory_report(
        _report("foi juntada aos autos a certidão de óbito do autor da herança"),
        _dossier(),
    )
    excerpt = report.acts[0].evidence[0].excerpt
    assert excerpt.casefold() in " ".join(PAGE_1.split()).casefold()


def test_grounds_altered_hash_and_wrong_page() -> None:
    report = validate_inventory_report(
        _report(
            "primeiras declarações foram apresentadas em 13 de maio de 2025",
            sha256="f" * 64,
            page=1,
        ),
        _dossier(),
    )
    evidence = report.acts[0].evidence[0]
    assert evidence.sha256 == DIGEST
    assert evidence.page == 2


def test_still_rejects_unrecoverable_evidence() -> None:
    with pytest.raises(ReportRejected, match="não é literal"):
        validate_inventory_report(
            _report("o tribunal condenou o réu ao pagamento de multa diária"),
            _dossier(),
        )


def test_ground_evidence_never_invents_document() -> None:
    index = build_page_index(_dossier()["documents"])
    result = ground_evidence(
        {
            "document_id": "doc-9",
            "page": 1,
            "excerpt": "trecho inexistente em qualquer página",
            "sha256": "a" * 64,
        },
        index,
    )
    assert result.grounded is False
