"""Testes unitários e de integração do motor de lotes (Batch Analysis Engine)."""

import asyncio
import base64
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

complete = importlib.import_module("analise_processual_completa")
batch_engine = importlib.import_module("batch_engine")


class BatchAnalysisEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_storage = os.environ.get("PJE_STORAGE_DIR")
        self.old_key = os.environ.get("PJE_AUDIT_MASTER_KEY")
        os.environ["PJE_STORAGE_DIR"] = self.temp_dir.name
        os.environ["PJE_AUDIT_MASTER_KEY"] = base64.urlsafe_b64encode(
            b"s" * 32
        ).decode()
        
        # Garante a criação do banco e das tabelas
        with complete._database() as conn:
            pass

    def tearDown(self):
        if self.old_storage is None:
            os.environ.pop("PJE_STORAGE_DIR", None)
        else:
            os.environ["PJE_STORAGE_DIR"] = self.old_storage
        if self.old_key is None:
            os.environ.pop("PJE_AUDIT_MASTER_KEY", None)
        else:
            os.environ["PJE_AUDIT_MASTER_KEY"] = self.old_key
        self.temp_dir.cleanup()

    def test_criar_e_adicionar_ao_lote(self):
        batch_id = batch_engine.criar_lote()
        self.assertTrue(batch_id.startswith("batch_"))

        # Adiciona processos ao lote
        cnjs = ["0001111-11.2026.8.14.0001", "0002222-22.2026.8.14.0001"]
        res = batch_engine.adicionar_ao_lote(batch_id, cnjs, grau="1g", persona="servidor")
        self.assertEqual(res["adicionados"], 2)
        self.assertEqual(res["ignorados"], 0)

        # Adiciona duplicado e invalido
        res_dup = batch_engine.adicionar_ao_lote(batch_id, ["0001111-11.2026.8.14.0001", ""], grau="1g", persona="servidor")
        self.assertEqual(res_dup["adicionados"], 0)
        self.assertEqual(res_dup["ignorados"], 1)

        # Confirma no status do lote
        status = batch_engine.status_lote(batch_id)
        self.assertEqual(status["total_processes"], 2)
        self.assertEqual(status["status"], "queued")
        self.assertEqual(status["queued"], 2)
        self.assertEqual(len(status["items"]), 2)

    def test_iniciar_e_retomar_lote(self):
        batch_id = batch_engine.criar_lote()
        batch_engine.adicionar_ao_lote(batch_id, ["0001111-11.2026.8.14.0001"])
        
        # Inicia o lote com autorização e force_reread
        res = batch_engine.iniciar_lote(batch_id, authorisation_ref="ref-teste-123", force_reread=True)
        self.assertEqual(res["status"], "running")

        # Verifica persistência das propriedades do lote
        with complete._database() as conn:
            cur = conn.execute("SELECT authorisation_ref, force_reread, status FROM analysis_batches WHERE batch_id = ?", (batch_id,))
            row = cur.fetchone()
            self.assertEqual(row["authorisation_ref"], "ref-teste-123")
            self.assertEqual(row["force_reread"], 1)
            self.assertEqual(row["status"], "running")

    def test_cancelar_lote(self):
        batch_id = batch_engine.criar_lote()
        batch_engine.adicionar_ao_lote(batch_id, ["0001111-11.2026.8.14.0001", "0002222-22.2026.8.14.0001"])
        
        # Inicia
        batch_engine.iniciar_lote(batch_id, authorisation_ref="ref-cancel")

        # Cancela
        res_cancel = batch_engine.cancelar_lote(batch_id)
        self.assertEqual(res_cancel["status"], "cancelled")

        status = batch_engine.status_lote(batch_id)
        self.assertEqual(status["status"], "cancelled")
        self.assertEqual(status["cancelled"], 2)
        self.assertEqual(status["queued"], 0)

    def test_scheduler_queue_items(self):
        batch_id = batch_engine.criar_lote()
        batch_engine.adicionar_ao_lote(batch_id, ["0001111-11.2026.8.14.0001", "0002222-22.2026.8.14.0001"])
        
        # Sem iniciar o lote, obter_proximos_itens_fila deve retornar vazio
        proximos = batch_engine.obter_proximos_itens_fila(2)
        self.assertEqual(len(proximos), 0)

        # Inicia o lote
        batch_engine.iniciar_lote(batch_id, authorisation_ref="ref-sched")
        
        # Agora deve retornar
        proximos = batch_engine.obter_proximos_itens_fila(1)
        self.assertEqual(len(proximos), 1)
        self.assertEqual(proximos[0]["process_cnj"], "0001111-11.2026.8.14.0001")


class AsyncSchedulerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_storage = os.environ.get("PJE_STORAGE_DIR")
        self.old_key = os.environ.get("PJE_AUDIT_MASTER_KEY")
        os.environ["PJE_STORAGE_DIR"] = self.temp_dir.name
        os.environ["PJE_AUDIT_MASTER_KEY"] = base64.urlsafe_b64encode(
            b"s" * 32
        ).decode()
        
        with complete._database() as conn:
            pass

    async def asyncTearDown(self):
        if self.old_storage is None:
            os.environ.pop("PJE_STORAGE_DIR", None)
        else:
            os.environ["PJE_STORAGE_DIR"] = self.old_storage
        if self.old_key is None:
            os.environ.pop("PJE_AUDIT_MASTER_KEY", None)
        else:
            os.environ["PJE_AUDIT_MASTER_KEY"] = self.old_key
        self.temp_dir.cleanup()

    async def test_scheduler_loop_execution(self):
        batch_id = batch_engine.criar_lote()
        batch_engine.adicionar_ao_lote(batch_id, ["0001111-11.2026.8.14.0001"])
        batch_engine.iniciar_lote(batch_id, authorisation_ref="ref-async", force_reread=False)

        agendados = []
        tasks_mock = {}

        def mock_agendar(job_id):
            agendados.append(job_id)
            # Simula a criação de uma task que já está rodando
            tasks_mock[job_id] = asyncio.create_task(asyncio.sleep(0.5))
            return True

        # Executa uma única iteração do loop do scheduler simulando
        # a lógica de _processar_fila_de_lotes_loop
        max_workers = 1
        ativos = sum(1 for t in tasks_mock.values() if not t.done())
        
        self.assertEqual(ativos, 0)
        
        proximos = batch_engine.obter_proximos_itens_fila(max_workers - ativos)
        self.assertEqual(len(proximos), 1)

        for item in proximos:
            b_id = item["batch_id"]
            cnj = item["process_cnj"]
            grau = item["grau"]
            persona = item["persona"]

            with complete._database() as conn:
                cur = conn.execute(
                    "SELECT authorisation_ref, force_reread FROM analysis_batches WHERE batch_id = ?",
                    (b_id,)
                )
                batch_row = cur.fetchone()
            
            auth_ref = batch_row["authorisation_ref"] if batch_row else "lote_process_analysis"
            force_reread = bool(batch_row["force_reread"]) if batch_row else False

            job_individual = complete.create_job(
                process_number=cnj,
                grau=grau,
                persona=persona,
                mode="integral",
                semantic_enabled=False,
                force_reread=force_reread,
                authorisation_ref=auth_ref
            )
            job_id = str(job_individual["job_id"])

            now = complete._now_iso()
            with complete._database() as conn:
                conn.execute(
                    """
                    UPDATE analysis_batch_items
                    SET job_id = ?, status = 'running', updated_at = ?
                    WHERE batch_id = ? AND process_cnj = ? AND grau = ?
                    """,
                    (job_id, now, b_id, cnj, grau)
                )

            mock_agendar(job_id)

        self.assertEqual(len(agendados), 1)
        
        # Verifica se o item do lote foi movido para 'running'
        status = batch_engine.status_lote(batch_id)
        self.assertEqual(status["running"], 1)
        self.assertEqual(status["queued"], 0)
        self.assertEqual(status["items"][0]["job_id"], agendados[0])

        # Simula a conclusão do job e a sincronização do lote
        job_id = agendados[0]
        complete.update_job(job_id, status="completed", phase="completed")

        batch_engine.sincronizar_status_itens_lote()

        status_after = batch_engine.status_lote(batch_id)
        self.assertEqual(status_after["completed"], 1)
        self.assertEqual(status_after["running"], 0)
        self.assertEqual(status_after["status"], "completed")

    async def test_scheduler_refills_slot_as_soon_as_one_job_exits(self):
        old_workers = os.environ.get("PJE_MAX_BROWSER_WORKERS")
        os.environ["PJE_MAX_BROWSER_WORKERS"] = "2"
        tasks = {}

        def schedule(job_id):
            tasks[job_id] = asyncio.create_task(asyncio.sleep(60))
            return True

        try:
            batch_id = batch_engine.criar_lote(
                mode="rapida",
                max_concurrency=2,
            )
            batch_engine.adicionar_ao_lote(
                batch_id,
                [
                    "0001111-11.2026.8.14.0001",
                    "0002222-22.2026.8.14.0001",
                    "0003333-33.2026.8.14.0001",
                ],
            )
            batch_engine.iniciar_lote(
                batch_id,
                authorisation_ref="ref-parallel",
            )

            first = await batch_engine.processar_ciclo_fila_lotes(schedule, tasks)
            self.assertEqual(first["started"], 2)
            status = batch_engine.status_lote(batch_id)
            self.assertEqual(status["running"], 2)
            self.assertEqual(status["queued"], 1)
            self.assertEqual(status["available_slots"], 0)

            exiting_job = status["items"][0]["job_id"]
            complete.update_job(exiting_job, status="completed", phase="completed")
            tasks[exiting_job].cancel()
            await asyncio.gather(tasks[exiting_job], return_exceptions=True)

            refill = await batch_engine.processar_ciclo_fila_lotes(schedule, tasks)
            self.assertEqual(refill["started"], 1)
            refilled_status = batch_engine.status_lote(batch_id)
            self.assertEqual(refilled_status["completed"], 1)
            self.assertEqual(refilled_status["running"], 2)
            self.assertEqual(refilled_status["queued"], 0)
            running_jobs = [
                item for item in refilled_status["items"] if item["status"] == "running"
            ]
            self.assertTrue(all(complete.get_job(item["job_id"])["mode"] == "rapida" for item in running_jobs))
        finally:
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
            if old_workers is None:
                os.environ.pop("PJE_MAX_BROWSER_WORKERS", None)
            else:
                os.environ["PJE_MAX_BROWSER_WORKERS"] = old_workers
