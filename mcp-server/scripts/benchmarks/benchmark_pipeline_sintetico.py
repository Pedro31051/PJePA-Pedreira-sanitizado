#!/usr/bin/env python3
"""Mede batching/streaming sintético sem criar acervo ou acessar o PJe."""

from __future__ import annotations

import argparse
import json
import platform
import resource
import statistics
import time
import tracemalloc


def batches(total: int, batch_size: int):
    for start in range(0, total, batch_size):
        yield range(start, min(start + batch_size, total))


def run(total: int, batch_size: int, repetitions: int) -> dict:
    durations = []
    checksum = 0
    tracemalloc.start()
    for _ in range(repetitions):
        started = time.perf_counter()
        for batch in batches(total, batch_size):
            checksum ^= sum((item * 2654435761) & 0xFFFFFFFF for item in batch)
        durations.append(time.perf_counter() - started)
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    median = statistics.median(durations)
    ordered = sorted(durations)
    p95_index = max(0, min(len(ordered) - 1, round(0.95 * len(ordered)) - 1))
    return {
        "method": "synthetic-streaming-batches",
        "scope": "infraestrutura de lotes; não mede rede, browser, OCR ou PJe",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "records": total,
        "batch_size": batch_size,
        "repetitions": repetitions,
        "median_seconds": round(median, 6),
        "p95_seconds": round(ordered[p95_index], 6),
        "throughput_records_per_second": round(total / median, 2),
        "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "peak_tracemalloc_kib": round(peak_bytes / 1024, 2),
        "checksum": checksum,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", type=int, default=100_000)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--repetitions", type=int, default=5)
    args = parser.parse_args()
    if min(args.records, args.batch_size, args.repetitions) <= 0:
        raise SystemExit("todos os parâmetros devem ser positivos")
    print(json.dumps(run(args.records, args.batch_size, args.repetitions), sort_keys=True))
