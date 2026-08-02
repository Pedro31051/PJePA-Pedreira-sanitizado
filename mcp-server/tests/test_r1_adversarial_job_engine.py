"""Adversarial & High-Concurrency Stress Test Suite for DurableJobEngine (`job_engine.py`).

Empirically stress-tests:
1. 20+ concurrent workers attempting to claim_next_job() / claim_job() simultaneously.
2. 50+ request coalescing callers attempting submit_job() concurrently for same CNJ/job_type.
3. Stale worker reaper & heartbeat timeout crash recovery.
4. Forced process restart recovery via step checkpoint persistence.
5. Mixed database contention under heavy concurrent read/write transactions in SQLite WAL mode.
"""
from __future__ import annotations

import concurrent.futures
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Set

from job_engine import DurableJobEngine


class AdversarialJobEngineStressTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_durable_jobs.db"
        self.engine = DurableJobEngine(db_path=self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_high_concurrency_worker_claims_25_workers_100_jobs(self):
        """Stress test 25 workers claiming 100 jobs simultaneously.
        Verifies exactly 1 worker acquires each job (0 double claims, 0 lost jobs).
        """
        total_jobs = 100
        num_workers = 25

        # Seed 100 pending jobs
        job_ids = []
        for i in range(total_jobs):
            jid = self.engine.submit_job(
                job_type="stress_analysis",
                process_cnj=f"000{i:04d}-00.2026.8.14.0000",
                grau="1g",
                payload={"index": i},
                allow_coalesce=False,
            )
            job_ids.append(jid)

        self.assertEqual(len(job_ids), total_jobs)

        claimed_records: List[Dict[str, Any]] = []

        def worker_loop(worker_index: int):
            worker_id = f"worker_{worker_index:02d}"
            local_claimed = []
            while True:
                claimed = self.engine.claim_job(worker_id=worker_id)
                if not claimed:
                    break
                local_claimed.append(claimed)
            return local_claimed

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [
                executor.submit(worker_loop, w_idx) for w_idx in range(num_workers)
            ]
            for future in concurrent.futures.as_completed(futures):
                claimed_records.extend(future.result())

        # Assertions
        self.assertEqual(
            len(claimed_records),
            total_jobs,
            f"Expected exactly {total_jobs} total job claims, got {len(claimed_records)}",
        )

        claimed_ids = [r["job_id"] for r in claimed_records]
        unique_claimed_ids = set(claimed_ids)

        self.assertEqual(
            len(unique_claimed_ids),
            total_jobs,
            f"Race condition detected! {total_jobs - len(unique_claimed_ids)} duplicate job claims occurred.",
        )

        # Verify DB state for all jobs
        all_db_jobs = self.engine.list_jobs(job_type="stress_analysis", limit=200)
        self.assertEqual(len(all_db_jobs), total_jobs)
        for job in all_db_jobs:
            self.assertEqual(job["status"], "RUNNING")
            self.assertIsNotNone(job["worker_id"])
            self.assertTrue(job["worker_id"].startswith("worker_"))

    def test_request_coalescing_50_concurrent_callers_same_job(self):
        """Stress test Request Coalescing under 50 concurrent submit_job() calls
        for the exact same (job_type, process_cnj, grau).
        Verifies all 50 callers receive the EXACT SAME job_id and exactly 1 DB record is created.
        """
        num_callers = 50
        target_cnj = "0007777-88.2026.8.14.0000"
        job_type = "pdf_consolidation"
        grau = "1g"

        def submit_caller(caller_id: int) -> str:
            return self.engine.submit_job(
                job_type=job_type,
                process_cnj=target_cnj,
                grau=grau,
                payload={"caller": caller_id},
                allow_coalesce=True,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_callers) as executor:
            futures = [
                executor.submit(submit_caller, c_idx) for c_idx in range(num_callers)
            ]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        self.assertEqual(len(results), num_callers)
        unique_job_ids = set(results)

        # Empirical assertion for request coalescing
        self.assertEqual(
            len(unique_job_ids),
            1,
            f"Request Coalescing failed! Expected 1 unique job_id for 50 parallel requests, got {len(unique_job_ids)}: {unique_job_ids}",
        )

        # Check DB record count
        matching_jobs = self.engine.list_jobs(
            job_type=job_type, process_cnj=target_cnj, limit=10
        )
        self.assertEqual(
            len(matching_jobs),
            1,
            f"Expected 1 job row in DB for coalesced request, found {len(matching_jobs)}",
        )

    def test_request_coalescing_50_concurrent_callers_multi_cnj(self):
        """Stress test 50 callers submitting jobs across 5 distinct CNJs (10 callers per CNJ).
        Verifies each CNJ produces exactly 1 job_id across its 10 concurrent callers.
        """
        cnjs = [f"000888{i}-00.2026.8.14.0000" for i in range(5)]
        num_callers = 50

        def submit_caller(caller_id: int) -> tuple[str, str]:
            target_cnj = cnjs[caller_id % len(cnjs)]
            jid = self.engine.submit_job(
                job_type="multi_cnj_test",
                process_cnj=target_cnj,
                grau="1g",
                allow_coalesce=True,
            )
            return target_cnj, jid

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_callers) as executor:
            futures = [
                executor.submit(submit_caller, c_idx) for c_idx in range(num_callers)
            ]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        cnj_to_jobs: Dict[str, Set[str]] = {cnj: set() for cnj in cnjs}
        for cnj, jid in results:
            cnj_to_jobs[cnj].add(jid)

        for cnj, job_set in cnj_to_jobs.items():
            self.assertEqual(
                len(job_set),
                1,
                f"CNJ {cnj} failed coalescing! Got multiple job_ids: {job_set}",
            )

        all_jobs = self.engine.list_jobs(job_type="multi_cnj_test", limit=50)
        self.assertEqual(len(all_jobs), 5)

    def test_stale_worker_reaper_and_crash_recovery(self):
        """Test stale worker reaper: simulate worker crash by setting heartbeat_at to 60s ago
        and running reap_stale_workers(). Verify job re-queues as PENDING with incremented retry_count,
        and fails when max_retries is reached.
        """
        job_id = self.engine.submit_job(
            job_type="crash_recovery_test",
            process_cnj="0001111-22.2026.8.14.0000",
            grau="1g",
            max_retries=2,
        )

        # Worker 1 claims job
        claimed = self.engine.claim_job(worker_id="worker_dead_1", job_id=job_id)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["status"], "RUNNING")
        self.assertEqual(claimed["retry_count"], 0)

        # Simulate Worker 1 crash: set heartbeat_at to 60 seconds ago
        stale_time_iso = "2026-07-31T20:00:00+00:00"
        with self.engine._connect() as conn:
            conn.execute(
                "UPDATE durable_jobs SET heartbeat_at = ? WHERE job_id = ?",
                (stale_time_iso, job_id),
            )

        # Run reaper
        reaped = self.engine.reap_stale_workers(stale_threshold_seconds=30.0)
        self.assertEqual(reaped, 1)

        # Job should now be PENDING with retry_count = 1
        j_after_reap1 = self.engine.get_job(job_id)
        self.assertEqual(j_after_reap1["status"], "PENDING")
        self.assertIsNone(j_after_reap1["worker_id"])
        self.assertEqual(j_after_reap1["retry_count"], 1)

        # Worker 2 claims job
        claimed_2 = self.engine.claim_job(worker_id="worker_alive_2", job_id=job_id)
        self.assertIsNotNone(claimed_2)
        self.assertEqual(claimed_2["worker_id"], "worker_alive_2")

        # Simulate Worker 2 crash (reach max retries = 2)
        with self.engine._connect() as conn:
            conn.execute(
                "UPDATE durable_jobs SET heartbeat_at = ? WHERE job_id = ?",
                (stale_time_iso, job_id),
            )

        # Run reaper again -> retry_count (1) < max_retries (2) -> retries to 2
        reaped2 = self.engine.reap_stale_workers(stale_threshold_seconds=30.0)
        self.assertEqual(reaped2, 1)

        j_after_reap2 = self.engine.get_job(job_id)
        self.assertEqual(j_after_reap2["status"], "PENDING")
        self.assertEqual(j_after_reap2["retry_count"], 2)

        # Worker 3 claims job
        claimed_3 = self.engine.claim_job(worker_id="worker_dead_3", job_id=job_id)
        self.assertIsNotNone(claimed_3)

        # Simulate Worker 3 crash -> retry_count (2) >= max_retries (2) -> FAILED
        with self.engine._connect() as conn:
            conn.execute(
                "UPDATE durable_jobs SET heartbeat_at = ? WHERE job_id = ?",
                (stale_time_iso, job_id),
            )

        reaped3 = self.engine.reap_stale_workers(stale_threshold_seconds=30.0)
        self.assertEqual(reaped3, 1)

        j_final = self.engine.get_job(job_id)
        self.assertEqual(j_final["status"], "FAILED")
        self.assertIn("max retries reached", j_final["error"]["message"])

    def test_process_restart_checkpoint_recovery(self):
        """Test forced process restart recovery: save checkpoints mid-job, simulate process kill,
        and verify job resumption resumes from saved checkpoint_json.
        """
        job_id = self.engine.submit_job(
            job_type="checkpoint_restart_test",
            process_cnj="0005555-44.2026.8.14.0000",
            grau="1g",
        )

        claimed = self.engine.claim_job(worker_id="worker_proc_1", job_id=job_id)
        self.assertIsNotNone(claimed)

        # Save Checkpoint 1 (download step)
        ckpt_1 = {
            "step": "downloaded_docs",
            "downloaded_doc_ids": ["101", "102", "103"],
            "progress_pct": 50.0,
        }
        ok1 = self.engine.save_checkpoint(job_id, ckpt_1)
        self.assertTrue(ok1)

        # Save Checkpoint 2 (OCR step)
        ckpt_2 = {
            "step": "ocr_processing",
            "downloaded_doc_ids": ["101", "102", "103"],
            "ocr_pages_completed": [1, 2, 3, 4],
            "progress_pct": 75.0,
        }
        ok2 = self.engine.save_checkpoint(job_id, ckpt_2)
        self.assertTrue(ok2)

        # SIMULATE PROCESS RESTART: Destroy self.engine reference and create new engine instance
        del self.engine
        restarted_engine = DurableJobEngine(db_path=self.db_path)

        # Inspect checkpoint after process restart
        retrieved_ckpt = restarted_engine.get_checkpoint(job_id)
        self.assertIsNotNone(retrieved_ckpt)
        self.assertEqual(retrieved_ckpt["step"], "ocr_processing")
        self.assertEqual(retrieved_ckpt["ocr_pages_completed"], [1, 2, 3, 4])
        self.assertEqual(retrieved_ckpt["progress_pct"], 75.0)

        # Resume job processing from checkpoint and complete
        complete_res = {"status": "success", "total_pages": 4, "pdf_sha256": "abc123hash"}
        ok_comp = restarted_engine.complete_job(job_id, complete_res)
        self.assertTrue(ok_comp)

        final_job = restarted_engine.get_job(job_id)
        self.assertEqual(final_job["status"], "COMPLETED")
        self.assertEqual(final_job["result"]["pdf_sha256"], "abc123hash")

        self.engine = restarted_engine

    def test_heavy_mixed_workload_database_contention(self):
        """Stress test 30 concurrent threads performing mixed submit, claim, heartbeat,
        checkpoint, complete, fail, and reap operations against SQLite WAL database.
        Verifies zero database lock errors or corruption.
        """
        num_threads = 30
        duration_s = 3.0
        start_time = time.time()
        errors: List[Exception] = []

        def mixed_worker(t_id: int):
            try:
                while time.time() - start_time < duration_s:
                    # 1. Submit job
                    jid = self.engine.submit_job(
                        job_type="mixed_stress",
                        process_cnj=f"000{t_id:02d}-00.2026.8.14.0000",
                        grau="1g",
                        allow_coalesce=False,
                    )
                    # 2. Claim job
                    c = self.engine.claim_job(worker_id=f"mix_w_{t_id}", job_id=jid)
                    if c:
                        # 3. Heartbeat
                        self.engine.heartbeat(jid, f"mix_w_{t_id}")
                        # 4. Checkpoint
                        self.engine.save_checkpoint(jid, {"thread": t_id, "t": time.time()})
                        # 5. Complete or Fail
                        if t_id % 2 == 0:
                            self.engine.complete_job(jid, {"ok": True})
                        else:
                            self.engine.fail_job(jid, "Simulated worker error")
                    # 6. Reap stale workers periodically
                    if t_id == 0:
                        self.engine.reap_stale_workers(stale_threshold_seconds=1.0)
            except Exception as e:
                errors.append(e)

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(mixed_worker, t) for t in range(num_threads)]
            concurrent.futures.wait(futures)

        self.assertEqual(
            len(errors),
            0,
            f"Database contention errors occurred during mixed workload: {errors}",
        )


if __name__ == "__main__":
    unittest.main()
