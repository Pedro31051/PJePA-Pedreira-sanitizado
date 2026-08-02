import hashlib

import pytest

from app.verifier import ReportRejected, validate_inventory_report

TEXT = "Em 10 de janeiro de 2025 foi juntada a certidão de óbito."
DIGEST = hashlib.sha256(TEXT.encode()).hexdigest()


def _dossier():
    return {
        "process_number": "0000000-00.2025.8.14.0000",
        "documents": [
            {
                "document_id": "doc-1",
                "sha256": DIGEST,
                "pages": [{"page": 1, "text": TEXT}],
            }
        ],
    }


def _report():
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
                        "page": 1,
                        "excerpt": "foi juntada a certidão de óbito",
                        "sha256": DIGEST,
                    }
                ],
            }
        ],
    }


def test_accepts_literal_evidence() -> None:
    report = validate_inventory_report(_report(), _dossier())
    assert report.read_only is True


def test_rejects_hallucinated_excerpt() -> None:
    payload = _report()
    payload["acts"][0]["evidence"][0]["excerpt"] = "trecho que não existe nos autos"
    with pytest.raises(ReportRejected, match="não é literal"):
        validate_inventory_report(payload, _dossier())


def test_rejects_non_read_only_report() -> None:
    payload = _report()
    payload["read_only"] = False
    with pytest.raises(ReportRejected, match="somente leitura"):
        validate_inventory_report(payload, _dossier())


def _evidence(excerpt: str):
    return {
        "document_id": "doc-1",
        "page": 1,
        "excerpt": excerpt,
        "sha256": DIGEST,
    }


def test_accepts_verified_contradiction_and_conclusion() -> None:
    payload = _report()
    payload["contradictions"] = [
        {
            "subject": "Data da juntada",
            "description": "Contradição sintética entre datas.",
            "evidence": [
                _evidence("Em 10 de janeiro de 2025"),
                _evidence("foi juntada a certidão de óbito"),
            ],
        }
    ]
    payload["conclusions"] = [
        {
            "conclusion": "A certidão de óbito está nos autos.",
            "supporting_act_sequences": [1],
            "evidence": [_evidence("certidão de óbito")],
        }
    ]
    report = validate_inventory_report(payload, _dossier())
    assert len(report.contradictions) == 1
    assert len(report.conclusions) == 1


def test_rejects_contradiction_with_fabricated_evidence() -> None:
    payload = _report()
    payload["contradictions"] = [
        {
            "subject": "Data da juntada",
            "description": "Contradição sintética entre datas.",
            "evidence": [
                _evidence("Em 10 de janeiro de 2025"),
                {
                    "document_id": "doc-1",
                    "page": 1,
                    "excerpt": "o oficial devolveu o mandado sem cumprimento",
                    "sha256": DIGEST,
                },
            ],
        }
    ]
    with pytest.raises(ReportRejected, match="não é literal"):
        validate_inventory_report(payload, _dossier())


def test_rejects_conclusion_pointing_to_missing_act() -> None:
    payload = _report()
    payload["conclusions"] = [
        {
            "conclusion": "A certidão de óbito está nos autos.",
            "supporting_act_sequences": [9],
            "evidence": [_evidence("certidão de óbito")],
        }
    ]
    with pytest.raises(ReportRejected, match="ato inexistente"):
        validate_inventory_report(payload, _dossier())


def test_rejects_unofficial_normative_source() -> None:
    payload = _report()
    payload["acts"][0]["normative_sources"] = [
        {
            "authority": "CPC",
            "title": "Fonte não oficial",
            "provision": "art. 610",
            "url": "https://example.com/blog",
            "retrieved_excerpt": "trecho recuperado da fonte não oficial",
            "relevance": "teste",
        }
    ]
    with pytest.raises(ReportRejected, match="não oficial"):
        validate_inventory_report(payload, _dossier())
