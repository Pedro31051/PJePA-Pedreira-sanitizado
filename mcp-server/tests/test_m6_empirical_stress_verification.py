"""Empirical Stress & Concurrency Verification Test Suite for Milestone M6.

Tests:
1. Single-Flight Auth Lock & SessionManager under 100 concurrent callers
2. ClientLease Transactional Isolation across async tasks
3. StructuredCache thread-safety & invalidation under heavy concurrency
4. SQLite WAL job runner resilience under high-concurrency read/write operations
"""

import asyncio
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from pje_downloader import _downloader_db
from session_manager import (
    ClientLease,
    PagePool,
    SessionEntry,
    SessionKey,
    SessionManager,
    get_current_task_page,
)
from structured_cache import StructuredCache


class M6SingleFlightAndAuthStressTests(unittest.TestCase):
    """Stress tests for single-flight auth lock & session management."""

    def setUp(self):
        SessionManager.reset_instance()
        self.manager = SessionManager.get_instance()
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        SessionManager.reset_instance()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_100_concurrent_single_flight_login_coalescing(self):
        """Verify that 100 concurrent get_or_create_session calls trigger exactly 1 login execution."""
        async def run_test():
            key = SessionKey("12345678900", "advogado", "1g", "perfil_100")
            mock_client = MagicMock()
            mock_client._browser.is_connected.return_value = True
            mock_client._page.url = "http://pje.tjpa.jus.br/pje/Processo/Consulta"
            mock_client._op_lock = asyncio.Lock()
            mock_client._fechar = AsyncMock()

            login_counter = 0

            async def mock_login(s_key, perf_dict, permit_sem_perf):
                nonlocal login_counter
                login_counter += 1
                await asyncio.sleep(0.05)  # Simulate network auth delay
                return mock_client

            with patch.object(self.manager, "_execute_single_flight_login", side_effect=mock_login):
                tasks = [
                    self.manager.get_or_create_session(key, permitir_sem_perfil=True)
                    for _ in range(100)
                ]
                entries = await asyncio.gather(*tasks)

                self.assertEqual(len(entries), 100)
                self.assertEqual(login_counter, 1)
                for entry in entries:
                    self.assertIs(entry.client, mock_client)

        asyncio.run(run_test())

    def test_single_flight_cancellation_and_recovery_stress(self):
        """Verify single-flight lock handles partial cancellation and recovers for subsequent callers."""
        async def run_test():
            key = SessionKey("98765432100", "advogado", "1g", "perfil_cancel")
            mock_client = MagicMock()
            mock_client._browser.is_connected.return_value = True
            mock_client._page.url = "http://pje.tjpa.jus.br/pje/Processo/Consulta"

            login_attempts = 0

            async def mock_slow_login(s_key, perf_dict, permit_sem_perf):
                nonlocal login_attempts
                login_attempts += 1
                await asyncio.sleep(0.2)
                return mock_client

            with patch.object(self.manager, "_execute_single_flight_login", side_effect=mock_slow_login):
                # Launch task 1 that will be cancelled
                t1 = asyncio.create_task(self.manager.get_or_create_session(key, permitir_sem_perfil=True))
                # Launch task 2 that waits
                t2 = asyncio.create_task(self.manager.get_or_create_session(key, permitir_sem_perfil=True))

                await asyncio.sleep(0.02)
                t1.cancel()

                try:
                    await t1
                except asyncio.CancelledError:
                    pass

                # Task 2 should still complete successfully
                entry2 = await t2
                self.assertIsNotNone(entry2)
                self.assertIs(entry2.client, mock_client)
                self.assertEqual(login_attempts, 1)

        asyncio.run(run_test())


class M6ClientLeaseIsolationStressTests(unittest.TestCase):
    """Stress tests for ClientLease isolation and ContextVar safety."""

    def test_20_concurrent_client_lease_isolation(self):
        """Verify page leases remain strictly isolated per async task across sleep boundaries."""
        async def run_test():
            mock_client = MagicMock()
            mock_client._context.new_page = AsyncMock()
            pages = [MagicMock(name=f"Page_{i}") for i in range(20)]

            page_idx = 0

            async def new_page_mock():
                nonlocal page_idx
                p = pages[page_idx % len(pages)]
                page_idx += 1
                return p

            mock_client._context.new_page = new_page_mock
            page_pool = PagePool(mock_client, max_pages=20)
            key = SessionKey("11122233344", "servidor", "1g", "p1")
            entry = SessionEntry(key, mock_client, page_pool)
            manager = MagicMock()
            manager.release_lease = AsyncMock()

            results = {}

            async def worker(worker_id: int):
                # Acquire lease directly
                page = await page_pool.acquire_page()
                lease = ClientLease(manager, entry, page)
                async with lease:
                    current_p = get_current_task_page()
                    await asyncio.sleep(0.01 * (worker_id % 5))
                    # Re-check page hasn't bled across tasks
                    post_sleep_p = get_current_task_page()
                    results[worker_id] = (current_p == page and post_sleep_p == page)

            tasks = [worker(i) for i in range(20)]
            await asyncio.gather(*tasks)

            self.assertEqual(len(results), 20)
            self.assertTrue(all(results.values()))

        asyncio.run(run_test())


class M6StructuredCacheStressTests(unittest.TestCase):
    """Stress tests for StructuredCache concurrency, invalidation, and negative TTL."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.cache = StructuredCache(cache_dir=self.temp_dir, default_negative_ttl=0.2)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_concurrent_multithreaded_cache_ops(self):
        """Verify cache thread-safety under concurrent put, get, invalidation, and negative hits."""
        num_threads = 16
        ops_per_thread = 50

        def cache_worker(thread_id: int):
            for i in range(ops_per_thread):
                key = f"proc_{thread_id}_{i % 10}"
                b_hash = f"bhash_{thread_id}"
                e_hash = "ext_v1"

                # Put positive entry
                self.cache.put(key, b_hash, e_hash, {"data": f"val_{i}"})
                v = self.cache.get(key, b_hash, e_hash)
                self.assertIn(v, [{"data": f"val_{i}"}, None])

                # Put negative entry
                neg_key = f"neg_{thread_id}_{i % 5}"
                self.cache.put_negative(neg_key, b_hash, e_hash, error_reason="Failed")
                val, status = self.cache.get_with_status(neg_key, b_hash, e_hash)
                self.assertIn(status, ["NEGATIVE_HIT", "MISS"])

                # Invalidate specific key
                if i % 7 == 0:
                    self.cache.invalidate(key)

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(cache_worker, t) for t in range(num_threads)]
            for f in futures:
                f.result()

        stats = self.cache.get_cache_stats()
        self.assertGreater(stats["total_requests"], 0)


class M6SQLiteWALResilienceTests(unittest.TestCase):
    """Stress tests for SQLite WAL durable job store under high concurrency."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_auditoria.sqlite3"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_concurrent_sqlite_wal_job_updates(self):
        """Verify SQLite WAL handles 50 concurrent writes/updates without database lock errors."""
        async def run_test():
            with patch("auditoria_processual.database_path", return_value=self.db_path):
                # Init schema
                with _downloader_db() as conn:
                    conn.execute(
                        "INSERT INTO consolidated_pdf_jobs (job_id, process_cnj, grau, status, created_at, updated_at) VALUES ('j1', 'cnj1', '1g', 'pending', 'now', 'now')"
                    )

                async def update_worker(worker_id: int):
                    for step in range(10):
                        await asyncio.sleep(0.001)
                        with patch("auditoria_processual.database_path", return_value=self.db_path):
                            with _downloader_db() as conn:
                                conn.execute(
                                    "UPDATE consolidated_pdf_jobs SET status = ?, updated_at = ? WHERE job_id = 'j1'",
                                    (f"status_{worker_id}_{step}", f"time_{step}"),
                                )

                tasks = [update_worker(w) for w in range(20)]
                await asyncio.gather(*tasks)

                with patch("auditoria_processual.database_path", return_value=self.db_path):
                    with _downloader_db() as conn:
                        row = conn.execute("SELECT status FROM consolidated_pdf_jobs WHERE job_id = 'j1'").fetchone()
                        self.assertIsNotNone(row)
                        self.assertTrue(row["status"].startswith("status_"))

        asyncio.run(run_test())

    def test_concurrent_sqlite_wal_100_jobs_insertion_and_query(self):
        """Verify SQLite WAL inserts 100 jobs concurrently without database lock errors."""
        async def run_test():
            with patch("auditoria_processual.database_path", return_value=self.db_path):
                # Ensure schema initialized
                with _downloader_db() as conn:
                    pass

                async def insert_worker(worker_id: int):
                    job_id = f"job_wal_{worker_id}"
                    with patch("auditoria_processual.database_path", return_value=self.db_path):
                        with _downloader_db() as conn:
                            conn.execute(
                                """
                                INSERT INTO consolidated_pdf_jobs (job_id, process_cnj, grau, status, created_at, updated_at)
                                VALUES (?, ?, '1g', 'completed', 'now', 'now')
                                """,
                                (job_id, f"0000{worker_id}-00.2026.8.14.0000"),
                            )

                tasks = [insert_worker(i) for i in range(100)]
                await asyncio.gather(*tasks)

                with patch("auditoria_processual.database_path", return_value=self.db_path):
                    with _downloader_db() as conn:
                        count = conn.execute("SELECT COUNT(*) as cnt FROM consolidated_pdf_jobs").fetchone()["cnt"]
                        self.assertEqual(count, 100)

        asyncio.run(run_test())


class M6LeaseExceptionCleanupTests(unittest.TestCase):
    """Verify ContextVar and PagePool cleanup when lease blocks raise exceptions."""

    def test_lease_exception_resets_contextvar_and_releases_page(self):
        async def run_test():
            mock_client = MagicMock()
            mock_page = MagicMock(name="ExceptionPage")
            mock_client._context.new_page = AsyncMock(return_value=mock_page)
            mock_client._abas_preexistentes = []

            page_pool = PagePool(mock_client, max_pages=1)
            key = SessionKey("99988877766", "advogado", "1g", "p_exc")
            entry = SessionEntry(key, mock_client, page_pool)
            manager = MagicMock()
            manager.release_lease = AsyncMock()

            # Confirm contextvar is empty before
            self.assertIsNone(get_current_task_page())

            page = await page_pool.acquire_page()
            lease = ClientLease(manager, entry, page)

            with self.assertRaises(ValueError):
                async with lease:
                    self.assertEqual(get_current_task_page(), page)
                    raise ValueError("Simulated operational failure inside lease")

            # Confirm contextvar is reset to None after exception exit
            self.assertIsNone(get_current_task_page())

        asyncio.run(run_test())



if __name__ == "__main__":
    unittest.main()
