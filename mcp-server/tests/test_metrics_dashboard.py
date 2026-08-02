"""Unit tests for Metrics Collector, Dashboard HTML Generator, and CLI PainelRevisao (`test_metrics_dashboard.py`).
"""

from __future__ import annotations

from generate_pje_dashboard import generate_html_dashboard
from metrics_collector import MetricsCollector
from painel_revisao import PainelRevisao


def test_metrics_collector_technical_and_domain():
    collector = MetricsCollector()

    # Record technical metrics
    collector.record_extraction_time("tier1_native", 0.45)
    collector.record_extraction_time("tier1_native", 0.55)
    collector.record_extraction_time("tier3_ocr", 2.10)

    collector.record_ocr_execution(fallback_occurred=True)
    collector.record_ocr_execution(fallback_occurred=False)

    collector.record_cache_access("hit")
    collector.record_cache_access("miss")
    collector.record_cache_access("negative_hit")

    # Record domain metrics
    collector.record_completeness_score(0.85)
    collector.record_completeness_score(0.95)
    collector.record_missing_requirements(["Certidão de Óbito", "Procuração"])
    collector.record_missing_requirements(["Certidão de Óbito"])
    collector.record_domain_profile("inventory/v1")
    collector.record_domain_profile("adverse-possession/v1")
    collector.record_domain_profile("inventory/v1")

    summary = collector.get_metrics_summary()
    tech = summary["technical_metrics"]
    domain = summary["domain_metrics"]

    assert tech["extraction_time_per_tier_avg"]["tier1_native"] == 0.5
    assert tech["extraction_time_per_tier_avg"]["tier3_ocr"] == 2.1
    assert tech["ocr_attempts"] == 2
    assert tech["ocr_fallbacks"] == 1
    assert tech["ocr_fallback_rate"] == 0.5
    assert tech["cache_hits"] == 1
    assert tech["cache_misses"] == 1
    assert tech["cache_negative_hits"] == 1
    assert tech["cache_efficiency"] == round(2 / 3, 4)

    assert domain["total_analyses_evaluated"] == 2
    assert domain["average_completeness_score"] == 0.90
    assert domain["top_missing_requirements"][0] == ("Certidão de Óbito", 2)
    assert domain["domain_profile_counts"] == {"inventory/v1": 2, "adverse-possession/v1": 1}

    # Test Reset
    collector.reset()
    clean_summary = collector.get_metrics_summary()
    assert clean_summary["technical_metrics"]["ocr_attempts"] == 0
    assert clean_summary["domain_metrics"]["total_analyses_evaluated"] == 0


def test_html_dashboard_generation():
    sample_report = {
        "process_number": "0801234-56.2026.8.14.0001",
        "status": "completed",
        "completeness": {
            "overall_score": 0.88,
            "dimensions": {
                "origin": 0.95,
                "documents": 0.90,
                "attachments": 0.85,
                "pages": 0.90,
                "metadata": 0.80,
                "temporal": 0.88,
            },
        },
        "domain_profile": {
            "profile_name": "inventory/v1",
            "entities": {
                "deceased": {"name": "João da Silva", "cpf": "123.456.789-00"},
                "inventariante": {"name": "Maria da Silva"},
            },
        },
        "missing_documents": ["Certidão de Quitação Fiscal Estadual"],
        "timeline": [
            {"date": "10/01/2026", "title": "Petição Inicial", "description": "Abertura de Inventário"},
            {"date": "15/01/2026", "title": "Nomeação de Inventariante"},
        ],
        "conflicts": [
            {
                "category": "timeline_date",
                "topic": "Data do Óbito",
                "status": "abstained",
                "abstention_reason": "contradictory_evidence",
            }
        ],
    }

    html_out = generate_html_dashboard(sample_report)
    assert isinstance(html_out, str)
    assert len(html_out) > 500
    assert "0801234-56.2026.8.14.0001" in html_out
    assert "Painel de Análise Processual" in html_out
    assert "88%" in html_out
    assert "inventory/v1" in html_out
    assert "Certidão de Quitação Fiscal Estadual" in html_out
    assert "ABSTENÇÃO EXPLÍCITA" in html_out
    assert "Data do Óbito" in html_out


def test_painel_revisao_cli():
    sample_report = {
        "process_number": "0809999-11.2026.8.14.0001",
        "status": "completed",
        "completeness": {
            "overall_score": 0.75,
            "dimensions": {
                "origin": 1.0,
                "documents": 0.70,
                "attachments": 0.80,
                "pages": 0.70,
                "metadata": 0.80,
                "temporal": 0.75,
            },
        },
        "domain_profile": {
            "profile_name": "adverse-possession/v1",
            "details": {"possession_years": 15, "land_area_m2": 250},
        },
        "timeline": [{"date": "01/02/2026", "title": "Citação dos Confinantes"}],
        "conflicts": [
            {
                "category": "property_boundary",
                "topic": "Área do Imóvel",
                "status": "abstained",
                "abstention_reason": "insufficient_evidence",
            }
        ],
    }

    panel = PainelRevisao(sample_report)

    summary_text = panel.render_summary()
    assert "0809999-11.2026.8.14.0001" in summary_text
    assert "75%" in summary_text

    completeness_text = panel.render_completeness()
    assert "COMPLETUDE MULTIDIMENSIONAR 6D" in completeness_text
    assert "75%" in completeness_text

    domain_text = panel.render_domain_profile()
    assert "adverse-possession/v1" in domain_text
    assert "possession_years" in domain_text

    conflicts_text = panel.render_conflicts()
    assert "ABSTAINED" in conflicts_text
    assert "insufficient_evidence" in conflicts_text

    full_report = panel.render_full_report()
    assert summary_text in full_report
    assert completeness_text in full_report
    assert domain_text in full_report
    assert conflicts_text in full_report
