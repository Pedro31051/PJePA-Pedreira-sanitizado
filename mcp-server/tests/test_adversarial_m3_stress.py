"""Adversarial Stress Test Suite for Milestone M3 (test_adversarial_m3_stress.py).

Focuses on:
1. Dynamic stress testing against conflict_engine.py and structuring_engine.py procedural deadline logic.
2. Conflicting party CPFs/CNPJs, conflicting timeline dates, opposing judicial orders, invalid date formats.
3. Explicit abstention status ("abstained", "contradictory_evidence", "insufficient_evidence") verification.
4. Exception safety, missing fields, unexpected types, and boundary edge cases.
"""

from __future__ import annotations

import pytest

import conflict_engine
import structuring_engine
import text_cascade


class TestPartyConflictAdversarial:
    """Stress tests for conflicting party identifiers (CPF/CNPJ)."""

    def test_conflicting_cpf_same_party_name(self):
        sp = structuring_engine.StructuredProcess(
            parties=[
                structuring_engine.Party(
                    name="João da Silva",
                    role="polo_ativo",
                    specific_role="Autor",
                    identifier="123.456.789-00",
                    document_source="Petição Inicial (Doc 1)",
                ),
                structuring_engine.Party(
                    name="João da Silva",
                    role="polo_ativo",
                    specific_role="Autor",
                    identifier="987.654.321-99",
                    document_source="Certidão RDT (Doc 5)",
                ),
            ]
        )

        report = conflict_engine.detect_conflicts(sp)
        assert report.has_conflicts is True
        assert report.total_conflicts >= 1

        party_conflicts = [c for c in report.conflicts if c.category == "party_identifier"]
        assert len(party_conflicts) == 1
        conf = party_conflicts[0]
        assert conf.status == "abstained"
        assert conf.abstention_reason == "contradictory_evidence"
        assert len(conf.conflicting_claims) == 2

    def test_conflicting_cnpj_same_company_name(self):
        sp = structuring_engine.StructuredProcess(
            parties=[
                structuring_engine.Party(
                    name="Empresa X Ltda",
                    role="polo_passivo",
                    specific_role="Réu",
                    identifier="11.222.333/0001-44",
                    document_source="Contrato Social",
                ),
                structuring_engine.Party(
                    name="Empresa X Ltda",
                    role="polo_passivo",
                    specific_role="Réu",
                    identifier="99.888.777/0001-11",
                    document_source="Notificação Extrajudicial",
                ),
            ]
        )

        report = conflict_engine.detect_conflicts(sp)
        assert report.has_conflicts is True
        cnpj_conflicts = [c for c in report.conflicts if c.category == "party_identifier"]
        assert len(cnpj_conflicts) == 1
        assert cnpj_conflicts[0].status == "abstained"
        assert cnpj_conflicts[0].abstention_reason == "contradictory_evidence"

    def test_party_name_deduplication_preserves_conflicting_identifiers(self):
        """Verify that extract_parties preserves party entries with identical names but different CPFs/CNPJs so conflict_engine detects the conflict."""
        text_with_party_conflict = (
            "AUTOR: MARCOS SOUZA, CPF: 111.111.111-11\n"
            "REQUERENTE: MARCOS SOUZA, CPF: 222.222.222-22\n"
            "RÉU: BANCO Y S/A, CNPJ: 00.111.222/0001-33"
        )
        sp = structuring_engine.structure_process(text_with_party_conflict)
        assert len([p for p in sp.parties if "marcos souza" in p.name.lower()]) == 2
        party_conflicts = [c for c in (sp.conflict_report.get("conflicts", []) if sp.conflict_report else []) if c.get("category") == "party_identifier"]
        assert len(party_conflicts) == 1
        assert party_conflicts[0].get("status") == "abstained"


class TestTimelineConflictAdversarial:
    """Stress tests for conflicting timeline event dates."""

    def test_conflicting_citacao_dates(self):
        sp = structuring_engine.StructuredProcess(
            timeline=[
                structuring_engine.TimelineEvent(
                    event_id="e1",
                    date="2024-01-10",
                    event_type="citacao",
                    description="Citação efetuada segundo certidão",
                    document_ref="Certidão de Citação",
                ),
                structuring_engine.TimelineEvent(
                    event_id="e2",
                    date="2024-01-25",
                    event_type="citacao",
                    description="Citação alegada pelo autor",
                    document_ref="Petição do Autor",
                ),
            ]
        )

        report = conflict_engine.detect_conflicts(sp)
        assert report.has_conflicts is True
        timeline_conflicts = [c for c in report.conflicts if c.category == "timeline_date"]
        assert len(timeline_conflicts) == 1
        assert timeline_conflicts[0].status == "abstained"
        assert timeline_conflicts[0].abstention_reason == "contradictory_evidence"


class TestJudicialOrderConflictAdversarial:
    """Stress tests for opposing judicial orders and contradictory decisions."""

    def test_opposing_orders_defer_and_indefer(self):
        sp = structuring_engine.StructuredProcess(
            orders=[
                structuring_engine.JudicialOrder(
                    order_id="o1",
                    order_type="determinacao",
                    target_party="Réu",
                    command_summary="DEFIRO A LIMINAR para suspensão do leilão.",
                    document_ref="Decisão 1",
                ),
                structuring_engine.JudicialOrder(
                    order_id="o2",
                    order_type="determinacao",
                    target_party="Réu",
                    command_summary="INDEFIRO O PEDIDO LIMINAR e mantenho a praça pública.",
                    document_ref="Decisão 2",
                ),
            ]
        )

        report = conflict_engine.detect_conflicts(sp)
        assert report.has_conflicts is True
        order_conflicts = [c for c in report.conflicts if c.category == "opposing_judicial_orders"]
        assert len(order_conflicts) == 1
        assert order_conflicts[0].status == "abstained"
        assert order_conflicts[0].abstention_reason == "contradictory_evidence"


class TestProceduralDeadlineAdversarial:
    """Stress tests for procedural deadline calculation logic."""

    def test_deadline_business_days_with_holidays(self):
        # Start date Monday 2026-08-03
        holidays = ["2026-08-04", "2026-08-05"]  # Tue & Wed are court holidays
        due = structuring_engine.calculate_procedural_deadline(
            start_date="2026-08-03",
            days=3,
            day_type="business_days",
            holidays=holidays,
        )
        assert due == "2026-08-10"

    def test_deadline_calendar_days_prorogation(self):
        due = structuring_engine.calculate_procedural_deadline(
            start_date="2026-08-03",
            days=5,
            day_type="calendar_days",
        )
        assert due == "2026-08-10"

    def test_deadline_zero_or_negative_days(self):
        due_zero = structuring_engine.calculate_procedural_deadline(
            start_date="2026-08-03",
            days=0,
            day_type="business_days",
        )
        assert due_zero == "2026-08-04"

    def test_deadline_with_invalid_start_date(self):
        due = structuring_engine.calculate_procedural_deadline(
            start_date="invalid_start_date",
            days=5,
            day_type="business_days",
        )
        assert isinstance(due, str)
        assert len(due) == 10


class TestExplicitAbstentionEngine:
    """Stress tests for explicit abstention logic in conflict_engine.py."""

    def test_evaluate_fact_empty_evidence_abstention(self):
        res = conflict_engine.evaluate_fact_with_abstention("Data da Citação", [])
        assert res["status"] == "abstained"
        assert res["reason"] == "insufficient_evidence"

    def test_evaluate_fact_contradictory_evidence_abstention(self):
        evidence = [
            {"source": "Certidão A", "value": "2024-01-10", "confidence": 0.95},
            {"source": "Petição B", "value": "2024-01-20", "confidence": 0.90},
        ]
        res = conflict_engine.evaluate_fact_with_abstention("Data de Notificação", evidence)
        assert res["status"] == "abstained"
        assert res["reason"] == "contradictory_evidence"

    def test_evaluate_fact_low_confidence_abstention(self):
        evidence = [
            {"source": "OCR de baixa qualidade", "value": "2024-01-10", "confidence": 0.45}
        ]
        res = conflict_engine.evaluate_fact_with_abstention("Data do Fato", evidence, threshold_confidence=0.80)
        assert res["status"] == "abstained"
        assert res["reason"] == "insufficient_evidence"

    def test_evaluate_fact_placeholder_value_abstention(self):
        for placeholder in ["desconhecido", "em apuração", "não informado", "none", "null"]:
            evidence = [{"source": "Doc 1", "value": placeholder, "confidence": 0.99}]
            res = conflict_engine.evaluate_fact_with_abstention("Endereço do Réu", evidence)
            assert res["status"] == "abstained"
            assert res["reason"] == "insufficient_evidence"

    def test_evaluate_fact_resolved_single_evidence(self):
        evidence = [
            {"source": "Certidão Oficial", "value": "2024-01-10", "confidence": 0.95},
            {"source": "Publicação DJE", "value": "2024-01-10", "confidence": 0.92},
        ]
        res = conflict_engine.evaluate_fact_with_abstention("Data da Citação", evidence)
        assert res["status"] == "resolved"


class TestTextCascadeTierStress:
    """Stress tests for Tier 1-4 Text Cascade Engine."""

    def test_empty_string_cascade(self):
        res = text_cascade.extract_text_cascade("")
        assert res.total_pages == 1

    def test_force_tier_overrides(self):
        sample = "EXCELENTÍSSIMO SENHOR DOUTOR JUIZ DE DIREITO DA 1ª VARA CÍVEL DA COMARCA DE BELÉM - PA. Petição de juntada."
        for tier in [
            text_cascade.TIER1_NATIVE,
            text_cascade.TIER2_LAYOUT_TABLES,
            text_cascade.TIER3_SELECTIVE_OCR,
            text_cascade.TIER4_MULTIMODAL_VISION,
        ]:
            res = text_cascade.extract_page_cascade(sample, page_num=1, force_tier=tier)
            assert res.tier_used == tier


# --- Failure Case Harness: Empirical Bug Demonstrations ---

class TestEmpiricalBugExposure:
    """Empirical tests demonstrating structural vulnerabilities and edge case bugs found during stress testing."""

    def test_bug1_evaluate_fact_with_abstention_none_values_index_error(self):
        """Demonstrate IndexError when evidence items contain only None values."""
        evidence_with_none = [{"source": "Doc 1", "value": None}]
        # Evaluating facts with None values should gracefully abstain, not throw IndexError
        try:
            res = conflict_engine.evaluate_fact_with_abstention("Data do Óbito", evidence_with_none)
            assert res["status"] == "abstained"
        except IndexError:
            pytest.fail("BUG PROVED: evaluate_fact_with_abstention raises IndexError on evidence_items with None values")

    def test_bug2_structured_process_to_dict_with_raw_dicts_attribute_error(self):
        """Demonstrate AttributeError when StructuredProcess contains dicts in list attributes."""
        sp = structuring_engine.StructuredProcess(
            parties=[{"name": "Carlos Silva", "role": "polo_ativo"}]
        )
        try:
            res_dict = sp.to_dict()
            assert isinstance(res_dict, dict)
        except AttributeError:
            pytest.fail("BUG PROVED: StructuredProcess.to_dict() raises AttributeError when list contains dict instead of Party object")

    def test_bug3_structure_process_none_text_type_error(self):
        """Demonstrate TypeError when extracted_input contains page with None text."""
        input_data = {"pages": [{"extracted_text": None}]}
        try:
            sp = structuring_engine.structure_process(input_data)
            assert isinstance(sp, structuring_engine.StructuredProcess)
        except TypeError:
            pytest.fail("BUG PROVED: structure_process raises TypeError when page extracted_text is None")

    def test_bug4_false_positive_abstention_for_dataclass_parties(self):
        """Demonstrate false positive abstention for valid Party dataclass objects in detect_conflicts."""
        sp = structuring_engine.StructuredProcess(
            parties=[
                structuring_engine.Party(name="Carlos Ramos", role="polo_ativo", specific_role="Autor", identifier="123.456.789-00"),
                structuring_engine.Party(name="Ana Lima", role="polo_passivo", specific_role="Ré", identifier="987.654.321-11"),
            ]
        )
        report = conflict_engine.detect_conflicts(sp)
        abs_parties = [a for a in report.abstained_facts if a.fact_id == "abs_insufficient_parties"]
        # Valid parties exist! Should NOT generate abs_insufficient_parties
        assert len(abs_parties) == 0, "BUG PROVED: detect_conflicts generates false positive abs_insufficient_parties for Party dataclasses"


class TestInputSanitization18EdgeCases:
    """Comprehensive test harness verifying all 18 input sanitization and edge case requirements."""

    # Fix 1.1: evidence_items list filtering (non-dict, None, integer)
    def test_case_01_evaluate_fact_non_dict_filtering(self):
        items = [None, 123, "not_a_dict", {"source": "Doc 1", "value": "2024-01-10", "confidence": 0.9}]
        res = conflict_engine.evaluate_fact_with_abstention("Topic", items)
        assert res["status"] == "resolved"
        assert res["value"] == "2024-01-10"

    # Fix 1.2: coerce source to string
    def test_case_02_evaluate_fact_null_or_non_string_source(self):
        items = [{"source": None, "value": "2024-01-10", "confidence": 0.95}]
        res = conflict_engine.evaluate_fact_with_abstention("Topic", items)
        assert res["status"] == "resolved"
        assert isinstance(res["explanation"], str)

    # Fix 1.3: convert confidence safely
    def test_case_03_evaluate_fact_invalid_confidence_types(self):
        items_none_conf = [{"source": "Doc", "value": "2024-01-10", "confidence": None}]
        res1 = conflict_engine.evaluate_fact_with_abstention("Topic", items_none_conf)
        assert res1["status"] in ("resolved", "abstained")

        items_str_conf = [{"source": "Doc", "value": "2024-01-10", "confidence": "invalid_number"}]
        res2 = conflict_engine.evaluate_fact_with_abstention("Topic", items_str_conf)
        assert res2["confidence"] == 0.0

    # Fix 1.4: evaluate_fact with all None values (no IndexError)
    def test_case_04_evaluate_fact_all_null_values(self):
        items = [{"source": "Doc 1", "value": None}, {"source": "Doc 2", "value": None}]
        res = conflict_engine.evaluate_fact_with_abstention("Topic", items)
        assert res["status"] == "abstained"
        assert res["reason"] == "insufficient_evidence"

    # Fix 1.5: _check_party_conflicts name and role coercion
    def test_case_05_check_party_conflicts_non_string_name_and_role(self):
        parties = [
            {"name": None, "role": None, "identifier": "111"},
            {"name": 12345, "role": 999, "identifier": "222"},
        ]
        conflicts = conflict_engine._check_party_conflicts(parties)
        assert isinstance(conflicts, list)

    # Fix 1.6: _check_party_conflicts list containing None items
    def test_case_06_check_party_conflicts_none_items(self):
        parties = [None, {"name": "João", "role": "polo_ativo", "identifier": "111.111.111-11"}]
        conflicts = conflict_engine._check_party_conflicts(parties)
        assert isinstance(conflicts, list)

    # Fix 1.7: _check_timeline_conflicts unhashable event_type and date
    def test_case_07_check_timeline_conflicts_unhashable_types(self):
        timeline = [
            {"event_type": ["citacao"], "date": ["2024-01-10"], "description": "Desc"},
            {"event_type": {"type": "citacao"}, "date": {"d": "2024-01-20"}, "description": "Desc"},
        ]
        conflicts = conflict_engine._check_timeline_conflicts(timeline)
        assert isinstance(conflicts, list)

    # Fix 1.8: _check_order_conflicts command_summary and dispositivo_summary coercion
    def test_case_08_check_order_conflicts_non_string_summaries(self):
        orders = [{"command_summary": None, "document_ref": None}]
        decisions = [{"dispositivo_summary": 12345, "document_ref": None}]
        conflicts = conflict_engine._check_order_conflicts(orders, decisions)
        assert isinstance(conflicts, list)

    # Fix 2.1: extract_parties coerce text parameter
    def test_case_09_extract_parties_null_or_int_text(self):
        res1 = structuring_engine.extract_parties(None)
        res2 = structuring_engine.extract_parties(12345)
        assert isinstance(res1, list)
        assert isinstance(res2, list)

    # Fix 2.2: extract_timeline coerce text parameter
    def test_case_10_extract_timeline_null_or_int_text(self):
        res1 = structuring_engine.extract_timeline(None)
        res2 = structuring_engine.extract_timeline(9999)
        assert isinstance(res1, list)
        assert isinstance(res2, list)

    # Fix 2.3: extract_decisions coerce text parameter
    def test_case_11_extract_decisions_null_or_int_text(self):
        res1 = structuring_engine.extract_decisions(None)
        res2 = structuring_engine.extract_decisions(7777)
        assert isinstance(res1, list)
        assert isinstance(res2, list)

    # Fix 2.4: extract_orders coerce text parameter
    def test_case_12_extract_orders_null_or_int_text(self):
        res1 = structuring_engine.extract_orders(None)
        res2 = structuring_engine.extract_orders(5555)
        assert isinstance(res1, list)
        assert isinstance(res2, list)

    # Fix 2.5: calculate_procedural_deadline coerce day_type
    def test_case_13_calculate_procedural_deadline_null_day_type(self):
        due1 = structuring_engine.calculate_procedural_deadline("2026-08-03", 5, day_type=None)
        due2 = structuring_engine.calculate_procedural_deadline("2026-08-03", 5, day_type=12345)
        assert isinstance(due1, str)
        assert isinstance(due2, str)

    # Fix 2.6: extract_deadlines with raw dicts and None items in orders
    def test_case_14_extract_deadlines_raw_dicts_and_null_orders(self):
        orders = [
            None,
            {"order_type": "intimacao", "deadline_days": 10, "command_summary": "Intime-se o réu", "deadline_type": "business_days"},
        ]
        deadlines = structuring_engine.extract_deadlines(orders, [])
        assert len(deadlines) >= 1
        assert deadlines[0].deadline_days == 10

    # Fix 3.1: _detect_visual_triggers coerce text
    def test_case_15_detect_visual_triggers_null_or_int_text(self):
        trig1 = text_cascade._detect_visual_triggers(None)
        trig2 = text_cascade._detect_visual_triggers(12345)
        assert isinstance(trig1, list)
        assert isinstance(trig2, list)

    # Fix 3.2: extract_page_cascade null page_input
    def test_case_16_extract_page_cascade_null_page_input(self):
        page = text_cascade.extract_page_cascade(None)
        assert isinstance(page, text_cascade.ExtractedPage)
        assert page.extracted_text is not None

    # Fix 3.3: extract_page_cascade dict with text=None
    def test_case_17_extract_page_cascade_dict_with_null_text(self):
        page = text_cascade.extract_page_cascade({"text": None, "blocks": None})
        assert isinstance(page, text_cascade.ExtractedPage)
        assert page.extracted_text is not None

    # Fix 3.4: extract_text_cascade page list with nulls, non-dicts, and None input
    def test_case_18_extract_text_cascade_list_with_nulls_and_none_input(self):
        res1 = text_cascade.extract_text_cascade([None, 123, {"text": None}, "Texto válido", ["nested"]])
        res2 = text_cascade.extract_text_cascade(None)
        assert isinstance(res1, text_cascade.TextCascadeResult)
        assert isinstance(res2, text_cascade.TextCascadeResult)
        assert res1.total_pages == 5
        assert res2.total_pages == 1



class TestNewRemediation2EdgeCases:
    """Stress tests verifying input boundary condition sanitizations in M3 Remediation 3."""

    def test_structure_process_non_string_extracted_text(self):
        """Verify structure_process handles integer, float, dict, and boolean extracted_text without TypeError."""
        sp_int = structuring_engine.structure_process({"pages": [{"extracted_text": 123}]})
        assert isinstance(sp_int, structuring_engine.StructuredProcess)

        sp_float = structuring_engine.structure_process({"pages": [{"extracted_text": 45.6}]})
        assert isinstance(sp_float, structuring_engine.StructuredProcess)

        sp_dict = structuring_engine.structure_process({"pages": [{"extracted_text": {"key": "val"}}]})
        assert isinstance(sp_dict, structuring_engine.StructuredProcess)

        sp_bool = structuring_engine.structure_process({"pages": [{"extracted_text": True}]})
        assert isinstance(sp_bool, structuring_engine.StructuredProcess)

    def test_case_19_extract_page_cascade_custom_visuals_nulls_and_non_dicts(self):
        """Verify custom_visuals containing None, non-dict, or dict with None type are handled cleanly."""
        res1 = text_cascade.extract_page_cascade("text", force_tier="tier4_multimodal_vision", custom_visuals=[None])
        assert isinstance(res1, text_cascade.ExtractedPage)

        res2 = text_cascade.extract_page_cascade("text", force_tier="tier4_multimodal_vision", custom_visuals=[{"type": None}])
        assert isinstance(res2, text_cascade.ExtractedPage)

        res3 = text_cascade.extract_page_cascade("text", force_tier="tier4_multimodal_vision", custom_visuals=["not_a_dict"])
        assert isinstance(res3, text_cascade.ExtractedPage)

    def test_case_20_calculate_procedural_deadline_null_and_string_days(self):
        """Verify calculate_procedural_deadline with None or non-int days handles safely without TypeError."""
        due1 = structuring_engine.calculate_procedural_deadline("2026-01-01", None)
        assert isinstance(due1, str)

        deadlines = structuring_engine.extract_deadlines([{"deadline_days": "invalid"}], [])
        assert isinstance(deadlines, list)

    def test_case_21_structured_process_to_dict_non_callable_to_dict(self):
        """Verify StructuredProcess.to_dict handles list items with non-callable to_dict attribute."""
        class BadParty:
            to_dict = None
        sp = structuring_engine.StructuredProcess(parties=[BadParty()])
        res = sp.to_dict()
        assert isinstance(res, dict)

    def test_case_22_detect_conflicts_to_dict_returning_none(self):
        """Verify detect_conflicts handles structured_process where to_dict() returns None."""
        class MockProcess:
            def to_dict(self):
                return None
        report = conflict_engine.detect_conflicts(MockProcess())
        assert isinstance(report, conflict_engine.ConflictReport)

    def test_case_23_check_party_conflicts_non_callable_to_dict(self):
        """Verify _check_party_conflicts handles party items with non-callable to_dict attribute."""
        class MockParty:
            to_dict = None
        conflicts = conflict_engine._check_party_conflicts([MockParty()])
        assert isinstance(conflicts, list)



