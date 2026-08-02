"""Comprehensive Unit & Integration Test Suite for Milestone M3:

- Text Extraction Cascade (Tiers 1-4 with tier metadata tagging).
- Structured Entity Extraction Engine (parties, timeline, decisions, orders, deadlines).
- Conflict Engine with Explicit Abstention logic.
- End-to-End Integration Flow.
"""

from __future__ import annotations

import conflict_engine
import structuring_engine
import text_cascade

# --- Tier 1-4 Text Cascade Tests ---

class TestTextCascade:
    def test_tier1_native_extraction(self):
        sample_native_text = (
            "EXCELENTÍSSIMO SENHOR DOUTOR JUIZ DE DIREITO DA 1ª VARA CÍVEL DA COMARCA DE BELÉM - PA.\n"
            "Processo nº 0801234-56.2024.8.14.0001\n"
            "JOÃO DA SILVA, brasileiro, casado, inscrito no CPF sob o nº 123.456.789-00, vem ajuizar\n"
            "AÇÃO DE COBRANÇA em face de MARIA DOS SANTOS, inscrita no CPF sob o nº 987.654.321-11.\n"
            "Nestes termos, pede deferimento. Belém-PA, 10 de janeiro de 2024."
        )

        page_result = text_cascade.extract_page_cascade(sample_native_text, page_num=1)
        assert page_result.tier_used == text_cascade.TIER1_NATIVE
        assert page_result.confidence >= 0.90
        assert page_result.page_number == 1
        assert "JOÃO DA SILVA" in page_result.extracted_text
        assert page_result.is_scanned is False
        assert len(page_result.tier_history) >= 1
        assert page_result.tier_history[0]["tier"] == text_cascade.TIER1_NATIVE

    def test_tier2_layout_table_extraction(self):
        table_text = (
            "TABELA DE VALORES E HONORÁRIOS\n"
            "| Item | Descrição | Valor (R$) |\n"
            "| --- | --- | --- |\n"
            "| 1 | Dano Material | 15.000,00 |\n"
            "| 2 | Dano Moral | 10.000,00 |\n"
            "| Total | Soma das Condenações | 25.000,00 |\n"
        )

        page_result = text_cascade.extract_page_cascade(table_text, page_num=2)
        assert page_result.tier_used == text_cascade.TIER2_LAYOUT_TABLES
        assert page_result.confidence >= 0.85
        assert len(page_result.tables) >= 1
        assert page_result.quality_metrics["table_count"] >= 1

    def test_tier3_selective_ocr_extraction(self):
        scanned_dummy_text = "Página digitalizada sem camada de texto nativa válida."

        page_result = text_cascade.extract_page_cascade(
            scanned_dummy_text, page_num=3, force_tier=text_cascade.TIER3_SELECTIVE_OCR
        )
        assert page_result.tier_used == text_cascade.TIER3_SELECTIVE_OCR
        assert page_result.is_scanned is True
        assert page_result.confidence >= 0.80

    def test_tier4_multimodal_vision_fallback(self):
        vision_trigger_text = (
            "PETIÇÃO INICIAL COM ANOTAÇÃO MANUSCRITA À MARGEM\n"
            "CARIMBO DE PROTOCOLO PJE Nº 998822\n"
            "VERIFICAR CROQUI / DIAGRAMA DO IMÓVEL ANEXO"
        )

        page_result = text_cascade.extract_page_cascade(vision_trigger_text, page_num=4)
        assert page_result.tier_used == text_cascade.TIER4_MULTIMODAL_VISION
        assert page_result.confidence >= 0.85
        assert page_result.has_handwriting_or_stamps is True
        assert len(page_result.visual_elements) >= 1
        assert any(e["type"] in ("stamp", "handwritten_note", "diagram", "signature") for e in page_result.visual_elements)

    def test_full_document_cascade_result(self):
        pages_input = [
            "Petição inicial nativa digital. Autor: Pessoa Sintética. CPF: 111.222.333-44.",
            "| Parcela | Valor |\n| 1 | R$ 500 |",
            "CARIMBO DE RECEBIMENTO E RUBRICA MANUSCRITA",
        ]

        result = text_cascade.extract_text_cascade(pages_input)
        assert isinstance(result, text_cascade.TextCascadeResult)
        assert result.total_pages == 3
        assert len(result.pages) == 3
        assert result.overall_quality > 0.80
        assert text_cascade.TIER1_NATIVE in result.tier_breakdown
        assert text_cascade.TIER4_MULTIMODAL_VISION in result.tier_breakdown


# --- Structuring Engine Tests ---

class TestStructuringEngine:
    def test_parties_extraction(self):
        text = (
            "AUTOR: JOÃO DOS SANTOS, CPF: 123.456.789-00\n"
            "RÉU: BANCO DO BRASIL S/A, CNPJ: 00.000.000/0001-91\n"
            "ADVOGADO: DR. CARLOS SILVA, OAB/PA 12345"
        )

        sp = structuring_engine.structure_process(text)
        assert len(sp.parties) >= 3

        roles = [p.role for p in sp.parties]
        assert "polo_ativo" in roles
        assert "polo_passivo" in roles
        assert "advogado" in roles

        autor = next(p for p in sp.parties if p.role == "polo_ativo")
        assert "JOÃO DOS SANTOS" in autor.name.upper()
        assert autor.identifier == "123.456.789-00"

        reu = next(p for p in sp.parties if p.role == "polo_passivo")
        assert "BANCO DO BRASIL" in reu.name.upper()
        assert reu.identifier == "00.000.000/0001-91"

    def test_timeline_events_extraction(self):
        text = (
            "Petição Inicial protocolada em 10/01/2024.\n"
            "Citação realizada em 20/01/2024.\n"
            "Contestação apresentada em 05/02/2024.\n"
            "Sentença proferida em 15/03/2024."
        )

        sp = structuring_engine.structure_process(text)
        assert len(sp.timeline) >= 4

        event_types = [t.event_type for t in sp.timeline]
        assert "peticao_inicial" in event_types
        assert "citacao" in event_types
        assert "contestacao" in event_types
        assert "sentenca" in event_types

    def test_judicial_decisions_extraction(self):
        text = (
            "DECISÃO INTERLOCUTÓRIA\n"
            "DEFIRO O PEDIDO DE TUTELA DE URGÊNCIA para determinar a suspensão da busca e apreensão.\n"
            "POSTO ISSO, JULGO PROCEDENTE O PEDIDO INICIAL."
        )

        sp = structuring_engine.structure_process(text)
        assert len(sp.decisions) >= 1
        dec = sp.decisions[0]
        assert dec.decision_type in ("sentenca", "tutela_urgencia", "decisao_interlocutoria")
        assert len(dec.granted_reliefs) >= 1

    def test_procedural_deadline_business_days_logic(self):
        # 2026-08-03 is a Monday
        start_date = "2026-08-03"
        # 5 business days: Tue (Aug 4), Wed (Aug 5), Thu (Aug 6), Fri (Aug 7), Mon (Aug 10)
        due_date = structuring_engine.calculate_procedural_deadline(
            start_date=start_date, days=5, day_type="business_days"
        )
        assert due_date == "2026-08-10"

    def test_procedural_deadline_calendar_days_prorogation_logic(self):
        # 2026-08-03 is a Monday. 5 calendar days lands on Saturday 2026-08-08 -> prorogates to Monday 2026-08-10
        start_date = "2026-08-03"
        due_date = structuring_engine.calculate_procedural_deadline(
            start_date=start_date, days=5, day_type="calendar_days"
        )
        assert due_date == "2026-08-10"


# --- Conflict Engine & Explicit Abstention Tests ---

class TestConflictEngine:
    def test_party_identifier_conflict_explicit_abstention(self):
        sp = structuring_engine.StructuredProcess(
            parties=[
                structuring_engine.Party(name="Maria Silva", role="polo_passivo", specific_role="Ré", identifier="111.111.111-11", document_source="Doc 1"),
                structuring_engine.Party(name="Maria Silva", role="polo_passivo", specific_role="Ré", identifier="999.999.999-99", document_source="Doc 2"),
            ]
        )

        report = conflict_engine.detect_conflicts(sp)
        assert report.has_conflicts is True
        assert report.total_conflicts >= 1

        conf_item = report.conflicts[0]
        assert conf_item.category == "party_identifier"
        assert conf_item.status == "abstained"
        assert conf_item.abstention_reason == "contradictory_evidence"

        assert len(report.abstained_facts) >= 1
        abs_fact = report.abstained_facts[0]
        assert abs_fact.status == "abstained"
        assert abs_fact.reason == "contradictory_evidence"

    def test_timeline_date_conflict_explicit_abstention(self):
        sp = structuring_engine.StructuredProcess(
            timeline=[
                structuring_engine.TimelineEvent(event_id="e1", date="2024-01-10", event_type="citacao", description="Citação segundo Certidão A", document_ref="Doc A"),
                structuring_engine.TimelineEvent(event_id="e2", date="2024-01-20", event_type="citacao", description="Citação segundo Petição B", document_ref="Doc B"),
            ]
        )

        report = conflict_engine.detect_conflicts(sp)
        assert report.has_conflicts is True

        timeline_conf = next(c for c in report.conflicts if c.category == "timeline_date")
        assert timeline_conf.status == "abstained"
        assert timeline_conf.abstention_reason == "contradictory_evidence"

    def test_opposing_judicial_orders_conflict_explicit_abstention(self):
        sp = structuring_engine.StructuredProcess(
            orders=[
                structuring_engine.JudicialOrder(order_id="o1", order_type="determinacao", target_party="Réu", command_summary="DEFIRO A LIMINAR para suspender a desocupação.", document_ref="Decisão 1"),
                structuring_engine.JudicialOrder(order_id="o2", order_type="determinacao", target_party="Réu", command_summary="INDEFIRO A LIMINAR e determino a imediata desocupação.", document_ref="Decisão 2"),
            ]
        )

        report = conflict_engine.detect_conflicts(sp)
        assert report.has_conflicts is True

        order_conf = next(c for c in report.conflicts if c.category == "opposing_judicial_orders")
        assert order_conf.status == "abstained"
        assert order_conf.abstention_reason == "contradictory_evidence"

    def test_insufficient_evidence_explicit_abstention(self):
        evidence_items = [
            {"source": "Certidão Incompleta", "value": "desconhecido", "confidence": 0.30}
        ]

        res = conflict_engine.evaluate_fact_with_abstention("Data do Óbito do Causa Mortis", evidence_items)
        assert res["status"] == "abstained"
        assert res["reason"] == "insufficient_evidence"
        assert res["value"] is None
        assert "insuficiente" in res["explanation"].lower() or "abstenção" in res["explanation"].lower()

    def test_clean_consistent_process_no_conflicts(self):
        sp = structuring_engine.StructuredProcess(
            parties=[
                structuring_engine.Party(name="Carlos Ramos", role="polo_ativo", specific_role="Autor", identifier="123.456.789-00"),
                structuring_engine.Party(name="Ana Lima", role="polo_passivo", specific_role="Ré", identifier="987.654.321-11"),
            ],
            timeline=[
                structuring_engine.TimelineEvent(event_id="e1", date="2024-01-10", event_type="peticao_inicial", description="Petição Inicial"),
                structuring_engine.TimelineEvent(event_id="e2", date="2024-01-15", event_type="citacao", description="Citação do Réu"),
            ],
            orders=[
                structuring_engine.JudicialOrder(order_id="o1", order_type="intimacao", target_party="Réu", command_summary="INTIME-SE para manifestação no prazo de 15 dias."),
            ]
        )

        report = conflict_engine.detect_conflicts(sp)
        assert report.has_conflicts is False
        assert len(report.conflicts) == 0


# --- End-to-End Integration Test ---

class TestE2EIntegrationM3:
    def test_full_pipeline_cascade_structuring_conflict(self):
        doc_pages = [
            (
                "EXCELENTÍSSIMO SENHOR JUIZ DE DIREITO DA 2ª VARA CÍVEL DE BELÉM.\n"
                "AUTOR: PAULO GOMES, CPF: 111.222.333-44\n"
                "RÉU: SEGURADORA X, CNPJ: 22.333.444/0001-55\n"
                "Petiçãio inicial apresentada em 01/02/2024."
            ),
            (
                "TABELA DE DANOS MATERIAIS\n"
                "| Danos | Valor |\n"
                "| Veículo | R$ 30.000 |\n"
                "INTIME-SE o réu para pagar em 15 dias."
            ),
            (
                "CARIMBO DE JUNÇÃO E ASSINATURA DIGITADO PJE\n"
                "DECISÃO: DEFIRO A TUTELA DE URGÊNCIA."
            )
        ]

        # Step 1: Text Cascade
        cascade_result = text_cascade.extract_text_cascade(doc_pages)
        assert cascade_result.total_pages == 3
        assert cascade_result.overall_quality > 0.80

        # Step 2: Structuring Engine
        structured_proc = structuring_engine.structure_process(cascade_result.to_dict())
        assert len(structured_proc.parties) >= 2
        assert len(structured_proc.deadlines) >= 1

        # Step 3: Conflict Engine
        report = conflict_engine.detect_conflicts(structured_proc)
        assert isinstance(report, conflict_engine.ConflictReport)
        assert report.summary != ""
