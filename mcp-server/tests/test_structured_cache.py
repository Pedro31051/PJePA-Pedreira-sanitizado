"""Unit tests for StructuredCache (`test_structured_cache.py`).

Tests cache hit, cache miss, dual-hash invalidation on code/byte changes,
negative cache expiration, and cache stats tracking.
"""

from __future__ import annotations

import time
from pathlib import Path

from structured_cache import StructuredCache, compute_hash


def test_cache_hit_and_miss(tmp_path: Path):
    cache = StructuredCache(cache_dir=tmp_path / "cache_store")
    
    key = "doc_123"
    byte_hash = compute_hash("dossier_bytes_v1")
    extractor_hash = compute_hash("extractor_v1")
    value = {"extracted_text": "Petição Inicial do Inventário"}

    # Initially cache miss
    val = cache.get(key, byte_hash, extractor_hash)
    assert val is None
    
    # Store in cache
    cache.put(key, byte_hash, extractor_hash, value)

    # Now cache hit
    val = cache.get(key, byte_hash, extractor_hash)
    assert val == value

    stats = cache.get_cache_stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["negative_hits"] == 0
    assert stats["hit_ratio"] == 0.5


def test_dual_hash_invalidation(tmp_path: Path):
    cache = StructuredCache(cache_dir=tmp_path / "cache_store")

    key = "doc_456"
    byte_hash_1 = compute_hash("dossier_bytes_v1")
    byte_hash_2 = compute_hash("dossier_bytes_v2")
    extractor_hash_1 = compute_hash("extractor_v1")
    extractor_hash_2 = compute_hash("extractor_v2")
    value = {"entities": ["Autor", "Réu"]}

    cache.put(key, byte_hash_1, extractor_hash_1, value)

    # Change byte_hash -> should invalidate
    val_b = cache.get(key, byte_hash_2, extractor_hash_1)
    assert val_b is None

    # Re-store
    cache.put(key, byte_hash_1, extractor_hash_1, value)

    # Change extractor_hash -> should invalidate
    val_e = cache.get(key, byte_hash_1, extractor_hash_2)
    assert val_e is None


def test_negative_cache_expiration(tmp_path: Path):
    cache = StructuredCache(cache_dir=tmp_path / "cache_store", default_negative_ttl=0.2)

    key = "failed_doc_789"
    byte_hash = compute_hash("corrupted_bytes")
    extractor_hash = compute_hash("extractor_v1")

    # Put negative entry with short TTL = 0.2s
    cache.put_negative(key, byte_hash, extractor_hash, error_reason="PDF corrupted", ttl=0.2)

    # Immediately check: should be negative hit
    val, status = cache.get_with_status(key, byte_hash, extractor_hash)
    assert val is None
    assert status == "NEGATIVE_HIT"
    assert cache.is_negative(key, byte_hash, extractor_hash) is True

    # Sleep past TTL
    time.sleep(0.25)

    # Check after TTL: should be expired -> MISS
    val_after, status_after = cache.get_with_status(key, byte_hash, extractor_hash)
    assert val_after is None
    assert status_after == "MISS"
    assert cache.is_negative(key, byte_hash, extractor_hash) is False


def test_cache_stats_tracking(tmp_path: Path):
    cache = StructuredCache(cache_dir=tmp_path / "cache_store")
    
    b_hash = compute_hash("data")
    e_hash = compute_hash("code")

    # 1 Miss
    cache.get("k1", b_hash, e_hash)
    
    # Put k1 and get -> 1 Hit
    cache.put("k1", b_hash, e_hash, "val1")
    cache.get("k1", b_hash, e_hash)

    # Put negative k2 and get -> 1 Negative Hit
    cache.put_negative("k2", b_hash, e_hash, reason="Error", ttl=600)
    cache.get("k2", b_hash, e_hash)

    stats = cache.get_cache_stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["negative_hits"] == 1
    assert stats["total_requests"] == 3
    assert stats["hit_ratio"] == round(2 / 3, 4)
