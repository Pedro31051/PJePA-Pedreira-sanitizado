"""Testes sintéticos do auditor processual consultivo."""

import asyncio
import base64
import gzip
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import auditoria_processual as audit
import caixas_tarefas
import server


def policy() -> dict:
    return {
        "schema_version": "pje.task-policy/v1",
        "bundle_id": "55555555-5555-4555-8555-555555555555",
        "version": "synthetic-1",
        "generated_at": "2026-07-27T19:00:00Z",
        "_file_sha256": "b" * 64,
        "tasks": [
            {
                "id": "task.providencias",
                "canonical_name": "Providências a adotar",
                "aliases": [
                    "Providencias a adotar",
                    "Verificar providência a adotar",
                ],
                "flow_type": "synthetic",
                "allowed_destination_ids": ["task.cumprimento"],
                "mutually_exclusive_with": ["task.suspenso"],
            },
            {
                "id": "task.cumprimento",
                "canonical_name": "Cumprimento de ato",
                "aliases": [],
                "flow_type": "synthetic",
                "allowed_destination_ids": [],
                "mutually_exclusive_with": [],
            },
            {
                "id": "task.suspenso",
                "canonical_name": "Processo suspenso",
                "aliases": [],
                "flow_type": "synthetic",
                "allowed_destination_ids": [],
                "mutually_exclusive_with": ["task.providencias"],
            },
        ],
        "sources": [
            {
                "id": "source.synthetic",
                "title": "Fonte sintética",
                "kind": "unit_playbook",
                "uri": "synthetic://source",
                "version": "1",
                "valid_from": "2026-01-01",
                "valid_until": None,
                "sha256": "a" * 64,
                "authority_priority": 100,
            }
        ],
        "rules": [
            {
                "id": "rule.providencias",
                "scope": {
                    "tribunal": "TJPA-SYNTHETIC",
                    "environment": "first_degree",
                    "unit_pattern": "Unidade sintética",
                    "role_pattern": "Servidor sintético",
                    "case_class_codes": [],
                    "flow_types": ["synthetic"],
                },
                "current_task_id": "task.providencias",
                "allowed_destination_ids": ["task.cumprimento"],
                "required_conditions": [
                    {
                        "fact": "pending_specific_order",
                        "operator": "equals",
                        "value": True,
                    }
                ],
                "blocking_conditions": [
                    {
                        "fact": "case_suspended",
                        "operator": "equals",
                        "value": True,
                    }
                ],
                "evidence_requirements": [
                    {
                        "fact": "pending_specific_order",
                        "minimum_count": 1,
                        "required": True,
                    }
                ],
                "next_acts": [
                    {
                        "id": "act.synthetic",
                        "sequence": 1,
                        "label": "Cumprir ordem sintética",
                        "responsible_role": "Servidor sintético",
                        "precondition": "Ordem pendente",
                        "expected_effect": "Cumprimento sintético",
                    }
                ],
                "source_ids": ["source.synthetic"],
                "priority": 100,
                "status": "approved",
                "outcome": "move",
                "valid_from": "2026-01-01",
                "valid_until": None,
                "approved_by": "Responsável sintético",
                "approved_at": "2026-07-27T18:00:00Z",
            }
        ],
    }


def snapshot() -> dict:
    return {
        "snapshot_id": "snapshot-synthetic",
        "tribunal": "TJPA",
        "grau": "1g",
        "persona": "advogado",
        "status": "completo",
        "cobertura_percentual": 100.0,
        "finalizado_em": "2026-07-27T18:00:00+00:00",
        "metadados": {
            "lotacao_selecionada": ("Servidor sintético - Unidade sintética")
        },
    }


def occurrence() -> dict:
    return {
        "chave_ocorrencia": "occurrence-synthetic",
        "numero_processo": "0000000-00.0000.0.00.0000",
        "tarefa": "Providências a adotar",
        "classe_judicial": "CLASSE-SINTETICA",
        "prioridade": False,
    }


def dossier(*, complete: bool = True, fact: bool = True) -> dict:
    facts = {
        "case_suspended": [
            {
                "value": False,
                "citations": [
                    {
                        "source_type": "document",
                        "document_id": "doc-state-synthetic",
                        "page": 1,
                        "excerpt": "Ausência sintética de suspensão vigente.",
                        "sha256": "d" * 64,
                        "confidence": 1.0,
                    }
                ],
            }
        ]
    }
    if fact:
        facts["pending_specific_order"] = [
            {
                "value": True,
                "citations": [
                    {
                        "source_type": "document",
                        "document_id": "doc-synthetic",
                        "page": 1,
                        "excerpt": "Trecho inteiramente sintético.",
                        "sha256": "c" * 64,
                        "confidence": 1.0,
                    }
                ],
            }
        ]
    return {
        "schema_version": "pje.process-dossier/v1",
        "process_number": occurrence()["numero_processo"],
        "occurrence_key": occurrence()["chave_ocorrencia"],
        "snapshot_id": snapshot()["snapshot_id"],
        "parallel_tasks": ["Providências a adotar"],
        "facts": facts,
        "documents": [],
        "gaps": [] if complete else ["cobertura sintética parcial"],
        "coverage": {"complete": complete},
        "extractor_versions": {"facts": "synthetic/v1"},
    }


class PolicyEvaluationTests(unittest.TestCase):
    def test_confirmed_mismatch_requires_complete_cited_evidence(self):
        finding = audit.evaluate_dossier(
            occurrence(),
            dossier(),
            policy(),
            snapshot(),
        )

        self.assertEqual(finding["status"], "confirmed_mismatch")
        self.assertEqual(finding["finding_type"], "wrong_box")
        self.assertEqual(
            finding["recommended_destination"],
            "Cumprimento de ato",
        )
        self.assertEqual(finding["confidence"], 1.0)
        self.assertTrue(finding["supporting_evidence"])

    def test_partial_coverage_never_confirms_mismatch(self):
        finding = audit.evaluate_dossier(
            occurrence(),
            dossier(complete=False),
            policy(),
            snapshot(),
        )

        self.assertEqual(finding["status"], "probable_mismatch")
        self.assertLess(finding["confidence"], 0.9)

    def test_missing_fact_abstains(self):
        finding = audit.evaluate_dossier(
            occurrence(),
            dossier(fact=False),
            policy(),
            snapshot(),
        )

        self.assertEqual(finding["status"], "insufficient_evidence")
        self.assertIn("pending_specific_order", finding["abstention_reason"])

    def test_unknown_blocking_fact_also_abstains(self):
        process_dossier = dossier()
        process_dossier["facts"].pop("case_suspended")

        finding = audit.evaluate_dossier(
            occurrence(),
            process_dossier,
            policy(),
            snapshot(),
        )

        self.assertEqual(finding["status"], "insufficient_evidence")
        self.assertIn("case_suspended", finding["abstention_reason"])

    def test_legitimate_parallel_task_is_preserved_without_false_duplicate(self):
        process_dossier = dossier()
        process_dossier["parallel_tasks"] = [
            "Providências a adotar",
            "Tarefa paralela não exclusiva",
        ]

        finding = audit.evaluate_dossier(
            occurrence(),
            process_dossier,
            policy(),
            snapshot(),
        )

        self.assertEqual(finding["status"], "confirmed_mismatch")
        self.assertEqual(finding["finding_type"], "wrong_box")

    def test_mutually_exclusive_parallel_task_is_not_deduplicated(self):
        process_dossier = dossier()
        process_dossier["parallel_tasks"] = [
            "Providências a adotar",
            "Processo suspenso",
        ]

        finding = audit.evaluate_dossier(
            occurrence(),
            process_dossier,
            policy(),
            snapshot(),
        )

        self.assertEqual(finding["status"], "confirmed_mismatch")
        self.assertEqual(finding["finding_type"], "invalid_parallel_task")
        self.assertIsNone(finding["recommended_destination"])

    def test_equal_priority_conflicting_destinations_abstain(self):
        conflicting_policy = policy()
        conflicting_policy["tasks"][0]["allowed_destination_ids"].append(
            "task.alternativa"
        )
        conflicting_policy["tasks"].append(
            {
                "id": "task.alternativa",
                "canonical_name": "Destino alternativo",
                "aliases": [],
                "flow_type": "synthetic",
                "allowed_destination_ids": [],
                "mutually_exclusive_with": [],
            }
        )
        second_rule = {
            **conflicting_policy["rules"][0],
            "id": "rule.providencias.alternativa",
            "allowed_destination_ids": ["task.alternativa"],
        }
        conflicting_policy["rules"].append(second_rule)

        finding = audit.evaluate_dossier(
            occurrence(),
            dossier(),
            conflicting_policy,
            snapshot(),
        )

        self.assertEqual(finding["status"], "insufficient_evidence")
        self.assertEqual(len(finding["conflicting_rules"]), 2)

    def test_specific_order_has_priority_over_general_playbook(self):
        ordered_policy = policy()
        ordered_policy["rules"][0]["priority"] = 900
        general_rule = {
            **ordered_policy["rules"][0],
            "id": "rule.providencias.geral",
            "priority": 10,
            "outcome": "stay",
            "allowed_destination_ids": [],
            "next_acts": [
                {
                    "id": "act.general",
                    "sequence": 1,
                    "label": "Ato geral sintético",
                    "responsible_role": "Servidor sintético",
                    "precondition": None,
                    "expected_effect": "Permanência sintética",
                }
            ],
        }
        ordered_policy["rules"].append(general_rule)

        finding = audit.evaluate_dossier(
            occurrence(),
            dossier(),
            ordered_policy,
            snapshot(),
        )

        self.assertEqual(
            finding["applied_rules"],
            ["rule.providencias"],
        )
        self.assertEqual(finding["status"], "confirmed_mismatch")

    def test_expired_rule_produces_policy_gap(self):
        expired_policy = policy()
        expired_policy["rules"][0]["valid_until"] = "2026-01-02"

        finding = audit.evaluate_dossier(
            occurrence(),
            dossier(),
            expired_policy,
            snapshot(),
        )

        self.assertEqual(finding["status"], "policy_gap")

    def test_suspension_blocks_general_destination(self):
        process_dossier = dossier()
        process_dossier["facts"]["case_suspended"][0]["value"] = True

        finding = audit.evaluate_dossier(
            occurrence(),
            process_dossier,
            policy(),
            snapshot(),
        )

        self.assertEqual(finding["status"], "insufficient_evidence")
        self.assertIsNone(finding["recommended_destination"])

    def test_correct_box_returns_ordered_next_act(self):
        stay_policy = policy()
        stay_policy["rules"][0]["outcome"] = "stay"
        stay_policy["rules"][0]["allowed_destination_ids"] = []

        finding = audit.evaluate_dossier(
            occurrence(),
            dossier(),
            stay_policy,
            snapshot(),
        )

        self.assertEqual(finding["status"], "consistent")
        self.assertEqual(finding["finding_type"], "next_act")
        self.assertEqual(finding["next_steps"][0]["sequence"], 1)

    def test_snapshot_validation_checks_unit_and_approval(self):
        result = audit.validate_snapshot_and_policy(
            snapshot(),
            policy(),
            task_name="Providências a adotar",
            policy_version="synthetic-1",
            requested_unit="Servidor sintético - Unidade sintética",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["approved_rules"], 1)


class PolicyGuardrailTests(unittest.TestCase):
    def _family_policy_and_snapshot(self):
        family_policy = policy()
        family_policy["tasks"][0]["canonical_name"] = "Verificar providência a adotar"
        family_policy["tasks"][0]["aliases"] = [
            "Providências a adotar",
            "Providencias a adotar",
        ]
        family_policy["rules"][0]["scope"] = {
            "tribunal": "TJPA",
            "environment": "first_degree",
            "orgao_julgador_id": "916",
            "unit_canonical_segment": (
                "Vara de Família, Sucessões e Registros Públicos de Marabá"
            ),
            "unit_aliases": ["Vara de Família"],
            "unit_pattern": (
                "^Vara de Família, Sucessões e Registros Públicos de Marabá$"
            ),
            "role_pattern": "^Diretor de Secretaria$",
            "case_class_codes": [],
            "flow_types": ["familia_maraba"],
        }
        family_snapshot = snapshot()
        family_snapshot["metadados"]["lotacao_selecionada"] = (
            "Vara de Família, Sucessões e Registros Públicos de Marabá / "
            "Secretaria Vara Cível / Diretor de Secretaria"
        )
        family_snapshot["ocorrencias_coletadas"] = 1
        family_snapshot["orgaos_julgadores"] = [
            {
                "id_orgao_julgador": "916",
                "orgao_julgador": (
                    "Vara de Família, Sucessões e Registros Públicos de Marabá"
                ),
                "ocorrencias": 1,
            }
        ]
        return family_policy, family_snapshot

    def test_allowlist_matches_task_identity_through_aliases(self):
        self.assertTrue(
            audit.task_matches_allowlist(
                policy(),
                "Verificar providência a adotar",
                ["Providências a adotar"],
            )
        )
        self.assertFalse(
            audit.task_matches_allowlist(
                policy(),
                "Verificar providência a adotar",
                ["Caixa inexistente"],
            )
        )

    def test_rejects_ambiguous_task_alias(self):
        ambiguous = policy()
        ambiguous["tasks"][1]["aliases"] = ["Providências a adotar"]

        with self.assertRaisesRegex(audit.AuditError, "ambíguo"):
            audit._validate_policy(ambiguous)

    def test_unit_alias_requires_proven_org_identity(self):
        family_policy, family_snapshot = self._family_policy_and_snapshot()

        accepted = audit.validate_snapshot_and_policy(
            family_snapshot,
            family_policy,
            task_name="Providências a adotar",
            policy_version="synthetic-1",
            requested_unit="Vara de Família",
        )
        self.assertTrue(accepted["ok"])

        rejected = audit.validate_snapshot_and_policy(
            family_snapshot,
            family_policy,
            task_name="Providências a adotar",
            policy_version="synthetic-1",
            requested_unit="Secretaria Vara Cível",
        )
        self.assertFalse(rejected["ok"])
        self.assertIn(
            "lotação solicitada difere da lotação do snapshot",
            rejected["blockers"],
        )

    def test_approved_tjpa_rule_requires_unit_identity(self):
        incomplete = policy()
        incomplete["rules"][0]["scope"]["tribunal"] = "TJPA"

        with self.assertRaisesRegex(audit.AuditError, "identidade da unidade"):
            audit._validate_policy(incomplete)

    def test_latest_movement_facts_are_cited_without_inferring_state(self):
        facts = audit.extract_structured_facts(
            {},
            [
                {
                    "data": "27 jul 2026",
                    "tipo": "Redistribuição",
                    "descricao": "Audiência mencionada na certidão",
                }
            ],
            [],
        )

        self.assertTrue(facts["latest_movement_mentions_redistribution"][0]["value"])
        self.assertTrue(facts["latest_movement_mentions_hearing"][0]["value"])
        citation = facts["latest_movement_mentions_redistribution"][0]["citations"][0]
        self.assertEqual(citation["timeline_order"], "latest_first")
        self.assertIn("Audiência", citation["excerpt"])

    def test_draft_family_policy_is_valid_but_not_approved(self):
        path = (
            Path(__file__).resolve().parents[1] / "policies/familia_maraba_draft_0.json"
        )
        loaded = audit.load_policy(str(path))

        self.assertEqual(loaded["version"], "familia-maraba-draft-0")
        self.assertFalse(any(rule["status"] == "approved" for rule in loaded["rules"]))


class EncryptedPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        key = base64.urlsafe_b64encode(b"k" * 32).decode("ascii")
        self.environment = patch.dict(
            os.environ,
            {
                "PJE_STORAGE_DIR": self.temp.name,
                "PJE_AUDIT_MASTER_KEY": key,
            },
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def test_result_dossier_review_and_export_are_encrypted(self):
        job = audit.create_job(
            snapshot_id="snapshot-synthetic",
            task_name="Providências a adotar",
            policy_version="synthetic-1",
            policy_sha256="b" * 64,
            authorisation_ref="authorisation-hash",
            unit_name="Unidade sintética",
            total_items=1,
        )
        process_dossier = dossier()
        dossier_meta = audit.save_dossier(process_dossier)
        finding = audit.evaluate_dossier(
            occurrence(),
            process_dossier,
            policy(),
            snapshot(),
        )
        finding.update(dossier_meta)
        finding_id = audit.save_finding(job["job_id"], finding)

        listing = audit.list_findings(job_id=job["job_id"])
        self.assertEqual(listing["pagination"]["total_items"], 1)
        self.assertNotIn(
            occurrence()["numero_processo"],
            json.dumps(listing, ensure_ascii=False),
        )

        explanation = audit.explain_finding(finding_id=finding_id)
        self.assertEqual(
            explanation["process_number"],
            occurrence()["numero_processo"],
        )

        review = audit.register_review(
            finding_id=finding_id,
            decision="accepted",
            reviewer="Revisor sintético",
            justification="Validação sintética.",
        )
        self.assertFalse(review["writes_to_pje"])
        self.assertEqual(len(audit.list_reviews(finding_id)), 1)

        report = audit.export_report(
            job["job_id"],
            ttl_hours=1,
            authorisation_ref="authorisation-hash",
        )
        path = audit.audit_export_path(report["export_id"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(report["records"], 1)
        self.assertTrue(report["encrypted_at_rest"])
        self.assertNotEqual(path.read_bytes()[:2], b"\x1f\x8b")
        decrypted = audit.read_export(
            report["export_id"],
            "authorisation-hash",
        )
        with gzip.open(io.BytesIO(decrypted), "rt", encoding="utf-8") as stream:
            exported_payload = json.load(stream)
        self.assertEqual(len(exported_payload["findings"]), 1)
        with self.assertRaises(audit.AuditError):
            audit.read_export(report["export_id"], "wrong-authorisation")

        database_bytes = audit.database_path().read_bytes()
        self.assertNotIn(
            occurrence()["numero_processo"].encode("utf-8"),
            database_bytes,
        )
        self.assertNotIn(
            b"Trecho inteiramente sintetico",
            database_bytes,
        )

        with closing(sqlite3.connect(audit.database_path())) as connection:
            connection.execute(
                "UPDATE audit_exports SET expires_at=? WHERE export_id=?",
                ("2000-01-01T00:00:00+00:00", report["export_id"]),
            )
            connection.commit()
        with self.assertRaises(audit.AuditError):
            audit.audit_export_path(report["export_id"])
        self.assertFalse(path.exists())


class McpAuditToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.policy_path = Path(self.temp.name) / "policy.json"
        policy_to_write = policy()
        policy_to_write.pop("_file_sha256")
        self.policy_path.write_text(
            json.dumps(policy_to_write, ensure_ascii=False),
            encoding="utf-8",
        )
        key = base64.urlsafe_b64encode(b"z" * 32).decode("ascii")
        self.environment = patch.dict(
            os.environ,
            {
                "PJE_STORAGE_DIR": self.temp.name,
                "PJE_AUDIT_MASTER_KEY": key,
                "PJE_TASK_POLICY_PATH": str(self.policy_path),
            },
        )
        self.environment.start()
        task = {
            "grupo": "tarefas",
            # Nome efetivamente observado no painel da Vara de Família.
            # O playbook sintético conserva o rótulo histórico como canônico
            # para provar que o servidor resolve aliases antes da allowlist.
            "nome": "Verificar providência a adotar",
            "quantidade": 1,
        }
        self.snapshot_id = caixas_tarefas.novo_snapshot(
            "1g",
            "advogado",
            [task],
            {
                "lotacao_solicitada": ("Servidor sintético - Unidade sintética"),
                "lotacao_selecionada": ("Servidor sintético - Unidade sintética"),
                "somente_leitura": True,
            },
        )
        caixas_tarefas.salvar_caixa(
            self.snapshot_id,
            task,
            1,
            [
                {
                    "idTaskInstance": "task-instance-synthetic",
                    "idProcesso": "process-synthetic",
                    "numeroProcesso": occurrence()["numero_processo"],
                    "classeJudicial": "CLASSE-SINTETICA",
                    "orgaoJulgador": "Unidade sintética",
                    "assuntoPrincipal": "Assunto sintético",
                    "dataChegada": 1722100000000,
                    "ultimoMovimento": 1722100000000,
                    "descricaoUltimoMovimento": "Movimento sintético",
                    "sigiloso": False,
                    "prioridade": False,
                    "conferido": False,
                    "temParteMoradorDeRua": False,
                    "tagsProcessoList": [],
                }
            ],
        )
        caixas_tarefas.finalizar_snapshot(self.snapshot_id)

    async def asyncTearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    async def test_plan_reads_snapshot_without_opening_process(self):
        result = await server.auditar_fluxo_processual_pje(
            acao="planejar_auditoria",
            snapshot_id=self.snapshot_id,
            nome_tarefa="Providências a adotar",
            playbook_version="synthetic-1",
            lotacao="Servidor sintético - Unidade sintética",
            persona="advogado",
        )

        self.assertTrue(result["ready"])
        self.assertEqual(result["scope"]["process_occurrences"], 1)
        self.assertTrue(result["read_only"])

    async def test_start_fails_closed_without_explicit_authorisation(self):
        result = await server.auditar_fluxo_processual_pje(
            acao="iniciar_auditoria_caixa",
            snapshot_id=self.snapshot_id,
            nome_tarefa="Providências a adotar",
            playbook_version="synthetic-1",
            persona="advogado",
        )

        self.assertIn("autorizacao_leitura", result["erro"])
        self.assertTrue(result["read_only"])

    async def test_start_accepts_observed_task_alias_and_persists_canonical_name(
        self,
    ):
        with patch.object(
            server,
            "_executar_job_auditoria",
            new_callable=AsyncMock,
        ):
            kwargs = dict(
                acao="iniciar_auditoria_caixa",
                snapshot_id=self.snapshot_id,
                nome_tarefa="Verificar providência a adotar",
                playbook_version="synthetic-1",
                lotacao="Servidor sintético - Unidade sintética",
                autorizacao_leitura=True,
                autorizacao_ref="autorização sintética",
                retomar=False,
                persona="advogado",
            )
            result = await server.auditar_fluxo_processual_pje(**kwargs)

        await asyncio.sleep(0)
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["task_name"], "Providências a adotar")

    async def test_capability_inventory_includes_audit_tool(self):
        result = await server.inventario_capacidades(filtro="auditoria")

        names = {item["ferramenta"] for item in result["ferramentas"]}
        self.assertIn("auditar_fluxo_processual_pje", names)


if __name__ == "__main__":
    unittest.main()
